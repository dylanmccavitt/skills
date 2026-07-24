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

The coordinator/user approves scope, revisions, stops, and every external delivery. Skill invocation is not authority. A delivery attempt needs a registered coordinator/user actor and a one-shot authority record bound to the task, repository, PR, reviewed head, effect, and exact tool request.

## Safety kernel

The kernel records compact task contracts, registered role owners, writer ownership, receipts, proof, and authority. Persisted transitions use locked compare-and-swap updates and atomic replacement. Review Gate must be registered independently from Implement; checkpoint recovery transfers the single writer to a confirmed successor. Immediately before an authorized external action, the hook verifies the exact request, refreshes the real PR head from GitHub, and consumes the grant once.

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
