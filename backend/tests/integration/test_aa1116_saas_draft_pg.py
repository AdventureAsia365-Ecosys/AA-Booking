"""AA-1116 (option B) on a real PostgreSQL, as role ``booking``: the monthly software draft reads the CIS 438 billing
terms from ``catalog.v_tenant_billing_v1`` (stood in by a table of its exact 438 columns around each test).

- a VND agency is drafted in VND from the VND list (no ``currency_unsupported``), with the seat minimum;
- an agreed price in force replaces the list unit price; an annual subscription is a zero line "invoice by hand";
- a free pilot is drafted at zero;
- the agency's currency moving from USD to VND clears the job lines of the USD draft of the month;
- the same move after the USD invoice of the month was issued drafts nothing in VND (``currency_moved_issued``);
- a draft with an amount ends with the zero "Discount: check the Tenant 360 quote" line (``discount_check``).

Names and places are invented."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from app.agency_billing import cis as cis_view
from app.agency_billing import drafts
from app.catalog import liveness
from tests.integration.test_aa1037_agency_billing import COLUMNS_SQL, purge

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

NAME = "it-1037 aa1116"  # purge() removes partners named it-1037%
# Copied verbatim from CIS tests/integration/test_aa1116_migration_438.py BILLING_COLUMNS (CIS 438, contract 1.9).
CIS_438_COLUMNS = (
    "tenant_id uuid, name text, kind text, legal_entity_name text, tax_id text, billing_email text, "
    "billing_address text, billing_country text, product text, plan_key text, plan_name text, "
    "price_usd_monthly numeric(10,2), price_unit text, seats_included integer, price_extra_seat_usd numeric(10,2), "
    "seats integer, subscription_status text, billing_currency text, price_vnd_monthly numeric(14,0), "
    "price_extra_seat_vnd numeric(14,0), seats_min integer, agreed_price_monthly numeric(14,2), agreed_currency text, "
    "agreed_until date, billing_interval text, trial_ends_at timestamp with time zone, is_pilot boolean, "
    "bill_free boolean"
)


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    await purge(conn)
    await conn.execute(f"CREATE TABLE catalog.v_tenant_billing_v1 ({CIS_438_COLUMNS})")
    await conn.execute("GRANT SELECT ON catalog.v_tenant_billing_v1 TO booking")
    assert await conn.fetchval(COLUMNS_SQL, cis_view.TERMS_VIEW.qualified) == CIS_438_COLUMNS
    missing = set(cis_view.TERMS_VIEW.columns) - {c.split(" ", 1)[0] for c in CIS_438_COLUMNS.split(", ")}
    assert not missing, f"Booking selects {missing} that CIS 438 does not have"
    liveness.reset_cache()
    try:
        yield conn
    finally:
        await purge(conn)
        liveness.reset_cache()
        await conn.close()


@pytest_asyncio.fixture
async def pool(admin: asyncpg.Connection, migrated_booking_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    p = await asyncpg.create_pool(migrated_booking_dsn, min_size=1, max_size=2)
    try:
        yield p
    finally:
        await p.close()


async def _partner(admin: asyncpg.Connection, label: str) -> str:
    tid = str(uuid.uuid4())
    await admin.execute(
        "INSERT INTO booking.b2b_partners (tenant_id, agency_name, status, billing_currency, m2m_client_id)"
        " VALUES ($1::uuid, $2, 'active', 'USD', 'aa1116client')",
        tid,
        f"{NAME} {label}",
    )
    return tid


async def _subscription(admin: asyncpg.Connection, tid: str, product: str, plan: str, **v: Any) -> None:
    row: dict[str, Any] = {
        "tenant_id": tid, "name": "Lotus Lantern", "kind": "demo", "legal_entity_name": "Lotus Lantern Travel Co",
        "tax_id": "TX-1116", "billing_email": "accounts@lotus.example", "billing_address": "8 Kite Lane, Riverbend",
        "billing_country": "VN", "product": product, "plan_key": plan, "plan_name": plan.split(".")[1].title(),
        "price_unit": "tenant", "subscription_status": "active", "billing_currency": "USD", **v,
    }  # fmt: skip
    cols = list(row)
    await admin.execute(
        f"INSERT INTO catalog.v_tenant_billing_v1 ({', '.join(cols)})"  # noqa: S608
        f" VALUES ({', '.join(f'${i + 1}' for i in range(len(cols)))})",
        *row.values(),
    )


async def _invoices(admin: asyncpg.Connection, tid: str) -> list[dict[str, Any]]:
    rows = await admin.fetch(
        "SELECT i.currency, i.status, l.line_role, l.quantity, l.unit_price, l.amount, l.description"
        " FROM booking.agency_invoice i JOIN booking.agency_invoice_line l ON l.invoice_id = i.id"
        " WHERE i.party_tenant_id = $1::uuid AND i.invoice_kind = 'saas' ORDER BY i.currency, l.line_no",
        tid,
    )
    return [dict(r) for r in rows]


async def test_vnd_agency_seat_minimum_agreed_and_annual(admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    tid = await _partner(admin, "Lotus Lantern")
    await _subscription(admin, tid, "content", "content.growth", price_usd_monthly=Decimal("149.00"),
                        price_vnd_monthly=Decimal(3790000), billing_currency="VND")  # fmt: skip
    await _subscription(admin, tid, "crm", "crm.team", price_usd_monthly=Decimal("19.00"),
                        price_vnd_monthly=Decimal(480000), price_unit="seat", seats=1, seats_min=3,
                        billing_currency="VND", agreed_price_monthly=Decimal(400000), agreed_currency="VND",
                        agreed_until=date(2027, 10, 31))  # fmt: skip
    await _subscription(admin, tid, "tripplanner", "tripplanner.pro", price_usd_monthly=Decimal("99.00"),
                        price_vnd_monthly=Decimal(2500000), billing_currency="VND",
                        billing_interval="year")  # fmt: skip
    out = await drafts.run_saas(pool, period="2026-11", tenant_id=tid, actor="job:test")
    assert (out["invoices_created"], out["lines_added"], out["currency_unsupported"]) == (1, 4, 0)
    assert (out["agreed"], out["annual_manual"], out["discount_check"]) == (1, 1, 1)
    rows = await _invoices(admin, tid)
    assert {r["currency"] for r in rows} == {"VND"}
    assert [(r["line_role"], r["quantity"], r["amount"]) for r in rows] == [
        ("subscription", Decimal("1.00"), Decimal("3790000.00")),
        ("subscription", Decimal("3.00"), Decimal("1200000.00")),
        ("subscription", Decimal("1.00"), Decimal("0.00")),
        ("subscription", Decimal("1.00"), Decimal("0.00")),
    ]
    assert rows[2]["description"].endswith("annual: invoice by hand")
    assert rows[3]["description"] == drafts.DISCOUNT_CHECK_TEXT
    total = await admin.fetchval(
        "SELECT total FROM booking.agency_invoice WHERE party_tenant_id = $1::uuid AND invoice_kind = 'saas'", tid
    )
    assert total == Decimal("4990000.00")


async def test_pilot_free_and_currency_move(admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    tid = await _partner(admin, "Paper Crane")
    await _subscription(admin, tid, "content", "content.growth", price_usd_monthly=Decimal("149.00"),
                        price_vnd_monthly=Decimal(3790000))  # fmt: skip
    first = await drafts.run_saas(pool, period="2026-11", tenant_id=tid, actor="job:test")
    assert first["invoices_created"] == 1
    assert [(r["currency"], r["amount"]) for r in await _invoices(admin, tid)] == [
        ("USD", Decimal("149.00")),
        ("USD", Decimal("0.00")),
    ]

    # CIS: the agency now bills in VND and is a free pilot
    await admin.execute(
        "UPDATE catalog.v_tenant_billing_v1 SET billing_currency = 'VND', is_pilot = true, bill_free = true"
        " WHERE tenant_id = $1::uuid",
        tid,
    )
    again = await drafts.run_saas(pool, period="2026-11", tenant_id=tid, actor="job:test")
    assert (again["currency_moved"], again["pilot_free"], again["invoices_created"]) == (1, 1, 1)
    rows = await _invoices(admin, tid)
    assert [(r["currency"], r["unit_price"]) for r in rows] == [("VND", Decimal("0.00"))]
    assert rows[0]["description"].endswith("pilot, free (list 3,790,000 VND)")
    usd_total = await admin.fetchval(
        "SELECT total FROM booking.agency_invoice WHERE party_tenant_id = $1::uuid AND currency = 'USD'", tid
    )
    assert usd_total == Decimal("0.00")


async def _issue_by_hand(admin: asyncpg.Connection, tid: str) -> None:
    """Mark the agency's SaaS draft issued (as bk_owner, guards off, like ``purge``): the issue path itself is
    test_aa1037_agency_billing.py's; here only the state "issued in USD" matters."""
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE bk_owner")
        entity = await admin.fetchval(
            "INSERT INTO booking.billing_entity (code, legal_entity, is_default, active)"
            " VALUES ('IT1037-AA1116', 'Kestrel Harbour Travel Ltd', false, true) RETURNING id::text"
        )
        await admin.execute("ALTER TABLE booking.agency_invoice DISABLE TRIGGER USER")
        await admin.execute(
            "UPDATE booking.agency_invoice SET status = 'issued', number = 'AAS-2026-001116', issued_at = now(),"
            " issued_by = 'it-1037', issued_on = current_date, due_date = current_date, billing_entity_id = $2::uuid,"
            " issuer = '{}'::jsonb, bill_to = '{}'::jsonb WHERE party_tenant_id = $1::uuid AND invoice_kind = 'saas'",
            tid,
            entity,
        )
        await admin.execute("ALTER TABLE booking.agency_invoice ENABLE TRIGGER USER")


async def test_currency_move_after_the_month_was_issued(admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    tid = await _partner(admin, "Kestrel Harbour")
    await _subscription(admin, tid, "content", "content.growth", price_usd_monthly=Decimal("149.00"),
                        price_vnd_monthly=Decimal(3790000))  # fmt: skip
    assert (await drafts.run_saas(pool, period="2026-11", tenant_id=tid, actor="job:test"))["invoices_created"] == 1
    await _issue_by_hand(admin, tid)

    # CIS: the agency now bills in VND; November is already issued in USD
    await admin.execute(
        "UPDATE catalog.v_tenant_billing_v1 SET billing_currency = 'VND' WHERE tenant_id = $1::uuid", tid
    )
    again = await drafts.run_saas(pool, period="2026-11", tenant_id=tid, actor="job:test")
    assert (again["period_issued"], again["currency_moved_issued"], again["invoices_created"]) == (1, 1, 0)
    live = await admin.fetch(
        "SELECT currency, status FROM booking.agency_invoice WHERE party_tenant_id = $1::uuid AND status <> 'void'",
        tid,
    )
    assert [(r["currency"], r["status"]) for r in live] == [("USD", "issued")]
    # December (no invoice yet) is drafted in VND
    dec = await drafts.run_saas(pool, period="2026-12", tenant_id=tid, actor="job:test")
    assert (dec["invoices_created"], dec["currency_moved_issued"]) == (1, 0)
