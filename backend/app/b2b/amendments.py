"""Changes to a confirmed B2B booking after the sale (AA-1075 bk-14, bk-047; S275 study item bk-14).

- **Staff cancel** (:func:`staff_cancel`, ``POST /booking/b2b-desk/bookings/{ref}/cancel``, ``b2b_desk.book``): the
  agency asked by e-mail; the same cancel as the agency's ``POST /bookings/{id}/cancel`` (``app.b2b.cancellation``:
  the locked policy, the dry run, the seats back on sale, the case cancelled, the supplier cascade, the
  ``booking.cancelled`` event), with ``entered_via = 'staff'`` on the row. Staff may ask for a lower fee in the same
  call (an override waiting for approval, below).
- **Fee override / waive** (maker-checker): :func:`request_override` stores a lower fee % (0 = waive) and the reason on
  the cancellation; the fee does not change yet. :func:`approve_override` (``b2b_desk.approve``, never the requester:
  403 ``maker_checker``, CHECK) applies it: fee = the cancelled total x % half-up to the currency unit, refundable =
  the rest; the agency gets ``booking.fee_changed``. :func:`reject_override` clears a pending request. An approved
  override is final (guard trigger).
- **Partial cancellation** (:func:`reduce_line`, agency ``POST /booking/b2b/v1/bookings/{id}/lines/{ref}/reduce`` and
  staff ``POST /booking/b2b-desk/bookings/{ref}/lines/{line_ref}/reduce``): fewer travellers on one per-person line
  (a tour, an activity, an entrance or a meal; rooms, transfers and guides are priced per room or vehicle, so those
  are re-quoted instead: 422 ``line_not_reducible``). At least one adult stays (removing everyone = cancel the
  booking). The removed share of the line price = the line's current price x removed seats / current seats (infants
  hold no seat and cost nothing to remove), half-up to the currency unit; the fee = that share x the policy % of
  today (the policy locked on the booking; no fee on a departure AA cancelled); refundable = the rest. The seats go
  back on sale at once (the hold line's seats), the case pax follows the biggest line, staff are told to update live
  supplier bookings, and the agency gets ``booking.line_reduced``. ``dry_run`` reads only. Idempotent on
  ``(booking, Idempotency-Key)``. The approved version stays as sold (bk-003 locks it): the reduction rows are the
  amendment record, and the settlement draft and the profit view read them.

No personal data: travellers are counts by age band.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import asyncpg

from app import audit
from app.auth.staff import Staff
from app.b2b import cancellation
from app.b2b.desk_bookings import staff_error
from app.b2b.errors import B2BError
from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.notifications import service as notifications

log = logging.getLogger(__name__)

HUNDRED = Decimal("100")
REDUCE_REASONS = ("client_cancelled", "agency_cancelled", "other")
REDUCIBLE_KINDS = frozenset({"trip_plan", "activity", "entrance", "meal"})
LIVE_LINE_STATUSES = frozenset({"available", "on_request", "confirmed"})
NOTIFY_REDUCED = "b2b_booking.travellers_reduced"
NOTIFY_OVERRIDE = "b2b_cancellation.override_pending"

# Staff: a confirmed B2B booking by trip case id or code, with its agency.
STAFF_BOOKING_SQL = """
SELECT c.id::text AS trip_case_id, c.code, c.status, h.id::text AS hold_id, h.partner_id::text AS partner_id,
       p.m2m_client_id, p.agency_name
FROM booking.trip_cases c
JOIN booking.b2b_holds h ON h.trip_case_id = c.id AND h.status = 'confirmed'
JOIN booking.b2b_partners p ON p.id = h.partner_id
WHERE c.channel = 'b2b' AND c.deleted_at IS NULL
"""
CANCELLATION_SQL = """
SELECT x.id::text AS id, x.hold_id::text AS hold_id, x.trip_case_id::text AS trip_case_id, x.currency::text AS currency,
       x.total_b2b_price, x.fee_pct, x.fee_amount, x.refundable_amount, x.fee_override_pct, x.waive_reason,
       x.override_by, x.override_at, x.override_approved_by, x.override_approved_at, x.entered_via, x.entered_by,
       x.reason_code, x.cancelled_at, ob.name AS override_by_name, ab.name AS override_approved_by_name
FROM booking.b2b_cancellations x
LEFT JOIN booking.staff_users ob ON ob.cis_user_id = x.override_by
LEFT JOIN booking.staff_users ab ON ab.cis_user_id = x.override_approved_by
WHERE x.trip_case_id = $1::uuid
"""
# One line of a confirmed booking with what partial cancellations already removed from it.
LINE_SQL = """
SELECT h.id::text AS hold_id, h.quote_id::text AS quote_id, h.confirmed_at,
       h.cancellation_policy_id::text AS cancellation_policy_id, c.id::text AS trip_case_id, c.code, c.status,
       c.start_date, ql.line_no, ql.line_ref, ql.component_kind, ql.pax_adult, ql.pax_child, ql.pax_infant,
       ql.b2b_price, q.currency::text AS currency, hl.status AS line_status, hl.departure_id::text AS departure_id,
       coalesce(r.adults, 0) AS removed_adults, coalesce(r.children, 0) AS removed_children,
       coalesce(r.infants, 0) AS removed_infants, coalesce(r.amount, 0) AS removed_amount
FROM booking.b2b_holds h
JOIN booking.trip_cases c ON c.id = h.trip_case_id
JOIN booking.b2b_quotes q ON q.id = h.quote_id
JOIN booking.b2b_quote_lines ql ON ql.quote_id = h.quote_id AND ql.line_ref = $3
JOIN booking.b2b_hold_lines hl ON hl.hold_id = h.id AND hl.line_ref = ql.line_ref
LEFT JOIN (
    SELECT hold_id, line_ref, sum(removed_adults) AS adults, sum(removed_children) AS children,
           sum(removed_infants) AS infants, sum(removed_amount) AS amount
    FROM booking.b2b_line_reductions GROUP BY hold_id, line_ref
) r ON r.hold_id = h.id AND r.line_ref = ql.line_ref
WHERE h.trip_case_id = $1::uuid AND h.partner_id = $2::uuid AND h.status = 'confirmed'
"""
STORED_REDUCTION_SQL = """
SELECT line_ref, removed_adults, removed_children, removed_infants, seats_after, policy_id::text AS policy_id,
       days_before_departure, fee_pct, currency::text AS currency, removed_amount, fee_amount, refundable_amount,
       created_at
FROM booking.b2b_line_reductions WHERE hold_id = $1::uuid AND idempotency_key = $2
"""
# The travellers of every line after partial cancellations: the case pax follows the biggest line.
CURRENT_PAX_SQL = """
SELECT ql.pax_adult - coalesce(sum(r.removed_adults), 0) AS adults,
       ql.pax_child - coalesce(sum(r.removed_children), 0) AS children
FROM booking.b2b_quote_lines ql
LEFT JOIN booking.b2b_line_reductions r ON r.hold_id = $2::uuid AND r.line_ref = ql.line_ref
WHERE ql.quote_id = $1::uuid
GROUP BY ql.line_no, ql.pax_adult, ql.pax_child
"""
HOLD_READ_SQL = (
    "SELECT id::text AS id FROM booking.b2b_holds WHERE trip_case_id = $1::uuid AND partner_id = $2::uuid"
    " AND status = 'confirmed'"
)
HOLD_LOCK_SQL = HOLD_READ_SQL + " FOR UPDATE"
AA_CANCELLED_SQL = """
SELECT EXISTS (SELECT 1 FROM booking.departures d WHERE d.id = $1::uuid AND d.status = 'cancelled')
"""
LIVE_SUPPLIER_SQL = (
    "SELECT code FROM booking.supplier_bookings WHERE trip_case_id = $1::uuid"
    " AND status IN ('pending', 'requested', 'confirmed') ORDER BY code"
)


def _uuid(value: str) -> str | None:
    try:
        return str(uuid.UUID(value))
    except (TypeError, ValueError):
        return None


def _money(amount: Decimal, currency: str) -> dict[str, str]:
    return cancellation.money(Decimal(amount), currency)


async def staff_booking(conn: asyncpg.Connection, ref: str, *, lock: bool = False) -> Any:
    """A confirmed B2B booking for staff by trip case id or code (404 ``booking_not_found``)."""
    ident = _uuid(ref)
    where, arg = (" AND c.id = $1::uuid", ident) if ident else (" AND c.code = $1", ref)
    row = await conn.fetchrow(STAFF_BOOKING_SQL + where + (" FOR UPDATE OF h" if lock else ""), arg)
    if row is None:
        raise NotFound("booking_not_found", f"no confirmed agency booking {ref}")
    return row


# ── Cancellation and its fee override ────────────────────────────────────────


def cancellation_out(row: Any, staff: Staff) -> dict[str, Any]:
    currency = row["currency"].strip()
    total = Decimal(row["total_b2b_price"])
    policy_fee = cancellation.compute_fee(total, Decimal(row["fee_pct"]), currency)
    pending = row["override_by"] is not None and row["override_approved_by"] is None
    return {
        "id": row["id"],
        "reason_code": row["reason_code"],
        "cancelled_at": row["cancelled_at"],
        "entered_via": row["entered_via"],
        "total": _money(total, currency),
        "fee_pct": format(Decimal(row["fee_pct"]).quantize(Decimal("0.01")), "f"),
        "policy_fee": _money(policy_fee, currency),
        "fee": _money(Decimal(row["fee_amount"]), currency),
        "refundable": _money(Decimal(row["refundable_amount"]), currency),
        "override": None
        if row["override_by"] is None
        else {
            "fee_pct": format(Decimal(row["fee_override_pct"]).quantize(Decimal("0.01")), "f"),
            "reason": row["waive_reason"],
            "requested_by": {"id": row["override_by"], "name": row["override_by_name"]},
            "requested_at": row["override_at"],
            "approved_by": {"id": row["override_approved_by"], "name": row["override_approved_by_name"]}
            if row["override_approved_by"]
            else None,
            "approved_at": row["override_approved_at"],
            "status": "pending" if pending else "approved",
        },
        "can_approve_override": pending and staff.can("b2b_desk.approve") and staff.cis_user_id != row["override_by"],
    }


async def get_cancellation(conn: asyncpg.Connection, staff: Staff, ref: str) -> dict[str, Any] | None:
    booking = await staff_booking(conn, ref)
    row = await conn.fetchrow(CANCELLATION_SQL, booking["trip_case_id"])
    return cancellation_out(row, staff) if row else None


async def staff_cancel(
    pool: asyncpg.Pool,
    staff: Staff,
    ref: str,
    *,
    reason_code: str,
    dry_run: bool,
    idempotency_key: str,
    fee_override_pct: Decimal | None = None,
    waive_reason: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Cancel a confirmed agency booking for the agency (or preview it); with ``fee_override_pct`` a lower fee is
    requested in the same transaction (it waits for a second approver)."""
    now = now or datetime.now(UTC)
    async with pool.acquire() as conn, conn.transaction():
        booking = await staff_booking(conn, ref)
        try:
            result = await cancellation.cancel_in(
                conn,
                partner_id=booking["partner_id"],
                client_id=booking["m2m_client_id"],
                booking_id=booking["trip_case_id"],
                reason_code=reason_code,
                dry_run=dry_run,
                idempotency_key=idempotency_key,
                now=now,
                entered_by=staff.cis_user_id,
            )
        except B2BError as exc:
            raise staff_error(exc) from None
        out: dict[str, Any] = {**result, "trip_case_code": booking["code"], "cancellation": None}
        if not dry_run:
            if fee_override_pct is not None:
                await _request(conn, staff, booking, fee_override_pct, waive_reason)
            row = await conn.fetchrow(CANCELLATION_SQL, booking["trip_case_id"])
            out["cancellation"] = cancellation_out(row, staff) if row else None
    return out


async def _locked_cancellation(conn: asyncpg.Connection, booking: Any) -> Any:
    row = await conn.fetchrow(CANCELLATION_SQL + " FOR UPDATE OF x", booking["trip_case_id"])
    if row is None:
        raise Conflict("not_cancelled", f"{booking['code']} is not cancelled; cancel it first")
    return row


async def _request(conn: asyncpg.Connection, staff: Staff, booking: Any, pct: Decimal, reason: str | None) -> None:
    reason = (reason or "").strip()
    if not reason:
        raise Invalid("waive_reason_required", "say why the fee is lowered or waived")
    if len(reason) > 500:
        raise Invalid("waive_reason_too_long", "the reason is at most 500 characters")
    row = await _locked_cancellation(conn, booking)
    if row["override_approved_by"] is not None:
        raise Conflict("override_final", "the fee override of this booking is already approved")
    if row["override_by"] is not None:
        if (
            row["override_by"] == staff.cis_user_id
            and Decimal(row["fee_override_pct"]) == pct
            and row["waive_reason"] == reason
        ):
            return  # the same request again (a replayed cancel, a double click): nothing changes
        # One open request at a time: the approver must decide the value they saw, so a new value is only asked
        # after a colleague rejects the pending one (maker-checker).
        raise Conflict(
            "override_pending",
            f"a lower fee of {row['fee_override_pct']} % already waits for approval; have it rejected first",
        )
    if not Decimal(0) <= pct < Decimal(row["fee_pct"]):
        raise Invalid(
            "invalid_override",
            f"the new fee % must be at least 0 and below the policy fee ({row['fee_pct']} %)",
        )
    await conn.execute(
        "UPDATE booking.b2b_cancellations SET fee_override_pct = $2, waive_reason = $3, override_by = $4,"
        " override_at = now() WHERE id = $1::uuid",
        row["id"],
        pct.quantize(Decimal("0.01")),
        reason,
        staff.cis_user_id,
    )
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="b2b_cancellation.override_requested",
        entity_type="trip_case",
        entity_id=booking["trip_case_id"],
        before={"fee_pct": row["fee_pct"], "fee_amount": row["fee_amount"]},
        after={"fee_override_pct": pct, "reason": reason},
        entity_label=booking["code"],
        summary=f"Asked to lower the cancellation fee of {booking['code']} to {pct} %",
    )
    await notifications.notify_staff(
        conn,
        type=NOTIFY_OVERRIDE,
        title=f"{booking['code']}: lower cancellation fee to approve",
        message=f"{booking['agency_name']}: fee {row['fee_pct']} % -> {pct} %.",
        entity_type="trip_case",
        entity_id=booking["trip_case_id"],
        data={"code": booking["code"]},
    )


async def request_override(
    pool: asyncpg.Pool, staff: Staff, ref: str, *, fee_override_pct: Decimal, waive_reason: str
) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        booking = await staff_booking(conn, ref)
        await _request(conn, staff, booking, fee_override_pct, waive_reason)
        return cancellation_out(await conn.fetchrow(CANCELLATION_SQL, booking["trip_case_id"]), staff)


async def approve_override(
    pool: asyncpg.Pool, staff: Staff, ref: str, *, expected_fee_override_pct: Decimal
) -> dict[str, Any]:
    """Approve the pending lower fee. ``expected_fee_override_pct`` is the value the approver saw: a stored value
    that differs (asked again in between) is refused, so the second person never approves a value they did not see."""
    async with pool.acquire() as conn, conn.transaction():
        booking = await staff_booking(conn, ref)
        row = await _locked_cancellation(conn, booking)
        if row["override_by"] is None:
            raise Conflict("no_override", "no lower fee was asked for on this cancellation")
        if Decimal(row["fee_override_pct"]) != expected_fee_override_pct:
            raise Conflict(
                "override_changed",
                f"the lower fee asked for is now {row['fee_override_pct']} %, not {expected_fee_override_pct} %;"
                " reload and decide again",
            )
        if row["override_approved_by"] is not None:
            return cancellation_out(row, staff)
        if row["override_by"] == staff.cis_user_id:
            raise Forbidden("maker_checker", "the person who asked for the lower fee cannot approve it")
        currency = row["currency"].strip()
        total = Decimal(row["total_b2b_price"])
        fee = cancellation.compute_fee(total, Decimal(row["fee_override_pct"]), currency)
        await conn.execute(
            "UPDATE booking.b2b_cancellations SET override_approved_by = $2, override_approved_at = now(),"
            " fee_amount = $3, refundable_amount = $4 WHERE id = $1::uuid",
            row["id"],
            staff.cis_user_id,
            fee,
            total - fee,
        )
        await audit.record(
            conn,
            actor_type="staff",
            actor_ref=staff.cis_user_id,
            action="b2b_cancellation.override_approved",
            entity_type="trip_case",
            entity_id=booking["trip_case_id"],
            before={"fee_amount": row["fee_amount"]},
            after={"fee_amount": fee, "fee_override_pct": row["fee_override_pct"]},
            entity_label=booking["code"],
            summary=f"Approved the lower cancellation fee of {booking['code']}: {fee} {currency}",
        )
        return cancellation_out(await conn.fetchrow(CANCELLATION_SQL, booking["trip_case_id"]), staff)


async def reject_override(pool: asyncpg.Pool, staff: Staff, ref: str) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        booking = await staff_booking(conn, ref)
        row = await _locked_cancellation(conn, booking)
        if row["override_approved_by"] is not None:
            raise Conflict("override_final", "the fee override of this booking is already approved")
        if row["override_by"] is None:
            return cancellation_out(row, staff)
        if row["override_by"] == staff.cis_user_id:
            raise Forbidden("maker_checker", "the person who asked for the lower fee cannot reject it; ask a colleague")
        await conn.execute(
            "UPDATE booking.b2b_cancellations SET fee_override_pct = NULL, waive_reason = NULL, override_by = NULL,"
            " override_at = NULL WHERE id = $1::uuid",
            row["id"],
        )
        await audit.record(
            conn,
            actor_type="staff",
            actor_ref=staff.cis_user_id,
            action="b2b_cancellation.override_rejected",
            entity_type="trip_case",
            entity_id=booking["trip_case_id"],
            before={"fee_override_pct": row["fee_override_pct"], "requested_by": row["override_by"]},
            entity_label=booking["code"],
            summary=f"Rejected the lower cancellation fee of {booking['code']}; the policy fee stays",
        )
        return cancellation_out(await conn.fetchrow(CANCELLATION_SQL, booking["trip_case_id"]), staff)


# ── Partial cancellation ─────────────────────────────────────────────────────


def _reduction_out(booking_id: str, *, dry_run: bool, data: dict[str, Any]) -> dict[str, Any]:
    currency = data["currency"]
    return {
        "booking_id": booking_id,
        "line_ref": data["line_ref"],
        "dry_run": dry_run,
        "removed": {
            "adult": int(data["removed_adults"]),
            "child": int(data["removed_children"]),
            "infant": int(data["removed_infants"]),
        },
        "seats_after": int(data["seats_after"]),
        "removed_b2b_amount": _money(Decimal(data["removed_amount"]), currency),
        "fee": _money(Decimal(data["fee_amount"]), currency),
        "refundable_b2b_amount": _money(Decimal(data["refundable_amount"]), currency),
        "fee_basis": {
            "policy_id": data["policy_id"],
            "days_before_departure": int(data["days_before_departure"]),
            "fee_pct": format(Decimal(data["fee_pct"]).quantize(Decimal("0.01")), "f"),
        },
        "reduced_at": None if dry_run else data["created_at"].isoformat(),
    }


def removed_share(price: Decimal, current_seats: int, removed_seats: int, currency: str) -> Decimal:
    """The line price share of the removed seats, half-up to the currency unit (never above the price)."""
    if current_seats <= 0 or removed_seats <= 0:
        return Decimal("0")
    share = (price * removed_seats / current_seats).quantize(cancellation.unit(currency), rounding=ROUND_HALF_UP)
    return min(share, price)


async def reduce_line(
    conn: asyncpg.Connection,
    *,
    partner_id: str,
    booking_id: str,
    line_ref: str,
    adults: int,
    children: int,
    infants: int,
    reason_code: str,
    dry_run: bool,
    idempotency_key: str,
    now: datetime,
    entered_by: str | None = None,
) -> dict[str, Any]:
    """Remove travellers from one line of a confirmed booking (call in a transaction); the ``LineReduction`` body.
    Raises B2BError (the agency API's codes; the staff route converts them)."""
    if reason_code not in REDUCE_REASONS:
        raise B2BError("invalid", f"reason_code must be one of {', '.join(REDUCE_REASONS)}")
    if min(adults, children, infants) < 0 or adults + children + infants < 1:
        raise B2BError("invalid", "remove at least one traveller (adult, child or infant counts, none negative)")
    ident = _uuid(booking_id)
    if ident is None:
        raise B2BError("not_found", "booking not found")
    hold_row = await conn.fetchrow(
        HOLD_READ_SQL if dry_run else HOLD_LOCK_SQL,
        ident,
        partner_id,
    )
    if hold_row is None:
        raise B2BError("not_found", "booking not found")
    stored = await conn.fetchrow(STORED_REDUCTION_SQL, hold_row["id"], idempotency_key)
    if stored is not None:
        if stored["line_ref"] != line_ref:
            raise B2BError("invalid", "this Idempotency-Key was already used for another line")
        return _reduction_out(ident, dry_run=False, data=dict(stored))
    line = await conn.fetchrow(LINE_SQL, ident, partner_id, line_ref)
    if line is None:
        raise B2BError("not_found", f"line {line_ref} not found on this booking")
    if line["status"] not in cancellation.CANCELLABLE_CASE_STATUSES:
        raise B2BError("invalid", f"the booking is {line['status']}; only a confirmed booking changes")
    if line["line_status"] not in LIVE_LINE_STATUSES:
        raise B2BError("invalid", f"line {line_ref} is {line['line_status']}")
    if line["component_kind"] not in REDUCIBLE_KINDS:
        raise B2BError(
            "invalid",
            f"line {line_ref} is priced per room or vehicle ({line['component_kind']}); re-quote it instead",
            reason="line_not_reducible",
        )
    if line["start_date"] is None:
        raise B2BError("unavailable", "the booking has no departure date")
    cur_adults = int(line["pax_adult"]) - int(line["removed_adults"])
    cur_children = int(line["pax_child"]) - int(line["removed_children"])
    cur_infants = int(line["pax_infant"]) - int(line["removed_infants"])
    if children > cur_children or infants > cur_infants or adults > cur_adults:
        raise B2BError(
            "invalid",
            f"line {line_ref} has {cur_adults} adult(s), {cur_children} child(ren), {cur_infants} infant(s)",
        )
    if cur_adults - adults < 1:
        raise B2BError("invalid", "at least one adult stays on the line; cancel the booking to remove everyone")
    currency = line["currency"].strip()
    seats_before = cur_adults + cur_children
    removed_seats = adults + children
    price = Decimal(line["b2b_price"]) - Decimal(line["removed_amount"])
    removed = removed_share(price, seats_before, removed_seats, currency)
    policy = await cancellation.policy_of_booking(
        conn, policy_id=line["cancellation_policy_id"], quote_id=line["quote_id"], confirmed_at=line["confirmed_at"]
    )
    if policy is None:
        raise B2BError("unavailable", "no cancellation policy is in force for this booking")
    fee = cancellation.compute(
        policy.tiers, total=removed, currency=currency, departure=line["start_date"], today=now.astimezone(UTC).date()
    )
    if line["departure_id"] and await conn.fetchval(AA_CANCELLED_SQL, line["departure_id"]):
        fee = cancellation.Fee(
            days_before=fee.days_before, fee_pct=Decimal("0"), fee=Decimal("0"), refundable=fee.fee + fee.refundable
        )
    data = {
        "line_ref": line_ref,
        "removed_adults": adults,
        "removed_children": children,
        "removed_infants": infants,
        "seats_after": seats_before - removed_seats,
        "policy_id": policy.id,
        "days_before_departure": fee.days_before,
        "fee_pct": fee.fee_pct,
        "currency": currency,
        "removed_amount": removed,
        "fee_amount": fee.fee,
        "refundable_amount": fee.refundable,
        "created_at": now,
    }
    if dry_run:
        return _reduction_out(ident, dry_run=True, data=data)
    await conn.execute(
        "INSERT INTO booking.b2b_line_reductions (hold_id, trip_case_id, partner_id, line_ref, removed_adults,"
        " removed_children, removed_infants, seats_before, seats_after, policy_id, days_before_departure, fee_pct,"
        " currency, removed_amount, fee_amount, refundable_amount, reason_code, idempotency_key, entered_via,"
        " entered_by, created_at) VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5, $6, $7, $8, $9, $10::uuid, $11, $12,"
        " $13, $14, $15, $16, $17, $18, $19, $20, $21)",
        line["hold_id"],
        ident,
        partner_id,
        line_ref,
        adults,
        children,
        infants,
        seats_before,
        seats_before - removed_seats,
        policy.id,
        fee.days_before,
        fee.fee_pct,
        currency,
        removed,
        fee.fee,
        fee.refundable,
        reason_code,
        idempotency_key,
        "staff" if entered_by else "api",
        entered_by,
        now,
    )
    if line["departure_id"] and removed_seats:
        # The seats go back on sale at once (held + sold are counted from the hold lines).
        await conn.execute(
            "UPDATE booking.b2b_hold_lines SET seats = greatest(seats - $3, 0) WHERE hold_id = $1::uuid"
            " AND line_ref = $2",
            line["hold_id"],
            line_ref,
            removed_seats,
        )
    pax = [
        (int(r["adults"]), int(r["children"]))
        for r in await conn.fetch(CURRENT_PAX_SQL, line["quote_id"], line["hold_id"])
    ]
    big_adults, big_children = max(pax, key=lambda p: (p[0] + p[1], p[0]))
    await conn.execute(
        "UPDATE booking.trip_cases SET pax = $2, pax_adults = $3, pax_children = $4 WHERE id = $1::uuid",
        ident,
        big_adults + big_children,
        big_adults,
        big_children,
    )
    live = [r["code"] for r in await conn.fetch(LIVE_SUPPLIER_SQL, ident)]
    if live:
        await notifications.notify_staff(
            conn,
            type=NOTIFY_REDUCED,
            title=f"{line['code']}: fewer travellers on line {line_ref}; update the supplier bookings",
            message=f"Removed {adults} adult(s), {children} child(ren), {infants} infant(s). Live: {', '.join(live)}.",
            entity_type="trip_case",
            entity_id=ident,
            data={"code": line["code"], "line_ref": line_ref, "supplier_bookings": live},
        )
    await audit.record(
        conn,
        actor_type="staff" if entered_by else "system",
        actor_ref=entered_by or "m2m:partner",
        action="b2b_booking.line_reduced",
        entity_type="trip_case",
        entity_id=ident,
        before={"adults": cur_adults, "children": cur_children, "infants": cur_infants},
        after={
            "line_ref": line_ref,
            "removed": {"adult": adults, "child": children, "infant": infants},
            "reason_code": reason_code,
            "removed_amount": removed,
            "fee": fee.fee,
            "currency": currency,
            "idempotency_key": idempotency_key,
        },
        entity_label=line["code"],
        summary=f"{line['code']} line {line_ref}: {removed_seats + infants} traveller(s) removed, fee {fee.fee}"
        f" {currency}" + (" (entered by AA staff)" if entered_by else ""),
    )
    log.info("b2b_line_reduced trip_case=%s line=%s seats=%s", line["code"], line_ref, removed_seats)
    return _reduction_out(ident, dry_run=False, data=data)


async def staff_reduce(
    pool: asyncpg.Pool,
    staff: Staff,
    ref: str,
    line_ref: str,
    *,
    adults: int,
    children: int,
    infants: int,
    reason_code: str,
    dry_run: bool,
    idempotency_key: str,
) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        booking = await staff_booking(conn, ref)
        try:
            return await reduce_line(
                conn,
                partner_id=booking["partner_id"],
                booking_id=booking["trip_case_id"],
                line_ref=line_ref,
                adults=adults,
                children=children,
                infants=infants,
                reason_code=reason_code,
                dry_run=dry_run,
                idempotency_key=idempotency_key,
                now=datetime.now(UTC),
                entered_by=staff.cis_user_id,
            )
        except B2BError as exc:
            raise staff_error(exc) from None


async def list_reductions(conn: asyncpg.Connection, ref: str) -> list[dict[str, Any]]:
    booking = await staff_booking(conn, ref)
    rows = await conn.fetch(
        "SELECT r.line_ref, r.removed_adults, r.removed_children, r.removed_infants, r.seats_after,"
        " r.policy_id::text AS policy_id, r.days_before_departure, r.fee_pct, r.currency::text AS currency,"
        " r.removed_amount, r.fee_amount, r.refundable_amount, r.created_at, r.entered_via, r.reason_code"
        " FROM booking.b2b_line_reductions r WHERE r.hold_id = $1::uuid ORDER BY r.created_at",
        booking["hold_id"],
    )
    return [
        {
            **_reduction_out(booking["trip_case_id"], dry_run=False, data=dict(r)),
            "entered_via": r["entered_via"],
            "reason_code": r["reason_code"],
        }
        for r in rows
    ]
