"""Click-based CLI entry point for gtfs-diff."""

import sys
from datetime import datetime
from pathlib import Path

import click

from gtfs_diff.diff.run import diff_json, run_diff
from gtfs_diff.feeds.csv_utils import _is_url
from gtfs_diff.gtfs_definitions import DEFAULT_ID_CHURN_THRESHOLD
from gtfs_diff.highlights import DEFAULT_SHAPE_MOVED_M, DEFAULT_STOP_MOVED_M
from gtfs_diff.matching import (
    DEFAULT_TRIP_MAX_SHIFT_MIN,
    DEFAULT_TRIP_MIN_SHARED_STOPS,
)
from gtfs_diff.report.html import HTML_ROW_CAP, render_html

# Above the column count of any GTFS file, so a capped file can still show
# modified rows for every changed column.
MIN_CAP = 100


def _check_cap(value: int | None) -> int | None:
    if value is not None and value != 0 and value < MIN_CAP:
        raise click.BadParameter(f"must be 0 or at least {MIN_CAP}, got {value}.")
    return value


@click.command()
@click.version_option(package_name="gtfs-diff-engine", prog_name="gtfs-diff-engine")
@click.argument("base_feed", type=str)
@click.argument("new_feed", type=str)
@click.option(
    "--files",
    default=None,
    metavar="NAMES",
    help=(
        "Comma-separated list of GTFS files to compare, e.g. "
        "'stops.txt,trips.txt'. Optional: for folder URLs, omitting it "
        "probes all known GTFS files; for local feeds it restricts the comparison."
    ),
)
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    default=None,
    help="Write JSON output to FILE instead of stdout.",
)
@click.option(
    "--cap",
    "-c",
    type=int,
    default=None,
    callback=lambda _ctx, _param, value: _check_cap(value),
    help=(
        f"Max row changes per file: 0 omits row-level detail, otherwise at "
        f"least {MIN_CAP}, so every changed column can have examples."
    ),
)
@click.option(
    "--pretty/--no-pretty", default=True, help="Pretty-print JSON (default: --pretty)."
)
@click.option(
    "--base-downloaded-at",
    default=None,
    help="ISO 8601 datetime for when base was downloaded.",
)
@click.option(
    "--new-downloaded-at",
    default=None,
    help="ISO 8601 datetime for when new was downloaded.",
)
@click.option(
    "--id-churn-threshold",
    type=click.FloatRange(0.0, 1.0),
    default=DEFAULT_ID_CHURN_THRESHOLD,
    show_default=True,
    help=(
        "Primary-key churn ratio (0.0-1.0) above which a file is reported as "
        "not_compared instead of diffed (detects regenerated ids)."
    ),
)
@click.option(
    "--id-churn-threshold-for",
    type=(str, click.FloatRange(0.0, 1.0)),
    multiple=True,
    metavar="FILENAME RATIO",
    help=(
        "Per-file id-churn threshold override; repeatable. Takes precedence "
        "over --id-churn-threshold. Example: "
        "--id-churn-threshold-for shapes.txt 0.95"
    ),
)
@click.option(
    "--large-file-threshold-mb",
    type=click.FloatRange(0.0),
    default=50.0,
    show_default=True,
    help=(
        "Files whose larger side is at least this many megabytes are diffed "
        "with the built-in DuckDB backend (lower memory for very large files). "
        "Use --no-duckdb to always use the in-memory engine."
    ),
)
@click.option(
    "--no-duckdb",
    is_flag=True,
    default=False,
    help="Disable the DuckDB backend; always use the in-memory engine.",
)
@click.option(
    "--column-stats/--no-column-stats",
    default=True,
    help=(
        "Include per-column modification counts and percentages in each "
        "modified file's stats (default: on). The file-level "
        "rows_changed_percentage is always computed."
    ),
)
@click.option(
    "--id-matching/--no-id-matching",
    default=True,
    help=(
        "Match agencies, stops, routes, services, trips and shapes by content "
        "before comparing rows, so renamed or regenerated IDs are diffed as the "
        "same entity (default: on). See docs/id_matching.md."
    ),
)
@click.option(
    "--trip-max-shift",
    "trip_max_shift",
    type=click.FloatRange(0.0),
    default=DEFAULT_TRIP_MAX_SHIFT_MIN,
    show_default=True,
    metavar="MINUTES",
    help=(
        "With --id-matching, also pair trips whose times moved by at most this "
        "many minutes at every stop, tried in 5-minute steps. 0 pairs only "
        "trips with identical times."
    ),
)
@click.option(
    "--trip-min-shared-stops",
    type=click.FloatRange(0.0, 1.0),
    default=DEFAULT_TRIP_MIN_SHARED_STOPS,
    show_default=True,
    metavar="SHARE",
    help=(
        "With --id-matching, also pair trips that share at least this share of "
        "their stops (0.9 = 90%, counted on the longer trip), with the same "
        "route, direction and days. 1 turns this off."
    ),
)
@click.option(
    "--include-key-changes",
    is_flag=True,
    default=False,
    help=(
        "With --id-matching, report a matched row whose key changed (a new "
        "trip_id, stop_id, shape_id...) as a change of its key columns, in "
        "every file. Off by default: a regenerated ID alone is not a change."
    ),
)
@click.option(
    "--as-of",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    default=None,
    metavar="YYYY-MM-DD",
    help=(
        "Notable highlights: report service days without trips from this date "
        "on (YYYY-MM-DD). Default: today."
    ),
)
@click.option(
    "--highlight-stop-moved-m",
    type=click.FloatRange(0.0),
    default=DEFAULT_STOP_MOVED_M,
    show_default=True,
    metavar="METRES",
    help="Notable highlights: report stops that moved at least this far.",
)
@click.option(
    "--highlight-shape-moved-m",
    type=click.FloatRange(0.0),
    default=DEFAULT_SHAPE_MOVED_M,
    show_default=True,
    metavar="METRES",
    help="Notable highlights: report shapes that moved at least this far.",
)
@click.option(
    "--shape-geometry",
    is_flag=True,
    default=False,
    help=(
        "Draw every changed, added and deleted shape on maps in the HTML "
        "report's Shapes tab. Needs --id-matching and the HTML report."
    ),
)
@click.option(
    "--html",
    "html_path",
    type=click.Path(path_type=Path),
    default=None,
    help=(
        "Where to write the HTML report. Default: next to --output with an "
        ".html suffix, or gtfs-diff.html when printing to stdout."
    ),
)
@click.option(
    "--no-html", is_flag=True, default=False, help="Do not write the HTML report."
)
@click.option(
    "--html-cap",
    type=click.IntRange(0),
    default=HTML_ROW_CAP,
    show_default=True,
    help=(
        "Max rows of each kind of change (added, deleted, modified) per file "
        "shown in the HTML report, and example rows per modified column. "
        "Applied on top of --cap."
    ),
)
def main(
    base_feed: str,
    new_feed: str,
    files: str | None,
    output: Path | None,
    cap: int | None,
    pretty: bool,
    base_downloaded_at: str | None,
    new_downloaded_at: str | None,
    id_churn_threshold: float,
    id_churn_threshold_for: tuple[tuple[str, float], ...],
    large_file_threshold_mb: float,
    no_duckdb: bool,
    column_stats: bool,
    id_matching: bool,
    trip_max_shift: float,
    trip_min_shared_stops: float,
    include_key_changes: bool,
    as_of: datetime | None,
    highlight_stop_moved_m: float,
    highlight_shape_moved_m: float,
    shape_geometry: bool,
    html_path: Path | None,
    no_html: bool,
    html_cap: int,
) -> None:
    """Compare two GTFS feeds and output a JSON diff.

    BASE_FEED: local path or http(s):// folder URL to the base GTFS feed\n
    NEW_FEED:  local path or http(s):// folder URL to the new GTFS feed\n
    Use optional --files with a comma-separated GTFS file list. For URLs,
    omitting --files auto-discovers known GTFS files.
    """
    if shape_geometry and not id_matching:
        click.echo("Error: --shape-geometry needs --id-matching.", err=True)
        sys.exit(1)
    if shape_geometry and no_html:
        click.echo(
            "Error: --shape-geometry draws in the HTML report; remove --no-html.",
            err=True,
        )
        sys.exit(1)

    base_is_url = _is_url(base_feed)
    new_is_url = _is_url(new_feed)

    base_path: str | Path = base_feed if base_is_url else Path(base_feed)
    new_path: str | Path = new_feed if new_is_url else Path(new_feed)

    if isinstance(base_path, Path) and not base_path.exists():
        click.echo(f"Error: {base_path} does not exist.", err=True)
        sys.exit(1)
    if isinstance(new_path, Path) and not new_path.exists():
        click.echo(f"Error: {new_path} does not exist.", err=True)
        sys.exit(1)

    try:
        base_dt = (
            datetime.fromisoformat(base_downloaded_at) if base_downloaded_at else None
        )
        new_dt = (
            datetime.fromisoformat(new_downloaded_at) if new_downloaded_at else None
        )
    except ValueError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    try:
        parsed_files = (
            [f.strip() for f in files.split(",") if f.strip()] if files else None
        )
        large_file_threshold_bytes = (
            None if no_duckdb else int(large_file_threshold_mb * 1024 * 1024)
        )
        run = run_diff(
            base_path=base_path,
            new_path=new_path,
            row_changes_cap_per_file=cap,
            base_downloaded_at=base_dt,
            new_downloaded_at=new_dt,
            id_churn_threshold=id_churn_threshold,
            id_churn_thresholds=dict(id_churn_threshold_for),
            files=parsed_files,
            large_file_threshold_bytes=large_file_threshold_bytes,
            column_stats=column_stats,
            id_matching=id_matching,
            shape_geometry=shape_geometry,
            trip_max_shift_minutes=trip_max_shift,
            trip_min_shared_stops=trip_min_shared_stops,
            include_key_changes=include_key_changes,
            as_of=as_of.date() if as_of else None,
            highlight_stop_moved_m=highlight_stop_moved_m,
            highlight_shape_moved_m=highlight_shape_moved_m,
        )
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    result = run.diff
    json_str = diff_json(result, pretty)

    if output is not None:
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json_str, encoding="utf-8")
        except OSError as exc:
            click.echo(f"Error writing output: {exc}", err=True)
            sys.exit(1)
    else:
        click.echo(json_str)

    if result.highlights is not None:
        found = sum(result.highlights.model_dump(mode="json")["counts"].values())
        click.echo(f"{found} notable highlights", err=True)

    stem = output.with_suffix("") if output is not None else Path("gtfs-diff")
    try:
        if not no_html:
            path = html_path or stem.with_name(stem.name + ".html")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                render_html(run, row_cap=html_cap),
                encoding="utf-8",
            )
            click.echo(f"HTML report written to {path}", err=True)
    except OSError as exc:
        click.echo(f"Error writing output: {exc}", err=True)
        sys.exit(1)
