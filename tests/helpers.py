"""Shared helper functions for building in-memory GTFS zip archives."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path


def make_gtfs_zip(files: dict[str, str]) -> bytes:
    """Build an in-memory GTFS zip archive from a {filename: csv_content} dict."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def write_zip(path: Path, files: dict[str, str]) -> Path:
    """Write a GTFS zip to *path* and return the path."""
    path.write_bytes(make_gtfs_zip(files))
    return path
