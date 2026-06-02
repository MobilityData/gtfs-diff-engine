"""Tests for gtfs-diff.models."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from gtfs_diff.models import (
    ColumnEntry,
    FieldChange,
    FileDiff,
    FileStats,
    FileSummary,
    GtfsDiff,
    Metadata,
    FeedSource,
    RowAdded,
    RowChanges,
    RowDeleted,
    RowModified,
    Summary,
    Truncated,
    UnsupportedFile,
)

NOW = datetime(2024, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Helpers to build valid model instances
# ---------------------------------------------------------------------------

def _feed_source(url: str = "http://example.com/feed.zip") -> FeedSource:
    return FeedSource(source=url, downloaded_at=NOW)


def _metadata(**kwargs) -> Metadata:
    defaults = dict(
        schema_version="2.0",
        generated_at=NOW,
        row_changes_cap_per_file=None,
        base_feed=_feed_source("http://base"),
        new_feed=_feed_source("http://new"),
        unsupported_files=[],
    )
    defaults.update(kwargs)
    return Metadata(**defaults)


def _file_diff(**kwargs) -> FileDiff:
    defaults = dict(
        file_name="stops.txt",
        file_action="modified",
        columns_added=[],
        columns_deleted=[],
        row_changes=None,
        truncated=None,
    )
    defaults.update(kwargs)
    return FileDiff(**defaults)


def _summary(**kwargs) -> Summary:
    defaults = dict(
        total_changes=0,
        files_added_count=0,
        files_deleted_count=0,
        files_modified_count=0,
        files_not_compared_count=0,
        files=[],
    )
    defaults.update(kwargs)
    return Summary(**defaults)


def _gtfs_diff(**kwargs) -> GtfsDiff:
    defaults = dict(
        metadata=_metadata(),
        summary=_summary(),
        file_diffs=[],
    )
    defaults.update(kwargs)
    return GtfsDiff(**defaults)


# ---------------------------------------------------------------------------
# GtfsDiff round-trip
# ---------------------------------------------------------------------------

class TestGtfsDiffRoundTrip:
    def test_round_trip_empty(self):
        obj = _gtfs_diff()
        dumped = obj.model_dump()
        restored = GtfsDiff.model_validate(dumped)
        assert restored == obj

    def test_round_trip_with_file_diff(self):
        file_diff = _file_diff(
            file_action="added",
            columns_added=[ColumnEntry(name="stop_id", position=1)],
        )
        obj = _gtfs_diff(
            summary=_summary(files_added_count=1, total_changes=1),
            file_diffs=[file_diff],
        )
        restored = GtfsDiff.model_validate(obj.model_dump())
        assert restored.file_diffs[0].file_action == "added"

    def test_round_trip_json(self):
        obj = _gtfs_diff()
        json_str = obj.model_dump_json()
        restored = GtfsDiff.model_validate_json(json_str)
        assert restored.metadata.schema_version == "2.0"


# ---------------------------------------------------------------------------
# ColumnEntry
# ---------------------------------------------------------------------------

class TestColumnEntry:
    def test_valid(self):
        col = ColumnEntry(name="stop_id", position=1)
        assert col.name == "stop_id"
        assert col.position == 1

    def test_position_zero_rejected(self):
        with pytest.raises(ValidationError):
            ColumnEntry(name="stop_id", position=0)

    def test_position_negative_rejected(self):
        with pytest.raises(ValidationError):
            ColumnEntry(name="stop_id", position=-5)

    def test_position_one_accepted(self):
        col = ColumnEntry(name="stop_id", position=1)
        assert col.position == 1


# ---------------------------------------------------------------------------
# RowChanges
# ---------------------------------------------------------------------------

class TestRowChanges:
    def test_valid(self):
        rc = RowChanges(
            primary_key=["stop_id"],
            columns=["stop_id", "stop_name"],
            added=[],
            deleted=[],
            modified=[],
        )
        assert rc.primary_key == ["stop_id"]

    def test_empty_primary_key_rejected(self):
        with pytest.raises(ValidationError):
            RowChanges(
                primary_key=[],
                columns=["stop_id"],
                added=[],
                deleted=[],
                modified=[],
            )


# ---------------------------------------------------------------------------
# RowModified
# ---------------------------------------------------------------------------

class TestRowModified:
    def test_valid(self):
        rm = RowModified(
            identifier={"stop_id": "S1"},
            raw_value="S1,Old Name",
            base_line_number=2,
            new_line_number=2,
            field_changes=[
                FieldChange(field="stop_name", base_value="Old", new_value="New")
            ],
        )
        assert len(rm.field_changes) == 1

    def test_empty_field_changes_rejected(self):
        with pytest.raises(ValidationError):
            RowModified(
                identifier={"stop_id": "S1"},
                raw_value="S1",
                base_line_number=2,
                new_line_number=2,
                field_changes=[],
            )


# ---------------------------------------------------------------------------
# FileSummary
# ---------------------------------------------------------------------------

class TestFileSummary:
    def test_valid(self):
        fs = FileSummary(file_name="stops.txt", status="modified")
        assert fs.file_name == "stops.txt"
        assert fs.status == "modified"

    def test_not_compared_status(self):
        fs = FileSummary(file_name="stops.txt", status="not_compared")
        assert fs.status == "not_compared"


class TestFileStats:
    def test_all_optional_counts_none(self):
        stats = FileStats()
        assert stats.rows_added_count is None
        assert stats.rows_deleted_count is None
        assert stats.rows_modified_count is None
        assert stats.columns_added_count is None
        assert stats.columns_deleted_count is None

    def test_with_counts(self):
        stats = FileStats(
            rows_added_count=3,
            rows_deleted_count=1,
            rows_modified_count=0,
        )
        assert stats.rows_added_count == 3


# ---------------------------------------------------------------------------
# Truncated
# ---------------------------------------------------------------------------

class TestTruncated:
    def test_valid(self):
        t = Truncated(is_truncated=True, omitted_count=5)
        assert t.is_truncated is True
        assert t.omitted_count == 5

    def test_is_truncated_must_be_true(self):
        with pytest.raises(ValidationError):
            Truncated(is_truncated=False, omitted_count=1)  # type: ignore[arg-type]

    def test_omitted_count_must_be_positive(self):
        with pytest.raises(ValidationError):
            Truncated(is_truncated=True, omitted_count=0)


# ---------------------------------------------------------------------------
# UnsupportedFile
# ---------------------------------------------------------------------------

class TestUnsupportedFile:
    @pytest.mark.parametrize("present_in", ["base", "new", "both"])
    def test_valid_present_in(self, present_in: str):
        uf = UnsupportedFile(file_name="custom.txt", present_in=present_in)  # type: ignore[arg-type]
        assert uf.present_in == present_in

    def test_invalid_present_in(self):
        with pytest.raises(ValidationError):
            UnsupportedFile(file_name="custom.txt", present_in="neither")  # type: ignore[arg-type]
