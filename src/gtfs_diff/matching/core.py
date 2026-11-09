"""Matching results, settings, and what every entity kind shares."""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..geometry import METERS_PER_DEGREE
from ..gtfs_definitions import get_primary_key
from ..tracing import _trace

STOP_RADIUS_M = 5.0
# Longitude cells are widened so neighbouring cells cover the radius up to ~72°.
MIN_COS_LAT = 0.3
SHAPE_NEAR_M = 30.0
SHAPE_CELL_M = 250.0
SHAPE_SAMPLES = 32
# Trips left unpaired may still pair when their times moved by up to this many
# minutes, tried in steps of SHIFT_STEP_MIN. See docs/id_matching.md.
DEFAULT_TRIP_MAX_SHIFT_MIN = 15.0
SHIFT_STEP_MIN = 5.0
# Trips still unpaired may pair when they share at least this share of their
# stops (counted on the longer trip). 1 turns that off.
DEFAULT_TRIP_MIN_SHARED_STOPS = 0.9
# No other unpaired trip of the same pattern may depart this close to a shifted
# pair (at least the largest shift).
SHIFT_ISOLATION_MIN = 60.0
# Marks a new ID that is unmatched but also used by an old entity, so the two
# are never compared as one row.
UNMATCHED_PREFIX = "\x01"

# Columns read for matching, per file.
MATCH_COLUMNS: dict[str, list[str]] = {
    "agency.txt": ["agency_id", "agency_name"],
    "routes.txt": [
        "route_id",
        "agency_id",
        "route_short_name",
        "route_long_name",
        "route_type",
    ],
    "trips.txt": ["trip_id", "route_id", "service_id", "direction_id", "shape_id"],
    "stop_times.txt": [
        "trip_id",
        "stop_id",
        "stop_sequence",
        "arrival_time",
        "departure_time",
    ],
    "stops.txt": ["stop_id", "stop_name", "stop_lat", "stop_lon", "location_type"],
    "calendar.txt": [
        "service_id",
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
        "start_date",
        "end_date",
    ],
    "calendar_dates.txt": ["service_id", "date", "exception_type"],
    "feed_info.txt": ["feed_start_date", "feed_end_date"],
    "shapes.txt": ["shape_id", "shape_pt_lat", "shape_pt_lon", "shape_pt_sequence"],
}

# Entity kind -> file that defines it.
ENTITY_FILES: dict[str, str] = {
    "agency": "agency.txt",
    "stop": "stops.txt",
    "route": "routes.txt",
    "service": "calendar.txt",
    "trip": "trips.txt",
    "shape": "shapes.txt",
}

# File -> {column: entity kind} for every column holding a matched ID.
ID_COLUMNS: dict[str, dict[str, str]] = {
    "agency.txt": {"agency_id": "agency"},
    "routes.txt": {"route_id": "route", "agency_id": "agency"},
    "fare_attributes.txt": {"agency_id": "agency"},
    "attributions.txt": {"agency_id": "agency", "route_id": "route", "trip_id": "trip"},
    "stops.txt": {"stop_id": "stop", "parent_station": "stop"},
    "stop_times.txt": {"trip_id": "trip", "stop_id": "stop"},
    "trips.txt": {
        "trip_id": "trip",
        "route_id": "route",
        "service_id": "service",
        "shape_id": "shape",
    },
    "calendar.txt": {"service_id": "service"},
    "calendar_dates.txt": {"service_id": "service"},
    "timeframes.txt": {"service_id": "service"},
    "frequencies.txt": {"trip_id": "trip"},
    "transfers.txt": {
        "from_stop_id": "stop",
        "to_stop_id": "stop",
        "from_route_id": "route",
        "to_route_id": "route",
        "from_trip_id": "trip",
        "to_trip_id": "trip",
    },
    "pathways.txt": {"from_stop_id": "stop", "to_stop_id": "stop"},
    "stop_areas.txt": {"stop_id": "stop"},
    "location_group_stops.txt": {"stop_id": "stop"},
    "fare_rules.txt": {"route_id": "route"},
    "route_networks.txt": {"route_id": "route"},
    "shapes.txt": {"shape_id": "shape"},
}

# stop_times.txt rows of a matched trip pair by stop position, so a trip whose
# stop_sequence numbering changed is still compared stop by stop.
SEQUENCE_FILE = "stop_times.txt"
SEQUENCE_COLUMN = "stop_sequence"


@dataclass(frozen=True)
class FileTranslation:
    """How to read the new feed's ID columns of one file.

    ``keys`` maps a column to ``{published new ID: comparison key}``, holding only
    IDs whose key differs from the published value. ``parquet`` holds the same
    maps as files for the DuckDB backend.
    """

    keys: dict[str, dict[str, str]]
    parquet: dict[str, str]
    # stop_times.txt: {(published trip_id, stop_sequence): old stop_sequence} for
    # matched trips numbered differently, so their stops pair by position.
    sequence: dict[tuple[str, str], str] | None = None
    sequence_parquet: str | None = None
    # Key columns compared as published, so a new ID is reported as a change
    # (include_key_changes); empty by default.
    key_columns: frozenset[str] = frozenset()

    def key(self, column: str, value: str) -> str:
        mapping = self.keys.get(column)
        return mapping.get(value, value) if mapping else value

    def translates(self, column: str) -> bool:
        return column in self.keys or (
            column == SEQUENCE_COLUMN and bool(self.sequence)
        )

    def row_key(self, column: str, row: dict[str, str]) -> str:
        """The comparison value of *column* in a new-feed *row*."""
        value = row.get(column, "")
        if column == SEQUENCE_COLUMN and self.sequence:
            return self.sequence.get((row.get("trip_id", ""), value), value)
        return self.key(column, value)


@dataclass
class ShapeChange:
    """One shape that changed between the feeds."""

    change: str  # renamed | redrawn | changed | added | deleted
    base_shape_id: str | None
    new_shape_id: str | None
    deviation_m: float | None = None
    base_length_m: float | None = None
    new_length_m: float | None = None
    route: str | None = None
    # [lon, lat, shape_pt_sequence] per point, in sequence order.
    base_coords: list[list[float]] | None = None
    new_coords: list[list[float]] | None = None
    base_points: int | None = None
    new_points: int | None = None


@dataclass
class MatchResult:
    """ID maps per entity kind plus a summary of how each kind was matched."""

    keys: dict[str, dict[str, str]] = field(default_factory=dict)
    parquet: dict[str, str] = field(default_factory=dict)
    sequence: dict[tuple[str, str], str] = field(default_factory=dict)
    sequence_parquet: str | None = None
    summary: dict = field(default_factory=dict)
    shape_changes: list[ShapeChange] | None = None
    # Notable highlights (see gtfs_diff.highlights), when asked for.
    highlights: list[dict] | None = None
    workdir: str | None = None

    def translation_for(
        self, file_name: str, include_key_changes: bool = False
    ) -> FileTranslation | None:
        columns = {
            col: kind
            for col, kind in ID_COLUMNS.get(file_name, {}).items()
            if kind in self.keys
        }
        if not columns:
            return None
        sequence = file_name == SEQUENCE_FILE and bool(self.sequence)
        return FileTranslation(
            keys={col: self.keys[kind] for col, kind in columns.items()},
            parquet={col: self.parquet[kind] for col, kind in columns.items()},
            sequence=self.sequence if sequence else None,
            sequence_parquet=self.sequence_parquet if sequence else None,
            key_columns=frozenset(
                get_primary_key(file_name) or [] if include_key_changes else []
            ),
        )

    def close(self) -> None:
        if self.workdir:
            shutil.rmtree(self.workdir, ignore_errors=True)
            self.workdir = None


def _meters(lat1: str, lon1: str, lat2: str, lon2: str) -> str:
    return (
        f"sqrt(power(({lat2} - {lat1}) * {METERS_PER_DEGREE}, 2) + "
        f"power(({lon2} - {lon1}) * {METERS_PER_DEGREE} * "
        f"cos(radians(({lat1} + {lat2}) / 2)), 2))"
    )


def _mutual_best(source: str, score: str, better: str = "DESC") -> str:
    """Pairs (old_id, new_id) that are each other's unique best by *score*."""
    return f"""
        WITH c AS ({source}),
        o AS (SELECT old_id, new_id,
                     rank() OVER (PARTITION BY old_id ORDER BY {score} {better}) AS r,
                     count(*) OVER (PARTITION BY old_id, {score}) AS ties
              FROM c),
        n AS (SELECT old_id, new_id,
                     rank() OVER (PARTITION BY new_id ORDER BY {score} {better}) AS r,
                     count(*) OVER (PARTITION BY new_id, {score}) AS ties
              FROM c)
        SELECT o.old_id, o.new_id FROM o JOIN n USING (old_id, new_id)
        WHERE o.r = 1 AND n.r = 1 AND o.ties = 1 AND n.ties = 1
    """


def _unique_on_both_sides(old_sql: str, new_sql: str, how: str) -> str:
    """Pairs whose (id, key) key occurs exactly once on each side."""
    return f"""
        WITH o AS ({old_sql}), n AS ({new_sql}),
             single AS (
                 SELECT key FROM o GROUP BY key HAVING count(*) = 1
                 INTERSECT
                 SELECT key FROM n GROUP BY key HAVING count(*) = 1)
        SELECT o.id, n.id, '{how}'
        FROM o JOIN n USING (key) WHERE key IN (SELECT key FROM single)
    """


class MatcherBase:
    """Loading and the pairing steps every entity kind shares."""

    def __init__(
        self,
        con,
        result: MatchResult,
        base: dict,
        new: dict,
        max_shift_min: float,
        min_shared_stops: float = DEFAULT_TRIP_MIN_SHARED_STOPS,
    ) -> None:
        self.con = con
        self.result = result
        self.paths = {"old": base, "new": new}
        self.max_shift_min = max_shift_min
        self.min_shared_stops = min_shared_stops
        self.shifts: dict | None = None
        self.stop_changes: dict | None = None

    def q(self, sql: str, params: list | None = None):
        return self.con.execute(sql, params or []).fetchone()[0]

    def has(self, *names: str) -> bool:
        return all(n in self.paths["old"] and n in self.paths["new"] for n in names)

    def load(self, side: str, name: str, columns: list[str]) -> None:
        table = f"{side}_{name.removesuffix('.txt')}"
        path = self.paths[side].get(name)
        if path is None:
            nulls = ", ".join(f"NULL::VARCHAR AS {c}" for c in columns)
            self.con.execute(f"CREATE TABLE {table} AS SELECT {nulls} WHERE false")
            return
        source = (
            "read_csv(?, all_varchar=true, header=true, null_padding=true, "
            "delim=',', quote='\"', escape='\"', sample_size=-1)"
        )
        described = self.con.execute(f"DESCRIBE SELECT * FROM {source}", [path])
        present = {r[0].lstrip("﻿").strip(): r[0] for r in described.fetchall()}
        select = ", ".join(
            "nullif(trim(\"{}\"), '') AS {}".format(present[c].replace('"', '""'), c)
            if c in present
            else f"NULL::VARCHAR AS {c}"
            for c in columns
        )
        self.con.execute(
            f"CREATE TABLE {table} AS SELECT {select} FROM {source}", [path]
        )

    def timed(self, kind: str, step) -> None:
        start = time.monotonic()
        step()
        self.finish(kind, time.monotonic() - start)
        _trace(
            f"  [matching] {kind}: {self.result.summary[kind]['matched']:,} matched "
            f"in {time.monotonic() - start:.1f}s"
        )

    # -- shared steps -------------------------------------------------------

    def ids(self, kind: str, table: str, column: str, where: str = "") -> None:
        """Tables old_ids_<kind> and new_ids_<kind> with every ID of the kind."""
        for side in ("old", "new"):
            self.con.execute(
                f"CREATE TABLE {side}_ids_{kind} AS "
                f"SELECT DISTINCT coalesce({column}, '') AS id "
                f"FROM {side}_{table} {where}"
            )
        self.con.execute(
            f"CREATE TABLE pairs_{kind} (old_id VARCHAR, new_id VARCHAR, how VARCHAR)"
        )

    def add_pairs(self, kind: str, select: str) -> None:
        """Insert pairs from *select* (old_id, new_id, how) among unpaired IDs."""
        self.con.execute(
            f"""
            INSERT INTO pairs_{kind}
            SELECT * FROM ({select}) s(old_id, new_id, how)
            WHERE old_id IN (SELECT id FROM old_ids_{kind})
              AND new_id IN (SELECT id FROM new_ids_{kind})
              AND old_id NOT IN (SELECT old_id FROM pairs_{kind})
              AND new_id NOT IN (SELECT new_id FROM pairs_{kind})
            """
        )

    def left(self, kind: str, side: str) -> str:
        col = "old_id" if side == "old" else "new_id"
        return f"(SELECT {col} FROM pairs_{kind})"

    def pair_same_id(self, kind: str) -> None:
        self.add_pairs(
            kind,
            f"""SELECT o.id, n.id, 'same id' FROM old_ids_{kind} o
                JOIN new_ids_{kind} n USING (id)
                WHERE o.id NOT IN {self.left(kind, "old")}
                  AND n.id NOT IN {self.left(kind, "new")}""",
        )

    def pair_by_votes(self, kind: str, column: str) -> None:
        """Mutual best pairs by the number of matched trips that link them."""
        votes = f"""
            SELECT o.{column} AS old_id, n.{column} AS new_id, count(*) AS votes
            FROM pairs_trip p
            JOIN old_trips o ON o.trip_id = p.old_id
            JOIN new_trips n ON n.trip_id = p.new_id
            WHERE o.{column} IS NOT NULL AND n.{column} IS NOT NULL
            GROUP BY ALL
        """
        best = _mutual_best(votes, "votes")
        self.add_pairs(kind, f"SELECT old_id, new_id, 'trip votes' FROM ({best})")

    def finish(self, kind: str, seconds: float) -> None:
        """Export the kind's key map and record its summary."""
        path = str(Path(self.result.workdir) / f"{kind}.parquet")
        self.con.execute(
            f"""
            CREATE TABLE keys_{kind} AS
            SELECT n.id AS new, CASE
                WHEN p.old_id IS NOT NULL THEN p.old_id
                WHEN n.id IN (SELECT id FROM old_ids_{kind})
                    THEN '{UNMATCHED_PREFIX}' || n.id
                END AS key
            FROM new_ids_{kind} n LEFT JOIN pairs_{kind} p ON p.new_id = n.id
            """
        )
        self.con.execute(f"DELETE FROM keys_{kind} WHERE key IS NULL OR key = new")
        self.con.execute(f"COPY keys_{kind} TO '{path}' (FORMAT parquet)")
        self.result.keys[kind] = dict(
            self.con.execute(f"SELECT new, key FROM keys_{kind}").fetchall()
        )
        self.result.parquet[kind] = path
        q = self.q
        matched = q(f"SELECT count(*) FROM pairs_{kind}")
        renamed = q(f"SELECT count(*) FROM pairs_{kind} WHERE old_id <> new_id")
        base_ids = q(f"SELECT count(*) FROM old_ids_{kind}")
        new_ids = q(f"SELECT count(*) FROM new_ids_{kind}")
        by_step = dict(
            self.con.execute(
                f"SELECT how, count(*) FROM pairs_{kind} GROUP BY how ORDER BY how"
            ).fetchall()
        )
        self.result.summary[kind] = {
            "file": ENTITY_FILES[kind],
            "base_ids": base_ids,
            "new_ids": new_ids,
            "matched": matched,
            "matched_same_id": matched - renamed,
            "matched_new_id": renamed,
            "unmatched_base": base_ids - matched,
            "unmatched_new": new_ids - matched,
            "by_step": by_step,
            "duration_seconds": round(seconds, 2),
        }
