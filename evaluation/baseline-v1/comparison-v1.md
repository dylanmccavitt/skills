# Frozen protocol-replay baseline

> Deterministic protocol behavior only. This comparison does not measure live-agent delivery quality, cost, latency, model performance, production safety, or identify a winning workflow.

## Comparison summary

| Workflow | Status | Events | Accepted | Rejected | Unsupported | Errors | Duration |
|---|---|---|---|---|---|---|---|
| v0.2.0 | Observed workflow | 5 (no change) | 3 (no change) | 1 (no change) | 1 (no change) | 0 (no change) | Unknown |
| v0.4.0 | Comparison reference | 5 (no change) | 3 (no change) | 1 (no change) | 1 (no change) | 0 (no change) | Unknown |
| Main comparison anchor | Observed workflow | 5 (no change) | 3 (no change) | 1 (no change) | 1 (no change) | 0 (no change) | Unknown |
| Frozen ownership candidate | Observed workflow | 5 (no change) | 3 (no change) | 1 (no change) | 1 (no change) | 0 (no change) | Unknown |

Deltas are signed event-count differences from the explicitly designated comparison reference. They are not quality scores or rankings.

## Chronological event flow

### v0.2.0

- Commit: `ac7372a5c26031dcfe3e5f4c66ac44b5f35a9cce`
- Workflow digest: `sha256:73494fc3df357552c8acabdb546c56eeac1e78e6ff3a810a711893230a0ed713`
- Raw evidence: [manifest](raw/run-25461cb9e642f5f1573653ba/manifest.json), [events](raw/run-25461cb9e642f5f1573653ba/events.jsonl), [result](raw/run-25461cb9e642f5f1573653ba/result.json)

| Seq | Event | From | To | Disposition | Technical detail |
|---:|---|---|---|---|---|
| 0 | Research packet | Research | Implementation | Accepted | Research approved |
| 1 | Implementation packet | Implementation | — | Rejected | Guard rejected (`guard-rejected`) |
| 2 | Flow blocked | Implementation | Blocked | Accepted | Lane blocked |
| 3 | Resume authorized | Blocked | Implementation | Accepted | Resume |
| 4 | Unknown trace event | Implementation | — | Unsupported | Unsupported event (`unsupported-event`) |

### v0.4.0

- Commit: `2aace457936de58fb64a40167da5fe60ffd8926f`
- Workflow digest: `sha256:1a727a8e959f8c3f494b9b5ed990db3d628287443ce16b0e8b990f100b00ca3d`
- Raw evidence: [manifest](raw/run-be30c7a68e8a732c789a28a3/manifest.json), [events](raw/run-be30c7a68e8a732c789a28a3/events.jsonl), [result](raw/run-be30c7a68e8a732c789a28a3/result.json)

| Seq | Event | From | To | Disposition | Technical detail |
|---:|---|---|---|---|---|
| 0 | Research packet | Research | Implementation | Accepted | Research approved |
| 1 | Implementation packet | Implementation | — | Rejected | Guard rejected (`guard-rejected`) |
| 2 | Flow blocked | Implementation | Blocked | Accepted | Lane blocked |
| 3 | Resume authorized | Blocked | Implementation | Accepted | Resume |
| 4 | Unknown trace event | Implementation | — | Unsupported | Unsupported event (`unsupported-event`) |

### Main comparison anchor

- Commit: `365c4a6acebd6e7d40adb23f49d0e2bec6c60fbc`
- Workflow digest: `sha256:21b468fe0f69de23595aefb237fd4a8948f400c21f04e60e14bcb727d188759a`
- Raw evidence: [manifest](raw/run-6c62c2be0d29c49ebf0d3c07/manifest.json), [events](raw/run-6c62c2be0d29c49ebf0d3c07/events.jsonl), [result](raw/run-6c62c2be0d29c49ebf0d3c07/result.json)

| Seq | Event | From | To | Disposition | Technical detail |
|---:|---|---|---|---|---|
| 0 | Research packet | Research | Implementation | Accepted | Research approved |
| 1 | Implementation packet | Implementation | — | Rejected | Guard rejected (`guard-rejected`) |
| 2 | Flow blocked | Implementation | Blocked | Accepted | Lane blocked |
| 3 | Resume authorized | Blocked | Implementation | Accepted | Resume |
| 4 | Unknown trace event | Implementation | — | Unsupported | Unsupported event (`unsupported-event`) |

### Frozen ownership candidate

- Commit: `5095aedcd498e868df791c122d2b8c687c9fb764`
- Workflow digest: `sha256:b654b27384d4a1de663fbfab8f387c9e24678f300a45545f7795afa9c6d29d78`
- Raw evidence: [manifest](raw/run-aaa790b836d826c55da5b477/manifest.json), [events](raw/run-aaa790b836d826c55da5b477/events.jsonl), [result](raw/run-aaa790b836d826c55da5b477/result.json)

| Seq | Event | From | To | Disposition | Technical detail |
|---:|---|---|---|---|---|
| 0 | Research packet | Research | Implementation | Accepted | Research approved |
| 1 | Implementation packet | Implementation | — | Rejected | Guard rejected (`guard-rejected`) |
| 2 | Flow blocked | Implementation | Blocked | Accepted | Lane blocked |
| 3 | Resume authorized | Blocked | Implementation | Accepted | Resume |
| 4 | Unknown trace event | Implementation | — | Unsupported | Unsupported event (`unsupported-event`) |

## Audit note

Canonical manifest JSON, event JSONL, and result JSON remain the source of truth. Human labels come only from vocabulary version 1; unknown values retain their exact raw form.
