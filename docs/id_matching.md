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
| stop | nearest stop of the same `location_type` within 5 m (same name first), then same ID |
| trip | same stops and times, then times shifted, then most stops shared, then same ID ([below](#trips)) |
| route | most of its matched trips, then the only route with that agency, type and name, then same ID |
| service | most of its matched trips, then same ID |
| shape | most of its matched trips, the only unpaired shape of its route and direction, identical points, lines within 30 m, then same ID |

"Most of its matched trips" means each is the other's most frequent partner,
with no tie.

A kind whose file is missing from either feed is compared by ID. Without
`stops.txt`, trips compare their stops by ID. If matching fails, the whole diff
falls back to IDs.

## Trips

**1. Same stops and times.** Same stops in the same order, same times, same
direction. Tried four ways, each looser:

1. same route name and service dates
2. same route name and weekdays
3. same weekdays, any route name (unique matches only)
4. same route name, any days (unique matches only)

Dates and weekdays count over the days both feeds cover. A service with none of
those days is compared by its weekdays over its own dates. Identical trips pair
by trip_id, then by headsign and short name, then in trip_id order.

**2. Times shifted.** Same stops, times moved by up to `--trip-max-shift`
minutes (15). Tried in 5-minute steps. Skipped when another trip of the route
departs in between or within 60 minutes, since the pairing could be ambiguous.
`0` turns it off.

**3. Most stops shared.** A trip that gained, lost or reordered a few stops: same route,
days and direction, at least `--trip-min-shared-stops` (90%) of its stops in
common and in the same order (each place the order breaks counts as a stop
not shared), times within `--trip-max-shift`. Skipped when another trip of the
route departs within twice the time change. `1` turns it off.

**4. Same ID.** What is left keeps its trip_id, route and direction, and starts
within `--trip-max-shift`: a trip retimed among others, or one that gained a
stop. Skipped when most matched trips changed ID, since a reused trip_id is
then a coincidence.

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

How each kind was matched, kept out of the diff JSON:

| Where | |
|---|---|
| CLI | Next to the diff: `diff.json` gives `diff.matching.json` |
| Python | `run_diff(...).matching` |
| HTML report | The **ID matching** tab |

It holds the settings, the time taken, and per entity:

| Field | Meaning |
|---|---|
| `base_ids`, `new_ids` | IDs in each feed |
| `matched` | Pairs: `matched_same_id` plus `matched_new_id` (the ID changed) |
| `unmatched_base`, `unmatched_new` | IDs left without a pair: removed or added |
| `by_step` | Pairs per step |
| `time_shift`, `shared_stops` | Trips: pairs made by steps 2 and 3 |
| `renumbered_stops` | Trips: matched trips whose `stop_sequence` values changed |
| `changes` | Shapes: kinds of change (below) |

Trip example, from Transport for NSW:

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

Shapes count `same`, `renamed` (new ID), `redrawn` (under 50 m apart),
`changed` (50 m or more apart), `added` and `deleted`. The distance is measured
on every point of both shapes, and the threshold is `--highlight-shape-moved-m`,
so `changed` shapes are exactly the `shape_moved` highlights. Lines under
`--shape-tolerance-m` (1 m) apart count as the same line, `same` or `renamed`,
so export rounding is not an edit. The report calls `redrawn` "edited" and
`changed` "moved".

### Each changed ID

`run_diff(...).id_matches` says how changed IDs were matched: `old_id`, the
step (`how`), and its figures. It covers the IDs in the diff's modified rows and
the first 100 of each step. The HTML report lists them in its ID matching tab,
and behind a **?** next to each changed ID in the Files tab.

| Step | Figures |
|---|---|
| stop `same place` | `meters` apart, `same_name` |
| trip `time shift`, `most stops shared` | `shift_s` (largest time change), `shared` (share of stops) |
| `trip votes` | `trips`: matched trips linking the two |

## Cost

| Feed | Trips | Matching | Whole diff |
|---|---|---|---|
| King County Metro | 32,000 | 1.5 s | 8.5 s |
| Transport for NSW | 205,000 | ~20 s | 93 s (67 s by ID) |

On NSW, peak memory rises from 5.6 GB to 10.3 GB.
