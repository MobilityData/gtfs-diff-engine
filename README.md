# GTFS Diff Engine

A memory-efficient Python library and CLI for comparing two GTFS feeds and producing a structured diff conforming to the [GTFS Diff v2 schema](https://github.com/MobilityData/gtfs_diff).

## Overview

GTFS Diff Engine compares two GTFS feeds (zip archives or directories) file-by-file and row-by-row, emitting a machine-readable JSON document that describes exactly what changed: which files were added or deleted, which columns appeared or disappeared, and which rows were inserted, removed, or modified (with before/after field values).

The output conforms to the **GTFS Diff v2 schema** maintained by MobilityData:
<https://github.com/MobilityData/gtfs_diff>

## Features

- **Memory-efficient streaming diff** — two-pass CSV indexing; no full in-memory table loads
- **Supports `.zip` archives and plain directories** — including zips with a single sub-directory layout
- **Row-level changes with primary key identification** — each change record includes the primary key fields for the affected row
- **Column-level change tracking** — columns added or deleted between feeds are reported with their original positions
- **Configurable row-changes cap** — limit output size per file; omitted changes are counted in a `Truncated` record
- **CLI and Python API** — use as a command-line tool or import directly in your code

## Installation

```bash
pip install gtfs-diff-engine
```

For a development (editable) install with test dependencies:

```bash
git clone https://github.com/your-org/gtfs_diff_engine
cd gtfs_diff_engine
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
Usage: gtfs-diff [OPTIONS] BASE_FEED NEW_FEED

  Compare two GTFS feeds (zip or directory) and output a JSON diff.

  BASE_FEED: path to the base GTFS feed (zip or directory)
  NEW_FEED:  path to the new GTFS feed (zip or directory)

Options:
  --version                       Show the version and exit.
  -o, --output FILE               Write JSON output to FILE instead of stdout.
  -c, --cap INTEGER               Max row changes per file (0 = omit row-level
                                  detail).
  --pretty / --no-pretty          Pretty-print JSON (default: --pretty).
  --base-downloaded-at TEXT       ISO 8601 datetime for when base was downloaded.
  --new-downloaded-at TEXT        ISO 8601 datetime for when new was downloaded.
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

# Omit row-level detail (column diffs and counts are still computed)
gtfs-diff --cap 0 base.zip new.zip

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
) -> GtfsDiff
```

| Parameter | Type | Description |
|---|---|---|
| `base_path` | `str \| Path` | Path to the base (old) GTFS feed — zip or directory |
| `new_path` | `str \| Path` | Path to the new GTFS feed — zip or directory |
| `row_changes_cap_per_file` | `int \| None` | `None` = include all; `0` = omit row detail; `N` = cap at N per file |
| `base_downloaded_at` | `datetime \| None` | When the base feed was downloaded (defaults to now) |
| `new_downloaded_at` | `datetime \| None` | When the new feed was downloaded (defaults to now) |

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
| `fare_products.txt` | `fare_product_id` |
| `fare_leg_rules.txt` | `leg_group_id` |
| `fare_transfer_rules.txt` | `from_leg_group_id`, `to_leg_group_id`, `transfer_count`, `duration_limit` |
| `timeframes.txt` | `timeframe_group_id`, `start_time`, `end_time`, `service_id` |
| `rider_categories.txt` | `rider_category_id` |
| `booking_rules.txt` | `booking_rule_id` |
| `location_groups.txt` | `location_group_id` |
| `location_group_stops.txt` | `location_group_id`, `stop_id` |

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
      "truncated": null
    }
  ]
}
```

## Memory Efficiency

The engine uses a **streaming two-pass algorithm**:

1. **Pass 1 (base feed):** stream the CSV line by line, building a `primary_key_tuple → (line_number, raw_csv_string)` in-memory index.
2. **Pass 2 (new feed):** same, producing a second index.
3. **Set arithmetic:** `added = new_keys − base_keys`, `deleted = base_keys − new_keys`, `common = intersection`.
4. **Modified detection:** for keys in `common`, parse the stored raw lines and compare only the *shared* columns — this avoids flagging every row as changed when a column is added or removed.

Only the raw CSV strings are stored in the index (not parsed dicts), keeping memory proportional to the number of rows rather than rows × columns.

> **Note:** For very large feeds (`stop_times.txt` with 10 M+ rows) the in-memory index may become a bottleneck. A disk-backed index (e.g. SQLite) would be more appropriate for production deployments at that scale; that optimisation is left as future work.

## Running Tests

```bash
pip install -e ".[dev]"
pytest tests/ -v
```

## License

See [LICENSE](LICENSE).
