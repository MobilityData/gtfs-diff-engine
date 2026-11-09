"""Self-contained HTML report for a diff. Works offline; only the optional
street map behind enlarged shapes is loaded from the internet. See
docs/html_report.md.
"""

from __future__ import annotations

import csv
import io
import json
from importlib import resources

from ..diff.run import DiffRun
from ..geometry import METERS_PER_DEGREE
from ..highlights import REPLACEMENT_M, capped, keep_rows
from ..matching import ShapeChange
from ..models import GtfsDiff
from .shape_points import HTML_SHAPE_POINTS, _drawn_points, pack

# Rows shown per file for each kind of change (added, deleted, modified), and
# example rows kept for each modified column. Applied on top of the JSON cap:
# the report never shows rows the JSON does not hold.
HTML_ROW_CAP = 100
# Shapes drawn in the geometry tab, worst first, and points kept per line.
HTML_SHAPE_CAP = 100
# Notable highlights shown per kind; the rest are counted ("and 12 more").
HTML_HIGHLIGHTS_SHOWN = 5
DRAWN_ORDER = ("changed", "added", "deleted", "redrawn")


def render_html(
    run: DiffRun | GtfsDiff,
    row_cap: int = HTML_ROW_CAP,
    shape_cap: int = HTML_SHAPE_CAP,
) -> str:
    """Return the report page for a run (or a bare diff) as a string.

    A :class:`DiffRun` also gives the settings, the unchanged files, the new
    keys of modified rows and the shape maps, which the diff itself leaves out.
    """
    if isinstance(run, GtfsDiff):
        run = DiffRun(diff=run)
    diff = run.diff
    metadata = diff.metadata.model_dump(mode="json")
    metadata["parameters"] = run.parameters or None
    metadata["duration_seconds"] = run.duration_seconds
    summary = diff.summary.model_dump(mode="json", exclude={"files"})
    summary["files_unchanged"] = run.files_unchanged
    found = diff.highlights
    shown = None
    if found is not None:
        items = [h.model_dump(mode="json", exclude_none=True) for h in found.items]
        shown = capped(items, HTML_HIGHLIGHTS_SHOWN)
    # Rows behind the highlights shown are always in the page, to jump to.
    focus = keep_rows(shown or [])
    payload = {
        "metadata": metadata,
        "summary": summary,
        "files": [
            _file_payload(
                fd,
                row_cap,
                run.new_identifiers.get(fd.file_name, {}),
                focus.get(fd.file_name, set()),
            )
            for fd in diff.file_diffs
        ]
        + [_unchanged_payload(f) for f in run.files_unchanged],
        "caps": {"rows": row_cap, "shapes": shape_cap, "points": HTML_SHAPE_POINTS},
        "shapes": _shapes_payload(run.shape_changes, shape_cap, shown),
        "highlights": shown,
        "highlight_counts": (
            found.model_dump(mode="json")["counts"] if found is not None else None
        ),
        "highlight_parameters": found.parameters if found is not None else None,
        "settings": settings_rows(run.parameters, row_cap),
        "constants": constants(),
    }
    data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    template = resources.files("gtfs_diff.report").joinpath("template.html").read_text()
    return template.replace("/*DATA*/null", data.replace("</", "<\\/"))


def constants() -> dict:
    """Engine values the page uses."""
    from ..cli import MIN_CAP  # the CLI imports this module

    return {
        "meters_per_degree": METERS_PER_DEGREE,
        "stop_replacement_m": REPLACEMENT_M,
        "min_cap": MIN_CAP,
    }


def settings_rows(parameters: dict | None, html_cap: int) -> list[list[str]] | None:
    """Every option the diff ran with: [CLI flag, value, its --help text]."""
    if not parameters:
        return None
    import click

    from ..cli import main  # the CLI imports this module

    p = parameters

    def on(v):
        return "on" if v else "off"

    def pct(v):
        return "–" if v is None else f"{round(v * 100)}%"

    churn = p.get("id_churn_thresholds") or {}
    size = p.get("large_file_threshold_bytes")
    cap = p.get("row_changes_cap_per_file")
    values = {
        "cap": "none" if cap is None else f"{cap:,}",
        "html_cap": f"{html_cap:,}",
        "files": ", ".join(p["files"]) if p.get("files") else "all files",
        "id_matching": on(p.get("id_matching")),
        "trip_max_shift": f"{p.get('trip_max_shift_minutes', 0):g} minutes",
        "trip_min_shared_stops": pct(p.get("trip_min_shared_stops")),
        "include_key_changes": on(p.get("include_key_changes")),
        "shape_geometry": on(p.get("shape_geometry")),
        "id_churn_threshold": pct(p.get("id_churn_threshold")),
        "id_churn_threshold_for": ", ".join(f"{f} {pct(v)}" for f, v in churn.items())
        or "none",
        "large_file_threshold_mb": "off" if size is None else f"{size / 1024**2:g} MB",
        "column_stats": on(p.get("column_stats")),
        "as_of": p.get("as_of") or "today",
        "highlight_stop_moved_m": f"{p.get('highlight_stop_moved_m', 0):g} m",
        "highlight_shape_moved_m": f"{p.get('highlight_shape_moved_m', 0):g} m",
    }
    options = {o.name: o for o in main.params if isinstance(o, click.Option)}
    return [[options[k].opts[0], v, options[k].help or ""] for k, v in values.items()]


def _split(raw: str) -> list[str]:
    return next(csv.reader(io.StringIO(raw)), [])


def _unchanged_payload(f: dict) -> dict:
    """A file compared and found equal (summary.files_unchanged)."""
    return {
        "name": f["file_name"],
        "action": "unchanged",
        "stats": {"total_rows_base": f.get("rows"), "total_rows_new": f.get("rows")},
        "columns_added": [],
        "columns_deleted": [],
        "ignored": [],
    }


def _file_payload(
    fd, row_cap: int, new_identifiers: dict | None = None, focus: set | None = None
) -> dict:
    stats = fd.stats.model_dump(exclude_none=True) if fd.stats else {}
    out = {
        "name": fd.file_name,
        "action": fd.file_action,
        "reason": fd.not_compared_reason.message if fd.not_compared_reason else None,
        "stats": stats,
        "columns_added": [c.name for c in fd.columns_added or []],
        "columns_deleted": [c.name for c in fd.columns_deleted or []],
        "ignored": [
            {"column": c.column, "message": c.reason.message}
            for c in fd.ignored_columns or []
        ],
    }
    rc = fd.row_changes
    if rc is None:
        return out
    out["primary_key"] = rc.primary_key
    out["columns"] = rc.columns
    out["added"] = [
        [r.new_line_number, _split(r.raw_value)] for r in rc.added[:row_cap]
    ]
    focus = focus or set()

    def key(r):
        return tuple(r.identifier.get(c, "") for c in rc.primary_key)

    out["deleted"] = [
        [r.base_line_number, _split(r.raw_value)]
        for i, r in enumerate(rc.deleted)
        if i < row_cap or key(r) in focus
    ]
    # The rows shown at first: the first row_cap modified rows and those behind
    # notable highlights. Then up to row_cap more for each changed column, so
    # the report can show a column's changes even when they are rare.
    first = {i for i, r in enumerate(rc.modified) if i < row_cap or key(r) in focus}
    extra: set[int] = set()
    per_column: dict[str, int] = {}
    for i, r in enumerate(rc.modified):
        for c in r.field_changes:
            if per_column.get(c.field, 0) < row_cap:
                per_column[c.field] = per_column.get(c.field, 0) + 1
                if i not in first:
                    extra.add(i)
    keep = [*sorted(first), *sorted(extra)]
    out["modified_first"] = len(first)
    out["modified_in_json"] = len(rc.modified)
    out["modified"] = [
        {
            "id": r.identifier,
            "new_id": (new_identifiers or {}).get(r.base_line_number),
            "lines": [r.base_line_number, r.new_line_number],
            "values": _split(r.raw_value),
            "changes": {c.field: [c.base_value, c.new_value] for c in r.field_changes},
        }
        for r in (rc.modified[i] for i in keep)
    ]
    return out


def _shape_pair(h: dict) -> tuple[str | None, str | None]:
    """The base and new shape_id a highlight refers to."""
    ids = {
        r["feed"]: r["identifier"].get("shape_id")
        for r in h.get("references") or []
        if r["file_name"] == "shapes.txt"
    }
    return ids.get("base"), ids.get("new")


def _shapes_payload(
    changes: list[ShapeChange] | None, cap: int, highlights: list[dict] | None = None
) -> dict | None:
    if changes is None:
        return None
    # Shapes behind the highlights shown are always drawn, to open from them.
    wanted = {_shape_pair(h) for h in highlights or [] if h["code"] == "shape_moved"}
    counts: dict[str, int] = {}
    for c in changes:
        counts[c.change] = counts.get(c.change, 0) + 1
    drawn = _round_robin(
        [[c for c in changes if c.change == k] for k in DRAWN_ORDER], cap
    )
    drawn += [
        c
        for c in changes
        if (c.base_shape_id, c.new_shape_id) in wanted and c not in drawn
    ]
    renamed = [c for c in changes if c.change == "renamed"][:cap]
    return {
        "counts": counts,
        "items": [
            {
                "change": c.change,
                "base": c.base_shape_id,
                "new": c.new_shape_id,
                "dev": c.deviation_m,
                "base_points": c.base_points,
                "new_points": c.new_points,
                "base_len": c.base_length_m,
                "new_len": c.new_length_m,
                "route": c.route,
                "base_coords": pack(_drawn_points(c.base_coords, c.new_coords)),
                "new_coords": pack(_drawn_points(c.new_coords, c.base_coords)),
            }
            for c in drawn
        ],
        "renamed": [[c.base_shape_id, c.new_shape_id, c.route] for c in renamed],
    }


def _round_robin(groups: list[list], cap: int) -> list:
    """Up to *cap* items, taking one from each group in turn, in group order."""
    taken = [0] * len(groups)
    total = 0
    while total < cap and any(t < len(g) for t, g in zip(taken, groups, strict=True)):
        for i, g in enumerate(groups):
            if total < cap and taken[i] < len(g):
                taken[i] += 1
                total += 1
    return [item for t, g in zip(taken, groups, strict=True) for item in g[:t]]
