"""Tests for gtfs_diff.engine — diff_feeds()."""

from __future__ import annotations

from pathlib import Path

import pytest

from gtfs_diff.engine import MissingPrimaryKeyError, diff_feeds
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
        base = write_zip(tmp_path / "base.zip", {
            "stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\n",
        })
        new = write_zip(tmp_path / "new.zip", {
            "stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\n",
            "routes.txt": "route_id,route_short_name\nR1,Route 1\n",
        })
        result = diff_feeds(base, new)
        fd = _get_file_diff(result, "routes.txt")
        assert fd.file_action == "added"
        assert len(fd.columns_added) == 2
        column_names = [c.name for c in fd.columns_added]
        assert "route_id" in column_names
        assert "route_short_name" in column_names

    def test_file_added_summary_status(self, tmp_path: Path):
        base = write_zip(tmp_path / "base.zip", {
            "stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\n",
        })
        new = write_zip(tmp_path / "new.zip", {
            "stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\n",
            "routes.txt": "route_id,route_short_name\nR1,Route 1\n",
        })
        result = diff_feeds(base, new)
        fs = _get_file_summary(result, "routes.txt")
        assert fs.status == "added"
        assert result.summary.files_added_count == 1


class TestFileDeleted:
    def test_file_deleted(self, tmp_path: Path):
        base = write_zip(tmp_path / "base.zip", {
            "stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\n",
            "routes.txt": "route_id,route_short_name\nR1,Route 1\n",
        })
        new = write_zip(tmp_path / "new.zip", {
            "stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\n",
        })
        result = diff_feeds(base, new)
        fd = _get_file_diff(result, "routes.txt")
        assert fd.file_action == "deleted"
        assert len(fd.columns_deleted) == 2
        column_names = [c.name for c in fd.columns_deleted]
        assert "route_id" in column_names

    def test_file_deleted_summary_status(self, tmp_path: Path):
        base = write_zip(tmp_path / "base.zip", {
            "stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\n",
            "routes.txt": "route_id,route_short_name\nR1,Route 1\n",
        })
        new = write_zip(tmp_path / "new.zip", {
            "stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\n",
        })
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
        base = write_zip(tmp_path / "base.zip", {
            "stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\n",
        })
        new = write_zip(tmp_path / "new.zip", {
            "stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\nS2,Stop Two,3.0,4.0\n",
        })
        result = diff_feeds(base, new)
        fs = _get_file_summary(result, "stops.txt")
        assert fs.rows_added_count == 1

    def test_rows_added_identifier(self, tmp_path: Path):
        base = write_zip(tmp_path / "base.zip", {
            "stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\n",
        })
        new = write_zip(tmp_path / "new.zip", {
            "stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\nS2,Stop Two,3.0,4.0\n",
        })
        result = diff_feeds(base, new)
        fd = _get_file_diff(result, "stops.txt")
        assert len(fd.row_changes.added) == 1
        assert fd.row_changes.added[0].identifier == {"stop_id": "S2"}


class TestRowsDeleted:
    def test_rows_deleted_count(self, tmp_path: Path):
        base = write_zip(tmp_path / "base.zip", {
            "stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\nS2,Stop Two,3.0,4.0\n",
        })
        new = write_zip(tmp_path / "new.zip", {
            "stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\n",
        })
        result = diff_feeds(base, new)
        fs = _get_file_summary(result, "stops.txt")
        assert fs.rows_deleted_count == 1

    def test_rows_deleted_identifier(self, tmp_path: Path):
        base = write_zip(tmp_path / "base.zip", {
            "stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\nS2,Stop Two,3.0,4.0\n",
        })
        new = write_zip(tmp_path / "new.zip", {
            "stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\n",
        })
        result = diff_feeds(base, new)
        fd = _get_file_diff(result, "stops.txt")
        assert len(fd.row_changes.deleted) == 1
        assert fd.row_changes.deleted[0].identifier == {"stop_id": "S2"}


class TestRowsModified:
    def test_rows_modified_count(self, tmp_path: Path):
        base = write_zip(tmp_path / "base.zip", {
            "stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\n",
        })
        new = write_zip(tmp_path / "new.zip", {
            "stops.txt": STOPS_HEADER + "S1,Stop One Renamed,1.0,2.0\n",
        })
        result = diff_feeds(base, new)
        fs = _get_file_summary(result, "stops.txt")
        assert fs.rows_modified_count == 1

    def test_rows_modified_field_changes(self, tmp_path: Path):
        base = write_zip(tmp_path / "base.zip", {
            "stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\n",
        })
        new = write_zip(tmp_path / "new.zip", {
            "stops.txt": STOPS_HEADER + "S1,Stop One Renamed,1.0,2.0\n",
        })
        result = diff_feeds(base, new)
        fd = _get_file_diff(result, "stops.txt")
        assert len(fd.row_changes.modified) == 1
        mod = fd.row_changes.modified[0]
        assert mod.identifier == {"stop_id": "S1"}
        field_names = [fc.field for fc in mod.field_changes]
        assert "stop_name" in field_names
        stop_name_change = next(fc for fc in mod.field_changes if fc.field == "stop_name")
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
        base = write_zip(tmp_path / "base.zip", {
            "stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\nS2,Stop Two,3.0,4.0\n",
        })
        new = write_zip(tmp_path / "new.zip", {
            "stops.txt": STOPS_HEADER + "S2,Stop Two,3.0,4.0\nS1,Stop One,1.0,2.0\n",
        })
        result = diff_feeds(base, new)
        assert result.file_diffs == []
        assert result.summary.total_changes == 0

    def test_trailing_zeros_in_coordinates_are_not_a_change(self, tmp_path: Path):
        # A producer may write '-73.55625' in one version and '-73.556250' in the
        # next. These are numerically identical and must not be reported as a diff.
        base = write_zip(tmp_path / "base.zip", {
            "shapes.txt": (
                "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\n"
                "11071,45.518332,-73.55625,150001\n"
            ),
        })
        new = write_zip(tmp_path / "new.zip", {
            "shapes.txt": (
                "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\n"
                "11071,45.518332,-73.556250,150001\n"
            ),
        })
        result = diff_feeds(base, new)
        assert result.file_diffs == []
        assert result.summary.total_changes == 0

    def test_swapped_column_order_is_not_a_change(self, tmp_path: Path):
        # Column order is irrelevant — the engine compares values by column name.
        # Swapping two columns must not produce any diff.
        # NOTE: should a column reorder be reported as a structural change even when
        # no field values differ? This is currently an open design question.
        base = write_zip(tmp_path / "base.zip", {
            "stops.txt": "stop_id,stop_name,stop_lat\nS1,Stop One,1.0\n",
        })
        new = write_zip(tmp_path / "new.zip", {
            "stops.txt": "stop_name,stop_id,stop_lat\nStop One,S1,1.0\n",
        })
        result = diff_feeds(base, new)
        assert result.file_diffs == []
        assert result.summary.total_changes == 0


# ---------------------------------------------------------------------------
# Column-level tests
# ---------------------------------------------------------------------------

class TestColumnAdded:
    def test_column_added(self, tmp_path: Path):
        base = write_zip(tmp_path / "base.zip", {
            "stops.txt": "stop_id,stop_name\nS1,Stop One\n",
        })
        new = write_zip(tmp_path / "new.zip", {
            "stops.txt": "stop_id,stop_name,stop_desc\nS1,Stop One,A description\n",
        })
        result = diff_feeds(base, new)
        fd = _get_file_diff(result, "stops.txt")
        added_names = [c.name for c in fd.columns_added]
        assert "stop_desc" in added_names

    def test_column_added_position(self, tmp_path: Path):
        base = write_zip(tmp_path / "base.zip", {
            "stops.txt": "stop_id,stop_name\nS1,Stop One\n",
        })
        new = write_zip(tmp_path / "new.zip", {
            "stops.txt": "stop_id,stop_name,stop_desc\nS1,Stop One,A description\n",
        })
        result = diff_feeds(base, new)
        fd = _get_file_diff(result, "stops.txt")
        stop_desc_col = next(c for c in fd.columns_added if c.name == "stop_desc")
        assert stop_desc_col.position == 3


class TestColumnDeleted:
    def test_column_deleted(self, tmp_path: Path):
        base = write_zip(tmp_path / "base.zip", {
            "stops.txt": "stop_id,stop_name,stop_desc\nS1,Stop One,A description\n",
        })
        new = write_zip(tmp_path / "new.zip", {
            "stops.txt": "stop_id,stop_name\nS1,Stop One\n",
        })
        result = diff_feeds(base, new)
        fd = _get_file_diff(result, "stops.txt")
        deleted_names = [c.name for c in fd.columns_deleted]
        assert "stop_desc" in deleted_names

    def test_column_deleted_position(self, tmp_path: Path):
        base = write_zip(tmp_path / "base.zip", {
            "stops.txt": "stop_id,stop_name,stop_desc\nS1,Stop One,A description\n",
        })
        new = write_zip(tmp_path / "new.zip", {
            "stops.txt": "stop_id,stop_name\nS1,Stop One\n",
        })
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
        base = write_zip(tmp_path / "base.zip", {
            "stops.txt": "stop_id,stop_name\nS1,Stop One\n",
        })
        new = write_zip(tmp_path / "new.zip", {
            "stops.txt": "stop_id,stop_name\nS1,Stop One\nS2,Stop Two\n",
        })
        result = diff_feeds(base, new, row_changes_cap_per_file=0)
        fd = _get_file_diff(result, "stops.txt")
        assert fd.row_changes is None


class TestCapLimits:
    def test_cap_limits_row_changes(self, tmp_path: Path):
        # 5 new rows added, cap = 3 → 3 included, omitted_count = 2
        base = write_zip(tmp_path / "base.zip", {
            "stops.txt": "stop_id,stop_name\nS0,Stop Zero\n",
        })
        new = write_zip(tmp_path / "new.zip", {
            "stops.txt": "stop_id,stop_name\n" + "".join(f"S{i},Stop {i}\n" for i in range(1, 7)),
        })
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
        base = write_zip(tmp_path / "base.zip", {
            "stops.txt": _make_stops_csv(0).replace("\n", "", 1),  # header only
        })
        # Write header-only base
        base = write_zip(tmp_path / "base2.zip", {
            "stops.txt": "stop_id,stop_name\n",
        })
        new = write_zip(tmp_path / "new.zip", {
            "stops.txt": _make_stops_csv(5),
        })
        result = diff_feeds(base, new, row_changes_cap_per_file=3)
        fd = _get_file_diff(result, "stops.txt")
        assert len(fd.row_changes.added) == 3
        assert fd.truncated.omitted_count == 2


class TestCapNone:
    def test_cap_none_includes_all(self, tmp_path: Path):
        base = write_zip(tmp_path / "base.zip", {
            "stops.txt": "stop_id,stop_name\n",
        })
        new = write_zip(tmp_path / "new.zip", {
            "stops.txt": _make_stops_csv(5),
        })
        result = diff_feeds(base, new, row_changes_cap_per_file=None)
        fd = _get_file_diff(result, "stops.txt")
        assert len(fd.row_changes.added) == 5
        assert fd.truncated is None


# ---------------------------------------------------------------------------
# Missing primary key column
# ---------------------------------------------------------------------------

class TestMissingPrimaryKeyError:
    def test_missing_pk_column_in_base_raises(self, tmp_path: Path):
        """diff_feeds raises MissingPrimaryKeyError when the base feed is missing a required PK column."""
        base = write_zip(tmp_path / "base.zip", {
            "stops.txt": "stop_name,stop_lat,stop_lon\nStop One,1.0,2.0\n",  # stop_id absent
        })
        new = write_zip(tmp_path / "new.zip", {
            "stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\n",
        })
        with pytest.raises(MissingPrimaryKeyError):
            diff_feeds(base, new)

    def test_missing_pk_column_in_new_raises(self, tmp_path: Path):
        """diff_feeds raises MissingPrimaryKeyError when the new feed is missing a required PK column."""
        base = write_zip(tmp_path / "base.zip", {
            "stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\n",
        })
        new = write_zip(tmp_path / "new.zip", {
            "stops.txt": "stop_name,stop_lat,stop_lon\nStop One,1.0,2.0\n",  # stop_id absent
        })
        with pytest.raises(MissingPrimaryKeyError):
            diff_feeds(base, new)

    def test_exception_carries_file_name(self, tmp_path: Path):
        base = write_zip(tmp_path / "base.zip", {
            "stops.txt": "stop_name,stop_lat,stop_lon\nStop One,1.0,2.0\n",
        })
        new = write_zip(tmp_path / "new.zip", {
            "stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\n",
        })
        with pytest.raises(MissingPrimaryKeyError) as exc_info:
            diff_feeds(base, new)
        assert exc_info.value.file_name == "stops.txt"

    def test_exception_carries_missing_columns(self, tmp_path: Path):
        base = write_zip(tmp_path / "base.zip", {
            "stops.txt": "stop_name,stop_lat,stop_lon\nStop One,1.0,2.0\n",
        })
        new = write_zip(tmp_path / "new.zip", {
            "stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\n",
        })
        with pytest.raises(MissingPrimaryKeyError) as exc_info:
            diff_feeds(base, new)
        assert "stop_id" in exc_info.value.missing_columns

    def test_exception_carries_headers(self, tmp_path: Path):
        base = write_zip(tmp_path / "base.zip", {
            "stops.txt": "stop_name,stop_lat,stop_lon\nStop One,1.0,2.0\n",
        })
        new = write_zip(tmp_path / "new.zip", {
            "stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\n",
        })
        with pytest.raises(MissingPrimaryKeyError) as exc_info:
            diff_feeds(base, new)
        assert exc_info.value.headers == ["stop_name", "stop_lat", "stop_lon"]


# ---------------------------------------------------------------------------
# Unsupported files
# ---------------------------------------------------------------------------

class TestUnsupportedFile:
    def test_unsupported_file_in_metadata(self, tmp_path: Path):
        base = write_zip(tmp_path / "base.zip", {
            "stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\n",
            "custom_data.txt": "foo,bar\n1,2\n",
        })
        new = write_zip(tmp_path / "new.zip", {
            "stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\n",
            "custom_data.txt": "foo,bar\n1,2\n",
        })
        result = diff_feeds(base, new)
        unsupported_names = [u.file_name for u in result.metadata.unsupported_files]
        assert "custom_data.txt" in unsupported_names

    def test_unsupported_file_present_in_both(self, tmp_path: Path):
        base = write_zip(tmp_path / "base.zip", {
            "stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\n",
            "custom_data.txt": "foo,bar\n1,2\n",
        })
        new = write_zip(tmp_path / "new.zip", {
            "stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\n",
            "custom_data.txt": "foo,bar\n1,2\n",
        })
        result = diff_feeds(base, new)
        uf = next(u for u in result.metadata.unsupported_files if u.file_name == "custom_data.txt")
        assert uf.present_in == "both"


# ---------------------------------------------------------------------------
# Schema validation (model round-trip)
# ---------------------------------------------------------------------------

class TestOutputMatchesSchema:
    def test_output_matches_schema(self, tmp_path: Path):
        base = write_zip(tmp_path / "base.zip", {
            "stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\n",
        })
        new = write_zip(tmp_path / "new.zip", {
            "stops.txt": STOPS_HEADER + "S1,Stop One,1.0,2.0\nS2,Stop Two,3.0,4.0\n",
        })
        result = diff_feeds(base, new)
        json_str = result.model_dump_json()
        restored = GtfsDiff.model_validate_json(json_str)
        assert restored.summary.total_changes >= 1
        assert restored.metadata.schema_version == "2.0"


# ---------------------------------------------------------------------------
# raw_value column ordering
# ---------------------------------------------------------------------------

class TestRawValueColumnOrder:
    def test_raw_value_has_empty_for_base_only_column(self, tmp_path: Path):
        # base has stop_lat; new does NOT have stop_lat (deleted column).
        # Added rows in new should have empty string for stop_lat in raw_value.
        base = write_zip(tmp_path / "base.zip", {
            "stops.txt": "stop_id,stop_name,stop_lat\nS1,Stop One,1.0\n",
        })
        new = write_zip(tmp_path / "new.zip", {
            "stops.txt": "stop_id,stop_name\nS1,Stop One\nS2,Stop Two\n",
        })
        result = diff_feeds(base, new)
        fd = _get_file_diff(result, "stops.txt")
        # S2 is an added row; union_columns = [stop_id, stop_name, stop_lat]
        # stop_lat is not in new_header_set → should be empty string
        assert len(fd.row_changes.added) == 1
        added = fd.row_changes.added[0]
        assert added.identifier == {"stop_id": "S2"}
        # Parse raw_value CSV
        import csv, io
        row = next(csv.reader(io.StringIO(added.raw_value)))
        # union_columns = base_headers + new_only_cols = [stop_id, stop_name, stop_lat]
        # stop_lat not in new → empty
        assert row[2] == ""  # stop_lat column

    def test_union_columns_order_base_first(self, tmp_path: Path):
        # New feed has an extra column; union_columns must be base_headers + new_only
        base = write_zip(tmp_path / "base.zip", {
            "stops.txt": "stop_id,stop_name\nS1,Stop One\n",
        })
        new = write_zip(tmp_path / "new.zip", {
            "stops.txt": "stop_id,stop_name,stop_desc\nS1,Stop One,Desc One\nS2,Stop Two,Desc Two\n",
        })
        result = diff_feeds(base, new)
        fd = _get_file_diff(result, "stops.txt")
        assert fd.row_changes.columns == ["stop_id", "stop_name", "stop_desc"]


# ---------------------------------------------------------------------------
# Line numbers
# ---------------------------------------------------------------------------

class TestLineNumbers:
    def test_added_row_line_number(self, tmp_path: Path):
        # Header = line 1, first data row = line 2, second data row = line 3
        base = write_zip(tmp_path / "base.zip", {
            "stops.txt": "stop_id,stop_name\nS1,Stop One\n",
        })
        new = write_zip(tmp_path / "new.zip", {
            "stops.txt": "stop_id,stop_name\nS1,Stop One\nS2,Stop Two\n",
        })
        result = diff_feeds(base, new)
        fd = _get_file_diff(result, "stops.txt")
        assert len(fd.row_changes.added) == 1
        assert fd.row_changes.added[0].new_line_number == 3

    def test_deleted_row_line_number(self, tmp_path: Path):
        base = write_zip(tmp_path / "base.zip", {
            "stops.txt": "stop_id,stop_name\nS1,Stop One\nS2,Stop Two\n",
        })
        new = write_zip(tmp_path / "new.zip", {
            "stops.txt": "stop_id,stop_name\nS1,Stop One\n",
        })
        result = diff_feeds(base, new)
        fd = _get_file_diff(result, "stops.txt")
        assert len(fd.row_changes.deleted) == 1
        assert fd.row_changes.deleted[0].base_line_number == 3

    def test_modified_row_line_numbers(self, tmp_path: Path):
        base = write_zip(tmp_path / "base.zip", {
            "stops.txt": "stop_id,stop_name\nS1,Stop One\nS2,Stop Two\n",
        })
        new = write_zip(tmp_path / "new.zip", {
            "stops.txt": "stop_id,stop_name\nS1,Stop One\nS2,Stop Two RENAMED\n",
        })
        result = diff_feeds(base, new)
        fd = _get_file_diff(result, "stops.txt")
        assert len(fd.row_changes.modified) == 1
        mod = fd.row_changes.modified[0]
        assert mod.base_line_number == 3
        assert mod.new_line_number == 3
