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

The kernel records compact task contracts, exact Implement command permissions, credential hashes, registered role owners, writer ownership, receipts, proof, and authority. Persisted transitions use locked compare-and-swap updates and atomic replacement. Review Gate is credentialed separately from Implement and decision actors; checkpoint freezes the outgoing writer before transferring ownership to a confirmed successor.

`voice_state.py create` provisions a task and one capability per actor. `transition` applies credentialed state changes. `deliver` also requires the exact granting decision actor and capability; it is the only supported external-action path, refreshes the approved PR head, consumes the grant once, and invokes `gh pr merge` with `--match-head-commit`. `classify` distinguishes ordinary, durable, and complex work; `orchestrate` accepts only a user-approved non-overlapping acyclic lane map.

Durable tasks carry state, task, actor, capability, and worktree context in `CODEX_ORCHESTRATION_*`. The installed hook checks the live lease before Bash or file writes, permits durable writers only exact contract-listed Bash commands, and denies Review Gate all shell and write-capable tools. Ordinary tasks omit that context: structured local edits remain available, while Bash is limited to literal read-only inspection. Delivery, protected-branch push, issue closure, publishing, and production deployment cannot ride through unregistered or composed shell commands.

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
