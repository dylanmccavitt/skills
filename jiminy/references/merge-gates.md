# Jiminy merge gates

Apply this checklist to the current live PR head immediately before merging. Proof from another SHA is invalid.

## Inspect

Resolve the repository and PR without reading secret stores:

```bash
git status --short --branch
git remote -v
gh pr view <pr> --json url,state,isDraft,headRefName,headRefOid,baseRefName,mergeable,mergeStateStatus,reviewDecision,statusCheckRollup,closingIssuesReferences
gh pr checks <pr>
```

Use the GitHub API when `gh pr view` omits a required branch-protection, merge-queue, or unresolved-conversation fact. Refresh after any push, review, rerun, or base-branch update.

## Required gate

All conditions must hold:

1. The PR is in Jiminy's explicit authority scope.
2. The PR is open and not a draft.
3. The leaf issue and PR scope match.
4. `headRefOid` equals the Gepetto reviewer packet's `reviewed_head_sha`.
5. Required CI checks for that SHA are successful. Treat pending, cancelled, timed out, action-required, and unexplained missing checks as not green.
6. No actionable review finding remains.
7. Required approvals and required conversation resolution are satisfied.
8. GitHub does not report a conflict or blocked merge state.
9. Every dependency that must merge first is verified merged.
10. No newer user instruction revoked or narrowed merge authority.

## Select the method

Use, in order:

1. The user's explicit method.
2. Repository documentation or contribution instructions.
3. The only merge method enabled by repository settings.
4. Squash as the fallback for a single-leaf PR when multiple methods are enabled and no convention is discoverable.

Do not delete the remote branch without explicit cleanup authority.

## Execute

Bind the action to the verified head. Use the selected method flag:

```bash
gh pr merge <pr> --squash --match-head-commit <reviewed-head-sha>
```

Substitute `--merge` or `--rebase` only when selected by the rules above. If the repository requires a merge queue, use its supported queue or auto-merge path and keep monitoring; queued is not merged.

## Verify

```bash
gh pr view <pr> --json url,state,mergedAt,mergeCommit,headRefOid,closingIssuesReferences
git fetch --prune
```

Require `state: MERGED` and a merge commit before reporting success. Check every linked issue independently. Do not close an open issue without separate authority. Fast-forward a clean local default-branch checkout when safe; never disturb unrelated user changes.
