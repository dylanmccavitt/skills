# Codex orchestration skills

Source control for three locally installed Codex skills:

- `gepetto` — coordinates tracked repository delivery work
- `jiminy` — monitors Gepetto work and validates merge gates
- `checkpoint-handoff` — continues long-running work in a fresh task

The installed paths under `~/.codex/skills/` are symbolic links to these directories, so edits made through either location are tracked here.

## Development

Check the working tree from this repository:

```sh
git status
```

Run the checkpoint handoff tests with:

```sh
python3 -m unittest checkpoint-handoff/scripts/test_checkpoint_hook.py
```
