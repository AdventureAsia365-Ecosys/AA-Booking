"""``GET /booking/b2b/v1/terms`` (AA-1037 R4 extras G6; contract booking.b2b.v1 1.3): the calling agency's terms with
AA, all read from data rows (no terms text in code):

- ``payment``: the deposit %, balance days before departure and settlement due days of the agency's billing terms
  (its override, else the default row, bk-041); null when no terms row exists;
- ``credit``: its billing status (bk-042 ``booking.v_tenant_billing_status``): ``holds_paused`` is true while it is
  ``past_due``, when ``POST /holds`` answers 409 ``credit_hold_overdue`` (G4); the overdue amount per currency;
- ``cancellation_policy``: the B2B default policy in force today (a tour may have its own; a quote or hold shows the
  one that applies to it);
- ``clauses``: the **approved** partner terms clauses (bk-044), e.g. the price-error clause with its free-cancel
  window. A draft clause is never shown to an agency.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Any

import asyncpg

from app.agency_billing import clauses, config
from app.b2b import cancellation

STATUS_SQL = """
SELECT status, oldest_overdue_days, overdue_invoices, overdue_amount::text AS overdue_amount, warn_after_days,
       suspend_after_days
FROM booking.v_tenant_billing_status WHERE tenant_id = $1::uuid
"""
HUNDRED = Decimal("100")
PCT = Decimal("0.01")


def pct_text(fraction: Any) -> str:
    """A stored fraction (0.3000) as the contract's percent text ("30.00")."""
    return format((Decimal(str(fraction)) * HUNDRED).quantize(PCT), "f")


def money_list(raw: Any) -> list[dict[str, str]]:
    """``{currency: amount}`` (jsonb) as contract ``Money`` items, by currency."""
    data = json.loads(raw) if isinstance(raw, str) else (raw or {})
    return [
        {"amount": format(Decimal(str(amount)).quantize(PCT), "f"), "currency": currency}
        for currency, amount in sorted(data.items())
    ]


def credit_out(row: Any) -> dict[str, Any]:
    if row is None:
        return {
            "status": "ok",
            "holds_paused": False,
            "overdue_invoices": 0,
            "oldest_overdue_days": 0,
            "overdue_amount": [],
            "warn_after_days": None,
            "suspend_after_days": None,
        }
    return {
        "status": row["status"],
        "holds_paused": row["status"] == "past_due",
        "overdue_invoices": int(row["overdue_invoices"]),
        "oldest_overdue_days": int(row["oldest_overdue_days"]),
        "overdue_amount": money_list(row["overdue_amount"]),
        "warn_after_days": row["warn_after_days"],
        "suspend_after_days": row["suspend_after_days"],
    }


async def partner_terms(
    conn: asyncpg.Connection, *, tenant_id: str, today: date, source_id: str | None = None
) -> dict[str, Any]:
    """AA-1075 (bk-18): with ``source_id`` (a CIS tour) the deposit % and balance days of that tour's most specific
    terms row (agency + tour > tour > agency > default); an unknown tour reads the agency's terms."""
    plan_id = None
    if source_id is not None:
        plan_id = await conn.fetchval("SELECT id::text FROM booking.trip_plans WHERE source_id = $1", source_id)
    terms = await config.plan_terms(conn, tenant_id, plan_id)
    payment = None
    if terms is not None:
        payment = {
            "deposit_pct": pct_text(terms["deposit_pct"]),
            "balance_days_before_departure": int(terms["balance_days_before_departure"]),
            "settlement_due_days": int(terms["settlement_due_days"]),
            "scope": terms["scope"],
        }
    status = await conn.fetchrow(STATUS_SQL, tenant_id)
    policy = await cancellation.policy_in_force(conn, None, today)
    return {
        "as_of": today,
        "payment": payment,
        "credit": credit_out(status),
        "cancellation_policy": cancellation.policy_out(policy, basis=cancellation.BASIS_TODAY),
        "clauses": await clauses.approved(conn),
    }
