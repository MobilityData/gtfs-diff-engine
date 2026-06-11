"""
GTFS file definitions, primary keys, and schema constants.

Design decisions:
- Primary keys follow the official GTFS Static specification. Where the spec defines
  a composite primary key, all constituent columns are listed.
- Files with an empty primary key list ([]) have no single identifying column (e.g.
  feed_info.txt is a single-row file, agency.txt allows omitting agency_id when there
  is only one agency). For these, the diff engine falls back to treating ALL columns
  collectively as the row identity.
- Files with no known primary key or that are not CSV (e.g. geojson locations) are
  simply absent from GTFS_PRIMARY_KEYS and therefore absent from SUPPORTED_FILES.
  Callers can check ``get_primary_key`` returning None to detect unsupported files.
"""

from collections.abc import Mapping

GTFS_PRIMARY_KEYS: dict[str, list[str]] = {
    # Core required files
    "agency.txt": ["agency_id"],
    "stops.txt": ["stop_id"],
    "routes.txt": ["route_id"],
    "trips.txt": ["trip_id"],
    "stop_times.txt": ["trip_id", "stop_sequence"],
    # Conditionally required
    "calendar.txt": ["service_id"],
    "calendar_dates.txt": ["service_id", "date"],
    # Fares v1
    "fare_attributes.txt": ["fare_id"],
    "fare_rules.txt": [
        "fare_id",
        "route_id",
        "origin_id",
        "destination_id",
        "contains_id",
    ],
    # Shapes / frequencies / transfers
    "shapes.txt": ["shape_id", "shape_pt_sequence"],
    "frequencies.txt": ["trip_id", "start_time"],
    "transfers.txt": [
        "from_stop_id",
        "to_stop_id",
        "from_route_id",
        "to_route_id",
        "from_trip_id",
        "to_trip_id",
    ],
    # Pathways / levels
    "pathways.txt": ["pathway_id"],
    "levels.txt": ["level_id"],
    # Feed metadata
    "feed_info.txt": [],  # single-row file, no primary key
    "translations.txt": [
        "table_name",
        "field_name",
        "language",
        "record_id",
        "record_sub_id",
        "field_value",
    ],
    "attributions.txt": ["attribution_id"],
    # Areas
    "areas.txt": ["area_id"],
    "stop_areas.txt": ["area_id", "stop_id"],
    # Networks
    "networks.txt": ["network_id"],
    "route_networks.txt": ["route_id"],
    # Fares v2
    "fare_media.txt": ["fare_media_id"],
    "fare_products.txt": ["fare_product_id", "rider_category_id", "fare_media_id"],
    "fare_leg_rules.txt": [
        "network_id",
        "from_area_id",
        "to_area_id",
        "from_timeframe_group_id",
        "to_timeframe_group_id",
        "fare_product_id",
    ],
    "fare_transfer_rules.txt": [
        "from_leg_group_id",
        "to_leg_group_id",
        "fare_product_id",
        "transfer_count",
        "duration_limit",
    ],
    "timeframes.txt": ["timeframe_group_id", "start_time", "end_time", "service_id"],
    # Rider categories / booking
    "rider_categories.txt": ["rider_category_id"],
    "booking_rules.txt": ["booking_rule_id"],
    # Location groups (flex)
    "location_groups.txt": ["location_group_id"],
    "location_group_stops.txt": ["location_group_id", "stop_id"],
}

SUPPORTED_FILES: set[str] = set(GTFS_PRIMARY_KEYS)


# Primary-key columns that are only *conditionally* present in a file. When such
# a column is absent from a feed's headers it is treated as a null (empty) value
# for every row during the compare step — the column stays in the effective key
# so both feeds are compared against an identical key structure — rather than
# raising MissingPrimaryKeyError. Only a *mandatory* (always-"Required") missing
# primary-key column is an error.
#
# A primary-key column is listed here when the GTFS Schedule reference
# (https://gtfs.org/documentation/schedule/reference/) gives it any presence
# other than "Required" (i.e. Optional, Conditionally Required, Recommended, or
# Conditionally Forbidden). translations.txt is the canonical case: a translation
# is identified by EITHER ``record_id`` (optionally plus ``record_sub_id``) OR
# ``field_value`` — mutually exclusive, all conditionally required — so a given
# feed only carries the subset it uses.
OPTIONAL_PRIMARY_KEY_COLUMNS: dict[str, set[str]] = {
    # agency_id: Conditionally Required (required only with multiple agencies).
    "agency.txt": {"agency_id"},
    # route_id, origin_id, destination_id, contains_id: all Optional.
    "fare_rules.txt": {"route_id", "origin_id", "destination_id", "contains_id"},
    # All six columns are Conditionally Required or Optional.
    "transfers.txt": {
        "from_stop_id",
        "to_stop_id",
        "from_route_id",
        "to_route_id",
        "from_trip_id",
        "to_trip_id",
    },
    # record_id, record_sub_id, field_value: all Conditionally Required.
    "translations.txt": {"record_id", "record_sub_id", "field_value"},
    # attribution_id: Optional.
    "attributions.txt": {"attribution_id"},
    # rider_category_id, fare_media_id: both Optional (fare_product_id is required).
    "fare_products.txt": {"rider_category_id", "fare_media_id"},
    # network_id, from_area_id, to_area_id, from/to_timeframe_group_id: all Optional
    # (fare_product_id is required).
    "fare_leg_rules.txt": {
        "network_id",
        "from_area_id",
        "to_area_id",
        "from_timeframe_group_id",
        "to_timeframe_group_id",
    },
    # All five columns are Optional or Conditionally Forbidden.
    "fare_transfer_rules.txt": {
        "from_leg_group_id",
        "to_leg_group_id",
        "fare_product_id",
        "transfer_count",
        "duration_limit",
    },
    # start_time, end_time: Conditionally Required.
    "timeframes.txt": {"start_time", "end_time"},
}


def get_primary_key(file_name: str) -> list[str] | None:
    """Return the primary key columns for a supported GTFS file,
    or None if unsupported."""
    return GTFS_PRIMARY_KEYS.get(file_name)


def get_optional_primary_key_columns(file_name: str) -> set[str]:
    """Return the conditionally-present primary-key columns for *file_name*.

    When absent from a feed's headers, these columns are treated as null (empty)
    values for every row during the compare step — the full primary key is kept
    so both feeds compare against an identical key structure — rather than
    triggering MissingPrimaryKeyError. Returns an empty set for files whose
    primary-key columns are all mandatory.
    """
    return OPTIONAL_PRIMARY_KEY_COLUMNS.get(file_name, set())


# ---------------------------------------------------------------------------
# Foreign-key relationships ("file hierarchy")
# ---------------------------------------------------------------------------
#
# Maps a file to its foreign keys: ``{column: (referenced_file, ...)}``. A column
# may reference more than one file (e.g. ``service_id`` is defined in either
# calendar.txt or calendar_dates.txt). These relationships serve two purposes:
#
#   1. Ordering — referenced ("parent") files are diffed before the files that
#      reference them, so a parent's not_compared status is known in advance.
#   2. Ignored columns — when a parent file is not compared because its primary
#      key is unreliable (id_churn), the regenerated key values also appear in
#      the child's foreign-key column. Comparing that column would surface pure
#      churn noise, so it is excluded from the diff and reported under
#      ``ignored_columns`` instead.
#
# Only well-established GTFS relationships are listed. Self-references (e.g.
# stops.parent_station → stops) are intentionally omitted: they add cycles to the
# ordering graph and are moot, since a self-referencing file that churns is
# already reported as not_compared in full.
GTFS_FOREIGN_KEYS: dict[str, dict[str, tuple[str, ...]]] = {
    "stops.txt": {
        "level_id": ("levels.txt",),
    },
    "routes.txt": {
        "agency_id": ("agency.txt",),
    },
    "trips.txt": {
        "route_id": ("routes.txt",),
        "service_id": ("calendar.txt", "calendar_dates.txt"),
        "shape_id": ("shapes.txt",),
    },
    "stop_times.txt": {
        "trip_id": ("trips.txt",),
        "stop_id": ("stops.txt",),
        "location_group_id": ("location_groups.txt",),
        "pickup_booking_rule_id": ("booking_rules.txt",),
        "drop_off_booking_rule_id": ("booking_rules.txt",),
    },
    "calendar_dates.txt": {
        "service_id": ("calendar.txt",),
    },
    "frequencies.txt": {
        "trip_id": ("trips.txt",),
    },
    "transfers.txt": {
        "from_stop_id": ("stops.txt",),
        "to_stop_id": ("stops.txt",),
        "from_route_id": ("routes.txt",),
        "to_route_id": ("routes.txt",),
        "from_trip_id": ("trips.txt",),
        "to_trip_id": ("trips.txt",),
    },
    "fare_attributes.txt": {
        "agency_id": ("agency.txt",),
    },
    "timeframes.txt": {
        "service_id": ("calendar.txt", "calendar_dates.txt"),
    },
    "fare_rules.txt": {
        "fare_id": ("fare_attributes.txt",),
        "route_id": ("routes.txt",),
    },
    "stop_areas.txt": {
        "area_id": ("areas.txt",),
        "stop_id": ("stops.txt",),
    },
    "route_networks.txt": {
        "network_id": ("networks.txt",),
        "route_id": ("routes.txt",),
    },
    "pathways.txt": {
        "from_stop_id": ("stops.txt",),
        "to_stop_id": ("stops.txt",),
    },
    "location_group_stops.txt": {
        "location_group_id": ("location_groups.txt",),
        "stop_id": ("stops.txt",),
    },
    "fare_products.txt": {
        "rider_category_id": ("rider_categories.txt",),
        "fare_media_id": ("fare_media.txt",),
    },
    "fare_leg_rules.txt": {
        "network_id": ("networks.txt",),
        "from_area_id": ("areas.txt",),
        "to_area_id": ("areas.txt",),
        "from_timeframe_group_id": ("timeframes.txt",),
        "to_timeframe_group_id": ("timeframes.txt",),
        "fare_product_id": ("fare_products.txt",),
    },
    "fare_transfer_rules.txt": {
        "fare_product_id": ("fare_products.txt",),
    },
}


def get_foreign_keys(file_name: str) -> dict[str, tuple[str, ...]]:
    """Return ``{column: (referenced_file, ...)}`` foreign keys for *file_name*.

    Returns an empty dict for files with no known foreign keys.
    """
    return GTFS_FOREIGN_KEYS.get(file_name, {})


# ---------------------------------------------------------------------------
# Primary-key churn ("generated id") detection
# ---------------------------------------------------------------------------
#
# Some GTFS producers regenerate primary-key values on every export (e.g.
# shape_id in shapes.txt, trip_id in trips.txt, service_id in calendar*.txt).
# When that happens a primary-key based comparison reports nearly every row as
# both added and deleted, which is misleading. The engine measures the "churn
# ratio" as the complement of the overlap coefficient — the fraction of the
# SMALLER feed's primary keys that have no match in the other feed:
#
#     churn_ratio = 1 - |common| / min(|base|, |new|)
#
# and, when it meets or exceeds the file's threshold, marks the file as
# ``not_compared`` with reason code ``id_churn`` instead of emitting a diff.
# Dividing by ``min`` (rather than ``max`` or the union) keeps the metric robust
# to bulk additions/deletions, which preserve a high overlap and so are NOT
# mistaken for regenerated ids. See docs/architecture.md for the rationale.
#
# Thresholds are expressed as a churn ratio in the range [0.0, 1.0]: 0.0 flags
# any unmatched key, 1.0 only flags a file whose keys are entirely disjoint.

# Default churn ratio above which a file is considered uncomparable. Chosen to
# be conservative so that ordinary large updates are still reported as a normal
# diff; only near-total key turnover (the signature of regenerated ids) trips it.
DEFAULT_ID_CHURN_THRESHOLD: float = 0.7

# Built-in per-file overrides for files whose primary keys are known to be
# volatile. This is the project's baseline domain knowledge; callers can layer
# their own per-file overrides on top at call time (see ``get_id_churn_threshold``
# and ``diff_feeds``) without mutating this module-level mapping.
ID_CHURN_THRESHOLDS: dict[str, float] = {}

# Minimum number of rows required on *both* feed sides before id-churn detection
# runs. Below this, near-total key turnover is not a reliable signal of
# regenerated ids (it is just as likely an ordinary edit to a tiny file).
MIN_ROWS_FOR_ID_CHURN_DETECTION: int = 50


def get_id_churn_threshold(
    file_name: str,
    default: float = DEFAULT_ID_CHURN_THRESHOLD,
    overrides: Mapping[str, float] | None = None,
) -> float:
    """Return the id-churn threshold for *file_name*.

    Resolution order, highest precedence first:

    1. *overrides* — a caller-supplied ``{file_name: threshold}`` mapping (e.g.
       passed to :func:`gtfs_diff.engine.diff_feeds`). Lets callers tune a single
       file without touching module state.
    2. :data:`ID_CHURN_THRESHOLDS` — the project's built-in per-file defaults.
    3. *default* — the global fallback (``DEFAULT_ID_CHURN_THRESHOLD`` unless the
       caller overrides the global threshold).
    """
    if overrides is not None and file_name in overrides:
        return overrides[file_name]
    return ID_CHURN_THRESHOLDS.get(file_name, default)
