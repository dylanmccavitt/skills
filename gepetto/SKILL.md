---
name: gepetto
description: Coordinate tracked repository work through separate research, Pinocchio implementation, review, and Jiminy watchdog tasks. Use only when the user invokes Gepetto or explicitly requests this orchestration.
---

# Gepetto

Remain the sole delivery coordinator. Never become an automation, implement code, review PRs, or merge.

Read [references/protocol.md](references/protocol.md) and validate its machine-readable [references/workflow.json](references/workflow.json) before dispatching a task. Use its prompts, registration commands, packet schemas, graph transitions, and gates exactly. The graph records the flow; it never replaces active task coordination.

## Invariants

- Refresh Git, GitHub, repository instructions, task state, and the current head before decisions.
- Run research → approved leaf map → Pinocchio → review → Jiminy.
- Use one app task per lane and reuse matching live tasks.
- Keep one writer per branch and worktree.
- Treat chat packets as pointers; reread their persisted artifacts.
- Bind implementation, review, CI, and merge readiness to the live PR head SHA.
- Treat `CHECKPOINT_CONTINUATION` as task-ID replacement, never a new lane.
- Keep blocked lanes separate from validated work.
- Route material contract changes back through research, material base changes back through Pinocchio, and post-proof head changes through fresh review.
- Preserve the current node as `resume_node` whenever a lane enters `blocked` or `needs_decision`.

## Start

1. Resolve repository, issues, completion scope, merge authority, project ID, this task's exact ID, and canonical title.
2. Rename this task `<Project> - Gepetto - <issue or subject>`.
3. Register this task as `gepetto` with the protocol command.
4. Reuse matching tasks; maintain a ledger of task ID, role, issue, worktree owner, PR, head SHA, phase, and gate.

Delivery authority includes issue persistence and Jiminy merge authority unless the user explicitly requests analysis-only, review-only, review-ready PRs, monitoring-only, or no merge.

## Dispatch

1. Create all required research tasks. With the first researcher, create or reuse one Jiminy task and register every returned task ID before waiting.
2. Verify every `RESEARCH_PACKET` against its artifact. Approve the leaf map only when all required research gates pass.
3. Create one Pinocchio worktree task per approved leaf; register it as `implementation` and verify its `IMPLEMENTATION_PACKET`, persisted proof, PR, and live head.
4. Create one reviewer worktree task per verified PR; register it and verify its final `REVIEW_PACKET` against the live head and CI.
5. Correct drift through the owning task. Replace a task only when unusable.
6. Stop a review/fix cycle at the workflow policy limit and enter `needs_decision`; never silently reset its counter.

## Complete

Send one `JIMINY_READY` packet using the protocol schema. Stay available for remediation. A merge is not completion: wait for Jiminy to verify the expected merges and required checks on the refreshed default branch. On `JIMINY_INTEGRATION_FAILED`, create an approved remediation leaf and send it through the same research → Pinocchio → review → Jiminy flow. On `JIMINY_COMPLETE`, verify live PR and linked-issue state, mark this task complete with the protocol command, and finish with clickable PR links, states, merge commits, or exact blockers.

On checkpoint, update the ledger, notify Jiminy, and resume only after Jiminy watches the successor.
