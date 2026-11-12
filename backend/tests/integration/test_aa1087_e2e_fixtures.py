"""AA-1087 / AA-1070 crm-13 (S278 lane l18): the Dev-only E2E fixture seeder (:mod:`app.e2e_fixtures`) and the
synthetic-departure rule of the staff identity's departure cancel (root ADR 0006a A10e-1), as role ``booking`` on a
real PostgreSQL.

Built on the bk-020 sellable plan (``tests.integration.test_bk020_b2b.plan``): a staff-entered booking of the partner
gives the seeder its template. ``catalog.v_tenant_account_v1`` (CIS 357 + 401) is stood in by a plain table that marks
that partner's tenant synthetic. Covers:

- the DRY RUN plan writes nothing; ``apply`` makes a new departure, the agency's confirmed booking on it, a pending
  supplier booking on that case and an issued settlement invoice, all named by the run; a second ``apply`` of the same
  run returns the same rows;
- the new departure is synthetic for the cancel rule (every agency holding seats on it is synthetic); a departure with
  no hold, or one a real agency also holds seats on, is not; the guard SQL refuses the supplier booking / invoice of a
  real agency as before;
- a real agency (``--agency-tenant``) and a missing staff row are refused.

Dates are relative to today; names are invented.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import date, timedelta
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio

from app import e2e_fixtures
from app.auth import e2e_staff
from app.db import Database
from tests.integration.test_aa1075_booking_ops import bk047  # noqa: F401 — fixture used by name
from tests.integration.test_bk020_b2b import (  # noqa: F401 — fixtures admin, client, plan are used by name
    DAY1,
    SALES,
    TENANT,
    _departure,
    _ok,
    _plan_line,
    _staff,
    admin,
    client,
    plan,
)
from tests.staff_tokens import LocalCis, LocalM2M

FIXTURES = (admin, bk047, client, plan)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

Client = tuple[httpx.AsyncClient, LocalCis, LocalM2M, Database]

ACTOR = "it-l18-e2e-staff"
REAL_TENANT = "e2000000-0000-4000-8000-0000000b1018"
ENTITY = "IT18FIXAA"
VIEW_COLUMNS = (
    "tenant_id uuid, name text, account_status text, suspend_reason text, in_service boolean, kind text, "
    "is_pilot boolean, trade_book boolean, is_synthetic boolean"
)
TODAY = date.today()


async def _purge_l18(conn: asyncpg.Connection) -> None:
    async with conn.transaction():
        await conn.execute("SET LOCAL ROLE bk_owner")
        for t in ("booking.agency_invoice", "booking.agency_invoice_line"):
            await conn.execute(f"ALTER TABLE {t} DISABLE TRIGGER USER")
        await conn.execute(
            "DELETE FROM booking.agency_invoice_line WHERE invoice_id IN"
            " (SELECT id FROM booking.agency_invoice WHERE created_by = $1)",
            ACTOR,
        )
        await conn.execute("DELETE FROM booking.agency_invoice WHERE created_by = $1", ACTOR)
        for t in ("booking.agency_invoice", "booking.agency_invoice_line"):
            await conn.execute(f"ALTER TABLE {t} ENABLE TRIGGER USER")
        await conn.execute("UPDATE booking.billing_entity SET is_default = false WHERE code = $1", ENTITY)
        await conn.execute("DELETE FROM booking.billing_entity WHERE code = $1", ENTITY)
    await conn.execute("DROP TABLE IF EXISTS catalog.v_tenant_account_v1")


@pytest_asyncio.fixture
async def seeded(
    bk047: None, admin: asyncpg.Connection, client: Client, plan: dict[str, str]
) -> AsyncIterator[dict[str, Any]]:
    """A synthetic partner (the bk-020 one) with one staff-entered booking on a departure; a real partner; the A10e
    staff row; a default billing entity."""
    c, cis, _, _ = client
    await _purge_l18(admin)
    await admin.execute(f"CREATE TABLE catalog.v_tenant_account_v1 ({VIEW_COLUMNS})")
    await admin.execute("GRANT SELECT ON catalog.v_tenant_account_v1 TO booking")
    for tid, synthetic in ((TENANT, True), (REAL_TENANT, False)):
        await admin.execute(
            "INSERT INTO catalog.v_tenant_account_v1 VALUES ($1, 'Copperleaf Journeys', 'active', NULL, true,"
            " 'agency', false, true, $2)",
            tid,
            synthetic,
        )
    real_partner = await admin.fetchval(
        "INSERT INTO booking.b2b_partners (tenant_id, agency_name, m2m_client_id) VALUES ($1, 'Brightmoor Tours',"
        " 'l18realclient') RETURNING id::text",
        REAL_TENANT,
    )
    await admin.execute(
        "INSERT INTO booking.staff_users (cis_user_id, email, role, active, name) VALUES ($1, $2, 'sales', true,"
        " 'E2E staff') ON CONFLICT (cis_user_id) DO UPDATE SET active = true",
        ACTOR,
        e2e_staff.E2E_BOOKING_STAFF_EMAIL,
    )
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE bk_owner")
        await admin.execute("UPDATE booking.billing_entity SET is_default = false WHERE is_default")
        await admin.execute(
            "INSERT INTO booking.billing_entity (code, legal_entity, is_default) VALUES ($1, 'Lanternfield Travel Ltd',"
            " true)",
            ENTITY,
        )
    dep = await _departure(c, cis, DAY1, 10)
    entry = {
        "partner_id": plan["partner"],
        "agency_ref": "AG-L18-1",
        "currency": "USD",
        "lines": [_plan_line("tour", plan["plan"], dep["id"])],
    }
    await _ok(
        await c.post("/b2b-desk/entries", json=entry, headers={**_staff(cis, SALES), "Idempotency-Key": "l18-key-1"}),
        201,
    )
    try:
        yield {"template_departure": dep["id"], "real_partner": real_partner}
    finally:
        await _purge_l18(admin)
        await admin.execute("UPDATE booking.staff_users SET active = false WHERE cis_user_id = $1", ACTOR)


async def _counts(conn: asyncpg.Connection) -> tuple[int, ...]:
    return tuple(
        [
            await conn.fetchval(f"SELECT count(*) FROM booking.{t}")  # noqa: S608 — fixed names
            for t in ("departures", "b2b_holds", "supplier_bookings", "agency_invoice")
        ]
    )


async def test_dry_run_then_apply_then_replay(
    seeded: dict[str, Any], admin: asyncpg.Connection, client: Client, migrated_booking_dsn: str
) -> None:
    pool = await asyncpg.create_pool(migrated_booking_dsn, min_size=1, max_size=3)
    try:
        before = await _counts(admin)
        async with pool.acquire() as conn:
            planned = await e2e_fixtures.plan(conn, run="it-l18-a", agency_tenant=None, today=TODAY)
        assert await _counts(admin) == before  # DRY RUN writes nothing
        assert planned["agency"]["tenant_id"] == TENANT
        assert planned["departure"]["state"] == "create"
        assert date.fromisoformat(planned["departure"]["start_date"]) >= TODAY + timedelta(days=60)
        assert (planned["desk_booking"]["state"], planned["supplier_booking"]["state"]) == ("create", "create")
        assert planned["invoice"]["state"] == "create"

        made = await e2e_fixtures.apply(pool, planned, today=TODAY)
        assert made["supplier_booking"]["status"] == "pending"
        assert made["invoice"]["status"] == "issued" and made["invoice"]["number"].startswith("AAT-")
        case = await admin.fetchrow(
            "SELECT channel, party_tenant_id::text AS tenant, status, entered_via FROM booking.trip_cases"
            " WHERE id = $1::uuid",
            made["trip_case"]["id"],
        )
        assert dict(case) == {"channel": "b2b", "tenant": TENANT, "status": "confirmed", "entered_via": "staff"}
        actors = {
            r["actor_ref"]
            for r in await admin.fetch(
                "SELECT DISTINCT actor_ref FROM booking.audit_logs WHERE action = ANY($1::text[])",
                ["departure.created", "supplier_booking.created", "agency_invoice.issued"],
            )
        }
        assert ACTOR in actors

        # The guard: every fixture is a synthetic agency's record.
        async with pool.acquire() as conn:
            for route, params in (
                (e2e_staff.SUPPLIER_CONFIRM[1], {"booking_id": made["supplier_booking"]["id"]}),
                (e2e_staff.PAYMENT_RECORD[1], {"ref": made["invoice"]["number"]}),
                (e2e_staff.DEPARTURE_CANCEL[1], {"departure_id": made["departure_id"]}),
            ):
                assert await e2e_staff.is_synthetic_record(conn, route, params) is True, route

        # Replay: same rows, nothing new.
        after = await _counts(admin)
        async with pool.acquire() as conn:
            again_plan = await e2e_fixtures.plan(conn, run="it-l18-a", agency_tenant=None, today=TODAY)
        assert again_plan["desk_booking"]["state"] == "exists"
        assert again_plan["supplier_booking"]["state"] == "exists"
        assert again_plan["invoice"]["state"] == "exists"
        again = await e2e_fixtures.apply(pool, again_plan, today=TODAY)
        assert again == made
        assert await _counts(admin) == after

        # A second run: a new departure on another date, another invoice period.
        async with pool.acquire() as conn:
            second = await e2e_fixtures.plan(conn, run="it-l18-b", agency_tenant=TENANT, today=TODAY)
        made_b = await e2e_fixtures.apply(pool, second, today=TODAY)
        assert made_b["departure_id"] != made["departure_id"]
        assert made_b["invoice"]["number"] != made["invoice"]["number"]
    finally:
        await pool.close()


async def test_departure_rule_empty_and_mixed_departures_are_real(
    seeded: dict[str, Any], admin: asyncpg.Connection, client: Client, plan: dict[str, str], migrated_booking_dsn: str
) -> None:
    c, cis, _, _ = client
    conn = await asyncpg.connect(migrated_booking_dsn)
    try:
        route = e2e_staff.DEPARTURE_CANCEL[1]
        template = seeded["template_departure"]
        assert await e2e_staff.is_synthetic_record(conn, route, {"departure_id": template}) is True
        empty = await _departure(c, cis, (date.fromisoformat(DAY1) + timedelta(days=3)).isoformat(), 10)
        assert await e2e_staff.is_synthetic_record(conn, route, {"departure_id": empty["id"]}) is False
        assert await e2e_staff.is_synthetic_record(conn, route, {"departure_id": str(uuid.uuid4())}) is False
        # A real agency also holds seats on the template departure: the departure is no longer synthetic.
        entry = {
            "partner_id": seeded["real_partner"],
            "agency_ref": "AG-L18-REAL",
            "currency": "USD",
            "lines": [_plan_line("tour", plan["plan"], template)],
        }
        await _ok(
            await c.post(
                "/b2b-desk/entries", json=entry, headers={**_staff(cis, SALES), "Idempotency-Key": "l18-key-real"}
            ),
            201,
        )
        assert await e2e_staff.is_synthetic_record(conn, route, {"departure_id": template}) is False
    finally:
        await conn.close()


async def test_a_real_agency_or_no_staff_row_is_refused(
    seeded: dict[str, Any], admin: asyncpg.Connection, migrated_booking_dsn: str
) -> None:
    conn = await asyncpg.connect(migrated_booking_dsn)
    try:
        with pytest.raises(e2e_fixtures.Refused, match="not synthetic"):
            await e2e_fixtures.plan(conn, run="it-l18-c", agency_tenant=REAL_TENANT, today=TODAY)
        with pytest.raises(e2e_fixtures.Refused, match="no B2B partner"):
            await e2e_fixtures.plan(conn, run="it-l18-c", agency_tenant=str(uuid.uuid4()), today=TODAY)
        await admin.execute("UPDATE booking.staff_users SET active = false WHERE cis_user_id = $1", ACTOR)
        with pytest.raises(e2e_fixtures.Refused, match="sign in once"):
            await e2e_fixtures.plan(conn, run="it-l18-c", agency_tenant=None, today=TODAY)
    finally:
        await conn.close()
