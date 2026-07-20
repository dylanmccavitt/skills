# Codex orchestration skills

Source control for four locally installed Codex skills:

- `gepetto` — coordinates tracked repository delivery work
- `pinocchio` — delivers one approved leaf as a verified pull request
- `jiminy` — monitors Gepetto work and validates merge gates
- `checkpoint` — continues long-running work in a fresh task

The installed paths under `~/.codex/skills/` are symbolic links to these directories, so edits made through either location are tracked here.

`~/.codex/hooks.json` is a symbolic link to `hooks/hooks.json`.

## Development

Check the working tree from this repository:

```sh
git status
```

Run the checkpoint handoff tests with:

```sh
python3 -m unittest checkpoint/scripts/test_checkpoint_hook.py hooks/test_orchestration_hooks.py hooks/test_orchestration_graph.py
```

Validate the machine-readable Gepetto delivery graph with:

```sh
python3 hooks/orchestration_graph.py
```

The graph at `gepetto/references/workflow.json` describes the existing task flow,
guards, invalidation routes, and terminal states. It does not automate or replace
the active Gepetto, Pinocchio, review, or Jiminy tasks.
