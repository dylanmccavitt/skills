import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, lstatSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

import { installSuite } from "../bin/install.mjs";

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
  for (const skill of ["gepetto", "pinocchio", "jiminy", "checkpoint"]) {
    const path = join(codexHome, "skills", skill);
    assert.equal(lstatSync(path).isSymbolicLink(), true);
    assert.equal(existsSync(join(path, "SKILL.md")), true);
  }
  const hooks = JSON.parse(readFileSync(hooksPath, "utf8"));
  assert.equal(hooks.custom, true);
  assert.equal(hooks.hooks.Stop[0].hooks[0].command, "existing");
  assert.equal(hooks.hooks.Stop.length, 2);
  const command = hooks.hooks.SessionStart[0].hooks[0].command;
  const hook = spawnSync(command, {
    shell: true,
    env: { ...process.env, CODEX_HOME: codexHome },
    input: '{"session_id":"unregistered","hook_event_name":"SessionStart","source":"compact"}',
    encoding: "utf8",
  });
  assert.equal(hook.status, 0, hook.stderr);
  assert.equal(hook.stdout, "");
  assert.equal(
    readdirSync(codexHome).some((name) => name.startsWith("hooks.json.backup-")),
    true,
  );
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
  assert.match(result.stdout, /Installed gepetto, pinocchio, jiminy, checkpoint/);
});

test("repeated installation is idempotent", () => {
  const codexHome = temporaryCodexHome();
  installSuite({ codexHome, sourceRoot: repositoryRoot });
  const first = readFileSync(join(codexHome, "hooks.json"), "utf8");
  installSuite({ codexHome, sourceRoot: repositoryRoot });
  const second = readFileSync(join(codexHome, "hooks.json"), "utf8");
  assert.equal(second, first);
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
