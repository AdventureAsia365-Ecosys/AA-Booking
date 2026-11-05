"""AA-1029 price readiness on PostgreSQL 15 with the RDS role shape, as role ``booking``, on the price chain fixture of
``test_aa1029_price_chain`` (invented supplier, hotel, services and tours only):

1. Before any rate: the fixture tour lists its four components without a rate (codes, never a net), a plan with no
   component is ``no_components``; the fixture rate sheet's dry run names the fixture plan in its ``impact`` (every
   component rated once the draft is published: it moves to priced) and the plan that also uses an unrated service
   (stays ``missing_rates``), and writes nothing.
2. After the import, the publication and the cost sheet: the fixture tour is ``priced`` with its from price 235, the
   plan with one unrated component is ``missing_rates`` naming only that service, a plan with a hand row is
   ``hand_priced`` (hand rows win over ``no_components``); the summary adds up.
3. ``GET /booking/catalog/price-readiness``: JSON with the filters, the staff CSV with its fixed header and no net,
   403 without ``pricing.read``.
"""

from __future__ import annotations

import csv
import io
from collections.abc import AsyncIterator
from decimal import Decimal

import asyncpg
import httpx
import pytest
import pytest_asyncio

from app.b2b import plan_components
from app.catalog import cost_sheet, price_readiness, trip_plan_prices, trip_plan_sync
from app.db import Database
from app.main import create_app
from app.settings import Settings
from app.suppliers import service as suppliers
from app.supply import rate_sheet
from tests.integration import ops
from tests.integration.test_aa1029_price_chain import (  # noqa: F401 — fixtures used by name
    CHECKER,
    MAKER,
    STAFF_TOUR,
    TODAY,
    TOUR,
    YEAR_END,
    _rule,
    _sheet,
    _staff,
    _supply,
    _tours,
    admin,
    pool,
)
from tests.staff_tokens import LocalCis

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

BARE_TOUR = "aa102900-0000-4000-8000-000000000003"
WRITER = "it-1029-writer"


@pytest_asyncio.fixture
async def bare_tour(admin: asyncpg.Connection) -> AsyncIterator[None]:  # noqa: F811
    """A third invented tour (its plan is purged with the synced ones by the price chain fixture)."""
    await ops.staff(admin, (WRITER, "content_creator"))
    yield
    await admin.execute("DELETE FROM catalog.v_tour WHERE source_id = $1", BARE_TOUR)


async def _setup(admin: asyncpg.Connection, pool: asyncpg.Pool) -> dict[str, str]:  # noqa: F811
    await _tours(admin)
    await admin.execute(
        "INSERT INTO catalog.v_tour (source_id, title, country, duration_days) VALUES ($1, 'Invented Bare Valley',"
        " 'Laos', 4)",
        BARE_TOUR,
    )
    assert (await trip_plan_sync.run(pool))["outcome"] == "ok"
    s = await _supply(admin)
    boat = await admin.fetchrow(
        "INSERT INTO booking.services (supplier_id, service_type, title, unit) VALUES ($1::uuid, 'activity',"
        " 'Invented lake boat', 'person') RETURNING id::text, code",
        s["supplier"],
    )
    s["boat"], s["boat_code"] = boat["id"], boat["code"]
    s["supplier_code"] = await admin.fetchval("SELECT code FROM booking.suppliers WHERE id = $1::uuid", s["supplier"])
    async with pool.acquire() as conn, conn.transaction():
        await plan_components.replace_components(
            conn,
            _staff(MAKER),
            TOUR,
            [
                {"day_no": 1, "service_id": s["guide"]},
                {"day_no": 1, "hotel_room_type_id": s["room"]},
                {"day_no": 2, "service_id": s["jeep"]},
                {"day_no": 2, "hotel_room_type_id": s["room"]},
            ],
        )
        await plan_components.replace_components(
            conn,
            _staff(MAKER),
            STAFF_TOUR,
            [{"day_no": 1, "hotel_room_type_id": s["room"]}, {"day_no": 1, "service_id": s["boat"]}],
        )
    await _rule(pool)
    return s


def _by_key(report: dict) -> dict[str, dict]:
    return {r["tour_key"]: r for r in report["items"]}


async def test_readiness_and_the_rate_sheet_impact(
    admin: asyncpg.Connection,  # noqa: F811
    pool: asyncpg.Pool,  # noqa: F811
    bare_tour: None,
) -> None:
    s = await _setup(admin, pool)

    # 1. No rate yet.
    async with pool.acquire() as conn:
        before = _by_key(await price_readiness.build(conn, on=TODAY))
    tour = before[TOUR]
    assert (tour["status"], tour["reason"], tour["components"]) == ("missing_rates", "missing_rates", 4)
    assert {c["code"] for c in tour["components_without_rate"]} == {s["hotel_code"], s["guide_code"], s["jeep_code"]}
    room = next(c for c in tour["components_without_rate"] if c["kind"] == "hotel")
    assert room == {
        "day_no": 1,
        "kind": "hotel",
        "code": s["hotel_code"],
        "room_type": "Garden Twin",
        "supplier_code": s["supplier_code"],
    }
    assert (tour["title"], tour["country"], tour["duration_days"], tour["in_catalog"]) == (
        "Invented Ridge Trek",
        "Vietnam",
        3,
        True,
    )
    assert before[BARE_TOUR]["status"] == "no_components" and before[BARE_TOUR]["from_price"] is None

    async with pool.acquire() as conn, conn.transaction():
        preview = await rate_sheet.import_sheet(conn, _staff(MAKER), s["supplier"], _sheet(s), dry_run=True)
    impact = preview["impact"]
    assert impact["would_move_to_priced"] == 1 and impact["plans_total"] == 2 and not impact["truncated"]
    plans = {p["tour_key"]: p for p in impact["plans"]}
    assert impact["plans"][0]["tour_key"] == TOUR  # the plans that move to priced first
    assert {k: plans[TOUR][k] for k in ("status", "all_rated_after", "status_after", "moves_to_priced")} == {
        "status": "missing_rates",
        "all_rated_after": True,
        "status_after": "priced",
        "moves_to_priced": True,
    }
    assert plans[TOUR]["components_from_sheet"] == 4
    other = plans[STAFF_TOUR]
    assert (other["all_rated_after"], other["status_after"], other["moves_to_priced"]) == (
        False,
        "missing_rates",
        False,
    )
    assert [c["code"] for c in other["components_without_rate_after"]] == [s["boat_code"]]
    assert BARE_TOUR not in plans
    assert "net_amount" not in str(impact)
    count = "SELECT count(*) FROM booking.supplier_contracts WHERE supplier_id = $1::uuid"
    assert await admin.fetchval(count, s["supplier"]) == 0  # the dry run wrote nothing

    # 2. Import, publish (a second person), recompute.
    async with pool.acquire() as conn, conn.transaction():
        done = await rate_sheet.import_sheet(conn, _staff(MAKER), s["supplier"], _sheet(s), dry_run=False)
    assert "impact" not in done
    async with pool.acquire() as conn, conn.transaction():
        await suppliers.update_contract(conn, _staff(CHECKER, "admin"), done["contract"]["id"], {"status": "published"})
    assert (await cost_sheet.run(pool))["failed"] == 0
    hand = {
        "channel": "b2b",
        "season": None,
        "peak": False,
        "valid_from": TODAY,
        "valid_to": YEAR_END,
        "pax_min": 1,
        "pax_max": None,
        "currency": "USD",
        "amount": Decimal("400.00"),
        "net_amount": None,
        "net_currency": None,
    }
    async with pool.acquire() as conn, conn.transaction():
        await trip_plan_prices.replace_prices(conn, _staff(MAKER), BARE_TOUR, [hand])

    async with pool.acquire() as conn:
        report = await price_readiness.build(conn, on=TODAY)
    after = _by_key(report)
    assert after[TOUR]["status"] == "priced" and after[TOUR]["reason"] is None
    assert after[TOUR]["from_price"] == {"amount": "235.00", "currency": "USD", "season": "Low"}
    assert after[TOUR]["computed_rows"] > 0 and after[TOUR]["components_without_rate"] == []
    assert (after[STAFF_TOUR]["status"], after[STAFF_TOUR]["reason"]) == ("missing_rates", "missing_rates")
    assert [c["code"] for c in after[STAFF_TOUR]["components_without_rate"]] == [s["boat_code"]]
    assert (after[BARE_TOUR]["status"], after[BARE_TOUR]["reason"], after[BARE_TOUR]["hand_rows"]) == (
        "hand_priced",
        "no_components",
        1,
    )
    summary = report["summary"]
    assert summary["total"] == len(report["items"]) == sum(summary["by_status"].values())
    assert sum(c["total"] for c in summary["by_country"]) == summary["total"]
    laos = next(c for c in summary["by_country"] if c["country"] == "Laos")
    assert laos["hand_priced"] >= 1

    # The rate sheet again: the fixture plan is already priced, so nothing moves.
    async with pool.acquire() as conn, conn.transaction():
        again = await rate_sheet.import_sheet(conn, _staff(MAKER), s["supplier"], _sheet(s), dry_run=True)
    assert again["impact"]["would_move_to_priced"] == 0
    assert {p["tour_key"]: p["status"] for p in again["impact"]["plans"]}[TOUR] == "priced"


@pytest_asyncio.fixture
async def client(
    admin: asyncpg.Connection,  # noqa: F811
    migrated_booking_dsn: str,
) -> AsyncIterator[tuple[httpx.AsyncClient, LocalCis]]:
    cis = LocalCis()
    settings = Settings(database_url=migrated_booking_dsn, db_ssl="disable", environment="test", git_sha="ci")
    db = Database(settings)
    app = create_app(settings, db, cis.verifier())
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://booking/booking") as c:
            yield c, cis
    finally:
        await db.close()


async def test_the_route_json_csv_and_permission(
    admin: asyncpg.Connection,  # noqa: F811
    pool: asyncpg.Pool,  # noqa: F811
    bare_tour: None,
    client: tuple[httpx.AsyncClient, LocalCis],
) -> None:
    s = await _setup(admin, pool)
    async with pool.acquire() as conn, conn.transaction():
        done = await rate_sheet.import_sheet(conn, _staff(MAKER), s["supplier"], _sheet(s), dry_run=False)
    async with pool.acquire() as conn, conn.transaction():
        await suppliers.update_contract(conn, _staff(CHECKER, "admin"), done["contract"]["id"], {"status": "published"})
    await cost_sheet.run(pool)
    c, cis = client

    def auth(sub: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {cis.token(sub=sub, cis_role='reviewer')}"}

    url = "/catalog/price-readiness"
    assert (await c.get(url, headers=auth(WRITER))).status_code == 403  # content creators read no prices
    full = await c.get(url, headers=auth(MAKER))
    assert full.status_code == 200, full.text
    body = full.json()
    assert set(body) == {"today", "horizon_to", "rules", "fx_currencies", "summary", "items", "degraded"}
    assert body["degraded"] == [] and "b2b" in body["rules"]
    priced = (await c.get(url, params={"status": "priced", "country": "vietnam"}, headers=auth(MAKER))).json()
    assert TOUR in {r["tour_key"] for r in priced["items"]}
    assert all(r["status"] == "priced" for r in priced["items"])
    assert priced["summary"] == body["summary"]  # the summary counts every plan whatever the filters
    found = (await c.get(url, params={"q": "bare valley"}, headers=auth(MAKER))).json()
    assert [r["tour_key"] for r in found["items"]] == [BARE_TOUR]
    assert (await c.get(url, params={"status": "nope"}, headers=auth(MAKER))).status_code == 422

    got = await c.get(url, params={"format": "csv"}, headers=auth(MAKER))
    assert got.status_code == 200 and got.headers["content-type"].startswith("text/csv")
    assert got.headers["content-disposition"] == f'attachment; filename="aa-price-readiness-{TODAY.isoformat()}.csv"'
    lines = got.text.splitlines()
    assert lines[0] == (
        "tour_key,title,country,duration_days,sellable,status,reason,components,components_without_rate,missing_fx,"
        "computed_rows,hand_rows,from_price,from_currency,note"
    )
    rows = {r["tour_key"]: r for r in csv.DictReader(io.StringIO(got.text))}
    assert (rows[TOUR]["status"], rows[TOUR]["from_price"], rows[TOUR]["from_currency"]) == ("priced", "235.00", "USD")
    assert rows[STAFF_TOUR]["components_without_rate"] == f"day 1 {s['boat_code']} ({s['supplier_code']})"
    assert "150.00" not in got.text  # the fixture's net per person never leaves
