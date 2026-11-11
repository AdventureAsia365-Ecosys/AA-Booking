"""Bookings AA staff enter on behalf of an agency (AA-1075 bk-11, bk-047; S275 study item bk-11).

An agency booking that arrives by e-mail or chat is keyed in on the B2B desk (``POST /booking/b2b-desk/bookings``,
permission ``b2b_desk.book``: admin, sales). One transaction runs the agency API's own services, so every rule of the
API applies (served tours, departure seats and the agency's allotment, the credit stop, the re-hold limit, the
cancellation policy locked at confirm):

1. **Quote** (``app.b2b.quotes.quote_in``) for the chosen agency (``b2b_partners``) at its tier / rate card, in the
   chosen currency. The agency reference is the proposal ref; the version is the next free one for that ref.
2. **Hold** (``app.b2b.holds.place_in``) with the key ``desk:<Idempotency-Key>``.
3. **Confirm** (``app.b2b.holds.confirm_in``): the B2B trip case says ``entered_via = 'staff'`` and names the staff
   member, so the bk-047 trigger emits ``booking.created_by_aa`` to the agency's CRM, which imports the booking.

**Price override** (maker-checker): staff may set the booking total (``override_total`` + a reason); the quote lines
are scaled to it before they are hashed, so the hold and the trip case carry that price. At or above the floor (the
rule's floor margin, ``floor_total``; never above the rule price when an agency discount is already lower) it books at
once. Below the floor the booking stops after the hold (``awaiting_approval``; the hold keeps the seats for
``b2b_hold_hours``) until a second staff member with ``b2b_desk.approve`` (admin, contracting) approves it (confirm,
then ``booked``) or rejects it (the hold is released). The person who entered it never decides it (403
``maker_checker``, CHECK in the database). A hold that expired first makes the row ``lapsed``.

Every row is ``booking.b2b_desk_bookings`` (append-only facts, status moves once). The same ``Idempotency-Key``
returns the same row; another request with it is 422 ``idempotency_key_reused``. No personal data: travellers are
counts, the agency reference is the agency's own id.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import asyncpg

from app import audit
from app.auth.staff import Staff
from app.b2b import holds, quotes
from app.b2b.errors import B2BError
from app.errors import Conflict, Forbidden, Gone, Invalid, NotFound, ServiceError, Unavailable
from app.notifications import service as notifications

log = logging.getLogger(__name__)

MAX_PAGE = 100
STATUSES = ("awaiting_approval", "booked", "rejected", "lapsed")
NOTIFY_TYPE = "b2b_desk.override_pending"
KEY_PREFIX = "desk:"

ROW_SQL = """
SELECT d.id::text AS id, d.partner_id::text AS partner_id, p.agency_name, p.tenant_id::text AS tenant_id,
       p.m2m_client_id, d.agency_ref, d.quote_id::text AS quote_id, d.hold_id::text AS hold_id,
       d.trip_case_id::text AS trip_case_id, tc.code AS trip_case_code, d.currency::text AS currency,
       d.quoted_total, d.floor_total, d.override_total, d.override_reason, d.idempotency_key, d.entered_by,
       eb.name AS entered_by_name, d.entered_at, d.decided_by, db.name AS decided_by_name, d.decided_at,
       d.status AS stored_status, h.expires_at AS hold_expires_at, h.status AS hold_status,
       CASE WHEN d.status = 'awaiting_approval' AND (h.status <> 'active' OR h.expires_at <= now()) THEN 'lapsed'
            ELSE d.status END AS status,
       q.price_hash::text AS price_hash, q.total_b2b_price
FROM booking.b2b_desk_bookings d
JOIN booking.b2b_partners p ON p.id = d.partner_id
JOIN booking.b2b_holds h ON h.id = d.hold_id
JOIN booking.b2b_quotes q ON q.id = d.quote_id
LEFT JOIN booking.trip_cases tc ON tc.id = d.trip_case_id
LEFT JOIN booking.staff_users eb ON eb.cis_user_id = d.entered_by
LEFT JOIN booking.staff_users db ON db.cis_user_id = d.decided_by
"""
PARTNER_SQL = (
    "SELECT id::text AS id, tenant_id::text AS tenant_id, m2m_client_id, status, agency_name"
    " FROM booking.b2b_partners WHERE id = $1::uuid"
)
NEXT_VERSION_SQL = (
    "SELECT coalesce(max(proposal_version), 0) + 1 FROM booking.b2b_quotes WHERE partner_id = $1::uuid"
    " AND proposal_ref = $2"
)
INSERT_SQL = """
INSERT INTO booking.b2b_desk_bookings
    (partner_id, agency_ref, quote_id, hold_id, trip_case_id, status, currency, quoted_total, floor_total,
     override_total, override_reason, idempotency_key, entered_by)
VALUES ($1::uuid, $2, $3::uuid, $4::uuid, $5::uuid, $6, $7, $8, $9, $10, $11, $12, $13)
RETURNING id::text
"""

# B2B API errors (contract codes) as staff errors: same code and message, the staff body shape.
_STAFF_ERRORS: dict[int, type[ServiceError]] = {404: NotFound, 409: Conflict, 410: Gone, 422: Invalid, 503: Unavailable}


def staff_error(exc: B2BError) -> ServiceError:
    """A B2B service error as the staff API answers it (``{"detail": code, "message": …}``); 429 reads as 409."""
    return _STAFF_ERRORS.get(exc.status_code, Conflict)(exc.error, exc.message, **exc.extra)


def _uuid(value: str) -> str | None:
    try:
        return str(uuid.UUID(value))
    except (TypeError, ValueError):
        return None


def money_text(value: Any) -> str | None:
    return None if value is None else format(Decimal(value).quantize(Decimal("0.01")), "f")


def row_out(row: Any, staff: Staff) -> dict[str, Any]:
    """A staff booking row. The floor price (a margin) only with ``pricing.read``."""
    return {
        "id": row["id"],
        "status": row["status"],
        "agency": {"partner_id": row["partner_id"], "name": row["agency_name"], "tenant_id": row["tenant_id"]},
        "agency_ref": row["agency_ref"],
        "quote_id": row["quote_id"],
        "hold_id": row["hold_id"],
        "hold_expires_at": row["hold_expires_at"],
        "trip_case": {"id": row["trip_case_id"], "code": row["trip_case_code"]} if row["trip_case_id"] else None,
        "currency": row["currency"],
        "total": money_text(row["total_b2b_price"]),
        "quoted_total": money_text(row["quoted_total"]),
        "floor_total": money_text(row["floor_total"]) if staff.can("pricing.read") else None,
        "override_total": money_text(row["override_total"]),
        "override_reason": row["override_reason"],
        "entered_by": {"id": row["entered_by"], "name": row["entered_by_name"]},
        "entered_at": row["entered_at"],
        "decided_by": {"id": row["decided_by"], "name": row["decided_by_name"]} if row["decided_by"] else None,
        "decided_at": row["decided_at"],
        "can_decide": row["status"] == "awaiting_approval"
        and staff.can("b2b_desk.approve")
        and staff.cis_user_id != row["entered_by"],
    }


async def _row(conn: asyncpg.Connection, desk_id: str, *, lock: bool = False) -> Any:
    ident = _uuid(desk_id)
    row = None
    if ident is not None:
        row = await conn.fetchrow(ROW_SQL + " WHERE d.id = $1::uuid" + (" FOR UPDATE OF d" if lock else ""), ident)
    if row is None:
        raise NotFound("desk_booking_not_found", f"staff booking {desk_id} not found")
    return row


async def _audit(conn: asyncpg.Connection, staff: Staff, action: str, row_id: str, summary: str, **after: Any) -> None:
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action=action,
        entity_type="b2b_desk_booking",
        entity_id=row_id,
        after=after or None,
        entity_label=f"desk booking {row_id[:8]}",
        summary=summary,
    )


# ── Enter ────────────────────────────────────────────────────────────────────


async def enter(
    pool: asyncpg.Pool,
    staff: Staff,
    *,
    partner_id: str,
    agency_ref: str,
    currency: str,
    lines: list[quotes.AskLine],
    idempotency_key: str,
    override_total: Decimal | None = None,
    override_reason: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Quote, hold and (unless an override below the floor waits) confirm one agency booking; the staff row."""
    reason = (override_reason or "").strip() or None
    if (override_total is None) != (reason is None):
        raise Invalid("override_reason_required", "a price override needs a reason (and a reason needs a price)")
    if override_total is not None and override_total < 0:
        raise Invalid("invalid_override", "the override total cannot be negative")
    agency_ref = agency_ref.strip()
    if not 1 <= len(agency_ref) <= 64:
        raise Invalid("invalid_agency_ref", "the agency reference is 1-64 characters")
    now = now or datetime.now(UTC)
    for attempt in (1, 2):
        try:
            async with pool.acquire() as conn, conn.transaction():
                return await _enter(
                    conn,
                    staff,
                    partner_id=partner_id,
                    agency_ref=agency_ref,
                    currency=currency,
                    lines=lines,
                    key=idempotency_key,
                    override_total=override_total,
                    reason=reason,
                    now=now,
                )
        except asyncpg.UniqueViolationError:
            # A concurrent request with the same key won: the second pass answers from it.
            if attempt == 2:
                raise
        except B2BError as exc:
            raise staff_error(exc) from None
    raise AssertionError("unreachable")  # pragma: no cover


async def _enter(
    conn: asyncpg.Connection,
    staff: Staff,
    *,
    partner_id: str,
    agency_ref: str,
    currency: str,
    lines: list[quotes.AskLine],
    key: str,
    override_total: Decimal | None,
    reason: str | None,
    now: datetime,
) -> dict[str, Any]:
    replay = await conn.fetchrow(ROW_SQL + " WHERE d.idempotency_key = $1", key)
    if replay is not None:
        if (replay["partner_id"], replay["agency_ref"]) != (_uuid(partner_id), agency_ref):
            raise Invalid("idempotency_key_reused", "this Idempotency-Key was already used for another booking")
        return row_out(replay, staff)
    ident = _uuid(partner_id)
    partner = await conn.fetchrow(PARTNER_SQL, ident) if ident else None
    if partner is None:
        raise NotFound("partner_not_found", "no B2B partner (agency) with this id")
    if partner["status"] != "active":
        raise Conflict("partner_suspended", f"{partner['agency_name']} is suspended; no new booking")
    caller = holds.Caller(
        partner_id=partner["id"],
        tenant_id=partner["tenant_id"],
        client_id=partner["m2m_client_id"],
        staff_ref=staff.cis_user_id,
    )
    version = int(await conn.fetchval(NEXT_VERSION_SQL, partner["id"], agency_ref))
    stored = await quotes.quote_in(
        conn,
        partner_id=partner["id"],
        client_id=partner["m2m_client_id"],
        proposal_ref=agency_ref,
        proposal_version=version,
        currency=currency,
        lines=lines,
        now=now,
        override_total=override_total,
    )
    # An agency whose approved discount already sells below the floor needs no second person for that price.
    floor = min(stored.floor_total, stored.quoted_total)
    waiting = override_total is not None and override_total < floor
    hold = await holds.place_in(
        conn,
        caller,
        quote_id=stored.quote_id,
        price_hash=stored.body["price_hash"],
        proposal_ref=agency_ref,
        proposal_version=version,
        idempotency_key=f"{KEY_PREFIX}{key}"[:200],
        now=now,
    )
    case_id = None
    if not waiting:
        booking, _code = await holds.confirm_in(
            conn,
            caller,
            hold["hold_id"],
            price_hash=stored.body["price_hash"],
            deposit_ref=None,
            idempotency_key=key,
        )
        case_id = booking["booking_id"]
    row_id = await conn.fetchval(
        INSERT_SQL,
        partner["id"],
        agency_ref,
        stored.quote_id,
        hold["hold_id"],
        case_id,
        "awaiting_approval" if waiting else "booked",
        currency,
        stored.quoted_total,
        floor,
        override_total,
        reason,
        key,
        staff.cis_user_id,
    )
    await _audit(
        conn,
        staff,
        "b2b_desk_booking.entered",
        row_id,
        f"Booking for {partner['agency_name']} ({agency_ref}) "
        + ("waits for a second approver (price below the floor)" if waiting else "entered and confirmed"),
        partner_id=partner["id"],
        agency_ref=agency_ref,
        quote_id=stored.quote_id,
        hold_id=hold["hold_id"],
        trip_case_id=case_id,
        override_total=override_total,
    )
    if waiting:
        await notifications.notify_staff(
            conn,
            type=NOTIFY_TYPE,
            title=f"Price below floor for {partner['agency_name']}: approve or reject",
            message=f"Agency reference {agency_ref}. The seats are held until {hold['expires_at']}.",
            entity_type="b2b_desk_booking",
            entity_id=row_id,
            data={"agency_ref": agency_ref, "hold_id": hold["hold_id"]},
        )
    log.info("b2b_desk_booking_entered row=%s partner=%s waiting=%s", row_id, partner["id"], waiting)
    return row_out(await _row(conn, row_id), staff)


# ── Decide ───────────────────────────────────────────────────────────────────


def _check_decider(row: Any, staff: Staff) -> None:
    if row["entered_by"] == staff.cis_user_id:
        raise Forbidden("maker_checker", "the person who entered the booking cannot decide its price")


async def approve(pool: asyncpg.Pool, staff: Staff, desk_id: str) -> dict[str, Any]:
    """A second staff member approves a price below the floor: the hold is confirmed (the trip case and the
    ``booking.created_by_aa`` event), the row ``booked``. A hold that expired first: the row is ``lapsed`` (410)."""
    lapsed = False
    async with pool.acquire() as conn, conn.transaction():
        row = await _row(conn, desk_id, lock=True)
        if row["stored_status"] == "booked":
            return row_out(row, staff)
        if row["stored_status"] != "awaiting_approval":
            raise Conflict("desk_booking_decided", f"this booking is {row['stored_status']}")
        _check_decider(row, staff)
        if row["status"] == "lapsed":
            await _set_status(conn, staff, row, "lapsed", decided=False)
            lapsed = True
        else:
            caller = holds.Caller(
                partner_id=row["partner_id"],
                tenant_id=row["tenant_id"],
                client_id=row["m2m_client_id"],
                staff_ref=staff.cis_user_id,
                entered_by=row["entered_by"],
            )
            try:
                booking, _code = await holds.confirm_in(
                    conn,
                    caller,
                    row["hold_id"],
                    price_hash=row["price_hash"],
                    deposit_ref=None,
                    idempotency_key=row["idempotency_key"],
                )
            except B2BError as exc:
                raise staff_error(exc) from None
            await conn.execute(
                "UPDATE booking.b2b_desk_bookings SET status = 'booked', trip_case_id = $2::uuid, decided_by = $3,"
                " decided_at = now() WHERE id = $1::uuid",
                row["id"],
                booking["booking_id"],
                staff.cis_user_id,
            )
            await _audit(
                conn,
                staff,
                "b2b_desk_booking.approved",
                row["id"],
                f"Approved the price for {row['agency_name']} ({row['agency_ref']}) and confirmed the booking",
                trip_case_id=booking["booking_id"],
                override_total=row["override_total"],
            )
        out = row_out(await _row(conn, row["id"]), staff)
    if lapsed:
        raise Gone("hold_expired", "the hold expired before the approval; enter the booking again")
    return out


async def reject(pool: asyncpg.Pool, staff: Staff, desk_id: str) -> dict[str, Any]:
    """A second staff member rejects a price below the floor: the hold is released (its seats go back on sale)."""
    async with pool.acquire() as conn, conn.transaction():
        row = await _row(conn, desk_id, lock=True)
        if row["stored_status"] == "rejected":
            return row_out(row, staff)
        if row["stored_status"] != "awaiting_approval":
            raise Conflict("desk_booking_decided", f"this booking is {row['stored_status']}")
        _check_decider(row, staff)
        await _set_status(conn, staff, row, "rejected", decided=True)
        return row_out(await _row(conn, row["id"]), staff)


async def _set_status(conn: asyncpg.Connection, staff: Staff, row: Any, status: str, *, decided: bool) -> None:
    await conn.execute(
        "UPDATE booking.b2b_holds SET status = 'released', released_at = now() WHERE id = $1::uuid"
        " AND status = 'active'",
        row["hold_id"],
    )
    await conn.execute(
        "UPDATE booking.b2b_desk_bookings SET status = $2, decided_by = $3,"
        " decided_at = CASE WHEN $3::text IS NULL THEN NULL ELSE now() END WHERE id = $1::uuid",
        row["id"],
        status,
        staff.cis_user_id if decided else None,
    )
    await _audit(
        conn,
        staff,
        f"b2b_desk_booking.{status}",
        row["id"],
        f"Staff booking for {row['agency_name']} ({row['agency_ref']}) {status}; the hold is released",
    )


# ── Read ─────────────────────────────────────────────────────────────────────


async def list_bookings(
    conn: asyncpg.Connection, staff: Staff, *, status: str | None, limit: int, offset: int
) -> dict[str, Any]:
    if status is not None and status not in STATUSES:
        raise Invalid("invalid_status", f"status must be one of {', '.join(STATUSES)}")
    where = "" if status is None else " WHERE x.status = $3"
    args: list[Any] = [limit, offset] + ([] if status is None else [status])
    rows = await conn.fetch(
        f"SELECT * FROM ({ROW_SQL}) x{where} ORDER BY x.entered_at DESC, x.id LIMIT $1 OFFSET $2",  # noqa: S608
        *args,
    )
    total = await conn.fetchval(f"SELECT count(*) FROM ({ROW_SQL}) x{where.replace('$3', '$1')}", *args[2:])  # noqa: S608
    waiting = await conn.fetchval(f"SELECT count(*) FROM ({ROW_SQL}) x WHERE x.status = 'awaiting_approval'")  # noqa: S608
    return {
        "items": [row_out(r, staff) for r in rows],
        "total": int(total or 0),
        "awaiting_approval": int(waiting or 0),
        "limit": limit,
        "offset": offset,
    }


async def get(conn: asyncpg.Connection, staff: Staff, desk_id: str) -> dict[str, Any]:
    return row_out(await _row(conn, desk_id), staff)
