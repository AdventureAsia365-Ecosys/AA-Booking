"""Supplier bookings and the supply track of a sold trip case (bk-026; AA-682 slice S1, S252 design §3.1, §4, §5).

A **supplier booking** books one supplier for one item of the case's approved version (a room type or a service), or
an extra item staff add by hand. Its life: ``pending`` (generated / added) → ``requested`` (asked the supplier) →
``confirmed`` (with the supplier's confirmation ref, optionally a voucher) or ``declined`` (one of the four desk
codes); ``pending`` / ``requested`` / ``confirmed`` → ``cancelled`` (with a reason); ``confirmed`` → ``requested`` (a
change, "re-request"). Edges live in ``booking.ops_transitions`` (the database refuses any other move, 409
``illegal_transition``). **Live** = pending, requested or confirmed: at most one live booking per version item (a
declined one can be replaced by a booking with another supplier, critic B3).

**Supply state** (``booking.v_trip_case_supply_state``): ``none`` (nothing to book), ``confirming`` or ``booked`` —
the computed **"Confirmed & Booked"** badge (design D1); never a case status.

**Case auto-advance** (design D2, :func:`advance_case`): the first supplier booking requested moves a ``confirmed``
case to ``supplier_confirming``; a ``supplier_confirming`` case with no live booking left goes back to ``confirmed``.
System moves (empty actor, a status note naming the cause), checked again by the bk-026 guard. Staff never pick these
statuses (``app.trip_cases.service.change_status``: 409 ``status_system_managed``).

**B2B tie-in, one action both ways** (design D13, memo seams 4, 8, 10). Generating for a B2B case links each item held
``on_request`` to its hold line. Confirming such a booking accepts the line and declining declines it, through
``app.b2b.desk.answer_line`` in the same transaction (its UPDATE fires the bk-022 trigger: ``line.confirmed`` /
``line.declined`` in ``booking.outbox`` for the agency's ``/events``); both need ``b2b_desk.write`` as well. The desk
refuses to answer a line that has a live supplier booking (409 ``answer_from_suppliers_tab``). A line is answered once,
so a linked booking whose line is confirmed cannot be cancelled or re-requested by staff (409 ``b2b_line_answered``;
only the agency's cancel moves it), and a declined line is final (no replacement). B2B cases take no voucher or
document (DM-6.8, ``app.ops.files``).

**Case cancel cascade** (critic B2, B9, :func:`on_case_cancelled`): when a case is cancelled (staff, or the agency's
``POST /bookings/{id}/cancel``), its pending bookings are cancelled in the same transaction; requested and confirmed
ones stay (cancelling with a supplier is a real-world act with fees) and raise one staff notification "cancel with
supplier". Reopening a case is refused while it has a live booking (bk-026 guard; 409 ``live_supplier_bookings``).

Lock order (no deadlock with the B2B cancel, which locks the hold, then the case): the B2B hold of a linked booking,
then the trip case, then the booking. Every write runs in the caller's transaction and writes one audit row.
Permissions: ``ops.read`` / ``ops.write`` (admin, sales, contracting); net amounts only with ``pricing.read``.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import asyncpg

from app import audit
from app.auth.staff import Staff
from app.b2b import desk
from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.notifications import service as notifications
from app.ops import files as ops_files

log = logging.getLogger(__name__)

STATUSES = ("pending", "requested", "confirmed", "declined", "cancelled")
LIVE = ("pending", "requested", "confirmed")
DECLINE_CODES = desk.DECLINE_CODES
# Cases whose supply may be booked: sold and not finished (pre-sale cases have no approved version to book from).
OPEN_CASE_STATUSES = frozenset(
    {"confirmed", "supplier_confirming", "invoice_sent", "payment_pending", "paid", "pre_departure", "on_trip"}
)
FACT_FIELDS = (
    "supplier_id",
    "hotel_room_type_id",
    "service_id",
    "item_description",
    "service_date",
    "nights",
    "pax",
    "net_amount",
    "net_currency",
)
MAX_PAGE = 100
NOTIFY_CANCEL_TYPE = "supplier_booking.cancel_with_supplier"

BOOKING_COLUMNS = """
    sb.id::text AS id, sb.code, sb.trip_case_id::text AS trip_case_id, tc.code AS trip_case_code,
    tc.channel, tc.status AS trip_case_status, sb.version_item_id::text AS version_item_id, i.position AS item_position,
    sb.supplier_id::text AS supplier_id, s.code AS supplier_code, s.company_name AS supplier_name,
    sb.hotel_room_type_id::text AS hotel_room_type_id, sb.service_id::text AS service_id, sb.item_description,
    sb.service_date, sb.nights, sb.pax, sb.net_amount, sb.net_currency, sb.status,
    sb.requested_at, sb.requested_by, rq.name AS requested_by_name, sb.confirmed_at, sb.confirmed_by,
    cf.name AS confirmed_by_name, sb.confirmation_ref, sb.declined_at, sb.decline_code, sb.cancelled_at,
    sb.cancel_reason, sb.voucher_file_id::text AS voucher_file_id, sb.b2b_hold_id::text AS b2b_hold_id,
    sb.b2b_line_ref, l.status AS b2b_line_status, sb.notes, sb.created_by, sb.created_at, sb.updated_at
"""
BOOKING_FROM = """
FROM booking.supplier_bookings sb
JOIN booking.trip_cases tc ON tc.id = sb.trip_case_id
JOIN booking.suppliers s ON s.id = sb.supplier_id
LEFT JOIN booking.trip_case_version_itinerary i ON i.id = sb.version_item_id
LEFT JOIN booking.b2b_hold_lines l ON l.hold_id = sb.b2b_hold_id AND l.line_ref = sb.b2b_line_ref
LEFT JOIN booking.staff_users rq ON rq.cis_user_id = sb.requested_by
LEFT JOIN booking.staff_users cf ON cf.cis_user_id = sb.confirmed_by
"""
SUPPLY_SQL = """
SELECT items_to_book, items_booked, live, confirmed, waiting, declined, state
FROM booking.v_trip_case_supply_state WHERE trip_case_id = $1::uuid
"""
# Supply items of the approved version: what a booking would copy, with the supplier, the cost rows and, for a B2B
# case, the hold line of the same position.
ITEMS_SQL = """
SELECT i.id::text AS id, i.position, i.title, i.day_from, i.day_to,
       i.hotel_room_type_id::text AS hotel_room_type_id, i.service_id::text AS service_id,
       coalesce(h.supplier_id, sv.supplier_id)::text AS supplier_id,
       CASE WHEN rt.id IS NOT NULL THEN h.name || ' · ' || rt.name ELSE sv.title END AS supply_label,
       (SELECT array_agg(c.net_amount ORDER BY c.created_at, c.id) FROM booking.version_item_costs c
         WHERE c.version_item_id = i.id) AS net_amounts,
       (SELECT array_agg(DISTINCT c.net_currency) FROM booking.version_item_costs c
         WHERE c.version_item_id = i.id) AS net_currencies,
       hl.hold_id::text AS b2b_hold_id, hl.line_ref AS b2b_line_ref, hl.held_as AS b2b_held_as,
       hl.status AS b2b_line_status,
       (SELECT b.code FROM booking.supplier_bookings b WHERE b.version_item_id = i.id
         AND b.status IN ('pending', 'requested', 'confirmed')) AS live_booking,
       EXISTS (SELECT 1 FROM booking.supplier_bookings b WHERE b.version_item_id = i.id
         AND b.status = 'declined' AND b.b2b_hold_id IS NOT NULL) AS b2b_declined
FROM booking.trip_case_versions v
JOIN booking.trip_case_version_itinerary i ON i.trip_case_version_id = v.id
LEFT JOIN booking.hotel_room_types rt ON rt.id = i.hotel_room_type_id
LEFT JOIN booking.hotels h ON h.id = rt.hotel_id
LEFT JOIN booking.services sv ON sv.id = i.service_id
LEFT JOIN booking.b2b_holds bh ON bh.trip_case_id = v.trip_case_id
LEFT JOIN booking.b2b_hold_lines hl ON hl.hold_id = bh.id AND hl.line_no = i.position
WHERE v.trip_case_id = $1::uuid AND v.is_approved AND v.deleted_at IS NULL
  AND (i.hotel_room_type_id IS NOT NULL OR i.service_id IS NOT NULL)
ORDER BY i.position
"""


# ── Helpers ──────────────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(UTC)


def _uuid(value: Any) -> str | None:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        return None


def _case_filter(ref: str) -> tuple[str, str]:
    ident = _uuid(ref)
    if ident:
        return "tc.id = $1::uuid", ident
    return "tc.code = $1", ref.strip().upper()


async def lock_case(conn: asyncpg.Connection, ref: str) -> Any:
    """The trip case row, locked (every supplier-booking write of a case runs one after the other)."""
    where, arg = _case_filter(ref)
    row = await conn.fetchrow(
        "SELECT tc.id::text AS id, tc.code, tc.status, tc.channel, tc.start_date, tc.pax FROM booking.trip_cases tc"  # noqa: S608
        f" WHERE {where} AND tc.deleted_at IS NULL FOR UPDATE",
        arg,
    )
    if row is None:
        raise NotFound("trip_case_not_found", f"trip case {ref} not found")
    return row


async def _read_case(conn: asyncpg.Connection, ref: str) -> Any:
    where, arg = _case_filter(ref)
    row = await conn.fetchrow(
        f"SELECT tc.id::text AS id, tc.code, tc.status, tc.channel FROM booking.trip_cases tc WHERE {where}"  # noqa: S608
        " AND tc.deleted_at IS NULL",
        arg,
    )
    if row is None:
        raise NotFound("trip_case_not_found", f"trip case {ref} not found")
    return row


def booking_out(row: Any, staff: Staff, files: list[dict] | None = None) -> dict[str, Any]:
    out = dict(row)
    if not staff.can("pricing.read"):
        out["net_amount"] = None
        out["net_currency"] = None
    out["trip_case"] = {
        "id": out.pop("trip_case_id"),
        "code": out.pop("trip_case_code"),
        "status": out.pop("trip_case_status"),
    }
    out["supplier"] = {"id": out.pop("supplier_id"), "code": out.pop("supplier_code"), "name": out.pop("supplier_name")}
    out["requested_by"] = (
        {"id": out["requested_by"], "name": out.pop("requested_by_name")}
        if out["requested_by"]
        else out.pop("requested_by_name")
    )
    out["confirmed_by"] = (
        {"id": out["confirmed_by"], "name": out.pop("confirmed_by_name")}
        if out["confirmed_by"]
        else out.pop("confirmed_by_name")
    )
    hold, line_ref, line_status = out.pop("b2b_hold_id"), out.pop("b2b_line_ref"), out.pop("b2b_line_status")
    out["b2b_line"] = {"hold_id": hold, "line_ref": line_ref, "status": line_status} if hold else None
    out["live"] = out["status"] in LIVE
    out["files"] = files or []
    return out


async def _get(conn: asyncpg.Connection, booking_id: str, *, lock: bool = False) -> Any:
    ident = _uuid(booking_id)
    if ident is None:
        raise NotFound("supplier_booking_not_found", f"supplier booking {booking_id} not found")
    row = await conn.fetchrow(
        f"SELECT {BOOKING_COLUMNS} {BOOKING_FROM} WHERE sb.id = $1::uuid"  # noqa: S608 — constants only
        + (" FOR UPDATE OF sb" if lock else ""),
        ident,
    )
    if row is None:
        raise NotFound("supplier_booking_not_found", f"supplier booking {booking_id} not found")
    return row


async def _one_out(conn: asyncpg.Connection, staff: Staff, booking_id: str) -> dict[str, Any]:
    row = await _get(conn, booking_id)
    attached = await ops_files.files_of(conn, "supplier_booking", [row["id"]])
    return booking_out(row, staff, attached.get(row["id"]))


async def supply_state(conn: asyncpg.Connection, case_id: str) -> dict[str, Any]:
    row = await conn.fetchrow(SUPPLY_SQL, case_id)
    if row is None:
        return {
            "state": "none",
            "items_to_book": 0,
            "items_booked": 0,
            "live": 0,
            "confirmed": 0,
            "waiting": 0,
            "declined": 0,
        }
    return dict(row)


def _label(case_code: str, code: str) -> str:
    return f"{case_code} {code}"


async def _audit(
    conn: asyncpg.Connection,
    staff: Staff,
    action: str,
    row: Any,
    summary: str,
    *,
    before: Any = None,
    after: Any = None,
) -> None:
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action=action,
        entity_type="supplier_booking",
        entity_id=row["id"],
        before=before,
        after=after,
        entity_label=_label(row["trip_case_code"], row["code"]),
        summary=summary,
    )


def _ensure_open(case: Any) -> None:
    if case["status"] not in OPEN_CASE_STATUSES:
        raise Conflict(
            "case_not_bookable",
            f"trip case {case['code']} is {case['status']}: supplier bookings are made for a confirmed trip only",
        )


# ── Case auto-advance (design D2) ────────────────────────────────────────────


async def advance_case(conn: asyncpg.Connection, case: Any, *, cause: str) -> str | None:
    """Move a locked case along the supply track when the facts say so; returns the new status or None.

    confirmed → supplier_confirming once a booking is requested or confirmed; supplier_confirming → confirmed when no
    live booking is left. A system move: empty ``booking.actor``, the cause in the status note and the audit row."""
    counts = await conn.fetchrow(
        "SELECT count(*) FILTER (WHERE status IN ('pending', 'requested', 'confirmed')) AS live,"
        " count(*) FILTER (WHERE status IN ('requested', 'confirmed')) AS asked"
        " FROM booking.supplier_bookings WHERE trip_case_id = $1::uuid",
        case["id"],
    )
    status = await conn.fetchval("SELECT status FROM booking.trip_cases WHERE id = $1::uuid", case["id"])
    target = None
    if status == "confirmed" and counts["asked"] > 0:
        target = "supplier_confirming"
    elif status == "supplier_confirming" and counts["live"] == 0:
        target = "confirmed"
    if target is None:
        return None
    note = f"Auto: {cause}"[:500]
    await conn.execute(
        "SELECT set_config('booking.actor', '', true), set_config('booking.status_note', $1, true)", note
    )
    await conn.execute("UPDATE booking.trip_cases SET status = $2 WHERE id = $1::uuid", case["id"], target)
    await conn.execute("SELECT set_config('booking.status_note', '', true)")
    await audit.record(
        conn,
        actor_type="system",
        actor_ref="aa682:advance_case",
        action="trip_case.status_changed",
        entity_type="trip_case",
        entity_id=case["id"],
        before={"status": status},
        after={"status": target, "cause": cause[:200]},
        entity_label=case["code"],
        summary=f"{status} -> {target} (automatic: {cause})"[:500],
    )
    log.info("trip_case_auto_advanced case=%s %s->%s", case["code"], status, target)
    return target


# ── Reads ────────────────────────────────────────────────────────────────────


async def list_for_case(conn: asyncpg.Connection, staff: Staff, ref: str) -> dict[str, Any]:
    """The case's supplier bookings (live first), its supply state and the supply items still without a live booking."""
    case = await _read_case(conn, ref)
    rows = await conn.fetch(
        f"SELECT {BOOKING_COLUMNS} {BOOKING_FROM} WHERE sb.trip_case_id = $1::uuid"  # noqa: S608 — constants only
        " ORDER BY (sb.status IN ('pending', 'requested', 'confirmed')) DESC, i.position NULLS LAST, sb.created_at",
        case["id"],
    )
    attached = await ops_files.files_of(conn, "supplier_booking", [r["id"] for r in rows])
    items = await conn.fetch(ITEMS_SQL, case["id"])
    to_book = [
        {
            "version_item_id": i["id"],
            "position": i["position"],
            "title": i["title"],
            "supply_label": i["supply_label"],
            "b2b_line_status": i["b2b_line_status"],
            "bookable": _item_skip_reason(case, i) is None,
            "skip_reason": _item_skip_reason(case, i),
        }
        for i in items
        if i["live_booking"] is None
    ]
    return {
        "trip_case": {"id": case["id"], "code": case["code"], "status": case["status"], "channel": case["channel"]},
        "supply": await supply_state(conn, case["id"]),
        "items": [booking_out(r, staff, attached.get(r["id"])) for r in rows],
        "unbooked_items": to_book,
        "documents_allowed": case["channel"] != "b2b",
    }


async def get_booking(conn: asyncpg.Connection, staff: Staff, booking_id: str) -> dict[str, Any]:
    return await _one_out(conn, staff, booking_id)


async def list_bookings(
    conn: asyncpg.Connection,
    staff: Staff,
    *,
    supplier_id: str | None = None,
    status: str | None = None,
    start: date | None = None,
    end: date | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Bookings across cases: the supplier Bookings tab (``supplier_id``) and the ops queue (``status``)."""
    if status is not None and status not in (*STATUSES, "live"):
        raise Invalid("unknown_status", f"status must be one of {', '.join((*STATUSES, 'live'))}")
    if not 1 <= limit <= MAX_PAGE or offset < 0:
        raise Invalid("invalid_page", f"limit is 1..{MAX_PAGE} and offset >= 0")
    supplier = None
    if supplier_id:
        supplier = await conn.fetchval(
            "SELECT id::text FROM booking.suppliers WHERE id::text = $1 OR code = upper($1)", supplier_id.strip()
        )
        if supplier is None:
            raise NotFound("supplier_not_found", f"supplier {supplier_id} not found")
    where = (
        "($1::uuid IS NULL OR sb.supplier_id = $1::uuid)"
        " AND ($2::text IS NULL OR sb.status = $2"
        " OR ($2 = 'live' AND sb.status IN ('pending', 'requested', 'confirmed')))"
        " AND ($3::date IS NULL OR sb.service_date >= $3) AND ($4::date IS NULL OR sb.service_date <= $4)"
    )
    total = await conn.fetchval(
        f"SELECT count(*) {BOOKING_FROM} WHERE {where}",
        supplier,
        status,
        start,
        end,  # noqa: S608
    )
    rows = await conn.fetch(
        f"SELECT {BOOKING_COLUMNS} {BOOKING_FROM} WHERE {where}"  # noqa: S608 — constants only
        " ORDER BY sb.service_date NULLS LAST, sb.created_at, sb.id LIMIT $5 OFFSET $6",
        supplier,
        status,
        start,
        end,
        limit,
        offset,
    )
    return {"items": [booking_out(r, staff) for r in rows], "total": int(total or 0), "limit": limit, "offset": offset}


async def summary(conn: asyncpg.Connection) -> dict[str, Any]:
    """Dashboard tile "Supplier answers pending": requested bookings, those waiting longer than
    ``supplier_answer_days``, and pending ones not yet requested."""
    days = await conn.fetchval("SELECT value FROM booking.settings WHERE key = 'supplier_answer_days'")
    try:
        days = int(days) if days is not None else 2
    except (TypeError, ValueError):
        days = 2
    row = await conn.fetchrow(
        "SELECT count(*) FILTER (WHERE sb.status = 'requested')::int AS requested,"
        " count(*) FILTER (WHERE sb.status = 'requested' AND sb.requested_at < now() - make_interval(days => $1))::int"
        " AS overdue, count(*) FILTER (WHERE sb.status = 'pending')::int AS pending"
        " FROM booking.supplier_bookings sb JOIN booking.trip_cases tc ON tc.id = sb.trip_case_id"
        " WHERE tc.status NOT IN ('cancelled', 'completed') AND tc.deleted_at IS NULL",
        max(1, min(days, 60)),
    )
    return {**dict(row), "answer_days": days}


# ── Generate and add ─────────────────────────────────────────────────────────


def _item_skip_reason(case: Any, item: Any) -> str | None:
    """Why an approved supply item gets no booking from Generate (None = it gets one)."""
    if item["live_booking"] is not None:
        return "booked"
    if item["supplier_id"] is None:
        return "no_supplier"
    if case["channel"] == "b2b":
        if item["b2b_line_status"] in ("declined", "cancelled") or item["b2b_declined"]:
            return "b2b_line_closed"
    return None


def _net(item: Any) -> tuple[Decimal | None, str | None]:
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


def _from_item(case: Any, item: Any) -> dict[str, Any]:
    net, currency = _net(item)
    start = case["start_date"]
    nights = (int(item["day_to"]) - int(item["day_from"]) + 1) if item["hotel_room_type_id"] else None
    linked = case["channel"] == "b2b" and item["b2b_hold_id"] and item["b2b_held_as"] == "on_request"
    return {
        "version_item_id": item["id"],
        "supplier_id": item["supplier_id"],
        "hotel_room_type_id": item["hotel_room_type_id"],
        "service_id": item["service_id"],
        "item_description": (item["supply_label"] or item["title"] or "Item")[:500],
        "service_date": start + timedelta(days=int(item["day_from"]) - 1) if start else None,
        "nights": min(nights, 60) if nights is not None else None,
        "pax": min(int(case["pax"]), 120) if case["pax"] else None,
        "net_amount": net,
        "net_currency": currency,
        "b2b_hold_id": item["b2b_hold_id"] if linked else None,
        "b2b_line_ref": item["b2b_line_ref"] if linked else None,
    }


INSERT_SQL = """
INSERT INTO booking.supplier_bookings (trip_case_id, version_item_id, supplier_id, hotel_room_type_id, service_id,
    item_description, service_date, nights, pax, net_amount, net_currency, b2b_hold_id, b2b_line_ref, notes,
    created_by, updated_by)
VALUES ($1::uuid, $2::uuid, $3::uuid, $4::uuid, $5::uuid, $6, $7, $8, $9, $10, $11, $12::uuid, $13, $14, $15, $15)
ON CONFLICT (version_item_id) WHERE status IN ('pending', 'requested', 'confirmed') AND version_item_id IS NOT NULL
DO NOTHING
RETURNING id::text
"""


async def _insert(conn: asyncpg.Connection, staff: Staff, case_id: str, data: dict[str, Any]) -> str | None:
    return await conn.fetchval(
        INSERT_SQL,
        case_id,
        data.get("version_item_id"),
        data["supplier_id"],
        data.get("hotel_room_type_id"),
        data.get("service_id"),
        data["item_description"],
        data.get("service_date"),
        data.get("nights"),
        data.get("pax"),
        data.get("net_amount"),
        data.get("net_currency"),
        data.get("b2b_hold_id"),
        data.get("b2b_line_ref"),
        data.get("notes"),
        staff.cis_user_id,
    )


async def generate(conn: asyncpg.Connection, staff: Staff, ref: str) -> dict[str, Any]:
    """One pending booking per supply item of the approved version that has no live booking (idempotent: a second
    call creates nothing; two concurrent calls are serialised by the case lock and the live-per-item index)."""
    case = await lock_case(conn, ref)
    _ensure_open(case)
    if not await conn.fetchval(
        "SELECT 1 FROM booking.trip_case_versions WHERE trip_case_id = $1::uuid AND is_approved AND deleted_at IS NULL",
        case["id"],
    ):
        raise Conflict("no_approved_version", f"trip case {case['code']} has no approved version")
    items = await conn.fetch(ITEMS_SQL, case["id"])
    planned = [_from_item(case, i) for i in items if _item_skip_reason(case, i) is None]
    created: list[str] = []
    for data in planned:
        new_id = await _insert(conn, staff, case["id"], data)
        if new_id is not None:
            created.append(new_id)
    if created:
        await audit.record(
            conn,
            actor_type="staff",
            actor_ref=staff.cis_user_id,
            action="supplier_booking.generated",
            entity_type="trip_case",
            entity_id=case["id"],
            after={"created": len(created), "supplier_booking_ids": created},
            entity_label=case["code"],
            summary=f"Generated {len(created)} supplier booking(s) from the approved version",
        )
    out = await list_for_case(conn, staff, case["id"])
    return {**out, "created": len(created)}


async def _check_supply(conn: asyncpg.Connection, supplier_id: str, room: str | None, service: str | None) -> None:
    supplier = await conn.fetchrow("SELECT status, code FROM booking.suppliers WHERE id = $1::uuid", supplier_id)
    if supplier is None:
        raise NotFound("supplier_not_found", f"supplier {supplier_id} not found")
    if supplier["status"] != "active":
        raise Conflict("supplier_inactive", f"supplier {supplier['code']} is inactive")
    if room and not await conn.fetchval(
        "SELECT 1 FROM booking.hotel_room_types rt JOIN booking.hotels h ON h.id = rt.hotel_id"
        " WHERE rt.id = $1::uuid AND h.supplier_id = $2::uuid",
        room,
        supplier_id,
    ):
        raise Invalid("supply_not_of_supplier", "the room type is not one of this supplier's hotels")
    if service and not await conn.fetchval(
        "SELECT 1 FROM booking.services WHERE id = $1::uuid AND supplier_id = $2::uuid", service, supplier_id
    ):
        raise Invalid("supply_not_of_supplier", "the service is not one of this supplier's services")


async def create(conn: asyncpg.Connection, staff: Staff, ref: str, data: dict[str, Any]) -> dict[str, Any]:
    """Add a booking by hand: an extra item (no ``version_item_id``) or the replacement of a declined one."""
    case = await lock_case(conn, ref)
    _ensure_open(case)
    supplier = _uuid(data.get("supplier_id"))
    if supplier is None:
        raise Invalid("supplier_required", "choose the supplier")
    room, service = data.get("hotel_room_type_id"), data.get("service_id")
    if room and service:
        raise Invalid("one_supply_record", "a booking books a room type or a service, not both")
    await _check_supply(conn, supplier, room, service)
    row: dict[str, Any] = {k: data.get(k) for k in FACT_FIELDS}
    row["supplier_id"] = supplier
    row["notes"] = (data.get("notes") or "").strip() or None
    if (row["net_amount"] is None) != (row["net_currency"] is None):
        raise Invalid("net_pair", "net amount and currency go together")
    item_id = data.get("version_item_id")
    if item_id:
        item = next((i for i in await conn.fetch(ITEMS_SQL, case["id"]) if i["id"] == str(item_id)), None)
        if item is None:
            raise Invalid("item_not_bookable", "the item is not a room or service of the approved version")
        if item["live_booking"] is not None:
            raise Conflict("booking_exists", f"item {item['position']} already has {item['live_booking']}")
        if case["channel"] == "b2b" and (item["b2b_line_status"] in ("declined", "cancelled") or item["b2b_declined"]):
            raise Conflict("b2b_line_answered", "the agency was told this line is declined or cancelled; it is final")
        base = _from_item(case, item) if item["supplier_id"] else {}
        row = {**base, **{k: v for k, v in row.items() if v is not None}, "supplier_id": supplier}
        row["version_item_id"] = item["id"]
        if case["channel"] == "b2b" and item["b2b_held_as"] == "on_request":
            row["b2b_hold_id"], row["b2b_line_ref"] = item["b2b_hold_id"], item["b2b_line_ref"]
    description = (row.get("item_description") or "").strip()
    if not description:
        raise Invalid("description_required", "say what is booked")
    row["item_description"] = description[:500]
    try:
        new_id = await _insert(conn, staff, case["id"], row)
    except asyncpg.CheckViolationError as exc:
        raise Conflict("invalid_supplier_booking", str(exc).split("\n")[0]) from exc
    if new_id is None:  # a concurrent booking of the same item won
        raise Conflict("booking_exists", "this item already has a live supplier booking")
    created = await _get(conn, new_id)
    await _audit(
        conn,
        staff,
        "supplier_booking.created",
        created,
        f"Added supplier booking {created['code']} ({created['supplier_name']})",
        after={"supplier_id": supplier, "version_item_id": row.get("version_item_id"), "status": "pending"},
    )
    return await _one_out(conn, staff, new_id)


# ── Transitions ──────────────────────────────────────────────────────────────


async def _lock_for_write(conn: asyncpg.Connection, booking_id: str) -> tuple[Any, Any]:
    """Lock order: the B2B hold (if linked), the case, the booking. Returns (case, booking)."""
    first = await _get(conn, booking_id)
    if first["b2b_hold_id"]:
        await conn.execute("SELECT 1 FROM booking.b2b_holds WHERE id = $1::uuid FOR UPDATE", first["b2b_hold_id"])
    case = await lock_case(conn, first["trip_case_id"])
    row = await _get(conn, first["id"], lock=True)
    return case, row


async def _set_status(conn: asyncpg.Connection, staff: Staff, row: Any, target: str, extra: dict[str, Any]) -> None:
    sets = ["status = $2", "updated_by = $3"] + [f"{col} = ${i + 4}" for i, col in enumerate(extra)]
    try:
        await conn.execute(
            f"UPDATE booking.supplier_bookings SET {', '.join(sets)} WHERE id = $1::uuid",  # noqa: S608 — fixed names
            row["id"],
            target,
            staff.cis_user_id,
            *extra.values(),
        )
    except asyncpg.CheckViolationError as exc:  # the edge table / guard is the last word
        raise Conflict("illegal_transition", str(exc).split("\n")[0], status=row["status"]) from exc


def _need(staff: Staff, permission: str) -> None:
    if not staff.can(permission):
        raise Forbidden("forbidden", f"this action needs {permission}")


def _b2b_linked(row: Any) -> bool:
    return bool(row["b2b_hold_id"])


async def update(conn: asyncpg.Connection, staff: Staff, booking_id: str, changes: dict[str, Any]) -> dict[str, Any]:
    """Edit the facts while pending / requested (supplier, room type or service, description, date, nights, pax, net),
    and the notes at any time."""
    case, row = await _lock_for_write(conn, booking_id)
    facts = {k: v for k, v in changes.items() if k in FACT_FIELDS}
    if "notes" in changes:
        facts["notes"] = (changes["notes"] or "").strip() or None
    changed = {
        k: v
        for k, v in facts.items()
        if (str(row[k]) if row[k] is not None else None) != (str(v) if v is not None else None)
    }
    if any(k in FACT_FIELDS for k in changed) and row["status"] not in ("pending", "requested"):
        raise Conflict("supplier_booking_locked", f"{row['code']} is {row['status']}: only its notes change")
    if "item_description" in changed and not (changed["item_description"] or "").strip():
        raise Invalid("description_required", "say what is booked")
    if not changed:
        return {**await _one_out(conn, staff, row["id"]), "changed": []}
    supplier = str(changed.get("supplier_id", row["supplier_id"]))
    room = changed.get("hotel_room_type_id", row["hotel_room_type_id"])
    service = changed.get("service_id", row["service_id"])
    if room and service:
        raise Invalid("one_supply_record", "a booking books a room type or a service, not both")
    if {"supplier_id", "hotel_room_type_id", "service_id"} & set(changed):
        await _check_supply(conn, supplier, str(room) if room else None, str(service) if service else None)
    net = changed.get("net_amount", row["net_amount"])
    currency = changed.get("net_currency", row["net_currency"])
    if (net is None) != (currency is None):
        raise Invalid("net_pair", "net amount and currency go together")
    casts = {"supplier_id": "::uuid", "hotel_room_type_id": "::uuid", "service_id": "::uuid"}
    sets = ", ".join(f"{col} = ${i + 3}{casts.get(col, '')}" for i, col in enumerate(changed))
    try:
        await conn.execute(
            f"UPDATE booking.supplier_bookings SET {sets}, updated_by = $2 WHERE id = $1::uuid",  # noqa: S608
            row["id"],
            staff.cis_user_id,
            *[str(v) if k in casts and v is not None else v for k, v in changed.items()],
        )
    except asyncpg.CheckViolationError as exc:
        raise Conflict("invalid_supplier_booking", str(exc).split("\n")[0]) from exc
    await _audit(
        conn,
        staff,
        "supplier_booking.updated",
        row,
        f"Edited {row['code']}: {', '.join(sorted(changed))}",
        before={k: row[k] for k in changed},
        after=changed,
    )
    return {**await _one_out(conn, staff, row["id"]), "changed": sorted(changed)}


async def request(conn: asyncpg.Connection, staff: Staff, booking_id: str) -> dict[str, Any]:
    """pending → requested (staff asked the supplier). The first request moves the case to supplier_confirming."""
    case, row = await _lock_for_write(conn, booking_id)
    if row["status"] == "requested":
        return await _one_out(conn, staff, row["id"])  # the same request again
    _ensure_open(case)
    await _set_status(conn, staff, row, "requested", {"requested_at": _now(), "requested_by": staff.cis_user_id})
    await _audit(
        conn,
        staff,
        "supplier_booking.requested",
        row,
        f"Requested {row['code']} from {row['supplier_name']}",
        before={"status": row["status"]},
        after={"status": "requested"},
    )
    await advance_case(conn, case, cause=f"supplier booking {row['code']} requested")
    return await _one_out(conn, staff, row["id"])


async def confirm(
    conn: asyncpg.Connection,
    staff: Staff,
    booking_id: str,
    *,
    confirmation_ref: str,
    voucher_file_id: str | None = None,
) -> dict[str, Any]:
    """requested → confirmed with the supplier's ref (and a voucher of this booking, B2C only). A booking of an
    on-request B2B line accepts the line in the same transaction (``line.confirmed``)."""
    ref = (confirmation_ref or "").strip()
    if not ref or len(ref) > 100:
        raise Invalid("confirmation_ref_required", "enter the supplier's confirmation ref (1-100 characters)")
    case, row = await _lock_for_write(conn, booking_id)
    if (
        row["status"] == "confirmed"
        and row["confirmation_ref"] == ref
        and voucher_file_id in (None, row["voucher_file_id"])
    ):
        return await _one_out(conn, staff, row["id"])  # the same answer again
    if _b2b_linked(row):
        _need(staff, "b2b_desk.write")
    if voucher_file_id is not None:
        if case["channel"] == "b2b":
            raise Invalid("document_not_allowed_b2b", "a B2B trip keeps the confirmation ref only, no voucher")
        if _uuid(voucher_file_id) is None:
            raise Invalid("invalid_voucher", "voucher_file_id is not a file id")
    if row["status"] != "requested":
        raise Conflict(
            "illegal_transition",
            f"{row['code']} is {row['status']}; only a requested booking is confirmed",
            status=row["status"],
        )
    extra: dict[str, Any] = {"confirmation_ref": ref, "confirmed_at": _now(), "confirmed_by": staff.cis_user_id}
    if voucher_file_id is not None:
        extra["voucher_file_id"] = voucher_file_id
    await _set_status(conn, staff, row, "confirmed", extra)
    if _b2b_linked(row):
        await desk.answer_line(
            conn, staff, row["b2b_hold_id"], row["b2b_line_ref"], accept=True, via_supplier_booking=True
        )
    await _audit(
        conn,
        staff,
        "supplier_booking.confirmed",
        row,
        f"{row['supplier_name']} confirmed {row['code']} (ref {ref})"
        + (" · B2B line accepted" if _b2b_linked(row) else ""),
        before={"status": row["status"]},
        after={"status": "confirmed", "confirmation_ref": ref, "voucher_file_id": voucher_file_id},
    )
    await advance_case(conn, case, cause=f"supplier booking {row['code']} confirmed")
    return await _one_out(conn, staff, row["id"])


async def decline(conn: asyncpg.Connection, staff: Staff, booking_id: str, *, decline_code: str) -> dict[str, Any]:
    """requested → declined with a desk code. A booking of an on-request B2B line declines the line in the same
    transaction (``line.declined``; final: no replacement on a B2B case)."""
    if decline_code not in DECLINE_CODES:
        raise Invalid("invalid_decline_code", f"decline_code must be one of {', '.join(DECLINE_CODES)}")
    case, row = await _lock_for_write(conn, booking_id)
    if row["status"] == "declined" and row["decline_code"] == decline_code:
        return await _one_out(conn, staff, row["id"])
    if _b2b_linked(row):
        _need(staff, "b2b_desk.write")
    if row["status"] != "requested":
        raise Conflict(
            "illegal_transition",
            f"{row['code']} is {row['status']}; only a requested booking is declined",
            status=row["status"],
        )
    await _set_status(conn, staff, row, "declined", {"decline_code": decline_code, "declined_at": _now()})
    if _b2b_linked(row):
        await desk.answer_line(
            conn,
            staff,
            row["b2b_hold_id"],
            row["b2b_line_ref"],
            accept=False,
            decline_code=decline_code,
            via_supplier_booking=True,
        )
    await _audit(
        conn,
        staff,
        "supplier_booking.declined",
        row,
        f"{row['supplier_name']} declined {row['code']} ({decline_code})"
        + (" · B2B line declined" if _b2b_linked(row) else ""),
        before={"status": row["status"]},
        after={"status": "declined", "decline_code": decline_code},
    )
    await advance_case(conn, case, cause=f"supplier booking {row['code']} declined")
    return await _one_out(conn, staff, row["id"])


async def cancel(conn: asyncpg.Connection, staff: Staff, booking_id: str, *, reason: str) -> dict[str, Any]:
    """pending / requested / confirmed → cancelled with a reason. Refused on a B2B line the agency was told is
    confirmed (only the agency's cancel moves it)."""
    reason = (reason or "").strip()
    if not reason:
        raise Invalid("reason_required", "say why the booking is cancelled")
    case, row = await _lock_for_write(conn, booking_id)
    if row["status"] == "cancelled":
        return await _one_out(conn, staff, row["id"])
    if _b2b_linked(row) and row["b2b_line_status"] == "confirmed":
        raise Conflict("b2b_line_answered", "the agency was told this line is confirmed; only its cancel moves it")
    if row["status"] not in LIVE:
        raise Conflict("illegal_transition", f"{row['code']} is {row['status']}", status=row["status"])
    await _set_status(conn, staff, row, "cancelled", {"cancel_reason": reason[:500], "cancelled_at": _now()})
    await _audit(
        conn,
        staff,
        "supplier_booking.cancelled",
        row,
        f"Cancelled {row['code']}: {reason[:200]}",
        before={"status": row["status"]},
        after={"status": "cancelled", "reason": reason[:500]},
    )
    await advance_case(conn, case, cause=f"supplier booking {row['code']} cancelled")
    return await _one_out(conn, staff, row["id"])


async def rerequest(conn: asyncpg.Connection, staff: Staff, booking_id: str, *, reason: str) -> dict[str, Any]:
    """confirmed → requested (a change with the supplier); the old confirmation ref stays in the audit row. Never for
    a B2B line (answered once)."""
    reason = (reason or "").strip()
    if not reason:
        raise Invalid("reason_required", "say what changes")
    case, row = await _lock_for_write(conn, booking_id)
    if _b2b_linked(row):
        raise Conflict("b2b_line_answered", "a B2B line is answered once; it cannot be re-requested")
    _ensure_open(case)
    if row["status"] != "confirmed":
        raise Conflict(
            "illegal_transition",
            f"{row['code']} is {row['status']}; only a confirmed booking is re-requested",
            status=row["status"],
        )
    await _set_status(
        conn,
        staff,
        row,
        "requested",
        {
            "requested_at": _now(),
            "requested_by": staff.cis_user_id,
            "confirmation_ref": None,
            "confirmed_at": None,
            "confirmed_by": None,
        },
    )
    await _audit(
        conn,
        staff,
        "supplier_booking.rerequested",
        row,
        f"Re-requested {row['code']}: {reason[:200]}",
        before={"status": "confirmed", "confirmation_ref": row["confirmation_ref"]},
        after={"status": "requested", "reason": reason[:500]},
    )
    return await _one_out(conn, staff, row["id"])


# ── Case cancel cascade (critic B2, B9) ──────────────────────────────────────


async def on_case_cancelled(conn: asyncpg.Connection, *, case_id: str, case_code: str, cause: str) -> dict[str, int]:
    """Called in the transaction that cancels a case (case row locked by the caller): pending bookings are cancelled,
    requested / confirmed ones stay and raise one staff notification to cancel them with the supplier."""
    cancelled = await conn.fetch(
        "UPDATE booking.supplier_bookings SET status = 'cancelled', cancel_reason = $2, cancelled_at = now()"
        " WHERE trip_case_id = $1::uuid AND status = 'pending' RETURNING code",
        case_id,
        f"Trip case cancelled: {cause}"[:500],
    )
    open_rows = await conn.fetch(
        "SELECT code, status FROM booking.supplier_bookings WHERE trip_case_id = $1::uuid"
        " AND status IN ('requested', 'confirmed') ORDER BY code",
        case_id,
    )
    if open_rows:
        codes = [r["code"] for r in open_rows]
        await notifications.notify_staff(
            conn,
            type=NOTIFY_CANCEL_TYPE,
            title=f"{case_code} cancelled: cancel {len(codes)} supplier booking(s) with the supplier",
            message=f"Requested or confirmed: {', '.join(codes)}. Cancel each with its supplier, then mark it"
            " cancelled.",
            entity_type="trip_case",
            entity_id=case_id,
            data={"code": case_code, "supplier_bookings": codes},
        )
    if cancelled or open_rows:
        await audit.record(
            conn,
            actor_type="system",
            actor_ref="aa682:case_cancel_cascade",
            action="supplier_booking.case_cancelled",
            entity_type="trip_case",
            entity_id=case_id,
            after={
                "cancelled": [r["code"] for r in cancelled],
                "to_cancel_with_supplier": [r["code"] for r in open_rows],
            },
            entity_label=case_code,
            summary=f"Case cancelled: {len(cancelled)} pending supplier booking(s) cancelled,"
            f" {len(open_rows)} to cancel with the supplier",
        )
    return {"cancelled": len(cancelled), "to_cancel_with_supplier": len(open_rows)}


async def live_count(conn: asyncpg.Connection, case_id: str) -> int:
    return int(
        await conn.fetchval(
            "SELECT count(*) FROM booking.supplier_bookings WHERE trip_case_id = $1::uuid"
            " AND status IN ('pending', 'requested', 'confirmed')",
            case_id,
        )
        or 0
    )
