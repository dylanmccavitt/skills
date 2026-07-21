---
name: checkpoint
description: Continue long-running work in a fresh Codex task after compaction or an explicit checkpoint request. Preserve the live checkout, task role, and exact next action without archiving either task.
---

# Checkpoint

Create one fresh task with `create_thread`; never fork. Keep the handoff small.

1. Refresh the live checkout and task state. Capture only: objective, exact cwd/worktree, branch and HEAD, dirty files, completed proof, blockers, authority gates, and next action. For lane state, point at the persisted Gepetto ledger (`ledger show`) instead of restating it.
2. Resolve the source task ID and title from live task state. Never guess.
3. Create a fresh task in the same project, then send it the compact capsule and exact source ID. The successor must refresh live state before writing and preserve the existing checkout and single-writer ownership.
4. Register the continuation with `python3 "${CODEX_HOME:-$HOME/.codex}/orchestration-skills/hooks/orchestration_state.py" continue --source-id <source-id> --successor-id <successor-id>`.
5. Confirm the successor received the capsule. Do not archive, pin, or otherwise hide either task.
6. Rename the source `<canonical title> - checkpoint <short source id>` and the successor `<canonical title>` so only the live task owns the canonical name.

For Gepetto, Jiminy, or a Gepetto-managed lane, preserve the role in both the title and capsule. Send this receipt to the linked coordinator, and to Jiminy only when a Jiminy task is live:

```text
CHECKPOINT_CONTINUATION role=<role> source=<source-id> successor=<successor-id> title=<canonical-title>
```

Gepetto must replace the old task ID in its persisted ledger with `ledger set`. A live Jiminy must replace the old ID with the successor. A self-checkpointed Gepetto or Jiminy must tell its live counterpart its new task ID before resuming.

If task tools are unavailable, return the capsule in chat and continue in place. Skip successor-only rename, registration, coordinator, and Jiminy steps. Never claim that a successor exists unless its live task ID was confirmed.
