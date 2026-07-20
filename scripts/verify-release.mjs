#!/usr/bin/env node

import { readFileSync, realpathSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const scriptPath = fileURLToPath(import.meta.url);
const repositoryRoot = resolve(dirname(scriptPath), "..");

export function verifyReleaseTag(tag, version) {
  const expected = `v${version}`;
  if (tag !== expected) {
    throw new Error(`Release tag ${tag || "<missing>"} does not match package version ${version}; expected ${expected}.`);
  }
  return expected;
}

function main() {
  const packageJson = JSON.parse(readFileSync(resolve(repositoryRoot, "package.json"), "utf8"));
  const tag = process.argv[2];
  verifyReleaseTag(tag, packageJson.version);
  console.log(`Release tag ${tag} matches package version ${packageJson.version}.`);
}

if (process.argv[1] && realpathSync(process.argv[1]) === scriptPath) {
  try {
    main();
  } catch (error) {
    console.error(error.message);
    process.exitCode = 1;
  }
}
