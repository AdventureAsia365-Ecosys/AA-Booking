"""AA-996 perf guard on a real PostgreSQL (after #130 / hotfix #131): the trip-plans list and detail read only the
itineraries of the tours they show, and the list issues as many statements for 25 rows as for 2.

The stub's parse-backed views have the live plan shape (``stub_catalog.sql``): ``catalog._tour_day_text`` is
``DISTINCT ON (tour_id, day_number)`` behind a security barrier over the test day rows plus ``NOISE_ROWS`` days of
tours outside ``catalog.v_tour``. ``EXPLAIN ANALYZE`` of each catalog statement a request ran tells how many itinerary
rows it fed into the parse: a read keyed by ``tour_id`` reads only the page's days; the #130 read (``source_id``, per
row) reads the noise once per page row. ``test_the_guard_fails_the_130_list_statement`` runs the #130 statement to
show the check fires on it."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import asyncpg
import httpx
import pytest
import pytest_asyncio

from app.catalog import trip_plans
from app.db import SLOW_QUERY_MS, Database
from app.main import create_app
from app.settings import Settings
from tests.catalog_perf import (
    NOISE_ROWS,
    PR130_LIST_SQL,
    QueryRecorder,
    Statement,
    explain,
    parse_rows,
    plan_findings,
)
from tests.staff_tokens import LocalCis

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ADMIN = "it-996g-admin"
COUNTRY = "Perfland"
TOURS = 25
DAYS = 3
KEYS = [f"aa996a00-0000-4000-8000-{i:012d}" for i in range(TOURS)]


async def _clean(conn: asyncpg.Connection) -> None:
    await conn.execute("SET ROLE bk_owner")
    try:
        await conn.execute("DELETE FROM booking.trip_plans WHERE source_id = ANY($1)", KEYS)
    finally:
        await conn.execute("RESET ROLE")
    await conn.execute("DELETE FROM catalog.stub_parsed_day WHERE source_id = ANY($1)", KEYS)
    await conn.execute("DELETE FROM catalog.v_tour_photo WHERE source_id = ANY($1)", KEYS)
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
        [(key, f"Perf tour {i:02d}", COUNTRY, DAYS) for i, key in enumerate(KEYS)],
    )
    await conn.executemany(
        "INSERT INTO catalog.v_tour_day (source_id, day_number, title, places) VALUES ($1, $2, $3, $4)",
        [(key, d, f"Day {d}", [f"Place {i}-{d}", "Shared town"]) for i, key in enumerate(KEYS) for d in range(1, 4)],
    )
    await conn.executemany(
        "INSERT INTO catalog.v_tour_photo (source_id, path, position) VALUES ($1, $2, 1)",
        [(key, f"/public/photos/perf-{i}") for i, key in enumerate(KEYS) if i % 2 == 0],
    )
    await conn.execute("SET ROLE bk_owner")
    try:
        await conn.executemany(
            "INSERT INTO booking.trip_plans (source_id, sellable) VALUES ($1, true)", [(k,) for k in KEYS[::3]]
        )
    finally:
        await conn.execute("RESET ROLE")
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
    """EXPLAIN runs as role booking: the grants and the security-barrier views are what the API meets."""
    conn = await asyncpg.connect(migrated_booking_dsn)
    try:
        yield conn
    finally:
        await conn.close()


async def _get(client: tuple[httpx.AsyncClient, dict[str, str]], recorder: QueryRecorder, path: str) -> httpx.Response:
    c, headers = client
    recorder.clear()
    response = await c.get(path, headers=headers)
    await asyncio.sleep(0)  # asyncpg hands query log records over with call_soon
    return response


async def _parse_budget(conn: asyncpg.Connection, statements: list[Statement]) -> tuple[int, list[str]]:
    """Itinerary rows fed into the parse by the catalog SELECTs of a request, and the unfiltered scans found."""
    rows, findings = 0, []
    for statement in statements:
        if not statement.flat.upper().startswith(("SELECT", "WITH")):
            continue
        plan = await explain(conn, statement, analyze=True)
        rows += parse_rows(plan)
        findings += plan_findings(plan)
    return rows, findings


async def test_the_list_parses_only_the_page_itineraries(
    client: tuple[httpx.AsyncClient, dict[str, str]], catalog_read_guard: QueryRecorder, booking: asyncpg.Connection
) -> None:
    response = await _get(client, catalog_read_guard, f"/catalog/trip-plans?country={COUNTRY}&limit={TOURS}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["items"]) == TOURS and body["degraded"] == []
    assert all(len(item["destinations"]) == DAYS + 1 for item in body["items"])  # 3 own places + the shared town
    statements = catalog_read_guard.catalog()
    assert any(s.parse_backed for s in statements)
    rows, findings = await _parse_budget(booking, statements)
    assert findings == []
    assert rows <= TOURS * DAYS < NOISE_ROWS, f"the list fed {rows} itinerary rows into the parse"
    # Timing budget: no statement near the slow_query budget even on the small stub.
    slow = [(round(s.elapsed * 1000), s.flat[:120]) for s in catalog_read_guard.statements if s.elapsed * 1000 >= 250]
    assert slow == [], f"statements over 250 ms (budget {SLOW_QUERY_MS} ms on Dev): {slow}"


async def test_the_list_statement_count_does_not_grow_with_the_page(
    client: tuple[httpx.AsyncClient, dict[str, str]], catalog_read_guard: QueryRecorder
) -> None:
    counts = {}
    await _get(client, catalog_read_guard, "/catalog/trip-plans?limit=1")  # first call: staff sign-in statements
    for limit in (2, TOURS):
        response = await _get(client, catalog_read_guard, f"/catalog/trip-plans?country={COUNTRY}&limit={limit}")
        assert response.status_code == 200 and len(response.json()["items"]) == limit
        counts[limit] = (len(catalog_read_guard.statements), len(catalog_read_guard.catalog()))
    assert counts[2] == counts[TOURS], f"statements (all, catalog) per request by page size: {counts}"
    assert counts[TOURS][1] <= 4  # count, page, destinations, cover photo


async def test_the_detail_parses_only_its_own_itinerary(
    client: tuple[httpx.AsyncClient, dict[str, str]], catalog_read_guard: QueryRecorder, booking: asyncpg.Connection
) -> None:
    response = await _get(client, catalog_read_guard, f"/catalog/trip-plans/{KEYS[0]}")
    assert response.status_code == 200, response.text
    assert [d["day_number"] for d in response.json()["days"]] == [1, 2, 3]
    rows, findings = await _parse_budget(booking, catalog_read_guard.catalog())
    assert findings == [] and rows == DAYS


async def test_the_guard_fails_the_130_list_statement(admin: asyncpg.Connection) -> None:
    # The #130 page (per-row LATERAL on v_tour_day by source_id): every page row parses the whole itinerary base.
    page = 5
    bad = await explain(admin, Statement(PR130_LIST_SQL, (page, 0), 0.0, None), analyze=True)
    assert plan_findings(bad), "the #130 statement must be reported"
    assert parse_rows(bad) >= NOISE_ROWS * page
    # The hotfix statement for the same tours reads their days only.
    tour_ids = [r["tour_id"] for r in await admin.fetch("SELECT tour_id::text FROM catalog.v_tour WHERE country = $1",
                                                        COUNTRY)]  # fmt: skip
    good = await explain(admin, Statement(trip_plans.DESTINATIONS_SQL, (tour_ids,), 0.0, None), analyze=True)
    assert plan_findings(good) == [] and parse_rows(good) == TOURS * DAYS
