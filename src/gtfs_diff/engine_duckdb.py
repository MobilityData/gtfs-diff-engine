"""DuckDB-backed diff for very large GTFS files.

This module mirrors the per-file "modified" diff produced by
:func:`gtfs_diff.engine._diff_file_modified`, but offloads the heavy
set-arithmetic and row scanning to DuckDB (a C++ engine that spills to disk)
so that files with millions of rows can be compared without loading every row
into a Python dict.

It is intentionally only used for the *common* case of a file that exists in
both feeds and has a simple, fully-present, explicit primary key (the routing
guard in :mod:`gtfs_diff.engine` enforces this). All other cases —
added/deleted files, empty/optional primary keys, id-churn edge cases — stay on
the in-memory engine, whose behavior this module reuses helper-by-helper to
guarantee byte-identical output.

Parity is preserved by:

* reading clean (BOM/whitespace-stripped) headers with the same reader the
  in-memory engine uses, and aliasing DuckDB's positional columns to them;
* coalescing SQL ``NULL`` (DuckDB's representation of an empty CSV field) to
  ``""`` everywhere, matching the in-memory padding behavior;
* using SQL only as a *superset* pre-filter for modified rows
  (``IS DISTINCT FROM`` on the raw strings) and letting the in-memory
  :func:`_values_differ` be the final arbiter (case-insensitive, trimmed,
  numeric-aware);
* building the output models with the exact same helpers
  (:func:`_compute_raw_value`, :func:`_compute_ignored_columns`,
  :func:`_detect_id_churn`, :func:`_build_not_compared_diff`).
"""

from __future__ import annotations

import contextlib
import shutil
import tempfile

from .csv_utils import _is_url, _read_headers, _values_differ
from .diff_helpers import (
    _assemble_modified_file_diff,
    _build_identifier_and_raw,
    _build_not_compared_diff,
    _compute_ignored_columns,
    _detect_id_churn,
    _diff_columns,
    _shared_columns,
    _split_row_changes_cap,
)
from .models import (
    FieldChange,
    FileDiff,
    FileSummary,
    RowAdded,
    RowDeleted,
    RowModified,
)
from .tracing import _trace

# Read modest batches when streaming candidate rows so memory stays bounded
# regardless of how many rows differ.
_FETCH_BATCH = 10_000


def is_duckdb_available() -> bool:
    """Return True if the optional ``duckdb`` dependency can be imported."""
    try:
        import duckdb  # noqa: F401
    except Exception:
        return False
    return True


def _q(identifier: str) -> str:
    """Quote a SQL identifier (double-quote, escaping embedded double-quotes)."""
    return '"' + identifier.replace('"', '""') + '"'


def _open_headers(path: str) -> list[str]:
    """Read clean, stripped headers from a local CSV file (BOM-aware)."""
    with open(path, encoding="utf-8-sig", newline="") as f:
        return _read_headers(f)


def _read_headers_via_duckdb(con, path: str) -> list[str]:
    """Read clean, stripped headers from a CSV *path* (local or URL) via DuckDB.

    Used for remote URLs so headers are obtained from a tiny ranged read through
    the ``httpfs`` extension instead of downloading the whole file. The leading
    BOM and surrounding whitespace are stripped to match :func:`_read_headers`.
    """
    probe = con.execute(
        "SELECT * FROM read_csv(?, all_varchar=true, header=true, "
        "null_padding=true, sample_size=-1) LIMIT 0",
        [path],
    )
    return [d[0].lstrip("\ufeff").strip() for d in probe.description]


def _create_table(con, table: str, path: str, clean_headers: list[str]) -> None:
    """Load *path* into *table* in a single ``read_csv`` pass.

    The clean header names are imposed directly via DuckDB's ``column_names``
    parameter (with ``header=true`` so the file's own header row is skipped), so
    no separate schema-probe read is needed. A 1-based ``__line`` column is added
    (header is line 1, so the first data row is line 2) to reproduce the
    in-memory engine's line numbers. Every value column is coalesced to ``''`` so
    an empty CSV field never surfaces as NULL.

    *clean_headers* is derived from this same file's header (a local read or a
    tiny remote probe), so its length matches the file's column count.
    """
    select_cols = ", ".join(f"COALESCE({_q(c)}, '') AS {_q(c)}" for c in clean_headers)
    con.execute(
        f"CREATE TABLE {_q(table)} AS "
        f"SELECT row_number() OVER () + 1 AS __line, {select_cols} "
        f"FROM read_csv(?, all_varchar=true, header=true, "
        f"column_names=?, null_padding=true, sample_size=-1)",
        [path, clean_headers],
    )


def _has_duplicate_pk(con, table: str, pk_cols: list[str]) -> bool:
    """Return True if *table* has more than one row for any primary-key value."""
    pk_list = ", ".join(_q(c) for c in pk_cols)
    row = con.execute(
        f"SELECT 1 FROM {_q(table)} GROUP BY {pk_list} HAVING COUNT(*) > 1 LIMIT 1"
    ).fetchone()
    return row is not None


def diff_modified_duckdb(
    file_name: str,
    base_path: str,
    new_path: str,
    pk_cols: list[str],
    row_changes_cap: int | None,
    id_churn_threshold: float,
    not_compared_files: dict[str, str],
    column_stats: bool = True,
) -> tuple[FileDiff, FileSummary]:
    """Compute the "modified" diff for a large file using DuckDB.

    The caller (``gtfs_diff.engine``) guarantees the file exists in both feeds
    and that *pk_cols* is an explicit primary key fully present in both headers.

    ``base_path`` / ``new_path`` may be local filesystem paths *or* ``http(s)://``
    URLs. URLs are read in place via DuckDB's ``httpfs`` extension (HTTP range
    requests), so the file is never fully downloaded by us.

    Raises:
        ValueError: If a duplicate primary key is found (parity with the
            in-memory engine, which also refuses to diff such a file).
    """
    import duckdb

    remote = _is_url(base_path) or _is_url(new_path)

    # Spill goes to a managed temp dir we delete ourselves, rather than DuckDB's
    # default ``.tmp`` in the current working directory — large-file spill must
    # never litter the caller's CWD and must be cleaned up after each file.
    spill_dir = tempfile.mkdtemp(prefix="gtfs_duckdb_")
    con = duckdb.connect()
    try:
        con.execute("SET preserve_insertion_order=true")
        con.execute("PRAGMA threads=1")  # deterministic row_number() line numbers
        con.execute("SET temp_directory=?", [spill_dir])
        if remote:
            # Read remote files in place over HTTP range requests (no full
            # download on our side — only the HEAD probes done by the engine).
            con.execute("INSTALL httpfs")
            con.execute("LOAD httpfs")

        base_headers = (
            _read_headers_via_duckdb(con, base_path)
            if _is_url(base_path)
            else _open_headers(base_path)
        )
        new_headers = (
            _read_headers_via_duckdb(con, new_path)
            if _is_url(new_path)
            else _open_headers(new_path)
        )

        columns_added, columns_deleted, union_columns = _diff_columns(
            base_headers, new_headers
        )
        base_header_set = set(base_headers)
        new_header_set = set(new_headers)

        _trace(f"  [{file_name}] (duckdb) loading base + new...")
        _create_table(con, "base_t", base_path, base_headers)
        _create_table(con, "new_t", new_path, new_headers)

        if _has_duplicate_pk(con, "base_t", pk_cols):
            raise ValueError(f"{file_name}: duplicate primary key in base feed.")
        if _has_duplicate_pk(con, "new_t", pk_cols):
            raise ValueError(f"{file_name}: duplicate primary key in new feed.")

        total_base = con.execute("SELECT COUNT(*) FROM base_t").fetchone()[0]
        total_new = con.execute("SELECT COUNT(*) FROM new_t").fetchone()[0]

        pk_join = " AND ".join(f"b.{_q(c)} = n.{_q(c)}" for c in pk_cols)
        common_count = con.execute(
            f"SELECT COUNT(*) FROM base_t b JOIN new_t n ON {pk_join}"
        ).fetchone()[0]
        true_added = total_new - common_count
        true_deleted = total_base - common_count

        _trace(
            f"  [{file_name}] (duckdb) counts — base={total_base:,} new={total_new:,} "
            f"common={common_count:,} added={true_added:,} deleted={true_deleted:,}"
        )

        # id-churn gate (identical policy to the in-memory engine).
        not_compared_reason = _detect_id_churn(
            pk_cols=pk_cols,
            pk_is_explicit=True,
            base_row_count=total_base,
            new_row_count=total_new,
            common_count=common_count,
            id_churn_threshold=id_churn_threshold,
        )
        if not_compared_reason is not None:
            _trace(
                f"  [{file_name}] (duckdb) not compared — "
                f"{not_compared_reason.code}: {not_compared_reason.message}"
            )
            return _build_not_compared_diff(
                file_name=file_name,
                reason=not_compared_reason,
                columns_added=columns_added,
                columns_deleted=columns_deleted,
                base_row_count=total_base,
                new_row_count=total_new,
            )

        ignored_columns, ignored_names = _compute_ignored_columns(
            file_name, base_headers, new_headers, pk_cols, not_compared_files
        )
        shared_cols = _shared_columns(base_headers, new_header_set, ignored_names)

        include_row_changes = row_changes_cap != 0
        cap = row_changes_cap

        added_rows: list[RowAdded] = []
        deleted_rows: list[RowDeleted] = []

        # --- Modified: SQL pre-filters to raw-different rows; Python decides. ---
        true_modified, modified_rows, column_mod_counts = _scan_modified(
            con,
            file_name=file_name,
            pk_cols=pk_cols,
            base_headers=base_headers,
            shared_cols=shared_cols,
            union_columns=union_columns,
            base_header_set=base_header_set,
            collect=include_row_changes,
            cap=cap,
        )

        if include_row_changes:
            added_limit, deleted_limit, modified_limit = _split_row_changes_cap(
                cap, true_added, true_deleted, true_modified
            )
            added_rows = _collect_added(
                con,
                pk_cols=pk_cols,
                new_headers=new_headers,
                union_columns=union_columns,
                new_header_set=new_header_set,
                limit=added_limit,
            )
            deleted_rows = _collect_deleted(
                con,
                pk_cols=pk_cols,
                base_headers=base_headers,
                union_columns=union_columns,
                base_header_set=base_header_set,
                limit=deleted_limit,
            )
            if modified_limit is not None:
                modified_rows = modified_rows[:modified_limit]
    finally:
        # Drop the per-file tables so DuckDB releases their buffers, then close
        # the connection (which also frees everything) and remove the spill dir.
        # Tables are dropped explicitly so the intent is clear and so memory is
        # reclaimed promptly even if the connection were ever reused.
        with contextlib.suppress(Exception):
            con.execute("DROP TABLE IF EXISTS base_t")
            con.execute("DROP TABLE IF EXISTS new_t")
        con.close()
        shutil.rmtree(spill_dir, ignore_errors=True)

    _trace(
        f"  [{file_name}] (duckdb) row diff summary — "
        f"added={true_added:,} deleted={true_deleted:,} modified={true_modified:,}"
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
        total_base=total_base,
        total_new=total_new,
        cap=cap,
        include_row_changes=include_row_changes,
        column_stats=column_stats,
        column_mod_counts=column_mod_counts,
    )


def _scan_modified(
    con,
    file_name: str,
    pk_cols: list[str],
    base_headers: list[str],
    shared_cols: list[str],
    union_columns: list[str],
    base_header_set: set[str],
    collect: bool,
    cap: int | None,
) -> tuple[int, list[RowModified], dict[str, int]]:
    """Stream raw-different common rows; apply ``_values_differ`` as final arbiter.

    Returns the *true* modified count, (when *collect*) up to *cap* RowModified
    records, and per-column modification counts over the full modified set
    (true counts, independent of *cap*). Streaming in batches keeps memory
    bounded no matter how many rows changed.
    """
    if not shared_cols:
        return 0, [], {}

    pk_join = " AND ".join(f"b.{_q(c)} = n.{_q(c)}" for c in pk_cols)
    distinct_pred = " OR ".join(
        f"b.{_q(c)} IS DISTINCT FROM n.{_q(c)}" for c in shared_cols
    )

    select_parts = ["b.__line AS b_line", "n.__line AS n_line"]
    select_parts += [f"b.{_q(c)} AS {_q('b__' + c)}" for c in base_headers]
    select_parts += [f"n.{_q(c)} AS {_q('n__' + c)}" for c in shared_cols]
    sql = (
        f"SELECT {', '.join(select_parts)} "
        f"FROM base_t b JOIN new_t n ON {pk_join} "
        f"WHERE {distinct_pred}"
    )

    cur = con.execute(sql)
    col_names = [d[0] for d in cur.description]
    idx = {name: i for i, name in enumerate(col_names)}

    true_modified = 0
    modified_rows: list[RowModified] = []
    column_mod_counts: dict[str, int] = {}
    while True:
        batch = cur.fetchmany(_FETCH_BATCH)
        if not batch:
            break
        for row in batch:
            field_changes = [
                FieldChange(
                    field=col,
                    base_value=row[idx["b__" + col]],
                    new_value=row[idx["n__" + col]],
                )
                for col in shared_cols
                if _values_differ(row[idx["b__" + col]], row[idx["n__" + col]])
            ]
            if not field_changes:
                continue
            true_modified += 1
            for fc in field_changes:
                column_mod_counts[fc.field] = column_mod_counts.get(fc.field, 0) + 1
            if not collect or (cap is not None and len(modified_rows) >= cap):
                continue
            b_dict = {col: row[idx["b__" + col]] for col in base_headers}
            identifier, raw_value = _build_identifier_and_raw(
                b_dict, pk_cols, union_columns, base_header_set
            )
            modified_rows.append(
                RowModified(
                    identifier=identifier,
                    raw_value=raw_value,
                    base_line_number=row[idx["b_line"]],
                    new_line_number=row[idx["n_line"]],
                    field_changes=field_changes,
                )
            )
    return true_modified, modified_rows, column_mod_counts


def _collect_added(
    con,
    pk_cols: list[str],
    new_headers: list[str],
    union_columns: list[str],
    new_header_set: set[str],
    limit: int | None,
) -> list[RowAdded]:
    """Fetch up to *limit* rows present only in the new feed."""
    if limit == 0:
        return []
    pk_eq = " AND ".join(f"b.{_q(c)} = n.{_q(c)}" for c in pk_cols)
    cols = ", ".join(f"n.{_q(c)} AS {_q(c)}" for c in new_headers)
    sql = (
        f"SELECT n.__line AS __line, {cols} FROM new_t n "
        f"WHERE NOT EXISTS (SELECT 1 FROM base_t b WHERE {pk_eq})"
        f" ORDER BY n.__line"
    )
    if limit is not None:
        sql += f" LIMIT {int(limit)}"

    cur = con.execute(sql)
    idx = {d[0]: i for i, d in enumerate(cur.description)}
    rows: list[RowAdded] = []
    for row in cur.fetchall():
        n_dict = {col: row[idx[col]] for col in new_headers}
        identifier, raw_value = _build_identifier_and_raw(
            n_dict, pk_cols, union_columns, new_header_set
        )
        rows.append(
            RowAdded(
                identifier=identifier,
                raw_value=raw_value,
                new_line_number=row[idx["__line"]],
            )
        )
    return rows


def _collect_deleted(
    con,
    pk_cols: list[str],
    base_headers: list[str],
    union_columns: list[str],
    base_header_set: set[str],
    limit: int | None,
) -> list[RowDeleted]:
    """Fetch up to *limit* rows present only in the base feed."""
    if limit == 0:
        return []
    pk_eq = " AND ".join(f"n.{_q(c)} = b.{_q(c)}" for c in pk_cols)
    cols = ", ".join(f"b.{_q(c)} AS {_q(c)}" for c in base_headers)
    sql = (
        f"SELECT b.__line AS __line, {cols} FROM base_t b "
        f"WHERE NOT EXISTS (SELECT 1 FROM new_t n WHERE {pk_eq})"
        f" ORDER BY b.__line"
    )
    if limit is not None:
        sql += f" LIMIT {int(limit)}"

    cur = con.execute(sql)
    idx = {d[0]: i for i, d in enumerate(cur.description)}
    rows: list[RowDeleted] = []
    for row in cur.fetchall():
        b_dict = {col: row[idx[col]] for col in base_headers}
        identifier, raw_value = _build_identifier_and_raw(
            b_dict, pk_cols, union_columns, base_header_set
        )
        rows.append(
            RowDeleted(
                identifier=identifier,
                raw_value=raw_value,
                base_line_number=row[idx["__line"]],
            )
        )
    return rows
