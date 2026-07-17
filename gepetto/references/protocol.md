# Gepetto coordination protocol

Use these packets as contracts between app tasks. Keep values concrete; use `null` only when the fact is genuinely unavailable. Never report a proposed URL, branch, task ID, or SHA as live.

Resolve `project_id` from the live Codex project list. Resolve completion scope from the user's request plus the current issue graph; stop for direction only when those conflict materially.

## Hook registration

Register the coordinator after resolving its exact task ID:

```bash
python3 /Users/dylanmccavitt/projects/codex-orchestration-skills/hooks/orchestration_state.py register --session-id <gepetto-task-id> --role gepetto
```

Immediately after `create_thread` returns, register each child before waiting or dispatching the next phase:

```bash
# Add --merge-authorized only when Jiminy has that authority.
python3 /Users/dylanmccavitt/projects/codex-orchestration-skills/hooks/orchestration_state.py register --session-id <task-id> --role <research|implementation|review|jiminy> --coordinator-thread-id <gepetto-task-id> [--merge-authorized]
```

Registration activates role-aware compaction, subagent contracts, receipt checks, and merge guards. A registration failure blocks that lane; report it separately from delivery proof.

After a terminal Gepetto or Jiminy result, disable its hooks:

```bash
python3 /Users/dylanmccavitt/projects/codex-orchestration-skills/hooks/orchestration_state.py complete --session-id <task-id>
```

## Research task prompt

Include the project path, issue URL, current default-branch SHA, repository instructions, and `coordinatorThreadId` when Gepetto resolved it. Use this request:

```text
You are a Gepetto research lane for <issue-url>. Work code-read-only. Refresh the live issue and repository first. Spawn researcher_<issue> to inspect the issue contract, relevant code/history, tests, dependencies, linked work, and conventions, then decide keep, split, consolidate, clarify, or block. Issue-write authority is <persist|propose-only>. With persist authority, GitHub is the canonical output: preserve unrelated issue text and idempotently append or replace a `<!-- gepetto-research:start -->` … `<!-- gepetto-research:end -->` section containing the full readable research contract. For keep, clarify, or block, update the source issue. For split, search duplicates, create each non-overlapping leaf with its full contract, link it when supported, and update the parent with the decision and child URLs. For consolidate, identify a related open issue, confirm their combined scope remains one independently reviewable leaf, select the canonical issue from live history and dependencies, update it with the combined contract, and update the source with the decision and canonical URL. Do not close either issue without explicit issue-close authority. Re-read every written issue and record its live URL and updatedAt. With propose-only authority, or when an attempted issue write is blocked, put the full contract in a uniquely named temporary Markdown file under `${TMPDIR:-/tmp}` and record its absolute path; a blocked persist attempt still has status blocked. Do not edit code, create branches/PRs, or perform unrelated GitHub mutations. A chat-only contract is failure. Wait for the researcher and verify the referenced artifact. Send only the compact RESEARCH_PACKET pointer receipt below to <coordinator-thread-id> when present and finish with exactly that receipt. Never paste the research contract, evidence, acceptance criteria, managed issue section, or temporary Markdown contents into chat. Never search for the parent by title.
```

## RESEARCH_PACKET

```yaml
RESEARCH_PACKET:
  issue_url: <live URL>
  repository: <owner/name>
  base_sha: <full SHA>
  issue_write_authority: persist|propose-only
  decision: keep|split|consolidate|clarify|block
  delivery_issue_urls:
    - <canonical leaf URL; list every child for split; empty for block>
  artifact:
    kind: github_issue|tmp_markdown
    status: persisted|propose-only|blocked
    marker: <gepetto-research for GitHub, null for temporary Markdown>
    locations:
      - issue_url: <raw live URL, present for a GitHub artifact>
        observed_updated_at: <timestamp after re-read>
        path: <absolute path, present for a temporary Markdown artifact>
```

The full artifact, not this receipt, contains the problem statement, evidence, scope, dependencies, leaf specifications, acceptance criteria, validation, clarifications, and blockers. For `keep`, the artifact defines one leaf matching the existing issue. For `split`, it defines at least two non-overlapping leaves and lists every child in `delivery_issue_urls`. For `consolidate`, it proves why the source is not independently useful, why the combined scope remains one leaf, and lists only the canonical issue in `delivery_issue_urls`; the artifact locations include both updated issues. For `clarify`, update the existing issue with concrete clarification additions. For `block`, persist the blocker and leave `delivery_issue_urls` empty. URLs in receipts must be raw strings, not Markdown links. Gepetto may advance only when `artifact.status` is `persisted`, except in an explicitly analysis-only run using a verified temporary Markdown artifact.

## Jiminy task prompt

Create or reuse one project-local Codex app task when dispatching the first research task. Include the exact Gepetto task ID, repository/issues, expected phase order, current child-task IDs, and merge authority. By default, use the request below unchanged: invoking Gepetto for delivery authorizes Jiminy to approve and merge every Gepetto-managed PR without a second user instruction. If the user explicitly requested analysis-only, review-only, review-ready PRs, or no merge, replace the entire merge-authority paragraph with the exact restriction and omit the merge instructions.

```text
Use $jiminy to watch Gepetto task <coordinator-thread-id> for <repository-and-issues>.
Gepetto is the sole delivery orchestrator. Monitor its research → implementation → review → reviewer-owned-fixer sequence. Message Gepetto with exact corrections when work stalls or drifts; do not create or direct delivery agents yourself.
Current child tasks: <task-ids-or-none>.

Merge authority: every Gepetto-managed PR in this delivery. The user's request to use Gepetto grants this authority; do not wait for a second user instruction. After Gepetto sends JIMINY_READY, independently decide whether each PR is approved to merge. Merge every approved PR in dependency order. For any PR not approved, report the exact blocker to Gepetto.

Send every JIMINY_PR_RESULT and final JIMINY_COMPLETE to <coordinator-thread-id> with codex_app__send_message_to_thread. Finish with the same final packet.

When any watched task sends CHECKPOINT_CONTINUATION, replace the old task ID with the confirmed successor ID, preserve its canonical role title, and leave both tasks unarchived. If Gepetto checkpoints, acknowledge the new coordinatorThreadId directly to its successor.
```

## Implementation task prompt

Include the actual leaf issue URL, the approved research artifact URL or absolute temporary Markdown path, its compact receipt, project path, default branch, base SHA, branch convention, authority to commit/push/open one PR and update the leaf issue, and `coordinatorThreadId` when available. Do not embed the full research contract in the task prompt. Use this request:

```text
You are the implementation lane for <leaf-issue-url>. Refresh remote and issue state, then read the approved research contract directly from <research-artifact-url-or-absolute-path>; do not ask for or reproduce it inline. Spawn one internal implementor agent named puppet_<issue> as the sole writer for this worktree and branch. It must implement only this leaf, add proportionate tests, run repository checks, inspect the final diff, commit with repository convention, push without force, and open one linked PR. It must not merge or close the issue. Wait for the implementor and verify its diff, checks, and live PR. Then preserve unrelated issue text and idempotently append or replace a `<!-- gepetto-implementation:start -->` … `<!-- gepetto-implementation:end -->` section on the leaf issue containing the full implementation proof: branch/base/commit/head SHAs, PR URL, changed files, exact checks and results, criterion-by-criterion proof, and caveats. Re-read the issue and record its live URL and updatedAt. If the issue write is blocked, put that full proof in a uniquely named temporary Markdown file under `${TMPDIR:-/tmp}` and record its absolute path; persistence status remains blocked. Send only the compact IMPLEMENTATION_PACKET pointer receipt below to <coordinator-thread-id> when present and finish with exactly that receipt. Never paste changed-file lists, checks, acceptance proof, the managed issue section, or temporary Markdown contents into chat.
```

## IMPLEMENTATION_PACKET

```yaml
IMPLEMENTATION_PACKET:
  issue_url: <live URL>
  task_role: puppet
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

The full artifact, not this receipt, contains branch/base/commit/head SHAs, changed files, exact checks and results, criterion-by-criterion proof, and caveats. Gepetto may dispatch review only after re-reading a persisted GitHub artifact and independently verifying its live PR head SHA. A temporary Markdown artifact preserves blocked work but does not satisfy the implementation persistence gate.

## Review task prompt

Include the issue URL, PR URL, exact expected head SHA, repository instructions, acceptance criteria, and `coordinatorThreadId` when available. Use this request:

```text
You are the independent review lane for <pr-url>. Refresh the PR and stop if its live head differs from <expected-head-sha>. Spawn one internal reviewer agent named reviewer_<pr>. It must inspect the issue contract, diff, surrounding code, tests, security/reliability implications, and repository rules, binding findings to the exact head SHA. The reviewer owns repairs: for actionable findings, it spawns bounded fixer_<pr>_<finding-id> subagents serially on this PR branch, waits for and verifies each tested, pushed fix, then independently re-reviews the new head. Repeat until blocked or no actionable findings remain and required CI is green. Do not merge. Wait for the reviewer, verify its evidence, then produce REVIEW_PACKET. If <coordinator-thread-id> is present, send the packet there with codex_app__send_message_to_thread. Finish with exactly the same packet.
```

## REVIEW_PACKET

```yaml
REVIEW_PACKET:
  issue_url: <live URL>
  pr_url: <live URL>
  reviewed_head_sha: <full SHA>
  findings:
    - id: <stable ID>
      severity: critical|high|medium|low
      disposition: fixed|blocked|accepted-by-user
      proof: <file/line, test, or commit>
  local_checks:
    - command: <exact command>
      result: pass|fail|blocked
  ci_checks:
    - name: <check name>
      conclusion: success|failure|pending|skipped
  pr_state:
    draft: true|false
    mergeable: true|false|unknown
    approvals_satisfied: true|false|unknown
    unresolved_required_threads: <number or unknown>
  blockers:
    - <blocker or none>
  ready_for_jiminy: true|false
```

`ready_for_jiminy` is false whenever the PR head changed after review, CI is pending or failing, an actionable finding remains, or a required repository gate is unknown.

## JIMINY_READY

```yaml
JIMINY_READY:
  coordinator_thread_id: <exact Gepetto task ID>
  repository: <owner/name>
  merge_authority: merge|monitoring-only
  merge_order:
    - <PR URL>
  pull_requests:
    - issue_url: <live issue URL>
      pr_url: <live PR URL>
      branch: <head branch>
      reviewed_head_sha: <full SHA>
      reviewer_task_id: <exact task ID>
      dependencies:
        - <PR URL or none>
      gates:
        review_packet_verified: true|false
        required_checks_green: true|false
        approvals_satisfied: true|false|unknown
        unresolved_required_threads: <number or unknown>
        mergeable: true|false|unknown
  gepetto_merged: false
```

Every listed PR must have a verified persisted implementation artifact and a `REVIEW_PACKET` bound to `reviewed_head_sha`. Any false or unknown required gate keeps the PR out of merge-ready state and must be reported as a blocker.
