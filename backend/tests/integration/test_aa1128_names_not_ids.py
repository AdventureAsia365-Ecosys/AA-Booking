"""AA-1128 B3 on a real PostgreSQL: staff reads name agencies and tours, never only their ids.

1. A B2B trip case (list and detail) carries ``party_agency_name`` from ``booking.b2b_partners`` read as role
   ``booking`` (RLS on that table, no tenant bound), and ``None`` for an agency Booking has no partner row for.
2. A tour lifecycle notification takes the tour's title from ``catalog.v_tour``, else from a trip case itinerary line
   of the key (a retired tour has left the catalog view).

Invented agencies, tours and ids only; every row this module writes is removed at the end."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import asyncpg
import pytest
import pytest_asyncio

from app.catalog import lifecycle, liveness
from app.trip_cases import service
from tests.integration import ops

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

AGENCY = "Invented Heron Bay Journeys"
TOUR_KEY = "aa112800-0000-4000-8000-000000000001"
GONE_KEY = "aa112800-0000-4000-8000-000000000002"


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    try:
        yield conn
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def pool(migrated_booking_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    p = await asyncpg.create_pool(migrated_booking_dsn, min_size=0, max_size=2)
    try:
        yield p
    finally:
        await p.close()


async def test_a_b2b_case_carries_its_agency_name(admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    named = await ops.b2b_case(admin, tag="1128-named")
    unnamed = await ops.b2b_case(admin, tag="1128-unnamed")
    tenant = await admin.fetchval(
        "SELECT party_tenant_id::text FROM booking.trip_cases WHERE id = $1::uuid", named["id"]
    )
    partner = await admin.fetchval(
        "INSERT INTO booking.b2b_partners (tenant_id, agency_name, status, billing_currency, m2m_client_id)"
        " VALUES ($1::uuid, $2, 'active', 'USD', $3) RETURNING id::text",
        tenant,
        AGENCY,
        f"it1128client{uuid.uuid4().hex[:8]}",
    )
    try:
        async with pool.acquire() as conn:
            detail = await service.get_case(conn, named["code"])
            other = await service.get_case(conn, unnamed["code"])
            listed = await service.list_cases(conn, q=named["code"])
        assert detail["party_agency_name"] == AGENCY
        assert other["party_agency_name"] is None
        assert [i["party_agency_name"] for i in listed["items"] if i["code"] == named["code"]] == [AGENCY]
    finally:
        await admin.execute("DELETE FROM booking.b2b_partners WHERE id = $1::uuid", partner)
        await admin.execute("DELETE FROM booking.trip_cases WHERE id = ANY($1::uuid[])", [named["id"], unnamed["id"]])


async def test_a_lifecycle_notification_takes_the_catalog_title_then_the_itinerary_title(
    admin: asyncpg.Connection, pool: asyncpg.Pool
) -> None:
    await admin.execute("DELETE FROM catalog.v_tour WHERE source_id = ANY($1::text[])", [TOUR_KEY, GONE_KEY])
    await admin.execute(
        "INSERT INTO catalog.v_tour (source_id, title, country, duration_days) VALUES ($1, 'Invented Ridge Trek',"
        " 'Vietnam', 3)",
        TOUR_KEY,
    )
    liveness.reset_cache()
    case = await ops.b2b_case(admin, tag="1128-title")
    version = await admin.fetchval(
        "INSERT INTO booking.trip_case_versions (trip_case_id, version_number) VALUES ($1::uuid, 1) RETURNING id::text",
        case["id"],
    )
    await admin.execute(
        "INSERT INTO booking.trip_case_version_itinerary (trip_case_version_id, position, title, day_from, day_to,"
        " source_tour_id) VALUES ($1::uuid, 1, 'Invented Lake Loop', 1, 2, $2)",
        version,
        GONE_KEY,
    )
    try:
        async with pool.acquire() as conn, conn.transaction():
            assert await lifecycle.tour_title(conn, TOUR_KEY) == "Invented Ridge Trek"
            assert await lifecycle.tour_title(conn, GONE_KEY) == "Invented Lake Loop"
            assert await lifecycle.tour_title(conn, str(uuid.UUID(int=0))) is None
    finally:
        await admin.execute(
            "DELETE FROM booking.trip_case_version_itinerary WHERE trip_case_version_id = $1::uuid", version
        )
        await admin.execute("DELETE FROM booking.trip_case_versions WHERE id = $1::uuid", version)
        await admin.execute("DELETE FROM booking.trip_cases WHERE id = $1::uuid", case["id"])
        await admin.execute("DELETE FROM catalog.v_tour WHERE source_id = $1", TOUR_KEY)
        liveness.reset_cache()
