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

The coordinator/user approves scope, revisions, stops, and every external delivery. Skill invocation is not authority. A delivery attempt needs a credentialed coordinator/user actor and a one-shot typed action bound to the task, repository, PR, reviewed head, and merge method.

## Safety kernel

The kernel records compact task contracts, credential hashes, registered role owners, writer ownership, receipts, proof, and authority. Persisted transitions use locked compare-and-swap updates and atomic replacement. Review Gate is credentialed separately from Implement and decision actors; checkpoint freezes the outgoing writer before transferring ownership to a confirmed successor.

`voice_state.py create` provisions a task and one capability per actor. `transition` applies credentialed state changes. `deliver` is the only supported external-action path: it refreshes the live PR head, consumes the grant once, and invokes `gh pr merge` with `--match-head-commit`. The installed hook blocks direct merge, protected-branch push, issue-close, publish, and production-deploy commands.

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
