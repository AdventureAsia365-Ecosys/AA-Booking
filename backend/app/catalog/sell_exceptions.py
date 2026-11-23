"""Staff side of the can-operate check (AA-1149 B13, bk-065): read a tour's check, the admin sale exception, and the
nights that need no hotel. The rules themselves live in :mod:`app.catalog.operability`.

- :func:`operability_of` (``pricing.read``): the tour's status, every finding (covered ones marked), its live exception,
  its ``no_hotel_nights``, its days and the policy's default and maximum exception length.
- :func:`create_exception` (``operable_exception.approve``: admin only, an approve-class capability no custom role can
  hold): the chosen rules must have overridable findings now (N1 nights without a hotel, N3 suppliers not active;
  422 ``rule_not_failing`` otherwise; a missing rate or an inactive room type, hotel or service is never overridable);
  the exception covers exactly those findings (a later new finding blocks again). Reason 20..500 characters (422
  ``reason_too_short`` / ``reason_too_long``), length in days 1 .. ``operable_exception_max_days`` (default
  ``operable_exception_default_days``; 422 ``expiry_too_long``). A previous unrevoked exception of the tour (live or
  expired) is revoked in the same transaction, under the plan row lock. One audit row each.
- :func:`revoke_exception` (same capability): ends the live exception now with a reason (409 ``no_exception``).
- :func:`set_no_hotel_nights` (``trip_plan.write``): the nights 1 .. days - 1 that need no hotel (422
  ``invalid_night``); tour data, audited.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import asyncpg

from app import audit
from app.auth.staff import Staff
from app.catalog import operability
from app.errors import Conflict, Invalid, NotFound
from app.pricing import policy_values
from app.pricing.policy_params_ops import OPERABLE_DEFAULT_DAYS, OPERABLE_MAX_DAYS

REASON_MIN, REASON_MAX = 20, 500
REVOKE_MAX = 300  # technical-constant: the bk-065 CHECK bound of revoke_reason
NOT_OVERRIDABLE = "a missing rate or an inactive room type, hotel or service cannot be overridden"

PLAN_SQL = "SELECT id::text AS id, source_id FROM booking.trip_plans WHERE source_system = 'cis' AND source_id = $1"
# The expiry comes from the transaction clock, the same now() as created_at, so a length equal to the bk-065 CHECK's
# outer bound (90 days) still passes it.
INSERT_SQL = """
INSERT INTO booking.trip_plan_operable_overrides (trip_plan_id, rules, findings, reason, expires_at, created_by)
VALUES ($1::uuid, $2::text[], $3::text[], $4, now() + make_interval(days => $5::int), $6)
RETURNING id::text, expires_at
"""
REVOKE_SQL = """
UPDATE booking.trip_plan_operable_overrides SET revoked_by = $2, revoked_at = now(), revoke_reason = $3
WHERE trip_plan_id = $1::uuid AND revoked_at IS NULL RETURNING id::text, expires_at
"""


def _now() -> datetime:
    return datetime.now(UTC)


async def _plan(conn: asyncpg.Connection, source_id: str, *, lock: bool = False) -> Any:
    row = await conn.fetchrow(PLAN_SQL + (" FOR UPDATE" if lock else ""), source_id)
    if row is None:
        raise NotFound("trip_plan_not_found", "save the commercial row of this tour first")
    return row


async def _policy(conn: asyncpg.Connection) -> tuple[int, int]:
    on = await policy_values.today_utc(conn)
    policy = await policy_values.require_staff(conn, on, OPERABLE_DEFAULT_DAYS, OPERABLE_MAX_DAYS)
    return policy.integer(OPERABLE_DEFAULT_DAYS), policy.integer(OPERABLE_MAX_DAYS)


async def _verdict(conn: asyncpg.Connection, plan_id: str) -> tuple[operability.PlanFacts, operability.Verdict]:
    now = _now()
    plans = await operability.load_many(conn, [plan_id])
    plan = plans[plan_id]
    rated = await operability.rated_for(conn, [plan], now.date())
    return plan, operability.evaluate(plan, rated, now)


def overridable(verdict: operability.Verdict) -> dict[str, list[str]]:
    """``{rule: finding keys}`` an exception may cover now (blocking N1 / N3-supplier findings)."""
    out: dict[str, list[str]] = {}
    for f in verdict.findings:
        if f.level == operability.BLOCK and f.overridable:
            out.setdefault(f.rule, []).append(f.key)
    return out


async def operability_of(conn: asyncpg.Connection, source_id: str) -> dict[str, Any]:
    row = await _plan(conn, source_id)
    plan, verdict = await _verdict(conn, row["id"])
    default_days, max_days = await _policy(conn)
    live = plan.exception if plan.exception is not None and plan.exception.live(_now()) else None
    return {
        "source_id": source_id,
        "trip_plan_id": row["id"],
        "days": plan.days,
        "no_hotel_nights": sorted(plan.no_hotel_nights),
        **verdict.out(limit=None),
        "exception": live.out() if live else None,
        "overridable": overridable(verdict),
        "not_overridable": NOT_OVERRIDABLE,
        "exception_default_days": default_days,
        "exception_max_days": max_days,
    }


def _reason(text: str | None, *, field: str, minimum: int, maximum: int) -> str:
    value = (text or "").strip()
    if len(value) < minimum:
        raise Invalid(f"{field}_too_short", f"the {field.replace('_', ' ')} needs at least {minimum} characters")
    if len(value) > maximum:
        raise Invalid(f"{field}_too_long", f"the {field.replace('_', ' ')} has at most {maximum} characters")
    return value


async def _audit(conn: asyncpg.Connection, staff: Staff, plan: Any, action: str, summary: str, **changes: Any) -> None:
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action=action,
        entity_type="trip_plan",
        entity_id=plan["id"],
        entity_label=str(plan["source_id"])[:80],
        summary=summary,
        **changes,
    )


async def create_exception(
    conn: asyncpg.Connection, staff: Staff, source_id: str, *, rules: list[str], reason: str, days: int | None
) -> dict[str, Any]:
    """Allow the sale of a blocked tour for the chosen rules' current findings (see the module doc)."""
    text = _reason(reason, field="reason", minimum=REASON_MIN, maximum=REASON_MAX)
    wanted = sorted(set(rules))
    if not wanted or any(r not in operability.OVERRIDABLE_RULES for r in wanted):
        raise Invalid("rule_not_overridable", f"only N1 and N3 (supplier) can be overridden; {NOT_OVERRIDABLE}")
    default_days, max_days = await _policy(conn)
    length = default_days if days is None else days
    if not 1 <= length <= max_days:
        raise Invalid("expiry_too_long", f"an exception lasts 1 to {max_days} days")
    plan = await _plan(conn, source_id, lock=True)
    _facts, verdict = await _verdict(conn, plan["id"])
    open_findings = overridable(verdict)
    idle = [r for r in wanted if r not in open_findings]
    if idle:
        raise Invalid("rule_not_failing", f"{', '.join(idle)} has no finding to cover on this tour now")
    findings = sorted(key for r in wanted for key in open_findings[r])
    previous = await conn.fetch(REVOKE_SQL, plan["id"], staff.cis_user_id, "Replaced by a new sale exception.")
    created = await conn.fetchrow(INSERT_SQL, plan["id"], wanted, findings, text, length, staff.cis_user_id)
    new_id, expires_at = created["id"], created["expires_at"]
    await _audit(
        conn,
        staff,
        plan,
        "trip_plan.sell_exception_created",
        f"Sale exception for {', '.join(wanted)} until {expires_at.date().isoformat()}: {len(findings)} finding(s)",
        before={"replaced": [r["id"] for r in previous]} if previous else None,
        after={"id": new_id, "rules": wanted, "findings": findings, "expires_at": expires_at.isoformat()},
    )
    return await operability_of(conn, source_id)


async def revoke_exception(conn: asyncpg.Connection, staff: Staff, source_id: str, *, reason: str) -> dict[str, Any]:
    text = _reason(reason, field="revoke_reason", minimum=1, maximum=REVOKE_MAX)
    plan = await _plan(conn, source_id, lock=True)
    ended = await conn.fetch(REVOKE_SQL, plan["id"], staff.cis_user_id, text)
    if not ended:
        raise Conflict("no_exception", "this tour has no sale exception to revoke")
    await _audit(
        conn,
        staff,
        plan,
        "trip_plan.sell_exception_revoked",
        f"Sale exception revoked: {text[:200]}",
        before={"id": ended[0]["id"], "expires_at": ended[0]["expires_at"].isoformat()},
        after={"revoked": True, "reason": text},
    )
    return await operability_of(conn, source_id)


async def set_no_hotel_nights(
    conn: asyncpg.Connection, staff: Staff, source_id: str, nights: list[int]
) -> dict[str, Any]:
    plan = await _plan(conn, source_id, lock=True)
    facts, _ = await _verdict(conn, plan["id"])
    wanted = sorted(set(nights))
    bad = [n for n in wanted if not 1 <= n < facts.days]
    if bad:
        raise Invalid("invalid_night", f"a {facts.days}-day tour has nights 1 to {facts.days - 1}; not {bad[0]}")
    before = sorted(facts.no_hotel_nights)
    if wanted != before:
        await conn.execute(
            "UPDATE booking.trip_plans SET no_hotel_nights = $2::smallint[], updated_by = $3 WHERE id = $1::uuid",
            plan["id"],
            wanted,
            staff.cis_user_id,
        )
        await _audit(
            conn,
            staff,
            plan,
            "trip_plan.no_hotel_nights_set",
            f"Nights with no hotel needed: {', '.join(map(str, wanted)) or 'none'}",
            before={"no_hotel_nights": before},
            after={"no_hotel_nights": wanted},
        )
    return await operability_of(conn, source_id)
