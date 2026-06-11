"""Lightweight progress tracing shared across the diff engine."""

from __future__ import annotations

import sys
from datetime import datetime


def _trace(msg: str) -> None:
    """Print a timestamped progress message with current RSS to stderr."""
    import psutil

    rss_mb = psutil.Process().memory_info().rss / 1024 / 1024
    print(
        f"[gtfs-diff {datetime.now().strftime('%H:%M:%S')} {rss_mb:.0f}MB] {msg}",
        file=sys.stderr,
        flush=True,
    )
