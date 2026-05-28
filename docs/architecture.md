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
| `gtfs_definitions.py` | Static registry of supported GTFS files and their primary key columns; `get_primary_key()` helper |
| `cli.py` | Click-based CLI entry point (`gtfs-diff`); thin wrapper around `diff_feeds()` |

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

- **Disk-backed index for huge feeds** — `stop_times.txt` can exceed 10 M rows; the in-memory index should be replaced with a SQLite-backed approach for production deployments at that scale.
- **Parallel file processing** — files within a feed are currently processed sequentially; parallel workers (e.g. `concurrent.futures.ThreadPoolExecutor`) could reduce wall-clock time for feeds with many files.
- **GeoJSON / Flex location support** — `locations.geojson` and other non-CSV GTFS Flex files are not CSV and are currently reported as unsupported. Dedicated diff logic for these formats is left as future work.

