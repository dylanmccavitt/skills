# Repository Instructions

## Code Review Rules

### Atomic orchestration state

- Flag state transitions that bypass the registry lock, expected-revision check, atomic replacement, or continuation journal/recovery path. Safe path: route mutations through `orchestration_state.py` primitives and prove conflicts plus crash recovery in tests.

### Exact-head delivery gates

- Flag review, CI, evidence, or merge authorization that remains valid after the PR head changes, or a merge path that does not refresh the live head first. Safe path: bind every receipt and authorization to the exact current head SHA and invalidate it on drift.

### Non-destructive installation

- Flag installer or uninstall behavior that replaces unrelated skills, removes unmanaged hook entries, follows unsafe symlinks, or destroys configuration without a recoverable backup. Safe path: mutate only package-owned paths and managed hook entries, reject ownership ambiguity, and keep install/uninstall idempotent.
