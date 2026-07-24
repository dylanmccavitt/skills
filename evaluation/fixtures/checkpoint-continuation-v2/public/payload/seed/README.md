# Resumable release report

Run the visible tests:

```sh
python3 -m unittest discover -s tests -p "test_*.py"
```

The two delivery stages are separate commands:

```sh
python3 -m release_builder.cli prepare --input records.json --checkpoint state.json
python3 -m release_builder.cli complete --checkpoint state.json --output report.md
```
