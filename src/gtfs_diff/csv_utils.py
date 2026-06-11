"""Low-level CSV helpers: header parsing, indexing, and value comparison.

These are pure functions shared by the in-memory engine and the DuckDB backend.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import TextIO

from .gtfs_definitions import get_optional_primary_key_columns


def _is_url(path: str | Path) -> bool:
    """Return True if *path* is an ``http://`` or ``https://`` URL."""
    return isinstance(path, str) and (
        path.startswith("http://") or path.startswith("https://")
    )


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


class DuplicatePrimaryKeyError(ValueError):
    """Raised when duplicate primary key values are found in a file's rows.

    A duplicate key means rows cannot be uniquely matched between feeds, so the
    file is reported as ``not_compared`` (like a missing primary key) rather than
    aborting the whole diff. Subclasses :class:`ValueError` for backward
    compatibility with callers that caught the previously-raised ``ValueError``.
    """

    def __init__(
        self,
        file_name: str,
        primary_key: list[str],
        duplicate_key: dict[str, str] | None = None,
        line_number: int | None = None,
        first_line: int | None = None,
        side: str | None = None,
    ) -> None:
        self.file_name = file_name
        self.primary_key = primary_key
        self.duplicate_key = duplicate_key
        self.line_number = line_number
        self.first_line = first_line
        self.side = side
        location = ""
        if line_number is not None and first_line is not None:
            location = f" at line {line_number} (first seen at line {first_line})"
        feed = f" in the {side} feed" if side in ("base", "new") else ""
        super().__init__(
            f"{file_name}: duplicate primary key "
            f"{duplicate_key if duplicate_key is not None else primary_key}"
            f"{location}{feed}."
        )

    @property
    def detail(self) -> str | None:
        """A short human-readable locator for the first duplicate, if known."""
        if self.duplicate_key is None:
            return None
        if self.line_number is not None and self.first_line is not None:
            return (
                f"e.g. {self.duplicate_key} appears at lines "
                f"{self.first_line} and {self.line_number}"
            )
        return f"e.g. {self.duplicate_key}"


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


def _read_headers_and_count(text_io: TextIO) -> tuple[list[str], int]:
    """Read the header row and count the data rows of a CSV stream.

    Used for files that cannot be indexed (e.g. a missing required primary key)
    but still need accurate row counts in their ``not_compared`` stats. The CSV
    reader is used so quoted fields containing newlines are counted as one row.
    """
    reader = csv.reader(text_io)
    try:
        headers = [h.strip() for h in next(reader)]
    except StopIteration:
        return [], 0
    count = sum(1 for _ in reader)
    return headers, count


def _missing_required_pk_columns(
    headers: list[str], pk_columns: list[str], file_name: str
) -> list[str]:
    """Return the required primary-key columns absent from *headers*.

    Conditionally-present (optional) primary-key columns are not required: they
    participate in the compare identity as NULL/empty values when absent (see
    :func:`_read_csv_index`). Only mandatory key columns count as missing. An
    empty *pk_columns* (composite key over all columns) never has a requirement,
    so an empty list is returned.
    """
    if not pk_columns:
        return []
    header_set = set(headers)
    optional_pk = get_optional_primary_key_columns(file_name)
    return [
        col for col in pk_columns if col not in header_set and col not in optional_pk
    ]


def _read_csv_index(
    text_io: TextIO,
    pk_columns: list[str] | None = None,
    file_name: str = "<unknown>",
    side: str | None = None,
) -> tuple[list[str], dict[tuple, tuple[int, str]]]:
    """Stream a CSV file and build a primary-key → (line_number, raw_csv_string) index.

    Args:
        text_io:    Open text stream for the CSV file (utf-8-sig recommended).
        pk_columns: Columns that form the primary key.  `None` / empty list
                    means use *all* columns as the composite key.
        file_name:  Used in error messages only.
        side:       Which feed this stream belongs to (``"base"`` / ``"new"``),
                    recorded on a raised :class:`DuplicatePrimaryKeyError` so the
                    not_compared reason can name the offending feed.

    Returns:
        headers: Stripped column names from the header row.
        index:   Maps `pk_tuple` → `(line_number, raw_csv_string)`.
                 Line numbers are 1-based; the header row is line 1, so the
                 first data row is line 2.

    Raises:
        MissingPrimaryKeyError: If expected primary key columns are absent from
                    the header (diff would silently treat all rows as identical).
        DuplicatePrimaryKeyError: If duplicate primary key values are found (diff
                    would silently discard earlier rows).
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
        # Conditionally-present PK columns (e.g. translations.txt's record_id /
        # record_sub_id / field_value) may be absent, but they still participate
        # in the compare identity as NULL/empty values. Only a *mandatory*
        # missing column is an error.
        missing_required = _missing_required_pk_columns(headers, pk_columns, file_name)
        if missing_required:
            raise MissingPrimaryKeyError(file_name, missing_required, headers)
        effective_pk = pk_columns

    index: dict[tuple, tuple[int, str]] = {}
    for line_num, row in enumerate(reader, start=2):
        # Pad short rows; trim rows wider than the header (malformed CSV safety).
        if len(row) < n:
            row = row + [""] * (n - len(row))
        row_vals = row[:n]
        row_dict = dict(zip(headers, row_vals, strict=True))
        pk_tuple = tuple(row_dict.get(col, "") for col in effective_pk)

        if pk_tuple in index:
            raise DuplicatePrimaryKeyError(
                file_name,
                list(effective_pk),
                duplicate_key=dict(zip(effective_pk, pk_tuple, strict=True)),
                line_number=line_num,
                first_line=index[pk_tuple][0],
                side=side,
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
