"""Core diff engine: loads two GTFS feeds and computes structured differences.

Memory note
-----------
The two-pass algorithm builds in-memory indexes mapping primary-key tuples to
(line_number, raw_csv_string) for every row in each file.  For typical transit
feeds this is fine.  For very large feeds (stop_times.txt can exceed 10 M rows)
a disk-backed index (e.g. SQLite) would be more appropriate; that is left as a
future optimization.
"""

from __future__ import annotations

import configparser
import csv
import io
import sys
import time
import zipfile
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import TextIO

from .gtfs_definitions import get_primary_key
from .models import (
    ColumnEntry,
    FeedSource,
    FieldChange,
    FileDiff,
    FileStats,
    FileSummary,
    GtfsDiff,
    Metadata,
    RowAdded,
    RowChanges,
    RowDeleted,
    RowModified,
    Summary,
    Truncated,
    UnsupportedFile,
)


def _read_schema_version() -> str:
    conf_text = resources.files("gtfs_diff").joinpath("schema.conf").read_text()
    parser = configparser.ConfigParser()
    parser.read_string("[default]\n" + conf_text)
    return parser.get("default", "SCHEMA_VERSION")


class MissingPrimaryKeyError(ValueError):
    """Raised when a required primary key column is absent from a file's headers."""

    def __init__(
        self, file_name: str, missing_columns: list[str], headers: list[str]
    ) -> None:
        self.file_name = file_name
        self.missing_columns = missing_columns
        self.headers = headers
        super().__init__(
            f"'{file_name}': required primary key column(s) "
            f"{missing_columns} not found in headers {headers}."
        )


def _trace(msg: str) -> None:
    """Print a timestamped progress message with current RSS to stderr."""
    import psutil

    rss_mb = psutil.Process().memory_info().rss / 1024 / 1024
    print(
        f"[gtfs-diff {datetime.now().strftime('%H:%M:%S')} {rss_mb:.0f}MB] {msg}",
        file=sys.stderr,
        flush=True,
    )


# A "lazy opener" maps a filename (e.g. "stops.txt") to a zero-arg callable
# that opens the file and returns a text stream.
LazyOpeners = dict[str, Callable[[], TextIO]]


# ---------------------------------------------------------------------------
# Low-level CSV helpers
# ---------------------------------------------------------------------------


def _row_to_csv(values: list[str]) -> str:
    """Serialize a list of string values to a single CSV line (no trailing newline)."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="")
    writer.writerow(values)
    return buf.getvalue()


def _read_headers(text_io: TextIO) -> list[str]:
    """Read only the header row from a CSV stream, stripping whitespace."""
    reader = csv.reader(text_io)
    try:
        row = next(reader)
        return [h.strip() for h in row]
    except StopIteration:
        return []


def _read_csv_index(
    text_io: TextIO,
    pk_columns: list[str] | None = None,
    file_name: str = "<unknown>",
) -> tuple[list[str], dict[tuple, tuple[int, str]]]:
    """Stream a CSV file and build a primary-key → (line_number, raw_csv_string) index.

    Args:
        text_io:    Open text stream for the CSV file (utf-8-sig recommended).
        pk_columns: Columns that form the primary key.  `None` / empty list
                    means use *all* columns as the composite key.
        file_name:  Used in error messages only.

    Returns:
        headers: Stripped column names from the header row.
        index:   Maps `pk_tuple` → `(line_number, raw_csv_string)`.
                 Line numbers are 1-based; the header row is line 1, so the
                 first data row is line 2.

    Raises:
        MissingPrimaryKeyError: If expected primary key columns are absent from
                    the header (diff would silently treat all rows as identical).
        ValueError: If duplicate primary key values are found (diff would
                    silently discard earlier rows).
    """
    reader = csv.reader(text_io)
    try:
        raw_headers = next(reader)
    except StopIteration:
        return [], {}

    headers = [h.strip() for h in raw_headers]
    n = len(headers)
    effective_pk = pk_columns if pk_columns else headers

    if pk_columns:
        missing = [col for col in pk_columns if col not in set(headers)]
        if missing:
            raise MissingPrimaryKeyError(file_name, missing, headers)

    index: dict[tuple, tuple[int, str]] = {}
    for line_num, row in enumerate(reader, start=2):
        # Pad short rows; trim rows wider than the header (malformed CSV safety).
        if len(row) < n:
            row = row + [""] * (n - len(row))
        row_vals = row[:n]
        row_dict = dict(zip(headers, row_vals, strict=True))
        pk_tuple = tuple(row_dict.get(col, "") for col in effective_pk)

        if pk_tuple in index:
            raise ValueError(
                f"{file_name}: duplicate primary key "
                f"{dict(zip(effective_pk, pk_tuple, strict=True))} "
                f"at line {line_num} "
                f"(first seen at line {index[pk_tuple][0]})."
            )

        index[pk_tuple] = (line_num, _row_to_csv(row_vals))

    return headers, index


def _parse_raw_line(raw_line: str, headers: list[str]) -> dict[str, str]:
    """Deserialise a raw CSV string (as stored in the index) back to a row dict."""
    reader = csv.reader(io.StringIO(raw_line))
    try:
        row = next(reader)
    except StopIteration:
        return {col: "" for col in headers}
    if len(row) < len(headers):
        row = row + [""] * (len(headers) - len(row))
    return dict(zip(headers, row, strict=True))


def _values_differ(a: str, b: str) -> bool:
    """Return True if two field values represent meaningfully different data.

    String-identical values are equal (fast path).
    If they differ as strings, attempt numeric comparison — this silently
    ignores cosmetic differences like trailing zeros in coordinate fields
    (e.g. '-73.55625' vs '-73.556250').
    Non-numeric strings fall back to string equality.
    """
    a = a.strip()
    b = b.strip()
    if a.lower() == b.lower():
        return False
    try:
        return float(a) != float(b)
    except (ValueError, OverflowError):
        return True


def _compute_raw_value(
    row_dict: dict[str, str],
    columns: list[str],
    present_headers: set[str],
) -> str:
    """Build an ordered CSV string aligned to the *union* columns list.

    Columns absent from ``present_headers`` (i.e. the file this row came from
    did not have that column) are rendered as empty strings.
    """
    values = [row_dict[col] if col in present_headers else "" for col in columns]
    return _row_to_csv(values)


# ---------------------------------------------------------------------------
# Feed opener
# ---------------------------------------------------------------------------


@contextmanager
def _open_feed(path: str | Path) -> Generator[LazyOpeners, None, None]:
    """Open a GTFS feed (zip archive or directory) and yield lazy file openers.

    Each entry in the returned dict is a zero-arg callable that, when called,
    opens the corresponding ``.txt`` file and returns a utf-8-sig text stream.
    Callers are responsible for closing each stream.

    Supports:
    * ``.zip`` archives (files at root *or* inside a single sub-directory).
    * Plain directories containing ``.txt`` files.
    """
    path = Path(path)

    if path.is_dir():
        openers: LazyOpeners = {}
        for txt_file in sorted(path.glob("*.txt")):

            def _make_opener(p: Path) -> Callable[[], TextIO]:
                return lambda: p.open(encoding="utf-8-sig")

            openers[txt_file.name] = _make_opener(txt_file)
        yield openers

    elif zipfile.is_zipfile(path):
        zf = zipfile.ZipFile(path, "r")
        try:
            # Accept both root-level files and files inside exactly one subdirectory.
            name_map: dict[str, str] = {}
            for member in zf.namelist():
                if member.endswith(".txt"):
                    basename = member.rsplit("/", 1)[-1]
                    if basename:
                        name_map[basename] = member

            openers = {}
            for basename, internal_path in name_map.items():

                def _make_opener(ip: str) -> Callable[[], TextIO]:  # type: ignore[misc]
                    return lambda: io.TextIOWrapper(zf.open(ip), encoding="utf-8-sig")

                openers[basename] = _make_opener(internal_path)
            yield openers
        finally:
            zf.close()

    else:
        raise ValueError(
            f"Unsupported feed path: {path!r}.  Must be a .zip file or a directory."
        )


# ---------------------------------------------------------------------------
# Per-file diff
# ---------------------------------------------------------------------------


def _diff_file(
    file_name: str,
    base_opener: Callable[[], TextIO] | None,
    new_opener: Callable[[], TextIO] | None,
    row_changes_cap: int | None,
) -> tuple[FileDiff, FileSummary]:
    """Dispatch to the appropriate diff helper based on feed presence."""
    if base_opener is None:
        assert new_opener is not None
        return _diff_file_added(file_name, new_opener)
    if new_opener is None:
        return _diff_file_deleted(file_name, base_opener)
    return _diff_file_modified(file_name, base_opener, new_opener, row_changes_cap)


def _diff_file_added(
    file_name: str,
    new_opener: Callable[[], TextIO],
) -> tuple[FileDiff, FileSummary]:
    """Build a diff result for a file that exists only in the new feed."""
    with new_opener() as f:
        new_headers = _read_headers(f)
    columns_added = [
        ColumnEntry(name=col, position=i + 1) for i, col in enumerate(new_headers)
    ]
    file_diff = FileDiff(
        file_name=file_name,
        file_action="added",
        columns_added=columns_added,
        columns_deleted=[],
        stats=FileStats(
            columns_added_count=len(columns_added), columns_deleted_count=0
        ),
    )
    summary = FileSummary(file_name=file_name, status="added")
    return file_diff, summary


def _diff_file_deleted(
    file_name: str,
    base_opener: Callable[[], TextIO],
) -> tuple[FileDiff, FileSummary]:
    """Build a diff result for a file that exists only in the base feed."""
    with base_opener() as f:
        base_headers = _read_headers(f)
    columns_deleted = [
        ColumnEntry(name=col, position=i + 1) for i, col in enumerate(base_headers)
    ]
    file_diff = FileDiff(
        file_name=file_name,
        file_action="deleted",
        columns_added=[],
        columns_deleted=columns_deleted,
        stats=FileStats(
            columns_added_count=0, columns_deleted_count=len(columns_deleted)
        ),
    )
    summary = FileSummary(file_name=file_name, status="deleted")
    return file_diff, summary


def _diff_columns(
    base_headers: list[str],
    new_headers: list[str],
) -> tuple[list[ColumnEntry], list[ColumnEntry], list[str]]:
    """Compute column-level differences between two header lists.

    Returns:
        columns_added:  Columns present in new but not in base.
        columns_deleted: Columns present in base but not in new.
        union_columns:  All columns — base order first, new-only appended.

    Note: column reorders (same columns, different positions) are silently
    ignored — values are always compared by name, not position.
    """
    base_header_set = set(base_headers)
    new_header_set = set(new_headers)
    columns_added = [
        ColumnEntry(name=col, position=i + 1)
        for i, col in enumerate(new_headers)
        if col not in base_header_set
    ]
    columns_deleted = [
        ColumnEntry(name=col, position=i + 1)
        for i, col in enumerate(base_headers)
        if col not in new_header_set
    ]
    new_only_cols = [col for col in new_headers if col not in base_header_set]
    union_columns: list[str] = base_headers + new_only_cols
    return columns_added, columns_deleted, union_columns


def _scan_modifications(
    file_name: str,
    common_keys: set[tuple],
    base_index: dict[tuple, tuple[int, str]],
    new_index: dict[tuple, tuple[int, str]],
    base_headers: list[str],
    new_headers: list[str],
) -> list[tuple[tuple, list[FieldChange], int, int]]:
    """Scan rows present in both feeds and return those whose field values differ.

    Compares only columns shared between both headers to avoid false positives
    when a column is added or removed.

    Returns a list of (pk_tuple, field_changes, base_line, new_line) for
    every common row that has at least one changed field.

    Note: row reorders (same rows, different line positions) are silently
    ignored — keys are compared as sets, so row order has no effect.
    """
    shared_cols = [col for col in base_headers if col in set(new_headers)]
    candidates: list[tuple[tuple, list[FieldChange], int, int]] = []

    n = len(common_keys)
    _trace(f"  [{file_name}] scanning {n:,} common rows...")
    t0 = time.monotonic()
    for pk_tuple in common_keys:
        b_line, b_raw = base_index[pk_tuple]
        n_line, n_raw = new_index[pk_tuple]
        b_dict = _parse_raw_line(b_raw, base_headers)
        n_dict = _parse_raw_line(n_raw, new_headers)
        field_changes = [
            FieldChange(field=col, base_value=b_dict[col], new_value=n_dict[col])
            for col in shared_cols
            if _values_differ(b_dict.get(col, ""), n_dict.get(col, ""))
        ]
        if field_changes:
            candidates.append((pk_tuple, field_changes, b_line, n_line))

    _trace(
        f"  [{file_name}] scan done in {time.monotonic() - t0:.1f}s — "
        f"{len(candidates):,} modified"
    )
    return candidates


def _diff_file_modified(
    file_name: str,
    base_opener: Callable[[], TextIO],
    new_opener: Callable[[], TextIO],
    row_changes_cap: int | None,
) -> tuple[FileDiff, FileSummary]:
    """Compute the diff for a file present in both feeds."""
    pk_def = get_primary_key(file_name)
    assert pk_def is not None  # caller guarantees supported files only

    # For files with an empty PK definition, use all base columns as the key.
    if len(pk_def) == 0:
        with base_opener() as f:
            pk_cols: list[str] = _read_headers(f)
    else:
        pk_cols = pk_def

    # Build indexes (two streaming passes, one per file).
    with base_opener() as f:
        _trace(f"  [{file_name}] indexing base feed...")
        t0 = time.monotonic()
        base_headers, base_index = _read_csv_index(f, pk_cols, file_name=file_name)
        _trace(
            f"  [{file_name}] base index done: {len(base_index):,} "
            f"rows in {time.monotonic() - t0:.1f}s"
        )

    with new_opener() as f:
        _trace(f"  [{file_name}] indexing new feed...")
        t0 = time.monotonic()
        new_headers, new_index = _read_csv_index(f, pk_cols, file_name=file_name)
        _trace(
            f"  [{file_name}] new index done:  {len(new_index):,} "
            f"rows in {time.monotonic() - t0:.1f}s"
        )

    # Column-level diff
    columns_added, columns_deleted, union_columns = _diff_columns(
        base_headers, new_headers
    )

    # Row-level diff
    base_keys = set(base_index)
    new_keys = set(new_index)
    added_keys = new_keys - base_keys
    deleted_keys = base_keys - new_keys
    common_keys = base_keys & new_keys

    true_added = len(added_keys)
    true_deleted = len(deleted_keys)

    modified_candidates = _scan_modifications(
        file_name, common_keys, base_index, new_index, base_headers, new_headers
    )
    true_modified = len(modified_candidates)
    _trace(
        f"  [{file_name}] row diff summary — "
        f"added={true_added:,} "
        f"deleted={true_deleted:,} "
        f"modified={true_modified:,}"
    )

    # Determine row-changes output based on cap.
    # cap=0 means "summary counts only" — row_changes is omitted from the output
    # entirely (serialized as null, excluded by exclude_none=True in the CLI).
    # True counts are still computed and surfaced in summary.files.
    base_header_set = set(base_headers)
    new_header_set = set(new_headers)
    added_rows: list[RowAdded] = []
    deleted_rows: list[RowDeleted] = []
    modified_rows: list[RowModified] = []
    truncated: Truncated | None = None
    include_row_changes = row_changes_cap != 0

    if include_row_changes:
        cap = row_changes_cap  # None = unlimited

        def _remaining(used: int) -> int | None:
            return None if cap is None else max(0, cap - used)

        # Fill added rows up to cap.
        added_limit = _remaining(0)
        for pk_tuple in list(added_keys)[:added_limit]:
            n_line, n_raw = new_index[pk_tuple]
            n_dict = _parse_raw_line(n_raw, new_headers)
            identifier = {col: n_dict.get(col, "") for col in pk_cols}
            raw_value = _compute_raw_value(n_dict, union_columns, new_header_set)
            added_rows.append(
                RowAdded(
                    identifier=identifier, raw_value=raw_value, new_line_number=n_line
                )
            )

        # Fill deleted rows up to remaining cap.
        deleted_limit = _remaining(len(added_rows))
        for pk_tuple in list(deleted_keys)[:deleted_limit]:
            b_line, b_raw = base_index[pk_tuple]
            b_dict = _parse_raw_line(b_raw, base_headers)
            identifier = {col: b_dict.get(col, "") for col in pk_cols}
            raw_value = _compute_raw_value(b_dict, union_columns, base_header_set)
            deleted_rows.append(
                RowDeleted(
                    identifier=identifier, raw_value=raw_value, base_line_number=b_line
                )
            )

        # Fill modified rows up to remaining cap.
        modified_limit = _remaining(len(added_rows) + len(deleted_rows))
        for pk_tuple, field_changes, b_line, n_line in modified_candidates[
            :modified_limit
        ]:
            b_raw = base_index[pk_tuple][1]
            b_dict = _parse_raw_line(b_raw, base_headers)
            identifier = {col: b_dict.get(col, "") for col in pk_cols}
            raw_value = _compute_raw_value(b_dict, union_columns, base_header_set)
            modified_rows.append(
                RowModified(
                    identifier=identifier,
                    raw_value=raw_value,
                    base_line_number=b_line,
                    new_line_number=n_line,
                    field_changes=field_changes,
                )
            )

        total_included = len(added_rows) + len(deleted_rows) + len(modified_rows)
        total_true = true_added + true_deleted + true_modified
        if cap is not None and total_true > cap:
            truncated = Truncated(
                is_truncated=True, omitted_count=total_true - total_included
            )

    row_changes: RowChanges | None = None
    if include_row_changes:
        # Use pk_cols for the primary_key field; for empty-pk files that means
        # all base columns — which is correct (they form the composite key).
        row_changes = RowChanges(
            primary_key=pk_cols,
            columns=union_columns,
            added=added_rows,
            deleted=deleted_rows,
            modified=modified_rows,
        )

    file_diff = FileDiff(
        file_name=file_name,
        file_action="modified",
        columns_added=columns_added,
        columns_deleted=columns_deleted,
        row_changes=row_changes,
        truncated=truncated,
        stats=FileStats(
            total_rows_base=len(base_index),
            total_rows_new=len(new_index),
            columns_added_count=len(columns_added),
            columns_deleted_count=len(columns_deleted),
            rows_added_count=true_added,
            rows_deleted_count=true_deleted,
            rows_modified_count=true_modified,
        ),
    )
    summary = FileSummary(file_name=file_name, status="modified")
    return file_diff, summary


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def diff_feeds(
    base_path: str | Path,
    new_path: str | Path,
    row_changes_cap_per_file: int | None = None,
    base_downloaded_at: datetime | None = None,
    new_downloaded_at: datetime | None = None,
) -> GtfsDiff:
    """Compare two GTFS feeds and return a structured :class:`GtfsDiff` result.

    Args:
        base_path:               Path to the base (old) GTFS feed — zip or directory.
        new_path:                Path to the new GTFS feed — zip or directory.
        row_changes_cap_per_file:
            * ``None``  — include all row changes (default).
            * ``0``     — omit all row-level detail (column diffs and counts
                          still computed).
            * ``N > 0`` — include up to *N* row changes per file (added first, then
                          deleted, then modified); a :class:`Truncated` record is
                          attached when the true count exceeds *N*.
        base_downloaded_at:  When the base feed was downloaded; defaults to *now*.
        new_downloaded_at:   When the new feed was downloaded; defaults to *now*.

    Returns:
        A fully-populated :class:`GtfsDiff` instance.
    """
    now = datetime.now(timezone.utc)
    base_downloaded_at = base_downloaded_at or now
    new_downloaded_at = new_downloaded_at or now

    file_diffs: list[FileDiff] = []
    file_summaries: list[FileSummary] = []
    unsupported_files: list[UnsupportedFile] = []

    with _open_feed(base_path) as base_openers, _open_feed(new_path) as new_openers:
        all_files = sorted(set(base_openers) | set(new_openers))
        _trace(f"Found {len(all_files)} file(s) to process: {', '.join(all_files)}")

        for file_name in all_files:
            _trace(f"Processing {file_name}...")
            in_base = file_name in base_openers
            in_new = file_name in new_openers

            # Determine support status.
            pk_def = get_primary_key(file_name)
            if pk_def is None:
                # Unsupported file — record it and skip.
                if in_base and in_new:
                    present_in: str = "both"
                elif in_base:
                    present_in = "base"
                else:
                    present_in = "new"
                unsupported_files.append(
                    UnsupportedFile(file_name=file_name, present_in=present_in)  # type: ignore[arg-type]
                )
                continue

            base_opener = base_openers.get(file_name)
            new_opener = new_openers.get(file_name)

            file_diff, file_summary = _diff_file(
                file_name=file_name,
                base_opener=base_opener,
                new_opener=new_opener,
                row_changes_cap=row_changes_cap_per_file,
            )

            # Per spec: file_diffs[] contains only *changed* files.
            # Skip files present in both feeds with no actual changes.
            stats = file_diff.stats
            if (
                file_summary.status == "modified"
                and stats is not None
                and not stats.columns_added_count
                and not stats.columns_deleted_count
                and not stats.rows_added_count
                and not stats.rows_deleted_count
                and not stats.rows_modified_count
            ):
                continue

            file_diffs.append(file_diff)
            file_summaries.append(file_summary)

    # Build summary aggregates.
    files_added = sum(1 for s in file_summaries if s.status == "added")
    files_deleted = sum(1 for s in file_summaries if s.status == "deleted")
    files_modified = sum(1 for s in file_summaries if s.status == "modified")

    def _stat(attr: str) -> int:
        return sum(getattr(fd.stats, attr, 0) or 0 for fd in file_diffs if fd.stats)

    total_changes = (
        _stat("rows_added_count")
        + _stat("rows_deleted_count")
        + _stat("rows_modified_count")
        + _stat("columns_added_count")
        + _stat("columns_deleted_count")
        + files_added
        + files_deleted
    )

    metadata = Metadata(
        schema_version=_read_schema_version(),
        generated_at=now,
        row_changes_cap_per_file=row_changes_cap_per_file,
        base_feed=FeedSource(source=str(base_path), downloaded_at=base_downloaded_at),
        new_feed=FeedSource(source=str(new_path), downloaded_at=new_downloaded_at),
        unsupported_files=unsupported_files,
    )
    summary = Summary(
        total_changes=total_changes,
        files_added_count=files_added,
        files_deleted_count=files_deleted,
        files_modified_count=files_modified,
        files_not_compared_count=0,
        files=file_summaries,
    )
    result = GtfsDiff(metadata=metadata, summary=summary, file_diffs=file_diffs)
    _trace("diff_feeds complete")
    return result
