"""The CIS read of agency billing: ``catalog.v_tenant_billing_v1`` (CIS 381, contract tenant.entitlements.v1, the
billing part for Booking). One row per tenant and subscription (a tenant without one has a single row with NULL
subscription columns): the bill-to fields CIS holds as the account master (root ADR 0019 d1 / d9: Booking takes a
snapshot at issue) and each subscription with its plan price.

Read as role ``booking`` like ``catalog.v_tenant_account_v1`` (no HTTP, no secret). Feature-detected
(:func:`app.catalog.liveness.view_ready`): without CIS 381 the SaaS draft answers ``skipped`` and an issue snapshots
only the agency name Booking holds (``bill_to.source = 'booking'``)."""

from __future__ import annotations

from typing import Any

import asyncpg

from app.catalog import liveness

COLUMNS = (
    "tenant_id",
    "name",
    "kind",
    "legal_entity_name",
    "tax_id",
    "billing_email",
    "billing_address",
    "billing_country",
    "product",
    "plan_key",
    "plan_name",
    "price_usd_monthly",
    "price_unit",
    "seats_included",
    "price_extra_seat_usd",
    "seats",
    "subscription_status",
)
VIEW = liveness.View("catalog", "v_tenant_billing_v1", COLUMNS)
SELECT = ", ".join(COLUMNS)
LIVE_SUBSCRIPTION = ("active", "past_due")
BILL_TO_FIELDS = ("legal_entity_name", "tax_id", "billing_email", "billing_address", "billing_country")


async def ready(conn: asyncpg.Connection) -> bool:
    return await liveness.view_ready(conn, VIEW)


async def subscriptions(conn: asyncpg.Connection, tenant_ids: list[Any] | None = None) -> list[dict[str, Any]]:
    """Live subscription rows (``active`` / ``past_due``) of every tenant, or of ``tenant_ids``."""
    sql = f"SELECT {SELECT} FROM {VIEW.qualified} WHERE subscription_status = ANY($1::text[])"  # noqa: S608
    args: list[Any] = [list(LIVE_SUBSCRIPTION)]
    if tenant_ids is not None:
        sql += " AND tenant_id = ANY($2::uuid[])"
        args.append([str(t) for t in tenant_ids])
    rows = await conn.fetch(sql + " ORDER BY tenant_id, product", *args)
    return [dict(r) for r in rows]


async def bill_to(conn: asyncpg.Connection, tenant_id: Any, agency_name: str) -> dict[str, Any]:
    """The bill-to snapshot of one agency: the CIS legal fields when the view is readable, else the name only."""
    snapshot: dict[str, Any] = {"agency_name": agency_name, "source": "booking"}
    if not await ready(conn):
        return snapshot
    row = await conn.fetchrow(
        f"SELECT {', '.join(BILL_TO_FIELDS)} FROM {VIEW.qualified} WHERE tenant_id = $1::uuid LIMIT 1",  # noqa: S608
        str(tenant_id),
    )
    if row is None:
        return snapshot
    return {"agency_name": agency_name, "source": "cis", **{k: row[k] for k in BILL_TO_FIELDS}}
