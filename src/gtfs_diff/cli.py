"""Click-based CLI entry point for gtfs-diff."""

import sys
from datetime import datetime
from pathlib import Path

import click

from gtfs_diff.engine import diff_feeds


@click.command()
@click.version_option(version="0.1.0", prog_name="gtfs-diff-engine")
@click.argument("base_feed", type=click.Path(exists=True, path_type=Path))
@click.argument("new_feed", type=click.Path(exists=True, path_type=Path))
@click.option("--output", "-o", type=click.Path(path_type=Path), default=None,
              help="Write JSON output to FILE instead of stdout.")
@click.option("--cap", "-c", type=int, default=None,
              help="Max row changes per file (0 = omit row-level detail).")
@click.option("--pretty/--no-pretty", default=True,
              help="Pretty-print JSON (default: --pretty).")
@click.option("--base-downloaded-at", default=None,
              help="ISO 8601 datetime for when base was downloaded.")
@click.option("--new-downloaded-at", default=None,
              help="ISO 8601 datetime for when new was downloaded.")
def main(
    base_feed: Path,
    new_feed: Path,
    output: Path | None,
    cap: int | None,
    pretty: bool,
    base_downloaded_at: str | None,
    new_downloaded_at: str | None,
) -> None:
    """Compare two GTFS feeds (zip or directory) and output a JSON diff.

    BASE_FEED: path to the base GTFS feed (zip or directory)\n
    NEW_FEED:  path to the new GTFS feed (zip or directory)
    """
    try:
        base_dt = datetime.fromisoformat(base_downloaded_at) if base_downloaded_at else None
        new_dt = datetime.fromisoformat(new_downloaded_at) if new_downloaded_at else None
    except ValueError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    try:
        result = diff_feeds(
            base_path=base_feed,
            new_path=new_feed,
            row_changes_cap_per_file=cap,
            base_downloaded_at=base_dt,
            new_downloaded_at=new_dt,
        )
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    json_str = result.model_dump_json(indent=2) if pretty else result.model_dump_json()

    if output is not None:
        try:
            output.write_text(json_str, encoding="utf-8")
        except OSError as exc:
            click.echo(f"Error writing output: {exc}", err=True)
            sys.exit(1)
    else:
        click.echo(json_str)
