"""The supply items of a case's approved version as Generate books them (AA-682 S1, bk-026): why an item gets no
booking, its net from its cost rows, its nights and the booking row it becomes. Split out of
:mod:`app.ops.supplier_bookings` (file size ratchet, AA-1149); the tour components of a sold B2B tour are
:mod:`app.ops.component_bookings`."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

import asyncpg

from app.errors import Conflict, Invalid, NotFound
from app.supply import rates as supply_rates


def item_skip_reason(case: Any, item: Any) -> str | None:
    """Why an approved supply item gets no booking from Generate (None = it gets one)."""
    if item["live_booking"] is not None:
        return "booked"
    if item["supplier_id"] is None:
        return "no_supplier"
    if case["channel"] == "b2b":
        if item["b2b_line_status"] in ("declined", "cancelled") or item["b2b_declined"]:
            return "b2b_line_closed"
    return None


def require_active_supplier(booking: Any) -> None:
    """A booking is requested only from an active supplier (409 ``supplier_inactive``): one deactivated after Generate
    (or while it ran) keeps its pending bookings, which staff then cancel or move to another supplier."""
    if booking["supplier_status"] != "active":
        raise Conflict(
            "supplier_inactive",
            f"supplier {booking['supplier_code']} is {booking['supplier_status']}: choose another supplier first",
        )


def net(item: Any) -> tuple[Decimal | None, str | None]:
    """Sum of the item's cost rows in their one currency (critic note 17); mixed currencies are refused."""
    amounts = list(item["net_amounts"] or [])
    currencies = [c.strip() for c in (item["net_currencies"] or []) if c]
    if not amounts:
        return None, None
    if len(currencies) > 1:
        raise Conflict(
            "mixed_cost_currencies",
            f"item {item['position']} has cost rows in {', '.join(sorted(currencies))}; add its booking by hand",
            position=item["position"],
        )
    return sum((Decimal(a) for a in amounts), Decimal("0")), currencies[0]


def nights(case: Any, item: Any) -> int | None:
    """Room nights of a generated booking, on the convention the item was priced and sold on.

    B2B items come from a held B2B line, where ``holds._open_case`` writes ``day_to = day_from + nights - 1``.
    B2C items are costed per ``supply_rates.quantity('room_night')`` = max(day_to - day_from, 1), so the booking
    asks the supplier for exactly the nights its net covers."""
    if not item["hotel_room_type_id"]:
        return None
    day_from, day_to = int(item["day_from"]), int(item["day_to"])
    if case["channel"] == "b2b":
        return day_to - day_from + 1
    return supply_rates.quantity("room_night", day_from=day_from, day_to=day_to, pax=None)


def from_item(case: Any, item: Any) -> dict[str, Any]:
    net_amount, currency = net(item)
    start = case["start_date"]
    room_nights = nights(case, item)
    linked = case["channel"] == "b2b" and item["b2b_hold_id"] and item["b2b_held_as"] == "on_request"
    return {
        "version_item_id": item["id"],
        "supplier_id": item["supplier_id"],
        "hotel_room_type_id": item["hotel_room_type_id"],
        "service_id": item["service_id"],
        "item_description": (item["supply_label"] or item["title"] or "Item")[:500],
        "service_date": start + timedelta(days=int(item["day_from"]) - 1) if start else None,
        "nights": min(room_nights, 60) if room_nights is not None else None,
        "pax": min(int(case["pax"]), 120) if case["pax"] else None,
        "net_amount": net_amount,
        "net_currency": currency,
        "b2b_hold_id": item["b2b_hold_id"] if linked else None,
        "b2b_line_ref": item["b2b_line_ref"] if linked else None,
    }


# AA-1153 (T8): the DMC of the rate that priced an item (a hotel may be sold by several): its first cost row's rate,
# else the B2B quote line's first rate (a room line), else the hotel's / service's supplier. Over ITEMS_SQL's aliases.
RATE_SUPPLIER_SQL = """coalesce(
           (SELECT k.supplier_id FROM booking.version_item_costs c
              JOIN booking.supplier_rates x ON x.id = c.supplier_rate_id
              JOIN booking.supplier_contracts k ON k.id = x.supplier_contract_id
             WHERE c.version_item_id = i.id ORDER BY c.created_at, c.id LIMIT 1),
           (SELECT k.supplier_id FROM booking.b2b_quote_lines ql
              JOIN booking.supplier_rates x ON x.id = ql.supplier_rate_ids[1]
              JOIN booking.supplier_contracts k ON k.id = x.supplier_contract_id
             WHERE ql.quote_id = bh.quote_id AND ql.line_no = i.position AND rt.id IS NOT NULL),
           h.supplier_id, sv.supplier_id)::text"""


def hotel_ref(row: Any, value: str | None) -> dict[str, Any]:
    """AA-1153 (T11): the hotel's own confirmation number a confirm records (a room booking only; trimmed, 100 max)."""
    text = (value or "").strip()
    return {"hotel_confirmation_ref": text[:100]} if text and row["hotel_room_type_id"] else {}


async def check_supply(conn: asyncpg.Connection, supplier_id: str, room: str | None, service: str | None) -> None:
    supplier = await conn.fetchrow("SELECT status, code FROM booking.suppliers WHERE id = $1::uuid", supplier_id)
    if supplier is None:
        raise NotFound("supplier_not_found", f"supplier {supplier_id} not found")
    if supplier["status"] != "active":
        raise Conflict("supplier_inactive", f"supplier {supplier['code']} is inactive")
    if room and not await conn.fetchval(  # AA-1153: a hotel linked (active) to the supplier
        "SELECT 1 FROM booking.hotel_room_types rt JOIN booking.hotel_supplier_links l ON l.hotel_id = rt.hotel_id"
        " WHERE rt.id = $1::uuid AND l.supplier_id = $2::uuid AND l.status = 'active'",
        room,
        supplier_id,
    ):
        raise Invalid("supply_not_of_supplier", "the room type is not one of this supplier's hotels")
    if service and not await conn.fetchval(
        "SELECT 1 FROM booking.services WHERE id = $1::uuid AND supplier_id = $2::uuid", service, supplier_id
    ):
        raise Invalid("supply_not_of_supplier", "the service is not one of this supplier's services")
