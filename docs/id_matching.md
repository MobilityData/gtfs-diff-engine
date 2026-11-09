# ID matching

Some producers regenerate IDs between exports, or reuse an ID for another trip.
Comparing by ID then shows renamed entities as deleted and added. ID matching
pairs entities by content first, then compares their rows.

It is on by default. `--no-id-matching` compares by ID only.

## What is matched

Each step only pairs what earlier steps left unpaired. Pairs are one to one.

| Entity | Paired by, in order |
|---|---|
| agency | same `agency_name`, then same ID |
| stop | nearest stop within 5 m (same name first), then same ID |
| trip | same stops and times, then times shifted, then most stops shared ([below](#trips)) |
| route | most of its matched trips, then the only route with that agency, type and name, then same ID |
| service | most of its matched trips, then same ID |
| shape | most of its matched trips, the only unpaired shape of its route and direction, identical points, lines within 30 m, then same ID |

"Most of its matched trips" means each is the other's most frequent partner,
with no tie.

A kind whose file is missing from either feed is compared by ID. If matching
fails, the whole diff falls back to IDs.

## Trips

Trips have no same-ID step: a reused trip_id is what matching must catch.

**1. Same stops and times.** Same stops in the same order, same times, same
direction. Tried four ways, each looser:

1. same route name and service dates
2. same route name and weekdays
3. same weekdays, any route name (unique matches only)
4. same route name, any days (unique matches only)

**2. Times shifted.** Same stops, times moved by up to `--trip-max-shift`
minutes (15). Tried in 5-minute steps. Skipped when another trip of the route
departs in between or within 60 minutes, since the pairing could be ambiguous.
`0` turns it off.

**3. Most stops shared.** A trip that gained or lost a few stops: same route,
days and direction, at least `--trip-min-shared-stops` (90%) of its stops in
common, times within `--trip-max-shift`. Skipped when another trip of the
route departs within twice the time change. `1` turns it off.

## How the diff uses matches

- **New IDs are not changes.** A matched entity with a new ID shows no change.
  `--include-key-changes` reports them as changes to the key columns.
- **References follow matches.** Columns such as `stop_times.stop_id` or
  `trips.route_id` are read through the matches in every file.
- **Stop times pair by stop**, not by `stop_sequence`. A renumbered trip shows
  no change; a removed stop is one deleted row.
- **An unmatched new ID that an old entity also uses** is kept apart: the old
  one is deleted, the new one added.

## Matching summary

`run_diff(...).matching` holds per-entity counts and timings (not in the JSON
or the report). Trip example, from Transport for NSW:

```json
"trip": {
  "base_ids": 205112, "new_ids": 204020,
  "matched": 201818, "matched_new_id": 248,
  "unmatched_base": 3294, "unmatched_new": 2202,
  "by_step": {"exact days": 196930, "weekly pattern": 3737, "no route name": 8,
              "no service days": 1003, "time shift 5 min": 26,
              "most stops shared": 112},
  "time_shift": {"paired": 28, "declined": 78},
  "shared_stops": {"paired": 112, "least_shared": 0.9}
}
```

`declined`: trips that had a candidate but no unambiguous pair.

Shapes count `same`, `renamed` (same points), `redrawn` (under 50 m apart),
`changed` (50 m or more apart), `added` and `deleted`. The distance is measured
on every point of both shapes, and the threshold is `--highlight-shape-moved-m`,
so `changed` shapes are exactly the `shape_moved` highlights. The report calls
`redrawn` "edited" and `changed` "moved".

## Cost

| Feed | Trips | Matching | Whole diff |
|---|---|---|---|
| King County Metro | 32,000 | 1.5 s | 8.5 s |
| Transport for NSW | 205,000 | ~20 s | 93 s (67 s by ID) |

On NSW, peak memory rises from 5.6 GB to 10.3 GB.
