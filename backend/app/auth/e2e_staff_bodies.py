"""Body checks of the Dev-only E2E writers (root ADR 0006a A10e-1..A10e-5), beside :mod:`app.auth.e2e_staff`.

A leaf module: pure functions of the parsed JSON body, no database, no import of the auth core.
:mod:`app.auth.e2e_staff` maps each allow-listed write to one of them (``BODY_CHECKS``) and runs it before any DB access
(403 ``e2e_read_only`` when it answers False). Split out of that file so it stays within the code-health size cap.

TEMPORARY: removed with the A10 family at go-live (AA-1054), with :mod:`app.auth.e2e_staff`.
"""

from __future__ import annotations

from typing import Any

# Every supplier, hotel and service name the maker creates starts with this (checked on the create body, and the
# supplier keeps it: a renamed supplier is no longer synthetic).
SYNTHETIC_SUPPLY_MARK = "SYNTHETIC"

# A10e-2: the only workbench actions the pricing maker sends (switch sellable on and back off; never a provisional
# price or a recompute).
PRICING_WORKBENCH_ACTIONS = frozenset({"make_sellable", "make_not_sellable"})
# The body field of a body-checked write that names the trip plans it touches (each one is checked synthetic).
BODY_TOUR_KEYS = "tour_keys"


def workbench_body_ok(body: Any) -> bool:
    """Exactly ``{"action": <a PRICING_WORKBENCH_ACTIONS value>, "tour_keys": [<tour key>, ...]}``, at least one key,
    every key a string (the route's own model bounds the count; each key is then checked synthetic)."""
    if not isinstance(body, dict) or set(body) != {"action", BODY_TOUR_KEYS}:
        return False
    keys = body[BODY_TOUR_KEYS]
    return (
        body["action"] in PRICING_WORKBENCH_ACTIONS
        and isinstance(keys, list)
        and len(keys) > 0
        and all(isinstance(k, str) for k in keys)
    )


def _fields_ok(body: Any, required: frozenset[str], allowed: frozenset[str], marked: str | None = None) -> bool:
    """A JSON object holding every ``required`` key and no key outside ``allowed``; ``marked``, when given, names a
    text field that must start with :data:`SYNTHETIC_SUPPLY_MARK`."""
    if not isinstance(body, dict) or not required <= set(body) or not set(body) <= allowed:
        return False
    return marked is None or (isinstance(body[marked], str) and body[marked].strip().startswith(SYNTHETIC_SUPPLY_MARK))


# A10e-3 create bodies: the marked name and the facts the matcher and the cost sheet read; never a contact, address,
# note, link or photo (no personal or free-text data is written by an E2E identity).
_SUPPLIER_FIELDS = frozenset({"company_name", "categories", "country_code", "destination_id"})
_HOTEL_FIELDS = frozenset({"supplier_id", "name", "destination_id", "country_code", "star_rating"})
_SERVICE_FIELDS = frozenset(
    {"supplier_id", "service_type", "title", "unit", "destination_id", "country_code", "source_id"}
)
_ROOM_TYPE_FIELDS = frozenset({"name", "max_occupancy", "bed_types"})
# A contract moves only by its status, sent alone (publish by the checker; expire at cleanup).
CONTRACT_STATUSES = frozenset({"published", "expired"})


def supplier_body_ok(body: Any) -> bool:
    return _fields_ok(body, frozenset({"company_name", "categories"}), _SUPPLIER_FIELDS, "company_name")


def hotel_body_ok(body: Any) -> bool:
    return _fields_ok(body, frozenset({"supplier_id", "name"}), _HOTEL_FIELDS, "name")


def service_body_ok(body: Any) -> bool:
    return _fields_ok(body, frozenset({"supplier_id", "service_type", "title"}), _SERVICE_FIELDS, "title")


def room_type_body_ok(body: Any) -> bool:
    return _fields_ok(body, frozenset({"name"}), _ROOM_TYPE_FIELDS)


def contract_body_ok(body: Any) -> bool:
    return isinstance(body, dict) and set(body) == {"status"} and body["status"] in CONTRACT_STATUSES


def no_body(body: Any) -> bool:
    """A deactivation carries no reason text (empty body or ``{}``)."""
    return body is None or body == {}


# A10e-5 (AA-1153 S289): link the maker's synthetic hotel to a second synthetic DMC. The DMC's own code and the
# preferred flag only; both ends are checked synthetic from the path and the body (app.auth.e2e_staff_supply).
_LINK_FIELDS = frozenset({"supplier_id", "supplier_hotel_code", "is_preferred"})


def link_body_ok(body: Any) -> bool:
    return _fields_ok(body, frozenset({"supplier_id"}), _LINK_FIELDS)


# A10e-5: a desk entry (quote, hold, confirm for an agency) of room lines only. The agency reference carries the mark
# (the synthetic agency's CRM shows it), no price override, no departure seat, no room type named outside the line.
DESK_ROOM_PREFIX = "rt:"
_DESK_FIELDS = frozenset({"partner_id", "agency_ref", "currency", "lines"})
_DESK_LINE_FIELDS = frozenset(
    {"line_ref", "component_id", "service_date", "nights", "pax", "rooms", "meal_plan", "child_ages"}
)


def desk_room_ids(body: Any) -> list[str]:
    """The room type ids of a desk entry's lines (the text after ``rt:``); [] when the body is not one."""
    lines = body.get("lines") if isinstance(body, dict) else None
    if not isinstance(lines, list):
        return []
    ids = [line.get("component_id") for line in lines if isinstance(line, dict)]
    return [i[len(DESK_ROOM_PREFIX) :] for i in ids if isinstance(i, str) and i.lower().startswith(DESK_ROOM_PREFIX)]


def _desk_line_ok(line: Any) -> bool:
    if not isinstance(line, dict) or not {"component_id", "pax"} <= set(line) or not set(line) <= _DESK_LINE_FIELDS:
        return False
    rooms = line.get("rooms") or []
    return isinstance(rooms, list) and all(isinstance(r, dict) and r.get("room_type_id") is None for r in rooms)


def desk_entry_body_ok(body: Any) -> bool:
    """Exactly the entry fields (no ``override_total`` / ``override_reason``), the agency reference marked, at least one
    line, every line an ``rt:`` room line without ``departure_id`` and without a room's own ``room_type_id``."""
    if not _fields_ok(body, _DESK_FIELDS, _DESK_FIELDS, "agency_ref"):
        return False
    lines = body["lines"]
    if not isinstance(lines, list) or not lines or not all(_desk_line_ok(line) for line in lines):
        return False
    return len(desk_room_ids(body)) == len(lines)
