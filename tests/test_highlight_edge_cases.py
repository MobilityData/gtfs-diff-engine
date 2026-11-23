"""Notable highlights and the HTML report on unusual feeds."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from gtfs_diff.engine import run_diff
from gtfs_diff.report.html import render_html
from tests.test_matching import BASE_TRIPS, _feed, _zips

DATA_OPEN = '<script id="data" type="application/json">'


def _run(tmp_path: Path, base: dict, new: dict, **kwargs):
    kwargs.setdefault("as_of", date(2026, 1, 1))
    return run_diff(*_zips(tmp_path, base, new), **kwargs)


def _codes(run, code: str) -> list:
    return [h for h in run.diff.highlights.items if h.code == code]


def _payload(html: str) -> dict:
    start = html.index(DATA_OPEN) + len(DATA_OPEN)
    return json.loads(html[start : html.index("</script>", start)])


def _calendar_dates(days: list[str]) -> str:
    return "service_id,date,exception_type\n" + "".join(f"WK,{d},1\n" for d in days)


class TestReportPayload:
    def test_feed_text_cannot_open_a_comment_in_the_page(self, tmp_path: Path):
        base = _feed(BASE_TRIPS)
        base["stops.txt"] += 'S3,"<!--<script>x",45.60000,-73.60000\n'
        html = render_html(_run(tmp_path, base, _feed(BASE_TRIPS)))
        start = html.index(DATA_OPEN) + len(DATA_OPEN)
        assert "<" not in html[start : html.index("</script>", start)]
        (removed,) = _payload(html)["highlights"]
        assert removed["details"]["stop_name"] == "<!--<script>x"

    def test_only_the_checks_that_ran_are_claimed(self, tmp_path: Path):
        new = _feed(BASE_TRIPS)
        del new["stops.txt"]
        run = _run(tmp_path, _feed(BASE_TRIPS), new)
        assert "stops" not in run.highlight_checks
        assert "service" in run.highlight_checks
        assert "stops" not in _payload(render_html(run))["highlight_checks"]


class TestCalendarSwitch:
    def test_day_lost_after_moving_to_calendar_dates(self, tmp_path: Path):
        new = _feed(BASE_TRIPS)
        del new["calendar.txt"]
        new["calendar_dates.txt"] = _calendar_dates(["20260609", "20260611"])
        run = _run(tmp_path, _feed(BASE_TRIPS), new)
        lost = _codes(run, "service_day_without_trips")
        assert any(h.details["date_from"] == "2026-06-10" for h in lost)

    def test_periods_apart_after_moving_to_calendar_dates(self, tmp_path: Path):
        new = _feed(BASE_TRIPS)
        del new["calendar.txt"]
        new["calendar_dates.txt"] = _calendar_dates(["20270601", "20270602"])
        run = _run(tmp_path, _feed(BASE_TRIPS), new)
        assert _codes(run, "service_periods_do_not_overlap")


class TestFares:
    def test_change_of_exactly_the_threshold(self, tmp_path: Path):
        header = "fare_id,price,currency_type,payment_method,transfers\n"
        base, new = _feed(BASE_TRIPS), _feed(BASE_TRIPS)
        base["fare_attributes.txt"] = header + "F1,1.10,USD,0,0\nF2,2.20,USD,0,0\n"
        new["fare_attributes.txt"] = header + "F1,1.65,USD,0,0\nF2,3.30,USD,0,0\n"
        run = _run(tmp_path, base, new)
        assert sorted(h.details["fare_id"] for h in _codes(run, "fare_changed")) == [
            "F1",
            "F2",
        ]

    def test_product_without_optional_keys_stays_in_the_diff(self, tmp_path: Path):
        header = "fare_product_id,fare_product_name,amount,currency\n"
        base, new = _feed(BASE_TRIPS), _feed(BASE_TRIPS)
        base["fare_products.txt"] = header + "".join(
            f"P{i:02d},Fare {i},{i + 1}.00,USD\n" for i in range(20)
        )
        new["fare_products.txt"] = (
            header
            + "".join(f"P{i:02d},Fare {i},{i + 1}.01,USD\n" for i in range(19))
            + "P19,Fare 19,99.00,USD\n"
        )
        run = _run(tmp_path, base, new, row_changes_cap_per_file=1)
        (fare,) = _codes(run, "fare_changed")
        key = {"fare_product_id": "P19", "rider_category_id": "", "fare_media_id": ""}
        assert fare.references[0].identifier == key
        fd = next(f for f in run.diff.file_diffs if f.file_name == "fare_products.txt")
        assert key in [r.identifier for r in fd.row_changes.modified]
