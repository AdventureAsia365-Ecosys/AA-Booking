"""AA-1092 (bk-058) on a real PostgreSQL: the tenant policy of booking.b2b_partners also covers bk_owner.

Registration and re-run no-op; b2b_partners_tenant is SELECT TO booking, bk_owner; under a bound tenant the bk_owner
view booking.v_tenant_billing_status (bk-042) answers the bound tenant's row (before bk-058 it answered none, which
would have turned the B2B credit stop off) and never another tenant's; with no tenant bound the view still answers
every agency; the owner still writes nothing while a tenant is bound.
All names are invented.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[3] / "migrations" / "058_b2b_partners_tenant_read_owner.sql"
POLICIES_SQL = (
    "SELECT polname, polcmd::text AS polcmd, polroles::regrole[]::text[] AS roles FROM pg_policy"
    " WHERE polrelid = 'booking.b2b_partners'::regclass ORDER BY polname"
)
STATUS_SQL = "SELECT tenant_id FROM booking.v_tenant_billing_status WHERE tenant_id = ANY($1::uuid[])"


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    try:
        yield conn
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def app(migrated_booking_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_booking_dsn)
    try:
        yield conn
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def two_partners(admin: asyncpg.Connection) -> AsyncIterator[tuple[uuid.UUID, uuid.UUID]]:
    a, b = uuid.uuid4(), uuid.uuid4()
    for tid, name, client in ((a, "Quillmere Voyages", "bk058clienta"), (b, "Saltreed Expeditions", "bk058clientb")):
        await admin.execute(
            "INSERT INTO booking.b2b_partners (tenant_id, agency_name, status, billing_currency, m2m_client_id)"
            " VALUES ($1, $2, 'active', 'USD', $3)",
            tid,
            name,
            client,
        )
    try:
        yield a, b
    finally:
        await admin.execute("SET ROLE bk_owner")
        await admin.execute("DELETE FROM booking.b2b_partners WHERE tenant_id = ANY($1::uuid[])", [a, b])
        await admin.execute("RESET ROLE")


async def _bound(conn: asyncpg.Connection, tenant: uuid.UUID) -> None:
    await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant))


async def test_registered_and_rerun_is_a_no_op(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 FROM booking.v_schema_versions WHERE version = 'bk-058'") == 1
    before = [dict(r) for r in await admin.fetch(POLICIES_SQL)]
    await admin.execute("SET ROLE bk_owner")
    await admin.execute(MIGRATION.read_text(encoding="utf-8"))
    await admin.execute("RESET ROLE")
    assert [dict(r) for r in await admin.fetch(POLICIES_SQL)] == before
    tenant = next(r for r in before if r["polname"] == "b2b_partners_tenant")
    assert (tenant["polcmd"], sorted(tenant["roles"])) == ("r", ["bk_owner", "booking"])


async def test_the_owner_view_answers_the_bound_tenant_only(
    app: asyncpg.Connection, two_partners: tuple[uuid.UUID, uuid.UUID]
) -> None:
    a, b = two_partners
    async with app.transaction():
        await _bound(app, a)
        mine = await app.fetch(STATUS_SQL, [a])
        other = await app.fetch(STATUS_SQL, [b])
    assert [r["tenant_id"] for r in mine] == [a]
    assert other == []


async def test_unbound_the_owner_view_answers_every_agency(
    app: asyncpg.Connection, two_partners: tuple[uuid.UUID, uuid.UUID]
) -> None:
    a, b = two_partners
    assert {r["tenant_id"] for r in await app.fetch(STATUS_SQL, [a, b])} == {a, b}


async def test_the_owner_bound_to_a_tenant_reads_it_and_writes_nothing(
    admin: asyncpg.Connection, two_partners: tuple[uuid.UUID, uuid.UUID]
) -> None:
    a, b = two_partners
    tx = admin.transaction()
    await tx.start()
    try:
        await admin.execute("SET LOCAL ROLE bk_owner")
        await _bound(admin, a)
        seen = await admin.fetch("SELECT tenant_id FROM booking.b2b_partners WHERE tenant_id = ANY($1::uuid[])", [a, b])
        assert [r["tenant_id"] for r in seen] == [a]
        assert await admin.execute("UPDATE booking.b2b_partners SET status = 'suspended' WHERE tenant_id = $1", a) == (
            "UPDATE 0"
        )
    finally:
        await tx.rollback()
