"""B2B cancellation fees (AA-974, bk-021; root ADR 0005a decision 4; contract booking.b2b.v1 ``/bookings/{id}/cancel``).

- **Policy** (``booking.cancellation_policies``): the fee AA charges an agency when it cancels a confirmed B2B
  booking, as tiers ``{min_days, fee_pct}``: cancelling ``d`` days before departure costs the ``fee_pct`` of the tier
  with the largest ``min_days <= max(d, 0)`` (:func:`fee_pct_for`), so a cancel on or after the start day takes the
  0-day tier. A policy belongs to a trip plan or, with no plan, is the channel default; rows are append-only and in
  force only once a second person approved them (``policy_approvals``, maker-checker like pricing rules and FX rows).
  In force on a day = the newest approved row of the plan dated on or before it, else the newest approved default.
  Not the supplier-side terms (``supplier_contracts.cancellation_policy`` stays free text).
- **Which policy a booking uses**: the one in force when the hold was confirmed, locked on the hold
  (``b2b_holds.cancellation_policy_id``, :func:`policy_for_lines`): the plan of the booking's first ``tp:`` line, else
  the default. A policy approved after the booking never changes its fee (a hold confirmed before the policy was
  locked resolves it among the rows approved by its confirm; bk-021 backfills the seeded default on older ones).
- **Fee** (:func:`compute`): days before departure = the trip case start date minus the UTC day of the cancel; fee =
  the booking's quoted ``total_b2b_price`` x ``fee_pct`` / 100, half-up to the currency unit (cent; whole VND);
  refundable = total - fee. In the quote currency, never re-converted (R17). A booking on a departure AA cancelled
  (staff status ``cancelled``) costs no fee (proposed default for Ms. Thu).
- **Cancel** (:func:`cancel_booking`, scope ``book``): ``dry_run`` reads only and returns the same numbers. A real
  cancel, in one transaction under a lock of the hold: one ``b2b_cancellations`` row (append-only), the hold lines
  ``cancelled`` (their seats are no longer sold, so the departure gets them back at once), the B2B trip case
  ``confirmed`` (or ``supplier_confirming`` / ``pre_departure`` once supplier bookings moved it, AA-682 critic B2) →
  ``cancelled`` (bk-005 / bk-026 edges; pending supplier bookings cancelled, requested / confirmed ones notified to
  staff; the status note names the client, reason and fee), an audit row. A
  repeat returns the stored cancellation (idempotent on the booking); another partner's booking is 404.
- **Staff admin** (``/booking/pricing/cancellation-policies``, ``pricing.read`` / ``pricing.write``): list with the
  status (pending, in force, scheduled, superseded), create a pending row, approve another person's row.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

import asyncpg

from app import audit
from app.auth.staff import Staff
from app.b2b.components import PREFIX_PLAN, parse_component
from app.b2b.errors import B2BError
from app.errors import Conflict, Invalid, NotFound
from app.ops import supplier_bookings
from app.pricing import policy as pricing_policy

log = logging.getLogger(__name__)

CHANNEL = "b2b"
MAX_TIERS = 20
MAX_DAYS = 730
HUNDRED = Decimal("100")
PCT_PLACES = Decimal("0.01")
REASONS = ("client_cancelled", "agency_cancelled", "departure_cancelled", "other")
ZERO_DECIMAL_CURRENCIES = frozenset({"VND", "JPY", "KRW"})
# Case statuses of a confirmed B2B booking (AA-682: the supply track moves it on from confirmed).
CANCELLABLE_CASE_STATUSES = frozenset({"confirmed", "supplier_confirming", "pre_departure"})


class TierError(ValueError):
    """A fee schedule that breaks the policy rules; the message is safe to show to staff."""


@dataclass(frozen=True)
class Tier:
    min_days: int
    fee_pct: Decimal


def parse_tiers(raw: Any) -> tuple[Tier, ...]:
    """Validate a fee schedule and return it sorted by ``min_days`` (descending). Rules: 1 to 20 tiers; ``min_days``
    a whole number 0 to 730, unique, one of them 0; ``fee_pct`` 0 to 100 with at most 2 decimals; the fee never rises
    when there are more days left (a later cancel never costs less)."""
    if isinstance(raw, str):
        raw = json.loads(raw, parse_float=Decimal)
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_TIERS:
        raise TierError(f"a fee schedule has 1 to {MAX_TIERS} tiers")
    tiers: list[Tier] = []
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"min_days", "fee_pct"}:
            raise TierError("each tier is {min_days, fee_pct}")
        days, pct = item["min_days"], item["fee_pct"]
        if isinstance(days, bool) or not isinstance(days, int) or not 0 <= days <= MAX_DAYS:
            raise TierError(f"min_days must be a whole number from 0 to {MAX_DAYS}")
        try:
            pct = Decimal(str(pct)) if not isinstance(pct, bool) else None
        except InvalidOperation:
            pct = None
        if pct is None or not pct.is_finite() or not 0 <= pct <= HUNDRED or pct != pct.quantize(PCT_PLACES):
            raise TierError("fee_pct must be a number from 0 to 100 with at most 2 decimals")
        tiers.append(Tier(days, pct.quantize(PCT_PLACES)))
    days_seen = [t.min_days for t in tiers]
    if len(set(days_seen)) != len(days_seen):
        raise TierError("min_days must be unique")
    if 0 not in days_seen:
        raise TierError("a tier with min_days 0 is required (cancels on or after the start day)")
    ordered = sorted(tiers, key=lambda t: t.min_days, reverse=True)
    for earlier, later in zip(ordered, ordered[1:], strict=False):  # earlier = more days left
        if earlier.fee_pct > later.fee_pct:
            raise TierError("the fee must not fall as the departure gets closer")
    return tuple(ordered)


def tiers_json(tiers: Sequence[Tier]) -> str:
    """The JSONB form (numbers, so the bk-021 CHECK can read them)."""
    return json.dumps([{"min_days": t.min_days, "fee_pct": float(t.fee_pct)} for t in tiers])


def tiers_out(tiers: Sequence[Tier]) -> list[dict[str, Any]]:
    return [{"min_days": t.min_days, "fee_pct": format(t.fee_pct, "f")} for t in tiers]


def fee_pct_for(tiers: Sequence[Tier], days_before: int) -> Decimal:
    """The fee % of the tier with the largest ``min_days <= max(days_before, 0)``."""
    days = max(days_before, 0)
    return max((t for t in tiers if t.min_days <= days), key=lambda t: t.min_days).fee_pct


def unit(currency: str) -> Decimal:
    return Decimal("1") if currency in ZERO_DECIMAL_CURRENCIES else PCT_PLACES


@dataclass(frozen=True)
class Fee:
    days_before: int
    fee_pct: Decimal
    fee: Decimal
    refundable: Decimal


def compute(tiers: Sequence[Tier], *, total: Decimal, currency: str, departure: date, today: date) -> Fee:
    days = (departure - today).days
    pct = fee_pct_for(tiers, days)
    fee = min((total * pct / HUNDRED).quantize(unit(currency), rounding=ROUND_HALF_UP), total)
    return Fee(days_before=days, fee_pct=pct, fee=fee, refundable=total - fee)


def money(amount: Decimal, currency: str) -> dict[str, str]:
    places = unit(currency)
    return {"amount": format(amount.quantize(places, rounding=ROUND_HALF_UP), "f"), "currency": currency}


# ── Policy in force ──────────────────────────────────────────────────────────

POLICY_COLUMNS = "p.id::text AS id, p.trip_plan_id::text AS trip_plan_id, p.valid_from, p.tiers::text AS tiers"
IN_FORCE_SQL = f"""
SELECT {POLICY_COLUMNS}
FROM booking.cancellation_policies p
JOIN booking.policy_approvals a ON a.cancellation_policy_id = p.id
WHERE p.channel = $1 AND (p.trip_plan_id = $2::uuid OR p.trip_plan_id IS NULL) AND p.valid_from <= $3
  AND ($4::timestamptz IS NULL OR a.approved_at <= $4)
ORDER BY (p.trip_plan_id IS NULL), p.valid_from DESC
LIMIT 1
"""  # noqa: S608 — constant fragment
BY_ID_SQL = f"SELECT {POLICY_COLUMNS} FROM booking.cancellation_policies p WHERE p.id = $1::uuid"  # noqa: S608


@dataclass(frozen=True)
class Policy:
    id: str
    tiers: tuple[Tier, ...]


def _policy(row: Any) -> Policy:
    return Policy(id=row["id"], tiers=parse_tiers(row["tiers"]))


async def policy_in_force(
    conn: asyncpg.Connection, plan_id: str | None, on: date, *, approved_by: datetime | None = None
) -> Policy | None:
    """The newest approved policy of the plan dated on or before ``on``, else the newest approved default. With
    ``approved_by``, only rows approved by then count (a booking confirmed then could not have been made under a row
    approved later, even one dated earlier)."""
    row = await conn.fetchrow(IN_FORCE_SQL, CHANNEL, plan_id, on, approved_by)
    return _policy(row) if row is not None else None


def first_plan(lines: Sequence[Any]) -> str | None:
    """The trip plan of the first ``tp:`` line (by line number), the booking's tour."""
    for line in sorted(lines, key=lambda r: r["line_no"]):
        try:
            prefix, ident = parse_component(line["component_id"])
        except ValueError:
            continue
        if prefix == PREFIX_PLAN:
            return ident
    return None


async def policy_for_lines(
    conn: asyncpg.Connection, lines: Sequence[Any], on: date, *, approved_by: datetime | None = None
) -> Policy | None:
    """The policy a booking of these quote lines is cancelled under, as in force on ``on`` (the confirm day)."""
    return await policy_in_force(conn, first_plan(lines), on, approved_by=approved_by)


# ── Cancel (POST /booking/b2b/v1/bookings/{id}/cancel) ───────────────────────

BOOKING_SQL = """
SELECT h.id::text AS hold_id, h.quote_id::text AS quote_id, h.status AS hold_status, h.confirmed_at,
       h.cancellation_policy_id::text AS cancellation_policy_id, c.id::text AS trip_case_id, c.code, c.status,
       c.start_date
FROM booking.b2b_holds h
JOIN booking.trip_cases c ON c.id = h.trip_case_id
WHERE h.trip_case_id = $1::uuid AND h.partner_id = $2::uuid
"""
STORED_SQL = """
SELECT policy_id::text AS policy_id, days_before_departure, fee_pct, currency::text AS currency, fee_amount,
       refundable_amount, cancelled_at
FROM booking.b2b_cancellations WHERE hold_id = $1::uuid
"""
# A departure of the booking that AA cancelled (staff status 'cancelled'): the agency pays no fee.
AA_CANCELLED_SQL = """
SELECT EXISTS (
    SELECT 1 FROM booking.b2b_hold_lines l JOIN booking.departures d ON d.id = l.departure_id
    WHERE l.hold_id = $1::uuid AND d.status = 'cancelled'
)
"""
QUOTE_SQL = "SELECT total_b2b_price, currency::text AS currency FROM booking.b2b_quotes WHERE id = $1::uuid"
QUOTE_LINES_SQL = "SELECT line_no, component_id FROM booking.b2b_quote_lines WHERE quote_id = $1::uuid"
INSERT_SQL = """
INSERT INTO booking.b2b_cancellations
    (hold_id, trip_case_id, partner_id, m2m_client_id, policy_id, reason_code, idempotency_key, departure_date,
     days_before_departure, fee_pct, currency, total_b2b_price, fee_amount, refundable_amount, cancelled_at)
VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5::uuid, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
"""


def _result(
    booking_id: str,
    *,
    dry_run: bool,
    policy_id: str,
    fee: Fee,
    currency: str,
    cancelled_at: datetime | None,
) -> dict[str, Any]:
    return {
        "booking_id": booking_id,
        "dry_run": dry_run,
        "fee": money(fee.fee, currency),
        "refundable_b2b_amount": money(fee.refundable, currency),
        "fee_basis": {
            "policy_id": policy_id,
            "days_before_departure": fee.days_before,
            "fee_pct": format(fee.fee_pct.quantize(PCT_PLACES), "f"),
        },
        "cancelled_at": cancelled_at.isoformat() if cancelled_at is not None else None,
    }


def _stored(booking_id: str, row: Any) -> dict[str, Any]:
    fee = Fee(
        days_before=int(row["days_before_departure"]),
        fee_pct=Decimal(row["fee_pct"]),
        fee=Decimal(row["fee_amount"]),
        refundable=Decimal(row["refundable_amount"]),
    )
    currency = row["currency"].strip()
    return _result(
        booking_id,
        dry_run=False,
        policy_id=row["policy_id"],
        fee=fee,
        currency=currency,
        cancelled_at=row["cancelled_at"],
    )


async def _booking_policy(conn: asyncpg.Connection, booking: Any) -> Policy:
    if booking["cancellation_policy_id"]:
        row = await conn.fetchrow(BY_ID_SQL, booking["cancellation_policy_id"])
        if row is not None:
            return _policy(row)
    # No locked policy (confirmed by the AA-973 code after bk-021 was applied; bk-021 backfills the older ones): the
    # one in force at the confirm, counting only rows approved by then, so a row approved later (even backdated)
    # never re-prices the booking.
    confirmed_at = booking["confirmed_at"] or datetime.now(UTC)
    found = await policy_for_lines(
        conn,
        await conn.fetch(QUOTE_LINES_SQL, booking["quote_id"]),
        confirmed_at.astimezone(UTC).date(),
        approved_by=confirmed_at,
    )
    if found is None:
        raise B2BError("unavailable", "no cancellation policy is in force for this booking")
    return found


async def cancel_booking(
    pool: asyncpg.Pool,
    *,
    partner_id: str,
    client_id: str,
    booking_id: str,
    reason_code: str,
    dry_run: bool,
    idempotency_key: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Cancel (or preview the cancel of) a confirmed B2B booking; the contract ``CancellationResult``."""
    if reason_code not in REASONS:
        raise B2BError("invalid", f"reason_code must be one of {', '.join(REASONS)}")
    try:
        ident = str(uuid.UUID(booking_id))
    except ValueError:
        raise B2BError("not_found", "booking not found") from None
    now = now or datetime.now(UTC)
    async with pool.acquire() as conn, conn.transaction():
        booking = await conn.fetchrow(BOOKING_SQL + ("" if dry_run else " FOR UPDATE OF h"), ident, partner_id)
        if booking is None:
            raise B2BError("not_found", "booking not found")
        stored = await conn.fetchrow(STORED_SQL, booking["hold_id"])
        if stored is not None:
            return _stored(ident, stored)
        # AA-682 (critic B2): once supplier bookings move the case along the supply track it is still the same
        # confirmed booking for the agency, so supplier_confirming and pre_departure cancel too.
        if booking["hold_status"] != "confirmed" or booking["status"] not in CANCELLABLE_CASE_STATUSES:
            raise B2BError("invalid", f"the booking is {booking['status']}; only a confirmed booking can be cancelled")
        if booking["start_date"] is None:
            raise B2BError("unavailable", "the booking has no departure date")
        quote = await conn.fetchrow(QUOTE_SQL, booking["quote_id"])
        policy = await _booking_policy(conn, booking)
        currency = quote["currency"].strip()
        fee = compute(
            policy.tiers,
            total=Decimal(quote["total_b2b_price"]),
            currency=currency,
            departure=booking["start_date"],
            today=now.astimezone(UTC).date(),
        )
        if await conn.fetchval(AA_CANCELLED_SQL, booking["hold_id"]):
            # AA cancelled a departure of this booking: no fee, whatever reason_code the agency sends.
            fee = Fee(
                days_before=fee.days_before, fee_pct=Decimal("0"), fee=Decimal("0"), refundable=fee.fee + fee.refundable
            )
        if dry_run:
            return _result(ident, dry_run=True, policy_id=policy.id, fee=fee, currency=currency, cancelled_at=None)
        await conn.execute(
            INSERT_SQL,
            booking["hold_id"],
            ident,
            partner_id,
            client_id,
            policy.id,
            reason_code,
            idempotency_key,
            booking["start_date"],
            fee.days_before,
            fee.fee_pct,
            currency,
            Decimal(quote["total_b2b_price"]),
            fee.fee,
            fee.refundable,
            now,
        )
        await conn.execute(
            "UPDATE booking.b2b_hold_lines SET status = 'cancelled' WHERE hold_id = $1::uuid"
            " AND status IN ('available', 'on_request', 'confirmed')",
            booking["hold_id"],
        )
        actor = f"m2m:{client_id}"
        await conn.execute("SELECT set_config('booking.actor', '', true)")
        await conn.execute(
            "SELECT set_config('booking.status_note', $1, true)",
            f"B2B cancel by {actor} ({reason_code}); fee {fee.fee_pct}% = {fee.fee} {currency}"[:500],
        )
        await conn.execute("UPDATE booking.trip_cases SET status = 'cancelled' WHERE id = $1::uuid", ident)
        # AA-682: pending supplier bookings are cancelled; requested / confirmed ones raise "cancel with supplier".
        cascade = await supplier_bookings.on_case_cancelled(
            conn, case_id=ident, case_code=booking["code"], cause=f"agency cancel ({reason_code})"
        )
        await audit.record(
            conn,
            actor_type="system",
            actor_ref=actor,
            action="b2b_booking.cancelled",
            entity_type="trip_case",
            entity_id=ident,
            before={"status": booking["status"]},
            after={
                "status": "cancelled",
                "hold_id": booking["hold_id"],
                "supplier_bookings": cascade,
                "reason_code": reason_code,
                "policy_id": policy.id,
                "days_before_departure": fee.days_before,
                "fee_pct": fee.fee_pct,
                "fee": fee.fee,
                "currency": currency,
                "idempotency_key": idempotency_key,
            },
            entity_label=booking["code"],
            summary=f"B2B booking {booking['code']} cancelled ({reason_code}), fee {fee.fee} {currency}",
        )
    log.info("b2b_booking_cancelled trip_case=%s partner=%s fee_pct=%s", booking["code"], partner_id, fee.fee_pct)
    return _result(ident, dry_run=False, policy_id=policy.id, fee=fee, currency=currency, cancelled_at=now)


# ── Staff admin (/booking/pricing/cancellation-policies) ─────────────────────

MAX_ROWS = pricing_policy.MAX_ROWS
LIST_SQL = f"""
WITH in_force AS (
    SELECT DISTINCT ON (p.channel, p.trip_plan_id) p.id
    FROM booking.cancellation_policies p
    JOIN booking.policy_approvals ap ON ap.cancellation_policy_id = p.id
    WHERE p.valid_from <= $1
    ORDER BY p.channel, p.trip_plan_id, p.valid_from DESC
)
SELECT p.id::text AS id, p.channel, p.trip_plan_id::text AS trip_plan_id, tp.source_id, p.valid_from, p.title,
       p.tiers::text AS tiers, p.note, p.created_at, p.created_by, cs.name AS created_by_name,
       a.approved_by, aps.name AS approved_by_name, a.approved_at,
       {pricing_policy._STATUS.format(day="valid_from")} AS status
FROM booking.cancellation_policies p
LEFT JOIN booking.trip_plans tp ON tp.id = p.trip_plan_id
LEFT JOIN booking.policy_approvals a ON a.cancellation_policy_id = p.id
LEFT JOIN booking.staff_users cs ON cs.cis_user_id = p.created_by
LEFT JOIN booking.staff_users aps ON aps.cis_user_id = a.approved_by
WHERE ($2::text IS NULL OR tp.source_id = $2) AND ($3::uuid IS NULL OR p.id = $3)
ORDER BY p.valid_from DESC, p.trip_plan_id NULLS FIRST, p.created_at DESC
LIMIT $4
"""  # noqa: S608 — constant fragments only


def _row_out(row: Any, staff: Staff) -> dict[str, Any]:
    return {
        **pricing_policy._common(row, staff),
        "channel": row["channel"],
        "trip_plan_id": row["trip_plan_id"],
        "source_id": row["source_id"],
        "valid_from": row["valid_from"],
        "title": row["title"],
        "tiers": tiers_out(parse_tiers(row["tiers"])),
        "note": row["note"],
    }


async def list_policies(
    conn: asyncpg.Connection, staff: Staff, *, today: date, source_id: str | None = None, limit: int = MAX_ROWS
) -> list[dict[str, Any]]:
    rows = await conn.fetch(LIST_SQL, today, source_id, None, min(limit, MAX_ROWS))
    return [_row_out(r, staff) for r in rows]


async def _by_id(conn: asyncpg.Connection, staff: Staff, policy_id: str, today: date) -> dict[str, Any]:
    row = await conn.fetchrow(LIST_SQL, today, None, policy_id, 1)
    if row is None:
        raise NotFound("cancellation_policy_not_found", "no cancellation policy with this id")
    return _row_out(row, staff)


def _label(row: dict[str, Any]) -> str:
    scope = row["source_id"] or "default"
    return f"{scope[:60]} from {row['valid_from']}"


async def create_policy(conn: asyncpg.Connection, staff: Staff, data: dict[str, Any], *, today: date) -> dict[str, Any]:
    """A new pending policy row. ``data``: title, source_id (None = the B2B default), valid_from, tiers, note."""
    try:
        tiers = parse_tiers(data["tiers"])
    except TierError as exc:
        raise Invalid("invalid_tiers", str(exc)) from None
    plan_id = None
    if data.get("source_id"):
        plan_id = await conn.fetchval(
            "SELECT id::text FROM booking.trip_plans WHERE source_system = 'cis' AND source_id = $1", data["source_id"]
        )
        if plan_id is None:
            raise NotFound("trip_plan_not_found", "save the commercial row of this tour first")
    title = (data.get("title") or "").strip()
    if not title:
        raise Invalid("title_required", "a cancellation policy needs a title")
    note = (data.get("note") or "").strip() or None
    try:
        policy_id = await conn.fetchval(
            "INSERT INTO booking.cancellation_policies (channel, trip_plan_id, valid_from, title, tiers, note,"
            " created_by) VALUES ($1, $2::uuid, $3, $4, $5::jsonb, $6, $7) RETURNING id::text",
            CHANNEL,
            plan_id,
            data["valid_from"],
            title,
            tiers_json(tiers),
            note,
            staff.cis_user_id,
        )
    except asyncpg.UniqueViolationError:
        raise Conflict(
            "cancellation_policy_exists", f"a policy for this scope dated {data['valid_from']} exists; use another date"
        ) from None
    row = await _by_id(conn, staff, policy_id, today)
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="cancellation_policy.created",
        entity_type="cancellation_policy",
        entity_id=policy_id,
        after={k: row[k] for k in ("source_id", "valid_from", "title", "tiers", "note")},
        entity_label=_label(row),
        summary=f"New cancellation policy {_label(row)}: "
        + ", ".join(f"{t['min_days']}+ d {t['fee_pct']}%" for t in row["tiers"])
        + " (pending approval)",
    )
    return row


async def approve_policy(conn: asyncpg.Connection, staff: Staff, policy_id: str, *, today: date) -> dict[str, Any]:
    await pricing_policy.approve_row(conn, staff, "cancellation_policy", policy_id)
    row = await _by_id(conn, staff, policy_id, today)
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="cancellation_policy.approved",
        entity_type="cancellation_policy",
        entity_id=policy_id,
        before={"status": "pending"},
        after={"status": row["status"], "created_by": (row["created_by"] or {}).get("id")},
        entity_label=_label(row),
        summary=f"Approved cancellation policy {_label(row)} ({row['status'].replace('_', ' ')})",
    )
    return row
