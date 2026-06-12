"""Click-based CLI entry point for gtfs-diff."""

import sys
from datetime import datetime
from pathlib import Path

import click

from gtfs_diff.engine import _is_url, diff_feeds
from gtfs_diff.gtfs_definitions import DEFAULT_ID_CHURN_THRESHOLD


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
    help="Max row changes per file (0 = omit row-level detail).",
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
) -> None:
    """Compare two GTFS feeds and output a JSON diff.

    BASE_FEED: local path or http(s):// folder URL to the base GTFS feed\n
    NEW_FEED:  local path or http(s):// folder URL to the new GTFS feed\n
    Use optional --files with a comma-separated GTFS file list. For URLs,
    omitting --files auto-discovers known GTFS files.
    """
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
        result = diff_feeds(
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
        )
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    if pretty:
        json_str = result.model_dump_json(indent=2, exclude_none=True)
    else:
        json_str = result.model_dump_json(exclude_none=True)

    if output is not None:
        try:
            output.write_text(json_str, encoding="utf-8")
        except OSError as exc:
            click.echo(f"Error writing output: {exc}", err=True)
            sys.exit(1)
    else:
        click.echo(json_str)
