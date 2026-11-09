"""Notable highlights: changes the people using a diff should check first.

Two feeds with no day of service in common, a service day that lost all its
trips, a stop removed without a replacement, a stop that moved far, a shape that
moved far. They are found while matching runs, from the tables it loaded, and
written to the diff's ``highlights`` section as the GTFS Diff v2 schema defines
it. See docs/highlights.md.
"""

from __future__ import annotations

from datetime import date

from .geometry import METERS_PER_DEGREE
from .gtfs_definitions import get_primary_key
from .models import Highlights

# Highlights kept per code in the diff (and for the rows the diff always keeps);
# the counts per code stay complete.
CAP_PER_CODE = 100

DEFAULT_STOP_MOVED_M = 50.0
DEFAULT_SHAPE_MOVED_M = 50.0
# A removed stop is replaced when a newly added stop is this close.
REPLACEMENT_M = 100.0

SERVICE_FILES = ["calendar.txt", "calendar_dates.txt", "trips.txt"]


def find_highlights(
    matcher, as_of: date, stop_moved_m: float, shape_moved_m: float
) -> list[dict]:
    """Highlights from a matcher's tables, most important kind first."""
    con = matcher.con
    tables = {
        r[0] for r in con.execute("SELECT table_name FROM duckdb_tables()").fetchall()
    }
    out: list[dict] = []
    if matcher.has("trips.txt") and (
        matcher.has("calendar.txt") or matcher.has("calendar_dates.txt")
    ):
        if "old_dates" not in tables:
            matcher.build_service_days()
        out += _service_periods_do_not_overlap(con)
        out += _service_days_without_trips(con, as_of)
    if "pairs_stop" in tables:
        out += _removed_stops(con)
        out += _moved_stops(con, stop_moved_m)
    if "shape_gap" in tables:
        out += _moved_shapes(con, shape_moved_m)
    return out


def _service_periods_do_not_overlap(con) -> list[dict]:
    """The first and last day each feed has trips, when no day is in both."""
    spans = {}
    for side in ("old", "new"):
        spans[side] = con.execute(
            f"""SELECT min(d)::DATE, max(d)::DATE FROM {side}_dates
                WHERE service_id IN (SELECT service_id FROM {side}_trips)"""
        ).fetchone()
    (b0, b1), (n0, n1) = spans["old"], spans["new"]
    if None in (b0, b1, n0, n1) or (n0 <= b1 and b0 <= n1):
        return []
    later = n0 > b1
    gap = ((n0 - b1) if later else (b0 - n1)).days - 1
    between = (
        f"{gap:,} day{'s' if gap != 1 else ''} without service between them"
        if gap
        else "nothing in between"
    )
    order = "" if later else " The new feed ends before the old one starts."
    return [
        {
            "code": "service_periods_do_not_overlap",
            "message": (
                f"The feeds have no day of service in common: the old feed runs from "
                f"{_date(b0)} to {_date(b1)}, the new one from {_date(n0)} to "
                f"{_date(n1)}, with {between}.{order}"
            ),
            "files": ["calendar.txt", "calendar_dates.txt"],
            "details": {
                "base_first": b0.isoformat(),
                "base_last": b1.isoformat(),
                "new_first": n0.isoformat(),
                "new_last": n1.isoformat(),
                "gap_days": gap,
            },
        }
    ]


def _service_days_without_trips(con, as_of: date) -> list[dict]:
    """Dates from *as_of* on, within the new feed's service period, with trips in
    the old feed and none in the new one, grouped into runs of consecutive days.

    The new feed's period is its feed_info.txt feed_start_date to feed_end_date,
    each falling back to the first or last day its calendars run: a date the new
    feed does not cover has not lost its trips."""
    rows = con.execute(
        """
        WITH period AS (
            SELECT coalesce(
                       (SELECT min(try_strptime(feed_start_date, '%Y%m%d'))::DATE
                        FROM new_feed_info),
                       (SELECT min(d) FROM new_dates)) AS first_day,
                   coalesce(
                       (SELECT max(try_strptime(feed_end_date, '%Y%m%d'))::DATE
                        FROM new_feed_info),
                       (SELECT max(d) FROM new_dates)) AS last_day
        ),
        o AS (
            SELECT d, count(*) AS trips FROM old_dates
            JOIN old_trips USING (service_id), period
            WHERE d >= ? AND d BETWEEN period.first_day AND period.last_day
            GROUP BY d
        ),
        n AS (
            SELECT d, count(*) AS trips FROM new_dates
            JOIN new_trips USING (service_id) GROUP BY d
        )
        SELECT o.d, o.trips FROM o LEFT JOIN n USING (d)
        WHERE n.trips IS NULL ORDER BY o.d
        """,
        [as_of],
    ).fetchall()
    runs: list[list] = []
    for d, trips in rows:
        if runs and (d - runs[-1][-1][0]).days == 1:
            runs[-1].append((d, trips))
        else:
            runs.append([(d, trips)])
    out = []
    for run in runs:
        first, last = run[0][0], run[-1][0]
        low, high = min(t for _, t in run), max(t for _, t in run)
        trips = f"{high:,}" if low == high else f"{low:,} to {high:,}"
        when = (
            f"on {_day(first)}, {first.year}"
            if first == last
            else f"from {_day(first)} to {_day(last)}, {last.year} ({len(run)} days)"
        )
        per_day = "" if first == last else " a day"
        out.append(
            {
                "code": "service_day_without_trips",
                "message": f"No trips {when}; the old feed had {trips} trips{per_day}.",
                "files": SERVICE_FILES,
                "details": {
                    "date_from": first.isoformat(),
                    "date_to": last.isoformat(),
                    "days": len(run),
                    "base_trips_min": low,
                    "base_trips_max": high,
                },
            }
        )
    return out


def _metres(lat1: str, lon1: str, lat2: str, lon2: str) -> str:
    return (
        f"{METERS_PER_DEGREE} * sqrt(pow({lat2} - {lat1}, 2) "
        f"+ pow(({lon2} - {lon1}) * cos(radians({lat1})), 2))"
    )


_BOARDING = "coalesce(location_type, '0') = '0'"
_POINTS = """SELECT stop_id, stop_name, try_cast(stop_lat AS DOUBLE) AS lat,
                    try_cast(stop_lon AS DOUBLE) AS lon FROM {side}_stops"""


def _removed_stops(con) -> list[dict]:
    """Old boarding stops with no match and no newly added stop nearby."""
    lat_r = REPLACEMENT_M / METERS_PER_DEGREE
    rows = con.execute(
        f"""
        WITH gone AS (
            SELECT * FROM ({_POINTS.format(side="old")} WHERE {_BOARDING})
            WHERE stop_id IS NOT NULL
              AND stop_id NOT IN (SELECT old_id FROM pairs_stop)
        ),
        fresh AS (
            SELECT * FROM ({_POINTS.format(side="new")} WHERE {_BOARDING})
            WHERE stop_id NOT IN (SELECT new_id FROM pairs_stop) AND lat IS NOT NULL
        ),
        near AS (
            SELECT g.stop_id, min({_metres("g.lat", "g.lon", "f.lat", "f.lon")}) AS m
            FROM gone g JOIN fresh f
              ON abs(f.lat - g.lat) <= {lat_r}
             AND abs(f.lon - g.lon) <= {lat_r} / greatest(cos(radians(g.lat)), 0.3)
            GROUP BY g.stop_id
        ),
        served AS (
            SELECT stop_id, count(DISTINCT trip_id) AS trips FROM old_stop_times
            WHERE stop_id IN (SELECT stop_id FROM gone) GROUP BY stop_id
        )
        SELECT g.stop_id, g.stop_name, g.lat, g.lon, coalesce(s.trips, 0)
        FROM gone g
        LEFT JOIN near n USING (stop_id)
        LEFT JOIN served s USING (stop_id)
        WHERE n.m IS NULL OR n.m > {REPLACEMENT_M}
        ORDER BY coalesce(s.trips, 0) DESC, g.stop_id
        """
    ).fetchall()
    return [
        {
            "code": "stop_removed",
            "message": (
                f"Stop {_name(name, sid)} was removed, with no new stop within "
                f"{REPLACEMENT_M:g} m to replace it."
            ),
            "files": ["stops.txt"],
            "references": [_ref("stops.txt", "base", stop_id=sid)],
            "details": {"stop_name": name, "lat": lat, "lon": lon, "base_trips": trips},
        }
        for sid, name, lat, lon, trips in rows
    ]


def _moved_stops(con, threshold: float) -> list[dict]:
    """Matched stops at least *threshold* metres from where they were."""
    rows = con.execute(
        f"""
        SELECT p.old_id, p.new_id, coalesce(n.stop_name, o.stop_name),
               o.lat, o.lon, n.lat, n.lon,
               {_metres("o.lat", "o.lon", "n.lat", "n.lon")} AS m
        FROM pairs_stop p
        JOIN ({_POINTS.format(side="old")}) o ON o.stop_id = p.old_id
        JOIN ({_POINTS.format(side="new")}) n ON n.stop_id = p.new_id
        WHERE {_metres("o.lat", "o.lon", "n.lat", "n.lon")} >= ?
        ORDER BY m DESC
        """,
        [threshold],
    ).fetchall()
    out = []
    for old_id, new_id, name, olat, olon, nlat, nlon, m in rows:
        renamed = f"; it is now {new_id}" if new_id != old_id else ""
        out.append(
            {
                "code": "stop_moved",
                "message": f"Stop {_name(name, old_id)} moved {m:,.0f} m{renamed}.",
                "files": ["stops.txt"],
                "references": [
                    _ref("stops.txt", "base", stop_id=old_id),
                    _ref("stops.txt", "new", stop_id=new_id),
                ],
                "details": {
                    "stop_name": name,
                    "distance_m": round(m, 1),
                    "base": [olat, olon],
                    "new": [nlat, nlon],
                },
            }
        )
    return out


def _moved_shapes(con, threshold: float) -> list[dict]:
    """Matched shapes at least *threshold* metres apart somewhere (shape_gap)."""
    pairs = con.execute(
        "SELECT old_id, new_id, gap_m FROM shape_gap WHERE gap_m >= ?", [threshold]
    ).fetchall()
    if not pairs:
        return []
    routes = dict(
        con.execute(
            """
            SELECT shape_id, arg_max(route, n) FROM (
                SELECT t.shape_id,
                       coalesce(r.route_short_name, r.route_long_name, t.route_id)
                           AS route,
                       count(*) AS n
                FROM new_trips t LEFT JOIN new_routes r USING (route_id)
                WHERE t.shape_id IS NOT NULL GROUP BY ALL
            ) GROUP BY shape_id
            """
        ).fetchall()
    )
    out = []
    for old_id, new_id, gap in pairs:
        route = routes.get(new_id)
        on = f" (route {route})" if route else ""
        renamed = f"; it is now {new_id}" if new_id != old_id else ""
        out.append(
            {
                "code": "shape_moved",
                "message": f"Shape {old_id}{on} moved up to {gap:,.0f} m{renamed}.",
                "files": ["shapes.txt"],
                "references": [
                    _ref("shapes.txt", "base", shape_id=old_id),
                    _ref("shapes.txt", "new", shape_id=new_id),
                ],
                "details": {"route": route, "distance_m": round(gap, 1)},
            }
        )
    return sorted(out, key=lambda h: -h["details"]["distance_m"])


def highlights_section(
    highlights: list[dict], parameters: dict, cap: int | None = CAP_PER_CODE
) -> Highlights:
    """The diff's ``highlights`` section: the counts, and the first *cap* of each
    code."""
    return Highlights(
        cap_per_code=cap,
        parameters=parameters,
        counts=counts(highlights),
        items=capped(highlights, cap),
    )


def counts(highlights: list[dict]) -> dict[str, int]:
    """How many highlights of each code there are."""
    out: dict[str, int] = {}
    for h in highlights:
        out[h["code"]] = out.get(h["code"], 0) + 1
    return out


def capped(highlights: list[dict], cap: int | None = CAP_PER_CODE) -> list[dict]:
    """The first *cap* highlights of each code (they come most important first)."""
    if cap is None:
        return list(highlights)
    seen: dict[str, int] = {}
    out = []
    for h in highlights:
        seen[h["code"]] = seen.get(h["code"], 0) + 1
        if seen[h["code"]] <= cap:
            out.append(h)
    return out


def keep_rows(highlights: list[dict]) -> dict[str, set[tuple]]:
    """Primary keys of the base rows the diff keeps whatever its cap, so every
    highlight in the diff can be found among the row changes: the base rows its
    references name by their full primary key."""
    out: dict[str, set[tuple]] = {}
    for h in capped(highlights):
        for r in h.get("references") or []:
            pk = get_primary_key(r["file_name"])
            if r["feed"] == "base" and pk and set(r["identifier"]) == set(pk):
                out.setdefault(r["file_name"], set()).add(
                    tuple(r["identifier"][c] for c in pk)
                )
    return out


def _ref(file_name: str, feed: str, **identifier: str) -> dict:
    return {"file_name": file_name, "feed": feed, "identifier": identifier}


def _date(d: date) -> str:
    return f"{d:%b} {d.day}, {d.year}"


def _day(d: date) -> str:
    return f"{d:%a %b} {d.day}"


def _name(name: str | None, stop_id: str) -> str:
    return f"{stop_id} ({name})" if name else stop_id


__all__ = [
    "CAP_PER_CODE",
    "DEFAULT_SHAPE_MOVED_M",
    "DEFAULT_STOP_MOVED_M",
    "find_highlights",
    "highlights_section",
]
