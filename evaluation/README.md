# Evaluation fixture contracts

This directory contains immutable evaluation inputs for later workflow runners.
It does not execute Codex, collect telemetry, compare workflow versions, or
produce reports. Passing these contracts cannot establish that one workflow is
better.

## Versioned layout

- `suite-v1.json` indexes exactly the version 1 baseline fixtures.
- `schemas/` defines the strict suite, public fixture, and held-out grader
  document shapes.
- `fixtures/<fixture-id>/public/` is the only tree a future runner may copy into
  an agent-visible workspace.
- `fixtures/<fixture-id>/grader/grader-v1.json` remains outside that workspace
  until delivery is complete.

An indexed fixture version is immutable. Any incompatible schema or fixture
change requires a new schema or fixture version and new digests.

## Canonical bytes and digests

JSON documents are decoded with duplicate-key rejection and then encoded as
UTF-8 canonical JSON: keys sorted, no insignificant whitespace, and one trailing
newline. A tree digest hashes every regular file beneath its root in sorted
POSIX-path order using the documented `evaluation-tree-v1` framing implemented
by `validate.py`. Symlinks, unexpected files, unsafe paths, and non-regular
assets are rejected.

The public-fixture digest is canonical JSON over the fixture identity, public
payload-tree digest, and seed-repository-tree digest. This avoids a circular
reference while binding a grader to the exact public task content. The suite
separately pins the complete public manifest, complete public payload, seed
repository, and held-out grader.

## Grader trust boundary

Public manifests expose only an opaque grader ID, version, and grader-contract
digest. They never expose grader paths, commands, expected observations,
seeded-defect inventories, or private rubric text. A future runner must
materialize only `public/payload/` for delivery, then stage the matching grader
after delivery has stopped.

Validate the checked-in corpus without network access:

```sh
python3 evaluation/validate.py
python3 -m unittest discover -s evaluation -p "test_*.py"
```
