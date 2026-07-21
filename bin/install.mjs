#!/usr/bin/env node

import { spawnSync } from "node:child_process";
import {
  copyFileSync,
  cpSync,
  lstatSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readlinkSync,
  realpathSync,
  renameSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { homedir } from "node:os";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const PACKAGE_NAME = "@dylanmccavitt/skills";
const SKILLS = ["gepetto", "pinocchio", "jiminy", "checkpoint"];
const MANAGED_DIRECTORIES = [...SKILLS, "hooks"];
const packageRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");

function readJson(path) {
  return JSON.parse(readFileSync(path, "utf8"));
}

function pathExists(path) {
  try {
    lstatSync(path);
    return true;
  } catch (error) {
    if (error?.code === "ENOENT") return false;
    throw error;
  }
}

function shellPythonAvailable() {
  const result = spawnSync("python3", ["--version"], { encoding: "utf8" });
  if (result.error?.code === "ENOENT") {
    throw new Error("Python 3 is required by the orchestration hooks but was not found on PATH.");
  }
  if (result.status !== 0) {
    throw new Error(`Unable to run Python 3: ${result.stderr.trim() || `exit ${result.status}`}`);
  }
}

function managedLink(linkPath, targetPath) {
  if (!pathExists(linkPath)) return false;
  if (!lstatSync(linkPath).isSymbolicLink()) return false;
  return resolve(dirname(linkPath), readlinkSync(linkPath)) === resolve(targetPath);
}

function preflightSkillLinks(skillsRoot, installRoot) {
  for (const skill of SKILLS) {
    const linkPath = join(skillsRoot, skill);
    const targetPath = join(installRoot, skill);
    if (pathExists(linkPath) && !managedLink(linkPath, targetPath)) {
      throw new Error(`Refusing to replace existing skill: ${linkPath}`);
    }
  }
}

function parseExistingHooks(hooksPath) {
  if (!pathExists(hooksPath)) return { hooks: {} };
  if (lstatSync(hooksPath).isSymbolicLink()) {
    throw new Error(`Refusing to replace symlinked hook configuration: ${hooksPath}`);
  }
  const config = readJson(hooksPath);
  if (!config || typeof config !== "object" || Array.isArray(config)) {
    throw new Error(`Hook configuration must be a JSON object: ${hooksPath}`);
  }
  if (config.hooks === undefined) config.hooks = {};
  if (!config.hooks || typeof config.hooks !== "object" || Array.isArray(config.hooks)) {
    throw new Error(`The hooks property must be a JSON object: ${hooksPath}`);
  }
  return config;
}

function mergeHooks(existing, managed) {
  const merged = structuredClone(existing);
  for (const [event, entries] of Object.entries(managed.hooks)) {
    const current = Array.isArray(merged.hooks[event]) ? merged.hooks[event] : [];
    const serialized = new Set(current.map((entry) => JSON.stringify(entry)));
    for (const entry of entries) {
      if (!serialized.has(JSON.stringify(entry))) current.push(entry);
    }
    merged.hooks[event] = current;
  }
  return merged;
}

function writeHooks(hooksPath, config) {
  mkdirSync(dirname(hooksPath), { recursive: true });
  if (pathExists(hooksPath)) {
    const backup = `${hooksPath}.backup-${new Date().toISOString().replaceAll(":", "-")}`;
    copyFileSync(hooksPath, backup);
  }
  const temporary = `${hooksPath}.tmp-${process.pid}`;
  writeFileSync(temporary, `${JSON.stringify(config, null, 2)}\n`, { mode: 0o600 });
  renameSync(temporary, hooksPath);
}

function stagePackage(sourceRoot, codexHome, version) {
  const staging = mkdtempSync(join(codexHome, ".orchestration-skills-"));
  for (const directory of MANAGED_DIRECTORIES) {
    cpSync(join(sourceRoot, directory), join(staging, directory), { recursive: true });
  }
  writeFileSync(
    join(staging, ".codex-orchestration-install.json"),
    `${JSON.stringify({ package: PACKAGE_NAME, version }, null, 2)}\n`,
  );
  return staging;
}

export function installSuite({ codexHome, sourceRoot = packageRoot } = {}) {
  if (process.platform === "win32") {
    throw new Error("This installer currently supports macOS and Linux. Windows support is not yet available.");
  }
  shellPythonAvailable();

  const resolvedHome = resolve(codexHome || process.env.CODEX_HOME || join(homedir(), ".codex"));
  const installRoot = join(resolvedHome, "orchestration-skills");
  const skillsRoot = join(resolvedHome, "skills");
  const hooksPath = join(resolvedHome, "hooks.json");
  const packageJson = readJson(join(sourceRoot, "package.json"));

  mkdirSync(resolvedHome, { recursive: true });
  mkdirSync(skillsRoot, { recursive: true });
  preflightSkillLinks(skillsRoot, installRoot);
  const existingHooks = parseExistingHooks(hooksPath);
  const managedHooks = readJson(join(sourceRoot, "hooks", "hooks.json"));
  const mergedHooks = mergeHooks(existingHooks, managedHooks);

  if (pathExists(installRoot)) {
    const markerPath = join(installRoot, ".codex-orchestration-install.json");
    if (!pathExists(markerPath) || readJson(markerPath).package !== PACKAGE_NAME) {
      throw new Error(`Refusing to replace unmanaged directory: ${installRoot}`);
    }
  }

  const staging = stagePackage(sourceRoot, resolvedHome, packageJson.version);
  const previous = `${installRoot}.previous-${process.pid}`;
  try {
    if (pathExists(installRoot)) renameSync(installRoot, previous);
    renameSync(staging, installRoot);

    for (const skill of SKILLS) {
      const linkPath = join(skillsRoot, skill);
      const targetPath = join(installRoot, skill);
      if (pathExists(linkPath)) rmSync(linkPath);
      symlinkSync(relative(skillsRoot, targetPath), linkPath, "dir");
    }
    writeHooks(hooksPath, mergedHooks);
    if (pathExists(previous)) rmSync(previous, { recursive: true });
  } catch (error) {
    for (const skill of SKILLS) {
      const linkPath = join(skillsRoot, skill);
      if (managedLink(linkPath, join(installRoot, skill))) rmSync(linkPath);
    }
    if (pathExists(installRoot)) rmSync(installRoot, { recursive: true });
    if (pathExists(previous)) renameSync(previous, installRoot);
    if (pathExists(installRoot)) {
      for (const skill of SKILLS) {
        symlinkSync(relative(skillsRoot, join(installRoot, skill)), join(skillsRoot, skill), "dir");
      }
    }
    if (pathExists(staging)) rmSync(staging, { recursive: true });
    throw error;
  }

  return { codexHome: resolvedHome, installRoot, skills: SKILLS };
}

function resolveCodexHome(codexHome) {
  return resolve(codexHome || process.env.CODEX_HOME || join(homedir(), ".codex"));
}

function managedHookEntries(sourceRoot) {
  return readJson(join(sourceRoot, "hooks", "hooks.json")).hooks;
}

export function uninstallSuite({ codexHome, sourceRoot = packageRoot } = {}) {
  const resolvedHome = resolveCodexHome(codexHome);
  const installRoot = join(resolvedHome, "orchestration-skills");
  const skillsRoot = join(resolvedHome, "skills");
  const hooksPath = join(resolvedHome, "hooks.json");
  const removed = [];

  for (const skill of SKILLS) {
    const linkPath = join(skillsRoot, skill);
    if (managedLink(linkPath, join(installRoot, skill))) {
      rmSync(linkPath);
      removed.push(linkPath);
    }
  }

  if (pathExists(installRoot)) {
    const markerPath = join(installRoot, ".codex-orchestration-install.json");
    if (!pathExists(markerPath) || readJson(markerPath).package !== PACKAGE_NAME) {
      throw new Error(`Refusing to remove unmanaged directory: ${installRoot}`);
    }
    rmSync(installRoot, { recursive: true });
    removed.push(installRoot);
  }

  if (pathExists(hooksPath)) {
    const config = parseExistingHooks(hooksPath);
    const managed = managedHookEntries(sourceRoot);
    let changed = false;
    for (const [event, entries] of Object.entries(managed)) {
      if (!Array.isArray(config.hooks[event])) continue;
      const serialized = new Set(entries.map((entry) => JSON.stringify(entry)));
      const kept = config.hooks[event].filter((entry) => !serialized.has(JSON.stringify(entry)));
      if (kept.length !== config.hooks[event].length) {
        changed = true;
        if (kept.length > 0) config.hooks[event] = kept;
        else delete config.hooks[event];
      }
    }
    if (changed) {
      writeHooks(hooksPath, config);
      removed.push(`managed hook entries in ${hooksPath}`);
    }
  }

  return { codexHome: resolvedHome, removed };
}

export function doctorSuite({ codexHome, sourceRoot = packageRoot } = {}) {
  const resolvedHome = resolveCodexHome(codexHome);
  const installRoot = join(resolvedHome, "orchestration-skills");
  const hooksPath = join(resolvedHome, "hooks.json");
  const checks = [];
  const check = (name, ok, detail) => checks.push({ name, ok, detail: ok ? "ok" : detail });

  try {
    shellPythonAvailable();
    check("python3", true);
  } catch (error) {
    check("python3", false, error.message);
  }

  const markerPath = join(installRoot, ".codex-orchestration-install.json");
  if (!pathExists(installRoot)) {
    check("install directory", false, `missing: ${installRoot}`);
  } else if (!pathExists(markerPath)) {
    check("install directory", false, `missing marker: ${markerPath}`);
  } else {
    let marker;
    try {
      marker = readJson(markerPath);
    } catch {
      marker = null;
    }
    check(
      "install directory",
      marker?.package === PACKAGE_NAME,
      `marker does not match ${PACKAGE_NAME}: ${markerPath}`,
    );
  }

  for (const skill of SKILLS) {
    const linkPath = join(resolvedHome, "skills", skill);
    check(
      `skill link ${skill}`,
      managedLink(linkPath, join(installRoot, skill)),
      `missing or unmanaged symlink: ${linkPath}`,
    );
  }

  if (!pathExists(hooksPath)) {
    check("hook configuration", false, `missing: ${hooksPath}`);
  } else if (lstatSync(hooksPath).isSymbolicLink()) {
    check("hook configuration", false, `symlinked: ${hooksPath}`);
  } else {
    let config = null;
    try {
      config = readJson(hooksPath);
    } catch {
      check("hook configuration", false, `unparseable JSON: ${hooksPath}`);
    }
    if (config) {
      check("hook configuration", true);
      const managed = managedHookEntries(sourceRoot);
      for (const [event, entries] of Object.entries(managed)) {
        const current = Array.isArray(config.hooks?.[event]) ? config.hooks[event] : [];
        const serialized = new Set(current.map((entry) => JSON.stringify(entry)));
        check(
          `hook entries ${event}`,
          entries.every((entry) => serialized.has(JSON.stringify(entry))),
          `managed entry missing for ${event}: ${hooksPath}`,
        );
      }
    }
  }

  const problems = checks.filter((entry) => !entry.ok).map((entry) => `${entry.name}: ${entry.detail}`);
  return { ok: problems.length === 0, problems, checks };
}

function parseArguments(arguments_) {
  const options = { command: "install" };
  for (let index = 0; index < arguments_.length; index += 1) {
    const argument = arguments_[index];
    if (argument === "--codex-home") {
      const value = arguments_[index + 1];
      if (!value) throw new Error("--codex-home requires a path");
      options.codexHome = value;
      index += 1;
    } else if (argument === "--help" || argument === "-h") {
      options.help = true;
    } else if (argument === "uninstall" || argument === "doctor") {
      options.command = argument;
    } else {
      throw new Error(`Unknown argument: ${argument}`);
    }
  }
  return options;
}

function printHelp() {
  console.log(`Install Codex orchestration skills\n\nUsage:\n  npx @dylanmccavitt/skills@latest [uninstall|doctor] [--codex-home PATH]\n`);
}

async function main() {
  try {
    const options = parseArguments(process.argv.slice(2));
    if (options.help) {
      printHelp();
      return;
    }
    if (options.command === "uninstall") {
      const result = uninstallSuite(options);
      console.log(
        result.removed.length > 0
          ? `Removed:\n${result.removed.map((entry) => `  ${entry}`).join("\n")}`
          : `Nothing to remove in ${result.codexHome}.`,
      );
      return;
    }
    if (options.command === "doctor") {
      const result = doctorSuite(options);
      for (const entry of result.checks) {
        console.log(`${entry.ok ? "ok" : "problem"} - ${entry.name}${entry.ok ? "" : `: ${entry.detail}`}`);
      }
      if (!result.ok) process.exitCode = 1;
      return;
    }
    const result = installSuite(options);
    console.log(`Installed ${result.skills.join(", ")} in ${result.codexHome}.`);
    console.log("The skills will be available in your next Codex task.");
  } catch (error) {
    console.error(`@dylanmccavitt/skills: ${error.message}`);
    process.exitCode = 1;
  }
}

if (process.argv[1] && realpathSync(process.argv[1]) === fileURLToPath(import.meta.url)) {
  await main();
}
