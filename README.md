# Codex orchestration skills

Source control for three locally installed Codex skills:

- `gepetto` — coordinates tracked repository delivery work
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
python3 -m unittest checkpoint/scripts/test_checkpoint_hook.py hooks/test_orchestration_hooks.py
```
