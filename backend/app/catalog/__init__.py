"""Reads of the CIS catalog read model (schema ``catalog``, CIS migration 225) and Booking's commercial rows.

- ``destinations``: ``catalog.v_destination`` (supplier place picker and check, AA-822); its names are re-exported
  here, so callers keep ``from app import catalog; catalog.require_destination(...)``.
- ``trip_plans``: ``catalog.v_tour*`` joined live to ``booking.trip_plans`` (AA-679, bk-009).
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
