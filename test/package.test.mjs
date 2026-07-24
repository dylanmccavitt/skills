import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import test from "node:test";

const root = resolve(new URL("..", import.meta.url).pathname);

test("publishes only the voice-first skill surface", () => {
  const pkg = JSON.parse(readFileSync(resolve(root, "package.json")));
  assert.deepEqual(pkg.files.filter((item) => item.endsWith("/")), ["bin/", "checkpoint/", "gepetto/", "implement/", "orchestrate/", "review-gate/"]);
  assert.equal(pkg.files.some((item) => item.includes("pinocchio") || item.includes("jiminy")), false);
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
    "gepetto/SKILL.md",
    "implement/SKILL.md",
    "orchestrate/SKILL.md",
    "review-gate/SKILL.md",
  ]) {
    assert.equal(files.has(path), true, `missing from tarball: ${path}`);
  }
  assert.equal([...files].some((path) => /(?:pinocchio|jiminy)\//.test(path)), false);
});
