"""Tests for gtfs_diff.engine — diff_feeds()."""

from __future__ import annotations

import io
import json
import os
import urllib.error
from pathlib import Path

import pytest

from gtfs_diff import engine_duckdb
from gtfs_diff.diff_helpers import _split_row_changes_cap
from gtfs_diff.engine import (
    FeedFileMeta,
    MissingPrimaryKeyError,
    _eligible_for_duckdb,
    _http_exists,
    _http_exists_via_get,
    _is_url,
    _join_url,
    _materialized_path,
    _maybe_diff_modified_duckdb,
    _open_remote_feed,
    _read_csv_index,
    diff_feeds,
)
from gtfs_diff.engine_duckdb import DUCKDB_TMPDIR_ENV, _resolve_spill_base
from gtfs_diff.gtfs_definitions import (
    get_foreign_keys,
    get_optional_primary_key_columns,
    get_primary_key,
)
from gtfs_diff.models import GtfsDiff
from tests.helpers import write_zip

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_file_diff(result: GtfsDiff, file_name: str):
    """Return the FileDiff for a given file name, or raise."""
    for fd in result.file_diffs:
        if fd.file_name == file_name:
            return fd
    raise KeyError(f"{file_name!r} not found in file_diffs")


def _get_file_summary(result: GtfsDiff, file_name: str):
    for fs in result.summary.files:
        if fs.file_name == file_name:
            return fs
    raise KeyError(f"{file_name!r} not found in summary.files")


STOPS_HEADER = "stop_id,stop_name,stop_lat,stop_lon\n"


# ---------------------------------------------------------------------------
# File-level tests
# ---------------------------------------------------------------------------


class TestFileAdded:
    def test_file_added(self, tmp_path: Path):
        base = write_zip(
            tmp_path / "base.zip",
            {
                "stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\n",
            },
        )
        new = write_zip(
            tmp_path / "new.zip",
            {
                "stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\n",
                "routes.txt": "route_id,route_short_name\nR1,Route 1\n",
            },
        )
        result = diff_feeds(base, new)
        fd = _get_file_diff(result, "routes.txt")
        assert fd.file_action == "added"
        assert len(fd.columns_added) == 2
        column_names = [c.name for c in fd.columns_added]
        assert "route_id" in column_names
        assert "route_short_name" in column_names

    def test_file_added_summary_status(self, tmp_path: Path):
        base = write_zip(
            tmp_path / "base.zip",
            {
                "stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\n",
            },
        )
        new = write_zip(
            tmp_path / "new.zip",
            {
                "stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\n",
                "routes.txt": "route_id,route_short_name\nR1,Route 1\n",
            },
        )
        result = diff_feeds(base, new)
        fs = _get_file_summary(result, "routes.txt")
        assert fs.status == "added"
        assert result.summary.files_added_count == 1


class TestFileDeleted:
    def test_file_deleted(self, tmp_path: Path):
        base = write_zip(
            tmp_path / "base.zip",
            {
                "stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\n",
                "routes.txt": "route_id,route_short_name\nR1,Route 1\n",
            },
        )
        new = write_zip(
            tmp_path / "new.zip",
            {
                "stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\n",
            },
        )
        result = diff_feeds(base, new)
        fd = _get_file_diff(result, "routes.txt")
        assert fd.file_action == "deleted"
        assert len(fd.columns_deleted) == 2
        column_names = [c.name for c in fd.columns_deleted]
        assert "route_id" in column_names

    def test_file_deleted_summary_status(self, tmp_path: Path):
        base = write_zip(
            tmp_path / "base.zip",
            {
                "stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\n",
                "routes.txt": "route_id,route_short_name\nR1,Route 1\n",
            },
        )
        new = write_zip(
            tmp_path / "new.zip",
            {
                "stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\n",
            },
        )
        result = diff_feeds(base, new)
        fs = _get_file_summary(result, "routes.txt")
        assert fs.status == "deleted"
        assert result.summary.files_deleted_count == 1


class TestIdenticalFeeds:
    def test_identical_feeds(self, tmp_path: Path):
        content = STOPS_HEADER + "S1,Stop One,1.0,2.0\nS2,Stop Two,3.0,4.0\n"
        base = write_zip(tmp_path / "base.zip", {"stops.txt": content})
        new = write_zip(tmp_path / "new.zip", {"stops.txt": content})
        result = diff_feeds(base, new)
        assert result.file_diffs == []
        assert result.summary.files_modified_count == 0
        assert result.summary.total_changes == 0


# ---------------------------------------------------------------------------
# Row-level tests
# ---------------------------------------------------------------------------


class TestRowsAdded:
    def test_rows_added_count(self, tmp_path: Path):
        base = write_zip(
            tmp_path / "base.zip",
            {
                "stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\n",
            },
        )
        new = write_zip(
            tmp_path / "new.zip",
            {
                "stops.txt": STOPS_HEADER
                + "S1,Stop One,1.0,2.0\nS2,Stop Two,3.0,4.0\n",
            },
        )
        result = diff_feeds(base, new)
        fd = _get_file_diff(result, "stops.txt")
        assert fd.stats.rows_added_count == 1

    def test_rows_added_identifier(self, tmp_path: Path):
        base = write_zip(
            tmp_path / "base.zip",
            {
                "stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\n",
            },
        )
        new = write_zip(
            tmp_path / "new.zip",
            {
                "stops.txt": STOPS_HEADER
                + "S1,Stop One,1.0,2.0\nS2,Stop Two,3.0,4.0\n",
            },
        )
        result = diff_feeds(base, new)
        fd = _get_file_diff(result, "stops.txt")
        assert len(fd.row_changes.added) == 1
        assert fd.row_changes.added[0].identifier == {"stop_id": "S2"}


class TestRowsDeleted:
    def test_rows_deleted_count(self, tmp_path: Path):
        base = write_zip(
            tmp_path / "base.zip",
            {
                "stops.txt": STOPS_HEADER
                + "S1,Stop One,1.0,2.0\nS2,Stop Two,3.0,4.0\n",
            },
        )
        new = write_zip(
            tmp_path / "new.zip",
            {
                "stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\n",
            },
        )
        result = diff_feeds(base, new)
        fd = _get_file_diff(result, "stops.txt")
        assert fd.stats.rows_deleted_count == 1

    def test_rows_deleted_identifier(self, tmp_path: Path):
        base = write_zip(
            tmp_path / "base.zip",
            {
                "stops.txt": STOPS_HEADER
                + "S1,Stop One,1.0,2.0\nS2,Stop Two,3.0,4.0\n",
            },
        )
        new = write_zip(
            tmp_path / "new.zip",
            {
                "stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\n",
            },
        )
        result = diff_feeds(base, new)
        fd = _get_file_diff(result, "stops.txt")
        assert len(fd.row_changes.deleted) == 1
        assert fd.row_changes.deleted[0].identifier == {"stop_id": "S2"}


class TestRowsModified:
    def test_rows_modified_count(self, tmp_path: Path):
        base = write_zip(
            tmp_path / "base.zip",
            {
                "stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\n",
            },
        )
        new = write_zip(
            tmp_path / "new.zip",
            {
                "stops.txt": STOPS_HEADER + "S1,Stop One Renamed,1.0,2.0\n",
            },
        )
        result = diff_feeds(base, new)
        fd = _get_file_diff(result, "stops.txt")
        assert fd.stats.rows_modified_count == 1

    def test_rows_modified_field_changes(self, tmp_path: Path):
        base = write_zip(
            tmp_path / "base.zip",
            {
                "stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\n",
            },
        )
        new = write_zip(
            tmp_path / "new.zip",
            {
                "stops.txt": STOPS_HEADER + "S1,Stop One Renamed,1.0,2.0\n",
            },
        )
        result = diff_feeds(base, new)
        fd = _get_file_diff(result, "stops.txt")
        assert len(fd.row_changes.modified) == 1
        mod = fd.row_changes.modified[0]
        assert mod.identifier == {"stop_id": "S1"}
        field_names = [fc.field for fc in mod.field_changes]
        assert "stop_name" in field_names
        stop_name_change = next(
            fc for fc in mod.field_changes if fc.field == "stop_name"
        )
        assert stop_name_change.base_value == "Stop One"
        assert stop_name_change.new_value == "Stop One Renamed"


class TestNoFalsePositives:
    def test_no_false_positives_on_unchanged_rows(self, tmp_path: Path):
        content = (
            STOPS_HEADER
            + "S1,Stop One,1.0,2.0\n"
            + "S2,Stop Two,3.0,4.0\n"
            + "S3,Stop Three,5.0,6.0\n"
        )
        base = write_zip(tmp_path / "base.zip", {"stops.txt": content})
        # only S3 changes
        new_content = (
            STOPS_HEADER
            + "S1,Stop One,1.0,2.0\n"
            + "S2,Stop Two,3.0,4.0\n"
            + "S3,Stop Three RENAMED,5.0,6.0\n"
        )
        new = write_zip(tmp_path / "new.zip", {"stops.txt": new_content})
        result = diff_feeds(base, new)
        fd = _get_file_diff(result, "stops.txt")
        modified_ids = [m.identifier["stop_id"] for m in fd.row_changes.modified]
        assert "S1" not in modified_ids
        assert "S2" not in modified_ids
        assert "S3" in modified_ids

    def test_swapped_row_order_is_not_a_change(self, tmp_path: Path):
        # Row order is irrelevant — the engine indexes by primary key.
        # Swapping two rows must not produce any diff.
        # NOTE: should a row reorder be reported as a structural change even when
        # no field values differ? This is currently an open design question.
        base = write_zip(
            tmp_path / "base.zip",
            {
                "stops.txt": STOPS_HEADER
                + "S1,Stop One,1.0,2.0\nS2,Stop Two,3.0,4.0\n",
            },
        )
        new = write_zip(
            tmp_path / "new.zip",
            {
                "stops.txt": STOPS_HEADER
                + "S2,Stop Two,3.0,4.0\nS1,Stop One,1.0,2.0\n",
            },
        )
        result = diff_feeds(base, new)
        assert result.file_diffs == []
        assert result.summary.total_changes == 0

    def test_trailing_zeros_in_coordinates_are_not_a_change(self, tmp_path: Path):
        # A producer may write '-73.55625' in one version and '-73.556250' in the
        # next. These are numerically identical and must not be reported as a diff.
        base = write_zip(
            tmp_path / "base.zip",
            {
                "shapes.txt": (
                    "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\n11071,45.518332,-73.55625,150001\n"
                ),
            },
        )
        new = write_zip(
            tmp_path / "new.zip",
            {
                "shapes.txt": (
                    "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\n11071,45.518332,-73.556250,150001\n"
                ),
            },
        )
        result = diff_feeds(base, new)
        assert result.file_diffs == []
        assert result.summary.total_changes == 0

    def test_swapped_column_order_is_not_a_change(self, tmp_path: Path):
        # Column order is irrelevant — the engine compares values by column name.
        # Swapping two columns must not produce any diff.
        # NOTE: should a column reorder be reported as a structural change even when
        # no field values differ? This is currently an open design question.
        base = write_zip(
            tmp_path / "base.zip",
            {
                "stops.txt": "stop_id,stop_name,stop_lat\nS1,Stop One,1.0\n",
            },
        )
        new = write_zip(
            tmp_path / "new.zip",
            {
                "stops.txt": "stop_name,stop_id,stop_lat\nStop One,S1,1.0\n",
            },
        )
        result = diff_feeds(base, new)
        assert result.file_diffs == []
        assert result.summary.total_changes == 0


class _UrlopenResponse:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False


class TestRemoteAndFileFilter:
    def test_local_files_filter_limits_compared_files(self, tmp_path: Path):
        base = tmp_path / "base"
        new = tmp_path / "new"
        base.mkdir()
        new.mkdir()

        routes = "route_id,route_short_name\nR1,Route 1\n"
        (base / "routes.txt").write_text(routes, encoding="utf-8")
        (new / "routes.txt").write_text(routes, encoding="utf-8")
        (base / "stops.txt").write_text(
            STOPS_HEADER + "S1,Stop One,1.0,2.0\n", encoding="utf-8"
        )
        (new / "stops.txt").write_text(
            STOPS_HEADER + "S1,Stop One Renamed,1.0,2.0\n", encoding="utf-8"
        )

        routes_only = diff_feeds(base, new, files=["routes.txt"])
        assert "stops.txt" not in {fd.file_name for fd in routes_only.file_diffs}

        stops_only = diff_feeds(base, new, files=["stops.txt"])
        stops_diff = _get_file_diff(stops_only, "stops.txt")
        assert stops_diff.file_action == "modified"

    def test_remote_modified_file(self, monkeypatch: pytest.MonkeyPatch):
        store = {
            "https://x/base/stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\n",
            "https://x/new/stops.txt": STOPS_HEADER + "S1,Stop One Renamed,1.0,2.0\n",
        }
        monkeypatch.setattr("gtfs_diff.engine._http_exists", lambda url: url in store)
        monkeypatch.setattr(
            "gtfs_diff.engine._http_get_text", lambda url: io.StringIO(store[url])
        )

        result = diff_feeds("https://x/base", "https://x/new", files=["stops.txt"])

        fd = _get_file_diff(result, "stops.txt")
        assert fd.file_action == "modified"
        assert len(fd.row_changes.modified) == 1
        mod = fd.row_changes.modified[0]
        assert mod.identifier == {"stop_id": "S1"}
        stop_name_change = next(
            fc for fc in mod.field_changes if fc.field == "stop_name"
        )
        assert stop_name_change.base_value == "Stop One"
        assert stop_name_change.new_value == "Stop One Renamed"

    def test_remote_added_and_deleted_files(self, monkeypatch: pytest.MonkeyPatch):
        store = {
            "https://x/base/stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\n",
            "https://x/new/routes.txt": "route_id,route_short_name\nR1,Route 1\n",
        }
        monkeypatch.setattr("gtfs_diff.engine._http_exists", lambda url: url in store)
        monkeypatch.setattr(
            "gtfs_diff.engine._http_get_text", lambda url: io.StringIO(store[url])
        )

        result = diff_feeds(
            "https://x/base",
            "https://x/new",
            files=["stops.txt", "routes.txt"],
        )

        assert _get_file_diff(result, "routes.txt").file_action == "added"
        assert _get_file_diff(result, "stops.txt").file_action == "deleted"

    def test_remote_without_files_probes_known_files(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        routes = "route_id,route_short_name\nR1,Route 1\n"
        store = {
            "https://x/base/stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\n",
            "https://x/new/stops.txt": STOPS_HEADER + "S1,Stop One Renamed,1.0,2.0\n",
            "https://x/base/routes.txt": routes,
            "https://x/new/routes.txt": routes,
        }
        monkeypatch.setattr("gtfs_diff.engine._http_exists", lambda url: url in store)
        monkeypatch.setattr(
            "gtfs_diff.engine._http_get_text", lambda url: io.StringIO(store[url])
        )

        result = diff_feeds("https://x/base", "https://x/new")

        assert _get_file_diff(result, "stops.txt").file_action == "modified"
        compared_files = {fd.file_name for fd in result.file_diffs}
        assert "routes.txt" not in compared_files

    def test_remote_without_files_skips_absent_known_files(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        store = {
            "https://x/base/stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\n",
            "https://x/new/stops.txt": STOPS_HEADER + "S1,Stop One Renamed,1.0,2.0\n",
        }
        monkeypatch.setattr("gtfs_diff.engine._http_exists", lambda url: url in store)
        monkeypatch.setattr(
            "gtfs_diff.engine._http_get_text", lambda url: io.StringIO(store[url])
        )

        result = diff_feeds("https://x/base", "https://x/new")

        assert _get_file_diff(result, "stops.txt").file_action == "modified"
        compared_files = {fd.file_name for fd in result.file_diffs}
        assert "trips.txt" not in compared_files

    def test_remote_explicit_files_limits_probing(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        store = {
            "https://x/base/stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\n",
            "https://x/new/stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\n",
            "https://x/base/routes.txt": "route_id,route_short_name\nR1,Route 1\n",
            "https://x/new/routes.txt": (
                "route_id,route_short_name\nR1,Route 1 Renamed\n"
            ),
        }
        probed: list[str] = []

        def exists(url: str) -> bool:
            probed.append(url)
            return url in store

        monkeypatch.setattr("gtfs_diff.engine._http_exists", exists)
        monkeypatch.setattr(
            "gtfs_diff.engine._http_get_text", lambda url: io.StringIO(store[url])
        )

        result = diff_feeds("https://x/base", "https://x/new", files=["stops.txt"])

        assert result.file_diffs == []
        assert probed == ["https://x/base/stops.txt", "https://x/new/stops.txt"]

    def test_url_helpers(self):
        assert _is_url("https://x") is True
        assert _is_url("/tmp/x") is False
        assert _join_url("https://x/base/", "stops.txt") == "https://x/base/stops.txt"
        assert _join_url("https://x/base/", "/stops.txt") == "https://x/base/stops.txt"


class TestHttpExistsProbing:
    @staticmethod
    def _http_error(url: str, code: int, msg: str = "Error") -> urllib.error.HTTPError:
        return urllib.error.HTTPError(url, code, msg, hdrs={}, fp=None)

    def test_head_200_returns_true(self, monkeypatch: pytest.MonkeyPatch):
        calls: list[str] = []

        def fake_urlopen(req, timeout):
            calls.append(req.get_method())
            return _UrlopenResponse()

        monkeypatch.setattr("gtfs_diff.engine.urllib.request.urlopen", fake_urlopen)

        assert _http_exists("https://x/feed/stops.txt") is True
        assert calls == ["HEAD"]

    def test_head_404_returns_false_without_get_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        calls: list[str] = []
        url = "https://x/feed/stops.txt"

        def fake_urlopen(req, timeout):
            calls.append(req.get_method())
            raise self._http_error(url, 404, "Not Found")

        monkeypatch.setattr("gtfs_diff.engine.urllib.request.urlopen", fake_urlopen)

        assert _http_exists(url) is False
        assert calls == ["HEAD"]

    def test_private_folder_missing_file_head_403_get_403_returns_false(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        calls: list[tuple[str, str | None]] = []
        url = "https://x/feed/missing.txt"

        def fake_urlopen(req, timeout):
            calls.append((req.get_method(), req.get_header("Range")))
            raise self._http_error(url, 403, "Forbidden")

        monkeypatch.setattr("gtfs_diff.engine.urllib.request.urlopen", fake_urlopen)

        assert _http_exists(url) is False
        assert calls == [("HEAD", None), ("GET", "bytes=0-0")]

    def test_head_disallowed_get_success_returns_true(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        calls: list[tuple[str, str | None]] = []
        url = "https://x/feed/stops.txt"

        def fake_urlopen(req, timeout):
            method = req.get_method()
            calls.append((method, req.get_header("Range")))
            if method == "HEAD":
                raise self._http_error(url, 405, "Method Not Allowed")
            assert method == "GET"
            return _UrlopenResponse()

        monkeypatch.setattr("gtfs_diff.engine.urllib.request.urlopen", fake_urlopen)

        assert _http_exists(url) is True
        assert calls == [("HEAD", None), ("GET", "bytes=0-0")]

    def test_head_401_get_401_returns_false(self, monkeypatch: pytest.MonkeyPatch):
        calls: list[str] = []
        url = "https://x/feed/private.txt"

        def fake_urlopen(req, timeout):
            calls.append(req.get_method())
            raise self._http_error(url, 401, "Unauthorized")

        monkeypatch.setattr("gtfs_diff.engine.urllib.request.urlopen", fake_urlopen)

        assert _http_exists(url) is False
        assert calls == ["HEAD", "GET"]

    def test_gone_410_returns_false_for_head_and_ranged_get(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        calls: list[str] = []
        url = "https://x/feed/gone.txt"

        def fake_urlopen(req, timeout):
            calls.append(req.get_method())
            raise self._http_error(url, 410, "Gone")

        monkeypatch.setattr("gtfs_diff.engine.urllib.request.urlopen", fake_urlopen)

        assert _http_exists(url) is False
        assert _http_exists_via_get(url) is False
        assert calls == ["HEAD", "GET"]

    def test_head_500_raises(self, monkeypatch: pytest.MonkeyPatch):
        calls: list[str] = []
        url = "https://x/feed/stops.txt"

        def fake_urlopen(req, timeout):
            calls.append(req.get_method())
            raise self._http_error(url, 500, "Internal Server Error")

        monkeypatch.setattr("gtfs_diff.engine.urllib.request.urlopen", fake_urlopen)

        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _http_exists(url)
        assert exc_info.value.code == 500
        assert calls == ["HEAD"]


# ---------------------------------------------------------------------------
# Column-level tests
# ---------------------------------------------------------------------------


class TestColumnAdded:
    def test_column_added(self, tmp_path: Path):
        base = write_zip(
            tmp_path / "base.zip",
            {
                "stops.txt": "stop_id,stop_name\nS1,Stop One\n",
            },
        )
        new = write_zip(
            tmp_path / "new.zip",
            {
                "stops.txt": "stop_id,stop_name,stop_desc\nS1,Stop One,A description\n",
            },
        )
        result = diff_feeds(base, new)
        fd = _get_file_diff(result, "stops.txt")
        added_names = [c.name for c in fd.columns_added]
        assert "stop_desc" in added_names

    def test_column_added_position(self, tmp_path: Path):
        base = write_zip(
            tmp_path / "base.zip",
            {
                "stops.txt": "stop_id,stop_name\nS1,Stop One\n",
            },
        )
        new = write_zip(
            tmp_path / "new.zip",
            {
                "stops.txt": "stop_id,stop_name,stop_desc\nS1,Stop One,A description\n",
            },
        )
        result = diff_feeds(base, new)
        fd = _get_file_diff(result, "stops.txt")
        stop_desc_col = next(c for c in fd.columns_added if c.name == "stop_desc")
        assert stop_desc_col.position == 3


class TestColumnDeleted:
    def test_column_deleted(self, tmp_path: Path):
        base = write_zip(
            tmp_path / "base.zip",
            {
                "stops.txt": "stop_id,stop_name,stop_desc\nS1,Stop One,A description\n",
            },
        )
        new = write_zip(
            tmp_path / "new.zip",
            {
                "stops.txt": "stop_id,stop_name\nS1,Stop One\n",
            },
        )
        result = diff_feeds(base, new)
        fd = _get_file_diff(result, "stops.txt")
        deleted_names = [c.name for c in fd.columns_deleted]
        assert "stop_desc" in deleted_names

    def test_column_deleted_position(self, tmp_path: Path):
        base = write_zip(
            tmp_path / "base.zip",
            {
                "stops.txt": "stop_id,stop_name,stop_desc\nS1,Stop One,A description\n",
            },
        )
        new = write_zip(
            tmp_path / "new.zip",
            {
                "stops.txt": "stop_id,stop_name\nS1,Stop One\n",
            },
        )
        result = diff_feeds(base, new)
        fd = _get_file_diff(result, "stops.txt")
        stop_desc_col = next(c for c in fd.columns_deleted if c.name == "stop_desc")
        assert stop_desc_col.position == 3


# ---------------------------------------------------------------------------
# Cap tests
# ---------------------------------------------------------------------------


def _make_stops_csv(n: int) -> str:
    header = "stop_id,stop_name\n"
    rows = "".join(f"S{i},Stop {i}\n" for i in range(1, n + 1))
    return header + rows


class TestCapZero:
    def test_cap_zero_omits_row_changes(self, tmp_path: Path):
        base = write_zip(
            tmp_path / "base.zip",
            {
                "stops.txt": "stop_id,stop_name\nS1,Stop One\n",
            },
        )
        new = write_zip(
            tmp_path / "new.zip",
            {
                "stops.txt": "stop_id,stop_name\nS1,Stop One\nS2,Stop Two\n",
            },
        )
        result = diff_feeds(base, new, row_changes_cap_per_file=0)
        fd = _get_file_diff(result, "stops.txt")
        assert fd.row_changes is None


class TestCapLimits:
    def test_cap_limits_row_changes(self, tmp_path: Path):
        # 5 new rows added, cap = 3 → 3 included, omitted_count = 2
        base = write_zip(
            tmp_path / "base.zip",
            {
                "stops.txt": "stop_id,stop_name\nS0,Stop Zero\n",
            },
        )
        new = write_zip(
            tmp_path / "new.zip",
            {
                "stops.txt": "stop_id,stop_name\n"
                + "".join(f"S{i},Stop {i}\n" for i in range(1, 7)),
            },
        )
        result = diff_feeds(base, new, row_changes_cap_per_file=3)
        fd = _get_file_diff(result, "stops.txt")
        # Total included across added+deleted+modified <= 3
        total_included = (
            len(fd.row_changes.added)
            + len(fd.row_changes.deleted)
            + len(fd.row_changes.modified)
        )
        assert total_included <= 3
        assert fd.truncated is not None
        assert fd.truncated.is_truncated is True
        assert fd.truncated.omitted_count >= 1

    def test_truncated_omitted_count_correct(self, tmp_path: Path):
        # 5 added rows, 0 deleted, 0 modified; cap = 3 → omitted = 2
        base = write_zip(
            tmp_path / "base.zip",
            {
                "stops.txt": _make_stops_csv(0).replace("\n", "", 1),  # header only
            },
        )
        # Write header-only base
        base = write_zip(
            tmp_path / "base2.zip",
            {
                "stops.txt": "stop_id,stop_name\n",
            },
        )
        new = write_zip(
            tmp_path / "new.zip",
            {
                "stops.txt": _make_stops_csv(5),
            },
        )
        result = diff_feeds(base, new, row_changes_cap_per_file=3)
        fd = _get_file_diff(result, "stops.txt")
        assert len(fd.row_changes.added) == 3
        assert fd.truncated.omitted_count == 2


class TestSplitRowChangesCap:
    """Unit tests for the fair cap allocator in diff_helpers."""

    def test_none_cap_is_unlimited(self):
        assert _split_row_changes_cap(None, 10, 10, 10) == (None, None, None)

    def test_single_active_type_gets_whole_cap(self):
        assert _split_row_changes_cap(6, 20, 0, 0) == (6, 0, 0)
        assert _split_row_changes_cap(6, 0, 20, 0) == (0, 6, 0)
        assert _split_row_changes_cap(6, 0, 0, 20) == (0, 0, 6)

    def test_two_active_types_split_evenly(self):
        assert _split_row_changes_cap(6, 20, 20, 0) == (3, 3, 0)
        assert _split_row_changes_cap(6, 20, 0, 20) == (3, 0, 3)

    def test_three_active_types_split_evenly(self):
        assert _split_row_changes_cap(9, 20, 20, 20) == (3, 3, 3)

    def test_indivisible_remainder_favours_earlier_types(self):
        # cap 5 over 3 types: 1 each, remainder 2 → added, deleted.
        assert _split_row_changes_cap(5, 20, 20, 20) == (2, 2, 1)

    def test_leftover_budget_redistributed(self):
        # added only has 1 row; its unused share flows to the others.
        assert _split_row_changes_cap(9, 1, 20, 20) == (1, 4, 4)

    def test_never_exceeds_true_counts(self):
        a, d, m = _split_row_changes_cap(100, 2, 3, 4)
        assert (a, d, m) == (2, 3, 4)

    def test_cap_larger_than_total_includes_all(self):
        assert _split_row_changes_cap(50, 5, 5, 5) == (5, 5, 5)

    def test_no_changes_allocates_nothing(self):
        assert _split_row_changes_cap(5, 0, 0, 0) == (0, 0, 0)


class TestCapFairSplit:
    """Integration: the cap is shared across change types, not added-first."""

    def test_each_change_type_represented(self, tmp_path: Path):
        # 5 added, 5 deleted, 5 modified; cap = 6 → 2 of each (a little of all).
        base_rows = "".join(f"D{i},Del {i}\n" for i in range(5))  # deleted
        base_rows += "".join(f"M{i},Base {i}\n" for i in range(5))  # modified base
        new_rows = "".join(f"A{i},Add {i}\n" for i in range(5))  # added
        new_rows += "".join(f"M{i},New {i}\n" for i in range(5))  # modified new
        base = write_zip(
            tmp_path / "base.zip", {"stops.txt": "stop_id,stop_name\n" + base_rows}
        )
        new = write_zip(
            tmp_path / "new.zip", {"stops.txt": "stop_id,stop_name\n" + new_rows}
        )
        result = diff_feeds(base, new, row_changes_cap_per_file=6)
        fd = _get_file_diff(result, "stops.txt")
        rc = fd.row_changes
        assert len(rc.added) == 2
        assert len(rc.deleted) == 2
        assert len(rc.modified) == 2
        assert fd.truncated is not None
        assert fd.truncated.omitted_count == 9  # 15 true - 6 shown


class TestCapNone:
    def test_cap_none_includes_all(self, tmp_path: Path):
        base = write_zip(
            tmp_path / "base.zip",
            {
                "stops.txt": "stop_id,stop_name\n",
            },
        )
        new = write_zip(
            tmp_path / "new.zip",
            {
                "stops.txt": _make_stops_csv(5),
            },
        )
        result = diff_feeds(base, new, row_changes_cap_per_file=None)
        fd = _get_file_diff(result, "stops.txt")
        assert len(fd.row_changes.added) == 5
        assert fd.truncated is None


# ---------------------------------------------------------------------------
# Missing primary key column
# ---------------------------------------------------------------------------


class TestNotComparedIdChurn:
    @staticmethod
    def _shapes_csv(start: int, n: int) -> str:
        header = "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\n"
        rows = "".join(f"SHP{i},50.0,-5.0,{i}\n" for i in range(start, start + n))
        return header + rows

    def test_full_id_churn_marks_file_not_compared(self, tmp_path: Path):
        # Every shape_id is regenerated → keys are entirely disjoint.
        base = write_zip(tmp_path / "base.zip", {"shapes.txt": self._shapes_csv(0, 60)})
        new = write_zip(
            tmp_path / "new.zip", {"shapes.txt": self._shapes_csv(1000, 60)}
        )
        result = diff_feeds(base, new)
        fd = _get_file_diff(result, "shapes.txt")
        assert fd.file_action == "not_compared"
        assert fd.row_changes is None
        assert fd.not_compared_reason is not None
        assert fd.not_compared_reason.code == "id_churn"

    def test_not_compared_summary_status_and_count(self, tmp_path: Path):
        base = write_zip(tmp_path / "base.zip", {"shapes.txt": self._shapes_csv(0, 60)})
        new = write_zip(
            tmp_path / "new.zip", {"shapes.txt": self._shapes_csv(1000, 60)}
        )
        result = diff_feeds(base, new)
        fs = _get_file_summary(result, "shapes.txt")
        assert fs.status == "not_compared"
        assert result.summary.files_not_compared_count == 1

    def test_not_compared_preserves_column_diffs(self, tmp_path: Path):
        # New feed regenerates ids AND adds a column; column diff must survive.
        base = write_zip(tmp_path / "base.zip", {"shapes.txt": self._shapes_csv(0, 60)})
        new_rows = "".join(f"SHP{i},50.0,-5.0,{i},1.5\n" for i in range(1000, 1060))
        new_csv = (
            "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence,"
            "shape_dist_traveled\n" + new_rows
        )
        new = write_zip(tmp_path / "new.zip", {"shapes.txt": new_csv})
        result = diff_feeds(base, new)
        fd = _get_file_diff(result, "shapes.txt")
        assert fd.file_action == "not_compared"
        added_names = [c.name for c in fd.columns_added]
        assert "shape_dist_traveled" in added_names
        assert fd.stats.total_rows_base == 60
        assert fd.stats.total_rows_new == 60

    def test_stable_ids_below_threshold_diffed_normally(self, tmp_path: Path):
        # 60 rows, only 1 deleted + 1 added → overlap-coefficient churn ~1.7%.
        base = write_zip(tmp_path / "base.zip", {"shapes.txt": self._shapes_csv(0, 60)})
        new = write_zip(tmp_path / "new.zip", {"shapes.txt": self._shapes_csv(1, 60)})
        result = diff_feeds(base, new)
        fd = _get_file_diff(result, "shapes.txt")
        assert fd.file_action == "modified"

    def test_bulk_add_is_not_flagged_as_churn(self, tmp_path: Path):
        # Base keys are a subset of new keys (file grew 3×). The overlap
        # coefficient stays at 1.0 (churn 0) so this is diffed, not flagged —
        # the key property that distinguishes it from Jaccard / ÷max.
        base = write_zip(tmp_path / "base.zip", {"shapes.txt": self._shapes_csv(0, 60)})
        new = write_zip(tmp_path / "new.zip", {"shapes.txt": self._shapes_csv(0, 180)})
        result = diff_feeds(base, new)
        fd = _get_file_diff(result, "shapes.txt")
        assert fd.file_action == "modified"

    def test_small_files_are_not_flagged(self, tmp_path: Path):
        # Disjoint keys but fewer rows than the detection minimum.
        base = write_zip(tmp_path / "base.zip", {"shapes.txt": self._shapes_csv(0, 1)})
        new = write_zip(tmp_path / "new.zip", {"shapes.txt": self._shapes_csv(1000, 1)})
        result = diff_feeds(base, new)
        fd = _get_file_diff(result, "shapes.txt")
        assert fd.file_action == "modified"

    def test_threshold_override_disables_detection(self, tmp_path: Path):
        base = write_zip(tmp_path / "base.zip", {"shapes.txt": self._shapes_csv(0, 60)})
        new = write_zip(
            tmp_path / "new.zip", {"shapes.txt": self._shapes_csv(1000, 60)}
        )
        # A threshold above 1.0 can never be reached → always diffed.
        result = diff_feeds(base, new, id_churn_threshold=1.01)
        fd = _get_file_diff(result, "shapes.txt")
        assert fd.file_action == "modified"

    def test_per_file_override_disables_detection(self, tmp_path: Path):
        base = write_zip(tmp_path / "base.zip", {"shapes.txt": self._shapes_csv(0, 60)})
        new = write_zip(
            tmp_path / "new.zip", {"shapes.txt": self._shapes_csv(1000, 60)}
        )
        # Fully churned, but a per-file override raises shapes.txt out of reach.
        result = diff_feeds(base, new, id_churn_thresholds={"shapes.txt": 1.01})
        fd = _get_file_diff(result, "shapes.txt")
        assert fd.file_action == "modified"

    def test_per_file_override_beats_global_threshold(self, tmp_path: Path):
        # ~33% churn: above the per-file override (0.1) but below the global
        # (0.9). The per-file value must win → flagged not_compared.
        base = write_zip(tmp_path / "base.zip", {"shapes.txt": self._shapes_csv(0, 60)})
        new = write_zip(tmp_path / "new.zip", {"shapes.txt": self._shapes_csv(20, 60)})
        result = diff_feeds(
            base,
            new,
            id_churn_threshold=0.9,
            id_churn_thresholds={"shapes.txt": 0.1},
        )
        fd = _get_file_diff(result, "shapes.txt")
        assert fd.file_action == "not_compared"
        assert fd.not_compared_reason.code == "id_churn"

    def test_per_file_override_only_affects_named_file(self, tmp_path: Path):
        # Override targets trips.txt; shapes.txt still uses the global default.
        base = write_zip(tmp_path / "base.zip", {"shapes.txt": self._shapes_csv(0, 60)})
        new = write_zip(
            tmp_path / "new.zip", {"shapes.txt": self._shapes_csv(1000, 60)}
        )
        result = diff_feeds(base, new, id_churn_thresholds={"trips.txt": 1.01})
        fd = _get_file_diff(result, "shapes.txt")
        assert fd.file_action == "not_compared"


# ---------------------------------------------------------------------------
# Foreign-key ignored columns (file hierarchy)
# ---------------------------------------------------------------------------


class TestForeignKeyIgnoredColumns:
    @staticmethod
    def _shapes_csv(start: int, n: int) -> str:
        header = "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\n"
        rows = "".join(f"SHP{i},50.0,-5.0,{i}\n" for i in range(start, start + n))
        return header + rows

    @staticmethod
    def _trips_csv(shape_ids: list[str], changed_headsign: bool) -> str:
        header = "trip_id,route_id,service_id,shape_id,trip_headsign\n"
        rows = []
        for idx, shp in enumerate(shape_ids, start=1):
            headsign = "Downtown"
            if changed_headsign and idx == 1:
                headsign = "Uptown"
            rows.append(f"T{idx},R1,SVC1,{shp},{headsign}\n")
        return header + "".join(rows)

    def test_fk_column_ignored_when_referenced_file_churns(self, tmp_path: Path):
        # shapes.txt fully regenerates shape_id → not_compared.
        # trips.txt keeps stable trip_id but its shape_id values also changed;
        # that column must be ignored, leaving only the real headsign change.
        base = write_zip(
            tmp_path / "base.zip",
            {
                "shapes.txt": self._shapes_csv(0, 60),
                "trips.txt": self._trips_csv(
                    [f"SHP{i}" for i in range(5)], changed_headsign=False
                ),
            },
        )
        new = write_zip(
            tmp_path / "new.zip",
            {
                "shapes.txt": self._shapes_csv(1000, 60),
                "trips.txt": self._trips_csv(
                    [f"SHP{1000 + i}" for i in range(5)], changed_headsign=True
                ),
            },
        )
        result = diff_feeds(base, new)

        shapes = _get_file_diff(result, "shapes.txt")
        assert shapes.file_action == "not_compared"

        trips = _get_file_diff(result, "trips.txt")
        assert trips.file_action == "modified"
        assert trips.ignored_columns is not None
        ignored = {ic.column: ic.reason.code for ic in trips.ignored_columns}
        assert ignored == {"shape_id": "references_not_compared_file"}
        # Only the headsign change counts; shape_id churn is excluded.
        assert trips.stats.rows_modified_count == 1
        changed_fields = {
            fc.field for fc in trips.row_changes.modified[0].field_changes
        }
        assert changed_fields == {"trip_headsign"}

    def test_fk_column_ignored_when_referenced_file_missing_pk(self, tmp_path: Path):
        assert "shapes.txt" in get_foreign_keys("trips.txt")["shape_id"]
        base = write_zip(
            tmp_path / "base.zip",
            {
                "shapes.txt": (
                    "shape_pt_lat,shape_pt_lon,shape_pt_sequence\n"
                    "50.0,-5.0,1\n"
                    "51.0,-5.1,2\n"
                ),
                "trips.txt": self._trips_csv(
                    [f"SHP{i}" for i in range(5)], changed_headsign=False
                ),
            },
        )
        new = write_zip(
            tmp_path / "new.zip",
            {
                "shapes.txt": (
                    "shape_pt_lat,shape_pt_lon,shape_pt_sequence\n"
                    "50.0,-5.0,1\n"
                    "51.0,-5.1,2\n"
                ),
                "trips.txt": self._trips_csv(
                    [f"SHP{1000 + i}" for i in range(5)], changed_headsign=True
                ),
            },
        )
        result = diff_feeds(base, new)

        shapes = _get_file_diff(result, "shapes.txt")
        assert shapes.file_action == "not_compared"
        assert shapes.not_compared_reason.code == "missing_primary_key"

        trips = _get_file_diff(result, "trips.txt")
        assert trips.file_action == "modified"
        assert trips.ignored_columns is not None
        ignored = {ic.column: ic.reason.code for ic in trips.ignored_columns}
        assert ignored == {"shape_id": "references_not_compared_file"}
        changed_fields = {
            fc.field for fc in trips.row_changes.modified[0].field_changes
        }
        assert changed_fields == {"trip_headsign"}

    def test_whole_diff_continues_when_file_missing_pk(self, tmp_path: Path):
        base = write_zip(
            tmp_path / "base.zip",
            {
                "stops.txt": "stop_name,stop_lat,stop_lon\nStop One,1.0,2.0\n",
                "routes.txt": "route_id,route_short_name\nR1,Route 1\n",
            },
        )
        new = write_zip(
            tmp_path / "new.zip",
            {
                "stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\n",
                "routes.txt": "route_id,route_short_name\nR1,Route One\n",
            },
        )
        result = diff_feeds(base, new)

        stops = _get_file_diff(result, "stops.txt")
        assert stops.file_action == "not_compared"
        assert stops.not_compared_reason.code == "missing_primary_key"
        assert _get_file_diff(result, "routes.txt").file_action == "modified"

    def test_fk_only_change_makes_file_unchanged(self, tmp_path: Path):
        # trips.txt's ONLY difference is the churned shape_id → once ignored the
        # file has no real change and is omitted from file_diffs entirely.
        base = write_zip(
            tmp_path / "base.zip",
            {
                "shapes.txt": self._shapes_csv(0, 60),
                "trips.txt": self._trips_csv(
                    [f"SHP{i}" for i in range(5)], changed_headsign=False
                ),
            },
        )
        new = write_zip(
            tmp_path / "new.zip",
            {
                "shapes.txt": self._shapes_csv(1000, 60),
                "trips.txt": self._trips_csv(
                    [f"SHP{1000 + i}" for i in range(5)], changed_headsign=False
                ),
            },
        )
        result = diff_feeds(base, new)
        names = [fd.file_name for fd in result.file_diffs]
        assert "trips.txt" not in names

    def test_fk_column_not_ignored_when_parent_stable(self, tmp_path: Path):
        # shapes.txt is unchanged (stable shape_id), so a shape_id edit in
        # trips.txt is a real change and must NOT be ignored.
        shapes = self._shapes_csv(0, 60)
        base = write_zip(
            tmp_path / "base.zip",
            {
                "shapes.txt": shapes,
                "trips.txt": self._trips_csv(["SHP0"], changed_headsign=False),
            },
        )
        new = write_zip(
            tmp_path / "new.zip",
            {
                "shapes.txt": shapes,
                "trips.txt": self._trips_csv(["SHP1"], changed_headsign=False),
            },
        )
        result = diff_feeds(base, new)
        trips = _get_file_diff(result, "trips.txt")
        assert trips.file_action == "modified"
        assert trips.ignored_columns is None
        changed_fields = {
            fc.field for fc in trips.row_changes.modified[0].field_changes
        }
        assert "shape_id" in changed_fields


class TestProcessingOrder:
    def test_parents_precede_children(self):
        from gtfs_diff.engine import _processing_order

        order = _processing_order(["trips.txt", "shapes.txt", "stop_times.txt"])
        assert order.index("shapes.txt") < order.index("trips.txt")
        assert order.index("trips.txt") < order.index("stop_times.txt")

    def test_missing_parent_is_ignored(self):
        from gtfs_diff.engine import _processing_order

        # routes.txt absent; ordering still works and stays deterministic.
        order = _processing_order(["trips.txt", "shapes.txt"])
        assert order == ["shapes.txt", "trips.txt"]


# ---------------------------------------------------------------------------
# Missing primary key column
# ---------------------------------------------------------------------------


class TestOptionalPrimaryKeyColumns:
    def test_translations_record_id_variant_pads_missing_pk_columns_with_null(self):
        csv_text = (
            "table_name,field_name,language,record_id,translation\n"
            "stops,stop_name,en,S1,Stop One\n"
            "stops,stop_name,en,S2,Stop Two\n"
        )

        _, index = _read_csv_index(
            io.StringIO(csv_text),
            get_primary_key("translations.txt"),
            "translations.txt",
        )

        assert ("stops", "stop_name", "en", "S1", "", "") in index
        assert ("stops", "stop_name", "en", "S2", "", "") in index
        assert len(index) == 2

    def test_translations_field_value_variant_pads_missing_pk_columns_with_null(self):
        csv_text = (
            "table_name,field_name,language,field_value,translation\n"
            "stops,stop_name,en,Stop One,Arrêt Un\n"
            "stops,stop_name,en,Stop Two,Arrêt Deux\n"
        )

        _, index = _read_csv_index(
            io.StringIO(csv_text),
            get_primary_key("translations.txt"),
            "translations.txt",
        )

        assert ("stops", "stop_name", "en", "", "", "Stop One") in index
        assert ("stops", "stop_name", "en", "", "", "Stop Two") in index
        assert len(index) == 2

    def test_translations_full_variant_uses_all_pk_columns(self):
        csv_text = (
            "table_name,field_name,language,record_id,record_sub_id,field_value,translation\n"
            "stop_times,stop_headsign,en,T1,1,Downtown,Centre-ville\n"
            "stop_times,stop_headsign,en,T1,2,Uptown,Haut de la ville\n"
        )

        _, index = _read_csv_index(
            io.StringIO(csv_text),
            get_primary_key("translations.txt"),
            "translations.txt",
        )

        assert ("stop_times", "stop_headsign", "en", "T1", "1", "Downtown") in index
        assert ("stop_times", "stop_headsign", "en", "T1", "2", "Uptown") in index
        assert len(index) == 2

    def test_translations_missing_mandatory_column_still_raises(self):
        csv_text = (
            "table_name,field_name,record_id,translation\nstops,stop_name,S1,Stop One\n"
        )

        with pytest.raises(MissingPrimaryKeyError) as exc_info:
            _read_csv_index(
                io.StringIO(csv_text),
                get_primary_key("translations.txt"),
                "translations.txt",
            )

        assert exc_info.value.file_name == "translations.txt"
        assert exc_info.value.missing_columns == ["language"]
        assert "record_id" not in exc_info.value.missing_columns
        assert "record_sub_id" not in exc_info.value.missing_columns
        assert "field_value" not in exc_info.value.missing_columns
        assert exc_info.value.headers == [
            "table_name",
            "field_name",
            "record_id",
            "translation",
        ]

    def test_mandatory_pk_files_are_unaffected(self):
        csv_text = "stop_name,stop_lat,stop_lon\nStop One,1.0,2.0\n"

        with pytest.raises(MissingPrimaryKeyError) as exc_info:
            _read_csv_index(
                io.StringIO(csv_text),
                get_primary_key("stops.txt"),
                "stops.txt",
            )

        assert exc_info.value.file_name == "stops.txt"
        assert exc_info.value.missing_columns == ["stop_id"]
        assert get_optional_primary_key_columns("stops.txt") == set()
        assert get_optional_primary_key_columns("translations.txt") == {
            "record_id",
            "record_sub_id",
            "field_value",
        }

    def test_diff_feeds_with_translations_record_id_variant(self, tmp_path: Path):
        base = write_zip(
            tmp_path / "base.zip",
            {
                "translations.txt": (
                    "table_name,field_name,language,record_id,translation\n"
                    "stops,stop_name,en,S1,Stop One\n"
                ),
            },
        )
        new = write_zip(
            tmp_path / "new.zip",
            {
                "translations.txt": (
                    "table_name,field_name,language,record_id,translation\n"
                    "stops,stop_name,en,S1,Stop 1\n"
                ),
            },
        )

        result = diff_feeds(base, new)

        fd = _get_file_diff(result, "translations.txt")
        assert fd.file_action == "modified"
        assert fd.stats.rows_modified_count == 1

    def test_diff_feeds_aligns_missing_optional_pk_with_empty_value(
        self, tmp_path: Path
    ):
        base = write_zip(
            tmp_path / "base.zip",
            {
                "translations.txt": (
                    "table_name,field_name,language,record_id,record_sub_id,translation\n"
                    "stops,stop_name,en,S1,,Stop One\n"
                ),
            },
        )
        new = write_zip(
            tmp_path / "new.zip",
            {
                "translations.txt": (
                    "table_name,field_name,language,record_id,translation\n"
                    "stops,stop_name,en,S1,Stop 1\n"
                ),
            },
        )

        result = diff_feeds(base, new)

        fd = _get_file_diff(result, "translations.txt")
        assert fd.file_action == "modified"
        assert fd.stats.rows_added_count == 0
        assert fd.stats.rows_deleted_count == 0
        assert fd.stats.rows_modified_count == 1


class TestExpandedOptionalPrimaryKeys:
    """Optional PK columns added from the GTFS reference (April 2026)."""

    def test_optional_pk_mapping_covers_conditionally_required_files(self):
        assert get_optional_primary_key_columns("agency.txt") == {"agency_id"}
        assert get_optional_primary_key_columns("fare_rules.txt") == {
            "route_id",
            "origin_id",
            "destination_id",
            "contains_id",
        }
        assert get_optional_primary_key_columns("attributions.txt") == {
            "attribution_id"
        }
        assert get_optional_primary_key_columns("timeframes.txt") == {
            "start_time",
            "end_time",
        }
        assert get_optional_primary_key_columns("fare_products.txt") == {
            "rider_category_id",
            "fare_media_id",
        }
        # Files whose every PK column is "Required" stay mandatory.
        assert get_optional_primary_key_columns("stops.txt") == set()
        assert get_optional_primary_key_columns("stop_times.txt") == set()

    def test_primary_key_discrepancies_fixed_against_spec(self):
        # Spec PK is composite; the project previously listed only the first column.
        assert get_primary_key("fare_products.txt") == [
            "fare_product_id",
            "rider_category_id",
            "fare_media_id",
        ]
        assert get_primary_key("fare_transfer_rules.txt") == [
            "from_leg_group_id",
            "to_leg_group_id",
            "fare_product_id",
            "transfer_count",
            "duration_limit",
        ]

    def test_agency_without_agency_id_is_null_padded_not_raised(self):
        # A single-agency feed may omit agency_id; it must not raise and the lone
        # row is keyed on a null agency_id.
        csv_text = (
            "agency_name,agency_url,agency_timezone\nMetro,https://m.example,UTC\n"
        )
        headers, index = _read_csv_index(
            io.StringIO(csv_text), get_primary_key("agency.txt"), "agency.txt"
        )
        assert headers == ["agency_name", "agency_url", "agency_timezone"]
        assert ("",) in index
        assert len(index) == 1

    def test_fare_products_aligns_when_optional_pk_columns_absent(self, tmp_path: Path):
        # Neither feed carries rider_category_id / fare_media_id; both pad them to
        # null, so the row aligns and a price change is reported as modified.
        base = write_zip(
            tmp_path / "base.zip",
            {
                "fare_products.txt": (
                    "fare_product_id,fare_product_name,amount,currency\n"
                    "FP1,Single,2.50,USD\n"
                ),
            },
        )
        new = write_zip(
            tmp_path / "new.zip",
            {
                "fare_products.txt": (
                    "fare_product_id,fare_product_name,amount,currency\n"
                    "FP1,Single,3.00,USD\n"
                ),
            },
        )
        result = diff_feeds(base, new)
        fd = _get_file_diff(result, "fare_products.txt")
        assert fd.file_action == "modified"
        assert fd.stats.rows_added_count == 0
        assert fd.stats.rows_deleted_count == 0
        assert fd.stats.rows_modified_count == 1

    def test_optional_pk_column_not_added_to_reported_headers(self, tmp_path: Path):
        # The injected null PK columns must affect only the compare step, never the
        # reported columns/headers.
        base = write_zip(
            tmp_path / "base.zip",
            {
                "fare_products.txt": (
                    "fare_product_id,fare_product_name,amount,currency\n"
                    "FP1,Single,2.50,USD\n"
                ),
            },
        )
        new = write_zip(
            tmp_path / "new.zip",
            {
                "fare_products.txt": (
                    "fare_product_id,fare_product_name,amount,currency\n"
                    "FP1,Single,3.00,USD\n"
                ),
            },
        )
        result = diff_feeds(base, new)
        fd = _get_file_diff(result, "fare_products.txt")
        reported_cols = set(fd.row_changes.columns)
        assert "rider_category_id" not in reported_cols
        assert "fare_media_id" not in reported_cols
        assert fd.columns_added == []
        assert fd.columns_deleted == []


class TestMissingPrimaryKeyNotCompared:
    def test_missing_pk_column_in_base_is_not_compared(self, tmp_path: Path):
        base = write_zip(
            tmp_path / "base.zip",
            {
                "stops.txt": "stop_name,stop_lat,stop_lon\n"
                "Stop One,1.0,2.0\n",  # stop_id absent
            },
        )
        new = write_zip(
            tmp_path / "new.zip",
            {
                "stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\n",
            },
        )
        result = diff_feeds(base, new)
        fd = _get_file_diff(result, "stops.txt")
        assert fd.file_action == "not_compared"
        assert fd.not_compared_reason is not None
        assert fd.not_compared_reason.code == "missing_primary_key"

    def test_missing_pk_column_in_new_is_not_compared(self, tmp_path: Path):
        base = write_zip(
            tmp_path / "base.zip",
            {
                "stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\n",
            },
        )
        new = write_zip(
            tmp_path / "new.zip",
            {
                "stops.txt": "stop_name,stop_lat,stop_lon\n"
                "Stop One,1.0,2.0\n",  # stop_id absent
            },
        )
        result = diff_feeds(base, new)
        fd = _get_file_diff(result, "stops.txt")
        assert fd.file_action == "not_compared"
        assert fd.not_compared_reason is not None
        assert fd.not_compared_reason.code == "missing_primary_key"

    def test_missing_pk_column_in_both_feeds_is_not_compared(self, tmp_path: Path):
        base = write_zip(
            tmp_path / "base.zip",
            {
                "stops.txt": "stop_name,stop_lat,stop_lon\nStop One,1.0,2.0\n",
            },
        )
        new = write_zip(
            tmp_path / "new.zip",
            {
                "stops.txt": "stop_name,stop_lat,stop_lon\nStop One,1.0,2.0\n",
            },
        )
        result = diff_feeds(base, new)
        fd = _get_file_diff(result, "stops.txt")
        assert fd.file_action == "not_compared"
        assert fd.not_compared_reason is not None
        assert fd.not_compared_reason.code == "missing_primary_key"

    def test_not_compared_reason_names_missing_column(self, tmp_path: Path):
        base = write_zip(
            tmp_path / "base.zip",
            {
                "stops.txt": "stop_name,stop_lat,stop_lon\nStop One,1.0,2.0\n",
            },
        )
        new = write_zip(
            tmp_path / "new.zip",
            {
                "stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\n",
            },
        )
        result = diff_feeds(base, new)
        fd = _get_file_diff(result, "stops.txt")
        assert fd.not_compared_reason is not None
        assert "stop_id" in fd.not_compared_reason.message

    def test_not_compared_stats_reflect_data_row_counts(self, tmp_path: Path):
        base = write_zip(
            tmp_path / "base.zip",
            {
                "stops.txt": (
                    "stop_name,stop_lat,stop_lon\nStop One,1.0,2.0\nStop Two,3.0,4.0\n"
                ),
            },
        )
        new = write_zip(
            tmp_path / "new.zip",
            {
                "stops.txt": (
                    STOPS_HEADER
                    + "S1,Stop One,1.0,2.0\n"
                    + "S2,Stop Two,3.0,4.0\n"
                    + "S3,Stop Three,5.0,6.0\n"
                ),
            },
        )
        result = diff_feeds(base, new)
        fd = _get_file_diff(result, "stops.txt")
        assert fd.file_action == "not_compared"
        assert fd.stats.total_rows_base == 2
        assert fd.stats.total_rows_new == 3


# ---------------------------------------------------------------------------
# Unsupported files
# ---------------------------------------------------------------------------


class TestUnsupportedFile:
    def test_unsupported_file_in_metadata(self, tmp_path: Path):
        base = write_zip(
            tmp_path / "base.zip",
            {
                "stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\n",
                "custom_data.txt": "foo,bar\n1,2\n",
            },
        )
        new = write_zip(
            tmp_path / "new.zip",
            {
                "stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\n",
                "custom_data.txt": "foo,bar\n1,2\n",
            },
        )
        result = diff_feeds(base, new)
        unsupported_names = [u.file_name for u in result.metadata.unsupported_files]
        assert "custom_data.txt" in unsupported_names

    def test_unsupported_file_present_in_both(self, tmp_path: Path):
        base = write_zip(
            tmp_path / "base.zip",
            {
                "stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\n",
                "custom_data.txt": "foo,bar\n1,2\n",
            },
        )
        new = write_zip(
            tmp_path / "new.zip",
            {
                "stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\n",
                "custom_data.txt": "foo,bar\n1,2\n",
            },
        )
        result = diff_feeds(base, new)
        uf = next(
            u
            for u in result.metadata.unsupported_files
            if u.file_name == "custom_data.txt"
        )
        assert uf.present_in == "both"


# ---------------------------------------------------------------------------
# Schema validation (model round-trip)
# ---------------------------------------------------------------------------


class TestOutputMatchesSchema:
    def test_output_matches_schema(self, tmp_path: Path):
        base = write_zip(
            tmp_path / "base.zip",
            {
                "stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\n",
            },
        )
        new = write_zip(
            tmp_path / "new.zip",
            {
                "stops.txt": STOPS_HEADER
                + "S1,Stop One,1.0,2.0\nS2,Stop Two,3.0,4.0\n",
            },
        )
        result = diff_feeds(base, new)
        json_str = result.model_dump_json()
        restored = GtfsDiff.model_validate_json(json_str)
        assert restored.summary.total_changes >= 1
        assert restored.metadata.schema_version == "v2-rc1"


# ---------------------------------------------------------------------------
# raw_value column ordering
# ---------------------------------------------------------------------------


class TestRawValueColumnOrder:
    def test_raw_value_has_empty_for_base_only_column(self, tmp_path: Path):
        # base has stop_lat; new does NOT have stop_lat (deleted column).
        # Added rows in new should have empty string for stop_lat in raw_value.
        base = write_zip(
            tmp_path / "base.zip",
            {
                "stops.txt": "stop_id,stop_name,stop_lat\nS1,Stop One,1.0\n",
            },
        )
        new = write_zip(
            tmp_path / "new.zip",
            {
                "stops.txt": "stop_id,stop_name\nS1,Stop One\nS2,Stop Two\n",
            },
        )
        result = diff_feeds(base, new)
        fd = _get_file_diff(result, "stops.txt")
        # S2 is an added row; union_columns = [stop_id, stop_name, stop_lat]
        # stop_lat is not in new_header_set → should be empty string
        assert len(fd.row_changes.added) == 1
        added = fd.row_changes.added[0]
        assert added.identifier == {"stop_id": "S2"}
        # Parse raw_value CSV
        import csv
        import io

        row = next(csv.reader(io.StringIO(added.raw_value)))
        # union_columns = base_headers + new_only_cols = [stop_id, stop_name, stop_lat]
        # stop_lat not in new → empty
        assert row[2] == ""  # stop_lat column

    def test_union_columns_order_base_first(self, tmp_path: Path):
        # New feed has an extra column; union_columns must be base_headers + new_only
        base = write_zip(
            tmp_path / "base.zip",
            {
                "stops.txt": "stop_id,stop_name\nS1,Stop One\n",
            },
        )
        new = write_zip(
            tmp_path / "new.zip",
            {
                "stops.txt": "stop_id,stop_name,stop_desc\nS1,Stop One,Desc One\n"
                "S2,Stop Two,Desc Two\n",
            },
        )
        result = diff_feeds(base, new)
        fd = _get_file_diff(result, "stops.txt")
        assert fd.row_changes.columns == ["stop_id", "stop_name", "stop_desc"]


# ---------------------------------------------------------------------------
# Line numbers
# ---------------------------------------------------------------------------


class TestLineNumbers:
    def test_added_row_line_number(self, tmp_path: Path):
        # Header = line 1, first data row = line 2, second data row = line 3
        base = write_zip(
            tmp_path / "base.zip",
            {
                "stops.txt": "stop_id,stop_name\nS1,Stop One\n",
            },
        )
        new = write_zip(
            tmp_path / "new.zip",
            {
                "stops.txt": "stop_id,stop_name\nS1,Stop One\nS2,Stop Two\n",
            },
        )
        result = diff_feeds(base, new)
        fd = _get_file_diff(result, "stops.txt")
        assert len(fd.row_changes.added) == 1
        assert fd.row_changes.added[0].new_line_number == 3

    def test_deleted_row_line_number(self, tmp_path: Path):
        base = write_zip(
            tmp_path / "base.zip",
            {
                "stops.txt": "stop_id,stop_name\nS1,Stop One\nS2,Stop Two\n",
            },
        )
        new = write_zip(
            tmp_path / "new.zip",
            {
                "stops.txt": "stop_id,stop_name\nS1,Stop One\n",
            },
        )
        result = diff_feeds(base, new)
        fd = _get_file_diff(result, "stops.txt")
        assert len(fd.row_changes.deleted) == 1
        assert fd.row_changes.deleted[0].base_line_number == 3

    def test_modified_row_line_numbers(self, tmp_path: Path):
        base = write_zip(
            tmp_path / "base.zip",
            {
                "stops.txt": "stop_id,stop_name\nS1,Stop One\nS2,Stop Two\n",
            },
        )
        new = write_zip(
            tmp_path / "new.zip",
            {
                "stops.txt": "stop_id,stop_name\nS1,Stop One\nS2,Stop Two RENAMED\n",
            },
        )
        result = diff_feeds(base, new)
        fd = _get_file_diff(result, "stops.txt")
        assert len(fd.row_changes.modified) == 1
        mod = fd.row_changes.modified[0]
        assert mod.base_line_number == 3
        assert mod.new_line_number == 3


# ---------------------------------------------------------------------------
# Change statistics
# ---------------------------------------------------------------------------


class TestChangeStats:
    @staticmethod
    def _diff_dirs(
        tmp_path: Path,
        name: str,
        base_files: dict[str, str],
        new_files: dict[str, str],
        **kwargs,
    ) -> GtfsDiff:
        base = _write_feed_dir(tmp_path, f"base_{name}", base_files)
        new = _write_feed_dir(tmp_path, f"new_{name}", new_files)
        return diff_feeds(base, new, **kwargs)

    @staticmethod
    def _column_stats_tuples(fd):
        return [
            (stat.column, stat.modifications_count, stat.modifications_percentage)
            for stat in fd.stats.column_stats
        ]

    def test_basic_column_stats_counts_percentages_and_order(self, tmp_path: Path):
        base = {
            "stops.txt": "stop_id,stop_name,stop_lat,stop_lon,stop_desc\n"
            + "S1,Alpha,1.0,2.0,First\n"
            + "S2,Beta,3.0,4.0,Second\n"
            + "S3,Gamma,5.0,6.0,Third\n"
        }
        new = {
            "stops.txt": "stop_id,stop_name,stop_lat,stop_lon,stop_desc\n"
            + "S1,Alpha Prime,1.1,2.0,First\n"
            + "S2,Beta Prime,3.0,4.0,Second\n"
            + "S3,Gamma,5.0,6.1,Third Prime\n"
        }
        result = self._diff_dirs(tmp_path, "basic_stats", base, new)
        fd = _get_file_diff(result, "stops.txt")

        assert fd.row_changes.columns == [
            "stop_id",
            "stop_name",
            "stop_lat",
            "stop_lon",
            "stop_desc",
        ]
        assert self._column_stats_tuples(fd) == [
            ("stop_name", 2, 66.67),
            ("stop_lat", 1, 33.33),
            ("stop_lon", 1, 33.33),
            ("stop_desc", 1, 33.33),
        ]

    def test_rows_changed_percentage_rounds_and_clamps(self, tmp_path: Path):
        rounded = self._diff_dirs(
            tmp_path,
            "percentage_rounding",
            {
                "stops.txt": "stop_id,stop_name\n"
                + "S1,Alpha\nS2,Beta\nS3,Gamma\nS4,Delta\nS5,Epsilon\n"
            },
            {
                "stops.txt": "stop_id,stop_name\n"
                + "S1,Alpha\nS2,Beta Prime\nS4,Delta\nS5,Epsilon\n"
                + "S6,Zeta\nS7,Eta\n"
            },
        )
        rounded_fd = _get_file_diff(rounded, "stops.txt")
        assert rounded_fd.stats.rows_added_count == 2
        assert rounded_fd.stats.rows_deleted_count == 1
        assert rounded_fd.stats.rows_modified_count == 1
        assert rounded_fd.stats.rows_changed_percentage == 66.67

        clamped = self._diff_dirs(
            tmp_path,
            "percentage_clamping",
            {"stops.txt": "stop_id,stop_name\nA,Alpha\nB,Beta\n"},
            {"stops.txt": "stop_id,stop_name\nC,Gamma\nD,Delta\n"},
        )
        clamped_fd = _get_file_diff(clamped, "stops.txt")
        assert clamped_fd.stats.rows_added_count == 2
        assert clamped_fd.stats.rows_deleted_count == 2
        assert clamped_fd.stats.rows_modified_count == 0
        assert clamped_fd.stats.rows_changed_percentage == 100.0

    def test_rows_changed_percentage_none_for_header_only_modified_file(
        self, tmp_path: Path
    ):
        result = self._diff_dirs(
            tmp_path,
            "empty_modified",
            {"stops.txt": "stop_id,stop_name\n"},
            {"stops.txt": "stop_id,stop_name,stop_desc\n"},
        )
        fd = _get_file_diff(result, "stops.txt")

        assert fd.file_action == "modified"
        assert fd.stats.total_rows_base == 0
        assert fd.stats.total_rows_new == 0
        assert fd.stats.rows_changed_percentage is None
        assert fd.stats.column_stats is None

    def test_column_stats_none_when_no_rows_are_modified(self, tmp_path: Path):
        result = self._diff_dirs(
            tmp_path,
            "no_modified_rows",
            {"stops.txt": "stop_id,stop_name\nS1,Alpha\nS2,Beta\n"},
            {"stops.txt": "stop_id,stop_name\nS1,Alpha\nS3,Gamma\n"},
        )
        fd = _get_file_diff(result, "stops.txt")

        assert fd.file_action == "modified"
        assert fd.stats.rows_added_count == 1
        assert fd.stats.rows_deleted_count == 1
        assert fd.stats.rows_modified_count == 0
        assert fd.stats.column_stats is None
        assert fd.stats.rows_changed_percentage == 100.0

    def test_change_stats_are_independent_of_row_changes_cap(self, tmp_path: Path):
        base_files = {
            "stops.txt": "stop_id,stop_name,stop_lat\n"
            + "S1,Alpha,1.0\n"
            + "S2,Beta,2.0\n"
            + "S3,Gamma,3.0\n"
            + "S4,Delta,4.0\n"
        }
        new_files = {
            "stops.txt": "stop_id,stop_name,stop_lat\n"
            + "S1,Alpha Prime,1.1\n"
            + "S2,Beta Prime,2.2\n"
            + "S3,Gamma Prime,3.0\n"
            + "S4,Delta,4.0\n"
            + "S5,Epsilon,5.0\n"
        }
        base = _write_feed_dir(tmp_path, "base_cap_independence", base_files)
        new = _write_feed_dir(tmp_path, "new_cap_independence", new_files)

        stats_by_cap = []
        for cap in (0, 1, None):
            fd = _get_file_diff(
                diff_feeds(base, new, row_changes_cap_per_file=cap), "stops.txt"
            )
            stats_by_cap.append(
                (
                    fd.stats.rows_changed_percentage,
                    self._column_stats_tuples(fd),
                )
            )

        assert stats_by_cap == [
            (
                80.0,
                [("stop_name", 3, 100.0), ("stop_lat", 2, 66.67)],
            ),
            (
                80.0,
                [("stop_name", 3, 100.0), ("stop_lat", 2, 66.67)],
            ),
            (
                80.0,
                [("stop_name", 3, 100.0), ("stop_lat", 2, 66.67)],
            ),
        ]

    def test_column_stats_toggle_off_keeps_rows_changed_percentage(
        self, tmp_path: Path
    ):
        result = self._diff_dirs(
            tmp_path,
            "toggle_off",
            {
                "stops.txt": "stop_id,stop_name\nS1,Alpha\n",
                "routes.txt": "route_id,route_short_name\nR1,One\n",
            },
            {
                "stops.txt": "stop_id,stop_name\nS1,Alpha Prime\n",
                "routes.txt": "route_id,route_short_name\nR1,One Prime\n",
            },
            column_stats=False,
        )

        modified = [fd for fd in result.file_diffs if fd.file_action == "modified"]
        assert {fd.file_name for fd in modified} == {"routes.txt", "stops.txt"}
        for fd in modified:
            assert fd.stats.column_stats is None
            assert fd.stats.rows_changed_percentage is not None

    def test_change_stats_match_between_in_memory_and_duckdb(self, tmp_path: Path):
        base = {
            "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\n"
            + "S1,Alpha,1.0,2.0\n"
            + "S2,Beta,3.0,4.0\n"
            + "S3,Gamma,5.0,6.0\n"
        }
        new = {
            "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\n"
            + "S1,Alpha Prime,1.1,2.0\n"
            + "S2,Beta,3.0,4.1\n"
            + "S4,Delta,7.0,8.0\n"
        }
        mem, duck = _assert_duckdb_parity(
            tmp_path, base, new, row_changes_cap_per_file=1
        )
        mem_stats = _get_file_diff(mem, "stops.txt").stats
        duck_stats = _get_file_diff(duck, "stops.txt").stats

        assert mem_stats.rows_changed_percentage == duck_stats.rows_changed_percentage
        assert mem_stats.column_stats == duck_stats.column_stats

    def test_non_modified_files_leave_change_stats_unset(self, tmp_path: Path):
        result = self._diff_dirs(
            tmp_path,
            "non_modified",
            {
                "stops.txt": "stop_id,stop_name\nS1,Alpha\n",
                "routes.txt": "route_id,route_short_name\nR1,One\n",
            },
            {
                "stops.txt": "stop_id,stop_name\nS1,Alpha Prime\n",
                "agency.txt": "agency_id,agency_name\nA1,Agency\n",
            },
        )
        added = _get_file_diff(result, "agency.txt")
        deleted = _get_file_diff(result, "routes.txt")

        assert added.file_action == "added"
        assert added.stats.column_stats is None
        assert added.stats.rows_changed_percentage is None
        assert deleted.file_action == "deleted"
        assert deleted.stats.column_stats is None
        assert deleted.stats.rows_changed_percentage is None


# ---------------------------------------------------------------------------
# DuckDB backend parity and routing
# ---------------------------------------------------------------------------


def _write_feed_dir(tmp_path: Path, name: str, files: dict[str, str]) -> Path:
    feed_dir = tmp_path / name
    feed_dir.mkdir()
    for file_name, content in files.items():
        (feed_dir / file_name).write_text(content, encoding="utf-8")
    return feed_dir


def _sorted_diff(result: GtfsDiff) -> dict:
    d = result.model_dump(mode="json", exclude_none=True)
    d.pop("metadata", None)
    for fd in d.get("file_diffs", []):
        rc = fd.get("row_changes")
        if rc:
            for k in ("added", "deleted", "modified"):
                rc[k].sort(key=lambda r: json.dumps(r, sort_keys=True))
    return d


def _assert_duckdb_parity(
    tmp_path: Path,
    base_files: dict[str, str],
    new_files: dict[str, str],
    *,
    row_changes_cap_per_file: int | None = None,
) -> tuple[GtfsDiff, GtfsDiff]:
    base = _write_feed_dir(tmp_path, "base", base_files)
    new = _write_feed_dir(tmp_path, "new", new_files)
    mem = diff_feeds(
        base,
        new,
        row_changes_cap_per_file=row_changes_cap_per_file,
        large_file_threshold_bytes=None,
    )
    duck = diff_feeds(
        base,
        new,
        row_changes_cap_per_file=row_changes_cap_per_file,
        large_file_threshold_bytes=0,
    )
    assert _sorted_diff(mem) == _sorted_diff(duck)
    return mem, duck


class TestDuckDBBackend:
    def test_duckdb_is_available(self):
        assert engine_duckdb.is_duckdb_available() is True

    def test_stops_added_deleted_modified_parity(self, tmp_path: Path):
        base = {
            "stops.txt": STOPS_HEADER
            + "S1,Alpha,1.0,2.0\n"
            + "S2,Beta,3.0,4.0\n"
            + "S3,Gamma,5.0,6.0\n"
        }
        new = {
            "stops.txt": STOPS_HEADER
            + "S1,Alpha Renamed,1.0,2.0\n"
            + "S3,Gamma,5.0,6.0\n"
            + "S4,Delta,7.0,8.0\n"
        }
        _assert_duckdb_parity(tmp_path, base, new)

    def test_numeric_equivalence_parity(self, tmp_path: Path):
        base = {"stops.txt": STOPS_HEADER + "S1,Alpha,45.5,-73.55625\n"}
        new = {"stops.txt": STOPS_HEADER + "S1,Alpha,45.5,-73.556250\n"}
        _assert_duckdb_parity(tmp_path, base, new)

    def test_case_and_whitespace_equivalence_parity(self, tmp_path: Path):
        base = {"stops.txt": "stop_id,stop_name,stop_desc\nS1,Echo, x \n"}
        new = {"stops.txt": "stop_id,stop_name,stop_desc\nS1,ECHO,x\n"}
        _assert_duckdb_parity(tmp_path, base, new)

    def test_quoted_embedded_commas_parity(self, tmp_path: Path):
        base = {"stops.txt": 'stop_id,stop_name,stop_desc\nS1,"Beta, Inc",old\n'}
        new = {"stops.txt": 'stop_id,stop_name,stop_desc\nS1,"Beta, Inc",new\n'}
        _assert_duckdb_parity(tmp_path, base, new)

    def test_empty_fields_and_added_column_parity(self, tmp_path: Path):
        base = {"stops.txt": "stop_id,stop_name\nS1,\nS2,Two\n"}
        new = {"stops.txt": "stop_id,stop_name,stop_desc\nS1,,blank name\nS2,Two,\n"}
        _assert_duckdb_parity(tmp_path, base, new)

    def test_bom_header_parity(self, tmp_path: Path):
        base = {"stops.txt": "\ufeffstop_id,stop_name\nS1,Alpha\n"}
        new = {"stops.txt": "\ufeffstop_id,stop_name\nS1,Alpha Prime\n"}
        _assert_duckdb_parity(tmp_path, base, new)

    def test_composite_primary_key_parity(self, tmp_path: Path):
        header = "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        base = {
            "stop_times.txt": header
            + "T1,08:00:00,08:00:00,S1,1\n"
            + "T1,08:05:00,08:05:00,S2,2\n"
            + "T2,09:00:00,09:00:00,S3,1\n"
        }
        new = {
            "stop_times.txt": header
            + "T1,08:00:00,08:00:00,S1,1\n"
            + "T1,08:06:00,08:06:00,S2,2\n"
            + "T3,10:00:00,10:00:00,S4,1\n"
        }
        _assert_duckdb_parity(tmp_path, base, new)

    def test_modified_line_numbers_parity(self, tmp_path: Path):
        base = {"stops.txt": "stop_id,stop_name\nS1,Alpha\nS2,Beta\nS3,Gamma\n"}
        new = {"stops.txt": "stop_id,stop_name\nS1,Alpha\nS2,Beta Prime\nS3,Gamma\n"}
        mem, duck = _assert_duckdb_parity(tmp_path, base, new)
        for result in (mem, duck):
            mod = _get_file_diff(result, "stops.txt").row_changes.modified[0]
            assert mod.identifier == {"stop_id": "S2"}
            assert mod.base_line_number == 3
            assert mod.new_line_number == 3

    def test_id_churn_parity(self, tmp_path: Path):
        base_rows = "".join(f"B{i},Base {i}\n" for i in range(50))
        new_rows = "".join(f"N{i},New {i}\n" for i in range(50))
        base = {"stops.txt": "stop_id,stop_name\n" + base_rows}
        new = {"stops.txt": "stop_id,stop_name\n" + new_rows}
        mem, duck = _assert_duckdb_parity(tmp_path, base, new)
        for result in (mem, duck):
            fd = _get_file_diff(result, "stops.txt")
            assert fd.file_action == "not_compared"
            assert fd.not_compared_reason is not None
            assert fd.not_compared_reason.code == "id_churn"

    def test_cap_truncation_counts_and_stats_parity(self, tmp_path: Path):
        base = {
            "stops.txt": "stop_id,stop_name\n"
            + "S1,Base One\nS2,Base Two\nS3,Base Three\nS4,Base Four\n"
        }
        new = {
            "stops.txt": "stop_id,stop_name\n"
            + "S1,New One\nS3,Base Three\nS5,New Five\nS6,New Six\n"
        }
        mem, duck = _assert_duckdb_parity(
            tmp_path, base, new, row_changes_cap_per_file=2
        )
        mem_fd = _get_file_diff(mem, "stops.txt")
        duck_fd = _get_file_diff(duck, "stops.txt")
        assert mem_fd.truncated == duck_fd.truncated
        assert mem_fd.truncated is not None
        assert mem_fd.truncated.is_truncated is True
        assert mem_fd.truncated.omitted_count == 3
        assert mem_fd.stats == duck_fd.stats
        assert mem_fd.stats.rows_added_count == 2
        assert mem_fd.stats.rows_deleted_count == 2
        assert mem_fd.stats.rows_modified_count == 1
        assert mem_fd.stats.total_rows_base == 4
        assert mem_fd.stats.total_rows_new == 4

    def test_cap_split_partial_modified_parity(self, tmp_path: Path):
        # 10 modified rows, cap 3 → all 3 budgeted to modified; both engines must
        # select the same earliest-by-line rows.
        base_rows = "".join(f"S{i},Base {i}\n" for i in range(10))
        new_rows = "".join(f"S{i},New {i}\n" for i in range(10))
        base = {"stops.txt": "stop_id,stop_name\n" + base_rows}
        new = {"stops.txt": "stop_id,stop_name\n" + new_rows}
        mem, duck = _assert_duckdb_parity(
            tmp_path, base, new, row_changes_cap_per_file=3
        )
        mem_fd = _get_file_diff(mem, "stops.txt")
        assert len(mem_fd.row_changes.modified) == 3
        assert [m.identifier for m in mem_fd.row_changes.modified] == [
            {"stop_id": "S0"},
            {"stop_id": "S1"},
            {"stop_id": "S2"},
        ]

    def test_cap_zero_omits_row_changes_but_keeps_stats_parity(self, tmp_path: Path):
        base = {"stops.txt": "stop_id,stop_name\nS1,Alpha\n"}
        new = {"stops.txt": "stop_id,stop_name\nS1,Alpha Prime\nS2,Beta\n"}
        mem, duck = _assert_duckdb_parity(
            tmp_path, base, new, row_changes_cap_per_file=0
        )
        for result in (mem, duck):
            fd = _get_file_diff(result, "stops.txt")
            assert fd.row_changes is None
            assert fd.stats.rows_added_count == 1
            assert fd.stats.rows_modified_count == 1

    def test_duplicate_primary_key_raises_in_both_backends(self, tmp_path: Path):
        base = _write_feed_dir(
            tmp_path,
            "base",
            {"stops.txt": "stop_id,stop_name\nS1,Alpha\nS1,Duplicate\n"},
        )
        new = _write_feed_dir(
            tmp_path,
            "new",
            {"stops.txt": "stop_id,stop_name\nS1,Alpha\n"},
        )
        with pytest.raises(ValueError, match="duplicate primary key"):
            diff_feeds(base, new, large_file_threshold_bytes=None)
        with pytest.raises(ValueError, match="duplicate primary key"):
            diff_feeds(base, new, large_file_threshold_bytes=0)

    def test_eligibility_rules(self):
        assert not _eligible_for_duckdb(
            "translations.txt", get_primary_key("translations.txt"), True
        )
        assert _eligible_for_duckdb("stops.txt", get_primary_key("stops.txt"), True)
        assert _eligible_for_duckdb(
            "stop_times.txt", get_primary_key("stop_times.txt"), True
        )
        assert not _eligible_for_duckdb("unknown.txt", [], False)

    def test_threshold_none_never_calls_duckdb_but_zero_does(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        base = _write_feed_dir(
            tmp_path, "base", {"stops.txt": "stop_id,stop_name\nS1,Alpha\n"}
        )
        new = _write_feed_dir(
            tmp_path, "new", {"stops.txt": "stop_id,stop_name\nS1,Beta\n"}
        )

        original = engine_duckdb.diff_modified_duckdb

        def fail_if_called(*args, **kwargs):
            raise AssertionError("DuckDB should not be called")

        monkeypatch.setattr(engine_duckdb, "diff_modified_duckdb", fail_if_called)
        result = diff_feeds(base, new, large_file_threshold_bytes=None)
        assert _get_file_diff(result, "stops.txt").file_action == "modified"

        calls = []
        monkeypatch.undo()

        def record_call(*args, **kwargs):
            calls.append(kwargs["file_name"])
            return original(*args, **kwargs)

        monkeypatch.setattr(engine_duckdb, "diff_modified_duckdb", record_call)
        result = diff_feeds(base, new, large_file_threshold_bytes=0)
        assert _get_file_diff(result, "stops.txt").file_action == "modified"
        assert calls == ["stops.txt"]

    def test_unknown_size_returns_none(self):
        meta = FeedFileMeta(size=None, local_path="unused.txt")
        assert (
            _maybe_diff_modified_duckdb(
                file_name="stops.txt",
                pk_def=get_primary_key("stops.txt"),
                pk_is_explicit=True,
                base_meta=meta,
                new_meta=meta,
                large_file_threshold_bytes=0,
                use_duckdb=True,
                row_changes_cap=None,
                id_churn_threshold=0.7,
                not_compared_files={},
            )
            is None
        )

    def test_duckdb_unavailable_falls_back_to_in_memory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        base_files = {"stops.txt": "stop_id,stop_name\nS1,Alpha\n"}
        new_files = {"stops.txt": "stop_id,stop_name\nS1,Beta\nS2,Gamma\n"}
        base = _write_feed_dir(tmp_path, "base", base_files)
        new = _write_feed_dir(tmp_path, "new", new_files)
        mem = diff_feeds(base, new, large_file_threshold_bytes=None)
        monkeypatch.setattr(engine_duckdb, "is_duckdb_available", lambda: False)
        fallback = diff_feeds(base, new, large_file_threshold_bytes=0)
        assert _sorted_diff(mem) == _sorted_diff(fallback)


class TestDuckDBSpillBase:
    def test_unset_env_returns_none(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv(DUCKDB_TMPDIR_ENV, raising=False)

        assert _resolve_spill_base() is None

    def test_empty_env_returns_none(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv(DUCKDB_TMPDIR_ENV, "")

        assert _resolve_spill_base() is None

    def test_whitespace_env_returns_none(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv(DUCKDB_TMPDIR_ENV, "   ")

        assert _resolve_spill_base() is None

    def test_existing_directory_returns_stripped_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv(DUCKDB_TMPDIR_ENV, f"  {tmp_path}  ")

        assert _resolve_spill_base() == str(tmp_path)

    def test_nested_directory_is_created(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        spill_base = tmp_path / "nested" / "spill"
        monkeypatch.setenv(DUCKDB_TMPDIR_ENV, str(spill_base))

        assert _resolve_spill_base() == str(spill_base)
        assert os.path.isdir(spill_base)

    def test_leading_tilde_is_expanded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        fake_home = tmp_path / "home"

        def fake_expanduser(path: str) -> str:
            return path.replace("~", str(fake_home), 1)

        monkeypatch.setattr(os.path, "expanduser", fake_expanduser)
        monkeypatch.setenv(DUCKDB_TMPDIR_ENV, "~/some_subdir_unlikely")

        resolved = _resolve_spill_base()

        assert resolved is not None
        assert not resolved.startswith("~")
        assert resolved.startswith(str(fake_home))
        assert os.path.isdir(resolved)


class TestDuckDBRemoteUrl:
    """The DuckDB backend reads remote files in place via httpfs (no download)."""

    def test_open_remote_feed_sets_url_on_meta(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("gtfs_diff.engine._http_exists", lambda url: True)
        monkeypatch.setattr("gtfs_diff.engine._http_content_length", lambda url: 12_345)
        handle = _open_remote_feed("https://x/base", ["stops.txt"])
        meta = handle.meta["stops.txt"]
        assert meta.url == "https://x/base/stops.txt"
        assert meta.local_path is None
        assert meta.size == 12_345

    def test_materialized_path_yields_url_without_downloading(self):
        def boom(dest: str) -> None:
            raise AssertionError("a URL must never be materialized to disk")

        meta = FeedFileMeta(size=1, url="https://x/base/stops.txt", materialize=boom)
        with _materialized_path(meta) as path:
            assert path == "https://x/base/stops.txt"

    def test_is_remote(self):
        assert engine_duckdb._is_url("https://x/base/stops.txt")
        assert engine_duckdb._is_url("http://x/base/stops.txt")
        assert not engine_duckdb._is_url("/tmp/base/stops.txt")

    def test_read_headers_via_duckdb_strips_bom_and_whitespace(self, tmp_path: Path):
        import duckdb

        p = tmp_path / "stops.txt"
        p.write_text("\ufeff stop_id , stop_name \nS1,Alpha\n", encoding="utf-8")
        con = duckdb.connect()
        try:
            assert engine_duckdb._read_headers_via_duckdb(con, str(p)) == [
                "stop_id",
                "stop_name",
            ]
        finally:
            con.close()

    def test_duckdb_receives_url_and_never_downloads(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        from gtfs_diff.models import FileDiff, FileStats, FileSummary

        monkeypatch.setattr("gtfs_diff.engine._http_exists", lambda url: True)
        monkeypatch.setattr("gtfs_diff.engine._http_content_length", lambda url: 10_000)

        def no_download(url: str, dest: str) -> None:
            raise AssertionError("remote DuckDB path must not download to a temp file")

        monkeypatch.setattr("gtfs_diff.engine._http_stream_to_file", no_download)

        captured: dict = {}

        def fake_duckdb(**kwargs):
            captured.update(kwargs)
            fd = FileDiff(
                file_name=kwargs["file_name"],
                file_action="modified",
                columns_added=[],
                columns_deleted=[],
                stats=FileStats(columns_added_count=0, columns_deleted_count=0),
            )
            return fd, FileSummary(file_name=kwargs["file_name"], status="modified")

        monkeypatch.setattr(engine_duckdb, "diff_modified_duckdb", fake_duckdb)

        diff_feeds(
            "https://x/base",
            "https://x/new",
            files=["stops.txt"],
            large_file_threshold_bytes=0,
        )

        # The raw URLs (not a staged temp path) are handed straight to DuckDB.
        assert captured["base_path"] == "https://x/base/stops.txt"
        assert captured["new_path"] == "https://x/new/stops.txt"

    def test_remote_duckdb_reads_via_httpfs_live(self, tmp_path: Path):
        """End-to-end: DuckDB reads a real local HTTP URL via httpfs.

        Skipped when the httpfs extension cannot be installed/loaded (e.g. no
        network and not already cached).
        """
        import functools
        import http.server
        import socketserver
        import threading

        duckdb = pytest.importorskip("duckdb")
        con = duckdb.connect()
        try:
            con.execute("INSTALL httpfs")
            con.execute("LOAD httpfs")
        except Exception:
            pytest.skip("httpfs extension unavailable (no network / not cached)")
        finally:
            con.close()

        base = _write_feed_dir(
            tmp_path,
            "base",
            {"stops.txt": "stop_id,stop_name\nS1,Alpha\nS2,Beta\nS3,Gamma\n"},
        )
        new = _write_feed_dir(
            tmp_path,
            "new",
            {"stops.txt": "stop_id,stop_name\nS1,Alpha\nS2,Renamed\nS4,Delta\n"},
        )

        handler = functools.partial(
            http.server.SimpleHTTPRequestHandler, directory=str(tmp_path)
        )
        httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            remote = diff_feeds(
                f"http://127.0.0.1:{port}/base",
                f"http://127.0.0.1:{port}/new",
                files=["stops.txt"],
                large_file_threshold_bytes=0,
            )
        finally:
            httpd.shutdown()

        # Parity: reading the same files over httpfs must match the local diff.
        local = diff_feeds(base, new, large_file_threshold_bytes=0)
        assert _sorted_diff(remote) == _sorted_diff(local)


class TestDuckDBResourceCleanup:
    """The DuckDB backend frees per-file tables and spill on completion."""

    def test_no_spill_dir_leaks_and_no_tmp_in_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import glob
        import tempfile as _tempfile

        # Run from an isolated CWD so DuckDB's default ``.tmp`` spill dir would
        # be visible here if temp_directory were not redirected.
        workdir = tmp_path / "cwd"
        workdir.mkdir()
        monkeypatch.chdir(workdir)

        base = _write_feed_dir(
            tmp_path, "base", {"stops.txt": "stop_id,stop_name\nS1,A\nS2,B\n"}
        )
        new = _write_feed_dir(
            tmp_path, "new", {"stops.txt": "stop_id,stop_name\nS1,A\nS2,C\n"}
        )

        pattern = str(Path(_tempfile.gettempdir()) / "gtfs_duckdb_*")
        before = set(glob.glob(pattern))

        result = diff_feeds(base, new, large_file_threshold_bytes=0)

        assert _get_file_diff(result, "stops.txt").file_action == "modified"
        # The managed spill directory must be removed after the diff.
        assert set(glob.glob(pattern)) == before
        # DuckDB's default CWD spill directory must never be created.
        assert not (workdir / ".tmp").exists()
