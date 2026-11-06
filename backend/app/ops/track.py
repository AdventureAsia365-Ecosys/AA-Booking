"""The case auto-advance after the sale (AA-682 design D2, D3, §4): one function moves a sold trip case along the
supply track (bk-026) from the facts, through the DB-checked edges.

``supplier_confirming`` while a supplier was asked (requested or confirmed booking), else ``confirmed``; a
``supplier_confirming`` case keeps its status while any booking is live (the S1 rule). The money track of a B2C case
(``invoice_sent`` / ``payment_pending`` / ``paid`` from the customer invoice, bk-028) is gone with the customer
invoices (AA-1046): no case takes a money status (``trip_cases_post_sale_guard`` refuses one since bk-045). Travel
statuses (``pre_departure`` onwards) belong to the lifecycle job and never step back here (critic note 12). Every move
is a system UPDATE (empty ``booking.actor``, status note "Auto: …", audit actor ``aa682:advance_case``) that
``trip_cases_post_sale_guard`` checks again.
"""

from __future__ import annotations

import logging
from typing import Any

import asyncpg

from app import audit

log = logging.getLogger(__name__)

TRACK_STATUSES = frozenset({"confirmed", "supplier_confirming"})
ACTOR = "aa682:advance_case"


def next_status(status: str, *, live: int, asked: int) -> str | None:
    """The status the facts call for, or None when the case stays (pure). A kept b2c case left in a money status
    (``invoice_sent`` / ``payment_pending`` / ``paid``) is not on the track any more and never moves."""
    if status not in TRACK_STATUSES:
        return None
    if status == "supplier_confirming":
        target = "supplier_confirming" if live else "confirmed"
    else:
        target = "supplier_confirming" if asked else "confirmed"
    return None if target == status else target


async def advance_case(conn: asyncpg.Connection, case: Any, *, cause: str) -> str | None:
    """Move a locked case when the facts say so; returns the new status or None. The caller holds the case row lock."""
    counts = await conn.fetchrow(
        "SELECT count(*) FILTER (WHERE status IN ('pending', 'requested', 'confirmed')) AS live,"
        " count(*) FILTER (WHERE status IN ('requested', 'confirmed')) AS asked"
        " FROM booking.supplier_bookings WHERE trip_case_id = $1::uuid",
        case["id"],
    )
    status = await conn.fetchval("SELECT status FROM booking.trip_cases WHERE id = $1::uuid", case["id"])
    target = next_status(status, live=counts["live"], asked=counts["asked"])
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
        actor_ref=ACTOR,
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
