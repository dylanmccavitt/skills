# Build the resumable release report

Complete the standard-library `release_builder` package in the seed repository.
The work is intentionally staged:

1. Validate and normalize the input records, then implement `prepare` so it
   writes a deterministic checkpoint.
2. Treat that checkpoint as the handoff boundary. In a fresh process, implement
   `complete` so it verifies the saved state and renders the final report
   without reading the original input.
3. Finish the visible tests and documentation while keeping the change inside
   the seed repository.

Run the visible tests before handing off the result.
