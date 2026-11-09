"""Former home of the diff engine, kept so ``from gtfs_diff.engine import
diff_feeds`` keeps working. The code now lives in :mod:`gtfs_diff.diff`."""

from .diff.files import DEFAULT_LARGE_FILE_THRESHOLD_BYTES
from .diff.run import DiffRun, diff_feeds, run_diff
from .feeds.csv_utils import DuplicatePrimaryKeyError, MissingPrimaryKeyError

__all__ = [
    "DEFAULT_LARGE_FILE_THRESHOLD_BYTES",
    "DiffRun",
    "DuplicatePrimaryKeyError",
    "MissingPrimaryKeyError",
    "diff_feeds",
    "run_diff",
]
