"""Which points of a changed shape the report draws, and how far each moved."""

from __future__ import annotations

from ..geometry import line_gaps

# Points kept per drawn shape.
HTML_SHAPE_POINTS = 300


def thin(coords: list[list[float]] | None, max_points: int) -> list[list[float]]:
    """Keep every k-th point (and the last) so a line has at most *max_points*."""
    if not coords:
        return []
    if len(coords) <= max_points:
        return coords
    step = -(-len(coords) // (max_points - 1))
    kept = coords[::step]
    if kept[-1] != coords[-1]:
        kept.append(coords[-1])
    return kept


MOVED_M = 1.0
# A stretch counts as the same in both shapes when both its ends are points of
# both shapes, within SAME_M of the other line, and its middle is within
# SAME_MID_M (which catches a detour between two points that did not move). Any
# stretch touching an edited point is therefore drawn as a change.
SAME_M = 2.0
SAME_MID_M = 5.0


def _drawn_points(coords, other) -> list[list]:
    """The points of one side of a shape change, as the report draws them.

    Each point is ``[lon, lat, sequence, only_here, gap_m, same_next]``:
    whether no point of the other shape sits at the same place, its distance
    to the other line, and whether the stretch to the next drawn point is the
    same in both shapes, all measured on the full shapes so the page only
    draws. Long shapes are cut to HTML_SHAPE_POINTS, keeping first the points
    that moved most (and those only in this shape), then points spread evenly
    along the line.
    """
    if not coords:
        return []
    if not other or len(other) < 2:
        return [[*p[:3], 0, None, 0] for p in thin(coords, HTML_SHAPE_POINTS)]
    there = {(round(p[0], 6), round(p[1], 6)) for p in other}
    gaps = line_gaps(coords, other)
    points = [
        [*p[:3], int((round(p[0], 6), round(p[1], 6)) not in there), round(g, 1)]
        for p, g in zip(coords, gaps, strict=True)
    ]
    if len(points) > HTML_SHAPE_POINTS:
        points = _keep_moved(points)
    mids = [
        [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2]
        for a, b in zip(points, points[1:], strict=False)
    ]
    mid_gaps = line_gaps(mids, other) if mids else []
    for i, p in enumerate(points):
        same = (
            i < len(mids)
            and not p[3]
            and not points[i + 1][3]
            and p[4] <= SAME_M
            and points[i + 1][4] <= SAME_M
            and mid_gaps[i] <= SAME_MID_M
        )
        p.append(int(same))
    return points


def pack(points: list[list]) -> list:
    """Drawn points as one flat list of deltas, about half the size.

    Per point: lon and lat (1e-6 degrees) and sequence as deltas, flags
    (1: only here, 2: same up to the next point), gap in dm (-1: none).
    """
    out: list = []
    px = py = 0
    ps: float = 0
    for p in points:
        x, y = round(p[0] * 1e6), round(p[1] * 1e6)
        s = p[2] if len(p) > 2 and p[2] is not None else 0
        ds = s - ps
        ds = int(ds) if float(ds).is_integer() else round(ds, 6)
        only = int(p[3]) if len(p) > 3 else 0
        same = int(p[5]) if len(p) > 5 else 0
        gap = -1 if len(p) < 5 or p[4] is None else round(p[4] * 10)
        out += [x - px, y - py, ds, only | 2 * same, gap]
        px, py, ps = x, y, s
    return out


def _keep_moved(points: list[list]) -> list[list]:
    """HTML_SHAPE_POINTS of *points*: the most moved first, then evenly spread."""
    moved = sorted(
        (i for i, p in enumerate(points) if p[3] or p[4] > MOVED_M),
        key=lambda i: -points[i][4],
    )
    keep = {0, len(points) - 1, *moved[: HTML_SHAPE_POINTS // 2]}
    room = HTML_SHAPE_POINTS - len(keep)
    if room > 0:
        step = len(points) / room
        keep.update(int(j * step) for j in range(room))
    return [points[i] for i in sorted(keep)]
