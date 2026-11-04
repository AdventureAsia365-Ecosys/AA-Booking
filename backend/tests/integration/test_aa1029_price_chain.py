"""The AA-1029 R4 tour price chain on PostgreSQL 15 with the RDS role shape, as role ``booking`` (the app's login),
end to end with an invented fixture rate sheet (invented supplier, hotel, services and tour only):

1. ``bk_trip_plan_sync`` gives every ``catalog.v_tour`` key a plan (not sellable), adopts a plan staff created first,
   and keeps the CIS source price text as a reviewed reference when the reference view is readable (a stand-in table
   created and dropped around the test, lessons.md); a second run writes nothing.
2. The supplier rate-sheet workbook is checked (``dry_run``), imported into a draft contract by one staff member and
   published by a second; a b2b rule with an RRP markup is entered and approved; the plan gets its components.
3. ``bk_price_recompute`` fills the matrix: computed rows per window x pax band for b2b, the RRP, the dated history;
   ``booking.v_from_prices`` serves the from price to role ``tripplanner``; price parity reads it; a staff override
   is kept by the next recompute; a second recompute with nothing new changes nothing, on the same day or later ones
   (the windows are anchored on the rate validity).
4. A plan priced by hand only (b2b rows, no components) is recomputed too: its RRP is filled and follows a newly
   approved markup.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import asyncpg
import pytest
import pytest_asyncio

from app.auth.staff import Staff
from app.b2b import plan_components, reads
from app.catalog import cost_sheet, liveness, price_history, trip_plan_prices, trip_plan_sync
from app.errors import Invalid
from app.pricing import policy
from app.suppliers import service as suppliers
from app.supply import rate_sheet
from tests.integration import ops
from tests.integration.supply import purge_supply

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

TOUR = "aa102900-0000-4000-8000-000000000001"
STAFF_TOUR = "aa102900-0000-4000-8000-000000000002"
MAKER, CHECKER = "it-1029-maker", "it-1029-checker"
TODAY = datetime.now(UTC).date()
YEAR_END = TODAY + timedelta(days=200)
SUPPLIER = "it-1029 Lumen Ridge Travel Co"
REF_COLUMNS = "source_system text, source_id text, price_raw text"


def _staff(cis_user_id: str, role: str = "contracting") -> Staff:
    return Staff(cis_user_id, None, cis_user_id, role, True, "reviewer")


async def _purge(admin: asyncpg.Connection, plans_before: set[str]) -> None:
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE bk_owner")
        ours = [
            r["id"]
            for r in await admin.fetch(
                "SELECT id FROM booking.trip_plans WHERE source_id = ANY($1::text[]) OR synced_at IS NOT NULL",
                [TOUR, STAFF_TOUR],
            )
            if str(r["id"]) not in plans_before
        ]
        for table in ("trip_plan_price_history", "trip_plan_prices", "trip_plan_components"):
            await admin.execute(f"DELETE FROM booking.{table} WHERE trip_plan_id = ANY($1::uuid[])", ours)  # noqa: S608
        await admin.execute("DELETE FROM booking.trip_plans WHERE id = ANY($1::uuid[])", ours)
        await admin.execute("UPDATE booking.trip_plans SET synced_at = NULL WHERE synced_at IS NOT NULL")
        await admin.execute(
            "DELETE FROM booking.policy_approvals WHERE pricing_rule_id IN"
            " (SELECT id FROM booking.pricing_rules WHERE note = 'it-1029')"
        )
        await admin.execute("DELETE FROM booking.pricing_rules WHERE note = 'it-1029'")
    await purge_supply(admin)
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE bk_owner")
        await admin.execute(
            "DELETE FROM booking.supplier_contracts WHERE supplier_id IN"
            " (SELECT id FROM booking.suppliers WHERE company_name = $1)",
            SUPPLIER,
        )
        for table, column in (
            (
                "hotel_room_types",
                "hotel_id IN (SELECT id FROM booking.hotels WHERE supplier_id IN"
                " (SELECT id FROM booking.suppliers WHERE company_name = $1))",
            ),
            ("hotels", "supplier_id IN (SELECT id FROM booking.suppliers WHERE company_name = $1)"),
            ("services", "supplier_id IN (SELECT id FROM booking.suppliers WHERE company_name = $1)"),
            ("suppliers", "company_name = $1"),
        ):
            await admin.execute(f"DELETE FROM booking.{table} WHERE {column}", SUPPLIER)  # noqa: S608
    await admin.execute("DELETE FROM catalog.v_tour WHERE source_id = ANY($1::text[])", [TOUR, STAFF_TOUR])
    await admin.execute("DROP TABLE IF EXISTS catalog.v_tour_price_reference_v1")
    liveness.reset_cache()


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    plans_before = {str(r["id"]) for r in await conn.fetch("SELECT id FROM booking.trip_plans")}
    await ops.staff(conn, (MAKER, "contracting"), (CHECKER, "admin"))
    try:
        yield conn
    finally:
        await _purge(conn, plans_before)
        await conn.close()


@pytest_asyncio.fixture
async def pool(admin: asyncpg.Connection, migrated_booking_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    p = await asyncpg.create_pool(migrated_booking_dsn, min_size=0, max_size=3)
    try:
        yield p
    finally:
        await p.close()


async def _tours(admin: asyncpg.Connection) -> None:
    await admin.execute("DELETE FROM catalog.v_tour WHERE source_id = ANY($1::text[])", [TOUR, STAFF_TOUR])
    await admin.execute(
        "INSERT INTO catalog.v_tour (source_id, title, country, duration_days) VALUES"
        " ($1, 'Invented Ridge Trek', 'Vietnam', 3), ($2, 'Invented Lake Loop', 'Vietnam', 2)",
        TOUR,
        STAFF_TOUR,
    )
    liveness.reset_cache()


async def test_bridge_creates_adopts_and_keeps_the_reference(admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    await _tours(admin)
    async with pool.acquire() as conn:  # a plan staff created before the bridge
        await conn.execute(
            "INSERT INTO booking.trip_plans (source_id, sellable, trip_style) VALUES ($1, true, 'walking')", STAFF_TOUR
        )
    out = await trip_plan_sync.run(pool)
    assert out["outcome"] == "ok" and out["references"] == "skipped"
    assert out["created"] >= 1 and out["adopted"] >= 1
    rows = {
        r["source_id"]: dict(r)
        for r in await admin.fetch(
            "SELECT source_id, sellable, trip_style, synced_at, created_by FROM booking.trip_plans"
            " WHERE source_id = ANY($1::text[])",
            [TOUR, STAFF_TOUR],
        )
    }
    assert rows[TOUR]["sellable"] is False and rows[TOUR]["synced_at"] is not None and rows[TOUR]["created_by"] is None
    assert rows[STAFF_TOUR]["sellable"] is True and rows[STAFF_TOUR]["trip_style"] == "walking"
    again = await trip_plan_sync.run(pool)
    assert (again["created"], again["adopted"]) == (0, 0)

    # The CIS reference view (stand-in): the text is kept and parsed; a change clears the review.
    await admin.execute(f"CREATE TABLE catalog.v_tour_price_reference_v1 ({REF_COLUMNS})")
    await admin.execute("GRANT SELECT ON catalog.v_tour_price_reference_v1 TO booking")
    await admin.execute(
        "INSERT INTO catalog.v_tour_price_reference_v1 VALUES ('cis', $1, 'from USD 1,450 per person'),"
        " ('cis', $2, 'on request')",
        TOUR,
        STAFF_TOUR,
    )
    liveness.reset_cache()
    out = await trip_plan_sync.run(pool)
    assert out["references"] == "ok" and out["references_changed"] == 2
    async with pool.acquire() as conn, conn.transaction():
        listed = await trip_plan_prices.list_prices(conn, _staff(MAKER), TOUR)
        assert listed["reference"] == {
            "raw": "from USD 1,450 per person",
            "amount": "1450.00",
            "currency": "USD",
            "reviewed_by": None,
            "reviewed_at": None,
        }
        await trip_plan_prices.review_reference(conn, _staff(MAKER), TOUR)
        assert (await trip_plan_prices.list_prices(conn, _staff(MAKER), TOUR))["reference"]["reviewed_by"] == MAKER
        other = await trip_plan_prices.list_prices(conn, _staff(MAKER), STAFF_TOUR)
        assert other["reference"]["amount"] is None  # kept as text, nothing parsed
    assert (await trip_plan_sync.run(pool))["references_changed"] == 0
    await admin.execute(
        "UPDATE catalog.v_tour_price_reference_v1 SET price_raw = 'USD 1,500' WHERE source_id = $1", TOUR
    )
    assert (await trip_plan_sync.run(pool))["references_changed"] == 1
    async with pool.acquire() as conn:
        ref = (await trip_plan_prices.list_prices(conn, _staff(MAKER), TOUR))["reference"]
    assert (ref["amount"], ref["reviewed_by"]) == ("1500.00", None)


async def _supply(admin: asyncpg.Connection) -> dict[str, str]:
    sid = await admin.fetchval(
        "INSERT INTO booking.suppliers (company_name, categories, country_code) VALUES ($1,"
        " ARRAY['hotel','guide','transport'], 'VN') RETURNING id::text",
        SUPPLIER,
    )
    hotel = await admin.fetchrow(
        "INSERT INTO booking.hotels (supplier_id, name) VALUES ($1::uuid, 'Invented Ridge Lodge') RETURNING id::text,"
        " code",
        sid,
    )
    room = await admin.fetchval(
        "INSERT INTO booking.hotel_room_types (hotel_id, name, max_occupancy) VALUES ($1::uuid, 'Garden Twin', 2)"
        " RETURNING id::text",
        hotel["id"],
    )
    guide = await admin.fetchrow(
        "INSERT INTO booking.services (supplier_id, service_type, title, unit) VALUES ($1::uuid, 'guide',"
        " 'Invented ridge guide', 'group') RETURNING id::text, code",
        sid,
    )
    jeep = await admin.fetchrow(
        "INSERT INTO booking.services (supplier_id, service_type, title, unit) VALUES ($1::uuid, 'transfer',"
        " 'Invented valley jeep', 'person') RETURNING id::text, code",
        sid,
    )
    return {
        "supplier": sid,
        "hotel_code": hotel["code"],
        "room": room,
        "guide": guide["id"],
        "guide_code": guide["code"],
        "jeep": jeep["id"],
        "jeep_code": jeep["code"],
    }


def _sheet(s: dict[str, str]) -> bytes:
    """The invented fixture rate sheet: a twin room (double 100, single 80 per night), a guide (60 per group), a jeep
    (20 per person) in USD, Low season until day 90 and High after."""
    mid = TODAY + timedelta(days=90)
    return rate_sheet.build_workbook(
        {
            "Title": "Invented 2027 rates",
            "Valid from": TODAY.isoformat(),
            "Valid to": YEAR_END.isoformat(),
            "Currency": "USD",
        },
        hotel_rows=[
            (s["hotel_code"], "garden twin", "Low", TODAY, mid, "BB", 2, 2, 100, None),
            (s["hotel_code"], "Garden Twin", "Low", TODAY, mid, "BB", 1, 1, 80, None),
            (s["hotel_code"], "Garden Twin", "High", mid + timedelta(days=1), YEAR_END, "BB", 2, 2, 140, None),
            (s["hotel_code"], "Garden Twin", "High", mid + timedelta(days=1), YEAR_END, "BB", 1, 1, 110, None),
        ],
        service_rows={
            "Guide": [(s["guide_code"], None, None, None, None, None, 60, None)],
            "Transport": [(s["jeep_code"], None, None, None, None, None, 20, None)],
        },
    )


async def _rule(pool: asyncpg.Pool, markup: str = "0.25", valid_from: date = TODAY) -> None:
    async with pool.acquire() as conn, conn.transaction():
        rule = await policy.create_rule(
            conn,
            _staff(MAKER),
            {
                "channel": "b2b",
                "target_margin": Decimal("0.15"),
                "floor_margin": Decimal("0.10"),
                "peak_uplift_pts": Decimal("0"),
                "card_fee_pct": Decimal("0.03"),
                "valid_from": valid_from,
                "note": "it-1029",
                "rrp_markup_pct": Decimal(markup),
            },
            today=TODAY,
        )
        await policy.approve_rule(conn, _staff(CHECKER, "admin"), rule["id"], today=TODAY)


async def test_rate_sheet_to_from_price(admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    await _tours(admin)
    assert (await trip_plan_sync.run(pool))["outcome"] == "ok"
    s = await _supply(admin)
    data = _sheet(s)

    # 1. The fixture sheet: a dry run writes nothing, an unknown item is refused with its row, the import makes a draft.
    async with pool.acquire() as conn, conn.transaction():
        preview = await rate_sheet.import_sheet(conn, _staff(MAKER), s["supplier"], data, dry_run=True)
    assert len(preview["rates"]) == 6 and "imported" not in preview
    assert (
        await admin.fetchval(
            "SELECT count(*) FROM booking.supplier_contracts WHERE supplier_id = $1::uuid", s["supplier"]
        )
        == 0
    )
    bad = rate_sheet.build_workbook(
        {"Title": "x", "Valid from": TODAY.isoformat(), "Currency": "USD"},
        service_rows={
            "Guide": [("SVC-999999", None, None, None, None, None, 1, None)],
            "Meal": [(s["guide_code"], None, None, None, None, None, 1, None)],
        },
    )
    async with pool.acquire() as conn:
        with pytest.raises(Invalid) as exc:
            async with conn.transaction():
                await rate_sheet.import_sheet(conn, _staff(MAKER), s["supplier"], bad, dry_run=False)
    assert {(e["sheet"], e["code"]) for e in exc.value.extra["errors"]} == {
        ("Guide", "unknown_item"),
        ("Meal", "wrong_sheet"),
    }
    async with pool.acquire() as conn, conn.transaction():
        done = await rate_sheet.import_sheet(conn, _staff(MAKER), s["supplier"], data, dry_run=False)
    assert done["imported"] == 6 and done["contract"]["status"] == "draft"
    async with pool.acquire() as conn, conn.transaction():
        await suppliers.update_contract(conn, _staff(CHECKER, "admin"), done["contract"]["id"], {"status": "published"})

    # 2. The plan's components and the b2b rule with its RRP markup.
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
    await _rule(pool)

    # 3. The cost sheet.
    out = await cost_sheet.run(pool)
    assert out["failed"] == 0 and "b2b" in out["rules"] and out["rrp_markup"] == "0.2500"
    rows = [
        dict(r)
        for r in await admin.fetch(
            "SELECT pr.channel, pr.season, pr.valid_from, pr.valid_to, pr.pax_min, pr.pax_max, pr.amount,"
            " pr.rrp_amount,"
            " pr.net_amount, pr.computed FROM booking.trip_plan_prices pr"
            " JOIN booking.trip_plans p ON p.id = pr.trip_plan_id"
            " WHERE p.source_id = $1 ORDER BY pr.position",
            TOUR,
        )
    ]
    assert rows and all(r["computed"] for r in rows)
    # A fixed-departure rule in force prices the same nets without an RRP (the RRP is b2b policy, b15).
    assert all(r["rrp_amount"] is None for r in rows if r["channel"] != "b2b")
    rows = [r for r in rows if r["channel"] == "b2b"]
    low2 = next(r for r in rows if r["pax_min"] == 2 and r["valid_from"] == TODAY)
    # 2 nights x double 100 + guide 60 + jeep 2 x 20 = 300 / 2 = 150 net; 150 / 0.82 = 182.93 -> 185; RRP 231.25 -> 235
    assert (low2["net_amount"], low2["amount"], low2["rrp_amount"], low2["season"]) == (
        Decimal("150.00"),
        Decimal("185.00"),
        Decimal("235.00"),
        "Low",
    )
    assert low2["valid_to"] == TODAY + timedelta(days=89)  # the day-2 night of a start on day 90 is High
    assert {(r["pax_min"], r["pax_max"]) for r in rows} == {(1, 1), (2, 2), (3, 4), (5, 6), (7, 10), (11, None)}

    await admin.execute("SET ROLE tripplanner")
    try:
        view = await admin.fetchrow(
            "SELECT amount::text AS amount, currency, basis, pax, season, price_kind FROM booking.v_from_prices"
            " WHERE source_system = 'cis' AND source_id = $1",
            TOUR,
        )
    finally:
        await admin.execute("RESET ROLE")
    assert dict(view) == {
        "amount": "235.00",
        "currency": "USD",
        "basis": "per_person",
        "pax": 2,
        "season": "Low",
        "price_kind": "rrp",
    }

    plan_id = await admin.fetchval("SELECT id::text FROM booking.trip_plans WHERE source_id = $1", TOUR)
    async with pool.acquire() as conn:
        history = await price_history.as_of(conn, plan_id, datetime.now(UTC))
        assert len(history) >= len(rows) and all(h["change_source"] == "cost_sheet" for h in history)
    assert (await cost_sheet.run(pool))["changed"] == 0  # nothing new: nothing written

    # The next day with the same rates: the windows are anchored on the rate validity, so nothing moves (no history
    # copy, no updated_at bump for the products feed).
    trail = "SELECT count(*) FROM booking.trip_plan_price_history WHERE trip_plan_id = $1::uuid"
    stamp = "SELECT updated_at FROM booking.trip_plans WHERE id = $1::uuid"
    history_rows, updated_at = await admin.fetchval(trail, plan_id), await admin.fetchval(stamp, plan_id)
    for later in (1, 2, 30):
        out = await cost_sheet.run(pool, plan_id, on=TODAY + timedelta(days=later))
        assert (out["changed"], out["unchanged"], out["failed"]) == (0, 1, 0)
    assert await admin.fetchval(trail, plan_id) == history_rows
    assert await admin.fetchval(stamp, plan_id) == updated_at

    # Price parity reads the RRP (the plan must be sellable for the B2B read).
    await admin.execute("UPDATE booking.trip_plans SET sellable = true WHERE id = $1::uuid", plan_id)
    parity = await reads.price_parity(pool, source_id=TOUR, currency="USD", on=TODAY)
    assert parity["from_price"] == {"amount": "235.00", "currency": "USD"}

    # 4. A staff override is a hand row: the next recompute keeps it and the history dates both sets.
    async with pool.acquire() as conn, conn.transaction():
        listed = await trip_plan_prices.list_prices(conn, _staff(MAKER), TOUR)
        items = [
            {
                k: v
                for k, v in r.items()
                if k
                in (
                    "channel",
                    "season",
                    "peak",
                    "valid_from",
                    "valid_to",
                    "pax_min",
                    "pax_max",
                    "currency",
                    "net_amount",
                    "net_currency",
                    "computed",
                )
            }
            | {"amount": Decimal("190.00") if (r["pax_min"], r["valid_from"]) == (2, TODAY) else None}
            for r in listed["items"]
        ]
        for item in items:
            item["net_amount"] = Decimal(item["net_amount"])
        await trip_plan_prices.replace_prices(conn, _staff(MAKER), TOUR, items)
    before = datetime.now(UTC)
    await cost_sheet.run(pool)
    kept = await admin.fetchrow(
        "SELECT amount, rrp_amount, overridden, computed FROM booking.trip_plan_prices WHERE trip_plan_id = $1::uuid"
        " AND pax_min = 2 AND valid_from = $2",
        plan_id,
        TODAY,
    )
    assert dict(kept) == {
        "amount": Decimal("190.00"),
        "rrp_amount": Decimal("240.00"),
        "overridden": True,
        "computed": True,
    }
    async with pool.acquire() as conn:
        changes = await price_history.changes(conn, plan_id)
        old = await price_history.as_of(conn, plan_id, before - timedelta(seconds=30))
    assert [c["change_source"] for c in changes][:2] == ["staff", "cost_sheet"]
    assert any(r["amount"] == Decimal("185.00") and r["pax_min"] == 2 for r in old) or old == []


async def test_recompute_without_rates_or_rule_writes_nothing(admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    await _tours(admin)
    await trip_plan_sync.run(pool)
    out = await cost_sheet.run(pool, on=date(2031, 1, 1))
    assert out["failed"] == 0 and out["changed"] == 0


async def test_a_plan_priced_by_hand_gets_its_rrp_and_follows_a_new_markup(
    admin: asyncpg.Connection, pool: asyncpg.Pool
) -> None:
    """A plan with b2b rows entered by staff and no components: a row written before bk-039 (no RRP) gets one, and a
    newly approved markup re-prices the RRP that booking.v_from_prices serves."""
    await _tours(admin)
    await trip_plan_sync.run(pool)
    await _rule(pool)
    hand = {
        "channel": "b2b",
        "season": "Low",
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
        await trip_plan_prices.replace_prices(conn, _staff(MAKER), STAFF_TOUR, [hand])
    plan_id = await admin.fetchval("SELECT id::text FROM booking.trip_plans WHERE source_id = $1", STAFF_TOUR)
    async with admin.transaction():  # a row written before bk-039 has no RRP
        await admin.execute("SET LOCAL ROLE bk_owner")
        await admin.execute(
            "UPDATE booking.trip_plan_prices SET rrp_amount = NULL WHERE trip_plan_id = $1::uuid", plan_id
        )

    async def served() -> str | None:
        await admin.execute("SET ROLE tripplanner")
        try:
            return await admin.fetchval(
                "SELECT amount::text AS amount FROM booking.v_from_prices"
                " WHERE source_system = 'cis' AND source_id = $1",
                STAFF_TOUR,
            )
        finally:
            await admin.execute("RESET ROLE")

    assert await served() is None  # a hand row without an RRP: no from price
    out = await cost_sheet.run(pool, plan_id)
    assert (out["plans"], out["changed"], out["reasons"]) == (1, 1, {"no_components": 1})
    assert await served() == "500.00"  # 400 x 1.25

    await _rule(pool, markup="0.40", valid_from=TODAY + timedelta(days=1))
    assert (await cost_sheet.run(pool, plan_id, on=TODAY + timedelta(days=1)))["changed"] == 1
    assert await served() == "560.00"  # 400 x 1.40
    row = await admin.fetchrow(
        "SELECT amount, computed, created_by FROM booking.trip_plan_prices WHERE trip_plan_id = $1::uuid", plan_id
    )
    assert dict(row) == {"amount": Decimal("400.00"), "computed": False, "created_by": MAKER}  # the hand row kept

    # The job's plan list includes the plan (no component, no computed row: only its b2b row).
    listed = {r["source_id"] for r in await admin.fetch(cost_sheet.PLANS_SQL, None)}
    assert STAFF_TOUR in listed
