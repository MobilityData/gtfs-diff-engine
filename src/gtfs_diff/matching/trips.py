"""Matching trips: equal stops and times, then times that moved, then trips that
gained or lost a few stops, then which stop_times rows pair."""

from __future__ import annotations

from pathlib import Path

from ..tracing import _trace
from .core import (
    SHIFT_ISOLATION_MIN,
    SHIFT_STEP_MIN,
    UNMATCHED_PREFIX,
)


def _seconds(col: str) -> str:
    return (
        f"try_cast(split_part({col}, ':', 1) AS INTEGER) * 3600"
        f" + try_cast(split_part({col}, ':', 2) AS INTEGER) * 60"
        f" + try_cast(split_part({col}, ':', 3) AS INTEGER)"
    )


def shift_steps(max_shift_min: float) -> list[float]:
    """Shift limits in minutes, tried in turn: 5, 10, ... up to the maximum."""
    if max_shift_min <= 0:
        return []
    steps = []
    step = SHIFT_STEP_MIN
    while step < max_shift_min:
        steps.append(step)
        step += SHIFT_STEP_MIN
    return [*steps, max_shift_min]


class TripsMixin:
    # -- trips ---------------------------------------------------------------

    def match_trips(self) -> None:
        self.ids("trip", "trips", "trip_id", "WHERE trip_id IS NOT NULL")
        if not self.has("stops.txt"):
            self.con.execute(
                "CREATE TABLE stop_sig_map (old_stop VARCHAR, new_stop VARCHAR)"
            )
        for side in ("old", "new"):
            self.build_trip_info(side)
        self.build_service_days()
        # Exact service dates, then the weekly pattern; then what is left without
        # route names, then without service days, keeping only signatures that
        # are unique on both sides.
        for tier, days, names, unique in (
            ("exact days", "days", True, False),
            ("weekly pattern", "pattern", True, False),
            ("no route name", "pattern", False, True),
            ("no service days", None, True, True),
        ):
            self.trip_signatures(days, names)
            self.pair_trip_signatures(tier, unique)
            self.con.execute("DROP TABLE old_sig; DROP TABLE new_sig")
        if self.max_shift_min > 0:
            self.pair_trip_shifts()
        if self.min_shared_stops < 1:
            self.pair_trip_stop_changes()
        self.map_stop_sequences()

    def map_stop_sequences(self) -> None:
        """The old stop_sequence for each stop_times row of a matched trip, by
        stop (old stops renamed through stop_sig_map; a stop visited twice pairs
        by visit). A new stop the old trip did not call at gets a key of its own,
        so it is an added row. Only rows whose key differs are kept."""
        self.con.execute(
            f"""
            CREATE TABLE sequence_keys AS
            WITH o AS (
                SELECT s.trip_id, s.stop_sequence, stop, row_number() OVER (
                           PARTITION BY s.trip_id, stop
                           ORDER BY try_cast(s.stop_sequence AS DOUBLE)) AS visit
                FROM (SELECT s.*, coalesce(m.new_stop, 'old:' || s.stop_id) AS stop
                      FROM old_stop_times s
                      LEFT JOIN stop_sig_map m ON m.old_stop = s.stop_id) s
            ),
            n AS (
                SELECT s.trip_id, s.stop_sequence, p.old_id, s.stop_id AS stop,
                       row_number() OVER (
                           PARTITION BY s.trip_id, s.stop_id
                           ORDER BY try_cast(s.stop_sequence AS DOUBLE)) AS visit
                FROM new_stop_times s JOIN pairs_trip p ON p.new_id = s.trip_id
            ),
            aligned AS (
                SELECT n.trip_id AS new_trip, n.stop_sequence AS new_seq,
                       coalesce(o.stop_sequence,
                                '{UNMATCHED_PREFIX}' || n.stop_sequence) AS old_seq
                FROM n LEFT JOIN o
                  ON o.trip_id = n.old_id AND o.stop = n.stop AND o.visit = n.visit
            )
            SELECT * FROM aligned WHERE old_seq IS DISTINCT FROM new_seq
            """
        )
        path = str(Path(self.result.workdir) / "stop_sequence.parquet")
        self.con.execute(f"COPY sequence_keys TO '{path}' (FORMAT parquet)")
        self.result.sequence_parquet = path
        self.result.sequence = {
            (trip, new): old
            for trip, new, old in self.con.execute(
                "SELECT new_trip, new_seq, old_seq FROM sequence_keys"
            ).fetchall()
        }
        self.renumbered = self.q("SELECT count(DISTINCT new_trip) FROM sequence_keys")

    def pair_trip_stop_changes(self) -> None:
        """
        Unpaired trips with the same agency, route name, service dates (then
        weekly pattern) and direction that share at least min_shared_stops of
        their stops (counted on the longer trip) but not all of them, with
        times at the shared stops at most the time shift threshold apart. A pair
        is kept only when each trip is the other's only candidate, and no other
        unpaired trip of the same route departs between their first times or
        within twice the largest time change of them.
        """
        limit = self.max_shift_min * 60
        near = (
            "s.first_time > least(u.old_first, u.new_first) - 2 * u.shift_s "
            "AND s.first_time < greatest(u.old_first, u.new_first) + 2 * u.shift_s"
        )
        self.con.execute(
            "CREATE TABLE stop_change_pairs (old_id VARCHAR, new_id VARCHAR, "
            "key VARCHAR, old_first DOUBLE, new_first DOUBLE, longer BIGINT, "
            "shared DOUBLE, shift_s DOUBLE)"
        )
        # Exact service dates first, then the weekly pattern: services that run
        # a few days only (a holiday, an exam week) share an empty pattern.
        for days in ("days", "pattern"):
            for side in ("old", "new"):
                col = "old_id" if side == "old" else "new_id"
                stop, join = "s.stop_id", ""
                if side == "old":
                    stop = "coalesce(m.new_stop, 'old:' || s.stop_id)"
                    join = "LEFT JOIN stop_sig_map m ON m.old_stop = s.stop_id"
                self.con.execute(
                    f"""
                    CREATE OR REPLACE TABLE {side}_part_calls AS
                    SELECT trip_id, stop, t, row_number() OVER (
                               PARTITION BY trip_id, stop ORDER BY seq) AS visit
                    FROM (
                        SELECT s.trip_id, {stop} AS stop,
                               try_cast(s.stop_sequence AS DOUBLE) AS seq,
                               coalesce({_seconds("s.departure_time")},
                                        {_seconds("s.arrival_time")}) AS t
                        FROM {side}_stop_times s {join}
                        WHERE s.trip_id NOT IN (SELECT {col} FROM pairs_trip)
                    )
                    """
                )
                self.con.execute(
                    f"""
                    CREATE OR REPLACE TABLE {side}_part_key AS
                    SELECT t.trip_id, c.n, c.first_time,
                           md5(t.agency || '|' || t.route_name || '|'
                               || coalesce(d.days, 'none') || '|'
                               || coalesce(t.direction_id, '')) AS key
                    FROM {side}_trip_info t
                    JOIN (SELECT trip_id, count(*) AS n, min(t) AS first_time
                          FROM {side}_part_calls GROUP BY trip_id) c USING (trip_id)
                    LEFT JOIN {side}_{days} d USING (service_id)
                    WHERE c.first_time IS NOT NULL
                    """
                )
            self.con.execute(
                f"""
                CREATE OR REPLACE TABLE pass_pairs AS
                WITH c AS (
                    SELECT o.trip_id AS old_id, n.trip_id AS new_id, o.key,
                           o.first_time AS old_first, n.first_time AS new_first,
                           greatest(o.n, n.n) AS longer
                    FROM old_part_key o JOIN new_part_key n USING (key)
                    WHERE abs(n.first_time - o.first_time) <= {limit}
                ),
                scored AS (
                    SELECT c.old_id, c.new_id, c.key, c.old_first, c.new_first,
                           c.longer,
                           count(*) / c.longer AS shared,
                           max(abs(nc.t - oc.t)) AS shift_s
                    FROM c
                    JOIN old_part_calls oc ON oc.trip_id = c.old_id
                    JOIN new_part_calls nc ON nc.trip_id = c.new_id
                     AND nc.stop = oc.stop AND nc.visit = oc.visit
                    GROUP BY c.old_id, c.new_id, c.key, c.old_first, c.new_first,
                             c.longer
                    HAVING count(*) / c.longer >= {self.min_shared_stops}
                       AND count(*) < c.longer
                       AND max(abs(nc.t - oc.t)) <= {limit}
                )
                SELECT * FROM scored u
                WHERE (SELECT count(*) FROM scored x WHERE x.old_id = u.old_id) = 1
                  AND (SELECT count(*) FROM scored x WHERE x.new_id = u.new_id) = 1
                  AND NOT EXISTS (
                      SELECT 1 FROM old_part_key s
                      WHERE s.key = u.key AND s.trip_id <> u.old_id AND {near})
                  AND NOT EXISTS (
                      SELECT 1 FROM new_part_key s
                      WHERE s.key = u.key AND s.trip_id <> u.new_id AND {near})
                """
            )
            self.con.execute(
                "INSERT INTO pairs_trip "
                "SELECT old_id, new_id, 'most stops shared' FROM pass_pairs"
            )
            self.con.execute("INSERT INTO stop_change_pairs SELECT * FROM pass_pairs")
        pairs, least_shared = self.con.execute(
            "SELECT count(*), min(shared) FROM stop_change_pairs"
        ).fetchone()
        self.stop_changes = {
            "min_shared_stops": self.min_shared_stops,
            "paired": pairs,
            "least_shared": round(least_shared, 3) if least_shared else None,
        }
        _trace(f"  [matching] trips with most stops shared: {pairs:,} paired")

    def pair_trip_shifts(self) -> None:
        """
        Unpaired trips with the same agency, route name, weekly pattern, direction
        and stops, whose times differ by at most a limit at every stop, for each
        limit of shift_steps in turn. A pair is kept only when:
        - each trip is the other's only candidate within the limit;
        - no other trip of the same pattern departs between the two first times;
        - no other unpaired trip of the same pattern departs within
          SHIFT_ISOLATION_MIN of either first time, since nearby trips that all
          moved could be paired more than one way.
        """
        steps = shift_steps(self.max_shift_min)
        isolation = max(SHIFT_ISOLATION_MIN, self.max_shift_min) * 60

        def seconds(col: str) -> str:
            return (
                f"try_cast(split_part({col}, ':', 1) AS INTEGER) * 3600"
                f" + try_cast(split_part({col}, ':', 2) AS INTEGER) * 60"
                f" + try_cast(split_part({col}, ':', 3) AS INTEGER)"
            )

        for side in ("old", "new"):
            col = "old_id" if side == "old" else "new_id"
            stop, join = "s.stop_id", ""
            if side == "old":
                stop = "coalesce(m.new_stop, 'old:' || s.stop_id)"
                join = "LEFT JOIN stop_sig_map m ON m.old_stop = s.stop_id"
            self.con.execute(
                f"""
                CREATE TABLE {side}_shift_calls AS
                SELECT s.trip_id, {stop} AS stop,
                       {seconds("s.arrival_time")} AS arr,
                       {seconds("s.departure_time")} AS dep,
                       row_number() OVER (
                           PARTITION BY s.trip_id
                           ORDER BY try_cast(s.stop_sequence AS DOUBLE)) AS pos
                FROM {side}_stop_times s {join}
                WHERE s.trip_id NOT IN (SELECT {col} FROM pairs_trip)
                """
            )
            self.con.execute(
                f"""
                CREATE TABLE {side}_shift_key AS
                WITH calls AS (
                    SELECT trip_id, count(*) AS n, sum(hash(pos, stop)::HUGEINT) AS h,
                           min(coalesce(dep, arr)) FILTER (WHERE pos = 1) AS first_time
                    FROM {side}_shift_calls GROUP BY trip_id
                )
                SELECT t.trip_id, c.first_time,
                       md5(t.agency || '|' || t.route_name || '|'
                           || coalesce(d.days, 'none') || '|'
                           || coalesce(t.direction_id, '') || '|' || c.n || '|'
                           || c.h) AS key
                FROM {side}_trip_info t
                JOIN calls c USING (trip_id)
                LEFT JOIN {side}_pattern d USING (service_id)
                WHERE c.first_time IS NOT NULL
                """
            )
        self.con.execute(
            "CREATE TABLE trip_shifts "
            "(old_id VARCHAR, new_id VARCHAR, shift_s INTEGER, step VARCHAR)"
        )
        for limit_min in steps:
            limit = limit_min * 60
            step = f"time shift {limit_min:g} min"
            self.con.execute(
                f"""
                CREATE OR REPLACE TABLE shift_candidates AS
                WITH o AS (SELECT * FROM old_shift_key
                           WHERE trip_id NOT IN (SELECT old_id FROM pairs_trip)),
                n AS (SELECT * FROM new_shift_key
                      WHERE trip_id NOT IN (SELECT new_id FROM pairs_trip)),
                c AS (
                    -- The first stop bounds the shift, so it finds candidates cheaply.
                    SELECT o.trip_id AS old_id, n.trip_id AS new_id, o.key,
                           o.first_time AS old_first, n.first_time AS new_first
                    FROM o JOIN n USING (key)
                    WHERE abs(n.first_time - o.first_time) <= {limit}
                    QUALIFY count(*) OVER (PARTITION BY o.trip_id) = 1
                        AND count(*) OVER (PARTITION BY n.trip_id) = 1
                )
                SELECT c.old_id, c.new_id, c.key, c.old_first, c.new_first,
                       max(greatest(abs(nc.arr - oc.arr), abs(nc.dep - oc.dep)))
                           AS shift_s
                FROM c
                JOIN old_shift_calls oc ON oc.trip_id = c.old_id
                JOIN new_shift_calls nc ON nc.trip_id = c.new_id AND nc.pos = oc.pos
                GROUP BY ALL
                HAVING max(greatest(abs(nc.arr - oc.arr), abs(nc.dep - oc.dep)))
                       <= {limit}
                """
            )
            between = (
                "s.first_time > least(u.old_first, u.new_first) "
                "AND s.first_time < greatest(u.old_first, u.new_first)"
            )
            near = (
                f"s.first_time BETWEEN least(u.old_first, u.new_first) - {isolation} "
                f"AND greatest(u.old_first, u.new_first) + {isolation}"
            )
            self.con.execute(
                f"""
                INSERT INTO trip_shifts
                SELECT u.old_id, u.new_id, u.shift_s::INTEGER, '{step}'
                FROM shift_candidates u
                WHERE NOT EXISTS (
                    SELECT 1 FROM old_shift_key s
                    WHERE s.key = u.key AND s.trip_id <> u.old_id AND {between})
                AND NOT EXISTS (
                    SELECT 1 FROM new_shift_key s
                    WHERE s.key = u.key AND s.trip_id <> u.new_id AND {between})
                AND NOT EXISTS (
                    SELECT 1 FROM old_shift_key s
                    WHERE s.key = u.key AND s.trip_id <> u.old_id AND {near}
                      AND s.trip_id NOT IN (SELECT old_id FROM pairs_trip))
                AND NOT EXISTS (
                    SELECT 1 FROM new_shift_key s
                    WHERE s.key = u.key AND s.trip_id <> u.new_id AND {near}
                      AND s.trip_id NOT IN (SELECT new_id FROM pairs_trip))
                """
            )
            self.con.execute(
                f"""INSERT INTO pairs_trip
                    SELECT old_id, new_id, step FROM trip_shifts
                    WHERE step = '{step}'"""
            )
        pairs, largest, moved = self.con.execute(
            "SELECT count(*), max(shift_s), count(*) FILTER (WHERE shift_s > 0) "
            "FROM trip_shifts"
        ).fetchone()
        last = steps[-1] * 60
        declined = self.q(
            f"""
            SELECT count(DISTINCT o.trip_id)
            FROM old_shift_key o JOIN new_shift_key n USING (key)
            WHERE o.trip_id NOT IN (SELECT old_id FROM pairs_trip)
              AND n.trip_id NOT IN (SELECT new_id FROM pairs_trip)
              AND abs(n.first_time - o.first_time) <= {last}
            """
        )
        self.shifts = {
            "max_shift_minutes": self.max_shift_min,
            "paired": pairs,
            "largest_shift_seconds": largest or 0,
            "declined": declined,
        }
        _trace(
            f"  [matching] trip time shift: {pairs:,} paired ({moved:,} moved), "
            f"{declined:,} declined"
        )

    def build_trip_info(self, side: str) -> None:
        short_filled, long_filled = self.con.execute(
            f"SELECT count(route_short_name), count(route_long_name) FROM {side}_routes"
        ).fetchone()
        first, second = "route_short_name", "route_long_name"
        if long_filled > short_filled:
            first, second = second, first
        self.con.execute(
            f"""
            CREATE TABLE {side}_trip_info AS
            SELECT t.trip_id, t.service_id, t.direction_id,
                   coalesce(a.agency_name,
                            (SELECT min(agency_name) FROM {side}_agency), '') AS agency,
                   coalesce(r.route_type, '') || '|' ||
                   coalesce(r.{first}, r.{second}, '') AS route_name
            FROM {side}_trips t
            LEFT JOIN {side}_routes r USING (route_id)
            LEFT JOIN {side}_agency a ON a.agency_id IS NOT DISTINCT FROM r.agency_id
            WHERE t.trip_id IS NOT NULL
            """
        )
        # Old stops are renamed to the new stop they map to, if any.
        stop = "s.stop_id"
        join = ""
        if side == "old":
            stop = "coalesce(m.new_stop, 'old:' || s.stop_id)"
            join = "LEFT JOIN stop_sig_map m ON m.old_stop = s.stop_id"
        self.con.execute(
            f"""
            CREATE TABLE {side}_calls AS
            SELECT trip_id, count(*) AS n,
                   sum(hash(pos, stop, arrival_time, departure_time)::HUGEINT) AS h
            FROM (
                SELECT s.trip_id, s.arrival_time, s.departure_time, {stop} AS stop,
                       row_number() OVER (
                           PARTITION BY s.trip_id
                           ORDER BY try_cast(s.stop_sequence AS DOUBLE)) AS pos
                FROM {side}_stop_times s {join}
            )
            GROUP BY trip_id
            """
        )

    def build_service_days(self) -> None:
        """Active dates per service, clipped to the dates both feeds cover."""
        for side in ("old", "new"):
            self.con.execute(
                f"""
                CREATE TABLE {side}_dates AS
                WITH ranges AS (
                    SELECT service_id,
                           strptime(start_date, '%Y%m%d')::DATE AS d0,
                           strptime(end_date, '%Y%m%d')::DATE AS d1,
                           [sunday, monday, tuesday, wednesday, thursday, friday,
                            saturday] AS week
                    FROM {side}_calendar
                    WHERE try_strptime(start_date, '%Y%m%d') IS NOT NULL
                      AND try_strptime(end_date, '%Y%m%d') IS NOT NULL
                ),
                weekly AS (
                    SELECT service_id, d::DATE AS d
                    FROM ranges, generate_series(d0, d1, INTERVAL 1 DAY) AS g(d)
                    WHERE week[dayofweek(d::DATE) + 1] = '1'
                ),
                exceptions AS (
                    SELECT service_id, strptime(date, '%Y%m%d')::DATE AS d,
                           exception_type
                    FROM {side}_calendar_dates
                    WHERE try_strptime(date, '%Y%m%d') IS NOT NULL
                )
                SELECT service_id, d FROM weekly
                WHERE NOT EXISTS (
                    SELECT 1 FROM exceptions e
                    WHERE e.service_id = weekly.service_id AND e.d = weekly.d
                      AND e.exception_type = '2'
                )
                UNION
                SELECT service_id, d FROM exceptions WHERE exception_type = '1'
                """
            )
        lo, hi = self.con.execute(
            """
            SELECT greatest((SELECT min(d) FROM old_dates),
                            (SELECT min(d) FROM new_dates)),
                   least((SELECT max(d) FROM old_dates), (SELECT max(d) FROM new_dates))
            """
        ).fetchone()
        for side in ("old", "new"):
            self.con.execute(
                f"""
                CREATE TABLE {side}_days AS
                SELECT service_id, count(*) || ':' || sum(hash(d)::HUGEINT) AS days
                FROM {side}_dates WHERE d BETWEEN ? AND ?
                GROUP BY service_id
                """,
                [lo, hi],
            )
            # Weekdays the service runs on at least half the time, so one holiday
            # handled differently between versions does not break the match.
            self.con.execute(
                f"""
                CREATE TABLE {side}_pattern AS
                WITH window_days AS (
                    SELECT dayofweek(d::DATE) AS dow, count(*) AS total
                    FROM generate_series(?::DATE, ?::DATE, INTERVAL 1 DAY) AS g(d)
                    GROUP BY dow
                ),
                active AS (
                    SELECT service_id, dayofweek(d) AS dow, count(*) AS runs
                    FROM {side}_dates WHERE d BETWEEN ? AND ?
                    GROUP BY service_id, dow
                )
                SELECT service_id,
                       list_sort(list(a.dow) FILTER (WHERE a.runs * 2 >= w.total))
                           ::VARCHAR AS days
                FROM active a JOIN window_days w USING (dow)
                GROUP BY service_id
                """,
                [lo, hi, lo, hi],
            )

    def trip_signatures(self, days: str | None, names: bool) -> None:
        for side in ("old", "new"):
            key = "t.agency || '|' || t.route_name" if names else "''"
            days_join = ""
            if days:
                key += " || '|' || coalesce(d.days, 'none')"
                days_join = f"LEFT JOIN {side}_{days} d USING (service_id)"
            self.con.execute(
                f"""
                CREATE TABLE {side}_sig AS
                SELECT t.trip_id,
                       md5({key} || '|' || coalesce(t.direction_id, '')
                           || '|' || c.n || '|' || c.h) AS sig
                FROM {side}_trip_info t
                JOIN {side}_calls c USING (trip_id)
                {days_join}
                """
            )

    def pair_trip_signatures(self, tier: str, unique: bool) -> None:
        """
        One-to-one pairs with equal signatures among unpaired trips: same trip_id
        first, then in trip_id order. With *unique*, the trips not paired by
        trip_id pair only when their signature occurs once on each side.
        """
        only_once = "QUALIFY count(*) OVER (PARTITION BY sig) = 1" if unique else ""
        self.con.execute(
            f"""
            INSERT INTO pairs_trip
            WITH o AS (
                SELECT * FROM old_sig
                WHERE trip_id NOT IN (SELECT old_id FROM pairs_trip)
            ),
            n AS (
                SELECT * FROM new_sig
                WHERE trip_id NOT IN (SELECT new_id FROM pairs_trip)
            ),
            same_id AS (
                SELECT o.trip_id AS old_id, n.trip_id AS new_id
                FROM o JOIN n USING (trip_id) WHERE o.sig = n.sig
            ),
            ro AS (
                SELECT * FROM o WHERE trip_id NOT IN (SELECT old_id FROM same_id)
                {only_once}
            ),
            rn AS (
                SELECT * FROM n WHERE trip_id NOT IN (SELECT new_id FROM same_id)
                {only_once}
            ),
            rest_old AS (
                SELECT sig, trip_id,
                       row_number() OVER (PARTITION BY sig ORDER BY trip_id) AS k
                FROM ro
            ),
            rest_new AS (
                SELECT sig, trip_id,
                       row_number() OVER (PARTITION BY sig ORDER BY trip_id) AS k
                FROM rn
            )
            SELECT old_id, new_id, '{tier}' FROM same_id
            UNION ALL
            SELECT o.trip_id, n.trip_id, '{tier}'
            FROM rest_old o JOIN rest_new n USING (sig, k)
            """
        )
