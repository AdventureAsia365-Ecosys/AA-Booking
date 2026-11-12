"""AA-1092 (bk-049) on a real PostgreSQL: row level security on booking.b2b_partners.

Registration and re-run no-op; RLS enabled AND forced; the two policies read app.tenant_id through NULLIF (RLS
catalogue guard rules a and d); the back office (role booking, no tenant bound) still reads and writes every partner;
a transaction that binds a tenant reads that tenant's row only, cannot update another partner (0 rows) and cannot
write at all; an empty app.tenant_id counts as unbound; the owner (bk_owner, as the migrations run) still sees every
row.
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

MIGRATION = Path(__file__).resolve().parents[3] / "migrations" / "049_b2b_partners_rls.sql"
# bk-058 (AA-1092) widens b2b_partners_tenant to bk_owner after it; a replay of bk-049 is a replay of the chain.
AMENDED_BY = Path(__file__).resolve().parents[3] / "migrations" / "058_b2b_partners_tenant_read_owner.sql"


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
    for tid, name, client in ((a, "Lanternwick Tours", "bk049clienta"), (b, "Marrowfen Journeys", "bk049clientb")):
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
        # Partners are never deleted by the app role (bk-019); the test rows go as the owner.
        await admin.execute("SET ROLE bk_owner")
        await admin.execute("DELETE FROM booking.b2b_partners WHERE tenant_id = ANY($1::uuid[])", [a, b])
        await admin.execute("RESET ROLE")


async def _bound(conn: asyncpg.Connection, tenant: uuid.UUID | str) -> None:
    await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant))


async def test_registered_and_rerun_is_a_no_op(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 FROM booking.v_schema_versions WHERE version = 'bk-049'") == 1
    before = await admin.fetch(
        "SELECT polname, polcmd, polroles::regrole[]::text[] AS roles FROM pg_policy"
        " WHERE polrelid = 'booking.b2b_partners'::regclass ORDER BY polname"
    )
    await admin.execute("SET ROLE bk_owner")
    await admin.execute(MIGRATION.read_text(encoding="utf-8"))
    await admin.execute(AMENDED_BY.read_text(encoding="utf-8"))
    await admin.execute("RESET ROLE")
    after = await admin.fetch(
        "SELECT polname, polcmd, polroles::regrole[]::text[] AS roles FROM pg_policy"
        " WHERE polrelid = 'booking.b2b_partners'::regclass ORDER BY polname"
    )
    assert [dict(r) for r in after] == [dict(r) for r in before]


async def test_rls_is_enabled_and_forced_with_the_two_policies(admin: asyncpg.Connection) -> None:
    row = await admin.fetchrow(
        "SELECT relrowsecurity AS enabled, relforcerowsecurity AS forced FROM pg_class"
        " WHERE oid = 'booking.b2b_partners'::regclass"
    )
    assert (row["enabled"], row["forced"]) == (True, True)
    policies = {
        r["name"]: r
        for r in await admin.fetch(
            "SELECT policyname AS name, permissive, roles::text[] AS roles, cmd, qual, with_check FROM pg_policies"
            " WHERE schemaname = 'booking' AND tablename = 'b2b_partners'"
        )
    }
    assert set(policies) == {"b2b_partners_back_office", "b2b_partners_tenant"}
    office, tenant = policies["b2b_partners_back_office"], policies["b2b_partners_tenant"]
    assert (office["permissive"], office["cmd"], sorted(office["roles"])) == (
        "PERMISSIVE",
        "ALL",
        ["bk_owner", "booking"],
    )
    # bk-058: bk_owner too, so a view it owns (v_tenant_billing_status) reads the bound tenant's row.
    assert (tenant["permissive"], tenant["cmd"], sorted(tenant["roles"])) == (
        "PERMISSIVE",
        "SELECT",
        ["bk_owner", "booking"],
    )
    assert tenant["with_check"] is None
    for text in (office["qual"], office["with_check"], tenant["qual"]):
        assert "NULLIF(current_setting('app.tenant_id'::text, true), ''::text)" in text
        assert "public" not in text


async def test_back_office_unbound_reads_and_writes_every_partner(
    app: asyncpg.Connection, two_partners: tuple[uuid.UUID, uuid.UUID]
) -> None:
    a, b = two_partners
    seen = await app.fetch("SELECT tenant_id FROM booking.b2b_partners WHERE tenant_id = ANY($1::uuid[])", [a, b])
    assert {r["tenant_id"] for r in seen} == {a, b}
    assert await app.execute("UPDATE booking.b2b_partners SET status = 'suspended' WHERE tenant_id = $1", b) == (
        "UPDATE 1"
    )
    assert await app.execute("UPDATE booking.b2b_partners SET status = 'active' WHERE tenant_id = $1", b) == (
        "UPDATE 1"
    )


async def test_an_empty_tenant_is_unbound(app: asyncpg.Connection, two_partners: tuple[uuid.UUID, uuid.UUID]) -> None:
    a, b = two_partners
    async with app.transaction():
        await _bound(app, "")
        seen = await app.fetch("SELECT tenant_id FROM booking.b2b_partners WHERE tenant_id = ANY($1::uuid[])", [a, b])
    assert {r["tenant_id"] for r in seen} == {a, b}


async def test_a_bound_tenant_reads_its_own_row_only(
    app: asyncpg.Connection, two_partners: tuple[uuid.UUID, uuid.UUID]
) -> None:
    a, b = two_partners
    async with app.transaction():
        await _bound(app, a)
        seen = await app.fetch("SELECT tenant_id FROM booking.b2b_partners")
        other = await app.fetchval("SELECT count(*) FROM booking.b2b_partners WHERE tenant_id = $1", b)
    assert [r["tenant_id"] for r in seen] == [a]
    assert other == 0


async def test_a_bound_tenant_cannot_write_any_partner(
    app: asyncpg.Connection, two_partners: tuple[uuid.UUID, uuid.UUID]
) -> None:
    a, b = two_partners
    tx = app.transaction()
    await tx.start()
    try:
        await _bound(app, a)
        assert await app.execute("UPDATE booking.b2b_partners SET status = 'suspended' WHERE tenant_id = $1", b) == (
            "UPDATE 0"
        )
        assert await app.execute("UPDATE booking.b2b_partners SET status = 'suspended' WHERE tenant_id = $1", a) == (
            "UPDATE 0"
        )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await app.execute(
                "INSERT INTO booking.b2b_partners (tenant_id, agency_name, status, billing_currency, m2m_client_id)"
                " VALUES ($1, 'Thistledown Rambles', 'active', 'USD', 'bk049clientc')",
                a,
            )
    finally:
        await tx.rollback()
    statuses = await app.fetch("SELECT status FROM booking.b2b_partners WHERE tenant_id = ANY($1::uuid[])", [a, b])
    assert {r["status"] for r in statuses} == {"active"}


async def test_the_owner_sees_every_row(admin: asyncpg.Connection, two_partners: tuple[uuid.UUID, uuid.UUID]) -> None:
    a, b = two_partners
    await admin.execute("SET ROLE bk_owner")
    try:
        n = await admin.fetchval("SELECT count(*) FROM booking.b2b_partners WHERE tenant_id = ANY($1::uuid[])", [a, b])
    finally:
        await admin.execute("RESET ROLE")
    assert n == 2
