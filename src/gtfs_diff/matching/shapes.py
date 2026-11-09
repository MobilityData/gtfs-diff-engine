"""Matching shapes and describing how each one changed."""

from __future__ import annotations

from ..geometry import line_gaps
from ..highlights import DEFAULT_SHAPE_MOVED_M
from .core import (
    METERS_PER_DEGREE,
    SHAPE_CELL_M,
    SHAPE_NEAR_M,
    SHAPE_SAMPLES,
    ShapeChange,
    _meters,
    _mutual_best,
    _unique_on_both_sides,
)


class ShapesMixin:
    # -- shapes -------------------------------------------------------------

    # A matched shape this far apart somewhere counts as moved ('changed').
    shape_moved_m: float = DEFAULT_SHAPE_MOVED_M

    def match_shapes(self, trips: bool, geometry: bool) -> None:
        """
        Mutual best by trip votes, then the only unpaired shape of a matched route
        and direction, then identical points, then lines that stay within
        SHAPE_NEAR_M of each other, then the same ID.
        """
        self.ids("shape", "shapes", "shape_id", "WHERE shape_id IS NOT NULL")
        for side in ("old", "new"):
            self.build_shape_geometry(side)
        if trips:
            self.pair_by_votes("shape", "shape_id")
            if "route" in self.result.keys:
                self.pair_shapes_by_route()
        self.pair_shapes_by_points()
        self.pair_same_id("shape")
        self.con.execute(
            f"""
            CREATE TABLE shape_pairs AS
            SELECT p.*, d.dev_m FROM pairs_shape p
            LEFT JOIN ({self.deviation_sql("pairs_shape")}) d USING (old_id, new_id)
            """
        )
        self.classify_shapes(geometry)

    def build_shape_geometry(self, side: str) -> None:
        """Per shape: length, ends, a fingerprint of its points and samples."""
        seg = _meters("lag(lat) OVER w", "lag(lon) OVER w", "lat", "lon")
        self.con.execute(
            f"""
            CREATE TABLE {side}_pts AS
            SELECT shape_id, seq, lat, lon,
                   sum(seg) OVER (PARTITION BY shape_id ORDER BY seq
                                  ROWS UNBOUNDED PRECEDING) AS cum
            FROM (
                SELECT shape_id, seq, lat, lon, coalesce({seg}, 0) AS seg
                FROM (
                    SELECT shape_id, try_cast(shape_pt_sequence AS DOUBLE) AS seq,
                           try_cast(shape_pt_lat AS DOUBLE) AS lat,
                           try_cast(shape_pt_lon AS DOUBLE) AS lon
                    FROM {side}_shapes WHERE shape_id IS NOT NULL
                )
                WHERE lat IS NOT NULL AND lon IS NOT NULL AND seq IS NOT NULL
                WINDOW w AS (PARTITION BY shape_id ORDER BY seq)
            )
            """
        )
        self.con.execute(
            f"""
            CREATE TABLE {side}_shape AS
            SELECT shape_id, count(*) AS points, max(cum) AS length_m,
                   arg_min(lat, seq) AS lat0, arg_min(lon, seq) AS lon0,
                   arg_max(lat, seq) AS lat1, arg_max(lon, seq) AS lon1,
                   md5(string_agg(round(lat, 5) || ',' || round(lon, 5), ';'
                                  ORDER BY seq)) AS fingerprint
            FROM {side}_pts GROUP BY shape_id
            """
        )
        self.con.execute(
            f"""
            CREATE TABLE {side}_seg AS
            SELECT shape_id, cum, lat, lon, lead(cum) OVER w AS next_cum,
                   lead(lat) OVER w AS next_lat, lead(lon) OVER w AS next_lon
            FROM {side}_pts WINDOW w AS (PARTITION BY shape_id ORDER BY seq)
            """
        )
        last = SHAPE_SAMPLES - 1
        self.con.execute(
            f"""
            CREATE TABLE {side}_sample AS
            WITH targets AS (
                SELECT s.shape_id, k, s.length_m * k / {last} AS target
                FROM {side}_shape s, range({SHAPE_SAMPLES}) r(k)
            )
            SELECT t.shape_id, t.k,
                   g.lat + coalesce((g.next_lat - g.lat) * f, 0) AS lat,
                   g.lon + coalesce((g.next_lon - g.lon) * f, 0) AS lon
            FROM targets t
            ASOF JOIN {side}_seg g ON g.shape_id = t.shape_id AND t.target >= g.cum,
            LATERAL (SELECT CASE WHEN g.next_cum > g.cum
                                 THEN least((t.target - g.cum)
                                            / (g.next_cum - g.cum), 1)
                            END AS f)
            """
        )

    def deviation_sql(self, table: str) -> str:
        """The largest gap between matching samples of each (old_id, new_id)."""
        gap = _meters("o.lat", "o.lon", "n.lat", "n.lon")
        return f"""
            SELECT p.old_id, p.new_id, max({gap}) AS dev_m
            FROM {table} p
            JOIN old_sample o ON o.shape_id = p.old_id
            JOIN new_sample n ON n.shape_id = p.new_id AND n.k = o.k
            GROUP BY ALL
        """

    def pair_shapes_by_route(self) -> None:
        """The only unpaired shape of a matched route and direction on each side."""
        for side, route in (
            ("old", "coalesce(m.new, '~old~' || t.route_id)"),
            ("new", "t.route_id"),
        ):
            # Old routes are keyed by the new route they matched.
            join = (
                "LEFT JOIN (SELECT p.old_id AS old, p.new_id AS new "
                "FROM pairs_route p) m ON m.old = t.route_id"
                if side == "old"
                else ""
            )
            self.con.execute(
                f"""
                CREATE TABLE {side}_shape_route AS
                SELECT shape_id AS id, arg_max(route_key, n) AS key FROM (
                    SELECT t.shape_id,
                           {route} || '|' || coalesce(t.direction_id, '') AS route_key,
                           count(*) AS n
                    FROM {side}_trips t {join}
                    WHERE t.shape_id IS NOT NULL
                    GROUP BY ALL
                ) GROUP BY shape_id
                """
            )
        self.add_pairs(
            "shape",
            _unique_on_both_sides(
                "SELECT * FROM old_shape_route "
                f"WHERE id NOT IN {self.left('shape', 'old')}",
                "SELECT * FROM new_shape_route "
                f"WHERE id NOT IN {self.left('shape', 'new')}",
                "only shape of route",
            ),
        )

    def pair_shapes_by_points(self) -> None:
        left_old = f"shape_id NOT IN {self.left('shape', 'old')}"
        left_new = f"shape_id NOT IN {self.left('shape', 'new')}"
        self.add_pairs(
            "shape",
            f"""SELECT o.shape_id, n.shape_id, 'identical points'
                FROM old_shape o JOIN new_shape n USING (shape_id, fingerprint)
                WHERE o.{left_old} AND n.{left_new}""",
        )
        self.add_pairs(
            "shape",
            _unique_on_both_sides(
                f"SELECT shape_id AS id, fingerprint AS key FROM old_shape "
                f"WHERE {left_old}",
                f"SELECT shape_id AS id, fingerprint AS key FROM new_shape "
                f"WHERE {left_new}",
                "identical points",
            ),
        )
        # Near: start in a neighbouring cell, similar length, ends close.
        cell = SHAPE_CELL_M / METERS_PER_DEGREE
        self.con.execute(
            f"""
            CREATE TABLE shape_candidates AS
            WITH o AS (SELECT *, floor(lat0 / {cell})::BIGINT AS cy,
                              floor(lon0 / {cell})::BIGINT AS cx
                       FROM old_shape WHERE {left_old}),
                 n AS (SELECT *, floor(lat0 / {cell})::BIGINT AS cy,
                              floor(lon0 / {cell})::BIGINT AS cx
                       FROM new_shape WHERE {left_new}),
                 oc AS (SELECT o.*, cy + dy AS ky, cx + dx AS kx FROM o,
                        (VALUES (-1),(0),(1)) a(dy), (VALUES (-1),(0),(1)) b(dx))
            SELECT oc.shape_id AS old_id, n.shape_id AS new_id
            FROM oc JOIN n ON n.cy = oc.ky AND n.cx = oc.kx
            WHERE abs(n.length_m - oc.length_m)
                      <= 0.2 * greatest(n.length_m, oc.length_m)
              AND {_meters("oc.lat1", "oc.lon1", "n.lat1", "n.lon1")} <= {SHAPE_CELL_M}
            """
        )
        best = _mutual_best(
            f"SELECT * FROM ({self.deviation_sql('shape_candidates')}) "
            f"WHERE dev_m <= {SHAPE_NEAR_M}",
            "round(dev_m)",
            "ASC",
        )
        self.add_pairs("shape", f"SELECT old_id, new_id, 'near points' FROM ({best})")

    def shape_gaps(self) -> None:
        """Table shape_gap: the largest distance between each pair's lines, on
        every point (dev_m's samples can miss a short detour)."""
        pairs = self.con.execute(
            """
            SELECT p.old_id, p.new_id FROM shape_pairs p
            JOIN old_shape o ON o.shape_id = p.old_id
            JOIN new_shape n ON n.shape_id = p.new_id
            WHERE o.fingerprint <> n.fingerprint
            """
        ).fetchall()
        coords = {}
        for side, col in (("old", 0), ("new", 1)):
            ids = sorted({p[col] for p in pairs})
            coords[side] = dict(
                self.con.execute(
                    f"""SELECT shape_id, list([lon, lat] ORDER BY seq) FROM {side}_pts
                        WHERE shape_id IN (SELECT unnest(?)) GROUP BY shape_id""",
                    [ids],
                ).fetchall()
            )
        rows = []
        for old_id, new_id in pairs:
            a, b = coords["old"].get(old_id), coords["new"].get(new_id)
            if a and b and len(a) > 1 and len(b) > 1:
                gap = max(max(line_gaps(a, b)), max(line_gaps(b, a)))
                rows.append((old_id, new_id, gap))
        self.con.execute(
            "CREATE TABLE shape_gap (old_id VARCHAR, new_id VARCHAR, gap_m DOUBLE)"
        )
        if rows:
            self.con.executemany("INSERT INTO shape_gap VALUES (?, ?, ?)", rows)

    def classify_shapes(self, geometry: bool) -> None:
        """Count shapes by kind of change, and list them when *geometry*.
        'changed' uses the shape_moved highlight's test."""
        self.shape_gaps()
        self.con.execute(
            f"""
            CREATE TABLE shape_change AS
            SELECT CASE
                       WHEN o.fingerprint = n.fingerprint AND p.old_id = p.new_id
                           THEN 'same'
                       WHEN o.fingerprint = n.fingerprint THEN 'renamed'
                       WHEN coalesce(g.gap_m, p.dev_m) < {float(self.shape_moved_m)}
                           THEN 'redrawn'
                       ELSE 'changed'
                   END AS change,
                   p.old_id, p.new_id, coalesce(g.gap_m, p.dev_m) AS dev_m,
                   o.length_m AS old_len, n.length_m AS new_len,
                   o.points AS old_points, n.points AS new_points
            FROM shape_pairs p
            JOIN old_shape o ON o.shape_id = p.old_id
            JOIN new_shape n ON n.shape_id = p.new_id
            LEFT JOIN shape_gap g ON g.old_id = p.old_id AND g.new_id = p.new_id
            UNION ALL
            SELECT 'deleted', shape_id, NULL, NULL, length_m, NULL, points, NULL
            FROM old_shape
            WHERE shape_id NOT IN (SELECT old_id FROM shape_pairs)
            UNION ALL
            SELECT 'added', NULL, shape_id, NULL, NULL, length_m, NULL, points
            FROM new_shape
            WHERE shape_id NOT IN (SELECT new_id FROM shape_pairs)
            """
        )
        counts = dict(
            self.con.execute(
                "SELECT change, count(*) FROM shape_change GROUP BY change"
            ).fetchall()
        )
        self.shape_counts = {
            k: counts.get(k, 0)
            for k in ("same", "renamed", "redrawn", "changed", "added", "deleted")
        }
        if geometry:
            self.result.shape_changes = self.shape_geometry()

    def shape_geometry(self) -> list[ShapeChange]:
        for side in ("old", "new"):
            self.con.execute(
                f"""
                CREATE TABLE {side}_shape_label AS
                SELECT shape_id, arg_max(route, n) AS route FROM (
                    SELECT t.shape_id,
                           coalesce(r.route_short_name, r.route_long_name,
                                    t.route_id) AS route,
                           count(*) AS n
                    FROM {side}_trips t LEFT JOIN {side}_routes r USING (route_id)
                    WHERE t.shape_id IS NOT NULL GROUP BY ALL
                ) GROUP BY shape_id
                """
            )
            self.con.execute(
                f"""
                CREATE TABLE {side}_coords AS
                SELECT shape_id,
                       list([round(lon, 6), round(lat, 6), seq] ORDER BY seq)
                           AS coords
                FROM {side}_pts
                WHERE shape_id IN (
                    SELECT {side}_id FROM shape_change
                    WHERE change NOT IN ('same', 'renamed'))
                GROUP BY shape_id
                """
            )
        rows = self.con.execute(
            """
            SELECT c.change, c.old_id, c.new_id, round(c.dev_m, 1),
                   round(c.old_len, 1), round(c.new_len, 1),
                   coalesce(ln.route, lo.route), oc.coords, nc.coords,
                   c.old_points, c.new_points
            FROM shape_change c
            LEFT JOIN old_shape_label lo ON lo.shape_id = c.old_id
            LEFT JOIN new_shape_label ln ON ln.shape_id = c.new_id
            LEFT JOIN old_coords oc ON oc.shape_id = c.old_id
            LEFT JOIN new_coords nc ON nc.shape_id = c.new_id
            WHERE c.change <> 'same'
            ORDER BY CASE c.change WHEN 'changed' THEN 0 WHEN 'added' THEN 1
                     WHEN 'deleted' THEN 2 WHEN 'redrawn' THEN 3 ELSE 4 END,
                     c.dev_m DESC NULLS LAST, coalesce(c.old_id, c.new_id)
            """
        ).fetchall()
        return [
            ShapeChange(
                change=r[0],
                base_shape_id=r[1],
                new_shape_id=r[2],
                deviation_m=r[3],
                base_length_m=r[4],
                new_length_m=r[5],
                route=r[6],
                base_coords=r[7],
                new_coords=r[8],
                base_points=r[9],
                new_points=r[10],
            )
            for r in rows
        ]
