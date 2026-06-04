"""Shared pytest fixtures for the gtfs-diff test suite."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers import write_zip

# ---------------------------------------------------------------------------
# Reusable fixtures
# ---------------------------------------------------------------------------

MINIMAL_BASE_FILES = {
    "stops.txt": (
        "stop_id,stop_name,stop_lat,stop_lon\n"
        "S1,Stop One,1.0,2.0\n"
        "S2,Stop Two,3.0,4.0\n"
    ),
}

MINIMAL_NEW_FILES = {
    "stops.txt": (
        "stop_id,stop_name,stop_lat,stop_lon\n"
        "S1,Stop One,1.0,2.0\n"
        "S3,Stop Three,5.0,6.0\n"
    ),
}


@pytest.fixture
def minimal_base_zip(tmp_path: Path) -> Path:
    """Return the path to a small GTFS zip for use in integration tests."""
    return write_zip(tmp_path / "base.zip", MINIMAL_BASE_FILES)


@pytest.fixture
def minimal_new_zip(tmp_path: Path) -> Path:
    """Return the path to a small GTFS zip for use in integration tests."""
    return write_zip(tmp_path / "new.zip", MINIMAL_NEW_FILES)
