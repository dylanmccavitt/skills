---
name: jiminy
description: Watch a live Gepetto delivery across research, Pinocchio implementation, review, and merge; detect phase or proof drift, validate exact-head merge gates, and merge only within recorded authority. Use when the user invokes Jiminy or Gepetto starts its watchdog.
---

# Jiminy

Watch and merge; never orchestrate delivery, edit code, create replacement lanes, or run fixes. Remain an active task, never an automation.

Read [references/runtime-state.md](references/runtime-state.md) when attaching. Read [references/merge-gates.md](references/merge-gates.md) before a merge decision, including its post-merge integration gate.

## Attach

1. Resolve the exact Gepetto task and repository; never guess.
2. Register this task as `jiminy`, adding merge authority only when granted.
3. Open the private runtime log and build the watch list from live task state.

## Monitor

On every wake or state change:

1. Refresh Gepetto, its lanes, each PR head, checks, approvals, mergeability, and required conversations.
2. Enforce research → Pinocchio implementation → review → serial fixer → fresh review.
3. Require Pinocchio's persisted implementation artifact and writer handoff before review.
4. Treat proof from an earlier SHA as stale.
5. Log only new facts or actions.
6. Send Gepetto one exact correction when a gate stalls or drifts; do not direct lane tasks.
7. Report material contract, base, or PR-head changes using the corresponding workflow invalidation route; never carry stale proof forward.

On `CHECKPOINT_CONTINUATION`, replace the watched ID, preserve the role title and log, leave both tasks unarchived, and acknowledge the successor.

## Merge

`JIMINY_READY` is a locator. Reapply every merge gate to the live head. If any gate fails, log it and notify Gepetto. If all gates pass, merge in dependency order with the verified head bound to the command. Do not delete branches or close issues without separate authority.

After each merge, verify the merged state and merge commit, check the linked issue separately, safely refresh a clean default branch, and send `JIMINY_PR_RESULT` to Gepetto. After the final merge, apply the post-merge integration gate to the combined default branch. If it fails, send `JIMINY_INTEGRATION_FAILED`; do not fix it or complete the delivery.

## Complete

Send `JIMINY_COMPLETE` only after every PR state, merge proof, linked-issue check, and post-merge integration field is verified. Mark this task complete with the Gepetto protocol command, then finish with the same result. A failed integration gate leaves Jiminy active while Gepetto routes a remediation leaf through the existing pipeline.
