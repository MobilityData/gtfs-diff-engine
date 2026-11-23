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
| [`stop_added`](#stop_added) | A stop is new, with no removed stop within 100 m. |
| [`stop_order_changed`](#stop_order_changed) | A trip calls at its stops in another order. |
| [`stop_moved`](#stop_moved) | A stop moved 50 m or more. |
| [`shape_moved`](#shape_moved) | A shape moved 50 m or more somewhere. |
| [`fare_changed`](#fare_changed) | A fare's price changed by 50% or more. |
| [`feature_added`](#feature_added) | The new feed uses a fares or pathways feature the old one did not. |
| [`feature_removed`](#feature_removed) | The old feed used a fares or pathways feature the new one does not. |

## Settings

| CLI | Default | Used by |
|---|---|---|
| `--as-of YYYY-MM-DD` | today | `service_day_without_trips` |
| `--highlight-stop-moved-m` | 50 | `stop_moved` |
| `--highlight-shape-moved-m` | 50 | `shape_moved` |
| `--highlight-fare-changed-pct` | 50 | `fare_changed` |

## In the JSON

```json
"highlights": {
  "cap_per_code": 100,
  "parameters": {"as_of": "2026-11-09", "stop_moved_m": 50.0, "shape_moved_m": 50.0, "fare_changed_pct": 50.0},
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

### `stop_added`

A boarding stop of the new feed:

- has no match in the old feed, and
- has no old, unmatched boarding stop within 100 m.

Listed by trips served, most first.

`details`: `stop_name`, `lat`, `lon`, `new_trips` (new trips that serve it),
`routes` (their names, most trips first).
`references`: the new stop.

### `stop_order_changed`

A matched trip whose shared stops come in another order. Stops added or
removed do not count: 1-2-3-4-5 to 1-3-2-4-5 is reported, 1-2-3-4-5 to
1-2-3-4-4b-5 or to 1-2-3-4 is not.

- Only stops visited once by each trip are compared.
- The stops that changed place are those outside the longest run still in
  the old order.
- One highlight per route and set of stops that changed place.

Listed by trips, most first.

`details`: `route`, `route_id`, `trips`, `stops` (each `stop_id` with
`base_after` and `new_after`, the stop before it in each feed, null when
first), `base_order` and `new_order` (the example trip's shared stops in each
feed's order, by new `stop_id`), `stop_names`.
`references`: one example trip's `stop_times.txt` row for the first stop, in
each feed.

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

### `fare_changed`

A fare with the same ID and currency in both feeds whose price changed by at
least `--highlight-fare-changed-pct`, up or down.

- Fares v1: `fare_attributes.txt`, by `fare_id` (`price`).
- Fares v2: `fare_products.txt`, by `fare_product_id`, `rider_category_id` and
  `fare_media_id` (`amount`).
- A fare that became free (5 to 0) is a 100% drop. A fare that was free and
  now costs something has no percentage and is always reported.
- Fares in another currency in the new feed are not compared.

Listed by size of change, largest first, fares that were free first.

`details`: `base_price`, `new_price`, `currency`, `change_pct` (null when the
fare was free), and `fare_id` and `routes` (from `fare_rules.txt`, v1) or
`fare_product_id`, `fare_product_name`, `rider_category_id`, `fare_media_id`
and `routes` (v2, from `fare_leg_rules.txt` and `route_networks.txt`).
`references`: the fare row in each feed.

### `feature_added`, `feature_removed`

A GTFS feature the new feed uses and the old feed did not (`feature_added`),
or the reverse (`feature_removed`). One highlight per feature. Only fares and
pathways features are checked, defined as in the
[GTFS Validator](https://github.com/MobilityData/gtfs-validator/blob/master/docs/FEATURES.md)
(`FeedMetadata.java`):

| Group | Features |
|---|---|
| Fares | Fares V1, Fare Products, Fare Transfers, Fare Media, Rider Categories, Time-Based Fares, Zone-Based Fares, Route-Based Fares, Contactless EMV Support |
| Pathways | Pathway Connections, Pathway Signs, Pathway Details, In-station Traversal Time, Levels |

A feature is used when its files have a record, and the fields it needs are
set (for example Rider Categories needs `rider_categories.txt` and a
`rider_category_id` in `fare_products.txt`).

The files and fields are those of the [GTFS features
pages](https://gtfs.org/getting_started/features/overview/) on gtfs.org, which
list them without saying when a feature counts as present. Where they differ
from the validator, both must hold:

| Feature | In addition to the validator |
|---|---|
| Zone-Based Fares | a record in `stop_areas.txt` |
| Route-Based Fares | `route_networks.txt` also counts, besides `networks.txt` and `routes.network_id` |
| Levels | a `level_id` in `stops.txt` |

`details`: `feature`, `group`, `fields` (the files and fields the feature lists on
gtfs.org), `url` (the feature's page on gtfs.org, which the
HTML report links to). No `references`.

## Adding a highlight

1. Add a function to `gtfs_diff/highlights.py` and call it from
   `find_highlights`. Return items in the shape above.
2. Give it a title and a short row in `report/template.html`
   (`HIGHLIGHT_KINDS`, `highlightKindNote`, `highlightRowText`).
3. Document it here and in the GTFS Diff specification's list of codes.

The schema needs no change.
