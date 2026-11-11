"""Per-departure price adjustments (AA-1073 rest, bk-048; S275 study item bk-17).

An **adjustment** changes the price of the tour (``tp:``) lines on one departure: ``adjust_pct`` (a fraction added to
the line price: -0.10 = 10 % off) or a signed ``amount`` per traveller (adults + children) in one currency, with a
``reason``. Rows are append-only facts (``booking.departure_price_adjustments``), like the allotments:

- **Pending → in force** by a second staff member with ``pricing.write`` (maker-checker: the maker never approves; a
  DB CHECK too). At most one in force and one pending per departure.
- **A change** is a new pending row naming the one in force it ``replaces``; approving it ends the old one
  (``replaced``) in the same transaction.
- **End**: a pending row is ``rejected``, one in force is ``withdrawn``.

The quote engine (:mod:`app.b2b.quotes`) reads the one in force (:func:`in_force`) and applies it to the ``tp:`` lines
on that departure after the agency discount; a line that then sells under the rule's floor margin is still sold and the
line's ``price_note`` says ``below_floor``. Staff endpoints: ``app.routers.b2b_desk`` (read ``b2b_desk.read``, write
and approve ``pricing.write``).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import asyncpg

from app import audit
from app.auth.staff import Staff
from app.errors import Conflict, Forbidden, Invalid, NotFound

MIN_PCT, MAX_PCT = Decimal("-0.5"), Decimal("1")
MAX_AMOUNT = Decimal("100000000")
MAX_REASON = 300

COLUMNS = """
a.id::text AS id, a.departure_id::text AS departure_id, a.adjust_pct, a.amount, a.currency, a.reason,
a.replaces_id::text AS replaces_id, a.created_by, cu.name AS created_by_name, a.created_at, a.approved_by,
au.name AS approved_by_name, a.approved_at, a.ended_at, a.ended_by, eu.name AS ended_by_name, a.end_reason
"""
FROM = """
FROM booking.departure_price_adjustments a
LEFT JOIN booking.staff_users cu ON cu.cis_user_id = a.created_by
LEFT JOIN booking.staff_users au ON au.cis_user_id = a.approved_by
LEFT JOIN booking.staff_users eu ON eu.cis_user_id = a.ended_by
"""
IN_FORCE_SQL = """
SELECT departure_id::text AS departure_id, id::text AS id, adjust_pct, amount, currency
FROM booking.departure_price_adjustments
WHERE departure_id = ANY($1::uuid[]) AND approved_at IS NOT NULL AND ended_at IS NULL
"""


@dataclass(frozen=True)
class Adjustment:
    """The adjustment in force on a departure, as the quote engine applies it."""

    id: str
    pct: Decimal | None
    amount: Decimal | None
    currency: str | None

    def note(self) -> dict[str, Any]:
        """What the quote line's ``price_note`` (and hash) records of it."""
        if self.pct is not None:
            return {"id": self.id, "pct": _text(self.pct)}
        return {"id": self.id, "amount": _text(self.amount or Decimal(0)), "currency": self.currency}


def _text(value: Decimal) -> str:
    return format(value.normalize(), "f") if value != 0 else "0"


def _uuid(value: str | None) -> str | None:
    try:
        return str(uuid.UUID(str(value))) if value is not None else None
    except ValueError:
        return None


async def in_force(conn: asyncpg.Connection, departure_ids: list[str]) -> dict[str, Adjustment]:
    """``{departure id: the adjustment in force}``; a departure without one is absent."""
    if not departure_ids:
        return {}
    return {
        r["departure_id"]: Adjustment(r["id"], r["adjust_pct"], r["amount"], r["currency"])
        for r in await conn.fetch(IN_FORCE_SQL, departure_ids)
    }


def state(row: Any) -> str:
    """``pending`` | ``in_force`` | ``ended``."""
    if row["ended_at"] is not None:
        return "ended"
    return "in_force" if row["approved_at"] is not None else "pending"


def _who(ref: str | None, name: str | None) -> dict[str, Any] | None:
    return None if ref is None else {"id": ref, "name": name}


def adjustment_out(row: Any, staff: Staff | None = None) -> dict[str, Any]:
    st = state(row)
    return {
        "id": row["id"],
        "departure_id": row["departure_id"],
        "adjust_pct": row["adjust_pct"],
        "amount": row["amount"],
        "currency": row["currency"],
        "reason": row["reason"],
        "state": st,
        "replaces_id": row["replaces_id"],
        "created_by": _who(row["created_by"], row["created_by_name"]),
        "created_at": row["created_at"],
        "approved_by": _who(row["approved_by"], row["approved_by_name"]),
        "approved_at": row["approved_at"],
        "ended_by": _who(row["ended_by"], row["ended_by_name"]),
        "ended_at": row["ended_at"],
        "end_reason": row["end_reason"],
        "can_approve": (
            st == "pending"
            and staff is not None
            and staff.can("pricing.write")
            and row["created_by"] != staff.cis_user_id
        ),
    }


async def _row(conn: asyncpg.Connection, adjustment_id: str, *, lock: bool = False) -> Any:
    ident = _uuid(adjustment_id)
    row = (
        await conn.fetchrow(
            f"SELECT {COLUMNS} {FROM} WHERE a.id = $1::uuid" + (" FOR UPDATE OF a" if lock else ""),  # noqa: S608
            ident,
        )
        if ident
        else None
    )
    if row is None:
        raise NotFound("price_adjustment_not_found", f"price adjustment {adjustment_id} not found")
    return row


async def list_for_departure(conn: asyncpg.Connection, staff: Staff, departure_id: str) -> list[dict[str, Any]]:
    """Every adjustment of the departure, newest first."""
    rows = await conn.fetch(
        f"SELECT {COLUMNS} {FROM} WHERE a.departure_id = $1::uuid ORDER BY a.created_at DESC",  # noqa: S608
        departure_id,
    )
    return [adjustment_out(r, staff) for r in rows]


def check_value(data: dict[str, Any]) -> tuple[Decimal | None, Decimal | None, str | None, str]:
    """``(pct, amount, currency, reason)`` of a request, or 422: exactly one of ``adjust_pct`` / ``amount`` (with its
    ``currency``), not zero, within the table CHECKs; a reason."""
    pct, amount = data.get("adjust_pct"), data.get("amount")
    currency = (data.get("currency") or "").strip().upper() or None
    reason = (data.get("reason") or "").strip()
    if (pct is None) == (amount is None):
        raise Invalid("invalid_adjustment", "give adjust_pct or amount, not both")
    if pct is not None:
        pct = Decimal(pct)
        if pct == 0 or not MIN_PCT <= pct <= MAX_PCT:
            raise Invalid("invalid_adjustment", "adjust_pct is between -0.5 and 1, not 0")
        currency = None
    else:
        amount = Decimal(amount)
        if amount == 0 or abs(amount) > MAX_AMOUNT:
            raise Invalid("invalid_adjustment", "amount is not 0 and at most 100,000,000 either way")
        if currency is None or len(currency) != 3 or not currency.isalpha():
            raise Invalid("invalid_currency", "an amount needs its currency (ISO 4217)")
    if not 1 <= len(reason) <= MAX_REASON:
        raise Invalid("invalid_reason", f"a reason of 1 to {MAX_REASON} characters")
    return pct, amount, currency, reason


def describe(pct: Decimal | None, amount: Decimal | None, currency: str | None) -> str:
    if pct is not None:
        return f"{'+' if pct > 0 else ''}{_text(pct * 100)} %"
    return f"{'+' if amount and amount > 0 else ''}{_text(amount or Decimal(0))} {currency} per traveller"


async def create(conn: asyncpg.Connection, staff: Staff, departure_id: str, data: dict[str, Any]) -> dict[str, Any]:
    """The maker step (call inside a transaction): a pending adjustment that replaces the one in force once approved.
    One pending per departure (409 ``price_adjustment_pending``); not on a cancelled departure."""
    dep = _uuid(departure_id)
    row = (
        await conn.fetchrow(
            "SELECT id::text AS id, start_date, status FROM booking.departures WHERE id = $1::uuid FOR UPDATE", dep
        )
        if dep
        else None
    )
    if row is None:
        raise NotFound("departure_not_found", f"departure {departure_id} not found")
    if row["status"] == "cancelled":
        raise Conflict("departure_cancelled", "a cancelled departure sells nothing")
    pct, amount, currency, reason = check_value(data)
    current = await conn.fetch(
        "SELECT id::text AS id, approved_at FROM booking.departure_price_adjustments"
        " WHERE departure_id = $1::uuid AND ended_at IS NULL",
        row["id"],
    )
    pending = next((r["id"] for r in current if r["approved_at"] is None), None)
    if pending is not None:
        raise Conflict(
            "price_adjustment_pending",
            "this departure already has a price adjustment waiting for approval",
            price_adjustment_id=pending,
        )
    replaces = next((r["id"] for r in current if r["approved_at"] is not None), None)
    new_id = await conn.fetchval(
        "INSERT INTO booking.departure_price_adjustments (departure_id, adjust_pct, amount, currency, reason,"
        " replaces_id, created_by) VALUES ($1::uuid, $2, $3, $4, $5, $6::uuid, $7) RETURNING id::text",
        row["id"],
        pct,
        amount,
        currency,
        reason,
        replaces,
        staff.cis_user_id,
    )
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="price_adjustment.created",
        entity_type="departure",
        entity_id=row["id"],
        after={
            "price_adjustment_id": new_id,
            "adjust_pct": pct,
            "amount": amount,
            "currency": currency,
            "replaces_id": replaces,
        },  # fmt: skip
        entity_label=str(row["start_date"]),
        summary=f"Price adjustment {describe(pct, amount, currency)} proposed (waiting for approval)",
    )
    return adjustment_out(await _row(conn, new_id), staff)


async def approve(conn: asyncpg.Connection, staff: Staff, adjustment_id: str) -> dict[str, Any]:
    """The checker step (call inside a transaction): another person than the maker puts it in force, ending the one in
    force. Approving one in force again returns it unchanged."""
    row = await _row(conn, adjustment_id, lock=True)
    st = state(row)
    if st == "in_force":
        return adjustment_out(row, staff)
    if st == "ended":
        raise Conflict("price_adjustment_ended", f"this adjustment was {row['end_reason']}; propose a new one")
    if row["created_by"] == staff.cis_user_id:
        raise Forbidden("maker_checker", "you proposed this price adjustment: another person approves it")
    status = await conn.fetchval(
        "SELECT status FROM booking.departures WHERE id = $1::uuid FOR UPDATE", row["departure_id"]
    )
    if status == "cancelled":
        raise Conflict("departure_cancelled", "a cancelled departure sells nothing")
    ended = await conn.fetch(
        "UPDATE booking.departure_price_adjustments SET ended_at = now(), ended_by = $2, end_reason = 'replaced'"
        " WHERE departure_id = $1::uuid AND approved_at IS NOT NULL AND ended_at IS NULL RETURNING id::text",
        row["departure_id"],
        staff.cis_user_id,
    )
    await conn.execute(
        "UPDATE booking.departure_price_adjustments SET approved_by = $2, approved_at = now() WHERE id = $1::uuid",
        row["id"],
        staff.cis_user_id,
    )
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="price_adjustment.approved",
        entity_type="departure",
        entity_id=row["departure_id"],
        after={"price_adjustment_id": row["id"], "replaced": [r["id"] for r in ended]},
        summary=f"Price adjustment {describe(row['adjust_pct'], row['amount'], row['currency'])} approved",
    )
    return adjustment_out(await _row(conn, row["id"]), staff)


async def end(conn: asyncpg.Connection, staff: Staff, adjustment_id: str) -> dict[str, Any]:
    """End an adjustment (call inside a transaction): a pending one is rejected, one in force is withdrawn (quotes from
    now price without it). Ending an ended one returns it unchanged."""
    row = await _row(conn, adjustment_id, lock=True)
    st = state(row)
    if st == "ended":
        return adjustment_out(row, staff)
    reason = "rejected" if st == "pending" else "withdrawn"
    await conn.execute(
        "UPDATE booking.departure_price_adjustments SET ended_at = now(), ended_by = $2, end_reason = $3"
        " WHERE id = $1::uuid",
        row["id"],
        staff.cis_user_id,
        reason,
    )
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action=f"price_adjustment.{reason}",
        entity_type="departure",
        entity_id=row["departure_id"],
        before={"price_adjustment_id": row["id"], "state": st},
        after={"price_adjustment_id": row["id"], "end_reason": reason},
        summary=f"Price adjustment {describe(row['adjust_pct'], row['amount'], row['currency'])} {reason}",
    )
    return adjustment_out(await _row(conn, row["id"]), staff)
