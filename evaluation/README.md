# Evaluation contracts and protocol replay

This directory contains immutable evaluation inputs for later workflow runners.
It also contains a deterministic, standard-library protocol replay command that
reads historical workflow JSON strictly as data and produces versioned raw
evidence. It does not execute Codex, collect telemetry, aggregate trials, score
workflow versions, or produce reports. Passing these contracts cannot establish
that one workflow is better.

## Versioned layout

- `suite-v1.json` indexes exactly the version 1 baseline fixtures.
- `schemas/` defines strict suite, public fixture, held-out grader, replay trace,
  run manifest, JSONL event record, and run result shapes.
- `fixtures/<fixture-id>/public/` is the only tree a future runner may copy into
  an agent-visible workspace.
- `fixtures/<fixture-id>/grader/grader-v1.json` remains outside that workspace
  until delivery is complete.
- `replay-trace-v1.json` is a frozen, non-agent protocol input covering accepted,
  rejected, blocked/resumed, and unsupported event behavior.
- `replay.py` resolves local Git refs and writes one content-bound run directory
  per exact workflow commit.

An indexed contract version is immutable. Any incompatible schema, fixture, or
evidence change requires a new version and new digests. Version 1 public assets
are UTF-8 text; validation fails closed on undecodable content so binary files
cannot bypass grader-leakage checks.

## Canonical bytes and digests

JSON documents are decoded with duplicate-key rejection and encoded as UTF-8
canonical JSON: keys sorted, no insignificant whitespace, no non-JSON numeric
constants, and one trailing newline. JSONL is exactly one canonical JSON object
plus one newline per event, with no blank lines.

Workflow content refs hash the exact bytes read by `git show
<commit>:gepetto/references/workflow.json`. Trace and evidence content refs hash
their canonical bytes. Replay state and run-key digests additionally use the
`protocol-replay-state-v1` and `protocol-replay-run-key-v1` domain strings, a
NUL separator, an eight-byte big-endian content length, and canonical JSON
bytes. Wall-clock timestamps, hostnames, checkout paths, and unordered platform
metadata are excluded from canonical evidence.

A tree digest hashes every regular file beneath its root in sorted POSIX-path
order using the `evaluation-tree-v1` framing implemented by `validate.py`.
Symlinks, unexpected files, unsafe paths, and non-regular assets are rejected.

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
after delivery has stopped. Validation derives private check IDs, command paths,
commands, expected-observation names, and nontrivial nested expected-result text
from each grader contract and rejects them in public paths, filenames, or file
contents.

## Deterministic local replay

`replay.py` uses local Git commands only. Every `--ref` must already resolve
unambiguously to one full commit. The command never fetches, checks out a target
ref, imports historical Python, invokes Codex, or reads installed Codex
configuration. It reads only the workflow JSON blob at the resolved commit.
Missing or ambiguous refs and missing workflow blobs fail closed.

The supported workflow-v1 compatibility surface is deliberately narrow:

- graph fields `version`, `workflow`, `initial_node`, `policies`, `nodes`,
  optional `packet_types`, and `transitions`;
- static `to` and dynamic `to_path` targets;
- `all` guards using `equals`, `equals_path`, `non_empty`,
  `less_than_policy`, `map_keys_equal_path`, `values_full_sha`, and
  `content_ref`;
- positive integer `increment` mutations and dotted-path `set` mutations; and
- the node and transition metadata present in the required version-1 workflow
  refs.

Unknown graph fields, transition constructs, operators, versions, duplicate IDs,
and duplicate JSON keys are rejected before replay. Packet-type declarations are
validated as graph data; packet implementation code is never imported.

Each event records its source and target node, semantic disposition, unique
transition ID when accepted, normalized error code otherwise, and before/after
state digests. Accepted events apply graph mutations. Rejected guards,
unsupported events, and evaluator errors retain the exact before-state digest.
Optional expected fields in the input trace are assertions: a mismatch fails the
run before evidence is persisted.

Run comparability requires exact equality for these non-workflow keys:

- suite, fixture, and trace IDs, versions, and content digests;
- evaluator commit and evaluator version;
- initial repository SHA;
- execution kind, model, reasoning, and time budget;
- environment-contract digest; and
- trial identity.

Requested ref names are provenance; resolved workflow commit and content digest
are authoritative workflow identity. Comparability intentionally permits only
workflow identity to differ. Mismatch errors name every unequal key. The command
does not aggregate comparable runs or calculate a score.

Evidence is written beneath a derived `run-<digest>` directory as
`manifest.json`, `events.jsonl`, and `result.json`. Temporary files are created
in that directory, flushed, and atomically replaced. An exact rerun is
byte-idempotent. A matching partial run can be completed after interruption, but
an existing conflicting artifact, unsafe symlink, or different completed run is
never overwritten.

With the named refs already present locally, run the four-way smoke comparison:

```sh
replay_output="$(mktemp -d)"
python3 evaluation/replay.py \
  --repo . \
  --trace evaluation/replay-trace-v1.json \
  --output "$replay_output" \
  --evaluator-ref HEAD \
  --ref v0.2.0 \
  --ref v0.4.0 \
  --ref 365c4a6acebd6e7d40adb23f49d0e2bec6c60fbc \
  --ref 5095aedcd498e868df791c122d2b8c687c9fb764
```

The checked-in tests construct equivalent local history without network access,
including the bounded review/fix behavior introduced after v0.2.0 and the
frozen candidate's additional merge-node review transition.

Validate the checked-in corpus and replay contracts without network access:

```sh
python3 evaluation/validate.py
python3 -m unittest discover -s evaluation -p "test_*.py"
```

Raw replay evidence is evaluator output, not trusted telemetry or a quality
conclusion. Consumers must validate all cross-artifact digests and state
bindings before use. Live trials, repeated-trial statistics, cost and latency
collection, Pareto analysis, reporting, ranking, and winner selection belong to
later evaluation layers.
