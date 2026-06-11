"""Core diff engine: loads two GTFS feeds and computes structured differences.

Memory note
-----------
The default two-pass algorithm builds in-memory indexes mapping primary-key
tuples to (line_number, raw_csv_string) for every row in each file.  For typical
transit feeds this is fine.  For very large feeds (stop_times.txt can exceed
10 M rows) this becomes expensive, so files whose larger side exceeds
``large_file_threshold_bytes`` are routed to the DuckDB backend
(:mod:`gtfs_diff.engine_duckdb`), which diffs them on disk without holding every
row in memory.  DuckDB is a runtime dependency; in the unlikely event it is
unavailable the engine falls back to the in-memory path.
"""

from __future__ import annotations

import configparser
import contextlib
import io
import os
import shutil
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable, Generator, Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import TextIO

# Re-exported for backward compatibility (``gtfs_diff.engine`` has historically
# been the import site for these helpers); they now live in focused modules.
from .csv_utils import (
    DuplicatePrimaryKeyError,  # noqa: F401  (re-export)
    MissingPrimaryKeyError,  # noqa: F401  (re-export)
    _is_url,
    _missing_required_pk_columns,
    _parse_raw_line,
    _read_csv_index,
    _read_headers,
    _read_headers_and_count,
)
from .diff_helpers import (
    _assemble_modified_file_diff,
    _build_identifier_and_raw,
    _build_not_compared_diff,
    _compute_ignored_columns,
    _detect_id_churn,
    _diff_columns,
    _duplicate_primary_key_reason,
    _missing_primary_key_reason,
    _processing_order,
    _scan_modifications,
    _split_row_changes_cap,
)
from .gtfs_definitions import (
    DEFAULT_ID_CHURN_THRESHOLD,
    SUPPORTED_FILES,
    get_id_churn_threshold,
    get_optional_primary_key_columns,
    get_primary_key,
)
from .models import (
    ColumnEntry,
    FeedSource,
    FileDiff,
    FileStats,
    FileSummary,
    GtfsDiff,
    Metadata,
    RowAdded,
    RowDeleted,
    RowModified,
    Summary,
    UnsupportedFile,
)
from .tracing import _trace


def _read_schema_version() -> str:
    conf_text = resources.files("gtfs_diff").joinpath("schema.conf").read_text()
    parser = configparser.ConfigParser()
    parser.read_string("[default]\n" + conf_text)
    return parser.get("default", "SCHEMA_VERSION")


# A "lazy opener" maps a filename (e.g. "stops.txt") to a zero-arg callable
# that opens the file and returns a text stream.
LazyOpeners = dict[str, Callable[[], TextIO]]


@dataclass
class FeedFileMeta:
    """Side-channel metadata about a feed file, used to route large files to the
    DuckDB backend without changing the text-opener fast path.

    Attributes:
        size:        Uncompressed size in bytes, if cheaply known (directory
                     ``stat``, zip ``file_size``, or HTTP ``Content-Length``);
                     ``None`` when unknown.
        local_path:  Real filesystem path DuckDB can read directly (set only for
                     plain-directory feeds); ``None`` for zip members and URLs.
        materialize: Writes the file's raw bytes to a given destination path,
                     streaming (bounded memory). Used to stage zip members to a
                     temp file for DuckDB.
        url:         Direct ``http(s)://`` URL of the file. When set, DuckDB reads
                     it in place via its ``httpfs`` extension (HTTP range
                     requests) instead of us downloading the whole file first —
                     our side only performs HEAD requests (size / existence).
    """

    size: int | None = None
    local_path: str | None = None
    materialize: Callable[[str], None] | None = None
    url: str | None = None


@dataclass
class FeedHandle:
    """An opened feed: lazy text openers plus per-file routing metadata."""

    openers: LazyOpeners = field(default_factory=dict)
    meta: dict[str, FeedFileMeta] = field(default_factory=dict)


# Files whose larger side is at least this many bytes (uncompressed) are routed
# to the DuckDB backend, when available. ~50 MB roughly corresponds to the point
# where an in-memory index of a wide GTFS file (e.g. stop_times.txt) becomes
# expensive; tune via ``diff_feeds(large_file_threshold_bytes=...)``.
DEFAULT_LARGE_FILE_THRESHOLD_BYTES = 50 * 1024**2


@contextmanager
def _materialized_path(
    meta: FeedFileMeta | None,
) -> Generator[str | None, None, None]:
    """Yield a real on-disk path *or* a remote URL for a feed file (for DuckDB),
    or None.

    Remote files expose their direct URL, which DuckDB reads in place via its
    ``httpfs`` extension (HTTP range requests) — nothing is downloaded by us.
    Directory feeds expose their original path directly (no copy). Zip members
    are streamed to a temporary file that is removed on exit, so nothing is left
    on disk after processing.
    """
    if meta is None:
        yield None
        return
    if meta.url is not None:
        yield meta.url
        return
    if meta.local_path is not None:
        yield meta.local_path
        return
    if meta.materialize is None:
        yield None
        return
    fd, tmp_name = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    try:
        meta.materialize(tmp_name)
        yield tmp_name
    finally:
        with contextlib.suppress(OSError):
            Path(tmp_name).unlink()


# ---------------------------------------------------------------------------
# Feed opener
# ---------------------------------------------------------------------------


@contextmanager
def _open_feed(
    path: str | Path, files: Iterable[str] | None = None
) -> Generator[FeedHandle, None, None]:
    """Open a GTFS feed (zip archive, directory, or HTTP folder URL).

    Yields a :class:`FeedHandle` whose ``openers`` map each ``.txt`` name to a
    zero-arg callable returning a utf-8-sig text stream (callers close it), and
    whose ``meta`` carries size / materialization info used to route large files
    to the DuckDB backend.

    Supports:
    * ``.zip`` archives (files at root *or* inside a single sub-directory).
    * Plain directories containing ``.txt`` files.
    * ``http(s)://`` folder URLs (e.g. a public GCP bucket folder). Remote
      folders cannot be listed, so *files* must be supplied: it is the
      authoritative list of ``.txt`` names to compare. Each name is probed with
      an HTTP request to determine presence (so added/deleted files are still
      detected) and fetched lazily.

    Args:
        path:  Feed location — a path to a zip/directory, or an ``http(s)://``
               folder URL.
        files: Optional explicit list of file names to consider. For local
               feeds this *filters* the discovered files to just these names;
               for remote URL feeds it is *required* and authoritative.
    """
    requested = list(files) if files is not None else None

    if _is_url(path):
        yield _open_remote_feed(str(path), requested)
        return

    path = Path(path)

    if path.is_dir():
        openers: LazyOpeners = {}
        meta: dict[str, FeedFileMeta] = {}
        for txt_file in sorted(path.glob("*.txt")):

            def _make_opener(p: Path) -> Callable[[], TextIO]:
                return lambda: p.open(encoding="utf-8-sig")

            def _make_materialize(p: Path) -> Callable[[str], None]:
                def _materialize(dest: str) -> None:
                    with p.open("rb") as src, open(dest, "wb") as out:
                        shutil.copyfileobj(src, out)

                return _materialize

            openers[txt_file.name] = _make_opener(txt_file)
            try:
                size: int | None = txt_file.stat().st_size
            except OSError:
                size = None
            meta[txt_file.name] = FeedFileMeta(
                size=size,
                local_path=str(txt_file),
                materialize=_make_materialize(txt_file),
            )
        yield _filter_handle(FeedHandle(openers, meta), requested)

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
            meta = {}
            for basename, internal_path in name_map.items():

                def _make_opener(ip: str) -> Callable[[], TextIO]:  # type: ignore[misc]
                    return lambda: io.TextIOWrapper(zf.open(ip), encoding="utf-8-sig")

                def _make_materialize(ip: str) -> Callable[[str], None]:
                    def _materialize(dest: str) -> None:
                        with zf.open(ip, "r") as src, open(dest, "wb") as out:
                            shutil.copyfileobj(src, out)

                    return _materialize

                openers[basename] = _make_opener(internal_path)
                try:
                    zsize: int | None = zf.getinfo(internal_path).file_size
                except KeyError:
                    zsize = None
                meta[basename] = FeedFileMeta(
                    size=zsize,
                    local_path=None,
                    materialize=_make_materialize(internal_path),
                )
            yield _filter_handle(FeedHandle(openers, meta), requested)
        finally:
            zf.close()

    else:
        raise ValueError(
            f"Unsupported feed path: {path!r}.  Must be a .zip file, a directory, "
            f"or an http(s):// folder URL."
        )


def _filter_handle(handle: FeedHandle, requested: list[str] | None) -> FeedHandle:
    """Restrict a feed handle's openers/meta to the requested file names (if any)."""
    if requested is None:
        return handle
    wanted = set(requested)
    return FeedHandle(
        openers={n: o for n, o in handle.openers.items() if n in wanted},
        meta={n: m for n, m in handle.meta.items() if n in wanted},
    )


# ---------------------------------------------------------------------------
# Remote (HTTP folder URL) feed support
# ---------------------------------------------------------------------------

_HTTP_TIMEOUT_SECONDS = 30


def _join_url(base_url: str, name: str) -> str:
    """Join a folder *base_url* and a file *name* into a single object URL."""
    return base_url.rstrip("/") + "/" + name.lstrip("/")


def _http_exists(url: str) -> bool:
    """Return True if *url* points at a fetchable object, False if it is absent.

    Tries a cheap ``HEAD`` first, falling back to a single-byte ranged ``GET``
    when the server rejects ``HEAD``.

    Object stores such as Google Cloud Storage are common hosts for GTFS feeds,
    where the *folder* is often private (no list permission) even though the
    individual files are public. In that configuration a request for a file
    that does **not** exist returns ``403 Forbidden`` (the server will not
    confirm or deny existence without list permission) rather than ``404``.
    We therefore treat 401/403/404/410 as "absent / not fetchable" instead of
    raising, so probing a missing file simply skips it (and is reported as an
    added/deleted file when appropriate).
    """
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SECONDS):
            return True
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 410):
            return False
        # 403/401 may mean "missing in a private folder" *or* "HEAD not allowed
        # on an existing object"; 405/501 mean HEAD is unsupported. A ranged GET
        # disambiguates: it succeeds for a real (public) file and fails for a
        # missing one.
        if exc.code in (401, 403, 405, 501):
            return _http_exists_via_get(url)
        raise


def _http_exists_via_get(url: str) -> bool:
    """Fallback existence check using a ranged ``GET`` for servers without HEAD.

    Treats 401/403/404/410 as "absent / not fetchable" (see :func:`_http_exists`
    for why a missing file in a private folder reports ``403``).
    """
    req = urllib.request.Request(url, method="GET", headers={"Range": "bytes=0-0"})
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SECONDS):
            return True
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403, 404, 410):
            return False
        raise


def _http_get_text(url: str) -> TextIO:
    """Fetch *url* and return its body as a utf-8-sig decoded text stream."""
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SECONDS) as resp:
        data = resp.read()
    return io.TextIOWrapper(io.BytesIO(data), encoding="utf-8-sig")


def _http_content_length(url: str) -> int | None:
    """Return the ``Content-Length`` of *url* (bytes), or None if unavailable.

    Used only as a cheap size hint for routing large files to DuckDB; any
    failure simply yields ``None`` (treated as "unknown size").
    """
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SECONDS) as resp:
            length = resp.headers.get("Content-Length")
            return int(length) if length is not None else None
    except (urllib.error.URLError, ValueError, OSError):
        return None


def _http_stream_to_file(url: str, dest: str) -> None:
    """Stream *url* to *dest* on disk with bounded memory (for the DuckDB path)."""
    req = urllib.request.Request(url, method="GET")
    with (
        urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SECONDS) as resp,
        open(dest, "wb") as out,
    ):
        shutil.copyfileobj(resp, out)


def _open_remote_feed(base_url: str, files: list[str] | None) -> FeedHandle:
    """Build a feed handle for an HTTP folder URL.

    Remote folders cannot be listed, so the file names to fetch are determined
    up front: either the caller-supplied *files* (authoritative) or, when none
    are given, every known GTFS file (:data:`gtfs_definitions.SUPPORTED_FILES`).
    Each candidate name is probed for presence (so missing files are correctly
    treated as added/deleted) and fetched lazily only when its opener is called.
    """
    candidates = list(files) if files else sorted(SUPPORTED_FILES)

    openers: LazyOpeners = {}
    meta: dict[str, FeedFileMeta] = {}
    for name in candidates:
        file_url = _join_url(base_url, name)
        if not _http_exists(file_url):
            continue

        def _make_opener(u: str) -> Callable[[], TextIO]:
            return lambda: _http_get_text(u)

        def _make_materialize(u: str) -> Callable[[str], None]:
            return lambda dest: _http_stream_to_file(u, dest)

        openers[name] = _make_opener(file_url)
        meta[name] = FeedFileMeta(
            size=_http_content_length(file_url),
            local_path=None,
            materialize=_make_materialize(file_url),
            url=file_url,
        )
    return FeedHandle(openers, meta)


# ---------------------------------------------------------------------------
# Per-file diff
# ---------------------------------------------------------------------------


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

    from . import engine_duckdb

    if not engine_duckdb.is_duckdb_available():
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
            return engine_duckdb.diff_modified_duckdb(
                file_name=file_name,
                base_path=base_path,
                new_path=new_path,
                pk_cols=pk_def,
                row_changes_cap=row_changes_cap,
                id_churn_threshold=id_churn_threshold,
                not_compared_files=not_compared_files,
                column_stats=column_stats,
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
) -> tuple[FileDiff, FileSummary]:
    """Compute the diff for a file present in both feeds."""
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
                f, pk_cols, file_name=file_name, side="new"
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
        deleted_order = sorted(deleted_keys, key=lambda k: base_index[k][0])
        for pk_tuple in deleted_order[:deleted_limit]:
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

        # Fill modified rows up to allocated cap (earliest rows first, by line).
        modified_order = sorted(modified_candidates, key=lambda c: c[2])
        for pk_tuple, field_changes, b_line, n_line in modified_order[:modified_limit]:
            b_raw = base_index[pk_tuple][1]
            b_dict = _parse_raw_line(b_raw, base_headers)
            identifier, raw_value = _build_identifier_and_raw(
                b_dict, pk_cols, union_columns, base_header_set
            )
            modified_rows.append(
                RowModified(
                    identifier=identifier,
                    raw_value=raw_value,
                    base_line_number=b_line,
                    new_line_number=n_line,
                    field_changes=field_changes,
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


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def diff_feeds(
    base_path: str | Path,
    new_path: str | Path,
    row_changes_cap_per_file: int | None = None,
    base_downloaded_at: datetime | None = None,
    new_downloaded_at: datetime | None = None,
    id_churn_threshold: float = DEFAULT_ID_CHURN_THRESHOLD,
    id_churn_thresholds: Mapping[str, float] | None = None,
    files: Iterable[str] | None = None,
    large_file_threshold_bytes: int | None = DEFAULT_LARGE_FILE_THRESHOLD_BYTES,
    column_stats: bool = True,
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
        id_churn_threshold:
            Global churn ratio (in ``[0.0, 1.0]``) above which a file is marked
            ``not_compared`` because its primary keys appear to be regenerated
            across versions. Applies to every file that has no more specific
            override.
        id_churn_thresholds:
            Optional ``{file_name: threshold}`` mapping of caller-supplied
            per-file overrides. These take precedence over both the built-in
            :data:`gtfs_definitions.ID_CHURN_THRESHOLDS` defaults and
            *id_churn_threshold*, letting callers tune individual files (e.g.
            ``{"shapes.txt": 0.95}``) without mutating module state.
        files:
            Optional explicit list of GTFS file names to compare. For local
            zip/directory feeds this *restricts* the comparison to just these
            files. For ``http(s)://`` folder URLs it names the files to fetch
            (e.g. ``["stops.txt", "trips.txt"]``); when omitted, every known
            GTFS file (:data:`gtfs_definitions.SUPPORTED_FILES`) is probed for
            existence and any that are present are compared. The same list is
            applied to both the base and new feeds.
        large_file_threshold_bytes:
            Files whose larger side is at least this many (uncompressed) bytes
            are routed to the DuckDB backend, which diffs them without holding
            every row in memory. Defaults to
            :data:`DEFAULT_LARGE_FILE_THRESHOLD_BYTES` (50 MB). Pass ``None`` to
            disable DuckDB entirely (always use the in-memory engine). The
            backend is used only when the file's size is cheaply known and its
            primary key is a simple explicit key; otherwise the in-memory engine
            is used.
        column_stats:
            When ``True`` (default), each modified file's ``stats.column_stats``
            is populated with per-column modification counts and percentages
            (true counts, unaffected by *row_changes_cap_per_file*). Pass
            ``False`` to omit the per-column breakdown from the output. The
            file-level ``stats.rows_changed_percentage`` is always computed.

    Files are diffed in foreign-key dependency order (referenced files first).
    When a referenced file is marked ``not_compared`` due to id churn, the
    foreign-key column(s) pointing at it in any referencing file are excluded
    from that file's field-level diff and reported under ``ignored_columns``.
    The returned ``file_diffs`` / ``summary.files`` are sorted by file name.

    Returns:
        A fully-populated :class:`GtfsDiff` instance.
    """
    now = datetime.now(timezone.utc)
    base_downloaded_at = base_downloaded_at or now
    new_downloaded_at = new_downloaded_at or now

    file_diffs: list[FileDiff] = []
    file_summaries: list[FileSummary] = []
    unsupported_files: list[UnsupportedFile] = []

    with (
        _open_feed(base_path, files) as base_handle,
        _open_feed(new_path, files) as new_handle,
    ):
        base_openers = base_handle.openers
        new_openers = new_handle.openers
        all_files = sorted(set(base_openers) | set(new_openers))
        # Diff referenced (parent) files before the files that reference them so
        # a parent's id_churn status is known when its children are compared.
        process_files = _processing_order(all_files)
        _trace(f"Found {len(all_files)} file(s) to process: {', '.join(process_files)}")

        # Files marked not_compared because their key values are unreliable —
        # regenerated identifiers (id_churn) or a missing required primary key.
        # Foreign-key columns pointing at these files are ignored in any
        # referencing file's diff. Maps file name -> not_compared reason code.
        not_compared_files: dict[str, str] = {}

        for file_name in process_files:
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
                id_churn_threshold=get_id_churn_threshold(
                    file_name,
                    default=id_churn_threshold,
                    overrides=id_churn_thresholds,
                ),
                not_compared_files=not_compared_files,
                base_meta=base_handle.meta.get(file_name),
                new_meta=new_handle.meta.get(file_name),
                large_file_threshold_bytes=large_file_threshold_bytes,
                use_duckdb=large_file_threshold_bytes is not None,
                column_stats=column_stats,
            )

            # Record any not_compared file (id_churn or missing primary key) so
            # that files referencing this one can ignore the corresponding
            # foreign-key column, the same way id-churn references are ignored.
            reason = file_diff.not_compared_reason
            if reason is not None:
                not_compared_files[file_name] = reason.code

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

    # Restore a deterministic, file-name-sorted output order (independent of the
    # dependency-driven processing order).
    file_diffs.sort(key=lambda fd: fd.file_name)
    file_summaries.sort(key=lambda fs: fs.file_name)

    # Build summary aggregates.
    files_added = sum(1 for s in file_summaries if s.status == "added")
    files_deleted = sum(1 for s in file_summaries if s.status == "deleted")
    files_modified = sum(1 for s in file_summaries if s.status == "modified")
    files_not_compared = sum(1 for s in file_summaries if s.status == "not_compared")

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
        files_not_compared_count=files_not_compared,
        files=file_summaries,
    )
    result = GtfsDiff(metadata=metadata, summary=summary, file_diffs=file_diffs)
    _trace("diff_feeds complete")
    return result
