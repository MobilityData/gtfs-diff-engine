"""Tests for content-based ID matching, shape geometry and the HTML report."""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import pytest
from click.testing import CliRunner

from gtfs_diff.cli import main
from gtfs_diff.diff.helpers import _select_modified
from gtfs_diff.engine import diff_feeds, run_diff
from gtfs_diff.matching import shift_steps
from gtfs_diff.models import GtfsDiff
from gtfs_diff.report.html import render_html
from gtfs_diff.report.shape_points import thin
from tests.helpers import write_zip

AGENCY = "agency_id,agency_name,agency_url,agency_timezone\nA,Agency,http://a,UTC\n"
CALENDAR = (
    "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
    "start_date,end_date\n{sid},1,1,1,1,1,0,0,20260101,20261231\n"
)
STOPS = (
    "stop_id,stop_name,stop_lat,stop_lon\n"
    "{s1},First,45.50000,-73.50000\n"
    "{s2},Second,45.51000,-73.51000\n"
)
ROUTES = "route_id,agency_id,route_short_name,route_type\n"
SHAPES_HEADER = "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\n"


def _shape(shape_id: str, offset: float = 0.0) -> str:
    """A 2 km line north-east from (45.5, -73.5), shifted north by *offset* deg."""
    return "".join(
        f"{shape_id},{45.5 + i * 0.001 + offset:.6f},{-73.5 + i * 0.001:.6f},{i}\n"
        for i in range(20)
    )


def _clock(start: str, plus_min: int = 0) -> str:
    """HH or HH:MM plus some minutes, as HH:MM:SS."""
    hh, _, mm = start.partition(":")
    total = int(hh) * 60 + int(mm or 0) + plus_min
    return f"{total // 60:02d}:{total % 60:02d}:00"


def _feed(
    trips: list[tuple[str, str]],
    *,
    route: str = "R1",
    sid: str = "WK",
    stops: tuple[str, str] = ("S1", "S2"),
    shape: str = "SH1",
    shape_offset: float = 0.0,
) -> dict[str, str]:
    """A feed with one route, one service, one shape and trips (trip_id, time)."""
    s1, s2 = stops
    trip_rows = "".join(f"{route},{sid},{t},0,{shape}\n" for t, _ in trips)
    times = "".join(
        f"{t},{s1},1,{_clock(start)},{_clock(start)}\n"
        f"{t},{s2},2,{_clock(start, 10)},{_clock(start, 10)}\n"
        for t, start in trips
    )
    return {
        "agency.txt": AGENCY,
        "routes.txt": ROUTES + f"{route},A,10,3\n",
        "calendar.txt": CALENDAR.format(sid=sid),
        "stops.txt": STOPS.format(s1=s1, s2=s2),
        "trips.txt": "route_id,service_id,trip_id,direction_id,shape_id\n" + trip_rows,
        "stop_times.txt": (
            "trip_id,stop_id,stop_sequence,arrival_time,departure_time\n" + times
        ),
        "shapes.txt": SHAPES_HEADER + _shape(shape, shape_offset),
    }


BASE_TRIPS = [("T1", "08"), ("T2", "09")]


def _file(diff, name: str):
    return next((fd for fd in diff.file_diffs if fd.file_name == name), None)


def _zips(tmp_path: Path, base: dict, new: dict) -> tuple[Path, Path]:
    return (
        write_zip(tmp_path / "base.zip", base),
        write_zip(tmp_path / "new.zip", new),
    )


class TestIdMatching:
    def _renamed_feeds(self, tmp_path: Path):
        return _zips(
            tmp_path,
            _feed(BASE_TRIPS),
            _feed(
                [("X1", "08"), ("X2", "09")],
                route="R9",
                sid="SV9",
                stops=("N1", "N2"),
                shape="SH9",
            ),
        )

    def test_renamed_ids_are_paired(self, tmp_path: Path):
        """Every ID was regenerated and nothing else changed: no row changes."""
        run = run_diff(*self._renamed_feeds(tmp_path))
        assert run.diff.file_diffs == []
        assert "id_matching" not in run.diff.model_dump()

        entities = run.matching["entities"]
        for kind in ("trip", "stop", "route", "service", "shape"):
            assert entities[kind]["matched_new_id"] == entities[kind]["matched"] > 0

    @pytest.mark.parametrize("threshold", [None, 0])
    def test_include_key_changes(self, tmp_path: Path, threshold):
        diff = diff_feeds(
            *self._renamed_feeds(tmp_path),
            include_key_changes=True,
            large_file_threshold_bytes=threshold,
        )

        def changes(name):
            fd = _file(diff, name)
            assert fd.stats.rows_added_count == fd.stats.rows_deleted_count == 0
            return {
                (c.field, c.base_value, c.new_value)
                for r in fd.row_changes.modified
                for c in r.field_changes
            }

        # Key columns only: trips.route_id, service_id and shape_id still
        # follow their matched entities.
        assert changes("trips.txt") == {
            ("trip_id", "T1", "X1"),
            ("trip_id", "T2", "X2"),
        }
        assert changes("stops.txt") == {
            ("stop_id", "S1", "N1"),
            ("stop_id", "S2", "N2"),
        }
        assert changes("stop_times.txt") == {
            ("trip_id", "T1", "X1"),
            ("trip_id", "T2", "X2"),
        }
        assert changes("shapes.txt") == {("shape_id", "SH1", "SH9")}
        assert changes("routes.txt") == {("route_id", "R1", "R9")}

    def test_without_matching_ids_decide(self, tmp_path: Path):
        base, new = _zips(
            tmp_path, _feed(BASE_TRIPS), _feed([("X1", "08"), ("X2", "09")])
        )
        run = run_diff(base, new, id_matching=False)
        trips = _file(run.diff, "trips.txt")
        assert trips.stats.rows_added_count == 2
        assert trips.stats.rows_deleted_count == 2
        assert run.matching is None

    def test_reused_trip_id_is_not_paired(self, tmp_path: Path):
        """A trip_id now on a trip with other times is a new trip."""
        base, new = _zips(
            tmp_path, _feed(BASE_TRIPS), _feed([("T1", "08"), ("T2", "14")])
        )
        diff = diff_feeds(base, new)
        trips = _file(diff, "trips.txt")
        assert trips.stats.rows_added_count == 1
        assert trips.stats.rows_deleted_count == 1
        assert trips.row_changes.added[0].identifier == {"trip_id": "T2"}
        assert trips.row_changes.deleted[0].identifier == {"trip_id": "T2"}

    def test_identical_copies_keep_their_ids_when_days_change(self, tmp_path: Path):
        """Same times on two services; both calendars change, so only the
        no-days pass can pair them, and their signatures are not unique."""
        cal = CALENDAR.split("\n")[0] + "\n"
        base_files = _feed([])
        new_files = _feed([])
        for files, days in (
            (base_files, ("1,1,1,1,1,0,0", "0,0,0,0,0,1,0")),
            (new_files, ("1,1,1,1,0,0,0", "0,0,0,0,0,1,1")),
        ):
            files["calendar.txt"] = cal + "".join(
                f"{sid},{d},20260101,20261231\n"
                for sid, d in zip(("WK", "SA"), days, strict=True)
            )
            files["trips.txt"] = (
                "route_id,service_id,trip_id,direction_id,shape_id\n"
                "R1,WK,T1,0,SH1\nR1,SA,T2,0,SH1\n"
            )
            files["stop_times.txt"] = (
                "trip_id,stop_id,stop_sequence,arrival_time,departure_time\n"
                "T1,S1,1,08:00:00,08:00:00\nT1,S2,2,08:10:00,08:10:00\n"
                "T2,S1,1,08:00:00,08:00:00\nT2,S2,2,08:10:00,08:10:00\n"
            )
        base, new = _zips(tmp_path, base_files, new_files)
        run = run_diff(base, new, trip_max_shift_minutes=0)
        diff = run.diff
        summary = run.matching["entities"]["trip"]
        assert summary["matched_same_id"] == 2
        assert _file(diff, "trips.txt") is None

    def test_trips_through_a_moved_stop_still_pair(self, tmp_path: Path):
        """S2 moved about 50 m and kept its ID: its trips are the same trips."""
        new_files = _feed(BASE_TRIPS)
        new_files["stops.txt"] = new_files["stops.txt"].replace(
            "45.51000,-73.51000", "45.51045,-73.51000"
        )
        base, new = _zips(tmp_path, _feed(BASE_TRIPS), new_files)
        run = run_diff(base, new, trip_max_shift_minutes=0)
        diff = run.diff
        assert run.matching["entities"]["trip"]["matched_same_id"] == 2
        assert _file(diff, "trips.txt") is None
        assert _file(diff, "stops.txt").stats.rows_modified_count == 1

    @pytest.mark.parametrize("threshold", [None, 0])
    def test_renumbered_stops_pair_by_position(self, tmp_path: Path, threshold):
        """X1 is T1 renamed and renumbered 1,2 -> 10,20, and its last stop
        leaves a minute later: only that row is reported."""
        new_files = _feed([("X1", "08"), ("T2", "09")])
        new_files["stop_times.txt"] = (
            new_files["stop_times.txt"]
            .replace("X1,S1,1,", "X1,S1,10,")
            .replace("X1,S2,2,08:10:00,08:10:00", "X1,S2,20,08:10:00,08:11:00")
        )
        base, new = _zips(tmp_path, _feed(BASE_TRIPS), new_files)
        run = run_diff(base, new, large_file_threshold_bytes=threshold)
        diff = run.diff
        assert run.matching["entities"]["trip"]["renumbered_stops"] == 1
        times = _file(diff, "stop_times.txt")
        assert times.stats.rows_added_count == times.stats.rows_deleted_count == 0
        (row,) = times.row_changes.modified
        assert row.identifier == {"trip_id": "T1", "stop_sequence": "2"}
        new_key = run.new_identifiers["stop_times.txt"][row.base_line_number]
        assert new_key == {"trip_id": "X1", "stop_sequence": "20"}
        assert "new_identifier" not in row.model_dump()
        assert [(c.field, c.new_value) for c in row.field_changes] == [
            ("departure_time", "08:11:00")
        ]

    def test_new_identifier_on_modified_child_rows(self, tmp_path: Path):
        header = "trip_id,start_time,end_time,headway_secs\n"
        base_files = _feed(BASE_TRIPS)
        base_files["frequencies.txt"] = header + "T1,08:00:00,09:00:00,900\n"
        new_files = _feed([("X1", "08"), ("X2", "09")])
        new_files["frequencies.txt"] = header + "X1,08:00:00,09:00:00,600\n"
        base, new = _zips(tmp_path, base_files, new_files)
        run = run_diff(base, new)
        freq = _file(run.diff, "frequencies.txt")
        row = freq.row_changes.modified[0]
        assert row.identifier == {"trip_id": "T1", "start_time": "08:00:00"}
        new_key = run.new_identifiers["frequencies.txt"][row.base_line_number]
        assert new_key == {"trip_id": "X1", "start_time": "08:00:00"}
        assert [c.field for c in row.field_changes] == ["headway_secs"]

    def test_duckdb_backend_matches_in_memory(self, tmp_path: Path):
        base_files = _feed(BASE_TRIPS + [("T3", "10")])
        new_files = _feed(
            [("X1", "08"), ("X2", "09"), ("T3", "11")], stops=("N1", "N2")
        )
        base, new = _zips(tmp_path, base_files, new_files)
        in_memory = diff_feeds(base, new, include_key_changes=True)
        on_disk = diff_feeds(
            base, new, include_key_changes=True, large_file_threshold_bytes=0
        )

        def rows(diff, name):
            fd = _file(diff, name)
            rc = fd.row_changes
            return (
                fd.stats.model_dump(),
                sorted(json.dumps(r.model_dump(), sort_keys=True) for r in rc.added),
                sorted(json.dumps(r.model_dump(), sort_keys=True) for r in rc.deleted),
                sorted(json.dumps(r.model_dump(), sort_keys=True) for r in rc.modified),
            )

        for name in ("trips.txt", "stop_times.txt", "stops.txt"):
            assert rows(in_memory, name) == rows(on_disk, name)


class TestTripTimeShift:
    def _trips(self, tmp_path: Path, new_trips, **kwargs):
        base, new = _zips(tmp_path, _feed(BASE_TRIPS), _feed(new_trips))
        run = run_diff(base, new, **kwargs)
        return run.diff, _file(run.diff, "trips.txt"), run.matching["entities"]["trip"]

    def test_shift_within_default_pairs(self, tmp_path: Path):
        diff, trips, summary = self._trips(tmp_path, [("T1", "08"), ("X2", "09:10")])
        assert trips is None  # X2 is T2 with a new ID: not a change by default
        assert summary["by_step"]["time shift 10 min"] == 1
        assert summary["time_shift"]["largest_shift_seconds"] == 600
        times = _file(diff, "stop_times.txt")
        assert times.stats.rows_modified_count == 2
        row = times.row_changes.modified[0]
        assert row.identifier["trip_id"] == "T2"
        assert {c.field for c in row.field_changes} == {
            "arrival_time",
            "departure_time",
        }

    def test_shift_beyond_threshold_is_not_paired(self, tmp_path: Path):
        new_trips = [("T1", "08"), ("T2", "09:20")]
        _, trips, _ = self._trips(tmp_path, new_trips)
        assert trips.stats.rows_added_count == trips.stats.rows_deleted_count == 1
        diff, trips, summary = self._trips(
            tmp_path, new_trips, trip_max_shift_minutes=30
        )
        assert trips is None  # same trip_id, same row: nothing to report
        assert summary["by_step"]["time shift 20 min"] == 1
        assert _file(diff, "stop_times.txt").stats.rows_modified_count == 2

    def test_zero_turns_shifts_off(self, tmp_path: Path):
        diff, trips, summary = self._trips(
            tmp_path, [("T1", "08"), ("T2", "09:05")], trip_max_shift_minutes=0
        )
        assert trips.stats.rows_added_count == trips.stats.rows_deleted_count == 1
        assert "time_shift" not in summary

    def test_nearby_moved_trips_are_not_paired(self, tmp_path: Path):
        """Two trips 30 min apart both moved: either pairing is possible."""
        base, new = _zips(
            tmp_path,
            _feed([("T1", "08:00"), ("T2", "08:30")]),
            _feed([("X1", "08:10"), ("X2", "08:40")]),
        )
        summary = run_diff(base, new).matching["entities"]["trip"]
        assert summary["matched"] == 0
        assert summary["time_shift"]["declined"] == 2

    def test_shift_steps(self):
        assert shift_steps(15) == [5, 10, 15]
        assert shift_steps(12) == [5, 10, 12]
        assert shift_steps(3) == [3]
        assert shift_steps(0) == []


def _long_feed(trips: list[tuple[str, str, list[int]]]) -> dict[str, str]:
    """One route with stops S01..S12, and trips (trip_id, start, stop numbers)."""
    stops = "".join(
        f"S{i:02d},Stop {i},{45.5 + i * 0.01:.5f},-73.50000\n" for i in range(1, 13)
    )
    times = "".join(
        f"{t},S{i:02d},{seq},{_clock(start, 2 * i)},{_clock(start, 2 * i)}\n"
        for t, start, numbers in trips
        for seq, i in enumerate(numbers, 1)
    )
    return {
        "agency.txt": AGENCY,
        "routes.txt": ROUTES + "R1,A,10,3\n",
        "calendar.txt": CALENDAR.format(sid="WK"),
        "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\n" + stops,
        "trips.txt": "route_id,service_id,trip_id,direction_id\n"
        + "".join(f"R1,WK,{t},0\n" for t, _, _ in trips),
        "stop_times.txt": (
            "trip_id,stop_id,stop_sequence,arrival_time,departure_time\n" + times
        ),
    }


TEN = list(range(1, 11))


class TestTripStopChanges:
    def _run(self, tmp_path: Path, new_trips, **kwargs):
        base, new = _zips(
            tmp_path,
            _long_feed([("T1", "08", TEN), ("T2", "10", TEN)]),
            _long_feed(new_trips),
        )
        run = run_diff(base, new, **kwargs)
        return run.diff, run.matching["entities"]["trip"]

    def test_trip_with_a_removed_stop_pairs(self, tmp_path: Path):
        without_5 = [i for i in TEN if i != 5]
        diff, summary = self._run(
            tmp_path, [("X1", "08", without_5), ("T2", "10", TEN)]
        )
        assert summary["by_step"]["most stops shared"] == 1
        assert summary["shared_stops"]["least_shared"] == 0.9
        assert _file(diff, "trips.txt") is None
        times = _file(diff, "stop_times.txt").stats
        assert (times.rows_deleted_count, times.rows_added_count) == (1, 0)
        assert times.rows_modified_count == 0  # later stops keep their old numbers

    def test_added_stop_is_an_added_row(self, tmp_path: Path):
        with_11 = [*TEN[:5], 11, *TEN[5:]]
        diff, summary = self._run(tmp_path, [("T1", "08", with_11), ("T2", "10", TEN)])
        assert summary["by_step"]["most stops shared"] == 1
        times = _file(diff, "stop_times.txt")
        assert (times.stats.rows_added_count, times.stats.rows_deleted_count) == (1, 0)
        assert times.row_changes.added[0].raw_value.split(",")[1] == "S11"

    def test_too_few_shared_stops_do_not_pair(self, tmp_path: Path):
        without_4_5 = [i for i in TEN if i not in (4, 5)]
        new_trips = [("X1", "08", without_4_5), ("T2", "10", TEN)]
        diff, summary = self._run(tmp_path, new_trips)
        assert "most stops shared" not in summary["by_step"]
        assert _file(diff, "trips.txt").stats.rows_added_count == 1
        _, summary = self._run(tmp_path, new_trips, trip_min_shared_stops=0.8)
        assert summary["by_step"]["most stops shared"] == 1

    def test_moved_trips_close_together_do_not_pair(self, tmp_path: Path):
        """Trips 10 min apart that both moved 6 min: either pairing is possible."""
        without_5 = [i for i in TEN if i != 5]
        base, new = _zips(
            tmp_path,
            _long_feed([("T1", "08", TEN), ("T2", "08:10", TEN)]),
            _long_feed([("X1", "08:06", without_5), ("X2", "08:16", without_5)]),
        )
        summary = run_diff(base, new).matching["entities"]["trip"]
        assert "most stops shared" not in summary["by_step"]

    def test_short_services_pair_by_their_dates(self, tmp_path: Path):
        """Two one-week services have no weekly pattern; their dates tell their
        08:00 trips apart."""
        calendar = (
            "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
            "start_date,end_date\n"
            "X1,1,1,1,1,1,0,0,20260601,20260605\n"
            "X2,1,1,1,1,1,0,0,20260608,20260612\n"
        )

        def feed(stops):
            files = _long_feed([("A", "08", stops), ("B", "08", stops)])
            files["calendar.txt"] = calendar
            files["trips.txt"] = (
                "route_id,service_id,trip_id,direction_id\nR1,X1,A,0\nR1,X2,B,0\n"
            )
            return files

        base, new = _zips(tmp_path, feed(TEN), feed([i for i in TEN if i != 5]))
        summary = run_diff(base, new).matching["entities"]["trip"]
        assert summary["by_step"]["most stops shared"] == 2

    def test_one_turns_it_off(self, tmp_path: Path):
        without_5 = [i for i in TEN if i != 5]
        _, summary = self._run(
            tmp_path,
            [("X1", "08", without_5), ("T2", "10", TEN)],
            trip_min_shared_stops=1,
        )
        assert "most stops shared" not in summary["by_step"]
        assert "shared_stops" not in summary


class TestShapeGeometry:
    def test_moved_shape_is_listed_with_points(self, tmp_path: Path):
        base, new = _zips(
            tmp_path,
            _feed(BASE_TRIPS),
            _feed(BASE_TRIPS, shape="SH2", shape_offset=0.001),
        )
        run = run_diff(base, new, shape_geometry=True)
        assert [
            (c.change, c.base_shape_id, c.new_shape_id) for c in run.shape_changes
        ] == [("changed", "SH1", "SH2")]
        change = run.shape_changes[0]
        assert 100 < change.deviation_m < 120
        assert len(change.base_coords) == len(change.new_coords) == 20
        # [lon, lat, shape_pt_sequence]
        assert [p[2] for p in change.base_coords] == list(range(20))

        counts = run.matching["entities"]["shape"]["changes"]
        assert counts["changed"] == 1

    @pytest.mark.parametrize(("threshold", "kind"), [(50, "redrawn"), (40, "changed")])
    def test_moved_means_the_shape_moved_highlight(self, tmp_path, threshold, kind):
        """About 45 m apart at the ends: moved only when the highlight says so."""
        base, new = _zips(
            tmp_path, _feed(BASE_TRIPS), _feed(BASE_TRIPS, shape_offset=0.0004)
        )
        run = run_diff(
            base, new, shape_geometry=True, highlight_shape_moved_m=threshold
        )
        (change,) = run.shape_changes
        assert change.change == kind
        assert 40 < change.deviation_m < 50
        moved = [h for h in run.diff.highlights.items if h.code == "shape_moved"]
        assert len(moved) == (kind == "changed")
        if moved:
            assert moved[0].details["distance_m"] == change.deviation_m

    def test_geometry_is_off_by_default(self, tmp_path: Path):
        base, new = _zips(tmp_path, _feed(BASE_TRIPS), _feed(BASE_TRIPS))
        assert run_diff(base, new).shape_changes is None

    def test_thin_keeps_ends(self):
        line = [[float(i), 0.0] for i in range(1000)]
        kept = thin(line, 300)
        assert len(kept) <= 300
        assert kept[0] == line[0] and kept[-1] == line[-1]


class TestHtmlReport:
    def test_offline_and_capped(self, tmp_path: Path):
        many = [(f"T{i}", f"{8 + i % 10:02d}") for i in range(40)]
        base, new = _zips(tmp_path, _feed([]), _feed(many))
        run = run_diff(base, new, shape_geometry=True)
        html = render_html(run, row_cap=5)

        # Nothing loaded from the network.
        assert not re.search(r"""(src|href)=["']?https?:""", html)
        assert "@import" not in html
        data = json.loads(
            re.search(
                r'<script id="data" type="application/json">(.*?)</script>', html, re.S
            )
            .group(1)
            .replace("<\\/", "</")
        )
        stop_times = next(f for f in data["files"] if f["name"] == "stop_times.txt")
        assert len(stop_times["added"]) == 5
        assert stop_times["stats"]["rows_added_count"] == 80
        assert data["caps"]["rows"] == 5

    def test_cap_per_kind_and_examples_per_column(self, tmp_path: Path):
        """Every trip gets a new headsign; only the last one also gets a block."""
        header = (
            "route_id,service_id,trip_id,direction_id,shape_id,trip_headsign,block_id"
        )
        many = [(f"T{i:02d}", f"{8 + i % 10:02d}:{i:02d}") for i in range(30)]
        base_files, new_files = _feed(many), _feed(many)
        for files, sign in ((base_files, "Old"), (new_files, "New")):
            rows = files["trips.txt"].splitlines()[1:]
            blocks = ["B2" if sign == "New" and "T29" in r else "B1" for r in rows]
            files["trips.txt"] = (
                header
                + "\n"
                + "".join(
                    f"{r},{sign},{b}\n" for r, b in zip(rows, blocks, strict=True)
                )
            )
        new_files["trips.txt"] += "R1,WK,T99,0,SH1,New,B1\n"
        base_files["trips.txt"] += "R1,WK,T98,0,SH1,Old,B1\n"
        base, new = _zips(tmp_path, base_files, new_files)
        html = render_html(diff_feeds(base, new), row_cap=5)
        data = json.loads(
            re.search(
                r'<script id="data" type="application/json">(.*?)</script>', html, re.S
            )
            .group(1)
            .replace("<\\/", "</")
        )
        trips = next(f for f in data["files"] if f["name"] == "trips.txt")
        # Each kind gets the full cap rather than a share of it.
        assert len(trips["added"]) == len(trips["deleted"]) == 1
        assert trips["modified_first"] == 5
        # The block_id change is only on the 30th row, past the first 5.
        assert any("block_id" in r["changes"] for r in trips["modified"])
        assert len(trips["modified"]) == 6

    def test_script_cannot_be_closed_by_data(self, tmp_path: Path):
        base_files = _feed(BASE_TRIPS)
        new_files = _feed(BASE_TRIPS)
        new_files["stops.txt"] = new_files["stops.txt"].replace(
            "First", "</script><b>x"
        )
        base, new = _zips(tmp_path, base_files, new_files)
        html = render_html(diff_feeds(base, new))
        assert "</script><b>x" not in html


class TestCli:
    @pytest.fixture(autouse=True)
    def _cwd(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)

    def test_html_next_to_output_by_default(self, tmp_path: Path):
        base, new = _zips(tmp_path, _feed(BASE_TRIPS), _feed([("X1", "08")]))
        out = tmp_path / "out" / "diff.json"
        out.parent.mkdir()
        result = CliRunner().invoke(main, [str(base), str(new), "-o", str(out)])
        assert result.exit_code == 0, result.output
        assert (out.parent / "diff.html").exists()
        assert "id_matching" not in json.loads(out.read_text())

    def test_missing_output_folder_is_created(self, tmp_path: Path):
        base, new = _zips(tmp_path, _feed(BASE_TRIPS), _feed(BASE_TRIPS))
        out = tmp_path / "not" / "there" / "diff.json"
        result = CliRunner().invoke(main, [str(base), str(new), "-o", str(out)])
        assert result.exit_code == 0, result.output
        assert out.exists() and out.with_suffix(".html").exists()

    def test_no_html(self, tmp_path: Path):
        base, new = _zips(tmp_path, _feed(BASE_TRIPS), _feed([("X1", "08")]))
        out = tmp_path / "diff.json"
        result = CliRunner().invoke(
            main, [str(base), str(new), "-o", str(out), "--no-html"]
        )
        assert result.exit_code == 0, result.output
        assert not (tmp_path / "diff.html").exists()

    def test_shape_geometry_draws_in_html(self, tmp_path: Path):
        base, new = _zips(
            tmp_path, _feed(BASE_TRIPS), _feed(BASE_TRIPS, shape_offset=0.001)
        )
        out = tmp_path / "diff.json"
        result = CliRunner().invoke(
            main, [str(base), str(new), "-o", str(out), "--shape-geometry"]
        )
        assert result.exit_code == 0, result.output
        assert list(tmp_path.glob("*.geojson")) == []
        assert '"change":"changed"' in (tmp_path / "diff.html").read_text()
        result = CliRunner().invoke(
            main, [str(base), str(new), "--shape-geometry", "--no-html"]
        )
        assert result.exit_code == 1

    def test_stdout_writes_default_html(self, tmp_path: Path):
        base, new = _zips(tmp_path, _feed(BASE_TRIPS), _feed(BASE_TRIPS))
        result = CliRunner().invoke(main, [str(base), str(new), "--html", "r.html"])
        assert result.exit_code == 0, result.output
        assert (tmp_path / "r.html").exists()

    def test_shape_geometry_needs_matching(self, tmp_path: Path):
        base, new = _zips(tmp_path, _feed(BASE_TRIPS), _feed(BASE_TRIPS))
        result = CliRunner().invoke(
            main, [str(base), str(new), "--shape-geometry", "--no-id-matching"]
        )
        assert result.exit_code == 1


class TestCappedModifiedRows:
    def test_columns_take_turns(self):
        rows = [(1, {"a"}), (2, {"a"}), (3, {"a"}), (4, {"a", "b"}), (5, {"c"})]
        picked = _select_modified(
            rows,
            3,
            line_of=lambda r: r[0],
            fields_of=lambda r: r[1],
            column_order=["a", "b", "c"],
        )
        assert [r[0] for r in picked] == [1, 4, 5]
        assert _select_modified(rows, None, lambda r: r[0], lambda r: r[1], []) == rows

    @pytest.mark.parametrize("threshold", [None, 0])
    def test_rare_column_kept_under_cap(self, tmp_path: Path, threshold):
        """30 trips change headsign; only the last also changes block_id."""
        header = (
            "route_id,service_id,trip_id,direction_id,shape_id,trip_headsign,block_id"
        )
        many = [(f"T{i:02d}", f"{8 + i % 10:02d}:{i:02d}") for i in range(30)]
        base_files, new_files = _feed(many), _feed(many)
        for files, sign in ((base_files, "Old"), (new_files, "New")):
            rows = files["trips.txt"].splitlines()[1:]
            blocks = ["B2" if sign == "New" and "T29" in r else "B1" for r in rows]
            files["trips.txt"] = (
                header
                + "\n"
                + "".join(
                    f"{r},{sign},{b}\n" for r, b in zip(rows, blocks, strict=True)
                )
            )
        base, new = _zips(tmp_path, base_files, new_files)
        diff = diff_feeds(
            base, new, row_changes_cap_per_file=5, large_file_threshold_bytes=threshold
        )
        modified = _file(diff, "trips.txt").row_changes.modified
        assert [r.identifier["trip_id"] for r in modified] == [
            "T00",
            "T01",
            "T02",
            "T03",
            "T29",
        ]


class TestRunRecord:
    def test_unchanged_files_and_parameters(self, tmp_path: Path):
        new_files = _feed(BASE_TRIPS)
        new_files["stops.txt"] = new_files["stops.txt"].replace("Second", "Renamed")
        base, new = _zips(tmp_path, _feed(BASE_TRIPS), new_files)
        run = run_diff(base, new, row_changes_cap_per_file=200)
        assert [f.file_name for f in run.diff.file_diffs] == ["stops.txt"]
        unchanged = {f["file_name"]: f["rows"] for f in run.files_unchanged}
        assert unchanged["stop_times.txt"] == 4
        assert "stops.txt" not in unchanged
        # Kept out of the JSON, which follows the GTFS Diff v2 schema.
        dumped = run.diff.model_dump()
        assert "files_unchanged" not in dumped["summary"]
        assert "parameters" not in dumped["metadata"]
        params = run.parameters
        assert params["row_changes_cap_per_file"] == 200
        assert params["trip_max_shift_minutes"] == 15
        assert params["include_key_changes"] is False

    def test_duration_is_in_the_report_not_the_json(self, tmp_path: Path):
        run = run_diff(*_zips(tmp_path, _feed(BASE_TRIPS), _feed(BASE_TRIPS)))
        assert run.duration_seconds > 0
        assert "duration_seconds" not in run.diff.model_dump()["metadata"]
        html = render_html(run)
        data = json.loads(
            re.search(
                r'<script id="data" type="application/json">(.*?)</script>', html, re.S
            ).group(1)
        )
        assert data["metadata"]["duration_seconds"] == run.duration_seconds

    def test_settings_come_from_the_cli(self, tmp_path: Path):
        """The page's settings use the CLI's own flags and --help text."""
        import click

        from gtfs_diff.cli import MIN_CAP

        options = {
            o.opts[0]: o.help for o in main.params if isinstance(o, click.Option)
        }
        run = run_diff(*_zips(tmp_path, _feed(BASE_TRIPS), _feed(BASE_TRIPS)))
        data = json.loads(
            re.search(
                r'<script id="data" type="application/json">(.*?)</script>',
                render_html(run),
                re.S,
            ).group(1)
        )
        assert data["settings"]
        for flag, _value, text in data["settings"]:
            assert options[flag] == text
        assert data["constants"]["min_cap"] == MIN_CAP


class TestFeedInfoKey:
    HEADER = "feed_publisher_name,feed_publisher_url,feed_lang,feed_version\n"

    def test_new_version_is_a_modified_row(self, tmp_path: Path):
        base, new = _zips(
            tmp_path,
            {"feed_info.txt": self.HEADER + "STM,http://stm.info,fr,2026-10\n"},
            {"feed_info.txt": self.HEADER + "STM,http://stm.info,fr,2026-11\n"},
        )
        fd = _file(diff_feeds(base, new), "feed_info.txt")
        assert fd.stats.rows_added_count == fd.stats.rows_deleted_count == 0
        (row,) = fd.row_changes.modified
        assert [(c.field, c.base_value, c.new_value) for c in row.field_changes] == [
            ("feed_version", "2026-10", "2026-11")
        ]

    def test_missing_publisher_url_is_not_compared(self, tmp_path: Path):
        header = "feed_publisher_name,feed_lang,feed_version\n"
        base, new = _zips(
            tmp_path,
            {"feed_info.txt": header + "STM,fr,2026-10\n"},
            {"feed_info.txt": header + "STM,fr,2026-11\n"},
        )
        fd = _file(diff_feeds(base, new), "feed_info.txt")
        assert fd.file_action == "not_compared"
        assert fd.not_compared_reason.code == "missing_primary_key"


class TestDrawnShapePoints:
    def test_pack_is_deltas_of_each_value(self):
        from gtfs_diff.report.shape_points import pack

        points = [
            [-73.5, 45.5, 1.0, 0, 0.0, 1],
            [-73.499988, 45.500004, 2.0, 1, 12.3, 0],
            [-73.4999, 45.5, 3.5, 0, None, 0],
        ]
        assert pack(points) == [
            *[-73500000, 45500000, 1, 2, 0],
            *[12, 4, 1, 1, 123],
            *[88, -4, 1.5, 0, -1],
        ]

    def test_most_moved_points_are_kept(self):
        from gtfs_diff.report.shape_points import HTML_SHAPE_POINTS, _drawn_points

        base = [[-73.5 + i * 1e-5, 45.5, i] for i in range(1000)]
        new = [list(p) for p in base]
        new[777] = [new[777][0], 45.5018, 777]  # about 200 m north
        drawn = _drawn_points(new, base)
        assert len(drawn) <= HTML_SHAPE_POINTS
        far = [p for p in drawn if p[2] == 777]
        assert far and far[0][3] == 1 and 190 < far[0][4] < 210
        assert drawn[0][2] == 0 and drawn[-1][2] == 999
        # The old line keeps its own point 777, which is not in the new shape.
        assert any(p[2] == 777 and p[3] == 1 for p in _drawn_points(base, new))
        # Only the stretches touching the moved point are marked as different.
        different = [p[2] for p in drawn[:-1] if p[5] == 0]
        assert 777 in different and len(different) == 2
        assert drawn[-1][5] == 0  # nothing after the last point


class TestNotableHighlights:
    def _highlights(self, tmp_path: Path, base_files, new_files, **kwargs):
        base, new = _zips(tmp_path, base_files, new_files)
        kwargs.setdefault("as_of", date(2026, 1, 1))
        found = run_diff(
            base, new, row_changes_cap_per_file=0, **kwargs
        ).diff.highlights
        return [h.model_dump(exclude_none=True) for h in found.items]

    def _of(self, highlights, kind):
        return [h for h in highlights if h["code"] == kind]

    def _id(self, h, feed):
        (r,) = [r for r in h["references"] if r["feed"] == feed]
        return next(iter(r["identifier"].values()))

    def test_day_that_lost_its_trips(self, tmp_path: Path):
        new_files = _feed(BASE_TRIPS)
        new_files["calendar_dates.txt"] = (
            "service_id,date,exception_type\nWK,20260610,2\nWK,20260611,2\n"
        )
        found = self._highlights(tmp_path, _feed(BASE_TRIPS), new_files)
        (h,) = self._of(found, "service_day_without_trips")
        d = h["details"]
        assert (d["date_from"], d["date_to"], d["days"]) == (
            "2026-06-10",
            "2026-06-11",
            2,
        )
        assert d["base_trips_max"] == 2
        assert "references" not in h
        assert "Wed Jun 10 to Thu Jun 11, 2026" in h["message"]
        # Days before --as-of are not reported.
        later = self._highlights(
            tmp_path, _feed(BASE_TRIPS), new_files, as_of=date(2026, 7, 1)
        )
        assert not self._of(later, "service_day_without_trips")

    def test_days_outside_the_new_feed_are_not_lost(self, tmp_path: Path):
        """The new feed starts on Jun 15: Jun 10-11 are not part of it."""
        exceptions = "service_id,date,exception_type\nWK,20260610,2\nWK,20260611,2\n"
        info = (
            "feed_publisher_name,feed_publisher_url,feed_lang,"
            "feed_start_date,feed_end_date\n"
        )
        new_files = _feed(BASE_TRIPS)
        new_files["calendar_dates.txt"] = exceptions
        new_files["feed_info.txt"] = info + "A,http://a,en,20260615,20261231\n"
        found = self._highlights(tmp_path, _feed(BASE_TRIPS), new_files)
        assert not self._of(found, "service_day_without_trips")
        # Without feed_info.txt, the period starts with the new calendar.
        del new_files["feed_info.txt"]
        new_files["calendar.txt"] = new_files["calendar.txt"].replace(
            "20260101", "20260615"
        )
        found = self._highlights(tmp_path, _feed(BASE_TRIPS), new_files)
        assert not self._of(found, "service_day_without_trips")

    def test_service_periods_that_do_not_overlap(self, tmp_path: Path):
        def runs(start, end):
            files = _feed(BASE_TRIPS)
            files["calendar.txt"] = files["calendar.txt"].replace(
                "20260101,20261231", f"{start},{end}"
            )
            return files

        # The old feed runs to Thu Dec 31, 2026; the new one from Fri Jan 15.
        found = self._highlights(
            tmp_path, _feed(BASE_TRIPS), runs("20270115", "20271231")
        )
        (h,) = self._of(found, "service_periods_do_not_overlap")
        assert h["details"] == {
            "base_first": "2026-01-01",
            "base_last": "2026-12-31",
            "new_first": "2027-01-15",
            "new_last": "2027-12-31",
            "gap_days": 14,
        }
        assert "with 14 days without service between them." in h["message"]
        # The new feed ends before the old one starts.
        earlier = self._highlights(
            tmp_path, _feed(BASE_TRIPS), runs("20250101", "20251231")
        )
        (h,) = self._of(earlier, "service_periods_do_not_overlap")
        assert h["message"].endswith("The new feed ends before the old one starts.")
        # One day in common is enough.
        touching = self._highlights(
            tmp_path, _feed(BASE_TRIPS), runs("20261231", "20271231")
        )
        assert not self._of(touching, "service_periods_do_not_overlap")

    def test_removed_stop(self, tmp_path: Path):
        base_files = _feed(BASE_TRIPS)
        base_files["stops.txt"] += "S3,Third,45.60000,-73.60000\n"
        base_files["stop_times.txt"] += "T1,S3,3,08:20:00,08:20:00\n"
        new_files = _feed(BASE_TRIPS)
        found = self._highlights(tmp_path, base_files, new_files)
        (h,) = self._of(found, "stop_removed")
        assert h["references"] == [
            {"file_name": "stops.txt", "feed": "base", "identifier": {"stop_id": "S3"}}
        ]
        assert h["details"]["base_trips"] == 1
        # A new stop 60 m away replaces it.
        new_files["stops.txt"] += "N3,Third street,45.60054,-73.60000\n"
        found = self._highlights(tmp_path, base_files, new_files)
        assert not self._of(found, "stop_removed")

    def test_moved_stop(self, tmp_path: Path):
        def moved(lat):
            files = _feed(BASE_TRIPS)
            files["stops.txt"] = files["stops.txt"].replace("45.51000", lat)
            return files

        far = self._highlights(tmp_path, _feed(BASE_TRIPS), moved("45.51072"))
        (h,) = self._of(far, "stop_moved")
        assert self._id(h, "base") == self._id(h, "new") == "S2"
        assert 75 < h["details"]["distance_m"] < 85
        near = self._highlights(tmp_path, _feed(BASE_TRIPS), moved("45.51027"))
        assert not self._of(near, "stop_moved")
        stricter = self._highlights(
            tmp_path, _feed(BASE_TRIPS), moved("45.51027"), highlight_stop_moved_m=20
        )
        assert len(self._of(stricter, "stop_moved")) == 1

    def test_moved_shape(self, tmp_path: Path):
        far = self._highlights(
            tmp_path, _feed(BASE_TRIPS), _feed(BASE_TRIPS, shape_offset=0.001)
        )
        (h,) = self._of(far, "shape_moved")
        assert self._id(h, "base") == self._id(h, "new") == "SH1"
        assert 100 < h["details"]["distance_m"] < 120
        near = self._highlights(
            tmp_path, _feed(BASE_TRIPS), _feed(BASE_TRIPS, shape_offset=0.0002)
        )
        assert not self._of(near, "shape_moved")

    def test_files_and_switches(self, tmp_path: Path):
        base, new = _zips(
            tmp_path, _feed(BASE_TRIPS), _feed(BASE_TRIPS, shape_offset=0.001)
        )
        out = tmp_path / "diff.json"
        result = CliRunner().invoke(
            main, [str(base), str(new), "-o", str(out), "--as-of", "2026-01-01"]
        )
        assert result.exit_code == 0, result.output
        assert not (tmp_path / "diff.highlights.json").exists()
        section = json.loads(out.read_text())["highlights"]
        assert section["cap_per_code"] == 100
        assert section["parameters"] == {
            "as_of": "2026-01-01",
            "stop_moved_m": 50.0,
            "shape_moved_m": 50.0,
        }
        assert section["counts"] == {"shape_moved": 1}
        assert [h["code"] for h in section["items"]] == ["shape_moved"]
        off = run_diff(base, new, id_matching=False).diff
        assert off.highlights is None
        assert "highlights" not in off.model_dump(exclude_none=True)

    def test_report_from_the_json_alone(self, tmp_path: Path):
        base, new = _zips(
            tmp_path, _feed(BASE_TRIPS), _feed(BASE_TRIPS, shape_offset=0.001)
        )
        text = run_diff(base, new, as_of=date(2026, 1, 1)).diff.model_dump_json()
        html = render_html(GtfsDiff.model_validate_json(text))
        data = json.loads(
            re.search(
                r'<script id="data" type="application/json">(.*?)</script>', html, re.S
            ).group(1)
        )
        assert [h["code"] for h in data["highlights"]] == ["shape_moved"]
        assert data["highlight_counts"] == {"shape_moved": 1}

    @pytest.mark.parametrize("threshold", [None, 0])
    def test_highlighted_row_survives_the_cap(self, tmp_path: Path, threshold):
        """30 stops are replaced by new ones nearby (deleted, not highlighted);
        stop X, last in the file, is removed outright (highlighted)."""
        base_files, new_files = _feed(BASE_TRIPS), _feed(BASE_TRIPS)
        for i in range(30):
            lat = 45.6 + i * 0.01
            base_files["stops.txt"] += f"E{i},Old {i},{lat:.5f},-73.60000\n"
            new_files["stops.txt"] += f"R{i},New {i},{lat + 0.0003:.5f},-73.60000\n"
        base_files["stops.txt"] += "X,Gone,46.50000,-73.60000\n"
        base, new = _zips(tmp_path, base_files, new_files)
        run = run_diff(
            base,
            new,
            row_changes_cap_per_file=4,
            large_file_threshold_bytes=threshold,
            as_of=date(2026, 1, 1),
        )
        removed = [h for h in run.diff.highlights.items if h.code == "stop_removed"]
        assert [h.references[0].identifier for h in removed] == [{"stop_id": "X"}]
        stops = _file(run.diff, "stops.txt")
        assert stops.stats.rows_deleted_count == 31
        deleted = [r.identifier["stop_id"] for r in stops.row_changes.deleted]
        assert "X" in deleted and len(deleted) == 2

    def test_caps_and_counts(self):
        from gtfs_diff.highlights import capped, counts

        many = [{"code": "shape_moved", "n": i} for i in range(12)]
        many += [{"code": "stop_moved", "n": 0}]
        assert counts(many) == {"shape_moved": 12, "stop_moved": 1}
        kept = capped(many, 5)
        assert [h["code"] for h in kept].count("shape_moved") == 5
        assert kept[-1]["code"] == "stop_moved"
        assert capped(many, None) == many
