"""Pure diff helpers: file ordering, column diffing, id-churn, and row scanning.

These functions contain no I/O and are shared by the in-memory engine and the
DuckDB backend. They operate on already-parsed headers/indexes and return model
objects or plain data structures.
"""

from __future__ import annotations

import time

from .csv_utils import _compute_raw_value, _parse_raw_line, _values_differ
from .gtfs_definitions import (
    MIN_ROWS_FOR_ID_CHURN_DETECTION,
    get_foreign_keys,
)
from .models import (
    ColumnEntry,
    ColumnStat,
    FieldChange,
    FileDiff,
    FileStats,
    FileSummary,
    IgnoredColumn,
    NotComparedReason,
    RowAdded,
    RowChanges,
    RowDeleted,
    RowModified,
    Truncated,
)
from .tracing import _trace


def _processing_order(files: list[str]) -> list[str]:
    """Return *files* ordered so referenced (parent) files precede the files
    that reference them via a foreign key.

    Uses a deterministic topological sort (alphabetical tie-break). Foreign keys
    pointing at files that are absent from *files* are ignored, and any cycle is
    broken by emitting the alphabetically-first remaining file — neither case can
    occur for well-formed GTFS but both are handled defensively.
    """
    present = set(files)
    deps: dict[str, set[str]] = {f: set() for f in files}
    for child in files:
        for refs in get_foreign_keys(child).values():
            for parent in refs:
                if parent in present and parent != child:
                    deps[child].add(parent)

    order: list[str] = []
    done: set[str] = set()
    remaining = set(files)
    while remaining:
        ready = sorted(f for f in remaining if deps[f] <= done)
        if not ready:  # breaking the cycle
            ready = [min(remaining)]
        for f in ready:
            order.append(f)
            done.add(f)
            remaining.discard(f)
    return order


def _compute_ignored_columns(
    file_name: str,
    base_headers: list[str],
    new_headers: list[str],
    pk_cols: list[str],
    not_compared_files: dict[str, str],
) -> tuple[list[IgnoredColumn], set[str]]:
    """Determine which foreign-key columns to exclude from the field-level diff.

    A foreign-key column is ignored when the file it references was itself marked
    ``not_compared`` — because its identifiers were regenerated (``id_churn``) or
    because it was missing a required primary key (``missing_primary_key``). In
    either case the referenced file's key values are unreliable, so any change in
    the referencing column is noise rather than a real edit. Primary-key columns
    are never ignored (they are the row identity).

    *not_compared_files* maps each not-compared file name to its reason code.

    Returns the ``IgnoredColumn`` records (in base-header order) and the set of
    ignored column names.
    """
    foreign_keys = get_foreign_keys(file_name)
    if not foreign_keys:
        return [], set()

    shared = set(base_headers) & set(new_headers)
    pk_set = set(pk_cols)
    ignored: list[IgnoredColumn] = []
    names: set[str] = set()
    for col in base_headers:
        if col not in shared or col in pk_set or col in names:
            continue
        refs = [r for r in foreign_keys.get(col, ()) if r in not_compared_files]
        if not refs:
            continue
        referenced = refs[0]
        ignored.append(
            IgnoredColumn(
                column=col,
                reason=NotComparedReason(
                    code="references_not_compared_file",
                    message=_ignored_column_message(
                        col, referenced, not_compared_files[referenced]
                    ),
                ),
            )
        )
        names.add(col)
    return ignored, names


def _ignored_column_message(column: str, referenced: str, reason_code: str) -> str:
    """Explain why a foreign-key *column* was excluded from the diff.

    The wording reflects *why* the *referenced* file was not compared so the
    message stays accurate for both id-churn and missing-primary-key cases.
    """
    if reason_code == "missing_primary_key":
        cause = (
            "was not compared because it is missing required primary key "
            "column(s), so its rows could not be matched"
        )
    elif reason_code == "id_churn":
        cause = (
            "was not compared because its primary key appears to be "
            "regenerated across versions (id_churn)"
        )
    elif reason_code == "duplicate_primary_key":
        cause = (
            "was not compared because it has duplicate primary key values, so "
            "its rows could not be uniquely matched"
        )
    else:
        cause = "was not compared"
    return (
        f"Column '{column}' references {referenced}, which {cause}. Its values "
        f"are unreliable, so the column was excluded from the diff."
    )


def _missing_primary_key_reason(
    missing_base: list[str], missing_new: list[str]
) -> NotComparedReason:
    """Build the ``not_compared`` reason for a file missing required PK columns.

    Reports which feed side(s) are missing which mandatory primary-key columns,
    so the file is skipped (rather than aborting the whole diff) and reported
    with column-level differences preserved.
    """
    parts: list[str] = []
    if missing_base:
        parts.append(f"the base feed is missing {sorted(set(missing_base))}")
    if missing_new:
        parts.append(f"the new feed is missing {sorted(set(missing_new))}")
    detail = " and ".join(parts) if parts else "a required primary key column"
    return NotComparedReason(
        code="missing_primary_key",
        message=(
            f"Required primary key column(s) are absent: {detail}. Rows cannot "
            f"be matched without the primary key, so row-level comparison was "
            f"skipped to avoid a misleading diff."
        ),
    )


def _feed_side_phrase(side: str | None) -> str:
    """Return a human phrase naming the offending feed side(s).

    ``side`` is ``"base"``, ``"new"``, ``"both"`` (or ``None`` when unknown).
    """
    if side == "base":
        return "the base feed"
    if side == "new":
        return "the new feed"
    if side == "both":
        return "both the base and new feed"
    return "the base or new feed"


def _duplicate_primary_key_reason(
    primary_key: list[str] | None,
    detail: str | None = None,
    side: str | None = None,
) -> NotComparedReason:
    """Build the ``not_compared`` reason for a file with duplicate primary keys.

    Duplicate key values mean rows cannot be uniquely matched between feeds, so
    the file is skipped (rather than aborting the whole diff) and reported with
    column-level differences preserved. *side* names which feed contains the
    duplicate (``"base"``, ``"new"``, ``"both"``, or ``None`` when unknown).
    *detail*, when given, locates an example duplicate (e.g. the offending key
    and line numbers).
    """
    pk = sorted(set(primary_key)) if primary_key else []
    pk_part = f" {pk}" if pk else ""
    extra = f" ({detail})" if detail else ""
    where = _feed_side_phrase(side)
    return NotComparedReason(
        code="duplicate_primary_key",
        message=(
            f"Duplicate primary key{pk_part} value(s) were found in {where}"
            f"{extra}, so rows cannot be uniquely matched between the base and "
            f"new feed. Row-level comparison was skipped to avoid a misleading "
            f"diff."
        ),
    )


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


def _detect_id_churn(
    pk_cols: list[str],
    pk_is_explicit: bool,
    base_row_count: int,
    new_row_count: int,
    common_count: int,
    id_churn_threshold: float,
) -> NotComparedReason | None:
    """Return a ``NotComparedReason`` when primary-key churn is too high.

    Files whose identifiers are regenerated on every export (e.g. ``shape_id``,
    ``trip_id``) produce two nearly disjoint key sets, so almost every row looks
    added or deleted. We measure how badly the keys fail to match between the
    two feeds and, when it meets or exceeds *id_churn_threshold*, flag the file
    as not comparable instead of emitting a misleading diff.

    The churn ratio is the complement of the **overlap coefficient** —
    ``|common| / min(|base|, |new|)`` — i.e. the fraction of the *smaller*
    feed's keys that have no match in the other feed::

        churn_ratio = 1 − |common| / min(|base|, |new|)

    Dividing by ``min`` (rather than ``max`` or the union, as in Jaccard) makes
    the metric robust to bulk additions or deletions: a feed that merely grows
    or shrinks keeps a high overlap and is *not* mistaken for id regeneration.
    Only when the keys themselves are replaced — the actual signature of churn —
    does the ratio approach 1. See ``docs/architecture.md`` for the rationale.

    Detection is skipped when it cannot yield a reliable signal:

    * files without an explicit primary key (those use all columns as a
      composite key, where any field edit would look like churn);
    * a feed side with no rows (a bulk add or delete, not regenerated ids);
    * files too small for near-total turnover to be statistically meaningful
      (see :data:`gtfs_definitions.MIN_ROWS_FOR_ID_CHURN_DETECTION`).
    """
    if not pk_is_explicit:
        return None

    smaller = min(base_row_count, new_row_count)
    if smaller < MIN_ROWS_FOR_ID_CHURN_DETECTION:
        return None

    churn_ratio = 1.0 - (common_count / smaller)
    if churn_ratio < id_churn_threshold:
        return None

    pct = round(churn_ratio * 100, 1)
    return NotComparedReason(
        code="id_churn",
        message=(
            f"{pct}% of primary key values {pk_cols} differ between the base "
            f"and new feed, indicating the identifiers are regenerated across "
            f"versions and cannot be reliably matched. Row-level comparison was "
            f"skipped to avoid a misleading diff."
        ),
    )


def _build_not_compared_diff(
    file_name: str,
    reason: NotComparedReason,
    columns_added: list[ColumnEntry],
    columns_deleted: list[ColumnEntry],
    base_row_count: int,
    new_row_count: int,
) -> tuple[FileDiff, FileSummary]:
    """Build the ``not_compared`` result for a file that could not be diffed.

    ``row_changes`` is omitted (``None``) while column-level differences are
    preserved, per the GTFS Diff v2 schema.
    """
    file_diff = FileDiff(
        file_name=file_name,
        file_action="not_compared",
        not_compared_reason=reason,
        columns_added=columns_added,
        columns_deleted=columns_deleted,
        row_changes=None,
        stats=FileStats(
            total_rows_base=base_row_count,
            total_rows_new=new_row_count,
            columns_added_count=len(columns_added),
            columns_deleted_count=len(columns_deleted),
        ),
    )
    summary = FileSummary(file_name=file_name, status="not_compared")
    return file_diff, summary


def _shared_columns(
    base_headers: list[str],
    new_header_set: set[str],
    ignored: set[str],
) -> list[str]:
    """Columns present in both feeds (in base order) excluding *ignored* ones.

    The shared set is the basis for the "modified" comparison: a column added or
    removed between versions is a column-level change, not a row-level one, so it
    must not trigger false row modifications. *ignored* columns (unreliable
    foreign keys to not_compared files) are likewise skipped.
    """
    return [c for c in base_headers if c in new_header_set and c not in ignored]


def _scan_modifications(
    file_name: str,
    common_keys: set[tuple],
    base_index: dict[tuple, tuple[int, str]],
    new_index: dict[tuple, tuple[int, str]],
    base_headers: list[str],
    new_headers: list[str],
    ignored_columns: set[str] | None = None,
) -> list[tuple[tuple, list[FieldChange], int, int]]:
    """Scan rows present in both feeds and return those whose field values differ.

    Compares only columns shared between both headers to avoid false positives
    when a column is added or removed. Columns in *ignored_columns* (unreliable
    foreign keys to not_compared files) are also skipped.

    Returns a list of (pk_tuple, field_changes, base_line, new_line) for
    every common row that has at least one changed field.

    Note: row reorders (same rows, different line positions) are silently
    ignored — keys are compared as sets, so row order has no effect.
    """
    ignored = ignored_columns or set()
    shared_cols = _shared_columns(base_headers, set(new_headers), ignored)
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


def _split_row_changes_cap(
    cap: int | None,
    true_added: int,
    true_deleted: int,
    true_modified: int,
) -> tuple[int | None, int | None, int | None]:
    """Split a row-changes *cap* fairly across the change types that have rows.

    Rather than filling the cap with added rows first, then deleted, then
    modified (which can hide whole change types when one is large), the budget
    is shared evenly between the types that actually have changes so the user
    sees a little of everything: with one active type it gets the whole cap,
    with two they split it ~50/50, with three ~33/33/33.

    Allocation uses water-filling: the budget is divided evenly among the types
    that still have rows to show; any leftover (because a type has fewer rows
    than its share) is redistributed to the remaining hungry types until the cap
    is exhausted or every change is included. A remainder that cannot divide
    evenly is handed out one row at a time in added → deleted → modified order.

    Returns ``(added_limit, deleted_limit, modified_limit)``. When *cap* is
    ``None`` (unlimited) all three limits are ``None``.
    """
    if cap is None:
        return None, None, None

    counts = [true_added, true_deleted, true_modified]
    limits = [0, 0, 0]
    active = [i for i, c in enumerate(counts) if c > 0]
    remaining = cap

    while remaining > 0:
        hungry = [i for i in active if limits[i] < counts[i]]
        if not hungry:
            break
        share = remaining // len(hungry)
        if share == 0:
            # Indivisible remainder: hand out one row at a time, in type order.
            for i in hungry:
                if remaining == 0:
                    break
                limits[i] += 1
                remaining -= 1
            break
        for i in hungry:
            grant = min(share, counts[i] - limits[i])
            limits[i] += grant
            remaining -= grant

    return limits[0], limits[1], limits[2]


def _compute_rows_changed_percentage(
    rows_added: int,
    rows_deleted: int,
    rows_modified: int,
    total_base: int,
    total_new: int,
) -> float | None:
    """Percentage of rows changed relative to the larger of the two versions.

    Returns ``None`` when both versions are empty (no meaningful denominator).
    The count of changed rows is the *true* total (added + deleted + modified),
    so the percentage is unaffected by any row-changes cap/truncation. With heavy
    churn the raw count can exceed the larger version's row count (added rows
    exist only in the new version, deleted rows only in the base), so the result
    is clamped to ``100.0`` to satisfy the schema's ``[0, 100]`` bound.
    """
    denominator = max(total_base, total_new)
    if denominator == 0:
        return None
    changed = rows_added + rows_deleted + rows_modified
    return round(min(changed / denominator * 100.0, 100.0), 2)


def _build_column_stats(
    column_mod_counts: dict[str, int],
    total_modified: int,
    column_order: list[str],
) -> list[ColumnStat] | None:
    """Build per-column modification statistics for a modified file.

    *column_mod_counts* maps a column name to the number of modified rows that
    changed in that column (a *true* count, independent of any cap). Only
    columns with at least one modification are included, ordered by their
    appearance in *column_order* for deterministic output. Returns ``None`` when
    there are no per-column modifications to report.
    """
    if total_modified == 0 or not column_mod_counts:
        return None
    stats = [
        ColumnStat(
            column=col,
            modifications_count=column_mod_counts[col],
            modifications_percentage=round(
                column_mod_counts[col] / total_modified * 100.0, 2
            ),
        )
        for col in column_order
        if column_mod_counts.get(col)
    ]
    return stats or None


def _build_identifier_and_raw(
    row_dict: dict[str, str],
    pk_cols: list[str],
    union_columns: list[str],
    header_set: set[str],
) -> tuple[dict[str, str], str]:
    """Build the ``(identifier, raw_value)`` pair for a single changed row.

    The identifier maps each primary-key column to its value (empty string when
    the column is absent from the row); ``raw_value`` is the row projected onto
    *union_columns*. Shared by both engines so the cap'd row payloads are
    byte-identical regardless of how *row_dict* was produced.
    """
    identifier = {col: row_dict.get(col, "") for col in pk_cols}
    raw_value = _compute_raw_value(row_dict, union_columns, header_set)
    return identifier, raw_value


def _assemble_modified_file_diff(
    *,
    file_name: str,
    pk_cols: list[str],
    union_columns: list[str],
    columns_added: list[ColumnEntry],
    columns_deleted: list[ColumnEntry],
    ignored_columns: list[IgnoredColumn],
    added_rows: list[RowAdded],
    deleted_rows: list[RowDeleted],
    modified_rows: list[RowModified],
    true_added: int,
    true_deleted: int,
    true_modified: int,
    total_base: int,
    total_new: int,
    cap: int | None,
    include_row_changes: bool,
    column_stats: bool,
    column_mod_counts: dict[str, int],
) -> tuple[FileDiff, FileSummary]:
    """Assemble the ``FileDiff``/``FileSummary`` pair for a modified file.

    Shared by both engines so the truncation accounting, ``RowChanges`` payload,
    and ``FileStats`` are built identically regardless of how the rows were
    collected (in-memory dict iteration or DuckDB queries). The caller supplies
    the already-collected (and already cap-limited) row lists plus the *true*
    counts; ``total_base``/``total_new`` are the full row counts of each version.
    """
    truncated: Truncated | None = None
    row_changes: RowChanges | None = None
    if include_row_changes:
        total_included = len(added_rows) + len(deleted_rows) + len(modified_rows)
        total_true = true_added + true_deleted + true_modified
        if cap is not None and total_true > cap:
            truncated = Truncated(
                is_truncated=True, omitted_count=total_true - total_included
            )
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
        ignored_columns=ignored_columns or None,
        columns_added=columns_added,
        columns_deleted=columns_deleted,
        row_changes=row_changes,
        truncated=truncated,
        stats=FileStats(
            total_rows_base=total_base,
            total_rows_new=total_new,
            columns_added_count=len(columns_added),
            columns_deleted_count=len(columns_deleted),
            rows_added_count=true_added,
            rows_deleted_count=true_deleted,
            rows_modified_count=true_modified,
            rows_changed_percentage=_compute_rows_changed_percentage(
                true_added, true_deleted, true_modified, total_base, total_new
            ),
            column_stats=(
                _build_column_stats(column_mod_counts, true_modified, union_columns)
                if column_stats
                else None
            ),
        ),
    )
    summary = FileSummary(file_name=file_name, status="modified")
    return file_diff, summary
