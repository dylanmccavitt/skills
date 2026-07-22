# Gepetto coordination protocol

Use these packets as contracts between app tasks. Keep values concrete; use `null` only where a schema explicitly permits it. Never report a proposed URL, branch, task ID, or SHA as live.

Every packet is exactly one `PACKET_TYPE:` header followed by one JSON object. `packet_version` is required and currently must be `1`. Actual packets do not use Markdown fences. Unknown keys, unknown packet types or versions, Markdown-formatted URLs, non-`sha256:` content references, and abbreviated SHAs are invalid.

## Machine-readable flow

[`workflow.json`](workflow.json) is the canonical machine-readable topology for this protocol. Validate it with `python3 "${CODEX_HOME:-$HOME/.codex}/orchestration-skills/hooks/orchestration_graph.py"` before dispatch. The skills remain the behavioral contracts for active tasks; the graph only records nodes, events, guards, invalidation routes, and terminal states.

Maintain each live lane's current node, prior node as `resume_node` when blocked, review/fix cycle count, and last accepted packet/head in the persisted ledger (see Ledger). Apply these graph events explicitly:

- `MATERIAL_CONTRACT_CHANGED` returns the affected work to research.
- `MATERIAL_BASE_CHANGED` returns it to Pinocchio implementation.
- `PR_HEAD_CHANGED` invalidates head-bound proof and requires fresh review.
- `FLOW_BLOCKED` and `AUTHORITY_REQUIRED` preserve `resume_node`; resume only after the blocking fact or authority changes.
- `REVIEW_FIX_LIMIT_EXCEEDED` enters `needs_decision` using the workflow policy limit.
- `DELIVERY_CANCELLED` is terminal and does not imply destructive cleanup.

`JIMINY_PR_RESULT.reviewed_head_sha` must equal the lane's persisted reviewed `head_sha`; caller-supplied context cannot replace that ledger value.

Resolve `project_id` from the live Codex project list. Resolve completion scope from the user's request plus the current issue graph; stop for direction only when those conflict materially.

## Hook registration

Register the coordinator after resolving its exact task ID:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/orchestration-skills/hooks/orchestration_state.py" register --session-id <gepetto-task-id> --role gepetto
```

Immediately after `create_thread` returns, register each child before waiting or dispatching the next phase. Coordinator registration is authoritative:

```bash
# Add --merge-authorized only when Jiminy has that authority.
python3 "${CODEX_HOME:-$HOME/.codex}/orchestration-skills/hooks/orchestration_state.py" register --session-id <task-id> --role <research|implementation|review|jiminy> --coordinator-thread-id <gepetto-task-id> [--merge-authorized]
```

Registration activates role-aware compaction, subagent contracts, receipt checks, and merge guards. A registration failure blocks that lane; report it separately from delivery proof.

Every child registration, including Jiminy, requires an active Gepetto coordinator; Gepetto rejects a coordinator. Jiminy merge authority must be granted on its initial coordinator-owned registration and cannot be escalated by re-registration.

Hook guards prevent active child lanes from registering Gepetto, granting merge authority, or mutating another session's coordinator state, ledger, or graph. This is same-user coordination integrity, not protection from a compromised local account; keep state directories and files at `0700`/`0600`.

Each child verifies that registration without rewriting it:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/orchestration-skills/hooks/orchestration_state.py" verify --session-id <task-id> --role <research|implementation|review|jiminy> --coordinator-thread-id <gepetto-task-id>
```

Verification and conflicting re-registration fail closed.

After Gepetto has verified the terminal Jiminy result, disable the Gepetto coordinator hooks:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/orchestration-skills/hooks/orchestration_state.py" complete --session-id <gepetto-task-id>
```

Jiminy does not run `complete` before its final response. Its valid exactly-once `JIMINY_COMPLETE` Stop deactivates its own registration; an invalid or duplicate terminal packet never deactivates it.

## Ledger

Persist lane state mechanically after registering each lane and after each accepted packet or node change; the JSON deep-merges into the coordinator session file:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/orchestration-skills/hooks/orchestration_state.py" ledger set --session-id <gepetto-task-id> --lane <lane-task-id> --json '{"role": "<role>", "issue": "<url>", "pr": "<url>", "head_sha": "<sha>", "node": "<node>", "resume_node": "<node-or-null>", "review_fix_cycles": <n>}'
```

Read it back on resume or checkpoint:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/orchestration-skills/hooks/orchestration_state.py" ledger show --session-id <gepetto-task-id>
```

Checkpoint capsules point at this ledger instead of restating lane state.

Move a continued lane atomically instead of copying it with `ledger set`:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/orchestration-skills/hooks/orchestration_state.py" ledger move --session-id <gepetto-task-id> --from-lane <source-task-id> --to-lane <successor-task-id>
```

The successor receives the lane state plus `continued_from`; the source becomes a tombstone with `successor_lane`.

## Content references

Hash stable repository instructions and artifacts through the public state CLI. `context bind` hashes exact file bytes with a versioned domain and input arity, stores a `sha256:` ref, and reports `reload_required`. Repeat `--file` for multiple files; ordering is normalized.

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/orchestration-skills/hooks/orchestration_state.py" context bind --session-id <task-id> --key repository-instructions --file <AGENTS.md> [--file <nested-AGENTS.md> ...]
python3 "${CODEX_HOME:-$HOME/.codex}/orchestration-skills/hooks/orchestration_state.py" context bind --session-id <task-id> --key research-artifact --source <artifact-url-or-path> --file <exact-fetched-artifact-snapshot>
```

Load full content only when `reload_required` is true; otherwise use the returned ref. Failures leave state unchanged, refs survive continuation, and source updates require a fresh exact-byte snapshot.

## Supervision

Hooks stamp `last_heartbeat` and a compatibility `events` counter on every registered active session. Record measurable pressure before classifying whenever token telemetry is available:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/orchestration-skills/hooks/orchestration_state.py" pressure record --session-id <task-id> --context-used-tokens <used> --context-limit-tokens <limit>
```

The sample also records persisted state bytes. Classify lane liveness mechanically when refreshing task state:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/orchestration-skills/hooks/orchestration_watchdog.py" check [--json]
```

The watchdog only reports; Gepetto owns every restart. Apply its statuses explicitly:

- `stale` raises `LANE_UNRESPONSIVE` into `blocked`; replace the lane task through the checkpoint flow, registering the continuation with `continue --supervised` so the restart consumes budget.
- `over-budget` raises `RESTART_BUDGET_EXCEEDED` into `needs_decision`; stop for a user decision.
- `recycle` requests a proactive checkpoint (a planned handoff, no `--supervised`). Measured context or state pressure takes precedence; the event threshold is used only when no pressure sample exists.

TTLs, restart budget, and pressure/event thresholds come from `policies.supervision` in `workflow.json`. Pressure resets on continuation. The command exits non-zero when any lane is stale or over-budget.

## Research task prompt

Dispatch a research task only for multi-issue scope or a likely split/consolidate; otherwise Gepetto researches inline and persists the same artifact. Include the project path, issue URL, current default-branch SHA, repository instructions, and `coordinatorThreadId` when Gepetto resolved it. Use this request:

```text
You are a Gepetto research lane for <issue-url>. Work code-read-only. Verify the coordinator's authoritative `research` registration; do not register again. Refresh the live issue and repository first. Inspect the issue contract, relevant code/history, tests, dependencies, linked work, and conventions, then decide keep, split, consolidate, clarify, or block. Issue-write authority is <persist|propose-only>. With persist authority, GitHub is the canonical output: preserve unrelated issue text and idempotently append or replace a `<!-- gepetto-research:start -->` … `<!-- gepetto-research:end -->` section containing the full readable research contract. For keep, clarify, or block, update the source issue. For split, search duplicates, create each non-overlapping leaf with its full contract, link it when supported, and update the parent with the decision and child URLs. For consolidate, identify a related open issue, confirm their combined scope remains one independently reviewable leaf, select the canonical issue from live history and dependencies, update it with the combined contract, and update the source with the decision and canonical URL. Do not close either issue without explicit issue-close authority. Re-read every written issue and record its live URL and updatedAt. With propose-only authority, or when an attempted issue write is blocked, put the full contract in a uniquely named temporary Markdown file under `${TMPDIR:-/tmp}` and record its absolute path; a blocked persist attempt still has status blocked. Do not edit code, create branches/PRs, or perform unrelated GitHub mutations. A chat-only contract is failure. Verify and content-bind the referenced artifact. Finish with exactly one compact RESEARCH_PACKET as the task final result; do not send it separately. Never paste the research contract, evidence, acceptance criteria, managed issue section, or temporary Markdown contents into chat. Never search for the parent by title.
```

## RESEARCH_PACKET

```text
RESEARCH_PACKET:
{
  "packet_version": 1,
  "issue_url": "<raw live URL>",
  "repository": "<owner/name>",
  "base_sha": "<full 40-character SHA>",
  "issue_write_authority": "<persist or propose-only>",
  "decision": "<keep, split, consolidate, clarify, or block>",
  "delivery_issue_urls": ["<canonical raw leaf URL; every child for split; empty for block>"],
  "artifact": {
    "kind": "github_issue",
    "status": "<persisted for github_issue; propose-only or blocked for tmp_markdown>",
    "marker": "gepetto-research",
    "content_ref": "sha256:<64 lowercase hex characters>",
    "locations": [
      {
        "issue_url": "<raw live URL>",
        "observed_updated_at": "<timestamp after re-read>"
      }
    ]
  }
}
```

The full artifact, not this receipt, contains the problem statement, evidence, scope, dependencies, leaf specifications, acceptance criteria, validation, clarifications, and blockers. For `keep`, the artifact defines one leaf matching the existing issue. For `split`, it defines at least two non-overlapping leaves and lists every child in `delivery_issue_urls`. For `consolidate`, it proves why the source is not independently useful, why the combined scope remains one leaf, and lists only the canonical issue in `delivery_issue_urls`; the artifact locations include both updated issues. For `clarify`, update the existing issue with concrete clarification additions. For `block`, persist the blocker and leave `delivery_issue_urls` empty. A `tmp_markdown` artifact uses `marker: null` and each location contains only an absolute `path`. With `propose-only` authority it has `status: propose-only`; with `persist` authority it is permitted only after a blocked write and has `status: blocked`. A `github_issue` artifact requires `persist` authority and `status: persisted`. Gepetto may advance only when `artifact.status` is `persisted`, except in an explicitly analysis-only run using a verified temporary Markdown artifact.

## Implementation task prompt

Read [../../pinocchio/references/protocol.md](../../pinocchio/references/protocol.md) before dispatching. Include the actual leaf issue URL, approved research artifact URL or absolute temporary Markdown path, its compact receipt, project path, default branch, base SHA, branch convention, authority to commit/push/open one PR and update the leaf issue, and `coordinatorThreadId`. Do not embed the full research contract. Use this request:

```text
Use $pinocchio to deliver <leaf-issue-url> from the approved contract at <research-artifact-url-or-absolute-path> with content ref <research-content-ref>. Work in a dedicated worktree from <default-branch>@<base-sha>. You may commit, push without force, open one linked PR, and update the leaf issue; you may not merge or close it. Verify the coordinator's authoritative `implementation` registration; do not register again. Finish with exactly one `IMPLEMENTATION_PACKET` as the task final result; do not send it separately.
```

## IMPLEMENTATION_PACKET

Use Pinocchio's packet schema and gates exactly. Dispatch review only after confirming the live PR head equals `pr_head_sha`.

## Review task prompt

Include the issue URL, PR URL, exact expected head SHA, repository instructions, acceptance criteria, and `coordinatorThreadId` when available. Use this request:

```text
You are the independent review lane for <pr-url>. Verify the coordinator's authoritative `review` registration; do not register again. Refresh the PR and stop if its live head differs from <expected-head-sha>. Spawn one internal reviewer agent named reviewer_<pr>. It must inspect the issue contract, diff, surrounding code, tests, security/reliability implications, and repository rules, binding findings to the exact head SHA, and collect ALL actionable findings before any repair. The reviewer owns repairs: it runs one fixer pass covering every actionable finding, applying tested fixes serially on this PR branch, verifies each pushed fix, then re-reviews scoped to the changed delta plus the acceptance criteria and required CI. A full fresh review is required after MATERIAL_CONTRACT_CHANGED, MATERIAL_BASE_CHANGED, or any head change not produced by this fixer pass (PR_HEAD_CHANGED). Repeat until blocked or no actionable findings remain and required CI is green. Do not merge. Wait for the reviewer, verify its evidence, then finish with exactly one REVIEW_PACKET as the task final result; do not send it separately.
```

The `ACTIONABLE_FINDINGS` graph transition mechanically requires `review_fix_cycles < policies.max_review_fix_cycles` and increments the counter on entry to `fixer`. Apply it atomically with:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/orchestration-skills/hooks/orchestration_state.py" graph apply --session-id <gepetto-task-id> --lane <review-task-id> --current-node review --event ACTIONABLE_FINDINGS
```

If the command reports no eligible transition, emit `REVIEW_FIX_LIMIT_EXCEEDED`, set `ready_for_jiminy: false`, include `review_fix_limit_exceeded` in `blockers`, and return control to Gepetto for a user decision. `PR_HEAD_CHANGED` from a fixer invalidates that fixer pass and returns to review.

## REVIEW_PACKET

```text
REVIEW_PACKET:
{
  "packet_version": 1,
  "issue_url": "<raw live URL>",
  "pr_url": "<raw live URL>",
  "reviewed_head_sha": "<full 40-character SHA>",
  "findings": [
    {
      "id": "<stable ID>",
      "severity": "<critical, high, medium, or low>",
      "disposition": "<fixed, blocked, or accepted-by-user>",
      "proof": "<file/line, test, or commit>"
    }
  ],
  "local_checks": [{"command": "<exact command>", "result": "<pass, fail, or blocked>"}],
  "ci_checks": [{"name": "<check name>", "conclusion": "<success, failure, pending, or skipped>"}],
  "pr_state": {
    "draft": false,
    "mergeable": "unknown",
    "approvals_satisfied": "unknown",
    "unresolved_required_threads": "unknown"
  },
  "blockers": [],
  "ready_for_jiminy": false
}
```

`ready_for_jiminy` is false whenever the PR head changed after review, CI is pending or failing, an actionable finding remains, or a required repository gate is unknown.

After every required review packet is ready, create or reuse and authoritatively register the single Jiminy task. Only then move each ready review lane into the Jiminy-owned graph with the live runner bound mechanically:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/orchestration-skills/hooks/orchestration_state.py" graph apply --session-id <gepetto-task-id> --lane <review-task-id> --current-node review --event REVIEW_PACKET --runner-session-id <jiminy-task-id> --context-json '<packet-and-live-head-context>'
```

The state CLI verifies that the runner is an active `jiminy` registration resolving to this Gepetto coordinator and records its task ID on the lane. Pass the same current runner ID to every later graph transition whose source or target node is Jiminy-owned. After a Jiminy checkpoint, use the verified successor ID.

## Jiminy task prompt

Create or reuse one project-local Codex app task only when sending `JIMINY_READY`; dispatch this prompt with that packet. Include the exact Gepetto task ID, repository, current PR list, and merge authority. By default, use the request below unchanged: invoking Gepetto for delivery authorizes Jiminy to approve and merge every Gepetto-managed PR without a second user instruction. If the user explicitly requested analysis-only, review-only, review-ready PRs, or no merge, replace the entire merge-authority paragraph with the exact restriction and omit the merge instructions.

```text
Use $jiminy to execute the merge set for Gepetto task <coordinator-thread-id> in <repository>.
Current PRs: <pr-urls>.

Merge authority: every Gepetto-managed PR in this delivery. The user's request to use Gepetto grants this authority; do not wait for a second user instruction. Using the accompanying JIMINY_READY packet as a locator, re-apply every merge gate to each live PR head, independently decide whether each PR is approved to merge, and merge every approved PR in dependency order. For any PR not approved, report the exact blocker to Gepetto. After the final merge, run the post-merge integration gate.

Send every intermediate JIMINY_PR_RESULT and any JIMINY_INTEGRATION_FAILED to <coordinator-thread-id> with codex_app__send_message_to_thread. Finish with exactly one JIMINY_COMPLETE as the task final result; do not send that terminal packet separately.

On CHECKPOINT_CONTINUATION from Gepetto, replace the coordinator ID with the confirmed successor ID, leave both tasks unarchived, and acknowledge the successor.
```

## JIMINY_READY

```text
JIMINY_READY:
{
  "packet_version": 1,
  "coordinator_thread_id": "<exact Gepetto task ID>",
  "repository": "<owner/name>",
  "merge_authority": "<merge or monitoring-only>",
  "merge_order": ["<raw PR URL>"],
  "expected_pr_urls": ["<same raw PR URL list as pull_requests, in order>"],
  "pull_requests": [
    {
      "issue_url": "<raw live issue URL>",
      "pr_url": "<raw live PR URL>",
      "branch": "<head branch>",
      "reviewed_head_sha": "<full 40-character SHA>",
      "reviewer_task_id": "<exact task ID>",
      "research_artifact": {
        "locator": "<raw live issue URL or absolute temporary path>",
        "content_ref": "sha256:<64 lowercase hex characters>"
      },
      "implementation_artifact": {
        "locator": "<raw live issue URL or absolute temporary path>",
        "content_ref": "sha256:<64 lowercase hex characters>"
      },
      "dependencies": [],
      "gates": {
        "review_packet_verified": true,
        "required_checks_green": true,
        "approvals_satisfied": "unknown",
        "unresolved_required_threads": "unknown",
        "mergeable": "unknown"
      }
    }
  ],
  "gepetto_merged": false
}
```

Every listed PR must have a verified persisted implementation artifact and a `REVIEW_PACKET` bound to `reviewed_head_sha`. Any false or unknown required gate keeps the PR out of merge-ready state and must be reported as a blocker.

## JIMINY_PR_RESULT

```text
JIMINY_PR_RESULT:
{
  "packet_version": 1,
  "pr_url": "<raw live URL>",
  "state": "MERGED",
  "reviewed_head_sha": "<full 40-character pre-merge head SHA>",
  "merge_commit_sha": "<full 40-character verified merge commit SHA>",
  "linked_issue_url": "<raw live URL>",
  "linked_issue_state": "<OPEN or CLOSED>"
}
```

Each result is intermediate and advances no phase by itself. Persist results as an exact PR URL → verified full merge commit SHA map. Apply `MERGES_VERIFIED` through the public `graph apply` command with `ready.expected_pr_urls` and `packet.merge_results` in `--context-json`, plus `--runner-session-id <jiminy-task-id>`. It is eligible only when the result-map keys exactly equal `JIMINY_READY.expected_pr_urls` and every value is a full SHA; missing, extra, or malformed PR/commit results block integration verification.

## JIMINY_INTEGRATION_FAILED

```text
JIMINY_INTEGRATION_FAILED:
{
  "packet_version": 1,
  "coordinator_thread_id": "<exact Gepetto task ID>",
  "repository": "<owner/name>",
  "default_branch": "<branch>",
  "observed_head_sha": "<full 40-character SHA>",
  "expected_merge_commits": ["<full 40-character SHA>"],
  "failed_checks": [
    {
      "name": "<check or verification step>",
      "result": "<failure or blocked>",
      "evidence": "<live URL or exact command result>"
    }
  ],
  "remediation_required": true
}
```

This event creates a new remediation leaf. Jiminy never fixes the failure; Gepetto routes the leaf through the unchanged research → Pinocchio → review → Jiminy pipeline.

## JIMINY_COMPLETE

```text
JIMINY_COMPLETE:
{
  "packet_version": 1,
  "coordinator_thread_id": "<exact Gepetto task ID>",
  "repository": "<owner/name>",
  "default_branch": "<branch>",
  "verified_default_head_sha": "<full 40-character SHA>",
  "pull_requests": [
    {
      "pr_url": "<raw live URL>",
      "state": "MERGED",
      "merge_commit_sha": "<full 40-character SHA>"
    }
  ],
  "integration": {
    "expected_merges_present": true,
    "required_checks_green": true,
    "linked_issues_verified": true,
    "runtime_ready_for_completion": true
  },
  "blockers": [],
  "private_log_path": "<absolute path>"
}
```

Jiminy may return `JIMINY_COMPLETE` only when every integration field is true. `runtime_ready_for_completion` means every lane is terminal or safely handed off. Jiminy's validated final Stop completes its registration; Gepetto completes its own registration only after accepting and independently verifying the packet. Worktree removal remains subject to separate cleanup authority and the repository hygiene rules, and is never forced.
