"""Distances between points and lines, in metres."""

from __future__ import annotations

import math

METERS_PER_DEGREE = 111_320
GRID_M = 100.0
# Points this far or more from the other line are treated as this far.
MAX_GAP_M = 5_000.0


def line_gaps(coords, other) -> list[float]:
    """Metres from each point of *coords* to the nearest segment of *other*.

    Segments are indexed on a GRID_M grid, and each point searches rings of
    cells outwards until no nearer segment can be found (up to MAX_GAP_M).
    """
    k = math.cos(math.radians(coords[0][1])) * METERS_PER_DEGREE

    def xy(p):
        return p[0] * k, p[1] * METERS_PER_DEGREE

    segs = [(xy(a), xy(b)) for a, b in zip(other, other[1:], strict=False)]
    grid: dict[tuple[int, int], list[int]] = {}
    for i, ((ax, ay), (bx, by)) in enumerate(segs):
        for cx in range(int(min(ax, bx) // GRID_M), int(max(ax, bx) // GRID_M) + 1):
            for cy in range(int(min(ay, by) // GRID_M), int(max(ay, by) // GRID_M) + 1):
                grid.setdefault((cx, cy), []).append(i)

    def seg_dist(px, py, i):
        (ax, ay), (bx, by) = segs[i]
        dx, dy = bx - ax, by - ay
        length = dx * dx + dy * dy
        t = 0.0
        if length:
            t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length))
        return math.hypot(px - ax - t * dx, py - ay - t * dy)

    out = []
    rings = int(MAX_GAP_M // GRID_M)
    for p in coords:
        px, py = xy(p)
        cx, cy = int(px // GRID_M), int(py // GRID_M)
        best = MAX_GAP_M
        for r in range(rings + 1):
            if best <= (r - 1) * GRID_M:
                break
            for x in range(cx - r, cx + r + 1):
                for y in (
                    (cy - r, cy + r)
                    if 0 < abs(x - cx) < r
                    else range(cy - r, cy + r + 1)
                ):
                    if max(abs(x - cx), abs(y - cy)) != r:
                        continue
                    for i in grid.get((x, y), ()):
                        best = min(best, seg_dist(px, py, i))
        out.append(best)
    return out
