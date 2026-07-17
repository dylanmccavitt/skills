---
name: checkpoint-handoff
description: Continue long-running work in a fresh Codex task after compaction or an explicit checkpoint request. Preserve the live checkout, task role, and exact next action without archiving either task.
---

# Checkpoint Handoff

Create one fresh task with `create_thread`; never fork. Keep the handoff small.

1. Refresh the live checkout and task state. Capture only: objective, exact cwd/worktree, branch and HEAD, dirty files, completed proof, active workers/tasks, blockers, authority gates, and next action.
2. Resolve the source task ID and title from live task state. Never guess.
3. Create a fresh task in the same project, then send it the compact capsule and exact source ID. The successor must refresh live state before writing and preserve the existing checkout and single-writer ownership.
4. Register the continuation with `python3 /Users/dylanmccavitt/projects/codex-orchestration-skills/hooks/orchestration_state.py continue --source-id <source-id> --successor-id <successor-id>`.
5. Confirm the successor received the capsule. Do not archive, pin, or otherwise hide either task.
6. Rename the source `<canonical title> - checkpoint <short source id>` and the successor `<canonical title>` so only the live task owns the canonical name.

For Gepetto, Jiminy, or a Gepetto-managed lane, preserve the role in both the title and capsule. Send this receipt to the linked coordinator/watchdog when one exists:

```text
CHECKPOINT_CONTINUATION role=<role> source=<source-id> successor=<successor-id> title=<canonical-title>
```

Gepetto must replace the old task ID in its ledger and tell Jiminy. Jiminy must follow the successor and update its watch list. A self-checkpointed Gepetto or Jiminy must tell its counterpart its new task ID before resuming.

If task tools are unavailable, return the capsule in chat and continue in place. Skip successor-only rename, registration, coordinator, and watchdog steps. Never claim that a successor exists unless its live task ID was confirmed.
