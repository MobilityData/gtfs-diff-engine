"""Matching agencies, stops, routes and services."""

from __future__ import annotations

from .core import (
    METERS_PER_DEGREE,
    MIN_COS_LAT,
    STOP_RADIUS_M,
    _unique_on_both_sides,
)


class EntitiesMixin:
    # -- agencies -----------------------------------------------------------

    def match_agencies(self) -> None:
        self.ids("agency", "agency", "agency_id")
        name = "lower(coalesce(agency_name, ''))"
        self.add_pairs(
            "agency",
            _unique_on_both_sides(
                f"SELECT coalesce(agency_id, '') AS id, {name} AS key FROM old_agency",
                f"SELECT coalesce(agency_id, '') AS id, {name} AS key FROM new_agency",
                "same name",
            ),
        )
        self.pair_same_id("agency")

    # -- stops --------------------------------------------------------------

    def match_stops(self) -> None:
        """
        Each old stop maps to the nearest new stop within STOP_RADIUS_M, same name
        first and, among those, the stop with its own ID (stop_sig_map, used by
        trip signatures). Stop pairs are the mutual picks, then the same ID.
        """
        self.ids("stop", "stops", "stop_id", "WHERE stop_id IS NOT NULL")
        lat_cell = STOP_RADIUS_M / METERS_PER_DEGREE
        lon_cell = lat_cell / MIN_COS_LAT
        points = """SELECT stop_id, stop_name, try_cast(stop_lat AS DOUBLE) AS lat,
                           try_cast(stop_lon AS DOUBLE) AS lon
                    FROM {}_stops
                    WHERE try_cast(stop_lat AS DOUBLE) IS NOT NULL
                      AND try_cast(stop_lon AS DOUBLE) IS NOT NULL
                      AND stop_id IS NOT NULL"""
        self.con.execute(
            f"""
            CREATE TABLE stop_candidates AS
            WITH op AS ({points.format("old")}),
            -- Each old stop once per neighbouring cell, so the join is a hash join.
            o AS (
                SELECT op.*, floor(lat / {lat_cell})::BIGINT + dy AS cy,
                       floor(lon / {lon_cell})::BIGINT + dx AS cx
                FROM op, (VALUES (-1), (0), (1)) a(dy), (VALUES (-1), (0), (1)) b(dx)
            ),
            n AS (
                SELECT np.*, floor(lat / {lat_cell})::BIGINT AS cy,
                       floor(lon / {lon_cell})::BIGINT AS cx
                FROM ({points.format("new")}) np
            )
            SELECT o.stop_id AS old_id, n.stop_id AS new_id,
                   o.stop_name IS NOT DISTINCT FROM n.stop_name AS same_name,
                   {METERS_PER_DEGREE} * sqrt(
                       pow(n.lat - o.lat, 2)
                       + pow((n.lon - o.lon) * cos(radians(o.lat)), 2)
                   ) AS meters
            FROM o JOIN n ON n.cy = o.cy AND n.cx = o.cx
            WHERE meters <= {STOP_RADIUS_M}
            """
        )
        self.con.execute(
            """
            CREATE TABLE stop_sig_map AS
            SELECT old_id AS old_stop, new_id AS new_stop FROM stop_candidates
            QUALIFY row_number() OVER (
                PARTITION BY old_id
                ORDER BY same_name DESC, old_id = new_id DESC, meters, new_id) = 1
            """
        )
        self.add_pairs(
            "stop",
            """
            SELECT m.old_stop, m.new_stop, 'same place' FROM stop_sig_map m
            JOIN stop_candidates c ON c.old_id = m.old_stop AND c.new_id = m.new_stop
            QUALIFY row_number() OVER (
                PARTITION BY m.new_stop
                ORDER BY c.same_name DESC, c.old_id = c.new_id DESC, c.meters,
                         c.old_id) = 1
            """,
        )
        self.pair_same_id("stop")
        # A stop that moved or was renamed but kept its ID is still that stop
        # for the trips that call at it.
        self.con.execute(
            """
            INSERT INTO stop_sig_map
            SELECT old_id, new_id FROM pairs_stop
            WHERE how = 'same id'
              AND old_id NOT IN (SELECT old_stop FROM stop_sig_map)
            """
        )

    # -- routes and services ------------------------------------------------

    def match_routes(self, trips: bool) -> None:
        """Mutual best by trip votes, then the only route of its name, then ID."""
        self.ids("route", "routes", "route_id", "WHERE route_id IS NOT NULL")
        if trips:
            self.pair_by_votes("route", "route_id")
        agencies = {
            side: self.q(f"SELECT count(*) FROM {side}_agency")
            for side in ("old", "new")
        }
        names = """
            SELECT r.route_id AS id,
                   coalesce(a.agency_name, '') || '|' || coalesce(r.route_type, '')
                   || '|' || coalesce(r.route_short_name, r.route_long_name, '') AS key
            FROM {side}_routes r
            LEFT JOIN {side}_agency a
              ON a.agency_id IS NOT DISTINCT FROM r.agency_id
              OR {single}
            WHERE r.route_id NOT IN {left}
        """
        self.add_pairs(
            "route",
            _unique_on_both_sides(
                names.format(
                    side="old",
                    left=self.left("route", "old"),
                    single=str(agencies["old"] == 1).upper(),
                ),
                names.format(
                    side="new",
                    left=self.left("route", "new"),
                    single=str(agencies["new"] == 1).upper(),
                ),
                "only route of name",
            ),
        )
        self.pair_same_id("route")

    def match_services(self, trips: bool) -> None:
        for side in ("old", "new"):
            self.con.execute(
                f"""CREATE TABLE {side}_service_ids AS
                    SELECT service_id FROM {side}_calendar
                    UNION SELECT service_id FROM {side}_calendar_dates"""
            )
        self.ids("service", "service_ids", "service_id", "WHERE service_id IS NOT NULL")
        if trips:
            self.pair_by_votes("service", "service_id")
        self.pair_same_id("service")
