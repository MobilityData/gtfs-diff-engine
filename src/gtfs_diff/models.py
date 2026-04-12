"""Pydantic v2 models for the GTFS Diff v2 output format."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class ColumnEntry(BaseModel):
    """A column that was added or deleted, with its name and original position."""

    model_config = ConfigDict(populate_by_name=True)

    name: str
    position: int = Field(..., ge=1)


class FeedSource(BaseModel):
    """Identifies a GTFS feed by its source URL and the time it was downloaded."""

    model_config = ConfigDict(populate_by_name=True)

    source: str
    downloaded_at: datetime


class UnsupportedFile(BaseModel):
    """A file present in one or both feeds that the diff engine does not support."""

    model_config = ConfigDict(populate_by_name=True)

    file_name: str
    present_in: Literal["base", "new", "both"]


class Metadata(BaseModel):
    """Top-level metadata describing the diff run and both feed sources."""

    model_config = ConfigDict(populate_by_name=True)

    schema_version: str
    generated_at: datetime
    row_changes_cap_per_file: Optional[int] = Field(None, ge=0)
    base_feed: FeedSource
    new_feed: FeedSource
    unsupported_files: list[UnsupportedFile]


class FileSummary(BaseModel):
    """High-level change counts for a single GTFS file."""

    model_config = ConfigDict(populate_by_name=True)

    file_name: str
    status: Literal["added", "deleted", "modified"]
    columns_added_count: Optional[int] = Field(None, ge=0)
    columns_deleted_count: Optional[int] = Field(None, ge=0)
    rows_added_count: Optional[int] = Field(None, ge=0)
    rows_deleted_count: Optional[int] = Field(None, ge=0)
    rows_modified_count: Optional[int] = Field(None, ge=0)


class Summary(BaseModel):
    """Aggregate change counts across all GTFS files in the diff."""

    model_config = ConfigDict(populate_by_name=True)

    total_changes: int = Field(..., ge=0)
    files_added_count: int = Field(..., ge=0)
    files_deleted_count: int = Field(..., ge=0)
    files_modified_count: int = Field(..., ge=0)
    files: list[FileSummary]


class FieldChange(BaseModel):
    """The before and after values for a single field within a modified row."""

    model_config = ConfigDict(populate_by_name=True)

    field: str
    base_value: str
    new_value: str


class RowAdded(BaseModel):
    """A row that exists only in the new feed."""

    model_config = ConfigDict(populate_by_name=True)

    identifier: dict[str, str]
    raw_value: str
    new_line_number: int = Field(..., ge=1)


class RowDeleted(BaseModel):
    """A row that exists only in the base feed."""

    model_config = ConfigDict(populate_by_name=True)

    identifier: dict[str, str]
    raw_value: str
    base_line_number: int = Field(..., ge=1)


class RowModified(BaseModel):
    """A row present in both feeds whose field values differ."""

    model_config = ConfigDict(populate_by_name=True)

    identifier: dict[str, str]
    raw_value: str
    base_line_number: int = Field(..., ge=1)
    new_line_number: int = Field(..., ge=1)
    field_changes: list[FieldChange] = Field(..., min_length=1)


class RowChanges(BaseModel):
    """All row-level changes for a file, keyed by primary key columns."""

    model_config = ConfigDict(populate_by_name=True)

    primary_key: list[str] = Field(..., min_length=1)
    columns: list[str]
    added: list[RowAdded]
    deleted: list[RowDeleted]
    modified: list[RowModified]


class Truncated(BaseModel):
    """Indicates that row changes were capped and some were omitted."""

    model_config = ConfigDict(populate_by_name=True)

    is_truncated: Literal[True]
    omitted_count: int = Field(..., ge=1)


class FileDiff(BaseModel):
    """Complete diff for a single GTFS file, including column and row changes."""

    model_config = ConfigDict(populate_by_name=True)

    file_name: str
    file_action: Literal["modified", "added", "deleted"]
    columns_added: list[ColumnEntry]
    columns_deleted: list[ColumnEntry]
    row_changes: Optional[RowChanges] = None
    truncated: Optional[Truncated] = None


class GtfsDiff(BaseModel):
    """Root model for the GTFS Diff v2 output format."""

    model_config = ConfigDict(populate_by_name=True)

    metadata: Metadata
    summary: Summary
    file_diffs: list[FileDiff]


__all__ = [
    "ColumnEntry",
    "FeedSource",
    "UnsupportedFile",
    "Metadata",
    "FileSummary",
    "Summary",
    "FieldChange",
    "RowAdded",
    "RowDeleted",
    "RowModified",
    "RowChanges",
    "Truncated",
    "FileDiff",
    "GtfsDiff",
]
