# Visible acceptance criteria

- The package uses only the Python standard library and runs offline.
- Input is a JSON array of records with exactly `id`, `component`, `status`,
  and `notes`; status is `ready` or `blocked`, IDs are unique, and normalized
  records sort by ID.
- `prepare` writes canonical JSON atomically with schema version 1, normalized
  records, ready/blocked totals, and a SHA-256 binding for those records.
- `complete` starts independently, verifies the binding, and writes a stable
  Markdown report using only the checkpoint. Missing, malformed, or tampered
  state fails with a nonzero exit.
- Two different valid input sets produce their corresponding reports; output
  is not hard-coded to the sample data.
- Existing paths outside `release_builder/`, `tests/`, and `README.md` remain
  unchanged.
- `python3 -m unittest discover -s tests -p "test_*.py"` passes.
