# Architecture

## Design Goals

- **Schema compliance** — all output conforms to the GTFS Diff v2 schema (<https://github.com/MobilityData/gtfs_diff>)
- **Memory efficiency** — stream CSV files rather than loading entire tables; store only compact raw-CSV strings in the key index
- **Clear public API** — a single `diff_feeds()` function returns a typed Pydantic model, suitable for programmatic use or JSON serialisation

## Module Structure

| Module | Responsibility |
|---|---|
| `engine.py` | Core diff logic: feed opener, CSV indexing, per-file diff, public `diff_feeds()` function |
| `models.py` | Pydantic v2 data models for the GTFS Diff v2 output format (`GtfsDiff`, `FileDiff`, `RowChanges`, etc.) |
| `gtfs_definitions.py` | Static registry of supported GTFS files, their primary keys, foreign-key relationships, and id-churn thresholds; `get_primary_key()` / `get_foreign_keys()` / `get_id_churn_threshold()` helpers |
| `cli.py` | Click-based CLI entry point (`gtfs-diff`); thin wrapper around `diff_feeds()` |

## Feed Sources

The engine accepts three feed source types:

- **Local directory** — a directory containing GTFS `.txt` files.
- **Local `.zip` archive** — including archives that wrap files in a single subdirectory.
- **HTTP(S) folder URL** — a public, unauthenticated folder-style URL whose GTFS files are addressed as `<folder_url>/<file_name>`.

`diff_feeds(..., files=...)` controls which files are considered and is optional for every feed source. For local directories and zip archives, `files` acts as a filter: when omitted, the opener lists the feed contents; when supplied, only the named GTFS `.txt` files are compared. For HTTP(S) folder URLs, supplying `files` checks exactly those names. When `files` is omitted for folder URLs, the engine probes every known GTFS file in `gtfs_definitions.SUPPORTED_FILES` (the files with primary-key definitions) and compares whichever ones exist.

Remote presence detection still supports added/deleted file reporting. For each candidate file, the engine joins the name to each folder URL and probes the resulting URL with `HEAD`; if the server rejects `HEAD`, it falls back to a ranged `GET`. A file that exists in only one version is therefore reported as added or deleted rather than being silently skipped.

For the small-file/in-memory path, remote contents are fetched lazily with `GET` only when a file is actually diffed, and are decoded as `utf-8-sig` like local files. For large eligible files routed to DuckDB, Python performs only `HEAD` probes for existence and size; DuckDB reads the file URL directly through its `httpfs` extension using HTTP range requests. The Python remote opener uses standard-library `urllib`; only public HTTP(S) URLs are supported. Authenticated feeds and `gs://` SDK access are intentionally out of scope.

### Private folders with public files

HTTP(S) folder URLs do not need to allow directory listing. The engine never requests the folder URL itself; it only requests individual file URLs formed as `<folder_url>/<file_name>`. A non-listable folder is therefore usable as long as the GTFS `.txt` files inside it are individually public.

Presence probing also tolerates object-store semantics. For example, Google Cloud Storage may return `403 Forbidden` rather than `404 Not Found` for a missing object in a private, non-listable bucket because the server will not confirm or deny existence without list permission. The engine treats `401`, `403`, `404`, and `410` probe responses as "absent or not fetchable" and skips that file, reporting it as added or deleted when it exists in only one feed. Genuine server errors such as `5xx` still propagate.

Python API example:

```python
from gtfs_diff.engine import diff_feeds

result = diff_feeds(
    "https://storage.googleapis.com/example/base",
    "https://storage.googleapis.com/example/new",
    files=["stops.txt", "trips.txt"],
)
```

CLI example:

```bash
gtfs-diff https://storage.googleapis.com/example/base \
          https://storage.googleapis.com/example/new \
          --files "stops.txt,trips.txt"
```

## Streaming Algorithm

The per-file diff (`_diff_file`) operates in two streaming passes:

### Pass 1 — index the base feed

```
for each row in base CSV:
    pk_tuple = tuple(row[col] for col in pk_columns)
    index[pk_tuple] = (line_number, raw_csv_string)
```

Only the compact `raw_csv_string` (re-serialised with `csv.writer`) is stored — not the parsed dict — to keep memory proportional to row count rather than row count × column count.

### Pass 2 — index the new feed

Identical pass over the new CSV, producing a second index.

### Set arithmetic

```
added_keys    = new_keys  − base_keys
deleted_keys  = base_keys − new_keys
common_keys   = base_keys ∩ new_keys
```

### Modified detection

For each key in `common_keys`, the stored raw lines are parsed back to dicts and compared **on shared columns only**:

```python
shared_cols = [col for col in base_headers if col in new_header_set]
field_changes = [
    FieldChange(field=col, base_value=b_dict[col], new_value=n_dict[col])
    for col in shared_cols
    if b_dict[col] != n_dict[col]
]
```

Comparing only shared columns ensures that adding or removing a column from a file does not cause every existing row to appear as modified.

### Optional primary-key columns

Most GTFS files have mandatory primary-key columns: if any are absent from a feed header, the low-level indexing helper raises `MissingPrimaryKeyError` rather than producing an unreliable row diff. The engine catches that error at the per-file boundary and reports just that file as `not_compared` with reason code `missing_primary_key`; the overall feed diff continues, and column-level differences for the affected file are still populated. As with `id_churn`, foreign-key columns in other files that reference the `not_compared` file are excluded from field-level diffs and listed under `ignored_columns` with reason code `references_not_compared_file`. Some files, however, define conditionally-present key columns. For example, `translations.txt` identifies a translation by either `record_id` (optionally with `record_sub_id`) or `field_value`; real feeds usually include only the subset required for the form they use.

For these optional primary-key columns, `_read_csv_index` keeps the file's **full** primary key and treats any optional column that is absent from a feed's headers as a null (empty) value for every row — effectively adding the missing PK header and filling it with nulls *for the compare step only*. This guarantees both feeds build their composite key over an identical set of columns, so a feed that omits an optional key column still aligns with one that includes it (instead of every row looking added/deleted). The padding affects only row identity during comparison: the injected columns are never added to the reported headers, column diff, or row values.

Which key columns are optional is derived from the GTFS Schedule reference: a primary-key column is treated as optional whenever its documented presence is anything other than "Required" (Optional, Conditionally Required, Recommended, or Conditionally Forbidden). See `gtfs_definitions.OPTIONAL_PRIMARY_KEY_COLUMNS`.

## Large files: the DuckDB backend

The in-memory two-pass engine remains the default path for every file. Very large GTFS tables, especially `stop_times.txt` with 1 M+ rows, can still make the Python key indexes expensive. For those cases the engine automatically routes an eligible modified file to the DuckDB backend, which performs the heavy set arithmetic and row scan on disk rather than keeping every row in Python dictionaries. DuckDB ships as a runtime dependency, so the backend is available out of the box.

The switch is deliberately conservative. A file is sent to DuckDB only when **all** of the following are true:

- `large_file_threshold_bytes` is not `None`;
- the larger side's uncompressed size is at least `large_file_threshold_bytes` (default `DEFAULT_LARGE_FILE_THRESHOLD_BYTES`, 50 MB);
- both feed sizes are cheaply known;
- the file has a simple, explicit primary key with no optional or conditional PK columns.

Empty-PK files and files with optional primary-key columns, such as `translations.txt`, always stay on the in-memory engine. Added and deleted files also stay on the normal code path; the DuckDB backend is only used for files present in both feeds. If the size is unknown, the PK is ineligible, or the file is below the threshold, the safe default is the in-memory engine. Any DuckDB backend error is traced and falls back to the in-memory engine (a defensive safeguard that also covers an unexpectedly missing `duckdb` install), except duplicate-primary-key `ValueError`s, which propagate just as they do in the in-memory path.

Size metadata comes from the cheapest source available for the feed type:

- local directories use `Path.stat().st_size`;
- zip feeds use each member's `ZipInfo.file_size` (uncompressed size);
- HTTP(S) folder URLs use the probe response's `Content-Length`.

DuckDB can read local paths and HTTP(S) URLs directly. Directory feeds are read in place, without copying. Remote URLs are also read in place by DuckDB through its `httpfs` extension using HTTP range requests; the extension is installed and loaded on first URL use. On the Python side, the engine only performs `HEAD` probes for existence and `Content-Length` routing, so remote files are not fully downloaded or staged to temporary files for this path. Zip members are still streamed to a temporary file, keeping memory bounded, and that file is deleted immediately after the per-file diff completes. This preserves the cleanup guarantee for staged archive contents.

Output parity is the main design constraint. The DuckDB path uses SQL only as a **superset pre-filter** for candidate changes: rows whose raw shared-column strings are distinct are streamed back to Python in batches. The final decision still uses the same `_values_differ` helper as the in-memory engine, so comparisons remain case-insensitive, whitespace-trimmed, and numeric-aware (for example, `-73.55625` and `-73.556250` are equal). The backend also reuses the same helpers for `raw_value` construction, id-churn detection, ignored foreign-key columns, line numbers (header = line 1, first data row = line 2), and cap/truncation accounting. Set-ordered outputs such as added and deleted rows may be emitted in a different order, but the records are otherwise identical.

Each file is diffed on its own short-lived in-memory DuckDB connection. The two per-file tables are dropped and the connection is closed in a `finally` block once the diff completes, so DuckDB releases their buffers before the next file is processed; any lingering process memory afterwards is the allocator returning freed pages to the OS lazily, not retained tables. DuckDB's on-disk spill is redirected to a per-file temporary directory (instead of its default `.tmp` in the current working directory) that is removed after each diff, so large-file spill never accumulates in or litters the caller's working directory.

## Change Statistics

Modified files include per-file row-change statistics and, by default, a per-column breakdown for modified rows. Both the in-memory engine (`engine.py`) and DuckDB backend (`engine_duckdb.py`) accumulate per-column modification counts while scanning the full modified set, not just the rows retained after `row_changes_cap_per_file`, so these counts are true and cap-independent.

The shared helpers `_compute_rows_changed_percentage` and `_build_column_stats` live in `diff_helpers.py`. They centralise the formulas for `rows_changed_percentage` and `column_stats`, guaranteeing parity between the two backends. The `column_stats` toggle gates only the per-column list; `rows_changed_percentage` is always computed for modified files.

## "Not Compared" Files

Some files cannot be meaningfully diffed by primary key. Rather than emitting a
misleading row-by-row diff, the engine reports such a file with
`file_action: "not_compared"`, a machine-readable `not_compared_reason`, and a
`row_changes` of `null`. Column-level differences (`columns_added` /
`columns_deleted`) are still populated.

The mechanism is generic: `id_churn`, `missing_primary_key`, and future reasons
(file too large, etc.) reuse the same `not_compared` code path by returning a
`NotComparedReason` from a detector or per-file error handler and short-circuiting
`_diff_file_modified`.

### Detecting regenerated ids (`id_churn`)

Several GTFS producers regenerate primary-key values on every export — most
notably `shape_id` (shapes.txt), `trip_id` (trips.txt) and `service_id`
(calendar*.txt). A primary-key comparison then reports nearly every row as both
added and deleted, drowning out real changes.

After the two key sets are built, the engine measures the **churn ratio** as the
complement of the [overlap coefficient](https://en.wikipedia.org/wiki/Overlap_coefficient):

```
churn_ratio = 1 − |common_keys| / min(|base_keys|, |new_keys|)
```

i.e. the fraction of the **smaller** feed's primary keys that have no match in
the other feed. When the ratio meets or exceeds the file's threshold the file is
marked `not_compared` with reason code `id_churn`, and the expensive
modification scan is skipped.

#### Why the overlap coefficient?

The denominator is what matters. Three candidates all read 0 for identical key
sets and 1 for fully disjoint sets, but they disagree on **asymmetric** sets:

| Metric | Formula | Bulk add: 100 keys ⊂ 1000 keys |
|---|---|-----------------------------|
| `÷max` | `1 − common/max(base,new)` | **0.90** false positive     |
| Jaccard | `1 − common/(base ∪ new)` | **0.90** false positive     |
| **Overlap** | `1 − common/min(base,new)` | **0.00**                    |

Both `÷max` and Jaccard penalise a feed that simply *grows* or *shrinks*: a file
that gained 900 rows looks 90% "churned" even though every original key is
preserved and perfectly matchable. That is a bulk add/delete, not id
regeneration, and flagging it as `not_compared` would hide a real, comparable
diff.

Detection is deliberately conservative and only runs when it can yield a
reliable signal:

- the file has an **explicit** primary key (empty-PK files use all columns as a
  composite key, where any field edit would look like churn);
- **both** feeds have at least `MIN_ROWS_FOR_ID_CHURN_DETECTION` rows (near-total
  turnover in a tiny file is just as likely an ordinary edit).

#### Configuring thresholds

Because GTFS files differ in how volatile their ids are, the threshold can be set
at several levels. For each file the engine resolves the threshold in this order
(highest precedence first):

1. **Caller per-file override** — a `{file_name: threshold}` mapping passed as
   `diff_feeds(id_churn_thresholds=...)`, or on the CLI via the repeatable
   `--id-churn-threshold-for FILENAME RATIO` option. This lets you tune one file
   (e.g. `{"shapes.txt": 0.95}`) without affecting others or mutating any state.
2. **Built-in per-file defaults** — `ID_CHURN_THRESHOLDS` in
   `gtfs_definitions.py`, the project's baseline domain knowledge for files whose
   keys are known to be volatile.
3. **Global threshold** — `diff_feeds(id_churn_threshold=...)` (CLI:
   `--id-churn-threshold`), applied to every file without a more specific value.
4. **`DEFAULT_ID_CHURN_THRESHOLD`** (currently `0.7`) — the ultimate fallback.

This resolution lives in `get_id_churn_threshold()`; callers never need to write
to the module-level map to customise behaviour.

### Propagating unreliable references through foreign keys

An unreliable parent key does not just affect its own file. GTFS files form a
hierarchy via foreign keys — e.g. `trips.shape_id → shapes.txt`,
`stop_times.trip_id → trips.txt`, `routes.agency_id → agency.txt`. When a parent
file's primary key churns, those same regenerated values reappear in every
child's foreign-key column; when a parent's mandatory primary-key column is
missing, child foreign keys cannot be validated against a comparable parent diff.
Comparing either column would report unreliable field changes, even though the
child rows may otherwise be identical.

To handle this, `diff_feeds()`:

1. **Orders files by dependency.** `_processing_order()` performs a deterministic
   topological sort over the present files using `GTFS_FOREIGN_KEYS`, so every
   referenced (parent) file is diffed *before* the files that reference it. Its
   `not_compared` status is therefore known in advance. (Self-references such as
   `stops.parent_station → stops` are excluded to keep the graph acyclic; cycles
   and missing parents are handled defensively.)

2. **Ignores unreliable foreign-key columns.** When a child is diffed, any
   foreign-key column pointing at a file that was marked `not_compared` due to
   `id_churn` or `missing_primary_key` is excluded from the field-level comparison
   (`_scan_modifications`) and listed in the child's `ignored_columns`, each with
   a `references_not_compared_file` reason. Primary-key columns are never ignored —
   if an unreliable referenced column is part of the child's own primary key, the
   normal per-file detection handles the child rather than treating that key as an
   ignorable field.

The net effect: a stable `trips.txt` whose only "change" is a regenerated or
uncomparable `shape_id` is correctly reported as unchanged (or shows only its
*real* edits), rather than every row appearing modified.

```jsonc
// trips.txt diff when shapes.txt was not_compared
"ignored_columns": [
  {
    "column": "shape_id",
    "reason": {
      "code": "references_not_compared_file",
      "message": "Column 'shape_id' references shapes.txt, which was not compared …"
    }
  }
]
```

Processing happens in dependency order, but the final `file_diffs` and
`summary.files` are re-sorted by file name so the output order is stable.

## Handling Edge Cases

### Files with no explicit primary key

`feed_info.txt` is a single-row file and `agency.txt` allows omitting `agency_id` when there is only one agency. Both are defined with an empty primary key list (`[]`) in `GTFS_PRIMARY_KEYS`. When the engine encounters an empty PK definition it falls back to using **all base-feed columns** as a composite key:

```python
if len(pk_def) == 0:
    pk_cols = initial_base_headers  # all columns form the key
```

### BOM handling

All files are opened with `encoding="utf-8-sig"`, which transparently strips the UTF-8 byte-order mark that some GTFS producers include.

### Malformed / short rows

Rows with fewer columns than the header are padded with empty strings; rows wider than the header are trimmed:

```python
if len(row) < n:
    row = row + [""] * (n - len(row))
row_vals = row[:n]
```

### Zip archives with a sub-directory layout

Some producers wrap all `.txt` files inside a single subdirectory within the zip. The feed opener handles this by mapping `basename → internal_path` regardless of path depth:

```python
basename = member.rsplit("/", 1)[-1]
```

## Cap and Truncation

When `row_changes_cap_per_file` is set to a positive integer `N`, row changes are filled in priority order until the cap is reached:

1. **Added** rows (up to `N`)
2. **Deleted** rows (up to `N − len(added)`)
3. **Modified** rows (up to `N − len(added) − len(deleted)`)

When `cap=0`, row-level detail is omitted entirely (column diffs and true change counts are still computed).

If the true total exceeds the cap, a `Truncated` record is attached:

```json
"truncated": { "is_truncated": true, "omitted_count": 4321 }
```

The `omitted_count` reflects the number of row changes that were detected but not included in the output.

## Column Union Ordering

When a file gains or loses columns between feeds, the `row_changes.columns` list uses a **union** ordering:

```
union_columns = base_headers + [col for col in new_headers if col not in base_header_set]
```

Base columns appear first (preserving their original order), followed by any new-only columns appended at the end. This ordering is used to align `raw_value` strings for both added and modified rows, so consumers can parse `raw_value` with a single consistent header list.

## Limitations and Future Work

- **DuckDB eligibility** — the disk-backed path is intentionally limited to modified files with simple explicit primary keys and known sizes; other files continue to use the in-memory engine.
- **Parallel file processing** — files within a feed are currently processed sequentially; parallel workers (e.g. `concurrent.futures.ThreadPoolExecutor`) could reduce wall-clock time for feeds with many files.
- **GeoJSON / Flex location support** — `locations.geojson` and other non-CSV GTFS Flex files are not CSV and are currently reported as unsupported. Dedicated diff logic for these formats is left as future work.

