import assert from "node:assert/strict";
import test from "node:test";

import { verifyReleaseTag } from "../scripts/verify-release.mjs";

test("accepts a release tag matching the package version", () => {
  assert.equal(verifyReleaseTag("v0.1.0", "0.1.0"), "v0.1.0");
});

test("rejects a release tag that does not match the package version", () => {
  assert.throws(
    () => verifyReleaseTag("v0.2.0", "0.1.0"),
    /expected v0\.1\.0/,
  );
});

test("rejects a missing release tag", () => {
  assert.throws(
    () => verifyReleaseTag(undefined, "0.1.0"),
    /Release tag <missing>/,
  );
});
