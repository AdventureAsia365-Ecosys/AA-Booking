"""Reads of the CIS catalog read model (schema ``catalog``, CIS migration 225) and Booking's commercial rows.

- ``app.catalog.destinations``: ``catalog.v_destination`` lookups for the supplier form (AA-822, bk-008). Re-exported
  here, so ``from app import catalog; catalog.require_destination(...)`` keeps working.
- ``app.catalog.trip_plans``: sellable trip plans over ``catalog.v_tour`` and ``booking.trip_plans`` (AA-679, bk-009).
"""

from app.catalog.destinations import (
    CATALOG_ERRORS,
    DESTINATION_SQL,
    MAX_SEARCH,
    destinations_by_id,
    require_destination,
    search_destinations,
)

__all__ = [
    "CATALOG_ERRORS",
    "DESTINATION_SQL",
    "MAX_SEARCH",
    "destinations_by_id",
    "require_destination",
    "search_destinations",
]
