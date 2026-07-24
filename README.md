# Voice-directed Codex skills

This package keeps repository work safe after a voice conversation moves on. It does not choose product scope or delivery for the user.

## Paths

| Path | Use | Flow |
| --- | --- | --- |
| Ordinary | small, local, low-risk work | coordinator → result |
| Durable | branch, PR, handoff, external effect, or meaningful risk | coordinator → Painter → Vigil → explicit delivery authority |
| Complex | approved dependent lanes | coordinator → Orchestrate → per-lane Painter/Vigil |

## Skills

- `gepetto`: optional, read-only issue/lane research and recommendation.
- `painter`: sole writer for one approved durable task.
- `vigil`: independent, zero-execution, read-only exact-head review and delivery gate.
- `checkpoint`: atomic durable handoff.
- `orchestrate`: optional approved complex-lane coordination.

The coordinator/user approves scope, revisions, stops, and every external delivery. Skill invocation is not authority. A delivery attempt needs a credentialed coordinator/user actor and a one-shot typed action bound to the task, repository, PR, reviewed head, and merge method.

## Safety kernel

The kernel records compact task contracts, an immutable canonical worktree and write-root set, exact Painter command permissions, role-bound credential hashes, registered role owners, writer ownership, receipts, proof, and authority. Persisted transitions use locked compare-and-swap updates and atomic replacement. Branch and canonical-worktree reservations are exclusive across every live task in the registry, including pending checkpoints. Vigil is credentialed separately from Painter and decision actors; checkpoint freezes the outgoing writer before transferring ownership to a confirmed successor.

`voice_state.py create` provisions a task and one capability per actor. `transition` applies credentialed state changes. `run` is the only Painter command gateway: it checks the active lease and exact approved command, fixes the workdir, strips ambient credentials, and runs mutable repository code without network or state-registry write access. macOS uses the system sandbox; Linux requires `bubblewrap` and fails closed when it is unavailable. The one typed branch-push adapter disables repository hooks and binds the push to the approved repository, branch, and current commit.

`deliver` requires the exact granting decision actor and capability; it refreshes the approved PR head, journals the prepared attempt, consumes the grant once, invokes `gh pr merge` with `--match-head-commit`, and records completion only after authenticated recovery observation sees that exact PR merged. `recover-delivery` re-observes the exact PR after interruption and either records an already-completed merge or safely retries the still-open exact-head action. `classify` distinguishes ordinary, durable, and complex work; `orchestrate` accepts only a lane map previously approved through a credentialed coordinator/user transition.

Durable tasks carry state, task, actor, capability, and worktree context in `CODEX_ORCHESTRATION_*`. The installed hook examines every tool. Painter writes must use a supported target adapter and stay within both the leased worktree and contract write roots; raw shell commands are denied in favor of the locked `run` gateway. The registry, lock, installed kernel, `.git`, path escapes, symlink escapes, requested-workdir changes, and identity-environment overrides are excluded. Vigil fails closed on unknown tools and may use only explicit read-only inspection tools plus the one canonical locked-kernel `transition` command. Ordinary tasks omit durable context: structured local edits remain available, while shell execution is limited to literal read-only inspection. Merge, release, issue closure, publishing, and production deployment cannot ride through unregistered commands or mutable wrappers.

## Install

```sh
npx @dylanmccavitt/skills@latest
```

The installer adds only managed skills and hooks, preserves unrelated hooks, and refuses unmanaged replacement. A verified package upgrade retires package-owned `pinocchio` and `implement` links in favor of `painter`, and `jiminy` and `review-gate` links in favor of `vigil`. Legacy hooks are removed only when the managed-install marker is present and the complete historical event entry matches exactly; wrappers, customized entries, mixed user hooks, and orphaned lookalikes are preserved. Remove the package with `npx @dylanmccavitt/skills@latest uninstall`.

## Development

```sh
npm test
```

The tests cover registry-wide writer conflicts, scoped Painter targets and sandboxing, fail-closed Vigil tools, review independence, exact-head delivery authority and CLI recovery, proof invalidation, and ownership-safe package installation.
