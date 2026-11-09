# Notable highlights

Changes worth checking first, such as a stop removed with nothing nearby. They
are in the diff's `highlights` section ([schema](https://github.com/MobilityData/gtfs-diff/blob/main/spec/v2/specification.md#highlights))
and at the top of the HTML report.

They need ID matching (on by default). With `--no-id-matching` there are none.

| Code | Reported when |
|---|---|
| [`service_periods_do_not_overlap`](#service_periods_do_not_overlap) | The feeds share no day of service. |
| [`service_day_without_trips`](#service_day_without_trips) | A date had trips and now has none. |
| [`stop_removed`](#stop_removed) | A stop is gone, with no new stop within 100 m. |
| [`stop_moved`](#stop_moved) | A stop moved 50 m or more. |
| [`shape_moved`](#shape_moved) | A shape moved 50 m or more somewhere. |

## Settings

| CLI | Default | Used by |
|---|---|---|
| `--as-of YYYY-MM-DD` | today | `service_day_without_trips` |
| `--highlight-stop-moved-m` | 50 | `stop_moved` |
| `--highlight-shape-moved-m` | 50 | `shape_moved` |

## In the JSON

```json
"highlights": {
  "cap_per_code": 100,
  "parameters": {"as_of": "2026-11-09", "stop_moved_m": 50.0, "shape_moved_m": 50.0},
  "counts": {"stop_removed": 1, "stop_moved": 1},
  "items": [
    {
      "code": "stop_removed",
      "message": "Stop 22197 (Covell at Sycamore / The Marketplace (EB)) was removed, with no new stop within 100 m to replace it.",
      "files": ["stops.txt"],
      "references": [{"file_name": "stops.txt", "feed": "base", "identifier": {"stop_id": "22197"}}],
      "details": {"stop_name": "Covell at Sycamore / The Marketplace (EB)", "lat": 38.560737, "lon": -121.764556, "base_trips": 188}
    }
  ]
}
```

| Field | Meaning |
|---|---|
| `cap_per_code` | Most items listed per code. |
| `parameters` | The settings used. |
| `counts` | The true number per code, including items over the cap. |
| `items` | The highlights, most important first within each code. |
| `code` | The kind. Ignore codes you don't know: the list will grow. |
| `message` | A sentence to show people. |
| `files` | The GTFS files involved. |
| `references` | The rows involved, by feed (`base` or `new`) and primary key. |
| `details` | The figures behind the message (per code, below). |

The rows in `references` always stay in `file_diffs`, whatever `--cap` is.

## The highlights

Distances are in metres. Dates are ISO 8601.

### `service_periods_do_not_overlap`

The new feed starts after the old one ends, or ends before it starts.

- Each feed's period: its first to last day with trips, from its calendars.
- Reported once, when the periods share no day.

`details`: `base_first`, `base_last`, `new_first`, `new_last`, and `gap_days`
(days without service between them). No `references`.

### `service_day_without_trips`

Dates with trips in the old feed and none in the new one.

- Only dates from `--as-of` on.
- Only dates in the new feed's period: `feed_info.txt` start and end dates,
  else its calendars' first and last days.
- Consecutive dates make one highlight.

`details`: `date_from`, `date_to`, `days`, and `base_trips_min` /
`base_trips_max` (old trips per day). No `references`.

### `stop_removed`

A boarding stop (`location_type` 0 or empty) of the old feed:

- has no match in the new feed ([how stops match](id_matching.md#what-is-matched)), and
- has no new, unmatched boarding stop within 100 m.

Listed by trips served, most first.

`details`: `stop_name`, `lat`, `lon`, `base_trips` (old trips that served it).
`references`: the old stop.

### `stop_moved`

A matched stop is at least `--highlight-stop-moved-m` from its old position.
Listed by distance, largest first.

`details`: `stop_name`, `distance_m`, `base` and `new` (`[lat, lon]`).
`references`: the stop in each feed.

### `shape_moved`

A matched shape whose points changed, and whose line is at least
`--highlight-shape-moved-m` from the other somewhere.

- Every point of each shape is measured to the other line. The largest gap
  counts.
- Measuring both ways catches a detour added and a stretch dropped.

Listed by distance, largest first.

`details`: `route` (its most common route, or null) and `distance_m`.
`references`: the shape in each feed, by `shape_id`.

## Adding a highlight

1. Add a function to `gtfs_diff/highlights.py` and call it from
   `find_highlights`. Return items in the shape above.
2. Give it a title and a short row in `report/template.html`
   (`HIGHLIGHT_KINDS`, `highlightKindNote`, `highlightRowText`).
3. Document it here and in the GTFS Diff specification's list of codes.

The schema needs no change.
