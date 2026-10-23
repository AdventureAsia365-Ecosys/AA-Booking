"""Pricing rules and FX rates as staff data, with maker-checker (root ADR 0005 decisions 2, 5, 8; UI spec Q19).

Both tables are append-only (bk-004): a change is a new row. A new row is **pending** until a second staff member
approves it (``bk-007`` ``booking.policy_approvals``); only approved rows are in force (``app.pricing.repository``).
The creator of a row may not approve it (403 ``own_row``; a bk-007 trigger refuses it too). Every write is audited.

Row status, on the pricing day ``today``:

- ``pending``: not approved yet, never used for a price;
- ``in_force``: the newest approved row of its channel / currency dated on or before ``today``;
- ``scheduled``: approved, dated after ``today`` (it takes over on that day);
- ``superseded``: approved, older than the row in force.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import asyncpg

from app import audit
from app.auth.staff import Staff
from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.pricing import engine

MAX_ROWS = 500
RULE_FIELDS = ("channel", "target_margin", "floor_margin", "peak_uplift_pts", "card_fee_pct", "valid_from", "note")
FX_FIELDS = ("currency", "rate_per_usd", "buffer_pct", "as_of", "source")

_STATUS = """
CASE
    WHEN a.id IS NULL THEN 'pending'
    WHEN p.id IN (SELECT id FROM in_force) THEN 'in_force'
    WHEN p.{day} > $1 THEN 'scheduled'
    ELSE 'superseded'
END
"""

RULES_SQL = f"""
WITH in_force AS (
    SELECT DISTINCT ON (r.channel) r.id
    FROM booking.pricing_rules r
    JOIN booking.policy_approvals ap ON ap.pricing_rule_id = r.id
    WHERE r.valid_from <= $1
    ORDER BY r.channel, r.valid_from DESC
)
SELECT p.id::text AS id, p.channel, p.target_margin, p.floor_margin, p.peak_uplift_pts, p.card_fee_pct,
       p.valid_from, p.note, p.created_at, p.created_by, cs.name AS created_by_name,
       a.approved_by, aps.name AS approved_by_name, a.approved_at,
       {_STATUS.format(day="valid_from")} AS status
FROM booking.pricing_rules p
LEFT JOIN booking.policy_approvals a ON a.pricing_rule_id = p.id
LEFT JOIN booking.staff_users cs ON cs.cis_user_id = p.created_by
LEFT JOIN booking.staff_users aps ON aps.cis_user_id = a.approved_by
WHERE ($2::text IS NULL OR p.channel = $2) AND ($3::uuid IS NULL OR p.id = $3)
ORDER BY p.valid_from DESC, p.channel, p.created_at DESC
LIMIT $4
"""  # noqa: S608 — constant fragments only

FX_SQL = f"""
WITH in_force AS (
    SELECT DISTINCT ON (f.currency) f.id
    FROM booking.fx_rates f
    JOIN booking.policy_approvals ap ON ap.fx_rate_id = f.id
    WHERE f.as_of <= $1
    ORDER BY f.currency, f.as_of DESC
)
SELECT p.id::text AS id, p.currency, p.rate_per_usd, p.buffer_pct, p.as_of, p.source, p.created_at,
       p.created_by, cs.name AS created_by_name,
       a.approved_by, aps.name AS approved_by_name, a.approved_at,
       {_STATUS.format(day="as_of")} AS status
FROM booking.fx_rates p
LEFT JOIN booking.policy_approvals a ON a.fx_rate_id = p.id
LEFT JOIN booking.staff_users cs ON cs.cis_user_id = p.created_by
LEFT JOIN booking.staff_users aps ON aps.cis_user_id = a.approved_by
WHERE ($2::text IS NULL OR p.currency = $2) AND ($3::uuid IS NULL OR p.id = $3)
ORDER BY p.as_of DESC, p.currency, p.created_at DESC
LIMIT $4
"""  # noqa: S608 — constant fragments only

POLICY_SQL = {
    "pricing_rule": "SELECT p.created_by, a.id AS approval_id FROM booking.pricing_rules p"
    " LEFT JOIN booking.policy_approvals a ON a.pricing_rule_id = p.id WHERE p.id = $1::uuid",
    "fx_rate": "SELECT p.created_by, a.id AS approval_id FROM booking.fx_rates p"
    " LEFT JOIN booking.policy_approvals a ON a.fx_rate_id = p.id WHERE p.id = $1::uuid",
    # bk-021 (AA-974): B2B cancellation-fee policies, same maker-checker (app.b2b.cancellation).
    "cancellation_policy": "SELECT p.created_by, a.id AS approval_id FROM booking.cancellation_policies p"
    " LEFT JOIN booking.policy_approvals a ON a.cancellation_policy_id = p.id WHERE p.id = $1::uuid",
}
APPROVE_SQL = {
    "pricing_rule": "INSERT INTO booking.policy_approvals (entity_type, pricing_rule_id, approved_by)"
    " VALUES ('pricing_rule', $1::uuid, $2)",
    "fx_rate": "INSERT INTO booking.policy_approvals (entity_type, fx_rate_id, approved_by)"
    " VALUES ('fx_rate', $1::uuid, $2)",
    "cancellation_policy": "INSERT INTO booking.policy_approvals (entity_type, cancellation_policy_id, approved_by)"
    " VALUES ('cancellation_policy', $1::uuid, $2)",
}
NOT_FOUND = {
    "pricing_rule": "rule_not_found",
    "fx_rate": "fx_rate_not_found",
    "cancellation_policy": "cancellation_policy_not_found",
}


def _person(ref: str | None, name: str | None) -> dict[str, Any] | None:
    if ref is None:
        return None
    return {"id": ref, "name": name}


def _common(row: Any, staff: Staff) -> dict[str, Any]:
    pending = row["status"] == "pending"
    return {
        "id": row["id"],
        "status": row["status"],
        "created_by": _person(row["created_by"], row["created_by_name"]),
        "created_at": row["created_at"],
        "approved_by": _person(row["approved_by"], row["approved_by_name"]),
        "approved_at": row["approved_at"],
        # Maker-checker, as the approve endpoint decides it (the UI only shows the button).
        "can_approve": pending and staff.can("pricing.write") and row["created_by"] != staff.cis_user_id,
    }


def _rule(row: Any, staff: Staff) -> dict[str, Any]:
    return {
        **_common(row, staff),
        "channel": row["channel"],
        "target_margin": row["target_margin"],
        "floor_margin": row["floor_margin"],
        "peak_uplift_pts": row["peak_uplift_pts"],
        "card_fee_pct": row["card_fee_pct"],
        "valid_from": row["valid_from"],
        "note": row["note"],
    }


def _fx(row: Any, staff: Staff) -> dict[str, Any]:
    return {
        **_common(row, staff),
        "currency": row["currency"].strip(),
        "rate_per_usd": row["rate_per_usd"],
        "buffer_pct": row["buffer_pct"],
        "as_of": row["as_of"],
        "source": row["source"],
    }


async def list_rules(
    conn: asyncpg.Connection, staff: Staff, *, today: date, channel: str | None = None, limit: int = MAX_ROWS
) -> list[dict[str, Any]]:
    rows = await conn.fetch(RULES_SQL, today, channel, None, min(limit, MAX_ROWS))
    return [_rule(r, staff) for r in rows]


async def list_fx_rates(
    conn: asyncpg.Connection, staff: Staff, *, today: date, currency: str | None = None, limit: int = MAX_ROWS
) -> list[dict[str, Any]]:
    rows = await conn.fetch(FX_SQL, today, currency, None, min(limit, MAX_ROWS))
    return [_fx(r, staff) for r in rows]


async def _rule_by_id(conn: asyncpg.Connection, staff: Staff, rule_id: str, today: date) -> dict[str, Any]:
    row = await conn.fetchrow(RULES_SQL, today, None, rule_id, 1)
    if row is None:
        raise NotFound("rule_not_found", "no pricing rule with this id")
    return _rule(row, staff)


async def _fx_by_id(conn: asyncpg.Connection, staff: Staff, fx_id: str, today: date) -> dict[str, Any]:
    row = await conn.fetchrow(FX_SQL, today, None, fx_id, 1)
    if row is None:
        raise NotFound("fx_rate_not_found", "no FX rate with this id")
    return _fx(row, staff)


def _pct(value: Decimal) -> str:
    return f"{(value * 100).normalize():f}%"


async def create_rule(conn: asyncpg.Connection, staff: Staff, data: dict[str, Any], *, today: date) -> dict[str, Any]:
    """A new pending rule row. ``data``: channel, target_margin, floor_margin, peak_uplift_pts, card_fee_pct,
    valid_from, note (already shape-checked by the router)."""
    channel = engine.Channel(data["channel"])
    try:  # the engine's own invariants (floor <= target, margin + uplift + fee < 1), same as the table CHECKs
        engine.PricingRule(
            channel=channel,
            target_margin=data["target_margin"],
            floor_margin=data["floor_margin"],
            peak_uplift_pts=data["peak_uplift_pts"],
            card_fee_pct=data["card_fee_pct"],
        )
    except engine.PricingError as exc:
        raise Invalid("invalid_rule", str(exc)) from None
    note = (data.get("note") or "").strip() or None
    try:
        rule_id = await conn.fetchval(
            """
            INSERT INTO booking.pricing_rules
                (channel, target_margin, floor_margin, peak_uplift_pts, card_fee_pct, valid_from, note, created_by)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING id::text
            """,
            channel.value,
            data["target_margin"],
            data["floor_margin"],
            data["peak_uplift_pts"],
            data["card_fee_pct"],
            data["valid_from"],
            note,
            staff.cis_user_id,
        )
    except asyncpg.UniqueViolationError:
        raise Conflict(
            "rule_exists", f"a {channel.value} rule dated {data['valid_from']} already exists; use another date"
        ) from None
    except asyncpg.CheckViolationError:
        raise Invalid("invalid_rule", "the rule breaks a pricing_rules check") from None
    row = await _rule_by_id(conn, staff, rule_id, today)
    label = f"{channel.value} from {data['valid_from']}"
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="pricing_rule.created",
        entity_type="pricing_rule",
        entity_id=rule_id,
        after={k: row[k] for k in RULE_FIELDS},
        entity_label=label,
        summary=f"New pricing rule {label}: target {_pct(data['target_margin'])}, floor {_pct(data['floor_margin'])}"
        " (pending approval)",
    )
    return row


async def create_fx_rate(
    conn: asyncpg.Connection, staff: Staff, data: dict[str, Any], *, today: date
) -> dict[str, Any]:
    """A new pending FX row. ``data``: currency, rate_per_usd, buffer_pct, as_of, source."""
    currency = data["currency"]
    if currency == engine.BASE_CURRENCY:
        raise Invalid("usd_is_base", "USD is the base currency and has no FX rate")
    source = (data.get("source") or "").strip()
    if not source:
        raise Invalid("source_required", "an FX rate needs its source (bank, ECB, manual-dev, …)")
    try:
        fx_id = await conn.fetchval(
            """
            INSERT INTO booking.fx_rates (currency, rate_per_usd, buffer_pct, as_of, source, created_by)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id::text
            """,
            currency,
            data["rate_per_usd"],
            data["buffer_pct"],
            data["as_of"],
            source,
            staff.cis_user_id,
        )
    except asyncpg.UniqueViolationError:
        raise Conflict(
            "fx_rate_exists", f"an {currency} rate dated {data['as_of']} already exists; use another date"
        ) from None
    except asyncpg.CheckViolationError:
        raise Invalid("invalid_fx_rate", "the rate breaks an fx_rates check") from None
    row = await _fx_by_id(conn, staff, fx_id, today)
    label = f"{currency} {data['as_of']}"
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="fx_rate.created",
        entity_type="fx_rate",
        entity_id=fx_id,
        after={k: row[k] for k in FX_FIELDS},
        entity_label=label,
        summary=f"New FX rate {label}: {data['rate_per_usd']} per USD, buffer {_pct(data['buffer_pct'])}"
        " (pending approval)",
    )
    return row


async def approve_row(conn: asyncpg.Connection, staff: Staff, entity_type: str, entity_id: str) -> None:
    """Second-person approval of one policy row (``POLICY_SQL`` entity types); 404 / 409 / 403 ``own_row``."""
    not_found = NOT_FOUND[entity_type]
    row = await conn.fetchrow(POLICY_SQL[entity_type], entity_id)
    if row is None:
        raise NotFound(not_found, f"no {entity_type.replace('_', ' ')} with this id")
    if row["approval_id"] is not None:
        raise Conflict("already_approved", "this row is already approved")
    if row["created_by"] == staff.cis_user_id:
        raise Forbidden("own_row", "maker-checker: a second person must approve a row you created")
    try:
        await conn.execute(APPROVE_SQL[entity_type], entity_id, staff.cis_user_id)
    except asyncpg.UniqueViolationError:  # approved concurrently by someone else
        raise Conflict("already_approved", "this row is already approved") from None
    except asyncpg.CheckViolationError:  # the bk-007 trigger: same rule as above, enforced by the DB
        raise Forbidden("own_row", "maker-checker: a second person must approve a row you created") from None


async def approve_rule(conn: asyncpg.Connection, staff: Staff, rule_id: str, *, today: date) -> dict[str, Any]:
    await approve_row(conn, staff, "pricing_rule", rule_id)
    row = await _rule_by_id(conn, staff, rule_id, today)
    label = f"{row['channel']} from {row['valid_from']}"
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="pricing_rule.approved",
        entity_type="pricing_rule",
        entity_id=rule_id,
        before={"status": "pending"},
        after={"status": row["status"], "created_by": (row["created_by"] or {}).get("id")},
        entity_label=label,
        summary=f"Approved pricing rule {label} ({row['status'].replace('_', ' ')})",
    )
    return row


async def approve_fx_rate(conn: asyncpg.Connection, staff: Staff, fx_id: str, *, today: date) -> dict[str, Any]:
    await approve_row(conn, staff, "fx_rate", fx_id)
    row = await _fx_by_id(conn, staff, fx_id, today)
    label = f"{row['currency']} {row['as_of']}"
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="fx_rate.approved",
        entity_type="fx_rate",
        entity_id=fx_id,
        before={"status": "pending"},
        after={"status": row["status"], "created_by": (row["created_by"] or {}).get("id")},
        entity_label=label,
        summary=f"Approved FX rate {label} ({row['status'].replace('_', ' ')})",
    )
    return row
