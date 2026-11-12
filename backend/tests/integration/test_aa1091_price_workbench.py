"""AA-1091 price setup workbench on PostgreSQL 15 with the RDS role shape, as role ``booking``, on the price chain
fixture of ``test_aa1029_price_chain`` (invented supplier, hotel, services, tours and itinerary text only):

1. The workbench lists every plan with its checks and next step (``add_components`` first).
2. Components suggested from the CIS itinerary: the hotel named in day 1's text, the same hotel for night 2 by its CIS
   destination (a ``v_tour_day_route`` stand-in), the guide, the jeep and the farm lunch by their title words; the
   airport transfers stay unmatched. Accepted suggestions saved with ``PUT …/components`` are then ``covered``.
3. The bulk workbook: a bad file is refused with every problem by sheet, row and column and writes nothing; the dry
   run previews per tour and writes nothing; the import writes components, hand prices, departures and the sellable
   switch through the single forms' services; a second import of the same file changes nothing.
4. Bulk actions: provisional price from the CIS reference (skipped once a b2b price exists), make sellable refused
   without components.
5. The routes: permissions, filters, the prefilled template round trip, the import as base64.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from decimal import Decimal

import asyncpg
import httpx
import pytest
import pytest_asyncio

from app.b2b import plan_components
from app.catalog import component_suggest, liveness, price_import, price_versions, price_workbench, trip_plan_sync
from app.db import Database
from app.errors import Invalid
from app.main import create_app
from app.settings import Settings
from app.suppliers import service as suppliers
from app.supply import rate_sheet
from tests.integration import ops
from tests.integration.price_versions_helper import publish_prices
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

WRITER = "it-1091-writer"
RIDGE_PLACE = str(uuid.UUID("aa109100-0000-4000-8000-0000000000d1"))
ROUTE_COLUMNS = "tour_id uuid, day_index integer, overnight_place text, overnight_destination_id uuid"
DAYS = (
    (
        1,
        "Arrive at the ridge",
        "Private transfer from the airport to Invented Ridge Lodge. Afternoon walk with your guide.",
    ),
    (2, "Valley jeep day", "Jeep ride through the valley with lunch at an invented farm."),
    (3, "Departure", "Transfer to the airport."),
)


@pytest_asyncio.fixture
async def world(admin: asyncpg.Connection) -> AsyncIterator[None]:  # noqa: F811
    """Departures, days and the route stand-in of this module; removed before the price chain fixture purges."""
    await ops.staff(admin, (WRITER, "content_creator"))
    yield
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE bk_owner")
        await admin.execute(
            "DELETE FROM booking.departures WHERE trip_plan_id IN"
            " (SELECT id FROM booking.trip_plans WHERE source_id = ANY($1::text[]))",
            [TOUR, STAFF_TOUR],
        )
    await admin.execute("DELETE FROM catalog.v_tour_day WHERE source_id = ANY($1::text[])", [TOUR, STAFF_TOUR])
    await admin.execute("DROP TABLE IF EXISTS catalog.v_tour_day_route")
    liveness.reset_cache()


async def _setup(admin: asyncpg.Connection, pool: asyncpg.Pool) -> dict[str, str]:  # noqa: F811
    await _tours(admin)
    for day, title, body in DAYS:
        await admin.execute(
            "INSERT INTO catalog.v_tour_day (source_id, day_number, title, body, places) VALUES ($1, $2, $3, $4, $5)",
            TOUR,
            day,
            title,
            body,
            ["Ridge Town"] if day < 3 else ["Airport"],
        )
    tour_id = await admin.fetchval("SELECT tour_id::text FROM catalog.v_tour WHERE source_id = $1", TOUR)
    await admin.execute(f"CREATE TABLE catalog.v_tour_day_route ({ROUTE_COLUMNS})")
    await admin.execute("GRANT SELECT ON catalog.v_tour_day_route TO booking")
    await admin.execute(
        "INSERT INTO catalog.v_tour_day_route VALUES ($1::uuid, 1, 'Ridge Town', $2::uuid),"
        " ($1::uuid, 2, 'Ridge Town', $2::uuid)",
        tour_id,
        RIDGE_PLACE,
    )
    liveness.reset_cache()
    assert (await trip_plan_sync.run(pool))["outcome"] == "ok"
    s = await _supply(admin)
    await admin.execute(
        "UPDATE booking.hotels SET country_code = 'VN', destination_id = $2::uuid WHERE code = $1",
        s["hotel_code"],
        RIDGE_PLACE,
    )
    lunch = await admin.fetchrow(
        "INSERT INTO booking.services (supplier_id, service_type, title, unit, country_code) VALUES ($1::uuid, 'meal',"
        " 'Invented farm lunch', 'person', 'VN') RETURNING id::text, code",
        s["supplier"],
    )
    s["lunch"], s["lunch_code"] = lunch["id"], lunch["code"]
    await _rule(pool)
    return s


def _needs(out: dict, day: int) -> dict[str, dict]:
    return {n["kind"]: n for n in out["days"][day - 1]["needs"]}


def _bad_workbook(s: dict[str, str]) -> bytes:
    return price_import.build_workbook(
        [{"tour_key": TOUR, "sellable": True}, {"tour_key": "no-such-tour"}],
        [{"source_id": STAFF_TOUR, "day_no": 1, "service_code": "SVC-999999"}],
        [
            {
                "source_id": STAFF_TOUR,
                "channel": "b2b",
                "valid_from": TODAY,
                "valid_to": YEAR_END,
                "pax_min": 1,
                "currency": "USD",
                "amount": Decimal("400"),
            },
            {
                "source_id": STAFF_TOUR,
                "channel": "b2b",
                "valid_from": TODAY,
                "valid_to": YEAR_END,
                "pax_min": 2,
                "currency": "USD",
                "amount": Decimal("390"),
            },
        ],
        [{"source_id": TOUR, "start_date": TODAY, "capacity": 10}],
    )


def _workbook(s: dict[str, str]) -> bytes:
    later = TODAY + timedelta(days=40)
    return price_import.build_workbook(
        [{"tour_key": TOUR, "sellable": True}, {"tour_key": STAFF_TOUR, "sellable": None}],
        [{"source_id": STAFF_TOUR, "day_no": 1, "hotel_code": s["hotel_code"], "room_type": "garden  twin"}],
        [
            {
                "source_id": STAFF_TOUR,
                "channel": "b2b",
                "season": "All year",
                "valid_from": TODAY,
                "valid_to": YEAR_END,
                "pax_min": 1,
                "pax_max": 2,
                "currency": "USD",
                "amount": Decimal("400"),
                "child_amount": Decimal("300"),
            },
            {
                "source_id": STAFF_TOUR,
                "channel": "b2b",
                "season": "All year",
                "valid_from": TODAY,
                "valid_to": YEAR_END,
                "pax_min": 3,
                "currency": "USD",
                "amount": Decimal("350"),
            },
        ],
        [
            {"source_id": TOUR, "start_date": later, "capacity": 12, "min_to_operate": 2},
            {"source_id": TOUR, "start_date": later + timedelta(days=7), "capacity": 12},
        ],
    )


async def _audits(conn: asyncpg.Connection, action: str) -> int:
    return await conn.fetchval("SELECT count(*) FROM booking.audit_logs WHERE action = $1", action)


async def test_workbench_suggestions_import_and_actions(
    admin: asyncpg.Connection,  # noqa: F811
    pool: asyncpg.Pool,  # noqa: F811
    world: None,
) -> None:
    s = await _setup(admin, pool)

    # 1. Nothing set up yet.
    async with pool.acquire() as conn:
        bench = await price_workbench.build(conn, on=TODAY)
    rows = {r["tour_key"]: r for r in bench["items"]}
    assert rows[TOUR]["next_step"] == "add_components" and "components" in rows[TOUR]["missing"]
    assert rows[TOUR]["checks"]["rule"] == {"ok": True}
    assert bench["summary"]["by_next_step"]["add_components"] >= 2
    async with pool.acquire() as conn:
        only = await price_workbench.build(conn, on=TODAY, missing_check="components", q="ridge trek")
    assert [r["tour_key"] for r in only["items"]] == [TOUR]

    # 2. Suggestions from the itinerary.
    async with pool.acquire() as conn:
        out = await component_suggest.build(conn, TOUR)
    assert out["degraded"] == [] and out["duration_days"] == 3
    day1, day2, day3 = _needs(out, 1), _needs(out, 2), _needs(out, 3)
    hotel = day1["hotel"]["suggestion"]
    assert hotel["code"] == s["hotel_code"] and hotel["hotel_room_type_id"] == s["room"]
    assert "named in the day's text" in hotel["reasons"] and hotel["confidence"] == "high"
    night2 = day2["hotel"]["suggestion"]
    assert night2["code"] == s["hotel_code"] and any("night's place" in r for r in night2["reasons"])
    assert "hotel" not in day3  # the last day has no night
    assert day1["guide"]["suggestion"]["service_id"] == s["guide"]
    assert day2["transfer"]["suggestion"]["service_id"] == s["jeep"]
    assert day2["meal"]["suggestion"]["service_id"] == s["lunch"] and day2["meal"]["keywords"] == ["lunch"]
    assert day1["transfer"]["status"] == "unmatched" and day3["transfer"]["status"] == "unmatched"
    assert {(u["day_no"], u["kind"]) for u in out["unmatched"]} == {(1, "transfer"), (3, "transfer")}
    assert out["counts"]["suggested"] == 5

    accepted = [
        {"day_no": 1, "hotel_room_type_id": hotel["hotel_room_type_id"]},
        {"day_no": 1, "service_id": day1["guide"]["suggestion"]["service_id"]},
        {"day_no": 2, "hotel_room_type_id": night2["hotel_room_type_id"]},
        {"day_no": 2, "service_id": day2["transfer"]["suggestion"]["service_id"]},
    ]
    async with pool.acquire() as conn, conn.transaction():
        await plan_components.replace_components(conn, _staff(MAKER), TOUR, accepted)
    async with pool.acquire() as conn:
        again = await component_suggest.build(conn, TOUR)
    assert _needs(again, 1)["hotel"]["status"] == "covered" and _needs(again, 2)["transfer"]["status"] == "covered"
    assert _needs(again, 2)["meal"]["status"] == "suggested"  # the lunch was rejected: still offered, never applied

    # 3. The workbook: a bad file writes nothing and names every problem.
    async with pool.acquire() as conn, conn.transaction():
        with pytest.raises(Invalid) as exc:
            await price_import.import_workbook(conn, _staff(MAKER), _bad_workbook(s), dry_run=False)
    errors = {(e["sheet"], e["row"], e["code"]) for e in exc.value.extra["errors"]}
    assert ("Tours", 3, "unknown_tour") in errors
    assert ("Components", 2, "unknown_item") in errors
    assert ("Prices", 3, "overlapping_prices") in errors
    assert ("Departures", 2, "invalid_start_date") in errors
    assert await admin.fetchval("SELECT count(*) FROM booking.audit_logs WHERE action = 'trip_plan.bulk_imported'") == 0

    async with pool.acquire() as conn, conn.transaction():
        preview = await price_import.import_workbook(conn, _staff(MAKER), _workbook(s), dry_run=True)
    tours = {t["tour_key"]: t for t in preview["tours"]}
    assert tours[STAFF_TOUR]["components_after"] == 1 and tours[STAFF_TOUR]["components_changed"]
    assert (tours[STAFF_TOUR]["hand_rows_after"], tours[STAFF_TOUR]["status_after"]) == (2, "hand_priced")
    assert (tours[TOUR]["sellable_before"], tours[TOUR]["sellable_after"]) == (False, True)
    assert len(tours[TOUR]["departures_new"]) == 2
    assert preview["totals"]["departures_new"] == 2
    count = "SELECT count(*) FROM booking.trip_plan_prices r JOIN booking.trip_plans p ON p.id = r.trip_plan_id"
    assert await admin.fetchval(count + " WHERE p.source_id = $1", STAFF_TOUR) == 0  # the dry run wrote nothing

    async with pool.acquire() as conn, conn.transaction():
        done = await price_import.import_workbook(conn, _staff(MAKER), _workbook(s), dry_run=False)
    assert done["dry_run"] is False and done["recomputed"] >= 1
    # AA-1091 (bk-052): the Prices sheet made a draft price version; nothing is live until a second person approves.
    (draft,) = [d for d in done["price_drafts"] if d["tour_key"] == STAFF_TOUR]
    assert draft["rows"] == 2 and draft["unchanged"] is False
    assert await admin.fetchval(count + " WHERE p.source_id = $1", STAFF_TOUR) == 0
    async with pool.acquire() as conn, conn.transaction():
        submitted = await price_versions.submit(conn, _staff(MAKER), draft["version_id"], "Workbook prices")
        await price_versions.approve(conn, _staff(CHECKER, "admin"), draft["version_id"], submitted["diff_sha256"])
    assert (
        await admin.fetchval("SELECT source FROM booking.price_list_versions WHERE id = $1::uuid", draft["version_id"])
        == "import"
    )
    hand = await admin.fetch(
        "SELECT r.amount, r.rrp_amount, r.child_amount, r.computed FROM booking.trip_plan_prices r"
        " JOIN booking.trip_plans p ON p.id = r.trip_plan_id WHERE p.source_id = $1 ORDER BY r.pax_min",
        STAFF_TOUR,
    )
    assert [(r["amount"], r["rrp_amount"], r["computed"]) for r in hand] == [
        (Decimal("400.00"), Decimal("500.00"), False),
        (Decimal("350.00"), Decimal("440.00"), False),  # RRP rounded up
    ]
    assert hand[0]["child_amount"] == Decimal("300.00")
    assert await admin.fetchval("SELECT sellable FROM booking.trip_plans WHERE source_id = $1", TOUR) is True
    deps = await admin.fetch(
        "SELECT d.capacity, d.min_to_operate FROM booking.departures d JOIN booking.trip_plans p ON p.id ="
        " d.trip_plan_id WHERE p.source_id = $1 ORDER BY d.start_date",
        TOUR,
    )
    assert [(d["capacity"], d["min_to_operate"]) for d in deps] == [(12, 2), (12, 0)]

    # The same file again changes nothing.
    marks = {a: await _audits(admin, a) for a in ("price_version.imported", "trip_plan.components_replaced")}
    async with pool.acquire() as conn, conn.transaction():
        second = await price_import.import_workbook(conn, _staff(MAKER), _workbook(s), dry_run=False)
    assert second["totals"]["departures_new"] == 0 and second["totals"]["departures_existing"] == 2
    assert second["totals"]["components_changed"] == 0 and second["totals"]["sellable_changes"] == 0
    assert {a: await _audits(admin, a) for a in marks} == marks

    # 4. Rates published: the tour prices; bulk actions.
    async with pool.acquire() as conn, conn.transaction():
        sheet = await rate_sheet.import_sheet(conn, _staff(MAKER), s["supplier"], _sheet(s), dry_run=False)
    async with pool.acquire() as conn, conn.transaction():
        await suppliers.update_contract(
            conn, _staff(CHECKER, "admin"), sheet["contract"]["id"], {"status": "published"}
        )
    async with pool.acquire() as conn, conn.transaction():
        recomputed = await price_workbench.apply_action(conn, _staff(MAKER), "recompute", [TOUR], on=TODAY)
    assert recomputed["done"] == 1
    async with pool.acquire() as conn:
        rows = {r["tour_key"]: r for r in (await price_workbench.build(conn, on=TODAY))["items"]}
    assert rows[TOUR]["status"] == "priced" and rows[TOUR]["next_step"] == "done"
    assert rows[TOUR]["checks"]["departures"]["count"] == 2 and rows[TOUR]["supplier_price"] is True

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE booking.trip_plans SET reference_price_raw = 'from USD 610', reference_amount = 610,"
            " reference_currency = 'USD' WHERE source_id = ANY($1::text[])",
            [TOUR, STAFF_TOUR],
        )
        await conn.execute("UPDATE booking.trip_plans SET sellable = false WHERE source_id = $1", STAFF_TOUR)
        await conn.execute(
            "DELETE FROM booking.trip_plan_prices WHERE trip_plan_id = (SELECT id FROM booking.trip_plans"
            " WHERE source_id = $1)",
            TOUR,
        )
    async with pool.acquire() as conn, conn.transaction():
        prov = await price_workbench.apply_action(
            conn, _staff(MAKER), "apply_provisional", [TOUR, STAFF_TOUR, "missing-key"], on=TODAY
        )
    results = {r["tour_key"]: r for r in prov["results"]}
    assert results[TOUR]["outcome"] == "done"
    assert (results[STAFF_TOUR]["outcome"], results[STAFF_TOUR]["reason"]) == ("skipped", "has_supplier_price")
    assert results["missing-key"]["reason"] == "no_trip_plan"
    async with pool.acquire() as conn:
        rows = {r["tour_key"]: r for r in (await price_workbench.build(conn, on=TODAY))["items"]}
    assert rows[TOUR]["provisional"] == {"amount": "610.00", "currency": "USD", "from_reference": True}
    assert rows[TOUR]["checks"]["price"] == {"ok": False, "kind": "provisional"}
    assert rows[TOUR]["reference"]["reviewed"] is True
    async with pool.acquire() as conn:
        assert (
            await conn.fetchval("SELECT count(*) FROM booking.v_from_prices WHERE source_id = $1", TOUR) == 0
        )  # never a public from price
    async with pool.acquire() as conn, conn.transaction():
        cleared = await price_workbench.apply_action(conn, _staff(MAKER), "clear_provisional", [TOUR], on=TODAY)
    assert cleared["done"] == 1

    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM booking.trip_plan_components WHERE trip_plan_id = (SELECT id FROM booking.trip_plans"
            " WHERE source_id = $1)",
            STAFF_TOUR,
        )
    async with pool.acquire() as conn, conn.transaction():
        sell = await price_workbench.apply_action(conn, _staff(MAKER), "make_sellable", [STAFF_TOUR], on=TODAY)
    assert sell["results"][0] == {"tour_key": STAFF_TOUR, "outcome": "skipped", "reason": "no_components"}


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


async def test_the_routes(
    admin: asyncpg.Connection,  # noqa: F811
    pool: asyncpg.Pool,  # noqa: F811
    world: None,
    client: tuple[httpx.AsyncClient, LocalCis],
) -> None:
    s = await _setup(admin, pool)
    c, cis = client
    async with pool.acquire() as conn, conn.transaction():
        await plan_components.replace_components(
            conn, _staff(MAKER), TOUR, [{"day_no": 1, "hotel_room_type_id": s["room"]}]
        )

    def auth(sub: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {cis.token(sub=sub, cis_role='reviewer')}"}

    url = "/catalog/price-workbench"
    assert (await c.get(url, headers=auth(WRITER))).status_code == 403
    got = await c.get(url, params={"missing": "components", "country": "vietnam"}, headers=auth(MAKER))
    assert got.status_code == 200, got.text
    listed = {r["tour_key"] for r in got.json()["items"]}
    assert STAFF_TOUR in listed and TOUR not in listed  # TOUR has a component now
    assert (await c.get(url, params={"missing": "nope"}, headers=auth(MAKER))).status_code == 422

    sug = await c.get(f"/catalog/trip-plans/{TOUR}/component-suggestions", headers=auth(MAKER))
    assert sug.status_code == 200 and sug.json()["counts"] == {"suggested": 4, "covered": 1, "unmatched": 2}
    assert (await c.get(f"/catalog/trip-plans/{TOUR}/component-suggestions", headers=auth(WRITER))).status_code == 403

    body = {"data_base64": base64.b64encode(_workbook(s)).decode(), "filename": "setup.xlsx"}
    assert (await c.post("/catalog/price-import", json=body, headers=auth(WRITER))).status_code == 403
    dry = await c.post("/catalog/price-import", params={"dry_run": "true"}, json=body, headers=auth(MAKER))
    assert dry.status_code == 200, dry.text
    assert dry.json()["dry_run"] is True
    bad = {"data_base64": base64.b64encode(_bad_workbook(s)).decode()}
    refused = await c.post("/catalog/price-import", params={"dry_run": "true"}, json=bad, headers=auth(MAKER))
    assert refused.status_code == 422 and refused.json()["detail"] == "price_import_invalid"
    imported = await c.post("/catalog/price-import", json=body, headers=auth(MAKER))
    assert imported.status_code == 200
    # AA-1091 (bk-052): the prices are a draft version until a second person approves it.
    (draft,) = [d for d in imported.json()["price_drafts"] if d["tour_key"] == STAFF_TOUR]
    sent = await c.post(
        f"/catalog/price-versions/{draft['version_id']}/submit", json={"reason": "Workbook"}, headers=auth(MAKER)
    )
    assert sent.status_code == 200, sent.text
    async with pool.acquire() as conn, conn.transaction():
        await price_versions.approve(conn, _staff(CHECKER, "admin"), draft["version_id"], sent.json()["diff_sha256"])

    # The prefilled template holds the state just imported; checking it back changes nothing.
    tpl = await c.get("/catalog/price-import/template", params={"q": "invented lake loop"}, headers=auth(MAKER))
    assert tpl.status_code == 200
    assert tpl.headers["content-type"].startswith("application/vnd.openxmlformats")
    sheets = price_import.parse(tpl.content)
    assert [t["tour_key"] for t in sheets.tours] == [STAFF_TOUR]
    assert len(sheets.components) == 1 and len(sheets.prices) == 2
    back = {"data_base64": base64.b64encode(tpl.content).decode()}
    check = await c.post("/catalog/price-import", params={"dry_run": "true"}, json=back, headers=auth(MAKER))
    assert check.status_code == 200, check.text
    assert check.json()["totals"]["components_changed"] == 0
    blank = await c.get("/catalog/price-import/template", params={"blank": "true"}, headers=auth(MAKER))
    assert price_import.parse(blank.content).problems[0].code == "no_rows"

    act = await c.post(
        f"{url}/actions", json={"action": "make_sellable", "tour_keys": [STAFF_TOUR]}, headers=auth(MAKER)
    )
    assert act.status_code == 200 and act.json()["done"] == 1
    assert (
        await c.post(f"{url}/actions", json={"action": "make_sellable", "tour_keys": [TOUR]}, headers=auth(WRITER))
    ).status_code == 403


async def test_import_keeps_net_cost_rows(
    admin: asyncpg.Connection,  # noqa: F811
    pool: asyncpg.Pool,  # noqa: F811
    world: None,
) -> None:
    """A row typed with a net cost on the single price form is not in the workbook: the prefilled export lists the
    plain hand rows only, and importing it back keeps the net cost, margin and override and writes nothing."""
    await _setup(admin, pool)
    base = {"channel": "b2b", "season": "All year", "peak": False, "valid_from": TODAY, "valid_to": YEAR_END}
    hand = {**base, "pax_min": 1, "pax_max": 2, "currency": "USD", "amount": Decimal("400.00"), "net_amount": None}
    net = {
        **base,
        "pax_min": 3,
        "pax_max": None,
        "currency": "USD",
        "amount": Decimal("330.00"),
        "net_amount": Decimal("250.00"),
        "net_currency": "USD",
    }
    async with pool.acquire() as conn, conn.transaction():
        await publish_prices(conn, STAFF_TOUR, [hand, net], maker=_staff(MAKER), checker=_staff(CHECKER, "admin"))
    rows_sql = (
        "SELECT pr.pax_min, pr.amount, pr.net_amount, pr.net_currency::text AS net_currency, pr.effective_margin,"
        " pr.priced_on, pr.overridden, pr.computed FROM booking.trip_plan_prices pr"
        " JOIN booking.trip_plans p ON p.id = pr.trip_plan_id WHERE p.source_id = $1 ORDER BY pr.pax_min"
    )
    before = [dict(r) for r in await admin.fetch(rows_sql, STAFF_TOUR)]
    assert before[1]["net_amount"] == Decimal("250.00") and before[1]["overridden"] is True
    imported = await _audits(admin, "price_version.imported")
    async with pool.acquire() as conn:
        items = [r for r in (await price_workbench.build(conn, on=TODAY))["items"] if r["tour_key"] == STAFF_TOUR]
        data = await price_import.prefilled(conn, items, today=TODAY)
    sheets = price_import.parse(data)
    assert [(p["pax_min"], p["amount"]) for p in sheets.prices] == [(1, Decimal("400"))]  # the plain hand row only
    async with pool.acquire() as conn, conn.transaction():
        out = await price_import.import_workbook(conn, _staff(MAKER), data, dry_run=False)
    (tour,) = out["tours"]
    assert (tour["hand_rows_before"], tour["hand_rows_after"], tour["net_rows_replaced"]) == (1, 1, 0)
    assert await _audits(admin, "price_version.imported") == imported  # nothing rewritten
    assert [dict(r) for r in await admin.fetch(rows_sql, STAFF_TOUR)] == before

    # A hand row in the file over the net-cost row replaces it, and the dry run says so.
    over = price_import.build_workbook(
        prices=[
            {"source_id": STAFF_TOUR, **base, "pax_min": 1, "pax_max": 2, "currency": "USD", "amount": Decimal(400)},
            {"source_id": STAFF_TOUR, **base, "pax_min": 3, "pax_max": None, "currency": "USD", "amount": Decimal(340)},
        ]
    )
    async with pool.acquire() as conn, conn.transaction():
        dry = await price_import.import_workbook(conn, _staff(MAKER), over, dry_run=True)
    assert dry["tours"][0]["net_rows_replaced"] == 1 and dry["tours"][0]["computed_rows_replaced"] == 0
    assert [dict(r) for r in await admin.fetch(rows_sql, STAFF_TOUR)] == before  # a dry run writes nothing


V2_COLUMNS = (
    "source_system text, source_id text, price_raw text, reference_amount numeric(12,2), reference_currency text,"
    " reference_basis text, reference_status text, reference_origin text, reference_reviewed_at timestamptz,"
    " reference_updated_at timestamptz"
)


async def test_structured_reference_from_cis(
    admin: asyncpg.Connection,  # noqa: F811
    pool: asyncpg.Pool,  # noqa: F811
) -> None:
    """CIS 402's v2 view (the CIS 402 column list) is read instead of v1: a confirmed per-person value wins over the
    parse, a per-group value gives no per-person amount, a value that needs review falls back to the parse, and an
    amount change alone counts."""
    await _tours(admin)
    await admin.execute(
        "CREATE TABLE catalog.v_tour_price_reference_v1 (source_system text, source_id text, price_raw text)"
    )
    await admin.execute("INSERT INTO catalog.v_tour_price_reference_v1 VALUES ('cis', $1, 'USD 999')", TOUR)
    await admin.execute(f"CREATE TABLE catalog.v_tour_price_reference_v2 ({V2_COLUMNS})")
    await admin.execute(
        "GRANT SELECT ON catalog.v_tour_price_reference_v1, catalog.v_tour_price_reference_v2 TO booking"
    )
    await admin.execute(
        "INSERT INTO catalog.v_tour_price_reference_v2 (source_system, source_id, price_raw, reference_amount,"
        " reference_currency, reference_basis, reference_status, reference_origin) VALUES"
        " ('cis', $1, 'from USD 1,450 pp', 1390, 'USD', 'per_person', 'confirmed', 'staff'),"
        " ('cis', $2, 'EUR 900 per group', 900, 'EUR', 'per_group', 'confirmed', 'staff')",
        TOUR,
        STAFF_TOUR,
    )
    liveness.reset_cache()
    out = await trip_plan_sync.run(pool)
    assert out["references"] == "ok" and out["references_source"] == "structured"
    assert out["references_changed"] == 2 and out["references_structured"] == 2
    ref = (
        "SELECT reference_price_raw AS raw, reference_amount, reference_currency::text AS c FROM booking.trip_plans"
        " WHERE source_id = $1"
    )
    assert tuple(await admin.fetchrow(ref, TOUR)) == ("from USD 1,450 pp", Decimal("1390.00"), "USD")
    assert tuple(await admin.fetchrow(ref, STAFF_TOUR)) == ("EUR 900 per group", None, None)  # no per-person amount
    assert (await trip_plan_sync.run(pool))["references_changed"] == 0
    async with pool.acquire() as conn, conn.transaction():
        await price_workbench.apply_action(conn, _staff(MAKER), "apply_provisional", [TOUR], on=TODAY)
    await admin.execute(
        "UPDATE catalog.v_tour_price_reference_v2 SET reference_status = 'needs_review', reference_amount = 1420"
        " WHERE source_id = $1",
        TOUR,
    )
    needs = await trip_plan_sync.run(pool)
    assert needs["references_changed"] == 1 and needs["references_structured"] == 1
    assert (await admin.fetchrow(ref, TOUR))["reference_amount"] == Decimal("1450.00")  # the parse decides
    await admin.execute(
        "UPDATE catalog.v_tour_price_reference_v2 SET reference_status = 'confirmed' WHERE source_id = $1", TOUR
    )
    assert (await trip_plan_sync.run(pool))["references_changed"] == 1
    row = await admin.fetchrow(
        "SELECT reference_amount, reference_reviewed_at, price_hint_amount FROM booking.trip_plans"
        " WHERE source_id = $1",
        TOUR,
    )
    assert row["reference_amount"] == Decimal("1420.00") and row["reference_reviewed_at"] is None
    assert row["price_hint_amount"] == Decimal("1390.00")  # the provisional hint is not moved by itself
    async with pool.acquire() as conn:
        bench = {r["tour_key"]: r for r in (await price_workbench.build(conn, on=TODAY))["items"]}
    assert bench[TOUR]["provisional"]["from_reference"] is False  # staff see it no longer matches the reference
