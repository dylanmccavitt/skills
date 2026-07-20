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

function parseArguments(arguments_) {
  const options = {};
  for (let index = 0; index < arguments_.length; index += 1) {
    const argument = arguments_[index];
    if (argument === "--codex-home") {
      const value = arguments_[index + 1];
      if (!value) throw new Error("--codex-home requires a path");
      options.codexHome = value;
      index += 1;
    } else if (argument === "--help" || argument === "-h") {
      options.help = true;
    } else {
      throw new Error(`Unknown argument: ${argument}`);
    }
  }
  return options;
}

function printHelp() {
  console.log(`Install Codex orchestration skills\n\nUsage:\n  npx @dylanmccavitt/skills@latest [--codex-home PATH]\n`);
}

async function main() {
  try {
    const options = parseArguments(process.argv.slice(2));
    if (options.help) {
      printHelp();
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
