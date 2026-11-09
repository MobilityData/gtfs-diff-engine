"""Content-based ID matching between two feeds.

Pairs agencies, stops, routes, services, trips and shapes by what they are rather
than by their IDs, so a renamed or regenerated ID is diffed as the same entity.
Trips pair on equal stops and times under a series of signatures, kept only
when a relaxed signature is unique on both sides, then on times that moved up
to a threshold. Everything else is paired from the trip pairs or from its own
content, then by ID for whatever is left (trips excepted). See
docs/id_matching.md.

Each kind lives in its own module: entities.py (agencies, stops, routes,
services), trips.py and shapes.py, on top of core.py.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from ..feeds.csv_utils import _is_url
from ..highlights import find_highlights
from ..tracing import _trace
from .core import (
    DEFAULT_TRIP_MAX_SHIFT_MIN,
    DEFAULT_TRIP_MIN_SHARED_STOPS,
    ID_COLUMNS,
    MATCH_COLUMNS,
    FileTranslation,
    MatcherBase,
    MatchResult,
    ShapeChange,
)
from .entities import EntitiesMixin
from .shapes import ShapesMixin
from .trips import TripsMixin, shift_steps

__all__ = [
    "DEFAULT_TRIP_MAX_SHIFT_MIN",
    "DEFAULT_TRIP_MIN_SHARED_STOPS",
    "ID_COLUMNS",
    "MATCH_COLUMNS",
    "FileTranslation",
    "MatchResult",
    "ShapeChange",
    "match_feeds",
    "shift_steps",
]


class _Matcher(EntitiesMixin, TripsMixin, ShapesMixin, MatcherBase):
    def run(self, shape_geometry: bool) -> None:
        start = time.monotonic()
        for side in ("old", "new"):
            for name, columns in MATCH_COLUMNS.items():
                self.load(side, name, columns)
        _trace(f"  [matching] loaded in {time.monotonic() - start:.1f}s")

        if self.has("agency.txt"):
            self.timed("agency", self.match_agencies)
        if self.has("stops.txt"):
            self.timed("stop", self.match_stops)
        trips = self.has("trips.txt", "stop_times.txt")
        if trips:
            self.timed("trip", self.match_trips)
            if self.shifts is not None:
                self.result.summary["trip"]["time_shift"] = self.shifts
            if self.stop_changes is not None:
                self.result.summary["trip"]["shared_stops"] = self.stop_changes
            self.result.summary["trip"]["renumbered_stops"] = self.renumbered
        if self.has("routes.txt"):
            self.timed("route", lambda: self.match_routes(trips))
        if self.has("trips.txt") and (
            self.has("calendar.txt") or self.has("calendar_dates.txt")
        ):
            self.timed("service", lambda: self.match_services(trips))
        if self.has("shapes.txt"):
            self.timed("shape", lambda: self.match_shapes(trips, shape_geometry))
            self.result.summary["shape"]["changes"] = self.shape_counts


def match_feeds(
    base_paths: dict[str, str],
    new_paths: dict[str, str],
    shape_geometry: bool = False,
    spill_dir: str | None = None,
    trip_max_shift_min: float = DEFAULT_TRIP_MAX_SHIFT_MIN,
    highlights: dict | None = None,
    trip_min_shared_stops: float = DEFAULT_TRIP_MIN_SHARED_STOPS,
) -> MatchResult:
    """Match the entities of two feeds given ``{file name: path or URL}`` per side.

    Kinds whose files are missing on either side are left out, so the diff keys
    them by ID. Trips whose times moved by up to *trip_max_shift_min* minutes
    are paired too (0 turns that off), and so are trips sharing at least
    *trip_min_shared_stops* of their stops (1 turns that off). With
    *shape_geometry*, the result also lists every changed shape with its points.
    With *highlights* (the keyword arguments of
    :func:`gtfs_diff.highlights.find_highlights`), it also has the notable
    highlights.
    """
    import duckdb

    workdir = tempfile.mkdtemp(prefix="gtfs_match_", dir=spill_dir)
    result = MatchResult(workdir=workdir)
    con = duckdb.connect()
    try:
        con.execute("SET temp_directory=?", [str(Path(workdir) / "spill")])
        if any(_is_url(p) for p in [*base_paths.values(), *new_paths.values()]):
            con.execute("INSTALL httpfs")
            con.execute("LOAD httpfs")
        matcher = _Matcher(
            con,
            result,
            base_paths,
            new_paths,
            trip_max_shift_min,
            trip_min_shared_stops,
        )
        # Shapes moved this far are 'changed', matching the shape_moved highlight.
        if highlights is not None:
            matcher.shape_moved_m = highlights["shape_moved_m"]
        matcher.run(shape_geometry)
        if highlights is not None:
            start = time.monotonic()
            result.highlights = find_highlights(matcher, **highlights)
            _trace(
                f"  [matching] {len(result.highlights):,} notable highlights "
                f"in {time.monotonic() - start:.1f}s"
            )
    except Exception:
        result.close()
        raise
    finally:
        con.close()
    return result
