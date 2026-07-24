import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import test from "node:test";

const root = resolve(new URL("..", import.meta.url).pathname);

test("publishes only the voice-first skill surface", () => {
  const pkg = JSON.parse(readFileSync(resolve(root, "package.json")));
  assert.deepEqual(pkg.files.filter((item) => item.endsWith("/")), ["bin/", "checkpoint/", "gepetto/", "orchestrate/", "painter/", "vigil/"]);
  assert.equal(
    pkg.files.some((item) =>
      ["pinocchio", "jiminy", "implement", "review-gate"].some((name) =>
        item.includes(`${name}/`)
      )
    ),
    false,
  );
});

test("packed artifact contains every runtime control-plane file", () => {
  const result = spawnSync(
    "npm",
    ["pack", "--dry-run", "--json", "--ignore-scripts"],
    { cwd: root, encoding: "utf8" },
  );
  assert.equal(result.status, 0, result.stderr);
  const packed = JSON.parse(result.stdout);
  const files = new Set(packed[0].files.map((entry) => entry.path));
  for (const path of [
    "bin/install.mjs",
    "hooks/hooks.json",
    "hooks/voice_state.py",
    "checkpoint/SKILL.md",
    "checkpoint/agents/openai.yaml",
    "gepetto/SKILL.md",
    "gepetto/agents/openai.yaml",
    "orchestrate/SKILL.md",
    "orchestrate/agents/openai.yaml",
    "painter/SKILL.md",
    "painter/agents/openai.yaml",
    "vigil/SKILL.md",
    "vigil/agents/openai.yaml",
  ]) {
    assert.equal(files.has(path), true, `missing from tarball: ${path}`);
  }
  assert.equal(
    [...files].some((path) => /(?:pinocchio|jiminy|implement|review-gate)\//.test(path)),
    false,
  );
});
