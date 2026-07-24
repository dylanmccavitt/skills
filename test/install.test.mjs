import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, lstatSync, mkdirSync, mkdtempSync, readFileSync, readlinkSync, readdirSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

import { doctorSuite, installSuite, uninstallSuite } from "../bin/install.mjs";

const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");

function temporaryCodexHome() {
  const root = mkdtempSync(join(tmpdir(), "codex-orchestration-test-"));
  const home = join(root, "Codex Home");
  mkdirSync(home, { recursive: true });
  return home;
}

test("installs all skills and preserves existing hooks", () => {
  const codexHome = temporaryCodexHome();
  const hooksPath = join(codexHome, "hooks.json");
  writeFileSync(hooksPath, `${JSON.stringify({ custom: true, hooks: { Stop: [{ hooks: [{ type: "command", command: "existing" }] }] } })}\n`);

  const result = installSuite({ codexHome, sourceRoot: repositoryRoot });

  assert.equal(result.codexHome, codexHome);
  for (const skill of ["gepetto", "implement", "review-gate", "checkpoint", "orchestrate"]) {
    const path = join(codexHome, "skills", skill);
    assert.equal(lstatSync(path).isSymbolicLink(), true);
    assert.equal(existsSync(join(path, "SKILL.md")), true);
  }
  const hooks = JSON.parse(readFileSync(hooksPath, "utf8"));
  assert.equal(hooks.custom, true);
  assert.equal(hooks.hooks.Stop[0].hooks[0].command, "existing");
  assert.equal(hooks.hooks.Stop.length, 1);
  assert.equal(hooks.hooks.SessionStart, undefined);
  assert.equal(
    readdirSync(codexHome).some((name) => name.startsWith("hooks.json.backup-")),
    true,
  );
});

test("installed hook blocks direct delivery in favor of the typed runner", () => {
  const codexHome = temporaryCodexHome();
  installSuite({ codexHome, sourceRoot: repositoryRoot });
  const config = JSON.parse(readFileSync(join(codexHome, "hooks.json"), "utf8"));
  const command = config.hooks.PreToolUse[0].hooks[0].command;

  const result = spawnSync(command, {
    shell: true,
    env: { ...process.env, CODEX_HOME: codexHome },
    input: JSON.stringify({
      hook_event_name: "PreToolUse",
      tool_name: "Bash",
      tool_input: { command: "gh pr merge 42 --repo owner/repo" },
    }),
    encoding: "utf8",
  });

  assert.equal(result.status, 2);
  assert.match(result.stderr, /typed voice_state\.py deliver/);
});

test("runs through an npm-style binary symlink", () => {
  const codexHome = temporaryCodexHome();
  const binRoot = join(dirname(codexHome), "node_modules", ".bin");
  mkdirSync(binRoot, { recursive: true });
  const binary = join(binRoot, "skills");
  symlinkSync(join(repositoryRoot, "bin", "install.mjs"), binary);

  const result = spawnSync(process.execPath, [binary, "--codex-home", codexHome], {
    encoding: "utf8",
  });
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /Installed gepetto, implement, review-gate, checkpoint, orchestrate/);
});

test("repeated installation is idempotent", () => {
  const codexHome = temporaryCodexHome();
  installSuite({ codexHome, sourceRoot: repositoryRoot });
  const first = readFileSync(join(codexHome, "hooks.json"), "utf8");
  installSuite({ codexHome, sourceRoot: repositoryRoot });
  const second = readFileSync(join(codexHome, "hooks.json"), "utf8");
  assert.equal(second, first);
});

test("upgrades a package-owned legacy install as an intentional clean swap", () => {
  const codexHome = temporaryCodexHome();
  const installRoot = join(codexHome, "orchestration-skills");
  const skillsRoot = join(codexHome, "skills");
  mkdirSync(skillsRoot, { recursive: true });
  mkdirSync(join(installRoot, "pinocchio"), { recursive: true });
  mkdirSync(join(installRoot, "jiminy"), { recursive: true });
  writeFileSync(
    join(installRoot, ".codex-orchestration-install.json"),
    `${JSON.stringify({ package: "@dylanmccavitt/skills", version: "0.4.0" })}\n`,
  );
  for (const skill of ["pinocchio", "jiminy"]) {
    symlinkSync(join(installRoot, skill), join(skillsRoot, skill), "dir");
  }
  const legacyCommand =
    '/usr/bin/env python3 "${CODEX_HOME:-$HOME/.codex}/orchestration-skills/hooks/orchestration_hook.py"';
  writeFileSync(
    join(codexHome, "hooks.json"),
    `${JSON.stringify({
      hooks: {
        SessionStart: [{ matcher: "^compact$", hooks: [{ type: "command", command: legacyCommand }] }],
        SubagentStart: [{ matcher: "*", hooks: [{ type: "command", command: legacyCommand }] }],
        SubagentStop: [{ matcher: "*", hooks: [{ type: "command", command: legacyCommand }] }],
        Stop: [{ hooks: [{ type: "command", command: legacyCommand }] }],
        PreToolUse: [
          { matcher: "^(Bash|apply_patch|Edit|Write)$", hooks: [{ type: "command", command: legacyCommand }] },
          {
            matcher: "mixed",
            hooks: [
              { type: "command", command: `sh -c '${legacyCommand}'` },
              { type: "command", command: "preserve-me" },
            ],
          },
        ],
      },
    })}\n`,
  );

  installSuite({ codexHome, sourceRoot: repositoryRoot });

  for (const skill of ["pinocchio", "jiminy"]) {
    assert.throws(() => lstatSync(join(skillsRoot, skill)), { code: "ENOENT" });
  }
  const hooks = JSON.parse(readFileSync(join(codexHome, "hooks.json"), "utf8"));
  const serialized = JSON.stringify(hooks);
  assert.equal(serialized.includes("orchestration_hook.py"), false);
  assert.equal(serialized.includes("preserve-me"), true);
  assert.equal(serialized.includes("voice_state.py"), true);
  for (const event of ["SessionStart", "SubagentStart", "SubagentStop", "Stop"]) {
    assert.equal(hooks.hooks[event], undefined);
  }
});

test("upgrade preserves legacy-named links not owned by this package", () => {
  const codexHome = temporaryCodexHome();
  const installRoot = join(codexHome, "orchestration-skills");
  const skillsRoot = join(codexHome, "skills");
  mkdirSync(skillsRoot, { recursive: true });
  mkdirSync(installRoot, { recursive: true });
  writeFileSync(
    join(installRoot, ".codex-orchestration-install.json"),
    `${JSON.stringify({ package: "@dylanmccavitt/skills", version: "0.4.0" })}\n`,
  );
  const external = join(codexHome, "external-pinocchio");
  mkdirSync(external);
  symlinkSync(external, join(skillsRoot, "pinocchio"), "dir");

  installSuite({ codexHome, sourceRoot: repositoryRoot });

  const link = join(skillsRoot, "pinocchio");
  assert.equal(lstatSync(link).isSymbolicLink(), true);
  assert.equal(resolve(skillsRoot, readlinkSync(link)), external);
});

test("repair install removes an orphan package legacy hook without a marker", () => {
  const codexHome = temporaryCodexHome();
  const legacyCommand =
    '/usr/bin/env python3 "${CODEX_HOME:-$HOME/.codex}/orchestration-skills/hooks/orchestration_hook.py"';
  writeFileSync(
    join(codexHome, "hooks.json"),
    `${JSON.stringify({
      hooks: {
        Stop: [{ hooks: [{ type: "command", command: legacyCommand }] }],
        PreToolUse: [{ matcher: "foreign", hooks: [{ type: "command", command: "preserve-me" }] }],
      },
    })}\n`,
  );

  installSuite({ codexHome, sourceRoot: repositoryRoot });

  const hooks = JSON.parse(readFileSync(join(codexHome, "hooks.json"), "utf8"));
  assert.equal(JSON.stringify(hooks).includes("orchestration_hook.py"), false);
  assert.equal(JSON.stringify(hooks).includes("preserve-me"), true);
});

test("refuses a symlinked package install root even when its target has a marker", () => {
  const codexHome = temporaryCodexHome();
  const external = join(dirname(codexHome), "external-install");
  mkdirSync(external, { recursive: true });
  writeFileSync(
    join(external, ".codex-orchestration-install.json"),
    `${JSON.stringify({ package: "@dylanmccavitt/skills", version: "0.4.0" })}\n`,
  );
  symlinkSync(external, join(codexHome, "orchestration-skills"), "dir");

  assert.throws(
    () => installSuite({ codexHome, sourceRoot: repositoryRoot }),
    /Refusing symlinked install directory/,
  );
});

test("refuses to replace an existing skill", () => {
  const codexHome = temporaryCodexHome();
  mkdirSync(join(codexHome, "skills", "gepetto"), { recursive: true });
  assert.throws(
    () => installSuite({ codexHome, sourceRoot: repositoryRoot }),
    /Refusing to replace existing skill/,
  );
});

test("refuses to replace symlinked hook configuration", () => {
  const codexHome = temporaryCodexHome();
  const target = join(codexHome, "external-hooks.json");
  writeFileSync(target, "{\"hooks\":{}}\n");
  symlinkSync(target, join(codexHome, "hooks.json"));
  assert.throws(
    () => installSuite({ codexHome, sourceRoot: repositoryRoot }),
    /Refusing to replace symlinked hook configuration/,
  );
});

test("uninstall removes managed pieces and preserves foreign entries", () => {
  const codexHome = temporaryCodexHome();
  const hooksPath = join(codexHome, "hooks.json");
  writeFileSync(hooksPath, `${JSON.stringify({ hooks: { Stop: [{ hooks: [{ type: "command", command: "existing" }] }] } })}\n`);
  installSuite({ codexHome, sourceRoot: repositoryRoot });
  const foreignLink = join(codexHome, "skills", "other-skill");
  symlinkSync(join(codexHome, "elsewhere"), foreignLink);

  const result = uninstallSuite({ codexHome, sourceRoot: repositoryRoot });

  assert.equal(result.removed.length > 0, true);
  for (const skill of ["gepetto", "implement", "review-gate", "checkpoint", "orchestrate"]) {
    assert.equal(existsSync(join(codexHome, "skills", skill)), false);
  }
  assert.equal(lstatSync(foreignLink).isSymbolicLink(), true);
  assert.equal(existsSync(join(codexHome, "orchestration-skills")), false);
  const hooks = JSON.parse(readFileSync(hooksPath, "utf8"));
  assert.deepEqual(hooks.hooks.Stop, [{ hooks: [{ type: "command", command: "existing" }] }]);
});

test("uninstall on an empty home is a no-op", () => {
  const codexHome = temporaryCodexHome();
  const result = uninstallSuite({ codexHome, sourceRoot: repositoryRoot });
  assert.deepEqual(result.removed, []);
});

test("uninstall refuses an unmanaged install directory", () => {
  const codexHome = temporaryCodexHome();
  const installRoot = join(codexHome, "orchestration-skills");
  const skillsRoot = join(codexHome, "skills");
  mkdirSync(installRoot, { recursive: true });
  mkdirSync(skillsRoot, { recursive: true });
  const link = join(skillsRoot, "gepetto");
  symlinkSync(join(installRoot, "gepetto"), link, "dir");
  assert.throws(
    () => uninstallSuite({ codexHome, sourceRoot: repositoryRoot }),
    /Refusing to remove unmanaged directory/,
  );
  assert.equal(lstatSync(link).isSymbolicLink(), true);
});

test("doctor passes on a fresh install", () => {
  const codexHome = temporaryCodexHome();
  installSuite({ codexHome, sourceRoot: repositoryRoot });
  const result = doctorSuite({ codexHome, sourceRoot: repositoryRoot });
  assert.equal(result.ok, true, JSON.stringify(result.problems));
  assert.equal(result.problems.length, 0);
});

test("doctor reports tampered installs and missing installs", () => {
  const codexHome = temporaryCodexHome();
  const missing = doctorSuite({ codexHome, sourceRoot: repositoryRoot });
  assert.equal(missing.ok, false);
  assert.equal(missing.problems.length > 0, true);

  installSuite({ codexHome, sourceRoot: repositoryRoot });
  writeFileSync(join(codexHome, "orchestration-skills", ".codex-orchestration-install.json"), "tampered\n");
  const tampered = doctorSuite({ codexHome, sourceRoot: repositoryRoot });
  assert.equal(tampered.ok, false);
  assert.equal(tampered.problems.some((problem) => problem.includes("marker")), true);
});
