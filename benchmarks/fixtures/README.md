# Specified edit fixtures

Copy each fixture to separate disposable directories for each model. Ask the model to edit only `solution.py`; an external runner executes `python -m unittest -q` afterward.

- **ranges**: Implement `parse_ranges(text)`. Accept blank input as `[]`. Otherwise accept comma-separated nonnegative integers or inclusive `start-end` intervals with optional surrounding spaces. Reject empty or malformed segments, negative numbers, and reversed ranges with `ValueError`. Sort and merge overlapping or adjacent intervals. Return `(start, end)` tuples.
- **summary**: Implement `summarize_results(rows)`. Each row has a unique string `id`, a status in `pass|fail|skip`, and a nonnegative integer `duration_ms`. Return counts for all three statuses, total duration, and the `id` of the slowest row; ties choose the first input row. Empty input has zero counts and `slowest_id: None`. Reject invalid or duplicate values with `ValueError`. Do not mutate input rows.

These small fixtures test specified edits and checking behavior. They cannot establish performance on large production changes.
