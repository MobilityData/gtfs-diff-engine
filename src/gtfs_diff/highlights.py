"""Notable highlights: changes the people using a diff should check first.

Two feeds with no day of service in common, a service day that lost all its
trips, a stop removed or added without a replacement, stops served in another
order, a stop that moved far, a shape that moved far. They are found while
matching runs, from the tables it loaded, and written to the diff's
``highlights`` section as the GTFS Diff v2 schema defines it. See
docs/highlights.md.
"""

from __future__ import annotations

from bisect import bisect_left
from datetime import date

from .geometry import METERS_PER_DEGREE, coord_sql
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


DEFAULT_FARE_CHANGED_PCT = 50.0

FARE_COLUMNS = {
    "fare_attributes.txt": ["fare_id", "price", "currency_type"],
    "fare_rules.txt": ["fare_id", "route_id"],
    "fare_leg_rules.txt": ["fare_product_id", "network_id"],
    "route_networks.txt": ["route_id", "network_id"],
    "fare_products.txt": [
        "fare_product_id",
        "fare_product_name",
        "rider_category_id",
        "fare_media_id",
        "amount",
        "currency",
    ],
}


# GTFS features, as the GTFS Validator computes them (FeedMetadata.java) and
# gtfs.org lists their files and fields: fares and pathways. Columns read
# per file; tables are loaded with a "f_" prefix.
FEATURE_COLUMNS = {
    "agency.txt": ["agency_id", "cemv_support"],
    "routes.txt": ["route_id", "network_id", "cemv_support"],
    "networks.txt": ["network_id"],
    "route_networks.txt": ["route_id", "network_id"],
    "stops.txt": ["stop_id", "level_id"],
    "stop_areas.txt": ["area_id", "stop_id"],
    "areas.txt": ["area_id"],
    "timeframes.txt": ["timeframe_group_id"],
    "rider_categories.txt": ["rider_category_id"],
    "fare_media.txt": ["fare_media_id"],
    "fare_attributes.txt": ["fare_id"],
    "fare_products.txt": ["fare_product_id", "rider_category_id", "fare_media_id"],
    "fare_leg_rules.txt": [
        "network_id",
        "from_area_id",
        "to_area_id",
        "from_timeframe_group_id",
        "to_timeframe_group_id",
    ],
    "fare_transfer_rules.txt": ["from_leg_group_id"],
    "levels.txt": ["level_id"],
    "pathways.txt": [
        "pathway_id",
        "signposted_as",
        "reversed_signposted_as",
        "max_slope",
        "min_width",
        "length",
        "stair_count",
        "traversal_time",
    ],
}


def find_highlights(
    matcher,
    as_of: date,
    stop_moved_m: float,
    shape_moved_m: float,
    fare_changed_pct: float = DEFAULT_FARE_CHANGED_PCT,
) -> list[dict]:
    """Highlights from a matcher's tables, most important kind first."""
    con = matcher.con
    tables = {
        r[0] for r in con.execute("SELECT table_name FROM duckdb_tables()").fetchall()
    }
    out: list[dict] = []
    checks = []
    if matcher.has("trips.txt") and matcher.has_calendar():
        if "old_dates" not in tables:
            matcher.build_service_days()
        out += _service_periods_do_not_overlap(con)
        out += _service_days_without_trips(con, as_of)
        checks.append("service")
    if "pairs_stop" in tables:
        out += _removed_stops(con)
        out += _added_stops(con)
        checks.append("stops")
    if "stop_change_pairs" in tables:
        out += _stop_order_changes(con)
        checks.append("stop_order")
    if "pairs_stop" in tables:
        out += _moved_stops(con, stop_moved_m)
    if "shape_gap" in tables:
        out += _moved_shapes(con, shape_moved_m)
        checks.append("shapes")
    if matcher.has("fare_attributes.txt") or matcher.has("fare_products.txt"):
        checks.append("fares")
    out += _changed_fares(matcher, tables, fare_changed_pct)
    out += _changed_features(matcher)
    matcher.result.highlight_checks = checks
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
_POINTS = (
    f"SELECT stop_id, stop_name, {coord_sql('stop_lat', 90)} AS lat, "
    f"{coord_sql('stop_lon', 180)} AS lon FROM {{side}}_stops"
)


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


_ROUTE_NAMES = """SELECT route_id, any_value(coalesce(route_short_name,
                         route_long_name, route_id)) AS route
                  FROM new_routes GROUP BY route_id"""


def _added_stops(con) -> list[dict]:
    """New boarding stops with no match and no removed stop nearby, with the
    routes that serve them."""
    lat_r = REPLACEMENT_M / METERS_PER_DEGREE
    rows = con.execute(
        f"""
        WITH fresh AS (
            SELECT * FROM ({_POINTS.format(side="new")} WHERE {_BOARDING})
            WHERE stop_id IS NOT NULL
              AND stop_id NOT IN (SELECT new_id FROM pairs_stop)
        ),
        gone AS (
            SELECT * FROM ({_POINTS.format(side="old")} WHERE {_BOARDING})
            WHERE stop_id NOT IN (SELECT old_id FROM pairs_stop) AND lat IS NOT NULL
        ),
        near AS (
            SELECT f.stop_id, min({_metres("f.lat", "f.lon", "g.lat", "g.lon")}) AS m
            FROM fresh f JOIN gone g
              ON abs(g.lat - f.lat) <= {lat_r}
             AND abs(g.lon - f.lon) <= {lat_r} / greatest(cos(radians(f.lat)), 0.3)
            GROUP BY f.stop_id
        ),
        served AS (
            SELECT s.stop_id, coalesce(r.route, t.route_id) AS route,
                   count(DISTINCT s.trip_id) AS trips
            FROM new_stop_times s
            JOIN new_trips t USING (trip_id)
            LEFT JOIN ({_ROUTE_NAMES}) r USING (route_id)
            WHERE s.stop_id IN (SELECT stop_id FROM fresh)
            GROUP BY ALL
        ),
        per_stop AS (
            SELECT stop_id, sum(trips) AS trips,
                   list(route ORDER BY trips DESC, route) AS routes
            FROM served GROUP BY stop_id
        )
        SELECT f.stop_id, f.stop_name, f.lat, f.lon, coalesce(p.trips, 0)::BIGINT,
               coalesce(p.routes, [])
        FROM fresh f
        LEFT JOIN near n USING (stop_id)
        LEFT JOIN per_stop p USING (stop_id)
        WHERE n.m IS NULL OR n.m > {REPLACEMENT_M}
        ORDER BY coalesce(p.trips, 0) DESC, f.stop_id
        """
    ).fetchall()
    out = []
    for sid, name, lat, lon, trips, routes in rows:
        served = (
            f"Served by {_routes(routes)} ({trips:,} trip{'s' if trips != 1 else ''})."
            if routes
            else "No trip serves it."
        )
        out.append(
            {
                "code": "stop_added",
                "message": (
                    f"Stop {_name(name, sid)} was added, with no removed stop within "
                    f"{REPLACEMENT_M:g} m that it replaces. {served}"
                ),
                "files": ["stops.txt"],
                "references": [_ref("stops.txt", "new", stop_id=sid)],
                "details": {
                    "stop_name": name,
                    "lat": lat,
                    "lon": lon,
                    "new_trips": trips,
                    "routes": routes,
                },
            }
        )
    return out


def _stop_order_changes(con) -> list[dict]:
    """Paired trips whose shared stops come in another order, one highlight per
    route and set of stops that changed place. Stops a trip visits more than
    once are left out, and only trips paired by most stops shared can differ."""
    rows = con.execute(
        f"""
        WITH p AS (SELECT old_id, new_id FROM stop_change_pairs),
        o AS (
            SELECT s.trip_id, coalesce(m.new_stop, 'old:' || s.stop_id) AS stop,
                   s.stop_sequence, try_cast(s.stop_sequence AS DOUBLE) AS seq
            FROM old_stop_times s LEFT JOIN stop_sig_map m ON m.old_stop = s.stop_id
            WHERE s.trip_id IN (SELECT old_id FROM p)
            QUALIFY count(*) OVER (PARTITION BY s.trip_id, stop) = 1
        ),
        n AS (
            SELECT trip_id, stop_id AS stop, stop_sequence,
                   try_cast(stop_sequence AS DOUBLE) AS seq
            FROM new_stop_times WHERE trip_id IN (SELECT new_id FROM p)
            QUALIFY count(*) OVER (PARTITION BY trip_id, stop_id) = 1
        ),
        orders AS (
            SELECT p.old_id, p.new_id,
                   list(n.stop ORDER BY o.seq) AS base_order,
                   list(n.stop ORDER BY n.seq) AS new_order,
                   list(o.stop_sequence ORDER BY n.seq) AS base_seqs,
                   list(n.stop_sequence ORDER BY n.seq) AS new_seqs
            FROM p JOIN o ON o.trip_id = p.old_id
            JOIN n ON n.trip_id = p.new_id AND n.stop = o.stop
            GROUP BY p.old_id, p.new_id
            HAVING list(o.seq ORDER BY n.seq) <> list_sort(list(o.seq ORDER BY n.seq))
        )
        SELECT o.*, t.route_id, coalesce(r.route, t.route_id)
        FROM orders o
        JOIN (SELECT trip_id, any_value(route_id) AS route_id FROM new_trips
              GROUP BY trip_id) t ON t.trip_id = o.new_id
        LEFT JOIN ({_ROUTE_NAMES}) r USING (route_id)
        ORDER BY o.new_id
        """
    ).fetchall()
    moves: dict[tuple, tuple] = {}
    groups: dict[tuple, dict] = {}
    for old_id, new_id, base, new, base_seqs, new_seqs, route_id, route in rows:
        key = (tuple(base), tuple(new))
        if key not in moves:
            moves[key] = _moved_in_order(base, new)
        moved = moves[key]
        group = groups.setdefault(
            (route_id, moved),
            {"route": route, "trips": 0, "example": None},
        )
        group["trips"] += 1
        if group["example"] is None:
            at = new.index(moved[0][0])
            group["example"] = (old_id, base_seqs[at], new_id, new_seqs[at], base, new)
    names = _stop_names(con, {s for g in groups.values() for s in g["example"][4]})
    out = []
    for (route_id, moved), g in sorted(groups.items(), key=lambda x: -x[1]["trips"]):
        trips = f"{g['trips']:,} trip{'s' if g['trips'] != 1 else ''}"
        if len(moved) == 1:
            stop, base_after, new_after = moved[0]
            what = (
                f"stop {_name(names.get(stop), stop)} now comes "
                f"{_after(new_after, names)} instead of {_after(base_after, names)}"
            )
        else:
            listed = [_name(names.get(s), s) for s, _, _ in moved[:3]]
            more = f" and {len(moved) - 3} more" if len(moved) > 3 else ""
            what = f"{len(moved)} stops changed place: {', '.join(listed)}{more}"
        old_id, base_seq, new_id, new_seq, base, new = g["example"]
        file = "stop_times.txt"
        out.append(
            {
                "code": "stop_order_changed",
                "message": f"On route {g['route']}, {what}, on {trips}.",
                "files": [file],
                "references": [
                    _ref(file, "base", trip_id=old_id, stop_sequence=base_seq),
                    _ref(file, "new", trip_id=new_id, stop_sequence=new_seq),
                ],
                "details": {
                    "route": g["route"],
                    "route_id": route_id,
                    "trips": g["trips"],
                    "stops": [
                        {"stop_id": s, "base_after": b, "new_after": n}
                        for s, b, n in moved
                    ],
                    "base_order": list(base),
                    "new_order": list(new),
                    "stop_names": {s: names.get(s) for s in base},
                },
            }
        )
    return out


def _moved_in_order(base: list[str], new: list[str]) -> tuple[tuple, ...]:
    """The stops of *new* outside a longest run kept in *base* order, each with
    the stop before it in *base* and in *new* (None at the start)."""
    rank = {s: i for i, s in enumerate(base)}
    tails: list[int] = []
    tail_at: list[int] = []
    prev = [-1] * len(new)
    for i, s in enumerate(new):
        k = bisect_left(tails, rank[s])
        if k == len(tails):
            tails.append(rank[s])
            tail_at.append(i)
        else:
            tails[k] = rank[s]
            tail_at[k] = i
        prev[i] = tail_at[k - 1] if k else -1
    kept, i = set(), tail_at[-1]
    while i >= 0:
        kept.add(i)
        i = prev[i]
    return tuple(
        (s, base[rank[s] - 1] if rank[s] else None, new[i - 1] if i else None)
        for i, s in enumerate(new)
        if i not in kept
    )


def _stop_names(con, stop_ids: set) -> dict:
    return dict(
        con.execute(
            "SELECT stop_id, any_value(stop_name) FROM new_stops "
            "WHERE list_contains(?, stop_id) GROUP BY stop_id",
            [sorted(s for s in stop_ids if s)],
        ).fetchall()
    )


def _after(stop: str | None, names: dict) -> str:
    return f"after {_name(names.get(stop), stop)}" if stop else "first"


def _routes(routes: list[str]) -> str:
    shown = ", ".join(routes[:3])
    more = f" and {len(routes) - 3} more" if len(routes) > 3 else ""
    return f"route{'s' if len(routes) > 1 else ''} {shown}{more}"


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


def _features(con, side: str) -> dict[tuple[str, str], bool]:
    """The features (name, group) a feed uses, by the validator's definitions."""

    def has(file: str, *fields: str) -> bool:
        table = f"f_{side}_{file.removesuffix('.txt')}"
        where = " OR ".join(f"{f} IS NOT NULL" for f in fields) or "true"
        return con.execute(
            f"SELECT count(*) > 0 FROM {table} WHERE {where}"
        ).fetchone()[0]

    products = has("fare_products.txt")
    legs = has("fare_leg_rules.txt")
    return {
        ("Fares V1", "Fares"): has("fare_attributes.txt"),
        ("Fare Products", "Fares"): products,
        ("Fare Transfers", "Fares"): has("fare_transfer_rules.txt"),
        ("Fare Media", "Fares"): has("fare_media.txt")
        and has("fare_products.txt", "fare_media_id"),
        ("Rider Categories", "Fares"): has("rider_categories.txt")
        and has("fare_products.txt", "rider_category_id"),
        ("Time-Based Fares", "Fares"): has("timeframes.txt")
        and products
        and legs
        and has(
            "fare_leg_rules.txt", "from_timeframe_group_id", "to_timeframe_group_id"
        ),
        ("Zone-Based Fares", "Fares"): has("areas.txt")
        and has("stop_areas.txt")
        and products
        and legs
        and has("fare_leg_rules.txt", "from_area_id", "to_area_id"),
        ("Route-Based Fares", "Fares"): (
            has("routes.txt", "network_id")
            or has("networks.txt")
            or has("route_networks.txt")
        )
        and products
        and legs
        and has("fare_leg_rules.txt", "network_id"),
        ("Contactless EMV Support", "Fares"): has("agency.txt", "cemv_support")
        or has("routes.txt", "cemv_support"),
        ("Pathway Connections", "Pathways"): has("pathways.txt"),
        ("Pathway Signs", "Pathways"): has(
            "pathways.txt", "signposted_as", "reversed_signposted_as"
        ),
        ("Pathway Details", "Pathways"): has(
            "pathways.txt", "max_slope", "min_width", "length", "stair_count"
        ),
        ("In-station Traversal Time", "Pathways"): has(
            "pathways.txt", "traversal_time"
        ),
        ("Levels", "Pathways"): has("levels.txt") and has("stops.txt", "level_id"),
    }


# Files and fields each feature lists on gtfs.org, the defining file first.
FEATURE_FIELDS: dict[str, dict[str, list[str]]] = {
    "Fares V1": {
        "fare_attributes.txt": [
            "fare_id",
            "price",
            "currency_type",
            "payment_method",
            "transfers",
            "agency_id",
            "transfer_duration",
        ],
        "fare_rules.txt": [
            "fare_id",
            "route_id",
            "origin_id",
            "destination_id",
            "contains_id",
        ],
        "stops.txt": ["zone_id"],
    },
    "Fare Products": {
        "fare_products.txt": [
            "fare_product_id",
            "fare_product_name",
            "amount",
            "currency",
            "fare_media_id",
        ],
        "fare_leg_rules.txt": ["fare_product_id"],
    },
    "Fare Transfers": {
        "fare_transfer_rules.txt": [
            "from_leg_group_id",
            "to_leg_group_id",
            "transfer_count",
            "duration_limit",
            "duration_limit_type",
            "fare_transfer_type",
            "fare_product_id",
        ],
        "fare_leg_rules.txt": ["leg_group_id"],
    },
    "Fare Media": {
        "fare_media.txt": ["fare_media_id", "fare_media_name", "fare_media_type"],
        "fare_products.txt": ["fare_media_id"],
    },
    "Rider Categories": {
        "rider_categories.txt": [
            "rider_category_id",
            "rider_category_name",
            "is_default_fare_category",
            "eligibility_url",
        ],
        "fare_products.txt": ["rider_category_id"],
    },
    "Time-Based Fares": {
        "timeframes.txt": [
            "timeframe_group_id",
            "start_time",
            "end_time",
            "service_id",
        ],
        "fare_leg_rules.txt": [
            "fare_product_id",
            "from_timeframe_group_id",
            "to_timeframe_group_id",
        ],
    },
    "Zone-Based Fares": {
        "areas.txt": ["area_id", "area_name"],
        "stop_areas.txt": ["area_id", "stop_id"],
        "fare_leg_rules.txt": ["fare_product_id", "from_area_id", "to_area_id"],
    },
    "Route-Based Fares": {
        "networks.txt": ["network_id", "network_name"],
        "route_networks.txt": ["network_id", "route_id"],
        "routes.txt": ["network_id"],
        "fare_leg_rules.txt": ["fare_product_id", "network_id"],
    },
    "Contactless EMV Support": {
        "agency.txt": ["cemv_support"],
        "routes.txt": ["cemv_support"],
    },
    "Pathway Connections": {
        "pathways.txt": [
            "pathway_id",
            "from_stop_id",
            "to_stop_id",
            "pathway_mode",
            "is_bidirectional",
        ]
    },
    "Pathway Signs": {"pathways.txt": ["signposted_as", "reversed_signposted_as"]},
    "Pathway Details": {
        "pathways.txt": ["max_slope", "min_width", "length", "stair_count"]
    },
    "In-station Traversal Time": {"pathways.txt": ["traversal_time"]},
    "Levels": {
        "levels.txt": ["level_id", "level_index", "level_name"],
        "stops.txt": ["level_id"],
    },
}


_DOCS = "https://gtfs.org/getting_started/features"
FEATURE_URLS = {
    "Fares V1": f"{_DOCS}/fares/#fares-v1",
    "Fare Products": f"{_DOCS}/fares/#fare-products",
    "Fare Transfers": f"{_DOCS}/fares/#fare-transfers",
    "Fare Media": f"{_DOCS}/fares/#fare-media",
    "Rider Categories": f"{_DOCS}/fares/#rider-categories",
    "Time-Based Fares": f"{_DOCS}/fares/#time-based-fares",
    "Zone-Based Fares": f"{_DOCS}/fares/#zone-based-fares",
    "Route-Based Fares": f"{_DOCS}/fares/#route-based-fares",
    "Contactless EMV Support": f"{_DOCS}/fares/#contactless-emv-support",
    "Pathway Connections": f"{_DOCS}/pathways/#pathway-connections",
    "Pathway Signs": f"{_DOCS}/pathways/#pathway-signs",
    "Pathway Details": f"{_DOCS}/pathways/#pathway-details",
    "In-station Traversal Time": f"{_DOCS}/pathways/#in-station-traversal-time",
    "Levels": f"{_DOCS}/pathways/#levels",
}


def _changed_features(matcher) -> list[dict]:
    """Features (fares, pathways) the new feed uses and the old one did not, then
    those the old feed used and the new one does not."""
    for name, columns in FEATURE_COLUMNS.items():
        for side in ("old", "new"):
            matcher.load(side, name, columns, prefix="f_")
    before = _features(matcher.con, "old")
    after = _features(matcher.con, "new")
    out = []
    for code, verb, was, now in (
        ("feature_added", "added", False, True),
        ("feature_removed", "removed", True, False),
    ):
        for (feature, group), used in after.items():
            if used is now and before[(feature, group)] is was:
                out.append(
                    {
                        "code": code,
                        "message": f"{feature} was {verb} ({group.lower()}).",
                        "files": list(FEATURE_FIELDS[feature]),
                        "details": {
                            "feature": feature,
                            "group": group,
                            "url": FEATURE_URLS[feature],
                            "fields": FEATURE_FIELDS[feature],
                        },
                    }
                )
    return out


def _fare_change(base: float, new: float, threshold_pct: float) -> float | None | bool:
    """The change in percent when it is at least *threshold_pct*, None when the
    fare was free (no percentage), False when it changed less."""
    if base < 0 or new < 0 or base == new:
        return False
    if base == 0:
        return None
    # Rounded so 1.10 -> 1.65 is 50%, not 49.99999999999998.
    pct = round((new - base) / base * 100, 6)
    return pct if abs(pct) >= threshold_pct else False


def _changed_fares(matcher, tables: set, threshold_pct: float) -> list[dict]:
    """Fares (same ID, same currency) whose price changed by at least
    *threshold_pct* percent, including fares that became or stopped being free."""
    con = matcher.con
    for name, columns in FARE_COLUMNS.items():
        if matcher.has(name):
            for side in ("old", "new"):
                if f"{side}_{name.removesuffix('.txt')}" not in tables:
                    matcher.load(side, name, columns)
    out: list[dict] = []
    if matcher.has("fare_attributes.txt"):
        routes = {}
        if matcher.has("fare_rules.txt"):
            for fare_id, route in con.execute(
                """
                SELECT r.fare_id, coalesce(t.route_short_name, t.route_long_name,
                                           r.route_id)
                FROM new_fare_rules r
                LEFT JOIN new_routes t USING (route_id)
                WHERE r.route_id IS NOT NULL ORDER BY 1, 2
                """
            ).fetchall():
                routes.setdefault(fare_id, []).append(route)
        rows = con.execute(
            """
            SELECT o.fare_id, try_cast(o.price AS DOUBLE),
                   try_cast(n.price AS DOUBLE), n.currency_type
            FROM old_fare_attributes o JOIN new_fare_attributes n USING (fare_id)
            WHERE o.currency_type IS NOT DISTINCT FROM n.currency_type
            """
        ).fetchall()
        for fare_id, base, new, currency in rows:
            if base is None or new is None:
                continue
            pct = _fare_change(base, new, threshold_pct)
            if pct is False:
                continue
            out.append(
                _fare_highlight(
                    "fare_attributes.txt",
                    {"fare_id": fare_id},
                    f"Fare {fare_id}",
                    base,
                    new,
                    currency,
                    pct,
                    {"fare_id": fare_id, "routes": routes.get(fare_id, [])},
                )
            )
    if matcher.has("fare_products.txt"):
        product_routes: dict = {}
        if matcher.has("fare_leg_rules.txt", "route_networks.txt"):
            for product, route in con.execute(
                """
                SELECT DISTINCT l.fare_product_id,
                       coalesce(t.route_short_name, t.route_long_name, rn.route_id)
                FROM new_fare_leg_rules l
                JOIN new_route_networks rn USING (network_id)
                LEFT JOIN new_routes t USING (route_id)
                ORDER BY 1, 2
                """
            ).fetchall():
                product_routes.setdefault(product, []).append(route)
        rows = con.execute(
            """
            SELECT o.fare_product_id, o.rider_category_id, o.fare_media_id,
                   coalesce(n.fare_product_name, o.fare_product_name),
                   try_cast(o.amount AS DOUBLE), try_cast(n.amount AS DOUBLE),
                   n.currency
            FROM old_fare_products o JOIN new_fare_products n
              ON o.fare_product_id = n.fare_product_id
             AND o.rider_category_id IS NOT DISTINCT FROM n.rider_category_id
             AND o.fare_media_id IS NOT DISTINCT FROM n.fare_media_id
            WHERE o.currency IS NOT DISTINCT FROM n.currency
            """
        ).fetchall()
        for product, rider, media, name, base, new, currency in rows:
            if base is None or new is None:
                continue
            pct = _fare_change(base, new, threshold_pct)
            if pct is False:
                continue
            # The full key, as the diff names the row: empty columns are "".
            key = {
                "fare_product_id": product,
                "rider_category_id": rider or "",
                "fare_media_id": media or "",
            }
            out.append(
                _fare_highlight(
                    "fare_products.txt",
                    key,
                    f"Fare product {_name(name, product)}",
                    base,
                    new,
                    currency,
                    pct,
                    {
                        "fare_product_id": product,
                        "fare_product_name": name,
                        "routes": product_routes.get(product, []),
                        "rider_category_id": rider,
                        "fare_media_id": media,
                    },
                )
            )
    return sorted(
        out,
        key=lambda h: (
            float("inf")
            if h["details"]["change_pct"] is None
            else abs(h["details"]["change_pct"])
        ),
        reverse=True,
    )


def _fare_highlight(file_name, key, label, base, new, currency, pct, extra) -> dict:
    unit = f" {currency}" if currency else ""
    if pct is None:
        message = f"{label} was free and now costs {new:g}{unit}."
    else:
        what = (
            "is now free"
            if new == 0
            else (f"{'rose' if pct > 0 else 'dropped'} {abs(pct):,.0f}%")
        )
        message = f"{label} {what} (from {base:g} to {new:g}{unit})."
    return {
        "code": "fare_changed",
        "message": message,
        "files": [file_name],
        "references": [
            _ref(file_name, "base", **key),
            _ref(file_name, "new", **key),
        ],
        "details": {
            **extra,
            "base_price": base,
            "new_price": new,
            "currency": currency,
            "change_pct": None if pct is None else round(pct, 1),
        },
    }


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
    """Primary keys of the rows the diff keeps whatever its cap, so every
    highlight in the diff can be found among the row changes: the rows its
    references name by their full primary key (base keys for deleted and
    modified rows, new keys for added rows)."""
    out: dict[str, set[tuple]] = {}
    for h in capped(highlights):
        for r in h.get("references") or []:
            pk = get_primary_key(r["file_name"])
            if pk and set(r["identifier"]) == set(pk):
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
    "FEATURE_COLUMNS",
    "DEFAULT_FARE_CHANGED_PCT",
    "DEFAULT_SHAPE_MOVED_M",
    "DEFAULT_STOP_MOVED_M",
    "find_highlights",
    "highlights_section",
]
