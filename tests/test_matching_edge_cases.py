"""ID matching on feeds that kept their IDs, or with unusual stops and calendars."""

from __future__ import annotations

from pathlib import Path

import pytest

from gtfs_diff.engine import run_diff
from tests.helpers import write_zip

AGENCY = "agency_id,agency_name,agency_url,agency_timezone\nA,Agency,http://a,UTC\n"
ROUTES = "route_id,agency_id,route_short_name,route_type\nR1,A,10,3\n"
CAL = (
    "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
    "start_date,end_date\n"
)
WEEKDAYS = CAL + "WK,1,1,1,1,1,0,0,20260101,20261231\n"
TWO_STOPS = (
    "stop_id,stop_name,stop_lat,stop_lon\n"
    "S1,First,45.50000,-73.50000\nS2,Second,45.51000,-73.51000\n"
)
STOP_TIMES = "trip_id,stop_id,stop_sequence,arrival_time,departure_time\n"


def _clock(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}:00"


def _feed(trips: str, stop_times: str, **files: str) -> dict[str, str]:
    feed = {
        "agency.txt": AGENCY,
        "routes.txt": ROUTES,
        "calendar.txt": WEEKDAYS,
        "stops.txt": TWO_STOPS,
        "trips.txt": trips,
        "stop_times.txt": STOP_TIMES + stop_times,
    }
    feed.update(files)
    return {name: text for name, text in feed.items() if text is not None}


def _run(tmp_path: Path, base: dict, new: dict, **kwargs):
    return run_diff(
        write_zip(tmp_path / "base.zip", base),
        write_zip(tmp_path / "new.zip", new),
        **kwargs,
    )


def _counts(run, name: str):
    """(added, deleted, modified) rows of *name*, or None when unchanged."""
    fd = next((f for f in run.diff.file_diffs if f.file_name == name), None)
    if fd is None:
        return None
    s = fd.stats
    return s.rows_added_count, s.rows_deleted_count, s.rows_modified_count


def _frequent(second_stop_plus: int) -> dict[str, str]:
    """Six trips ten minutes apart; the second stop *second_stop_plus* minutes
    after the usual ten."""
    trips = [f"T{i}" for i in range(6)]
    times = "".join(
        f"{t},S1,1,{_clock(480 + 10 * i)},{_clock(480 + 10 * i)}\n"
        f"{t},S2,2,{_clock(490 + 10 * i + second_stop_plus)},"
        f"{_clock(490 + 10 * i + second_stop_plus)}\n"
        for i, t in enumerate(trips)
    )
    return _feed(
        "route_id,service_id,trip_id\n" + "".join(f"R1,WK,{t}\n" for t in trips),
        times,
    )


class TestStableTripIds:
    def test_retimed_trips_among_others_stay_paired(self, tmp_path: Path):
        """The time shift step declines (trips 10 min apart); the IDs decide."""
        run = _run(tmp_path, _frequent(0), _frequent(2))
        assert _counts(run, "trips.txt") is None
        assert _counts(run, "stop_times.txt") == (0, 0, 6)
        assert run.matching["entities"]["trip"]["by_step"]["same id"] == 6

    def test_trip_that_gained_a_stop_stays_paired(self, tmp_path: Path):
        stops = "stop_id,stop_name,stop_lat,stop_lon\n" + "".join(
            f"S{i},Stop {i},45.5{i},-73.5{i}\n" for i in range(6)
        )

        def line(n: int) -> dict:
            times = "".join(
                f"T1,S{k},{k},{_clock(480 + k)},{_clock(480 + k)}\n" for k in range(n)
            )
            return _feed(
                "route_id,service_id,trip_id\nR1,WK,T1\n", times, **{"stops.txt": stops}
            )

        run = _run(tmp_path, line(5), line(6))
        assert _counts(run, "trips.txt") is None
        assert _counts(run, "stop_times.txt") == (1, 0, 0)

    def test_not_when_the_feed_regenerates_ids(self, tmp_path: Path):
        """Most trips matched under new IDs: a shared ID is a coincidence."""
        base_trips = "route_id,service_id,trip_id\n" + "".join(
            f"R1,WK,{t}\n" for t in ("A", "B", "C", "X")
        )
        new_trips = "route_id,service_id,trip_id\n" + "".join(
            f"R1,WK,{t}\n" for t in ("D", "E", "F", "X")
        )

        def times(ids, x_to):
            """X runs at 15:00 to *x_to*, so only its ID could pair it."""
            return "".join(
                f"{t},S1,1,{_clock(m)},{_clock(m)}\n"
                f"{t},{x_to if t == 'X' else 'S2'},2,"
                f"{_clock(m + 10)},{_clock(m + 10)}\n"
                for t, m in zip(ids, [480, 540, 600, 900], strict=True)
            )

        stops = TWO_STOPS + "S3,Third,45.52000,-73.52000\n"
        trip = run_diff(
            write_zip(tmp_path / "b.zip", _feed(base_trips, times("ABCX", "S2"))),
            write_zip(
                tmp_path / "n.zip",
                _feed(new_trips, times("DEFX", "S3"), **{"stops.txt": stops}),
            ),
        ).matching["entities"]["trip"]
        assert trip["matched_new_id"] == 3
        assert "same id" not in trip["by_step"]


class TestStopKinds:
    @staticmethod
    def _station(station: str, platform: str) -> dict:
        stops = (
            "stop_id,stop_name,stop_lat,stop_lon,location_type,parent_station\n"
            f"{station},Central,45.50000,-73.50000,1,\n"
            f"{platform},Central,45.50000,-73.50000,0,{station}\n"
            "S2,Other,45.51000,-73.51000,0,\n"
        )
        return _feed(
            "route_id,service_id,trip_id\nR1,WK,T1\n",
            f"T1,{platform},1,08:00:00,08:00:00\nT1,S2,2,08:10:00,08:10:00\n",
            **{"stops.txt": stops},
        )

    def test_platform_with_a_new_id_does_not_pair_with_its_station(self, tmp_path):
        run = _run(tmp_path, self._station("PS", "S1"), self._station("PS", "S9"))
        assert run.matching["entities"]["stop"]["matched"] == 3
        assert _counts(run, "trips.txt") is None

    def test_regenerated_station_and_platform(self, tmp_path):
        run = _run(tmp_path, self._station("PS", "S1"), self._station("B1", "A1"))
        assert run.diff.file_diffs == []


class TestServiceDays:
    @staticmethod
    def _season(wk: str, we: str, t_wk: str, t_we: str, start: str, end: str):
        calendar = (
            CAL
            + f"{wk},1,1,1,1,1,0,0,{start},{end}\n{we},0,0,0,0,0,1,1,{start},{end}\n"
        )
        return _feed(
            f"route_id,service_id,trip_id\nR1,{wk},{t_wk}\nR1,{we},{t_we}\n",
            "".join(
                f"{t},S1,1,08:00:00,08:00:00\n{t},S2,2,08:10:00,08:10:00\n"
                for t in (t_wk, t_we)
            ),
            **{"calendar.txt": calendar},
        )

    def test_next_season_pairs_weekday_with_weekday(self, tmp_path: Path):
        """No dates in common: each service is told apart by its weekdays."""
        run = _run(
            tmp_path,
            self._season("WK", "WE", "TW", "TE", "20260101", "20260331"),
            self._season("SVC_WK", "SVC_WE", "A1", "B1", "20260401", "20260630"),
        )
        cal = next(f for f in run.diff.file_diffs if f.file_name == "calendar.txt")
        changed = {c.field for r in cal.row_changes.modified for c in r.field_changes}
        assert changed == {"start_date", "end_date"}
        assert _counts(run, "trips.txt") is None

    def test_identical_trips_pair_by_headsign(self, tmp_path: Path):
        def feed(trips):
            return _feed(
                "route_id,service_id,trip_id,trip_headsign\n"
                + "".join(f"R1,WK,{t},{h}\n" for t, h in trips),
                "".join(
                    f"{t},S1,1,08:00:00,08:00:00\n{t},S2,2,08:10:00,08:10:00\n"
                    for t, _ in trips
                ),
            )

        run = _run(
            tmp_path,
            feed([("TA", "North"), ("TB", "South")]),
            feed([("X2", "North"), ("X1", "South")]),
        )
        assert _counts(run, "trips.txt") is None


class TestMatchingStaysOn:
    def test_without_stops_file_trips_compare_stops_by_id(self, tmp_path: Path):
        feed = _frequent(0)
        del feed["stops.txt"]
        run = _run(tmp_path, feed, feed)
        assert run.diff.file_diffs == []
        assert run.matching["entities"]["trip"]["matched"] == 6

    def test_spill_folder_with_a_quote(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("GTFS_DIFF_DUCKDB_TMPDIR", str(tmp_path / "o'brien"))
        run = _run(tmp_path, _frequent(0), _frequent(2))
        assert "error" not in run.matching
        assert _counts(run, "trips.txt") is None

    @pytest.mark.parametrize("lat", ["NaN", "inf", "1e30"])
    def test_bad_coordinate(self, tmp_path: Path, lat: str):
        stops = TWO_STOPS + f"S3,Broken,{lat},-73.52000\n"
        feed = _feed(
            "route_id,service_id,trip_id\nR1,WK,T1\n",
            "T1,S1,1,08:00:00,08:00:00\nT1,S2,2,08:10:00,08:10:00\n",
            **{"stops.txt": stops},
        )
        run = _run(tmp_path, feed, feed)
        assert "error" not in run.matching
        assert run.diff.highlights is not None
