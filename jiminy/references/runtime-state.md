# Jiminy runtime state

1. Resolve the state root from non-empty `JIMINY_STATE_DIR`, then non-empty `XDG_STATE_HOME` plus `/codex/jiminy`, otherwise `~/.local/state/codex/jiminy`. Require an absolute path outside every Git worktree.
2. Use `<state-root>/<remote-host>/<owner>/<repository>/<safe-gepetto-thread-id>.md`. Sanitize generated segments to `[A-Za-z0-9._-]`; reject empty, `.`, and `..`.
3. Create directories with mode `0700` and the log with mode `0600`. Record task IDs, issue/PR mapping, head SHAs, gate changes, blockers, nudges, authority, and verified results. Never record secrets.
4. Use the stable Gepetto task ID as the filename and its mutable title only in the header.
5. If `work/jiminy/<safe-gepetto-title>.md` conclusively identifies the same task and repository, migrate and verify it before removing the legacy file. Never merge, overwrite, or remove ambiguous state.
6. Preserve this log across checkpoint successors and record every replacement task ID.

The log is runtime state. GitHub issue sections remain the canonical research and implementation artifacts.
