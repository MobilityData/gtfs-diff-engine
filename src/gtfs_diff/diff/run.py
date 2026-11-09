"""Comparing two feeds: matching, then every file, then the summary."""

from __future__ import annotations

import configparser
import contextlib
import json
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from importlib import resources
from pathlib import Path

from ..feeds.sources import FeedHandle, _materialized_path, _open_feed
from ..gtfs_definitions import (
    DEFAULT_ID_CHURN_THRESHOLD,
    get_id_churn_threshold,
    get_primary_key,
)
from ..highlights import (
    DEFAULT_SHAPE_MOVED_M,
    DEFAULT_STOP_MOVED_M,
    highlights_section,
    keep_rows,
)
from ..matching import (
    DEFAULT_TRIP_MAX_SHIFT_MIN,
    DEFAULT_TRIP_MIN_SHARED_STOPS,
    MATCH_COLUMNS,
    MatchResult,
    ShapeChange,
    match_feeds,
)
from ..models import (
    FeedSource,
    FileDiff,
    FileSummary,
    GtfsDiff,
    Metadata,
    Summary,
    UnsupportedFile,
)
from ..tracing import _trace
from .files import DEFAULT_LARGE_FILE_THRESHOLD_BYTES, _diff_file
from .helpers import (
    _processing_order,
)


def _read_schema_version() -> str:
    conf_text = resources.files("gtfs_diff").joinpath("schema.conf").read_text()
    parser = configparser.ConfigParser()
    parser.read_string("[default]\n" + conf_text)
    return parser.get("default", "SCHEMA_VERSION")


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
    id_matching: bool = True,
    trip_max_shift_minutes: float = DEFAULT_TRIP_MAX_SHIFT_MIN,
    include_key_changes: bool = False,
) -> GtfsDiff:
    """Compare two GTFS feeds and return a structured :class:`GtfsDiff` result.

    Args:
        base_path:               Path to the base (old) GTFS feed — zip or directory.
        new_path:                Path to the new GTFS feed — zip or directory.
        row_changes_cap_per_file:
            * ``None``  — include all row changes (default).
            * ``0``     — omit all row-level detail (column diffs and counts
                          still computed).
            * ``N > 0`` — include up to *N* row changes per file, with the budget
                          split fairly across the change types that have rows
                          (added / deleted / modified) so a little of each is
                          shown — one active type gets the whole cap, two split
                          it ~50/50, three ~33/33/33, with any leftover
                          redistributed to types that still have rows; a
                          :class:`Truncated` record is attached when the true
                          count exceeds *N*.
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
        id_matching:
            When ``True`` (default), agencies, stops, routes, services, trips
            and shapes are matched by content before the diff, so rows of an
            entity whose ID changed are compared with its old rows. How each
            entity was matched is on :class:`DiffRun` (``run_diff``), not in
            the diff. See ``docs/id_matching.md``.
        trip_max_shift_minutes:
            With *id_matching*, trips left unpaired are still paired when their
            times moved by at most this many minutes at every stop (default
            15). ``0`` pairs only trips with identical times.
        include_key_changes:
            With *id_matching*, a matched entity whose ID changed is by default
            not a change. ``True`` reports changed key columns of paired rows as
            field changes, in every file.

    Files are diffed in foreign-key dependency order (referenced files first).
    When a referenced file is marked ``not_compared`` due to id churn, the
    foreign-key column(s) pointing at it in any referencing file are excluded
    from that file's field-level diff and reported under ``ignored_columns``.
    The returned ``file_diffs`` / ``summary.files`` are sorted by file name.

    Returns:
        A fully-populated :class:`GtfsDiff` instance.
    """
    return run_diff(
        base_path,
        new_path,
        row_changes_cap_per_file=row_changes_cap_per_file,
        base_downloaded_at=base_downloaded_at,
        new_downloaded_at=new_downloaded_at,
        id_churn_threshold=id_churn_threshold,
        id_churn_thresholds=id_churn_thresholds,
        files=files,
        large_file_threshold_bytes=large_file_threshold_bytes,
        column_stats=column_stats,
        id_matching=id_matching,
        trip_max_shift_minutes=trip_max_shift_minutes,
        include_key_changes=include_key_changes,
    ).diff


@dataclass
class DiffRun:
    """A diff plus what matching found that does not fit the diff document."""

    diff: GtfsDiff
    shape_changes: list[ShapeChange] | None = None
    # Kept out of the diff document so the JSON keeps to the GTFS Diff v2
    # schema; the HTML report reads them from here.
    # How each entity was matched (counts per entity and step).
    matching: dict | None = None
    # Every option the diff ran with.
    parameters: dict = field(default_factory=dict)
    # Files compared and found equal: [{"file_name", "rows"}].
    files_unchanged: list[dict] = field(default_factory=list)
    # {file name: {base line number: key as published in the new feed}} for
    # modified rows whose key changed (matched entities with new IDs).
    new_identifiers: dict[str, dict[int, dict[str, str]]] = field(default_factory=dict)
    # Time the run took, from opening the feeds to the finished diff.
    duration_seconds: float | None = None


def run_diff(
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
    id_matching: bool = True,
    shape_geometry: bool = False,
    trip_max_shift_minutes: float = DEFAULT_TRIP_MAX_SHIFT_MIN,
    include_key_changes: bool = False,
    trip_min_shared_stops: float = DEFAULT_TRIP_MIN_SHARED_STOPS,
    as_of: date | None = None,
    highlight_stop_moved_m: float = DEFAULT_STOP_MOVED_M,
    highlight_shape_moved_m: float = DEFAULT_SHAPE_MOVED_M,
) -> DiffRun:
    """:func:`diff_feeds`, also returning what the diff document leaves out.

    With *id_matching*, the diff has notable highlights: service days from
    *as_of* (default today) that lost all their trips, removed stops, and stops
    and shapes that moved at least *highlight_stop_moved_m* /
    *highlight_shape_moved_m* metres. With *shape_geometry*, the run also has
    every changed shape's points.
    """
    started = time.monotonic()
    now = datetime.now(timezone.utc)
    base_downloaded_at = base_downloaded_at or now
    new_downloaded_at = new_downloaded_at or now

    file_diffs: list[FileDiff] = []
    file_summaries: list[FileSummary] = []
    unsupported_files: list[UnsupportedFile] = []
    match: MatchResult | None = None
    matching_summary: dict | None = None
    # Compared and found equal: absent from file_diffs (per the schema).
    files_unchanged: list[dict] = []
    if files is not None:
        files = list(files)
    parameters = {
        "row_changes_cap_per_file": row_changes_cap_per_file,
        "id_churn_threshold": id_churn_threshold,
        "id_churn_thresholds": dict(id_churn_thresholds or {}),
        "files": files,
        "large_file_threshold_bytes": large_file_threshold_bytes,
        "column_stats": column_stats,
        "id_matching": id_matching,
        "trip_max_shift_minutes": trip_max_shift_minutes,
        "trip_min_shared_stops": trip_min_shared_stops,
        "include_key_changes": include_key_changes,
        "shape_geometry": shape_geometry,
        "as_of": (as_of or date.today()).isoformat(),
        "highlight_stop_moved_m": highlight_stop_moved_m,
        "highlight_shape_moved_m": highlight_shape_moved_m,
    }
    highlight_settings = {
        "as_of": as_of or date.today(),
        "stop_moved_m": highlight_stop_moved_m,
        "shape_moved_m": highlight_shape_moved_m,
    }

    with (
        _open_feed(base_path, files) as base_handle,
        _open_feed(new_path, files) as new_handle,
        contextlib.ExitStack() as cleanup,
    ):
        if id_matching:
            match, matching_summary = _run_matching(
                base_handle,
                new_handle,
                shape_geometry,
                trip_max_shift_minutes,
                include_key_changes,
                highlight_settings,
                trip_min_shared_stops,
            )
            if match is not None:
                cleanup.callback(match.close)
        # Rows behind notable highlights stay in the diff whatever the cap.
        keep = keep_rows(match.highlights) if match and match.highlights else {}
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
                translation=(
                    match.translation_for(file_name, include_key_changes)
                    if match
                    else None
                ),
                keep=keep.get(file_name),
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
                files_unchanged.append(
                    {"file_name": file_name, "rows": stats.total_rows_base}
                )
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
    highlights = None
    if match is not None and match.highlights is not None:
        highlights = highlights_section(
            match.highlights,
            {**highlight_settings, "as_of": highlight_settings["as_of"].isoformat()},
        )
    result = GtfsDiff(
        metadata=metadata,
        summary=summary,
        file_diffs=file_diffs,
        highlights=highlights,
    )
    _trace("diff_feeds complete")
    return DiffRun(
        diff=result,
        shape_changes=match.shape_changes if match else None,
        matching=matching_summary,
        parameters=parameters,
        files_unchanged=sorted(files_unchanged, key=lambda f: f["file_name"]),
        new_identifiers=_take_new_identifiers(file_diffs),
        duration_seconds=round(time.monotonic() - started, 2),
    )


def diff_json(diff: GtfsDiff, pretty: bool = True) -> str:
    """The diff as GTFS Diff v2 JSON. Empty values are left out, except the caps
    the schema requires, where null means no cap."""
    doc = diff.model_dump(mode="json", exclude_none=True)
    doc["metadata"] = diff.metadata.model_dump(mode="json")
    if diff.highlights is not None:
        doc["highlights"] = {
            "cap_per_code": diff.highlights.cap_per_code,
            **doc["highlights"],
        }
    if pretty:
        return json.dumps(doc, indent=2, ensure_ascii=False)
    return json.dumps(doc, separators=(",", ":"), ensure_ascii=False)


def _take_new_identifiers(file_diffs: list[FileDiff]) -> dict:
    """Remove ``new_identifier`` from modified rows (it is not in the schema)
    and return them per file and base line number."""
    out: dict[str, dict[int, dict[str, str]]] = {}
    for fd in file_diffs:
        for row in fd.row_changes.modified if fd.row_changes else []:
            extra = row.__pydantic_extra__ or {}
            if "new_identifier" in extra:
                out.setdefault(fd.file_name, {})[row.base_line_number] = extra.pop(
                    "new_identifier"
                )
    return out


def _run_matching(
    base_handle: FeedHandle,
    new_handle: FeedHandle,
    shape_geometry: bool,
    trip_max_shift_minutes: float,
    include_key_changes: bool,
    highlights: dict,
    trip_min_shared_stops: float,
) -> tuple[MatchResult | None, dict]:
    """Match entities across the feeds; on failure, diff by ID and say why."""
    from .duckdb_backend import _resolve_spill_base

    start = time.monotonic()
    _trace("Matching entities by content...")
    try:
        with contextlib.ExitStack() as stack:
            paths = []
            for handle in (base_handle, new_handle):
                side = {}
                for name in MATCH_COLUMNS:
                    if name in handle.meta:
                        path = stack.enter_context(
                            _materialized_path(handle.meta[name])
                        )
                        if path is not None:
                            side[name] = path
                paths.append(side)
            match = match_feeds(
                paths[0],
                paths[1],
                shape_geometry=shape_geometry,
                spill_dir=_resolve_spill_base(),
                trip_max_shift_min=trip_max_shift_minutes,
                highlights=highlights,
                trip_min_shared_stops=trip_min_shared_stops,
            )
    except Exception as exc:
        _trace(f"  matching failed ({exc!r}) — diffing by ID")
        return None, {"error": str(exc)}
    seconds = round(time.monotonic() - start, 2)
    _trace(f"  matching done in {seconds:.1f}s")
    return match, {
        "trip_max_shift_minutes": trip_max_shift_minutes,
        "include_key_changes": include_key_changes,
        "duration_seconds": seconds,
        "entities": match.summary,
    }
