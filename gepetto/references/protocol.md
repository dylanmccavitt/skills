# Gepetto coordination protocol

Use these packets as contracts between app tasks. Keep values concrete; use `null` only when the fact is genuinely unavailable. Never report a proposed URL, branch, task ID, or SHA as live.

## Research task prompt

Include the project path, issue URL, current default-branch SHA, repository instructions, and `coordinatorThreadId` when Gepetto resolved it. Use this request:

```text
You are a Gepetto research lane for <issue-url>. Work code-read-only. Refresh the live issue and repository first. Spawn researcher_<issue> to inspect the issue contract, relevant code/history, tests, dependencies, linked work, and conventions, then decide keep, split, clarify, or block. Issue-write authority is <persist|propose-only>. With persist authority, GitHub is the canonical output: preserve unrelated issue text and idempotently append or replace a `<!-- gepetto-research:start -->` … `<!-- gepetto-research:end -->` section containing the full readable research contract. For keep, clarify, or block, update the source issue. For split, search duplicates, create each leaf with its full contract, link it when supported, and update the parent with the decision and child URLs. Re-read every written issue and record its live URL and updatedAt. Do not edit code, create branches/PRs, or perform unrelated GitHub mutations. A chat-only packet is failure. Wait for the researcher and verify GitHub persistence. Send the RESEARCH_PACKET receipt to <coordinator-thread-id> when present and finish with the same receipt. Never search for the parent by title.
```

## RESEARCH_PACKET

```yaml
RESEARCH_PACKET:
  issue_url: <live URL>
  observed_issue_updated_at: <timestamp>
  repository: <owner/name>
  base_sha: <full SHA>
  issue_write_authority: persist|propose-only
  decision: keep|split|clarify|block
  evidence:
    - <file, issue, PR, test, or history fact>
  problem_statement: <concise statement>
  in_scope:
    - <item>
  out_of_scope:
    - <item>
  dependencies:
    - <issue/PR/component or none>
  leaf_issues:
    - issue_url: <live URL when created or existing; null only for propose-only>
      title: <leaf title>
      purpose: <one independently reviewable result>
      depends_on: <leaf title/ID or none>
      acceptance_criteria:
        - <observable criterion>
      validation:
        - <test or proof>
  clarification_additions:
    - <criterion/test/edge case/documentation need>
  blockers:
    - <missing fact or authority>
  github_persistence:
    status: persisted|propose-only|blocked
    marker: gepetto-research
    issues:
      - issue_url: <raw live URL, not Markdown>
        action: created|updated
        observed_updated_at: <timestamp after re-read>
```

For `keep`, return one leaf matching the existing issue. For `split`, return at least two non-overlapping leaves and create them when authorized. For `clarify`, update the existing issue with concrete clarification additions. For `block`, persist the blocker without inventing leaves. URLs in packets must be raw strings, not Markdown links. Gepetto may advance only when `github_persistence.status` is `persisted`, except in an explicitly analysis-only run.

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

Include the actual leaf issue URL, approved research packet, project path, default branch, base SHA, branch convention, authority to commit/push/open one PR, and `coordinatorThreadId` when available. Use this request:

```text
You are the implementation lane for <leaf-issue-url>. Refresh remote and issue state. Spawn one internal implementor agent named puppet_<issue> as the sole writer for this worktree and branch. It must implement only this leaf, add proportionate tests, run repository checks, inspect the final diff, commit with repository convention, push without force, and open one linked PR. It must not merge or close the issue. Wait for the implementor, verify its diff, checks, and live PR, then produce IMPLEMENTATION_PACKET. If <coordinator-thread-id> is present, send the packet there with codex_app__send_message_to_thread. Finish with exactly the same packet.
```

## IMPLEMENTATION_PACKET

```yaml
IMPLEMENTATION_PACKET:
  issue_url: <live URL>
  task_role: puppet
  branch: <head branch>
  base_branch: <base branch>
  base_sha: <full SHA used to begin>
  commit_sha: <full final commit SHA>
  pr_url: <live URL>
  pr_head_sha: <full live PR head SHA>
  changed_files:
    - <path>
  checks:
    - command: <exact command>
      result: pass|fail|blocked
  acceptance_criteria:
    - criterion: <criterion>
      proof: <test, diff, or behavior>
  caveats:
    - <remaining caveat or none>
```

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
