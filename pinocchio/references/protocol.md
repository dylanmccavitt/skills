# Pinocchio delivery protocol

Require the project name, live leaf issue or PR URL, approved research artifact, repository path, default branch and base SHA, branch convention, commit/push/PR and issue-update authority, coordinator task ID, and implementation task ID. Use the canonical title `<Project> - Pinocchio - <issue or PR>`. Never copy the full research contract into chat.

Gepetto registers the task immediately after creation. Verify that authoritative registration before work; do not register again:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/orchestration-skills/hooks/orchestration_state.py" verify --session-id <pinocchio-task-id> --role implementation --coordinator-thread-id <gepetto-task-id>
```

Content-bind the exact fetched research artifact and repository instruction files with the Gepetto protocol CLI. Load their full text only when `reload_required` is true; otherwise use the recorded `sha256:` refs.

## Persisted proof

Preserve unrelated issue text and idempotently append or replace a `<!-- gepetto-implementation:start -->` … `<!-- gepetto-implementation:end -->` section on the leaf issue. Record branch, base, commit, and live PR head SHAs; PR URL; changed files; exact checks and results; criterion-by-criterion proof; writer handoff; and caveats. Re-read the issue and record its live URL and `updatedAt`.

If the issue write is blocked, save the full proof in a uniquely named temporary Markdown file under `${TMPDIR:-/tmp}`. This preserves evidence but does not satisfy Gepetto's persistence gate.

## Receipt

The receipt is exactly the header below followed by one JSON object. Do not wrap the actual receipt in a Markdown fence. Unknown keys are invalid.

```text
IMPLEMENTATION_PACKET:
{
  "packet_version": 1,
  "issue_url": "<raw live URL>",
  "task_role": "pinocchio",
  "pr_url": "<raw live URL>",
  "pr_head_sha": "<full 40-character live PR head SHA>",
  "artifact": {
    "kind": "github_issue",
    "status": "<persisted or blocked>",
    "marker": "gepetto-implementation",
    "content_ref": "sha256:<64 lowercase hex characters>",
    "issue_url": "<raw live URL>",
    "observed_updated_at": "<timestamp after re-read>"
  }
}
```

For a temporary artifact, use `kind: "tmp_markdown"`, `marker: null`, and an absolute `path` instead of `issue_url` and `observed_updated_at`. Persistence is mandatory. Gepetto dispatches review only after confirming the live PR head equals `pr_head_sha`. Finish with exactly one receipt as the task final result; do not send it separately or paste proof contents into chat.
