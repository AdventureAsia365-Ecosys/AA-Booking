"""The CIS read of agency billing: ``catalog.v_tenant_billing_v1`` (CIS 377, contract tenant.entitlements.v1, the
billing part for Booking). One row per tenant and subscription (a tenant without one has a single row with NULL
subscription columns): the bill-to fields CIS holds as the account master (root ADR 0019 d1 / d9: Booking takes a
snapshot at issue) and each subscription with its plan price.

Read as role ``booking`` like ``catalog.v_tenant_account_v1`` (no HTTP, no secret). Feature-detected
(:func:`app.catalog.liveness.view_ready`): without CIS 377 the SaaS draft answers ``skipped`` and an issue snapshots
only the agency name Booking holds (``bill_to.source = 'booking'``).

AA-1116 (option B, CIS 438, contract tenant.entitlements.v1 1.9) appends the commercial terms (:data:`TERMS_COLUMNS`):
the agency's billing currency, the VND list, the seat minimum, the agreed price per billed unit with its currency and
end, the billing interval, the trial end, the pilot flag and ``bill_free``. They are detected on their own
(:data:`TERMS_VIEW`): before 438 the rows carry them as ``None`` and the draft keeps the 377 rules (USD list, the
Booking ``saas_currency``).

Contract 1.10 (CIS 443-444) appends the CIS quote's discount of each row (:data:`DISCOUNT_COLUMNS`): ``discount_pct``
(the share of the row's amount, 0-100) and ``discount_kind`` (``founding`` | ``combo``). Detected on their own too
(:data:`DISCOUNT_VIEW`): before 444 the rows carry them as ``None`` and the draft has no discount line."""

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
# CIS 438 (AA-1116): copied from CIS tests/integration/test_aa1116_migration_438.py BILLING_COLUMNS.
TERMS_COLUMNS = (
    "billing_currency",
    "price_vnd_monthly",
    "price_extra_seat_vnd",
    "seats_min",
    "agreed_price_monthly",
    "agreed_currency",
    "agreed_until",
    "billing_interval",
    "trial_ends_at",
    "is_pilot",
    "bill_free",
)
# CIS 444 (AA-1116, contract 1.10): the quote discount of the row.
DISCOUNT_COLUMNS = ("discount_pct", "discount_kind")
VIEW = liveness.View("catalog", "v_tenant_billing_v1", COLUMNS)
TERMS_VIEW = liveness.View("catalog", "v_tenant_billing_v1", COLUMNS + TERMS_COLUMNS)
DISCOUNT_VIEW = liveness.View("catalog", "v_tenant_billing_v1", COLUMNS + TERMS_COLUMNS + DISCOUNT_COLUMNS)
OPTIONAL_COLUMNS = TERMS_COLUMNS + DISCOUNT_COLUMNS
SELECT = ", ".join(COLUMNS)
LIVE_SUBSCRIPTION = ("active", "past_due")
BILL_TO_FIELDS = ("legal_entity_name", "tax_id", "billing_email", "billing_address", "billing_country")


async def ready(conn: asyncpg.Connection) -> bool:
    return await liveness.view_ready(conn, VIEW)


async def subscriptions(conn: asyncpg.Connection, tenant_ids: list[Any] | None = None) -> list[dict[str, Any]]:
    """Live subscription rows (``active`` / ``past_due``) of every tenant, or of ``tenant_ids``; every row carries
    :data:`TERMS_COLUMNS` (``None`` before CIS 438) and :data:`DISCOUNT_COLUMNS` (``None`` before CIS 444)."""
    columns = COLUMNS
    if await liveness.view_ready(conn, TERMS_VIEW):
        columns += TERMS_COLUMNS
        if await liveness.view_ready(conn, DISCOUNT_VIEW):
            columns += DISCOUNT_COLUMNS
    sql = f"SELECT {', '.join(columns)} FROM {VIEW.qualified} WHERE subscription_status = ANY($1::text[])"  # noqa: S608
    args: list[Any] = [list(LIVE_SUBSCRIPTION)]
    if tenant_ids is not None:
        sql += " AND tenant_id = ANY($2::uuid[])"
        args.append([str(t) for t in tenant_ids])
    rows = await conn.fetch(sql + " ORDER BY tenant_id, product", *args)
    return [{**dict.fromkeys(OPTIONAL_COLUMNS), **dict(r)} for r in rows]


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
