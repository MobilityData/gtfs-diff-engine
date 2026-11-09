"""Diffing one file present in one or both feeds, in memory or with DuckDB."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING, TextIO

from ..feeds.csv_utils import (
    DuplicatePrimaryKeyError,
    MissingPrimaryKeyError,
    _missing_required_pk_columns,
    _parse_raw_line,
    _read_csv_index,
    _read_headers,
    _read_headers_and_count,
)
from ..feeds.sources import FeedFileMeta, _materialized_path
from ..gtfs_definitions import (
    get_optional_primary_key_columns,
    get_primary_key,
)
from ..models import (
    ColumnEntry,
    FileDiff,
    FileStats,
    FileSummary,
    RowAdded,
    RowDeleted,
    RowModified,
)
from ..tracing import _trace
from .helpers import (
    _assemble_modified_file_diff,
    _build_identifier_and_raw,
    _build_not_compared_diff,
    _compute_ignored_columns,
    _detect_id_churn,
    _diff_columns,
    _duplicate_primary_key_reason,
    _missing_primary_key_reason,
    _new_identifier,
    _scan_modifications,
    _select_modified,
    _split_row_changes_cap,
)

if TYPE_CHECKING:
    from ..matching import FileTranslation


# Files whose larger side is at least this many bytes (uncompressed) are routed
# to the DuckDB backend, when available. ~50 MB roughly corresponds to the point
# where an in-memory index of a wide GTFS file (e.g. stop_times.txt) becomes
# expensive; tune via ``diff_feeds(large_file_threshold_bytes=...)``.
DEFAULT_LARGE_FILE_THRESHOLD_BYTES = 50 * 1024**2


def _diff_file(
    file_name: str,
    base_opener: Callable[[], TextIO] | None,
    new_opener: Callable[[], TextIO] | None,
    row_changes_cap: int | None,
    id_churn_threshold: float,
    not_compared_files: dict[str, str],
    base_meta: FeedFileMeta | None = None,
    new_meta: FeedFileMeta | None = None,
    large_file_threshold_bytes: int | None = None,
    use_duckdb: bool = False,
    column_stats: bool = True,
    translation: FileTranslation | None = None,
    keep: set[tuple] | None = None,
) -> tuple[FileDiff, FileSummary]:
    """Dispatch to the appropriate diff helper based on feed presence."""
    if base_opener is None:
        assert new_opener is not None
        return _diff_file_added(file_name, new_opener)
    if new_opener is None:
        return _diff_file_deleted(file_name, base_opener)
    return _diff_file_modified(
        file_name,
        base_opener,
        new_opener,
        row_changes_cap,
        id_churn_threshold,
        not_compared_files,
        base_meta=base_meta,
        new_meta=new_meta,
        large_file_threshold_bytes=large_file_threshold_bytes,
        use_duckdb=use_duckdb,
        column_stats=column_stats,
        translation=translation,
        keep=keep,
    )


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


def _eligible_for_duckdb(
    file_name: str,
    pk_def: list[str],
    pk_is_explicit: bool,
) -> bool:
    """Whether a file's primary key is simple enough for the DuckDB backend.

    The DuckDB path only handles an explicit primary key with no
    conditionally-present (optional) columns, so the SQL can treat every PK
    column as mandatory and present. Empty-PK and optional-PK files (e.g.
    ``translations.txt``) stay on the in-memory engine.
    """
    if not pk_is_explicit or not pk_def:
        return False
    return not get_optional_primary_key_columns(file_name)


def _maybe_diff_modified_duckdb(
    file_name: str,
    pk_def: list[str],
    pk_is_explicit: bool,
    base_meta: FeedFileMeta | None,
    new_meta: FeedFileMeta | None,
    large_file_threshold_bytes: int | None,
    use_duckdb: bool,
    row_changes_cap: int | None,
    id_churn_threshold: float,
    not_compared_files: dict[str, str],
    column_stats: bool = True,
    translation: FileTranslation | None = None,
    keep: set[tuple] | None = None,
) -> tuple[FileDiff, FileSummary] | None:
    """Diff a modified file via DuckDB when it is large and eligible.

    Returns the ``(FileDiff, FileSummary)`` result, or ``None`` to signal the
    caller should use the in-memory engine (file too small, ineligible PK,
    unknown size, duckdb unavailable/disabled, or a backend error).
    """
    if not use_duckdb or large_file_threshold_bytes is None:
        return None
    if not _eligible_for_duckdb(file_name, pk_def, pk_is_explicit):
        return None

    base_size = base_meta.size if base_meta else None
    new_size = new_meta.size if new_meta else None
    if base_size is None or new_size is None:
        return None  # unknown size — safe default is the in-memory engine
    if max(base_size, new_size) < large_file_threshold_bytes:
        return None

    from . import duckdb_backend

    if not duckdb_backend.is_duckdb_available():
        _trace(
            f"  [{file_name}] large file but duckdb not installed — "
            f"using in-memory engine"
        )
        return None

    try:
        with (
            _materialized_path(base_meta) as base_path,
            _materialized_path(new_meta) as new_path,
        ):
            if base_path is None or new_path is None:
                return None
            _trace(
                f"  [{file_name}] large file "
                f"({max(base_size, new_size):,} bytes) — using duckdb backend"
            )
            return duckdb_backend.diff_modified_duckdb(
                file_name=file_name,
                base_path=base_path,
                new_path=new_path,
                pk_cols=pk_def,
                row_changes_cap=row_changes_cap,
                id_churn_threshold=id_churn_threshold,
                not_compared_files=not_compared_files,
                column_stats=column_stats,
                translation=translation,
                keep=keep,
            )
    except Exception as exc:  # pragma: no cover - defensive fallback
        _trace(
            f"  [{file_name}] duckdb backend failed ({exc!r}) — "
            f"falling back to in-memory engine"
        )
        return None


def _build_missing_pk_not_compared(
    file_name: str,
    base_opener: Callable[[], TextIO],
    new_opener: Callable[[], TextIO],
    pk_cols: list[str],
) -> tuple[FileDiff, FileSummary]:
    """Build the ``not_compared`` result for a file missing required PK columns.

    Re-reads the headers (and counts data rows) of both feeds to preserve
    column-level differences and accurate row counts, then reports which side(s)
    lack which mandatory primary-key columns. Used as the fallback when indexing
    raises :class:`MissingPrimaryKeyError`, so one broken file no longer aborts
    the whole diff.
    """
    with base_opener() as f:
        base_headers, base_count = _read_headers_and_count(f)
    with new_opener() as f:
        new_headers, new_count = _read_headers_and_count(f)

    missing_base = _missing_required_pk_columns(base_headers, pk_cols, file_name)
    missing_new = _missing_required_pk_columns(new_headers, pk_cols, file_name)
    reason = _missing_primary_key_reason(missing_base, missing_new)
    _trace(f"  [{file_name}] not compared — {reason.code}: {reason.message}")

    columns_added, columns_deleted, _ = _diff_columns(base_headers, new_headers)
    return _build_not_compared_diff(
        file_name=file_name,
        reason=reason,
        columns_added=columns_added,
        columns_deleted=columns_deleted,
        base_row_count=base_count,
        new_row_count=new_count,
    )


def _build_duplicate_pk_not_compared(
    file_name: str,
    base_opener: Callable[[], TextIO],
    new_opener: Callable[[], TextIO],
    error: DuplicatePrimaryKeyError,
) -> tuple[FileDiff, FileSummary]:
    """Build the ``not_compared`` result for a file with duplicate primary keys.

    Re-reads the headers (and counts data rows) of both feeds to preserve
    column-level differences and accurate row counts. Used as the fallback when
    indexing raises :class:`DuplicatePrimaryKeyError`, so one file with
    ambiguous keys no longer aborts the whole diff.
    """
    with base_opener() as f:
        base_headers, base_count = _read_headers_and_count(f)
    with new_opener() as f:
        new_headers, new_count = _read_headers_and_count(f)

    reason = _duplicate_primary_key_reason(
        error.primary_key, detail=error.detail, side=error.side
    )
    _trace(f"  [{file_name}] not compared — {reason.code}: {reason.message}")

    columns_added, columns_deleted, _ = _diff_columns(base_headers, new_headers)
    return _build_not_compared_diff(
        file_name=file_name,
        reason=reason,
        columns_added=columns_added,
        columns_deleted=columns_deleted,
        base_row_count=base_count,
        new_row_count=new_count,
    )


def _diff_file_modified(
    file_name: str,
    base_opener: Callable[[], TextIO],
    new_opener: Callable[[], TextIO],
    row_changes_cap: int | None,
    id_churn_threshold: float,
    not_compared_files: dict[str, str],
    base_meta: FeedFileMeta | None = None,
    new_meta: FeedFileMeta | None = None,
    large_file_threshold_bytes: int | None = None,
    use_duckdb: bool = False,
    column_stats: bool = True,
    translation: FileTranslation | None = None,
    keep: set[tuple] | None = None,
) -> tuple[FileDiff, FileSummary]:
    """Compute the diff for a file present in both feeds.

    With *translation*, new rows are keyed by the base feed's IDs of the
    entities they were matched to.
    """
    pk_def = get_primary_key(file_name)
    assert pk_def is not None  # caller guarantees supported files only
    pk_is_explicit = len(pk_def) > 0

    # For files with an empty PK definition, use all base columns as the key.
    if len(pk_def) == 0:
        with base_opener() as f:
            pk_cols: list[str] = _read_headers(f)
    else:
        pk_cols = pk_def

    # Route very large files to the DuckDB backend (when eligible) so they can be
    # diffed without holding every row in memory. On any failure, fall back to
    # the in-memory engine below.
    duckdb_result = _maybe_diff_modified_duckdb(
        file_name=file_name,
        pk_def=pk_def,
        pk_is_explicit=pk_is_explicit,
        base_meta=base_meta,
        new_meta=new_meta,
        large_file_threshold_bytes=large_file_threshold_bytes,
        use_duckdb=use_duckdb,
        row_changes_cap=row_changes_cap,
        id_churn_threshold=id_churn_threshold,
        not_compared_files=not_compared_files,
        column_stats=column_stats,
        translation=translation,
        keep=keep,
    )
    if duckdb_result is not None:
        return duckdb_result

    # Build indexes (two streaming passes, one per file). A file that cannot be
    # keyed/matched — because it is missing a required primary key column or has
    # duplicate primary key values — is reported as not_compared (preserving
    # column-level differences) instead of aborting the entire diff, so the rest
    # of the feed is still compared.
    try:
        with base_opener() as f:
            _trace(f"  [{file_name}] indexing base feed...")
            t0 = time.monotonic()
            base_headers, base_index = _read_csv_index(
                f, pk_cols, file_name=file_name, side="base"
            )
            _trace(
                f"  [{file_name}] base index done: {len(base_index):,} "
                f"rows in {time.monotonic() - t0:.1f}s"
            )

        with new_opener() as f:
            _trace(f"  [{file_name}] indexing new feed...")
            t0 = time.monotonic()
            new_headers, new_index = _read_csv_index(
                f, pk_cols, file_name=file_name, side="new", translation=translation
            )
            _trace(
                f"  [{file_name}] new index done:  {len(new_index):,} "
                f"rows in {time.monotonic() - t0:.1f}s"
            )
    except MissingPrimaryKeyError:
        return _build_missing_pk_not_compared(
            file_name, base_opener, new_opener, pk_cols
        )
    except DuplicatePrimaryKeyError as exc:
        return _build_duplicate_pk_not_compared(file_name, base_opener, new_opener, exc)

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

    # Generic "not compared" gate: when a file cannot be meaningfully diffed
    # (here: regenerated primary keys), skip the expensive row scan and report
    # the file as not_compared, preserving column-level differences.
    not_compared_reason = _detect_id_churn(
        pk_cols=pk_cols,
        pk_is_explicit=pk_is_explicit,
        base_row_count=len(base_index),
        new_row_count=len(new_index),
        common_count=len(common_keys),
        id_churn_threshold=id_churn_threshold,
    )
    if not_compared_reason is not None:
        _trace(
            f"  [{file_name}] not compared — {not_compared_reason.code}: "
            f"{not_compared_reason.message}"
        )
        return _build_not_compared_diff(
            file_name=file_name,
            reason=not_compared_reason,
            columns_added=columns_added,
            columns_deleted=columns_deleted,
            base_row_count=len(base_index),
            new_row_count=len(new_index),
        )

    # Foreign-key columns pointing at files that churned are unreliable here too:
    # exclude them from the field-level comparison and report them as ignored.
    ignored_columns, ignored_names = _compute_ignored_columns(
        file_name, base_headers, new_headers, pk_cols, not_compared_files
    )

    modified_candidates = _scan_modifications(
        file_name,
        common_keys,
        base_index,
        new_index,
        base_headers,
        new_headers,
        ignored_columns=ignored_names,
        translation=translation,
    )
    true_modified = len(modified_candidates)

    # Per-column modification counts over *all* modified rows (independent of any
    # row-changes cap), used to populate column_stats with true counts.
    column_mod_counts: dict[str, int] = {}
    for _pk, field_changes, _b_line, _n_line in modified_candidates:
        for fc in field_changes:
            column_mod_counts[fc.field] = column_mod_counts.get(fc.field, 0) + 1

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
    include_row_changes = row_changes_cap != 0

    if include_row_changes:
        added_limit, deleted_limit, modified_limit = _split_row_changes_cap(
            row_changes_cap, true_added, true_deleted, true_modified
        )

        # Fill added rows up to allocated cap (earliest rows first, by line).
        added_order = sorted(added_keys, key=lambda k: new_index[k][0])
        for pk_tuple in added_order[:added_limit]:
            n_line, n_raw = new_index[pk_tuple]
            n_dict = _parse_raw_line(n_raw, new_headers)
            identifier, raw_value = _build_identifier_and_raw(
                n_dict, pk_cols, union_columns, new_header_set
            )
            added_rows.append(
                RowAdded(
                    identifier=identifier, raw_value=raw_value, new_line_number=n_line
                )
            )

        # Fill deleted rows up to allocated cap (earliest rows first, by line).
        # Rows behind notable highlights (*keep*) first, then the earliest.
        keep = keep or set()
        deleted_order = sorted(
            deleted_keys, key=lambda k: (k not in keep, base_index[k][0])
        )[:deleted_limit]
        deleted_order.sort(key=lambda k: base_index[k][0])
        for pk_tuple in deleted_order:
            b_line, b_raw = base_index[pk_tuple]
            b_dict = _parse_raw_line(b_raw, base_headers)
            identifier, raw_value = _build_identifier_and_raw(
                b_dict, pk_cols, union_columns, base_header_set
            )
            deleted_rows.append(
                RowDeleted(
                    identifier=identifier, raw_value=raw_value, base_line_number=b_line
                )
            )

        # Fill modified rows up to allocated cap, with examples of every changed
        # column (see _select_modified), in line order.
        modified_order = _select_modified(
            modified_candidates,
            modified_limit,
            line_of=lambda c: c[2],
            fields_of=lambda c: [fc.field for fc in c[1]],
            column_order=union_columns,
            first={c[2] for c in modified_candidates if c[0] in keep},
        )
        for pk_tuple, field_changes, b_line, n_line in modified_order:
            b_raw = base_index[pk_tuple][1]
            b_dict = _parse_raw_line(b_raw, base_headers)
            identifier, raw_value = _build_identifier_and_raw(
                b_dict, pk_cols, union_columns, base_header_set
            )
            extra = {}
            if translation is not None:
                n_dict = _parse_raw_line(new_index[pk_tuple][1], new_headers)
                new_identifier = _new_identifier(identifier, n_dict, pk_cols)
                if new_identifier is not None:
                    extra["new_identifier"] = new_identifier
            modified_rows.append(
                RowModified(
                    identifier=identifier,
                    raw_value=raw_value,
                    base_line_number=b_line,
                    new_line_number=n_line,
                    field_changes=field_changes,
                    **extra,
                )
            )

    return _assemble_modified_file_diff(
        file_name=file_name,
        pk_cols=pk_cols,
        union_columns=union_columns,
        columns_added=columns_added,
        columns_deleted=columns_deleted,
        ignored_columns=ignored_columns,
        added_rows=added_rows,
        deleted_rows=deleted_rows,
        modified_rows=modified_rows,
        true_added=true_added,
        true_deleted=true_deleted,
        true_modified=true_modified,
        total_base=len(base_index),
        total_new=len(new_index),
        cap=row_changes_cap,
        include_row_changes=include_row_changes,
        column_stats=column_stats,
        column_mod_counts=column_mod_counts,
    )
