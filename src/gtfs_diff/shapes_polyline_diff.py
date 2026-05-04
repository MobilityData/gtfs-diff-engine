"""
Two-Phase Content-Addressed Polyline Diff for GTFS shapes.txt files.

Algorithm summary
-----------------
Phase 1 — Shape matching  O(S² × P):
    Build a coordinate fingerprint (set of rounded lat/lon pairs) for each shape.
    Match shapes across feeds by maximising Jaccard similarity of those fingerprints,
    independent of shape_id values.  This makes the comparison resilient to ID
    regeneration (e.g. numeric → UUID) between feed versions.

Phase 2 — Point diffing  O(S × P) typical, O(S × P²) worst-case:
    For each matched shape pair compare points by sequence number first.
    Any sequence numbers present in only one feed are reconciled by coordinate
    proximity: same location → sequence-number change; otherwise → true add/remove.

Complexity notation
-------------------
    S  number of shapes per feed
    P  average number of points per shape
    N  = S × P  total points
"""

from __future__ import annotations

import csv
import math
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Union

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Coords:
    lat: float
    lon: float

    def __str__(self) -> str:
        return f"({self.lat}, {self.lon})"


@dataclass(frozen=True)
class PointMoved:
    """A point whose coordinates changed (matched by sequence number)."""
    sequence: int
    base_coords: Coords
    new_coords: Coords
    distance_m: float


@dataclass(frozen=True)
class PointSeqChanged:
    """A point with identical coordinates but a different sequence number."""
    base_sequence: int
    new_sequence: int
    coords: Coords


@dataclass(frozen=True)
class PointAdded:
    """A point present only in the new feed with no coordinate match in base."""
    sequence: int
    coords: Coords


@dataclass(frozen=True)
class PointRemoved:
    """A point present only in the base feed with no coordinate match in new."""
    sequence: int
    coords: Coords


PointChange = Union[PointMoved, PointSeqChanged, PointAdded, PointRemoved]


@dataclass
class ShapeDiff:
    """Diff for a geometrically matched shape pair."""
    base_shape_id: str
    new_shape_id: str
    changes: list[PointChange] = field(default_factory=list)

    @property
    def is_modified(self) -> bool:
        return bool(self.changes)


@dataclass(frozen=True)
class ShapeAdded:
    """A shape present only in the new feed (no geometric match in base)."""
    shape_id: str


@dataclass(frozen=True)
class ShapeRemoved:
    """A shape present only in the base feed (no geometric match in new)."""
    shape_id: str


@dataclass
class ShapesDiffResult:
    """Top-level result of a content-addressed polyline diff of two shapes.txt files."""
    base_file: str
    new_file: str
    shapes_added: list[ShapeAdded] = field(default_factory=list)
    shapes_removed: list[ShapeRemoved] = field(default_factory=list)
    shapes_modified: list[ShapeDiff] = field(default_factory=list)
    # base_id -> new_id for every geometrically matched shape (modified or not)
    shape_id_mapping: dict[str, str] = field(default_factory=dict)

    @property
    def shapes_unchanged_count(self) -> int:
        return len(self.shape_id_mapping) - len(self.shapes_modified)

    def format(self) -> str:
        lines = [
            f"base : {self.base_file}",
            f"new  : {self.new_file}",
            "",
            "Shape ID correspondence (base -> new)",
            "─" * 60,
        ]
        for base_id, new_id in sorted(self.shape_id_mapping.items()):
            if base_id == new_id:
                continue
            modified = any(sd.base_shape_id == base_id for sd in self.shapes_modified)
            tag = "  [MODIFIED]" if modified else ""
            lines.append(f"  {base_id} -> {new_id}{tag}")
        for sr in self.shapes_removed:
            lines.append(f"  {sr.shape_id} -> (removed)")
        for sa in self.shapes_added:
            lines.append(f"  (added) -> {sa.shape_id}")

        lines += [
            "",
            "Summary",
            "─" * 60,
            f"  added shapes    : {len(self.shapes_added)}",
            f"  removed shapes  : {len(self.shapes_removed)}",
            f"  modified shapes : {len(self.shapes_modified)}",
            f"  unchanged shapes: {self.shapes_unchanged_count}",
        ]

        if self.shapes_modified:
            lines += ["", "Modifications", "─" * 60]
            for sd in self.shapes_modified:
                id_label = (
                    sd.base_shape_id
                    if sd.base_shape_id == sd.new_shape_id
                    else f"{sd.base_shape_id} -> {sd.new_shape_id}"
                )
                lines.append(f"\n  Shape {id_label}: MODIFIED")
                for ch in sd.changes:
                    if isinstance(ch, PointMoved):
                        lines.append(
                            f"    Point seq={ch.sequence}: MOVED"
                            f"  {ch.base_coords} -> {ch.new_coords}"
                            f"  ({ch.distance_m:.4f}m)"
                        )
                    elif isinstance(ch, PointSeqChanged):
                        lines.append(
                            f"    Point {ch.coords}: SEQ CHANGED"
                            f"  seq={ch.base_sequence} -> seq={ch.new_sequence}"
                        )
                    elif isinstance(ch, PointAdded):
                        lines.append(f"    Point seq={ch.sequence}: ADDED  {ch.coords}")
                    elif isinstance(ch, PointRemoved):
                        lines.append(f"    Point seq={ch.sequence}: REMOVED  {ch.coords}")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# (seq, lat, lon) triples sorted by sequence
_ShapePoints = list[tuple[int, float, float]]


def _trace(msg: str) -> None:
    """Print a timestamped progress line to stderr."""
    from datetime import datetime
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[shapes-diff {ts}] {msg}", file=sys.stderr, flush=True)


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _parse_shapes(path: str | Path) -> dict[str, _ShapePoints]:
    _trace(f"Parsing {path} ...")
    shapes: dict[str, _ShapePoints] = defaultdict(list)
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            shapes[row["shape_id"]].append((
                int(row["shape_pt_sequence"]),
                float(row["shape_pt_lat"]),
                float(row["shape_pt_lon"]),
            ))
    for pts in shapes.values():
        pts.sort()
    total_pts = sum(len(p) for p in shapes.values())
    _trace(f"  -> {len(shapes)} shapes, {total_pts} points")
    return dict(shapes)


def _coord_fingerprint(pts: _ShapePoints, decimals: int = 5) -> frozenset[tuple[float, float]]:
    """Rounded coordinate set used for Jaccard-based shape matching."""
    return frozenset((round(lat, decimals), round(lon, decimals)) for _, lat, lon in pts)


def _match_shapes_by_geometry(
    s1: dict[str, _ShapePoints],
    s2: dict[str, _ShapePoints],
    min_match_score: float = 0.05,
) -> dict[str, tuple[str, float]]:
    """
    Phase 1: greedily match each shape in s1 to the best-scoring shape in s2
    using Jaccard similarity of coordinate fingerprints.

    Pairs whose best score is below min_match_score are not matched and will
    be reported as removed/added rather than modified.

    Returns {base_id: (new_id, score)}.
    """
    fp2 = {sid: _coord_fingerprint(pts) for sid, pts in s2.items()}
    matches: dict[str, tuple[str, float]] = {}
    used: set[str] = set()

    total = len(s1)
    _trace(f"Phase 1 — shape matching: {total} shapes to match ...")
    for i, (sid1, pts1) in enumerate(s1.items(), 1):
        fp1 = _coord_fingerprint(pts1)
        best_id, best_score = None, -1.0
        for sid2, fp in fp2.items():
            if sid2 in used:
                continue
            score = len(fp1 & fp) / max(len(fp1), len(fp), 1)
            if score > best_score:
                best_score, best_id = score, sid2
        if best_id is not None and best_score >= min_match_score:
            matches[sid1] = (best_id, best_score)
            used.add(best_id)
        if i % max(1, total // 10) == 0 or i == total:
            _trace(f"  {i}/{total} shapes matched")

    _trace(f"  -> {len(matches)} matched, {len(s1) - len(matches)} unmatched")
    return matches


def _diff_shape_points(
    pts1: _ShapePoints,
    pts2: _ShapePoints,
    coord_tolerance_m: float,
    match_tolerance_m: float,
) -> list[PointChange]:
    """
    Phase 2: produce a list of PointChange records for one matched shape pair.
    """
    by_seq1 = {seq: (lat, lon) for seq, lat, lon in pts1}
    by_seq2 = {seq: (lat, lon) for seq, lat, lon in pts2}
    seqs1, seqs2 = set(by_seq1), set(by_seq2)

    changes: list[PointChange] = []

    # Points present in both feeds under the same sequence number
    for seq in seqs1 & seqs2:
        lat1, lon1 = by_seq1[seq]
        lat2, lon2 = by_seq2[seq]
        d = _haversine_m(lat1, lon1, lat2, lon2)
        if d > coord_tolerance_m:
            changes.append(PointMoved(
                sequence=seq,
                base_coords=Coords(lat1, lon1),
                new_coords=Coords(lat2, lon2),
                distance_m=d,
            ))

    # Reconcile unmatched sequences by coordinate proximity
    unmatched1 = {seq: by_seq1[seq] for seq in seqs1 - seqs2}
    unmatched2 = {seq: by_seq2[seq] for seq in seqs2 - seqs1}

    used2: set[int] = set()
    for seq1, (lat1, lon1) in sorted(unmatched1.items()):
        paired = next(
            (
                (seq2, lat2, lon2)
                for seq2, (lat2, lon2) in unmatched2.items()
                if seq2 not in used2 and _haversine_m(lat1, lon1, lat2, lon2) <= match_tolerance_m
            ),
            None,
        )
        if paired:
            seq2, lat2, lon2 = paired
            used2.add(seq2)
            changes.append(PointSeqChanged(
                base_sequence=seq1,
                new_sequence=seq2,
                coords=Coords(lat1, lon1),
            ))
        else:
            changes.append(PointRemoved(sequence=seq1, coords=Coords(lat1, lon1)))

    for seq2, (lat2, lon2) in sorted(unmatched2.items()):
        if seq2 not in used2:
            changes.append(PointAdded(sequence=seq2, coords=Coords(lat2, lon2)))

    return changes


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def content_addressed_polyline_diff(
    base_file: str | Path,
    new_file: str | Path,
    coord_tolerance_m: float = 0.0,
    match_tolerance_m: float = 0.1,
    min_match_score: float = 0.05,
) -> ShapesDiffResult:
    """
    Compare two GTFS shapes.txt files using a Two-Phase Content-Addressed
    Polyline Diff.

    Args:
        base_file:         Path to the base shapes.txt.
        new_file:          Path to the new shapes.txt.
        coord_tolerance_m: Distance (metres) below which two points at the same
                           sequence number are considered identical.
                           Default 0.0 reports every numeric difference.
        match_tolerance_m: Distance (metres) used to pair unmatched points by
                           coordinate proximity when resolving sequence-number
                           changes vs true adds/removes. Default 0.1 m.
        min_match_score:   Minimum Jaccard similarity (0–1) required to consider
                           two shapes a geometric match. Pairs below this threshold
                           are reported as removed/added rather than modified.
                           Default 0.05.

    Returns:
        ShapesDiffResult containing added, removed, and modified shapes with
        point-level detail for each modification.
    """
    s1 = _parse_shapes(base_file)
    s2 = _parse_shapes(new_file)

    matches = _match_shapes_by_geometry(s1, s2, min_match_score)
    matched_new_ids = {new_id for new_id, _ in matches.values()}

    result = ShapesDiffResult(
        base_file=str(base_file),
        new_file=str(new_file),
        shapes_removed=[ShapeRemoved(sid) for sid in sorted(set(s1) - set(matches))],
        shapes_added=[ShapeAdded(sid) for sid in sorted(set(s2) - matched_new_ids)],
    )

    total = len(matches)
    _trace(f"Phase 2 — point diffing: {total} shape pairs ...")
    for i, base_id in enumerate(sorted(matches), 1):
        new_id, _ = matches[base_id]
        result.shape_id_mapping[base_id] = new_id
        changes = _diff_shape_points(
            s1[base_id], s2[new_id], coord_tolerance_m, match_tolerance_m
        )
        sd = ShapeDiff(base_shape_id=base_id, new_shape_id=new_id, changes=changes)
        if sd.is_modified:
            result.shapes_modified.append(sd)
        if i % max(1, total // 10) == 0 or i == total:
            _trace(f"  {i}/{total} shapes diffed")

    _trace(
        f"Done — {len(result.shapes_modified)} modified, "
        f"{result.shapes_unchanged_count} unchanged, "
        f"{len(result.shapes_added)} added, "
        f"{len(result.shapes_removed)} removed"
    )
    return result


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <base_shapes.txt> <new_shapes.txt>", file=sys.stderr)
        sys.exit(1)

    result = content_addressed_polyline_diff(sys.argv[1], sys.argv[2])
    print(result.format())


if __name__ == "__main__":
    main()
