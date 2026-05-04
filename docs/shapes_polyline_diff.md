# Shapes Polyline Diff Algorithm

## Overview

`shapes_polyline_diff.py` compares two GTFS `shapes.txt` files at the point level.
It is designed to be **resilient to shape ID regeneration** — feeds that renumber
all shape IDs (e.g. numeric → UUID, or a full renumber after a network restructure)
are handled correctly because shapes are matched by geometry, not by ID.

The algorithm runs in two phases:

| Phase | Name | Complexity |
|---|---|---|
| 1 | Shape matching by geometry | O(S² × P) |
| 2 | Point diffing per matched pair | O(S × P) typical, O(S × P²) worst-case |

`S` = number of shapes per feed, `P` = average points per shape.

---

## Phase 1 — Shape Matching by Geometry

### Coordinate Fingerprint

Each shape is reduced to a **coordinate fingerprint**: a `frozenset` of
`(lat, lon)` pairs rounded to 5 decimal places (~1 m precision).

```python
frozenset((round(lat, 5), round(lon, 5)) for _, lat, lon in points)
```

Rounding suppresses sub-metre noise that could prevent two otherwise identical
shapes from matching.

### Jaccard Similarity

For every pair of (base shape, new shape), the algorithm computes the
**Jaccard similarity** of their fingerprints:

```
score = |fp_base ∩ fp_new| / max(|fp_base|, |fp_new|)
```

A score of `1.0` means the two shapes share every coordinate. A score of `0.0`
means they share none.

### Greedy Matching

Shapes are matched greedily: for each base shape, the new shape with the
highest Jaccard score that has not already been claimed is selected as its match.

### Minimum Match Score Threshold

A match is only accepted if its Jaccard score meets or exceeds `min_match_score`
(default `0.05`). Pairs that fall below this threshold are **not matched** —
the base shape is reported as **removed** and the new shape as **added**.

This prevents a base shape from being matched to a completely different new shape
simply because both are the last unmatched shapes in their respective feeds.

> **Example**: if base shape `5680001` covers Montréal-Est and new shape `4070003`
> covers Montréal-Ouest, their coordinate sets share nothing. Jaccard ≈ 0.0,
> which is below the threshold, so `5680001` is reported as removed and
> `4070003` as added — not as a single modified shape.

---

## Phase 2 — Point Diffing

For each matched shape pair, points are compared by **sequence number** first,
then by **coordinate proximity** for unmatched sequences.

### Step 1 — Same Sequence Number

Points that share a sequence number in both feeds are compared directly.
If the Haversine distance between their coordinates exceeds `coord_tolerance_m`
(default `0.0` — every numeric difference is reported), a `PointMoved` record
is emitted.

### Step 2 — Sequence Reconciliation

Points whose sequence number appears in only one feed are reconciled by
coordinate proximity using `match_tolerance_m` (default `0.1` m):

- If a base-only point has a coordinate match within `match_tolerance_m` in the
  new feed's unmatched pool → `PointSeqChanged` (same location, different sequence).
- If no coordinate match is found → `PointRemoved`.
- New-only points with no coordinate match in the base pool → `PointAdded`.

### Change Types

| Type | Meaning |
|---|---|
| `PointMoved` | Same sequence number, coordinates shifted by more than `coord_tolerance_m` |
| `PointSeqChanged` | Same coordinates (within `match_tolerance_m`), different sequence number |
| `PointAdded` | Point exists only in the new feed, no coordinate match in base |
| `PointRemoved` | Point exists only in the base feed, no coordinate match in new |

---

## Output — `ShapesDiffResult`

| Field | Description |
|---|---|
| `shapes_added` | Shapes in the new feed with no geometric match in the base |
| `shapes_removed` | Shapes in the base feed with no geometric match in the new |
| `shapes_modified` | Matched shape pairs that have at least one point-level change |
| `shape_id_mapping` | `{base_id → new_id}` for every matched pair (modified or not) |
| `ids_changed` | `True` if any matched shape has a different ID in the new feed |
| `shapes_unchanged_count` | Number of matched pairs with no point-level changes |

The `format()` method produces a human-readable report. The **Shape ID
correspondence** section is omitted when all matched IDs are identical
(`ids_changed == False`), and within that section only shapes whose ID
actually changed are listed.

---

## Parameters

| Parameter | Default | Description |
|---|---|---|
| `coord_tolerance_m` | `0.0` | Distance (m) below which two co-sequenced points are considered identical. `0.0` reports every numeric difference. |
| `match_tolerance_m` | `0.1` | Distance (m) used to pair unmatched sequences by coordinate proximity (sequence-number change vs true add/remove). |
| `min_match_score` | `0.05` | Minimum Jaccard similarity for a shape pair to be considered a geometric match. Pairs below this are treated as removed + added. |

---

## Limitations and Future Work

- **Greedy matching is not globally optimal** — a Hungarian algorithm approach
  would guarantee the maximum-weight bipartite matching, at higher cost.
- **Duplicate coordinate sets** — if two shapes in the same feed are geometrically
  identical, the greedy matcher may assign the match arbitrarily.
- **Very large feeds** — Phase 1 is O(S²); feeds with tens of thousands of shapes
  may benefit from spatial indexing (e.g. bounding-box pre-filtering) to reduce
  candidate pairs before computing Jaccard scores.
