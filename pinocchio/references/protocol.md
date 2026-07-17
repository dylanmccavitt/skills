# Pinocchio delivery protocol

Require the live leaf issue URL, approved research artifact, repository path, default branch and base SHA, branch convention, commit/push/PR and issue-update authority, coordinator task ID, and implementation task ID. Never copy the full research contract into chat.

Register the task immediately after creation:

```bash
python3 /Users/dylanmccavitt/projects/codex-orchestration-skills/hooks/orchestration_state.py register --session-id <pinocchio-task-id> --role implementation --coordinator-thread-id <gepetto-task-id>
```

## Persisted proof

Preserve unrelated issue text and idempotently append or replace a `<!-- gepetto-implementation:start -->` … `<!-- gepetto-implementation:end -->` section on the leaf issue. Record branch, base, commit, and live PR head SHAs; PR URL; changed files; exact checks and results; criterion-by-criterion proof; writer handoff; and caveats. Re-read the issue and record its live URL and `updatedAt`.

If the issue write is blocked, save the full proof in a uniquely named temporary Markdown file under `${TMPDIR:-/tmp}`. This preserves evidence but does not satisfy Gepetto's persistence gate.

## Receipt

```yaml
IMPLEMENTATION_PACKET:
  issue_url: <live URL>
  task_role: pinocchio
  pr_url: <live URL>
  pr_head_sha: <full live PR head SHA>
  artifact:
    kind: github_issue|tmp_markdown
    status: persisted|blocked
    marker: <gepetto-implementation for GitHub, null for temporary Markdown>
    issue_url: <raw live URL, present for a GitHub artifact>
    observed_updated_at: <timestamp after re-read, present for a GitHub artifact>
    path: <absolute path, present for a temporary Markdown artifact>
```

Gepetto may dispatch review only after rereading a persisted GitHub artifact and independently confirming that its PR head equals `pr_head_sha`. Finish with only this receipt; never paste proof contents into chat.
