import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import test from "node:test";

const root = resolve(new URL("..", import.meta.url).pathname);

test("publishes only the voice-first skill surface", () => {
  const pkg = JSON.parse(readFileSync(resolve(root, "package.json")));
  assert.deepEqual(pkg.files.filter((item) => item.endsWith("/")), ["bin/", "checkpoint/", "gepetto/", "implement/", "orchestrate/", "review-gate/"]);
  assert.equal(pkg.files.some((item) => item.includes("pinocchio") || item.includes("jiminy")), false);
});
