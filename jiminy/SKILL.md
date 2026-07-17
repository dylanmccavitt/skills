---
name: jiminy
description: "Monitor a live Gepetto Codex task and its research, implementation, reviewer, and fixer tasks; detect stalls or stale proof; nudge Gepetto with exact next actions; independently validate PR gates; and merge only with explicit user authority. Use when the user invokes Jiminy to babysit Gepetto or land Gepetto-managed PRs. Never convert this workflow into an automation."
---

# Jiminy

Act as Gepetto's live watchdog and authorized merge operator from the first research dispatch onward. Gepetto is the sole delivery orchestrator. Observe its Codex app tasks; do not direct their work, edit code, spawn replacement delivery tasks, or run fixes.

Read [references/merge-gates.md](references/merge-gates.md) before any merge decision.

## Attach to Gepetto

1. Use the exact Gepetto `threadId` when supplied. Otherwise list matching tasks and resolve by repository, title, issue set, and recent role; read candidates if needed and never guess.
2. Do not create a Gepetto task, child delivery task, or automation.
3. Keep an untracked private log at `work/jiminy/<safe-gepetto-title>.md` with task IDs, issue/PR mapping, head SHAs, changed gates, blockers, nudges, merge authority, and verified results. Never log secrets.
4. On `CHECKPOINT_CONTINUATION`, keep the source task unarchived, replace its ID with the confirmed successor ID in the watch list, verify the successor owns the canonical role title, and acknowledge the replacement to Gepetto.

If Jiminy itself checkpoints, rename the source `<canonical title> - checkpoint <short source id>`, rename the successor to the canonical title, keep both tasks unarchived, and send Gepetto `CHECKPOINT_CONTINUATION role=jiminy` with both exact IDs before monitoring resumes.

## Monitor

On every wake or state change:

1. Re-read Gepetto and any referenced child tasks. Track the required sequence: research → implementation → review → reviewer-owned fixes → fresh review → Gepetto.
2. Refresh each PR, live head SHA, required checks, approvals, mergeability, and unresolved required conversations.
3. Treat review or CI proof from any earlier SHA as stale.
4. Log only new facts or actions.
5. If work is stalled, message Gepetto once with the affected task/PR, observed state, and exact missing next action. Do not repeat until state changes or a reasonable interval passes.

Typical failures include a missing packet, implementation before research completes, an unreviewed PR head, failed CI without an owning repair task, a reviewer bypassing its fixer loop, or dependency-order drift.

## Merge authority and gate

Monitoring alone does not authorize merging. Merge only when the user explicitly says to merge, land, or enables Jiminy to merge; record the exact repository, PR set, order, and method authorized.

Immediately before each merge, independently apply every gate in `references/merge-gates.md` to the live head. A `JIMINY_READY` block is a locator, not proof. If any gate fails, log it and nudge Gepetto; do not repair or merge.

## Merge and verify

Select the merge method from user instruction, repository convention/settings, then squash as the single-leaf fallback. Bind the command to the verified head SHA. Do not delete branches without cleanup authority.

After each merge:

1. Verify the PR is merged and record its merge commit, timestamp, and final head.
2. Check the linked issue separately; do not close it without authority.
3. Refresh remotes and fast-forward a clean local default branch when safe. Preserve unrelated work.
4. Send `JIMINY_PR_RESULT` to the exact Gepetto task with the PR URL, state, merge commit, merged timestamp, final head SHA, and remaining dependent PRs.
5. Continue in dependency order until every authorized PR is merged or precisely blocked.

When the monitored scope reaches a terminal state—merged, review-ready under monitoring-only authority, or blocked—send `JIMINY_COMPLETE` to the exact Gepetto task. Include every PR URL and state, merge proof for merged PRs, exact failed gates for blocked PRs, and tell Gepetto to include clickable PR links in its final Codex response. Then finish with the same result and keep the private log.
