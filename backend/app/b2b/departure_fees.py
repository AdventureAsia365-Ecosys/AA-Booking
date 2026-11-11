"""Departure fees charged to agencies (AA-1073 rest, bk-048; S275 study item bk-09).

A **departure fee** is money AA charges one agency (``booking.b2b_partners``) on one departure: ``seats`` x
``price_per_seat`` in one ``currency``, for a reason ``min_shortfall`` (the departure is short of its minimum and the
agency's booking or block is why it runs at a loss or is cancelled), ``allotment_unreleased`` (allotted seats kept past
their release date without being used) or ``other`` (a note says why). Rows are facts
(``booking.departure_fees``):

- **Maker-checker**: proposed by a staff member with ``b2b_desk.write`` (``pending``), approved or rejected by a second
  person with ``b2b_desk.approve`` (never the maker: 403 ``maker_checker`` and a DB CHECK). The decision is final.
- **Approval** emits ``departure.fee_charged`` to that agency (outbox trigger: fee id, amount, currency; contract
  booking.b2b.v1 1.8) and the settlement draft job bills it once on the agency's settlement invoice of the month
  (``bk_settlement_draft``, role ``departure_fee``, :mod:`app.agency_billing.drafts`).
- **Terms**: the partner terms clause ``departure_fees`` (bk-048, draft until the business owner approves it) is what
  agencies read in ``GET /terms``.

Staff endpoints: ``app.routers.b2b_desk``. The staff UI offers "Charge fee" on an at-risk departure (B2B desk) and on
an agency's allotment (departure drawer), prefilled from :func:`suggestions`.
"""

from __future__ import annotations

import uuid
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import asyncpg

from app import audit
from app.auth.staff import Staff
from app.errors import Conflict, Forbidden, Invalid, NotFound

REASONS = ("min_shortfall", "allotment_unreleased", "other")
REASON_LABEL = {
    "min_shortfall": "minimum shortfall",
    "allotment_unreleased": "allotment not released",
    "other": "other",
}
STATUSES = ("pending", "approved", "rejected")
MAX_SEATS = 1000
MAX_PRICE = Decimal("100000000")
MAX_NOTE = 300
MAX_PAGE = 200
CENT = Decimal("0.01")

COLUMNS = """
f.id::text AS id, f.departure_id::text AS departure_id, d.start_date, f.partner_id::text AS partner_id, p.agency_name,
f.reason, f.seats::int AS seats, f.price_per_seat, f.currency, f.amount, f.note, f.status, f.created_by,
cu.name AS created_by_name, f.created_at, f.decided_by, du.name AS decided_by_name, f.decided_at,
(SELECT i.number FROM booking.agency_invoice_line il JOIN booking.agency_invoice i ON i.id = il.invoice_id
  WHERE il.source_fee_id = f.id AND NOT il.voided ORDER BY il.created_at DESC LIMIT 1) AS invoice_number,
EXISTS (SELECT 1 FROM booking.agency_invoice_line il WHERE il.source_fee_id = f.id AND NOT il.voided) AS billed
"""
FROM = """
FROM booking.departure_fees f
JOIN booking.departures d ON d.id = f.departure_id
JOIN booking.b2b_partners p ON p.id = f.partner_id
LEFT JOIN booking.staff_users cu ON cu.cis_user_id = f.created_by
LEFT JOIN booking.staff_users du ON du.cis_user_id = f.decided_by
"""


def _uuid(value: str | None) -> str | None:
    try:
        return str(uuid.UUID(str(value))) if value is not None else None
    except ValueError:
        return None


def _who(ref: str | None, name: str | None) -> dict[str, Any] | None:
    return None if ref is None else {"id": ref, "name": name}


def fee_out(row: Any, staff: Staff | None = None) -> dict[str, Any]:
    return {
        "id": row["id"],
        "departure_id": row["departure_id"],
        "start_date": row["start_date"],
        "partner": {"id": row["partner_id"], "agency_name": row["agency_name"]},
        "reason": row["reason"],
        "seats": int(row["seats"]),
        "price_per_seat": row["price_per_seat"],
        "currency": row["currency"],
        "amount": row["amount"],
        "note": row["note"],
        "status": row["status"],
        "created_by": _who(row["created_by"], row["created_by_name"]),
        "created_at": row["created_at"],
        "decided_by": _who(row["decided_by"], row["decided_by_name"]),
        "decided_at": row["decided_at"],
        # Billed = on a live (not voided) settlement line; the invoice number once that invoice is issued.
        "billed": bool(row["billed"]),
        "invoice_number": row["invoice_number"],
        "can_decide": (
            row["status"] == "pending"
            and staff is not None
            and staff.can("b2b_desk.approve")
            and row["created_by"] != staff.cis_user_id
        ),
    }


async def _row(conn: asyncpg.Connection, fee_id: str, *, lock: bool = False) -> Any:
    ident = _uuid(fee_id)
    if ident is None:
        raise NotFound("fee_not_found", f"departure fee {fee_id} not found")
    if lock:
        await conn.execute("SELECT 1 FROM booking.departure_fees WHERE id = $1::uuid FOR UPDATE", ident)
    row = await conn.fetchrow(f"SELECT {COLUMNS} {FROM} WHERE f.id = $1::uuid", ident)  # noqa: S608
    if row is None:
        raise NotFound("fee_not_found", f"departure fee {fee_id} not found")
    return row


async def list_fees(
    conn: asyncpg.Connection,
    staff: Staff,
    *,
    departure_id: str | None = None,
    partner_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Fees, newest first, filtered by departure, agency and status; ``pending`` = how many wait for a checker."""
    if status is not None and status not in STATUSES:
        raise Invalid("invalid_status", f"status is one of {', '.join(STATUSES)}")
    where = (
        "($1::uuid IS NULL OR f.departure_id = $1::uuid) AND ($2::uuid IS NULL OR f.partner_id = $2::uuid)"
        " AND ($3::text IS NULL OR f.status = $3)"
    )
    args = (_uuid(departure_id) if departure_id else None, _uuid(partner_id) if partner_id else None, status)
    rows = await conn.fetch(
        f"SELECT {COLUMNS} {FROM} WHERE {where} ORDER BY f.created_at DESC, f.id LIMIT $4 OFFSET $5",  # noqa: S608
        *args,
        limit,
        offset,
    )
    total = await conn.fetchval(f"SELECT count(*) {FROM} WHERE {where}", *args)  # noqa: S608
    pending = await conn.fetchval("SELECT count(*) FROM booking.departure_fees WHERE status = 'pending'")
    return {
        "items": [fee_out(r, staff) for r in rows],
        "total": int(total or 0),
        "pending": int(pending or 0),
        "limit": limit,
        "offset": offset,
    }


def check_fee(data: dict[str, Any]) -> tuple[str, int, Decimal, str, Decimal, str | None]:
    """``(reason, seats, price_per_seat, currency, amount, note)`` of a request, or 422 (the bk-048 CHECKs)."""
    reason = data.get("reason")
    if reason not in REASONS:
        raise Invalid("invalid_reason", f"reason is one of {', '.join(REASONS)}")
    seats = int(data.get("seats") or 0)
    if not 1 <= seats <= MAX_SEATS:
        raise Invalid("invalid_seats", f"seats is between 1 and {MAX_SEATS}")
    price = Decimal(str(data.get("price_per_seat") or 0)).quantize(CENT, rounding=ROUND_HALF_UP)
    if not Decimal(0) < price <= MAX_PRICE:
        raise Invalid("invalid_price", "price_per_seat is above 0")
    currency = (data.get("currency") or "").strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        raise Invalid("invalid_currency", "currency is an ISO 4217 code")
    note = (data.get("note") or "").strip() or None
    if note is not None and len(note) > MAX_NOTE:
        raise Invalid("invalid_note", f"a note of at most {MAX_NOTE} characters")
    if reason == "other" and note is None:
        raise Invalid("note_required", "say why in the note when the reason is other")
    return reason, seats, price, currency, (price * seats).quantize(CENT, rounding=ROUND_HALF_UP), note


async def create(conn: asyncpg.Connection, staff: Staff, departure_id: str, data: dict[str, Any]) -> dict[str, Any]:
    """The maker step (call inside a transaction): a pending fee for ``partner_id`` on the departure."""
    dep_id = _uuid(departure_id)
    dep = (
        await conn.fetchrow("SELECT id::text AS id, start_date FROM booking.departures WHERE id = $1::uuid", dep_id)
        if dep_id
        else None
    )
    if dep is None:
        raise NotFound("departure_not_found", f"departure {departure_id} not found")
    partner = _uuid(data.get("partner_id"))
    agency = (
        await conn.fetchval("SELECT agency_name FROM booking.b2b_partners WHERE id = $1::uuid", partner)
        if partner
        else None
    )
    if agency is None:
        raise Invalid("partner_not_found", "partner_id is not a B2B partner")
    reason, seats, price, currency, amount, note = check_fee(data)
    fee_id = await conn.fetchval(
        "INSERT INTO booking.departure_fees (departure_id, partner_id, reason, seats, price_per_seat, currency, amount,"
        " note, created_by) VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8, $9) RETURNING id::text",
        dep["id"],
        partner,
        reason,
        seats,
        price,
        currency,
        amount,
        note,
        staff.cis_user_id,
    )
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="departure_fee.created",
        entity_type="departure",
        entity_id=dep["id"],
        after={
            "fee_id": fee_id,
            "partner_id": partner,
            "reason": reason,
            "seats": seats,
            "price_per_seat": price,
            "currency": currency,
            "amount": amount,
        },  # fmt: skip
        entity_label=str(dep["start_date"]),
        summary=f"Departure fee {amount} {currency} for {agency} ({REASON_LABEL[reason]}) proposed",
    )
    return fee_out(await _row(conn, fee_id), staff)


async def decide(conn: asyncpg.Connection, staff: Staff, fee_id: str, *, approve: bool) -> dict[str, Any]:
    """The checker step (call inside a transaction): another person than the maker approves (the agency is told and
    the settlement bills it) or rejects. A decided fee is final: the same decision again returns it, the other is 409
    ``fee_decided``."""
    row = await _row(conn, fee_id, lock=True)
    wanted = "approved" if approve else "rejected"
    if row["status"] != "pending":
        if row["status"] == wanted:
            return fee_out(row, staff)
        raise Conflict("fee_decided", f"this fee is already {row['status']}")
    if row["created_by"] == staff.cis_user_id:
        raise Forbidden("maker_checker", "you proposed this fee: another person decides it")
    await conn.execute(
        "UPDATE booking.departure_fees SET status = $2, decided_by = $3, decided_at = now() WHERE id = $1::uuid",
        row["id"],
        wanted,
        staff.cis_user_id,
    )
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action=f"departure_fee.{wanted}",
        entity_type="departure",
        entity_id=row["departure_id"],
        before={"fee_id": row["id"], "status": "pending"},
        after={"fee_id": row["id"], "status": wanted},
        entity_label=str(row["start_date"]),
        summary=f"Departure fee {row['amount']} {row['currency']} for {row['agency_name']} {wanted}",
    )
    return fee_out(await _row(conn, row["id"]), staff)


def suggestions(
    *,
    min_to_operate: int,
    sold: int,
    status: str,
    allotments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """What "Charge fee" prefills (pure): the departure's shortfall of its minimum (a scheduled departure with a
    minimum, short of it), and each agency's unused allotment seats (in force or released). Staff choose the agency
    of a shortfall and the price in every case."""
    out: list[dict[str, Any]] = []
    if min_to_operate > 0 and status != "cancelled" and sold < min_to_operate:
        out.append({"reason": "min_shortfall", "partner_id": None, "seats": min_to_operate - sold})
    for a in allotments:
        unused = int(a.get("unused") or 0)
        if unused > 0 and a.get("state") in ("in_force", "released"):
            out.append({"reason": "allotment_unreleased", "partner_id": a["partner_id"], "seats": unused})
    return out
