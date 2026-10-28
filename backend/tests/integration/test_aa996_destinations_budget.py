"""AA-996 S255 on a real PostgreSQL: a trip-plans page of MAX_PAGE (100) rows reads its destinations in bounded
chunks, each keyed by ``tour_id`` and parsing only its own tours' days, and a second read of the page comes from the
per-process cache.

Live check 28/10 (role booking on Dev): one ``catalog.v_tour_place`` read for 100 tours took 994 ms against the
1 s statement_timeout of a list extra (380 ms for 25). The stub cannot be made as slow as Dev; what it checks is the
shape that keeps every statement inside the budget: at most ``DESTINATION_CHUNK`` tours a statement, each under its
own timeout, the plan keyed by tour_id, and no statement near the slow-query budget."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import asyncpg
import httpx
import pytest
import pytest_asyncio

from app.catalog import trip_plans
from app.db import Database
from app.main import create_app
from app.settings import Settings
from tests.catalog_perf import QueryRecorder, explain, parse_rows, plan_findings
from tests.staff_tokens import LocalCis

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ADMIN = "it-996b-admin"
COUNTRY = "Budgetland"
TOURS = trip_plans.MAX_PAGE
DAYS = 2
KEYS = [f"aa996b00-0000-4000-8000-{i:012d}" for i in range(TOURS)]


async def _clean(conn: asyncpg.Connection) -> None:
    await conn.execute("DELETE FROM catalog.stub_parsed_day WHERE source_id = ANY($1)", KEYS)
    await conn.execute("DELETE FROM catalog.v_tour WHERE source_id = ANY($1)", KEYS)


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    await _clean(conn)
    await conn.execute(
        "INSERT INTO booking.staff_users (cis_user_id, role, active, name) VALUES ($1, 'admin', true, $1)"
        " ON CONFLICT (cis_user_id) DO UPDATE SET role = 'admin', active = true",
        ADMIN,
    )
    await conn.executemany(
        "INSERT INTO catalog.v_tour (source_id, title, country, duration_days) VALUES ($1, $2, $3, $4)",
        [(key, f"Budget tour {i:03d}", COUNTRY, DAYS) for i, key in enumerate(KEYS)],
    )
    await conn.executemany(
        "INSERT INTO catalog.v_tour_day (source_id, day_number, title, places) VALUES ($1, $2, $3, $4)",
        [(key, d, f"Day {d}", [f"Town {i}-{d}"]) for i, key in enumerate(KEYS) for d in range(1, DAYS + 1)],
    )
    try:
        yield conn
    finally:
        await _clean(conn)
        await conn.close()


@pytest_asyncio.fixture
async def client(
    admin: asyncpg.Connection, migrated_booking_dsn: str
) -> AsyncIterator[tuple[httpx.AsyncClient, dict[str, str]]]:
    settings = Settings(database_url=migrated_booking_dsn, db_ssl="disable", environment="test", git_sha="ci")
    db, cis = Database(settings), LocalCis()
    app = create_app(settings, db, cis.verifier())
    headers = {"Authorization": f"Bearer {cis.token(sub=ADMIN, cis_role='reviewer')}"}
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://booking/booking") as c:
            yield c, headers
    finally:
        await db.close()


@pytest_asyncio.fixture
async def booking(migrated_booking_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_booking_dsn)
    try:
        yield conn
    finally:
        await conn.close()


async def _list(client: tuple[httpx.AsyncClient, dict[str, str]], recorder: QueryRecorder) -> dict:
    c, headers = client
    recorder.clear()
    response = await c.get(f"/catalog/trip-plans?country={COUNTRY}&limit={TOURS}&sort=title", headers=headers)
    await asyncio.sleep(0)  # asyncpg hands query log records over with call_soon
    assert response.status_code == 200, response.text
    return response.json()


def _destination_reads(recorder: QueryRecorder) -> list:
    return [s for s in recorder.statements if "FROM catalog.v_tour_place" in s.flat]


async def test_a_full_page_reads_destinations_in_bounded_chunks(
    client: tuple[httpx.AsyncClient, dict[str, str]], catalog_read_guard: QueryRecorder, booking: asyncpg.Connection
) -> None:
    body = await _list(client, catalog_read_guard)
    assert len(body["items"]) == TOURS and body["degraded"] == []
    first = body["items"][0]
    assert first["title"] == "Budget tour 000" and first["destinations"] == ["Town 0-1", "Town 0-2"]
    reads = _destination_reads(catalog_read_guard)
    chunk = trip_plans.DESTINATION_CHUNK
    assert len(reads) == -(-TOURS // chunk)
    assert all(len(s.args[0]) <= chunk for s in reads)
    # every chunk ran under its own statement_timeout, inside the extra budget
    timeouts = [
        int(s.flat.removeprefix("SET LOCAL statement_timeout = "))
        for s in catalog_read_guard.statements
        if s.flat.startswith("SET LOCAL statement_timeout = ")
    ]
    assert len(timeouts) >= len(reads) and max(timeouts) <= trip_plans.EXTRA_TIMEOUT_MS
    for statement in reads:
        plan = await explain(booking, statement, analyze=True)
        assert plan_findings(plan) == []
        assert parse_rows(plan) <= chunk * DAYS  # only the chunk's own itinerary days reach the parse
    slow = [round(s.elapsed * 1000) for s in reads if s.elapsed * 1000 >= 250]
    assert slow == [], f"destination reads at or over 250 ms on the stub: {slow}"


async def test_the_second_read_of_a_page_takes_the_places_from_the_cache(
    client: tuple[httpx.AsyncClient, dict[str, str]], catalog_read_guard: QueryRecorder
) -> None:
    first = await _list(client, catalog_read_guard)
    assert len(_destination_reads(catalog_read_guard)) == -(-TOURS // trip_plans.DESTINATION_CHUNK)
    again = await _list(client, catalog_read_guard)
    assert _destination_reads(catalog_read_guard) == []
    assert [i["destinations"] for i in again["items"]] == [i["destinations"] for i in first["items"]]
    assert again["degraded"] == []
