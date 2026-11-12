"""AA-1092 residual (needs bk-049 + bk-058): the B2B API binds the caller's tenant on a real PostgreSQL.

- A connection of the B2B pool bound to tenant A reads A's ``b2b_partners`` row and not B's (refused: no row), and
  writes none.
- Every statement of a B2B request runs on a connection bound to the agency the request acts for (the binding is the
  first statement of each connection it takes, the clearing the last).
- The bk_owner view ``v_tenant_billing_status`` still answers the caller's own row under the binding
  (``GET /b2b/v1/billing-status`` shows its own terms override), never another agency's.
- After B2B requests no pooled connection is left bound: the staff partner list still sees every agency.
All names are invented.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio

from app.b2b.access import BIND_SQL, UNBIND_SQL, TenantBoundPool
from app.db import Database
from app.main import create_app
from app.settings import Settings
from tests.staff_tokens import CRM_CLIENT, M2M_ISSUER, LocalCis, LocalM2M

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

TENANT_A = "e2000000-0000-4000-8000-000000001092"
TENANT_B = "e2000000-0000-4000-8000-000000002092"
ADMIN = "it-1092-admin"
# asyncpg's own reset when a connection goes back to the pool (it ends with RESET ALL, a second clearing).
POOL_RESET = "SELECT pg_advisory_unlock_all();"
WARN_A, SUSPEND_A = 11, 41


async def _clean(conn: asyncpg.Connection) -> None:
    tenants = [TENANT_A, TENANT_B]
    await conn.execute("DELETE FROM booking.billing_terms WHERE party_tenant_id = ANY($1::uuid[])", tenants)
    await conn.execute("SET ROLE bk_owner")
    try:
        await conn.execute("DELETE FROM booking.b2b_partners WHERE tenant_id = ANY($1::uuid[])", tenants)
    finally:
        await conn.execute("RESET ROLE")


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    await _clean(conn)
    for tenant, name in ((TENANT_A, "Fernhollow Trails"), (TENANT_B, "Brackenmoor Travel")):
        await conn.execute(
            "INSERT INTO booking.b2b_partners (tenant_id, agency_name, status, billing_currency, m2m_client_id)"
            " VALUES ($1::uuid, $2, 'active', 'USD', $3)",
            tenant,
            name,
            CRM_CLIENT,
        )
    # A's own terms override, so its billing status row is recognisable (B keeps the default row).
    await conn.execute(
        "INSERT INTO booking.billing_terms (party_tenant_id, deposit_pct, balance_days_before_departure, saas_due_days,"
        " settlement_due_days, warn_after_days, suspend_after_days) VALUES ($1::uuid, 0.3, 30, 14, 14, $2, $3)",
        TENANT_A,
        WARN_A,
        SUSPEND_A,
    )
    await conn.execute(
        "INSERT INTO booking.staff_users (cis_user_id, role, active, name) VALUES ($1, 'admin', true, $1)"
        " ON CONFLICT (cis_user_id) DO UPDATE SET role = EXCLUDED.role, active = true",
        ADMIN,
    )
    try:
        yield conn
    finally:
        await _clean(conn)
        await conn.close()


class StatementLog:
    """asyncpg query logger: (SQL, args) of every statement, in order."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, tuple[Any, ...]]] = []

    def __call__(self, record: Any) -> None:
        self.rows.append((record.query, tuple(record.args or ())))


@pytest_asyncio.fixture
async def booking_db(
    admin: asyncpg.Connection, migrated_booking_dsn: str
) -> AsyncIterator[tuple[Database, StatementLog]]:
    log = StatementLog()
    settings = Settings(
        database_url=migrated_booking_dsn,
        db_ssl="disable",
        environment="test",
        git_sha="ci",
        cognito_m2m_issuer=M2M_ISSUER,
        cognito_m2m_crm_client_id=CRM_CLIENT,
    )
    db = Database(settings, query_loggers=(log,))
    try:
        yield db, log
    finally:
        await db.close()


@pytest_asyncio.fixture
async def client(
    booking_db: tuple[Database, StatementLog],
) -> AsyncIterator[tuple[httpx.AsyncClient, LocalCis, LocalM2M, StatementLog]]:
    db, log = booking_db
    cis, m2m = LocalCis(), LocalM2M()
    app = create_app(db._settings, db, cis.verifier(), m2m_verifier=m2m.verifier())  # noqa: SLF001
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://booking/booking") as c:
        yield c, cis, m2m, log


def _m2m(m2m: LocalM2M, tenant: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {m2m.token()}", "X-AA-Tenant-Id": tenant}


async def test_a_bound_connection_reads_its_own_partner_only(booking_db: tuple[Database, StatementLog]) -> None:
    db, _ = booking_db
    bound = TenantBoundPool(await db.pool(), TENANT_A)
    seen = await bound.fetch(
        "SELECT tenant_id::text AS tenant_id FROM booking.b2b_partners WHERE tenant_id = ANY($1::uuid[])",
        [TENANT_A, TENANT_B],
    )
    assert [r["tenant_id"] for r in seen] == [TENANT_A]
    assert (
        await bound.fetchrow("SELECT agency_name FROM booking.b2b_partners WHERE tenant_id = $1::uuid", TENANT_B)
        is None
    )
    assert (
        await bound.execute("UPDATE booking.b2b_partners SET status = 'suspended' WHERE tenant_id = $1::uuid", TENANT_A)
        == "UPDATE 0"
    )
    # The plain pool (staff, jobs) is not bound: both rows.
    plain = await (await db.pool()).fetchval(
        "SELECT count(*) FROM booking.b2b_partners WHERE tenant_id = ANY($1::uuid[])", [TENANT_A, TENANT_B]
    )
    assert plain == 2


async def test_every_statement_of_a_b2b_request_is_bound_to_its_agency(
    client: tuple[httpx.AsyncClient, LocalCis, LocalM2M, StatementLog],
) -> None:
    c, _, m2m, log = client
    for tenant in (TENANT_A, TENANT_B):
        log.rows.clear()
        res = await c.get("/b2b/v1/billing-status", headers=_m2m(m2m, tenant))
        assert res.status_code == 200, res.text
        assert log.rows, "the request ran no statement"
        held = 0  # connections the request holds bound right now (one request runs its statements in order)
        for sql, args in log.rows:
            if sql == BIND_SQL:
                assert args == (tenant,)
                held += 1
            elif sql == UNBIND_SQL:
                held -= 1
            elif sql.startswith(POOL_RESET):
                assert held == 0 and "RESET ALL" in sql
            else:
                assert held > 0, f"unbound statement in a B2B request: {sql[:80]}"
        assert held == 0  # every connection cleared before it went back


async def test_the_billing_view_answers_the_callers_own_row(
    client: tuple[httpx.AsyncClient, LocalCis, LocalM2M, StatementLog], admin: asyncpg.Connection
) -> None:
    c, _, m2m, _ = client
    mine = (await c.get("/b2b/v1/billing-status", headers=_m2m(m2m, TENANT_A))).json()
    assert (mine["warn_after_days"], mine["suspend_after_days"]) == (WARN_A, SUSPEND_A)
    default = await admin.fetchrow(
        "SELECT warn_after_days, suspend_after_days FROM booking.billing_terms WHERE party_tenant_id IS NULL"
    )
    other = (await c.get("/b2b/v1/billing-status", headers=_m2m(m2m, TENANT_B))).json()
    expected = (default["warn_after_days"], default["suspend_after_days"]) if default else (None, None)
    assert (other["warn_after_days"], other["suspend_after_days"]) == expected


async def test_a_foreign_client_for_another_tenant_is_refused(
    client: tuple[httpx.AsyncClient, LocalCis, LocalM2M, StatementLog], admin: asyncpg.Connection
) -> None:
    c, _, m2m, _ = client
    await admin.execute(
        "UPDATE booking.b2b_partners SET m2m_client_id = 'bk1092otherclient' WHERE tenant_id = $1::uuid", TENANT_B
    )
    refused = await c.get("/b2b/v1/billing-status", headers=_m2m(m2m, TENANT_B))
    assert refused.status_code == 403 and refused.json()["error"] == "forbidden"
    unknown = await c.get("/b2b/v1/billing-status", headers=_m2m(m2m, str(uuid.UUID(int=1092))))
    assert unknown.status_code == 403 and unknown.json()["error"] == "unknown_partner"


async def test_no_pooled_connection_stays_bound_and_staff_see_every_agency(
    client: tuple[httpx.AsyncClient, LocalCis, LocalM2M, StatementLog], booking_db: tuple[Database, StatementLog]
) -> None:
    c, cis, m2m, _ = client
    db, _ = booking_db
    await asyncio.gather(*(c.get("/b2b/v1/billing-status", headers=_m2m(m2m, TENANT_A)) for _ in range(8)))
    staff = {"Authorization": f"Bearer {cis.token(sub=ADMIN, cis_role='reviewer')}"}
    for _ in range(3):
        res = await c.get("/b2b-partners", headers=staff)
        assert res.status_code == 200, res.text
        names = {p["tenant_id"] for p in res.json()["items"]}
        assert {TENANT_A, TENANT_B} <= names
    pool = await db.pool()
    conns = [await pool.acquire() for _ in range(pool.get_size())]
    try:
        values = [await conn.fetchval("SELECT current_setting('app.tenant_id', true) AS ok") for conn in conns]
    finally:
        for conn in conns:
            await pool.release(conn)
    assert all(v in (None, "") for v in values), values
