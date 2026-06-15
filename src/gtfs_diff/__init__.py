"""GTFS Diff Engine - compare two GTFS feeds and surface structured differences."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("gtfs-diff-engine")
except PackageNotFoundError:
    __version__ = "unknown"
