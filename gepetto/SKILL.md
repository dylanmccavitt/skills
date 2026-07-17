---
name: gepetto
description: "Run live Codex orchestration for tracked repository work. Use when the user invokes Gepetto or asks Codex to coordinate issues through separate research, implementation, and review tasks. Gepetto remains the sole orchestrator, requires researchers to persist their contracts in GitHub issues, enforces phase barriers and one writer per worktree, and launches Jiminy with the first researcher as a watchdog and merge operator. Do not use for a direct edit without requested orchestration."
---

# Gepetto

Act as the sole live coordinator, never an automation. Create and direct separate Codex app tasks for research, implementation, and review. Jiminy watches those tasks, reports drift to Gepetto, approves PRs for merge, and merges approved PRs; it never orchestrates delivery work.

Read [references/protocol.md](references/protocol.md) before creating a child task. Use its prompts and packet schemas.

## Invariants

- Refresh Git, GitHub, repository instructions, Codex projects, and task state before acting and after every return or head change.
- Run phases in order: all required research packets → approved leaf map → implementation packets → reviewer packets. Never dispatch a later phase on an unverified earlier-phase assertion.
- Create one app task per delivery lane. Each delivery task spawns its named internal agent and waits for it; Jiminy is the separate watchdog task.
- Reuse a matching live task; never duplicate a role/issue/branch lane.
- Keep one writer per branch and worktree. Implementation and reviewer fix work use separate project worktrees tied to the correct branch.
- Pass `coordinatorThreadId` when uniquely resolved. Require research and implementation tasks to send their compact artifact receipts to Gepetto and finish with the same receipt; reviewer tasks still send their review packet. Otherwise read the final response directly. Full research contracts and implementation proofs belong only in their referenced artifacts, never inline in task chat.
- Bind review and CI proof to the exact PR head SHA. Any push invalidates both.
- Treat `CHECKPOINT_CONTINUATION` as a task-ID replacement, not a new lane. Keep the source task unarchived, give its successor the lane's canonical title, update the ledger, and tell Jiminy the exact replacement ID.

Control flow: Gepetto launches Jiminy with the first research task; Gepetto then orchestrates researcher → implementer → reviewer → reviewer-owned fixers, while Jiminy monitors throughout and returns merge/completion proof to Gepetto.

## Start and track

1. Resolve the repository, issues, authority, and completion condition from the request. A request to use Gepetto for delivery authorizes Jiminy to act as merge operator for every Gepetto-managed PR in that delivery. Jiminy independently decides whether each PR is approved to merge, then merges every approved PR. Narrow this to monitoring-only only when the user explicitly requests analysis-only, review-only, review-ready PRs, or no merge. Gepetto itself never merges.
2. Inspect the current branch/status, remotes, head SHA, default branch, `AGENTS.md`, relevant issues/PRs, and remote state.
3. Resolve the exact Codex project ID. Repository child tasks must use that project.
4. Rename this task `<Project> - Gepetto - <issue or subject>`. Resolve its task ID only by a unique match; never guess.
5. List existing tasks and reuse matching lanes.
6. Maintain a compact ledger: task IDs, role, issue, branch/worktree owner, PR, live head SHA, phase, and last verified gate.

If Gepetto itself checkpoints, rename the source `<canonical title> - checkpoint <short source id>`, rename the successor to the canonical title, keep both tasks unarchived, and send Jiminy `CHECKPOINT_CONTINUATION role=gepetto` with both exact IDs. Resume only after Jiminy is watching the successor.

If the user requested analysis only, return the issue map without tracker mutations. End-to-end Gepetto authority includes creating leaf issues, updating issue bodies with managed research contracts and implementation proofs, and delegating merge approval and execution to Jiminy; pass the corresponding issue-write authority to research and implementation lanes and merge authority to Jiminy. Do not invent a monitoring-only restriction from the absence of the literal word “merge,” and do not require a second user instruction after Jiminy approves the PR. Only an explicit user restriction removes Jiminy's merge authority.

## Phase 1: research

When creating the first research task, create or reuse one dedicated Jiminy Codex app task in the same project's local environment. This is one dispatch step: Jiminy must be running no later than the first researcher. Pass the exact `coordinatorThreadId`, repository/issues, expected phase order, current child-task IDs, and merge-authority scope. Default that scope to every Gepetto-managed PR in this delivery and tell Jiminy to approve and merge ready PRs without waiting for a second user instruction. Use monitoring-only only when quoting the user's explicit restriction. Record `jiminyThreadId` in Gepetto's ledger.

Create all required research tasks in the project's local environment. Each task must spawn `researcher_<issue>`, remain code-read-only, persist one evidence-backed decision as an artifact, and return only a compact `RESEARCH_PACKET` receipt pointing to that artifact. End-to-end research has authority to create and update GitHub issues but may not edit code, create branches/PRs, or perform unrelated GitHub mutations.

- `keep`: the issue is one reviewable leaf.
- `split`: create ordered, non-overlapping leaf specifications.
- `clarify`: strengthen criteria, tests, edge cases, or proof without padding scope.
- `block`: identify the missing fact or authority.

The GitHub issue body is the canonical research artifact. Before returning, every authorized researcher must persist its completed contract in GitHub using an idempotent managed section while preserving unrelated issue text:

- `keep`, `clarify`, or `block`: update the source issue body with the full research contract.
- `split`: create each justified leaf with its full contract, then update the parent with the decision and live child links.

When issue writes are explicitly `propose-only`, or an attempted write is blocked, write the full contract to a uniquely named temporary Markdown file and return its absolute path. A blocked write remains blocked for end-to-end delivery; the temporary file preserves the work but does not satisfy the GitHub persistence gate.

The chat `RESEARCH_PACKET` is only a pointer receipt. It must include the decision, persistence status, and re-read issue URLs/timestamps or temporary Markdown path. Never copy the contract, evidence list, acceptance criteria, managed issue section, or Markdown file contents into chat. A chat-only packet is incomplete. Gepetto must re-read the referenced artifact and verify its content before implementation; only analysis-only `propose-only` runs may advance from a temporary Markdown artifact.

## Phase 2: implementation

For every approved leaf, create a project worktree task from the correct base branch. Each task must spawn `puppet_<issue>` as the sole writer, implement only that leaf, test it, commit, push without force, open one linked PR, persist the full implementation proof in an idempotent `gepetto-implementation` section on the leaf issue, and return only a compact `IMPLEMENTATION_PACKET` pointer receipt. Preserve the issue's unrelated text and research section.

If the leaf issue cannot be updated, write the full implementation proof to a uniquely named temporary Markdown file and return its absolute path. The chat receipt must never repeat changed-file lists, check output, acceptance-criteria proof, or the Markdown contents. A failed required issue write remains a blocked persistence gate even when the temporary file preserves the proof.

Respect dependencies and use one leaf → one branch → one worktree → one PR. Re-read the referenced implementation artifact and verify the returned PR and live head SHA before review.

## Phase 3: review and fixes

For each verified PR, create a fresh reviewer worktree task starting from its head branch. Each task must spawn `reviewer_<pr>` to review the exact live head SHA against the issue contract, diff, tests, repository rules, and CI.

The reviewer owns its fix loop:

1. For each actionable finding, the reviewer spawns a bounded `fixer_<pr>_<finding>` subagent on the same PR branch.
2. The reviewer waits for writing fixers serially and verifies each tested, pushed repair.
3. The reviewer performs a fresh independent review of the new head.
4. Repeat until blocked or no actionable findings remain and required CI is green.
5. The reviewer returns one final `REVIEW_PACKET` to its app task, which verifies and forwards it to Gepetto. Never merge.

Gepetto independently verifies the packet against the live PR head. Correct drift by messaging the owning task; replace it only when unusable.

## Complete through Jiminy

When every leaf is review-ready, build `JIMINY_READY` with the Gepetto task ID/title, issue and PR URLs, branches, exact reviewed head SHAs, reviewer task IDs, green-check evidence, merge order, dependencies, and confirmation that Gepetto did not merge.

Send `JIMINY_READY` to the existing `jiminyThreadId`; do not create a second Jiminy task. Jiminy independently decides whether each PR is approved to merge and merges every approved PR within the delivery scope unless the user explicitly withheld merge authority. Gepetto remains responsible for directing any research, implementation, reviewer, or fixer remediation Jiminy requests.

Keep Gepetto available for Jiminy's result. On `JIMINY_PR_RESULT` or `JIMINY_COMPLETE`, verify the reported live PR state and finish the Gepetto task with every PR as a clickable Markdown link plus its state and merge commit when merged. Do not make the user find the PR in a child task.

Keep blocked lanes separate from validated work and merge readiness false.
