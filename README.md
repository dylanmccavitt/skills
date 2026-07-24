# Voice-directed Codex skills

This package keeps repository work safe after a voice conversation moves on. It does not choose product scope or delivery for the user.

## Paths

| Path | Use | Flow |
| --- | --- | --- |
| Ordinary | small, local, low-risk work | coordinator → result |
| Durable | branch, PR, handoff, external effect, or meaningful risk | coordinator → Implement → Review Gate → explicit delivery authority |
| Complex | approved dependent lanes | coordinator → Orchestrate → per-lane Implement/Review Gate |

## Skills

- `gepetto`: optional, read-only issue/lane research and recommendation.
- `implement`: sole writer for one approved durable task.
- `review-gate`: independent, read-only exact-head review and delivery gate.
- `checkpoint`: atomic durable handoff.
- `orchestrate`: optional approved complex-lane coordination.

The coordinator/user approves scope, revisions, stops, and every external delivery. Skill invocation is not authority. A delivery action needs an explicit current authority record bound to repository, PR, and head.

## Safety kernel

The kernel records compact task contracts, writer ownership, receipts, proof, and authority. It uses locked compare-and-swap updates, atomic replacement, checkpoint recovery, and exact-head invalidation. Human-facing results are short receipts; canonical state remains local and machine-readable.

## Install

```sh
npx @dylanmccavitt/skills@latest
```

The installer adds only managed skills and hooks, preserves unrelated hooks, and refuses unmanaged replacement. Remove it with `npx @dylanmccavitt/skills@latest uninstall`.

## Development

```sh
npm test
```

The tests cover writer conflicts, review independence, exact-head delivery authority, proof invalidation, and package installation.
