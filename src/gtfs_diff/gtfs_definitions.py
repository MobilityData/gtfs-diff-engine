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
    "fare_rules.txt": ["fare_id", "route_id", "origin_id", "destination_id", "contains_id"],
    # Shapes / frequencies / transfers
    "shapes.txt": ["shape_id", "shape_pt_sequence"],
    "frequencies.txt": ["trip_id", "start_time"],
    "transfers.txt": ["from_stop_id", "to_stop_id", "from_route_id", "to_route_id", "from_trip_id", "to_trip_id"],
    # Pathways / levels
    "pathways.txt": ["pathway_id"],
    "levels.txt": ["level_id"],
    # Feed metadata
    "feed_info.txt": [],  # single-row file, no primary key
    "translations.txt": ["table_name", "field_name", "language", "record_id", "record_sub_id", "field_value"],
    "attributions.txt": ["attribution_id"],
    # Areas
    "areas.txt": ["area_id"],
    "stop_areas.txt": ["area_id", "stop_id"],
    # Networks
    "networks.txt": ["network_id"],
    "route_networks.txt": ["route_id"],
    # Fares v2
    "fare_media.txt": ["fare_media_id"],
    "fare_products.txt": ["fare_product_id"],
    "fare_leg_rules.txt": ["leg_group_id"],  # partial key, best effort
    "fare_transfer_rules.txt": ["from_leg_group_id", "to_leg_group_id", "transfer_count", "duration_limit"],
    "timeframes.txt": ["timeframe_group_id", "start_time", "end_time", "service_id"],
    # Rider categories / booking
    "rider_categories.txt": ["rider_category_id"],
    "booking_rules.txt": ["booking_rule_id"],
    # Location groups (flex)
    "location_groups.txt": ["location_group_id"],
    "location_group_stops.txt": ["location_group_id", "stop_id"],
}

SUPPORTED_FILES: set[str] = set(GTFS_PRIMARY_KEYS)


def get_primary_key(file_name: str) -> list[str] | None:
    """Return the primary key columns for a supported GTFS file, or None if unsupported."""
    return GTFS_PRIMARY_KEYS.get(file_name)
