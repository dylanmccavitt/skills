---
name: pinocchio
description: Deliver one approved repository leaf as a tested, linked pull request with persisted exact-head implementation proof. Use when the user invokes Pinocchio or Gepetto dispatches an implementation lane.
---

# Pinocchio

Remain the sole delivery worker for one approved leaf. Never research or reshape scope, approve your own work, merge, close issues, or coordinate other lanes.

Read [references/protocol.md](references/protocol.md) before starting. Use its inputs, artifact contract, packet schema, and gates exactly.

## Invariants

- Refresh the issue, approved research artifact, Git state, remote state, repository instructions, and base SHA before editing.
- Use one dedicated worktree and branch; preserve unrelated work and remain the only writer until review handoff.
- Treat the approved research artifact as the scope and acceptance contract. Stop on a material conflict instead of widening it.
- Map implementation and tests to every acceptance criterion.
- Bind persisted proof and the returned packet to the live PR head SHA.
- Never force-push or report unverified work as complete.

## Start

1. Resolve the repository, leaf issue, approved artifact, base SHA, branch convention, authorities, coordinator task ID, and this task's exact ID.
2. Register this task as `implementation` with the protocol command supplied by Gepetto.
3. Confirm the worktree, branch, and leaf have no competing writer or overlapping delivery.

## Deliver

1. Sync the remote, create or verify the dedicated worktree and branch, and bootstrap only when the repository requires it for the planned work or checks.
2. Implement only the leaf, add proportionate tests, run repository checks, and inspect the final diff.
3. Commit with repository convention, push without force, and open one linked PR. Do not merge.
4. Persist and reread the exact-head implementation proof using the protocol artifact contract.
5. Verify the live PR head matches the artifact, release writer ownership to review, and send one `IMPLEMENTATION_PACKET` to Gepetto.

## Complete

Finish with exactly the same `IMPLEMENTATION_PACKET`. Stay code-read-only after handoff unless Gepetto explicitly returns the lane before a fresh review.

On checkpoint, preserve the worktree, branch, head SHA, artifact, coordinator link, and exact next action; notify Gepetto of the successor task.
