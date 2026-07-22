import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");

const expectedPackedFiles = [
  "LICENSE",
  "README.md",
  "bin/install.mjs",
  "checkpoint/SKILL.md",
  "checkpoint/agents/openai.yaml",
  "gepetto/SKILL.md",
  "gepetto/agents/openai.yaml",
  "gepetto/references/protocol.md",
  "gepetto/references/workflow.json",
  "hooks/hooks.json",
  "hooks/orchestration_events.py",
  "hooks/orchestration_graph.py",
  "hooks/orchestration_hook.py",
  "hooks/orchestration_state.py",
  "hooks/orchestration_watchdog.py",
  "jiminy/SKILL.md",
  "jiminy/agents/openai.yaml",
  "jiminy/references/merge-gates.md",
  "jiminy/references/runtime-state.md",
  "package.json",
  "pinocchio/SKILL.md",
  "pinocchio/agents/openai.yaml",
  "pinocchio/references/protocol.md",
].sort();

test("packed artifact contains exactly the required runtime and reference files", () => {
  const npm = process.platform === "win32" ? "npm.cmd" : "npm";
  const packed = spawnSync(npm, ["pack", "--dry-run", "--json", "--ignore-scripts"], {
    cwd: repositoryRoot,
    encoding: "utf8",
  });

  assert.equal(packed.status, 0, packed.stderr);
  const report = JSON.parse(packed.stdout);
  assert.equal(report.length, 1);
  assert.deepEqual(
    report[0].files.map(({ path }) => path).sort(),
    expectedPackedFiles,
  );
});
