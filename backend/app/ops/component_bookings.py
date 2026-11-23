"""Supplier bookings of a sold tour, one per component (AA-1149 T15; bk-065).

A B2B trip case confirmed from a hold whose quote has a ``tp:`` line keeps that line's components as priced in
``booking.b2b_quote_line_parts`` (:mod:`app.b2b.quote_parts`): the sold snapshot. Generate on the Suppliers tab
(:func:`app.ops.supplier_bookings.generate`) calls :func:`generate` here, which makes one pending supplier booking per
part (``quote_part_id``; the database keeps one live booking per part, ``supplier_bookings_part_live_uq``) with the
part's supplier, room type or service, date, pax, quantity, room mix and net. Editing the tour afterwards changes
nothing: the parts are the quote's.

**Fewer travellers after the sale** (an agency partial cancellation, ``booking.b2b_line_reductions``,
:func:`app.b2b.amendments.reduce`): the booking takes the line's current pax (sold adults and children minus those
removed, the ``CURRENT_PAX_SQL`` rule of :mod:`app.b2b.amendments`), never more than the part's pax, and its notes say
how many were removed; the quantity (rooms, vehicles) and the net stay as sold, for staff to check with the supplier
and edit on the booking (the reduction notice reaches only bookings live at the time of the reduction).

**Which parts** (the same set as ``booking.v_trip_case_supply_state``): the parts of the confirmed hold's lines that
are ``confirmed`` (an on-request line once the desk accepted it: a declined line never leaves component bookings
behind) and have no departure (a departure's costs are booked once per departure). Skip reasons shown on the tab:
``booked`` (a live booking), ``waived`` (closed with a reason, :func:`waive`), ``line_not_confirmed`` (the desk has not
accepted the on-request line yet), ``b2b_line_closed`` (declined or cancelled), ``supplier_not_active`` (pick another
supplier with :func:`create_for_part`: the part's room type / service are then dropped, the booking names the new
supplier's room type or service if given, else only the description), ``item_not_active`` (the part's room type,
hotel or service was deactivated since the sale: :func:`create_for_part` drops it the same way, with the same supplier
or another one). A case sold before bk-065 has no parts: its
tour lines are listed with ``sold_before_split`` (add the bookings by hand).

Component bookings are never linked to the hold line (the desk answers an on-request tour line itself), so confirming
or declining one never answers the agency. :func:`create_for_part` books a part by hand (a replacement after a
decline, or another supplier); :func:`waive` closes a part that needs no booking (reason 20..500 characters, audited;
refused while a live booking names it). Every write runs in the caller's transaction, after the case lock.
"""

from __future__ import annotations

import json
from typing import Any

import asyncpg

from app import audit
from app.auth.staff import Staff
from app.errors import Conflict, Invalid

LIVE = ("pending", "requested", "confirmed")
WAIVE_MIN, WAIVE_MAX = 20, 500
OCCUPANCY_LABELS = {"single": "Single", "double": "Double", "twin": "Twin", "triple": "Triple", "family": "Family"}

PARTS_SQL = """
SELECT qp.id::text AS id, qp.line_no, qp.part_no, qp.day_no, qp.service_date,
       qp.hotel_room_type_id::text AS hotel_room_type_id, qp.service_id::text AS service_id,
       qp.supplier_id::text AS supplier_id, su.code AS supplier_code, su.company_name AS supplier_name,
       su.status AS supplier_status, qp.quantity, qp.nights, qp.pax, qp.rooms, qp.net_amount,
       qp.net_currency::text AS net_currency, ql.line_ref, l.status AS line_status,
       CASE WHEN rt.id IS NOT NULL THEN h.name || ' · ' || rt.name ELSE sv.title END AS supply_label,
       sv.service_type,
       CASE WHEN rt.id IS NOT NULL THEN rt.status = 'active' AND h.status = 'active'
            ELSE sv.status = 'active' END AS item_active,
       ql.pax_adult + ql.pax_child - (SELECT coalesce(sum(r.removed_adults + r.removed_children), 0)
                                        FROM booking.b2b_line_reductions r
                                       WHERE r.hold_id = bh.id AND r.line_ref = ql.line_ref) AS line_pax,
       (SELECT b.code FROM booking.supplier_bookings b WHERE b.quote_part_id = qp.id
         AND b.status IN ('pending', 'requested', 'confirmed')) AS live_booking,
       w.reason AS waive_reason
FROM booking.b2b_holds bh
JOIN booking.b2b_hold_lines l ON l.hold_id = bh.id
JOIN booking.b2b_quote_lines ql ON ql.quote_id = bh.quote_id AND ql.line_no = l.line_no AND ql.departure_id IS NULL
JOIN booking.b2b_quote_line_parts qp ON qp.quote_id = ql.quote_id AND qp.line_no = ql.line_no
JOIN booking.suppliers su ON su.id = qp.supplier_id
LEFT JOIN booking.hotel_room_types rt ON rt.id = qp.hotel_room_type_id
LEFT JOIN booking.hotels h ON h.id = rt.hotel_id
LEFT JOIN booking.services sv ON sv.id = qp.service_id
LEFT JOIN booking.trip_case_part_waivers w ON w.quote_part_id = qp.id
WHERE bh.trip_case_id = $1::uuid AND bh.status = 'confirmed'
ORDER BY qp.line_no, qp.part_no
"""
# Tour lines of the confirmed hold with no part: sold before bk-065 (no snapshot); booked by hand.
UNSPLIT_SQL = """
SELECT ql.line_no, ql.line_ref, ql.service_date, l.status AS line_status
FROM booking.b2b_holds bh
JOIN booking.b2b_hold_lines l ON l.hold_id = bh.id
JOIN booking.b2b_quote_lines ql ON ql.quote_id = bh.quote_id AND ql.line_no = l.line_no
WHERE bh.trip_case_id = $1::uuid AND bh.status = 'confirmed' AND ql.component_kind = 'trip_plan'
  AND ql.departure_id IS NULL AND l.status NOT IN ('declined', 'cancelled')
  AND NOT EXISTS (SELECT 1 FROM booking.b2b_quote_line_parts qp WHERE qp.quote_id = ql.quote_id
                   AND qp.line_no = ql.line_no)
ORDER BY ql.line_no
"""
INSERT_SQL = """
INSERT INTO booking.supplier_bookings (trip_case_id, quote_part_id, supplier_id, hotel_room_type_id, service_id,
    item_description, service_date, nights, pax, quantity, net_amount, net_currency, notes, created_by, updated_by)
VALUES ($1::uuid, $2::uuid, $3::uuid, $4::uuid, $5::uuid, $6, $7, $8, $9, $10, $11, $12, $13, $14, $14)
ON CONFLICT (quote_part_id) WHERE status IN ('pending', 'requested', 'confirmed') AND quote_part_id IS NOT NULL
DO NOTHING
RETURNING id::text
"""


def skip_reason(part: Any) -> str | None:
    """Why a sold tour component gets no booking from Generate (None = it gets one)."""
    if part["live_booking"] is not None:
        return "booked"
    if part["waive_reason"] is not None:
        return "waived"
    if part["line_status"] in ("declined", "cancelled"):
        return "b2b_line_closed"
    if part["line_status"] != "confirmed":
        return "line_not_confirmed"
    if part["supplier_status"] != "active":
        return "supplier_not_active"
    if not part["item_active"]:
        return "item_not_active"
    return None


def booked_pax(part: Any) -> tuple[int, int]:
    """``(pax to book, travellers removed after the sale)``: the line's current pax (sold minus agency reductions),
    never more than the part's pax and never below 1 (a fully cancelled line is closed, not booked)."""
    removed = max(int(part["pax"]) - int(part["line_pax"]), 0)
    return max(int(part["pax"]) - removed, 1), removed


def rooms_text(rooms: Any) -> str:
    """``Double ×2 + Single ×1`` from a part's room mix (a JSON string or a list)."""
    mix = json.loads(rooms) if isinstance(rooms, str) else list(rooms or [])
    return " + ".join(
        f"{OCCUPANCY_LABELS.get(r['occupancy'], str(r['occupancy']).capitalize())} ×{r['count']}" for r in mix
    )


def description(part: Any) -> str:
    label = part["supply_label"] or "Tour component"
    if part["hotel_room_type_id"]:
        return f"{label} · {rooms_text(part['rooms'])}, 1 night"[:500]
    return f"{label} · day {part['day_no']} · {booked_pax(part)[0]} pax"[:500]


def _row(part: Any, *, supplier_id: str | None = None) -> dict[str, Any]:
    pax, removed = booked_pax(part)
    note = (
        f"The agency removed {removed} traveller(s) from line {part['line_ref']} after the sale: check the quantity"
        " and the net with the supplier."
        if removed
        else None
    )
    return {
        "quote_part_id": part["id"],
        "supplier_id": supplier_id or part["supplier_id"],
        "hotel_room_type_id": part["hotel_room_type_id"],
        "service_id": part["service_id"],
        "item_description": description(part),
        "service_date": part["service_date"],
        "nights": 1 if part["hotel_room_type_id"] else None,
        "pax": pax,
        "quantity": part["quantity"],
        "net_amount": part["net_amount"],
        "net_currency": part["net_currency"],
        "notes": note,
    }


async def _insert(conn: asyncpg.Connection, staff: Staff, case_id: str, row: dict[str, Any]) -> str | None:
    return await conn.fetchval(
        INSERT_SQL,
        case_id,
        row["quote_part_id"],
        row["supplier_id"],
        row["hotel_room_type_id"],
        row["service_id"],
        row["item_description"],
        row["service_date"],
        row["nights"],
        row["pax"],
        row["quantity"],
        row["net_amount"],
        row["net_currency"],
        row["notes"],
        staff.cis_user_id,
    )


async def parts(conn: asyncpg.Connection, case_id: str) -> list[Any]:
    return list(await conn.fetch(PARTS_SQL, case_id))


async def generate(conn: asyncpg.Connection, staff: Staff, case_id: str) -> list[str]:
    """One pending booking per bookable part of the (locked) case; returns the new booking ids (idempotent)."""
    created: list[str] = []
    for part in await parts(conn, case_id):
        if skip_reason(part) is None:
            new_id = await _insert(conn, staff, case_id, _row(part))
            if new_id is not None:
                created.append(new_id)
    return created


def _unbooked_part(part: Any) -> dict[str, Any]:
    reason = skip_reason(part)
    return {
        "version_item_id": None,
        "quote_part_id": part["id"],
        "position": part["line_no"],
        "tour_day": part["day_no"],
        "title": f"Line {part['line_ref']} · day {part['day_no']}",
        "supply_label": description(part),
        "supplier": {"id": part["supplier_id"], "code": part["supplier_code"], "name": part["supplier_name"]},
        "b2b_line_status": part["line_status"],
        "bookable": reason is None,
        "skip_reason": reason,
        "waive_reason": part["waive_reason"],
    }


async def unbooked(conn: asyncpg.Connection, case: Any) -> list[dict[str, Any]]:
    """The tour components of the case without a live booking (waived ones included, marked), then the tour lines
    sold before the split."""
    if case["channel"] != "b2b":
        return []
    out = [_unbooked_part(p) for p in await parts(conn, case["id"]) if p["live_booking"] is None]
    for line in await conn.fetch(UNSPLIT_SQL, case["id"]):
        out.append(
            {
                "version_item_id": None,
                "quote_part_id": None,
                "position": line["line_no"],
                "tour_day": None,
                "title": f"Line {line['line_ref']}",
                "supply_label": None,
                "supplier": None,
                "b2b_line_status": line["line_status"],
                "bookable": False,
                "skip_reason": "sold_before_split",
                "waive_reason": None,
            }
        )
    return out


async def _part(conn: asyncpg.Connection, case_id: str, part_id: str) -> Any:
    found = next((p for p in await parts(conn, case_id) if p["id"] == str(part_id)), None)
    if found is None:
        raise Invalid("part_not_bookable", "the tour component is not a part of this trip's confirmed booking")
    return found


async def create_for_part(
    conn: asyncpg.Connection, staff: Staff, case: Any, data: dict[str, Any], *, check_supply: Any
) -> str:
    """Book one part by hand (the case is locked by the caller): its supplier or another one (``supplier_id``), the
    part's facts unless the body names others. ``check_supply`` = the supplier / item check of the caller. Returns the
    new booking id.

    The part's room type / service belong to the part's own supplier. With another supplier, or when the item was
    deactivated since the sale (``item_not_active``), they are dropped: the booking names a room type or service when
    the body gives one (checked against the supplier), else only the description (the body's
    ``item_description``, or the part's as sold)."""
    part = await _part(conn, case["id"], data["quote_part_id"])
    reason = skip_reason(part)
    if reason in ("booked", "waived", "b2b_line_closed", "line_not_confirmed"):
        raise Conflict("part_not_bookable", f"this tour component is {reason.replace('_', ' ')}", reason=reason)
    supplier = str(data.get("supplier_id") or part["supplier_id"])
    row = _row(part, supplier_id=supplier)
    if supplier != str(part["supplier_id"]) or reason == "item_not_active":
        row["hotel_room_type_id"] = row["service_id"] = None
    for key in ("hotel_room_type_id", "service_id", "item_description", "service_date", "nights", "pax"):
        if data.get(key) is not None:
            row[key] = data[key]
    row["item_description"] = (row["item_description"] or "").strip()[:500] or description(part)
    for key in ("net_amount", "net_currency"):
        if data.get(key) is not None:
            row[key] = data[key]
    if (row["net_amount"] is None) != (row["net_currency"] is None):
        raise Invalid("net_pair", "net amount and currency go together")
    if row["hotel_room_type_id"] and row["service_id"]:
        raise Invalid("one_supply_record", "a booking books a room type or a service, not both")
    row["notes"] = (data.get("notes") or "").strip() or row["notes"]
    await check_supply(conn, supplier, row["hotel_room_type_id"], row["service_id"])
    new_id = await _insert(conn, staff, case["id"], row)
    if new_id is None:  # a concurrent booking of the same part won
        raise Conflict("booking_exists", "this tour component already has a live supplier booking")
    return new_id


async def waive(conn: asyncpg.Connection, staff: Staff, case: Any, part_id: str, *, reason: str) -> None:
    """Close a part that needs no booking (the case is locked by the caller)."""
    text = (reason or "").strip()
    if not WAIVE_MIN <= len(text) <= WAIVE_MAX:
        raise Invalid("reason_length", f"say why in {WAIVE_MIN} to {WAIVE_MAX} characters")
    part = await _part(conn, case["id"], part_id)
    if part["live_booking"] is not None:
        raise Conflict("booking_exists", f"cancel {part['live_booking']} first")
    if part["waive_reason"] is not None:
        return
    await conn.execute(
        "INSERT INTO booking.trip_case_part_waivers (trip_case_id, quote_part_id, reason, created_by)"
        " VALUES ($1::uuid, $2::uuid, $3, $4)",
        case["id"],
        part["id"],
        text,
        staff.cis_user_id,
    )
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="supplier_booking.part_waived",
        entity_type="trip_case",
        entity_id=case["id"],
        after={"quote_part_id": part["id"], "reason": text},
        entity_label=case["code"],
        summary=f"No booking needed for {description(part)[:120]}: {text[:200]}",
    )
