# GTFS Diff Engine

A memory-efficient Python library and CLI for comparing two GTFS feeds and producing a structured diff conforming to the [GTFS Diff v2 schema](https://github.com/MobilityData/gtfs_diff).

## Overview

GTFS Diff Engine compares two GTFS feeds (zip archives, directories, or public HTTP(S) folder URLs) file-by-file and row-by-row, emitting a machine-readable JSON document that describes exactly what changed: which files were added or deleted, which columns appeared or disappeared, and which rows were inserted, removed, or modified (with before/after field values).

The output conforms to the **GTFS Diff v2 schema** maintained by MobilityData:
<https://github.com/MobilityData/gtfs_diff>

## Features

- **Memory-efficient streaming diff** — two-pass CSV indexing; no full in-memory table loads
- **Built-in DuckDB backend for very large files** — automatically diffs eligible 50 MB+ files such as million-row `stop_times.txt` on disk without exhausting memory
- **Supports `.zip` archives, plain directories, and public HTTP(S) folder URLs** — including non-listable folders whose individual GTFS files are public
- **Row-level changes with primary key identification** — each change record includes the primary key fields for the affected row
- **Column-level change tracking** — columns added or deleted between feeds are reported with their original positions
- **Per-file and per-column change statistics** — modified files report true row-change percentages and optional per-column modification counts
- **Unreliable-diff detection (`not_compared`)** — files whose primary keys are regenerated between versions ("id churn") or missing mandatory key columns are flagged `not_compared` instead of producing a misleading row diff; id-churn thresholds are tunable globally or per file
- **Configurable row-changes cap** — limit output size per file; omitted changes are counted in a `Truncated` record
- **CLI and Python API** — use as a command-line tool or import directly in your code

## Installation

```bash
pip install gtfs-diff-engine
```

This installs the DuckDB backend used for very large files automatically, so no extra steps are required.

For a development (editable) install with test dependencies:

```bash
git clone https://github.com/MobilityData/gtfs-diff-engine
cd gtfs-diff-engine
pip install -e ".[dev]"
```

## Quick Start

```python
from gtfs_diff.engine import diff_feeds

result = diff_feeds("base.zip", "new.zip")
print(result.summary.total_changes)

# Save to JSON
with open("diff.json", "w") as f:
    f.write(result.model_dump_json(indent=2))
```

## CLI Usage

```
Usage: python -m gtfs_diff [OPTIONS] BASE_FEED NEW_FEED

  Compare two GTFS feeds and output a JSON diff.

  BASE_FEED: local path or http(s):// folder URL to the base GTFS feed

  NEW_FEED:  local path or http(s):// folder URL to the new GTFS feed

  Use optional --files with a comma-separated GTFS file list. For URLs,
  omitting --files auto-discovers known GTFS files.

Options:
  --version                       Show the version and exit.
  --files NAMES                   Comma-separated list of GTFS files to
                                  compare, e.g. 'stops.txt,trips.txt'.
                                  Optional: for folder URLs, omitting it
                                  probes all known GTFS files; for local feeds
                                  it restricts the comparison.
  -o, --output PATH               Write JSON output to FILE instead of stdout.
  -c, --cap INTEGER               Max row changes per file (0 = omit row-level
                                  detail).
  --pretty / --no-pretty          Pretty-print JSON (default: --pretty).
  --base-downloaded-at TEXT       ISO 8601 datetime for when base was
                                  downloaded.
  --new-downloaded-at TEXT        ISO 8601 datetime for when new was
                                  downloaded.
  --id-churn-threshold FLOAT RANGE
                                  Primary-key churn ratio (0.0-1.0) above
                                  which a file is reported as not_compared
                                  instead of diffed (detects regenerated ids).
                                  [default: 0.7; 0.0<=x<=1.0]
  --id-churn-threshold-for FILENAME RATIO
                                  Per-file id-churn threshold override;
                                  repeatable. Takes precedence over --id-
                                  churn-threshold. Example: --id-churn-
                                  threshold-for shapes.txt 0.95
  --large-file-threshold-mb FLOAT RANGE
                                  Files whose larger side is at least this
                                  many megabytes are diffed with the built-in
                                  DuckDB backend (lower memory for very large
                                  files). Use --no-duckdb to always use the
                                  in-memory engine.  [default: 50.0; x>=0.0]
  --no-duckdb                     Disable the DuckDB backend; always use the
                                  in-memory engine.
  --column-stats / --no-column-stats
                                  Include per-column modification counts and
                                  percentages in each modified file's stats
                                  (default: on). The file-level
                                  rows_changed_percentage is always computed.
  --help                          Show this message and exit.
```

**Examples:**

```bash
# Basic usage — print diff to stdout
gtfs-diff base.zip new.zip

# Cap row changes to 500 per file
gtfs-diff --cap 500 base.zip new.zip

# Save output to a file
gtfs-diff -o diff.json base.zip new.zip

# Compare public HTTP(S) folder feeds; auto-discovers known GTFS files
gtfs-diff https://storage.googleapis.com/example/base \
          https://storage.googleapis.com/example/new

# Non-listable folders are OK if individual files are public;
# missing files that return 403/404 are skipped
gtfs-diff https://files.mobilitydatabase.org/mdb-2126/base/extracted \
          https://files.mobilitydatabase.org/mdb-2126/new/extracted

# Compare only selected files from public HTTP(S) folder feeds
gtfs-diff https://storage.googleapis.com/example/base \
          https://storage.googleapis.com/example/new \
          --files "stops.txt,trips.txt"

# Lower the id-churn sensitivity globally (mark a file not_compared sooner)
gtfs-diff --id-churn-threshold 0.5 base.zip new.zip

# Override the id-churn threshold for specific files (repeatable)
gtfs-diff --id-churn-threshold-for shapes.txt 0.95 \
          --id-churn-threshold-for trips.txt 0.9 \
          base.zip new.zip

# Omit row-level detail (column diffs and counts are still computed)
gtfs-diff --cap 0 base.zip new.zip

# Lower the DuckDB auto-switch threshold to 10 MB
gtfs-diff --large-file-threshold-mb 10 base.zip new.zip

# Disable DuckDB and always use the in-memory engine
gtfs-diff --no-duckdb base.zip new.zip

# Omit per-column modification statistics
gtfs-diff --no-column-stats base.zip new.zip

# With feed download timestamps
gtfs-diff --base-downloaded-at 2024-01-01T00:00:00Z \
          --new-downloaded-at 2024-06-01T00:00:00Z \
          base.zip new.zip
```

## Python API Reference

### `diff_feeds()`

```python
def diff_feeds(
    base_path: str | Path,
    new_path: str | Path,
    row_changes_cap_per_file: int | None = None,
    base_downloaded_at: datetime | None = None,
    new_downloaded_at: datetime | None = None,
    id_churn_threshold: float = 0.7,
    id_churn_thresholds: Mapping[str, float] | None = None,
    files: Iterable[str] | None = None,
    large_file_threshold_bytes: int | None = 52428800,
    column_stats: bool = True,
) -> GtfsDiff
```

| Parameter | Type | Description |
|---|---|---|
| `base_path` | `str \| Path` | Path or URL to the base (old) GTFS feed — zip, directory, or HTTP(S) folder URL |
| `new_path` | `str \| Path` | Path or URL to the new GTFS feed — zip, directory, or HTTP(S) folder URL |
| `row_changes_cap_per_file` | `int \| None` | `None` = include all; `0` = omit row detail; `N` = cap at N per file |
| `base_downloaded_at` | `datetime \| None` | When the base feed was downloaded (defaults to now) |
| `new_downloaded_at` | `datetime \| None` | When the new feed was downloaded (defaults to now) |
| `id_churn_threshold` | `float` | Global primary-key churn ratio (`0.0`–`1.0`, default `0.7`) above which a file is marked `not_compared` instead of diffed |
| `id_churn_thresholds` | `Mapping[str, float] \| None` | Optional `{file_name: threshold}` per-file overrides; take precedence over `id_churn_threshold` |
| `files` | `Iterable[str] \| None` | Optional file list. Local feeds compare all discoverable files when omitted or only these files when supplied; HTTP(S) folder URLs probe all known supported GTFS files when omitted or these exact files when supplied. Non-listable folders are supported when individual files are public; missing files that return 403/404 are skipped |
| `large_file_threshold_bytes` | `int \| None` | Files whose larger side is at least this many uncompressed bytes are routed to the built-in DuckDB backend (default `52428800`, 50 MB). Pass `None` to disable DuckDB entirely and always use the in-memory engine; pass a smaller number to route more eligible files |
| `column_stats` | `bool` | When `True` (default), include per-column modification counts and percentages in each modified file's `stats.column_stats`. Pass `False` to omit that per-column breakdown; `stats.rows_changed_percentage` is still computed |

```python
# URL feeds can auto-discover known supported GTFS files
result = diff_feeds(
    "https://storage.googleapis.com/example/base",
    "https://storage.googleapis.com/example/new",
)

# Or restrict the comparison to selected files
result = diff_feeds(
    "https://storage.googleapis.com/example/base",
    "https://storage.googleapis.com/example/new",
    files=["stops.txt", "trips.txt"],
)

# Route smaller eligible files through DuckDB
result = diff_feeds(
    "base.zip",
    "new.zip",
    large_file_threshold_bytes=10 * 1024 * 1024,
)

# Or disable DuckDB entirely
result = diff_feeds("base.zip", "new.zip", large_file_threshold_bytes=None)
```

**Returns:** a `GtfsDiff` Pydantic model with three top-level fields:

| Field | Type | Description |
|---|---|---|
| `metadata` | `Metadata` | Schema version, timestamps, feed sources, unsupported files |
| `summary` | `Summary` | Aggregate counts of changed files, rows, columns |
| `file_diffs` | `list[FileDiff]` | Per-file diff records |

## Supported GTFS Files

| File | Primary Key |
|---|---|
| `agency.txt` | `agency_id` |
| `stops.txt` | `stop_id` |
| `routes.txt` | `route_id` |
| `trips.txt` | `trip_id` |
| `stop_times.txt` | `trip_id`, `stop_sequence` |
| `calendar.txt` | `service_id` |
| `calendar_dates.txt` | `service_id`, `date` |
| `fare_attributes.txt` | `fare_id` |
| `fare_rules.txt` | `fare_id`, `route_id`, `origin_id`, `destination_id`, `contains_id` |
| `shapes.txt` | `shape_id`, `shape_pt_sequence` |
| `frequencies.txt` | `trip_id`, `start_time` |
| `transfers.txt` | `from_stop_id`, `to_stop_id`, `from_route_id`, `to_route_id`, `from_trip_id`, `to_trip_id` |
| `pathways.txt` | `pathway_id` |
| `levels.txt` | `level_id` |
| `feed_info.txt` | *(all columns — single-row file)* |
| `translations.txt` | `table_name`, `field_name`, `language`, `record_id`, `record_sub_id`, `field_value` |
| `attributions.txt` | `attribution_id` |
| `areas.txt` | `area_id` |
| `stop_areas.txt` | `area_id`, `stop_id` |
| `networks.txt` | `network_id` |
| `route_networks.txt` | `route_id` |
| `fare_media.txt` | `fare_media_id` |
| `fare_products.txt` | `fare_product_id`, `rider_category_id`, `fare_media_id` |
| `fare_leg_rules.txt` | `leg_group_id` |
| `fare_transfer_rules.txt` | `from_leg_group_id`, `to_leg_group_id`, `fare_product_id`, `transfer_count`, `duration_limit` |
| `timeframes.txt` | `timeframe_group_id`, `start_time`, `end_time`, `service_id` |
| `rider_categories.txt` | `rider_category_id` |
| `booking_rules.txt` | `booking_rule_id` |
| `location_groups.txt` | `location_group_id` |
| `location_group_stops.txt` | `location_group_id`, `stop_id` |

For `translations.txt`, `record_id`, `record_sub_id`, and `field_value` are conditional primary-key columns. When some are absent from a feed, the engine keeps the full primary key and treats the missing columns as null (empty) values during comparison only, so feeds that include different subsets still align. Missing mandatory key columns mark only that file as `not_compared` with reason `missing_primary_key`; the feed diff continues, and column-level differences are still reported. Foreign-key columns in other files that reference that not-compared file are excluded from field-level diffs and listed under `ignored_columns` with reason `references_not_compared_file`, matching id-churn handling. The same optional-column padding applies to other files with conditionally-required key columns (e.g. `agency.txt`, `fare_rules.txt`, `fare_products.txt`, `fare_leg_rules.txt`, `fare_transfer_rules.txt`, `timeframes.txt`, `transfers.txt`, `attributions.txt`).

Files not in this table (e.g. GeoJSON flex locations) are recorded in `metadata.unsupported_files` and skipped.

## Output Schema

The output follows the GTFS Diff v2 schema. Below is a minimal example:

```json
{
  "metadata": {
    "schema_version": "2.0",
    "generated_at": "2024-06-01T12:00:00Z",
    "row_changes_cap_per_file": null,
    "base_feed": { "source": "base.zip", "downloaded_at": "2024-01-01T00:00:00Z" },
    "new_feed":  { "source": "new.zip",  "downloaded_at": "2024-06-01T00:00:00Z" },
    "unsupported_files": []
  },
  "summary": {
    "total_changes": 3,
    "files_added_count": 0,
    "files_deleted_count": 0,
    "files_modified_count": 1,
    "files": [
      {
        "file_name": "stops.txt",
        "status": "modified",
        "columns_added_count": 0,
        "columns_deleted_count": 0,
        "rows_added_count": 1,
        "rows_deleted_count": 0,
        "rows_modified_count": 2
      }
    ]
  },
  "file_diffs": [
    {
      "file_name": "stops.txt",
      "file_action": "modified",
      "columns_added": [],
      "columns_deleted": [],
      "row_changes": {
        "primary_key": ["stop_id"],
        "columns": ["stop_id", "stop_name", "stop_lat", "stop_lon"],
        "added": [
          {
            "identifier": { "stop_id": "S999" },
            "raw_value": "S999,New Stop,48.8566,2.3522",
            "new_line_number": 42
          }
        ],
        "deleted": [],
        "modified": [
          {
            "identifier": { "stop_id": "S001" },
            "raw_value": "S001,Central Station,48.8600,2.3470",
            "base_line_number": 5,
            "new_line_number": 5,
            "field_changes": [
              { "field": "stop_name", "base_value": "Central Stn", "new_value": "Central Station" }
            ]
          }
        ]
      },
      "stats": {
        "total_rows_base": 10,
        "total_rows_new": 11,
        "columns_added_count": 0,
        "columns_deleted_count": 0,
        "rows_added_count": 1,
        "rows_deleted_count": 0,
        "rows_modified_count": 2,
        "rows_changed_percentage": 27.27,
        "column_stats": [
          {
            "column": "stop_name",
            "modifications_count": 1,
            "modifications_percentage": 50.0
          }
        ]
      },
      "truncated": null
    }
  ]
}
```

For modified files, `stats.rows_changed_percentage` is the percentage of rows that were added, deleted, or modified relative to the larger of the two versions: `min((rows_added + rows_deleted + rows_modified) / max(total_rows_base, total_rows_new) * 100, 100)`, rounded to 2 decimals. It is `null` when both versions are empty and uses true counts, so it is not affected by `--cap` or `row_changes_cap_per_file` truncation.

`stats.column_stats` is per-column modification statistics. Only covers modified rows. Each entry has `column`, `modifications_count`, and `modifications_percentage`; the count is the number of modified rows whose value changed in that column, and the percentage is relative to total modified rows. Counts are true counts, unaffected by caps. Columns with no modified-row changes are omitted, and entries follow `row_changes.columns` order. `column_stats` is `null` when there are no modifications or when `column_stats=False` / `--no-column-stats` is used. These stats appear only for files with `file_action: "modified"`.

## Configuration

### Environment variables

- `GTFS_DIFF_DUCKDB_TMPDIR`: optional base directory for DuckDB's on-disk spill files when the DuckDB backend handles large eligible files (50 MB+ by default). Set this to a volume with enough free space if the system temp directory (for example, `/tmp`) is too small for multi-gigabyte feed comparisons. A leading `~` is expanded and the directory is created if needed. If unset or blank, the engine uses the system temp directory.

## Memory Efficiency

The engine uses a **streaming two-pass algorithm**:

1. **Pass 1 (base feed):** stream the CSV line by line, building a `primary_key_tuple → (line_number, raw_csv_string)` in-memory index.
2. **Pass 2 (new feed):** same, producing a second index.
3. **Set arithmetic:** `added = new_keys − base_keys`, `deleted = base_keys − new_keys`, `common = intersection`.
4. **Modified detection:** for keys in `common`, parse the stored raw lines and compare only the *shared* columns — this avoids flagging every row as changed when a column is added or removed.

Only the raw CSV strings are stored in the index (not parsed dicts), keeping memory proportional to the number of rows rather than rows × columns.

> **Note:** Very large eligible files are diffed with the built-in, disk-backed DuckDB backend. It auto-switches at 50 MB by default, reads remote HTTP(S) files in place through DuckDB `httpfs` range requests after Python-side `HEAD` checks, uses the in-memory engine for ineligible files, and can be disabled with `large_file_threshold_bytes=None` or `--no-duckdb`.

## Running Tests

```bash
pip install -e ".[dev]"
pytest tests/ -v
```

## Releasing a New Version

1. **Bump the version** in `pyproject.toml`:
   ```toml
   [project]
   version = "x.y.z"
   ```

2. **Commit and push** the version bump to `main`.

3. **Create a GitHub Release** via the GitHub UI (or `gh release create`):
   - Set the tag to `vx.y.z` (e.g. `v0.2.0`)
   - Write a release title and notes summarising changes
   - Click **Publish release**

4. **The publish workflow fires automatically.** The [`Publish to PyPI`](.github/workflows/publish.yml) GitHub Actions workflow triggers on release publication, builds the package, and pushes it to PyPI via [Trusted Publisher (OIDC)](https://docs.pypi.org/trusted-publishers/) — no API token required.

5. **Verify** the new version appears on [https://pypi.org/project/gtfs-diff-engine](https://pypi.org/project/gtfs-diff-engine) and is installable:
   ```bash
   pip install gtfs-diff-engine==x.y.z
   ```

> **One-time PyPI setup:** A maintainer must configure the repository as a Trusted Publisher on PyPI before the first automated release. Go to the [gtfs-diff-engine PyPI project](https://pypi.org/manage/project/gtfs-diff-engine/settings/publishing/), add a publisher for `MobilityData/gtfs-diff-engine`, workflow `publish.yml`, environment `pypi`.

## License

See [LICENSE](LICENSE).
