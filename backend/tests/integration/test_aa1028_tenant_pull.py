"""bk_tenant_pull on PostgreSQL 15 with the RDS role shape (AA-1028 R1 slice 2, root ADR 0019 d1 / d9), as role
``booking``.

``catalog.v_tenant_account_v1`` (CIS 357) is stood in by a plain table of 357's exact columns, created around each test
and dropped after (lessons.md: stand-ins only around the code that needs them). Covers:

- without the view: ``skipped``, nothing written; without the CRM client id: ``not_configured``;
- every tenant gets a row (AA itself, kind internal, never): name, active only while in service with trade.book,
  the CRM client, USD; one ``b2b_partner.created`` audit row each;
- a second run with nothing new writes nothing; a CIS suspension / a lost trade.book suspends, a reinstatement
  reactivates (audited), a tenant gone from CIS is suspended, never deleted;
- staff can no longer change the projected fields of a projected row (409 ``projected_from_cis``), only Booking's
  commercial terms; a row staff registered that the pull never touched stays editable.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from app.b2b import partners, tenant_pull
from app.catalog import liveness
from app.errors import Conflict

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

# Copied verbatim from the closing check of CIS api/migrations/357_tenant_account_catalog_view.sql.
CIS_357_COLUMNS = (
    "tenant_id uuid, name text, account_status text, suspend_reason text, in_service boolean, kind text, "
    "is_pilot boolean, trade_book boolean"
)
STUB = (
    f"CREATE TABLE catalog.v_tenant_account_v1 ({CIS_357_COLUMNS}); "
    "GRANT SELECT ON catalog.v_tenant_account_v1 TO booking;"
)
DROP = "DROP TABLE IF EXISTS catalog.v_tenant_account_v1"
COLUMNS_SQL = """
SELECT string_agg(a.attname || ' ' || format_type(a.atttypid, a.atttypmod), ', ' ORDER BY a.attnum)
FROM pg_attribute a WHERE a.attrelid = $1::regclass AND a.attnum > 0 AND NOT a.attisdropped
"""
CLIENT = "crmClient1028"
ADD = (
    "INSERT INTO catalog.v_tenant_account_v1 (tenant_id, name, account_status, suspend_reason, in_service, kind, "
    "is_pilot, trade_book) VALUES ($1, $2, $3, NULL, $4, $5, false, $6)"
)


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    # Other modules' partners stay as they are: the pull suspends rows whose tenant is not in the view.
    statuses = await conn.fetch("SELECT id, status, updated_by FROM booking.b2b_partners")
    try:
        yield conn
    finally:
        for r in statuses:
            await conn.execute("UPDATE booking.b2b_partners SET status = $2, updated_by = $3 WHERE id = $1",
                               r["id"], r["status"], r["updated_by"])  # fmt: skip
        await conn.close()


@pytest_asyncio.fixture
async def view(admin: asyncpg.Connection) -> AsyncIterator[asyncpg.Connection]:
    await admin.execute(DROP)
    await admin.execute(STUB)
    assert await admin.fetchval(COLUMNS_SQL, tenant_pull.VIEW.qualified) == CIS_357_COLUMNS
    missing = set(tenant_pull.VIEW.columns) - {c.split(" ", 1)[0] for c in CIS_357_COLUMNS.split(", ")}
    assert not missing, f"Booking selects {missing} that CIS 357 does not have"
    liveness.reset_cache()
    try:
        yield admin
    finally:
        await admin.execute(DROP)
        liveness.reset_cache()


@pytest_asyncio.fixture
async def pool(migrated_booking_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    p = await asyncpg.create_pool(migrated_booking_dsn, min_size=0, max_size=2)
    try:
        yield p
    finally:
        await p.close()


async def _row(admin: asyncpg.Connection, tenant: uuid.UUID) -> dict[str, Any] | None:
    r = await admin.fetchrow(
        "SELECT id::text AS id, agency_name, status, m2m_client_id, billing_currency, projected_at "
        "FROM booking.b2b_partners WHERE tenant_id = $1", tenant)  # fmt: skip
    return dict(r) if r else None


async def _audit(admin: asyncpg.Connection, partner_id: str) -> list[str]:
    rows = await admin.fetch(
        "SELECT action FROM booking.audit_logs WHERE entity_type = 'b2b_partner' AND entity_id = $1::uuid "
        "ORDER BY created_at, id", partner_id)  # fmt: skip
    return [r["action"] for r in rows]


async def test_without_the_view_or_the_client_nothing_runs(admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    await admin.execute(DROP)
    liveness.reset_cache()
    assert (await tenant_pull.run(pool, CLIENT))["outcome"] == "skipped"
    assert (await tenant_pull.run(pool, ""))["outcome"] == "not_configured"
    assert await tenant_pull.TenantPullSchedule()(pool) is None


async def test_every_tenant_is_projected_and_follows_cis(view: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    agency, demo, closed, internal, gone = (uuid.uuid4() for _ in range(5))
    await view.execute(ADD, agency, "  Zephyr   Trails ", "active", True, "agency", True)
    await view.execute(ADD, demo, "Larkspur Demo", "trial", True, "demo", True)
    await view.execute(ADD, closed, "Quillfeather Tours", "suspended", False, "agency", True)
    await view.execute(ADD, internal, "Adventure Asia", "active", True, "internal", True)
    await view.execute(ADD, gone, "Marigold Coast", "active", True, "agency", False)
    first = await tenant_pull.run(pool, CLIENT)
    assert first["outcome"] == "ok" and first["created"] == 4 and first["internal_skipped"] == 1
    a = await _row(view, agency)
    assert a["agency_name"] == "Zephyr Trails" and a["status"] == "active" and a["m2m_client_id"] == CLIENT
    assert a["billing_currency"] == "USD" and a["projected_at"] is not None
    assert (await _row(view, demo))["status"] == "active"
    assert (await _row(view, closed))["status"] == "suspended"
    assert (await _row(view, gone))["status"] == "suspended"  # no trade.book
    assert await _row(view, internal) is None
    assert await _audit(view, a["id"]) == ["b2b_partner.created"]
    # nothing new: nothing written
    again = await tenant_pull.run(pool, CLIENT)
    assert again["created"] == again["updated"] == again["suspended_gone"] == 0 and again["unchanged"] >= 4
    # CIS suspends the agency, reinstates the other one, and one tenant leaves CIS
    await view.execute("UPDATE catalog.v_tenant_account_v1 SET in_service = false, account_status = 'suspended' "
                       "WHERE tenant_id = $1", agency)  # fmt: skip
    await view.execute("UPDATE catalog.v_tenant_account_v1 SET in_service = true, account_status = 'active' "
                       "WHERE tenant_id = $1", closed)  # fmt: skip
    await view.execute("DELETE FROM catalog.v_tenant_account_v1 WHERE tenant_id = $1", demo)
    third = await tenant_pull.run(pool, CLIENT)
    assert third["updated"] == 2 and third["suspended_gone"] == 1
    assert (await _row(view, agency))["status"] == "suspended"
    assert (await _row(view, closed))["status"] == "active"
    assert (await _row(view, demo))["status"] == "suspended"
    assert await _audit(view, a["id"]) == ["b2b_partner.created", "b2b_partner.suspended"]
    assert await _audit(view, (await _row(view, closed))["id"]) == ["b2b_partner.created", "b2b_partner.reactivated"]


async def test_projected_rows_are_read_only_for_staff(view: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    tenant, manual = uuid.uuid4(), uuid.uuid4()
    await view.execute(ADD, tenant, "Driftwood Isles", "active", True, "agency", True)
    staff = SimpleNamespace(cis_user_id=None)  # created_by references booking.staff_users; NULL needs no staff row
    async with pool.acquire() as conn, conn.transaction():
        made = await partners.create_partner(conn, staff, {"tenant_id": str(manual), "agency_name": "Hand Made",
                                                           "m2m_client_id": CLIENT})  # fmt: skip
    await tenant_pull.run(pool, CLIENT)
    projected = await _row(view, tenant)
    async with pool.acquire() as conn:
        with pytest.raises(Conflict) as info:
            async with conn.transaction():
                await partners.update_partner(conn, staff, projected["id"], {"status": "suspended"})
        assert info.value.code == "projected_from_cis"
        async with conn.transaction():
            out = await partners.update_partner(conn, staff, projected["id"], {"credit_terms_ref": "NET-30"})
        assert out["credit_terms_ref"] == "NET-30" and out["status"] == "active"
    # the hand-made row (tenant not in CIS) is suspended by the pull, and from then on it is a projected row too
    assert made["status"] == "active" and made["projected_at"] is None
    assert (await _row(view, manual))["status"] == "suspended"


async def test_the_schedule_queues_one_run_per_slot(view: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    await view.execute("DELETE FROM booking.job WHERE kind = $1", tenant_pull.KIND)
    hook = tenant_pull.TenantPullSchedule()
    first = await hook(pool)
    assert first is not None and await hook(pool) is None
    assert await view.fetchval("SELECT count(*) FROM booking.job WHERE kind = $1", tenant_pull.KIND) == 1
    await view.execute("DELETE FROM booking.job WHERE kind = $1", tenant_pull.KIND)
