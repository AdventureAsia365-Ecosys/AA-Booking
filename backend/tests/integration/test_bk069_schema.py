"""AA-1153 part 1 (bk-069) on a real PostgreSQL, as role ``booking`` where the app writes: registration and re-run
no-op, the policy seed, the backfilled and trigger-made supplier links, the link-based rate and restriction guards (a
second DMC prices a hotel it is linked to, never one it is not), seasons and hotel child prices (draft only, inside the
contract, a linked hotel), the append-only import log, the board CHECK on tour nights, the hotel confirmation number
and the facts CHECK on hotels, and the cascades an owner clean-up follows. All names are invented."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from tests.integration import ops
from tests.integration.supply import purge_supply

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[3] / "migrations" / "069_hotel_contracts_dmc.sql"
MAKER, CHECKER = "it-1153-maker", "it-1153-checker"
PARAMS = (
    ("hotel_match_block_pct", 85),
    ("hotel_match_block_m", 300),
    ("hotel_match_warn_pct", 60),
    ("hotel_match_warn_m", 1000),
    ("hotel_facts_reconfirm_days", 180),
    ("rate_sheet_jump_warn_pct", 60),
)
GUARD_ERRORS = (
    asyncpg.CheckViolationError,
    asyncpg.UniqueViolationError,
    asyncpg.InsufficientPrivilegeError,
    asyncpg.ObjectNotInPrerequisiteStateError,
)


async def _clean(conn: asyncpg.Connection) -> None:
    await purge_supply(conn)
    async with conn.transaction():
        await conn.execute("SET LOCAL ROLE bk_owner")
        await conn.execute("DELETE FROM booking.rate_sheet_imports")
        await conn.execute("DELETE FROM booking.supplier_contracts WHERE title LIKE 'it-1153 %'")
        await conn.execute("DELETE FROM booking.suppliers WHERE company_name LIKE 'it-1153 %'")


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    await _clean(conn)
    await ops.staff(conn, (MAKER, "contracting"), (CHECKER, "admin"))
    try:
        yield conn
    finally:
        await _clean(conn)
        await conn.close()


@pytest_asyncio.fixture
async def app_conn(admin: asyncpg.Connection, migrated_booking_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_booking_dsn)
    try:
        yield conn
    finally:
        await conn.close()


async def _supplier(conn: asyncpg.Connection, name: str) -> str:
    return await conn.fetchval(
        "INSERT INTO booking.suppliers (company_name, categories, country_code) VALUES ($1, ARRAY['hotel'], 'VN')"
        " RETURNING id::text",
        f"it-1153 {name}",
    )


async def _contract(conn: asyncpg.Connection, supplier: str, title: str) -> str:
    return await conn.fetchval(
        "INSERT INTO booking.supplier_contracts (supplier_id, title, valid_from, valid_to, currency, created_by,"
        " updated_by) VALUES ($1::uuid, $2, DATE '2027-01-01', DATE '2027-12-31', 'USD', $3, $3) RETURNING id::text",
        supplier,
        f"it-1153 {title}",
        MAKER,
    )


async def _graph(app: asyncpg.Connection) -> dict[str, str]:
    """Two DMCs; a hotel listed by the first, with one room type; a draft contract of each DMC."""
    a, b = await _supplier(app, "Lantern DMC"), await _supplier(app, "Harbour DMC")
    hotel = await app.fetchval(
        "INSERT INTO booking.hotels (supplier_id, name, created_by) VALUES ($1::uuid, 'Lotus Bay Lodge', $2)"
        " RETURNING id::text",
        a,
        MAKER,
    )
    room = await app.fetchval(
        "INSERT INTO booking.hotel_room_types (hotel_id, name) VALUES ($1::uuid, 'Deluxe') RETURNING id::text", hotel
    )
    return {
        "a": a,
        "b": b,
        "hotel": hotel,
        "room": room,
        "ka": await _contract(app, a, "A 2027"),
        "kb": await _contract(app, b, "B 2027"),
    }


async def _rate(app: asyncpg.Connection, contract: str, room: str, net: str = "56.00") -> str:
    return await app.fetchval(
        "INSERT INTO booking.supplier_rates (supplier_contract_id, hotel_room_type_id, unit, currency, valid_from,"
        " valid_to, meal_plan, net_amount, created_by) VALUES ($1::uuid, $2::uuid, 'room_night', 'USD',"
        " DATE '2027-01-01', DATE '2027-12-31', 'HB', $3::numeric, $4) RETURNING id::text",
        contract,
        room,
        net,
        MAKER,
    )


async def test_registered_once_and_rerun_is_a_no_op(admin: asyncpg.Connection) -> None:
    await admin.execute("SET ROLE bk_owner")
    try:
        await admin.execute(MIGRATION.read_text(encoding="utf-8"))
    finally:
        await admin.execute("RESET ROLE")
    assert await admin.fetchval("SELECT count(*) FROM shared.schema_versions WHERE version = 'bk-069'") == 1
    seeded = await admin.fetch(
        "SELECT p.param_key, p.value::text AS value, e.actor FROM booking.pricing_policy p"
        " JOIN booking.pricing_policy_events e ON e.policy_id = p.id AND e.event = 'approved'"
        " WHERE p.created_by = 'seed:bk-069' ORDER BY p.id"
    )
    assert [(r["param_key"], int(r["value"])) for r in seeded] == list(PARAMS)
    assert all(r["actor"] != "seed:bk-069" for r in seeded)


async def test_contract_file_without_a_directive() -> None:
    text = MIGRATION.read_text(encoding="utf-8")
    assert text.startswith("-- Migration bk-069")
    assert "-- migrate:" not in text


async def test_every_hotel_is_linked_to_its_own_supplier(app_conn: asyncpg.Connection) -> None:
    g = await _graph(app_conn)
    link = await app_conn.fetchrow(
        "SELECT supplier_id::text AS supplier_id, status, is_preferred, created_by FROM booking.hotel_supplier_links"
        " WHERE hotel_id = $1::uuid",
        g["hotel"],
    )
    assert dict(link) == {"supplier_id": g["a"], "status": "active", "is_preferred": False, "created_by": MAKER}
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app_conn.execute("DELETE FROM booking.hotel_supplier_links WHERE hotel_id = $1::uuid", g["hotel"])


async def test_link_rules(app_conn: asyncpg.Connection) -> None:
    g = await _graph(app_conn)
    insert = (
        "INSERT INTO booking.hotel_supplier_links (hotel_id, supplier_id, supplier_hotel_code, is_preferred, status,"
        " created_by) VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6)"
    )
    await app_conn.execute(insert, g["hotel"], g["b"], "hb-77", True, "active", MAKER)
    other = await app_conn.fetchval(
        "INSERT INTO booking.hotels (supplier_id, name) VALUES ($1::uuid, 'Quiet Pine Inn') RETURNING id::text", g["a"]
    )
    with pytest.raises(asyncpg.UniqueViolationError):  # the DMC's code names one hotel only (case and spaces ignored)
        await app_conn.execute(insert, other, g["b"], " HB-77 ", False, "active", MAKER)
    with pytest.raises(asyncpg.UniqueViolationError):  # one preferred DMC per hotel
        await app_conn.execute(
            "UPDATE booking.hotel_supplier_links SET is_preferred = true WHERE hotel_id = $1::uuid AND supplier_id ="
            " $2::uuid",
            g["hotel"],
            g["a"],
        )
    with pytest.raises(asyncpg.CheckViolationError):  # an inactive link is never preferred
        await app_conn.execute(
            "UPDATE booking.hotel_supplier_links SET status = 'inactive' WHERE hotel_id = $1::uuid"
            " AND supplier_id = $2::uuid",
            g["hotel"],
            g["b"],
        )


async def test_a_second_dmc_prices_a_linked_hotel_only(app_conn: asyncpg.Connection) -> None:
    g = await _graph(app_conn)
    with pytest.raises(asyncpg.CheckViolationError):  # DMC B is not linked yet
        await _rate(app_conn, g["kb"], g["room"])
    await app_conn.execute(
        "INSERT INTO booking.hotel_supplier_links (hotel_id, supplier_id, created_by) VALUES ($1::uuid, $2::uuid, $3)",
        g["hotel"],
        g["b"],
        MAKER,
    )
    assert await _rate(app_conn, g["kb"], g["room"])
    assert await _rate(app_conn, g["ka"], g["room"], "65.00")  # the first DMC keeps pricing its own hotel
    await app_conn.execute(
        "UPDATE booking.hotel_supplier_links SET status = 'inactive' WHERE hotel_id = $1::uuid AND supplier_id ="
        " $2::uuid",
        g["hotel"],
        g["b"],
    )
    with pytest.raises(asyncpg.CheckViolationError):  # an inactive link prices nothing new
        await _rate(app_conn, g["kb"], g["room"], "57.00")
    with pytest.raises(asyncpg.CheckViolationError):  # restrictions follow the same link rule
        await app_conn.execute(
            "INSERT INTO booking.supply_restrictions (supplier_contract_id, hotel_room_type_id, inventory_kind,"
            " date_from, date_to, status, created_by) VALUES ($1::uuid, $2::uuid, 'on_request', DATE '2027-02-01',"
            " DATE '2027-02-10', 'contract', $3)",
            g["kb"],
            g["room"],
            MAKER,
        )


async def test_seasons_and_child_prices_follow_the_draft(app_conn: asyncpg.Connection) -> None:
    g = await _graph(app_conn)
    season = (
        "INSERT INTO booking.supplier_contract_seasons (supplier_contract_id, name, date_from, date_to, created_by)"
        " VALUES ($1::uuid, $2, $3, $4, $5) RETURNING id::text"
    )
    child = (
        "INSERT INTO booking.hotel_child_policies (supplier_contract_id, hotel_id, age_min, age_max, basis, meal_plan,"
        " net_amount, created_by) VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, $7::numeric, $8) RETURNING id::text"
    )
    assert await app_conn.fetchval(season, g["ka"], "Low", date(2027, 5, 1), date(2027, 9, 30), MAKER)
    with pytest.raises(asyncpg.CheckViolationError):  # outside the contract validity
        await app_conn.fetchval(season, g["ka"], "High", date(2027, 10, 1), date(2028, 3, 31), MAKER)
    with pytest.raises(asyncpg.UniqueViolationError):
        await app_conn.fetchval(season, g["ka"], " low ", date(2027, 5, 1), date(2027, 6, 30), MAKER)
    assert await app_conn.fetchval(child, g["ka"], g["hotel"], 6, 11, "sharing", "HB", "15.00", MAKER)
    with pytest.raises(asyncpg.CheckViolationError):  # DMC B is not linked to the hotel
        await app_conn.fetchval(child, g["kb"], g["hotel"], 6, 11, "sharing", None, "15.00", MAKER)
    for bad in ((12, 11, "sharing", None), (0, 5, "cot", None), (0, 5, "sharing", "XX")):
        with pytest.raises(asyncpg.CheckViolationError):
            await app_conn.fetchval(child, g["ka"], g["hotel"], *bad, "0", MAKER)
    await app_conn.execute(
        "UPDATE booking.supplier_contracts SET status = 'published', published_by = $2, published_at = now()"
        " WHERE id = $1::uuid",
        g["ka"],
        CHECKER,
    )
    for sql, args in (
        (season, (g["ka"], "Peak", date(2027, 12, 20), date(2027, 12, 31), MAKER)),
        (child, (g["ka"], g["hotel"], 0, 5, "sharing", None, "0", MAKER)),
    ):
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):  # fixed once published
            await app_conn.fetchval(sql, *args)
    with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
        await app_conn.execute("DELETE FROM booking.supplier_contract_seasons WHERE supplier_contract_id = $1::uuid", g["ka"])


async def test_import_log_is_append_only(app_conn: asyncpg.Connection) -> None:
    g = await _graph(app_conn)
    row = await app_conn.fetchval(
        "INSERT INTO booking.rate_sheet_imports (supplier_id, supplier_contract_id, file_sha256, rate_rows, checks,"
        " diff, imported_by) VALUES ($1::uuid, $2::uuid, repeat('a', 64), 3, '[]', '{}', $3) RETURNING id::text",
        g["b"],
        g["kb"],
        MAKER,
    )
    for sql in (
        "UPDATE booking.rate_sheet_imports SET rate_rows = 4 WHERE id = $1::uuid",
        "DELETE FROM booking.rate_sheet_imports WHERE id = $1::uuid",
    ):
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await app_conn.execute(sql, row)
    with pytest.raises(asyncpg.CheckViolationError):
        await app_conn.execute(
            "INSERT INTO booking.rate_sheet_imports (supplier_id, supplier_contract_id, file_sha256, rate_rows,"
            " imported_by) VALUES ($1::uuid, $2::uuid, 'not-a-hash', 1, $3)",
            g["a"],
            g["ka"],
            MAKER,
        )


async def test_board_hotel_ref_and_facts_checks(app_conn: asyncpg.Connection) -> None:
    g = await _graph(app_conn)
    plan = await app_conn.fetchval(
        "INSERT INTO booking.trip_plans (source_system, source_id, sellable) VALUES ('cis', $1, false)"
        " ON CONFLICT (source_system, source_id) DO UPDATE SET sellable = false RETURNING id::text",
        "e1000000-0000-4000-8000-000000001153",
    )
    insert = (
        "INSERT INTO booking.trip_plan_components (trip_plan_id, day_no, hotel_room_type_id, service_id, meal_plan)"
        " VALUES ($1::uuid, $2, $3::uuid, $4::uuid, $5)"
    )
    await app_conn.execute(insert, plan, 1, g["room"], None, "HB")
    await app_conn.execute(insert, plan, 2, g["room"], None, None)
    service = await app_conn.fetchval(
        "INSERT INTO booking.services (supplier_id, service_type, title) VALUES ($1::uuid, 'meal', 'Riverside dinner')"
        " RETURNING id::text",
        g["a"],
    )
    for args in ((plan, 3, None, service, "HB"), (plan, 4, g["room"], None, "XB")):
        with pytest.raises(asyncpg.CheckViolationError):
            await app_conn.execute(insert, *args)
    before = await app_conn.fetchval("SELECT supply_version FROM booking.hotels WHERE id = $1::uuid", g["hotel"])
    for facts in (
        ("hotel_website", None, None, None),  # a source alone is fine
        ("dmc_sheet", g["b"], date(2027, 1, 5), MAKER),
    ):
        await app_conn.execute(
            "UPDATE booking.hotels SET facts_source = $2, facts_source_supplier_id = $3::uuid, facts_confirmed_on = $4,"
            " facts_confirmed_by = $5, external_codes = '{\"lantern\": \"LB-1\"}' WHERE id = $1::uuid",
            g["hotel"],
            *facts,
        )
    for bad in (
        ("site_visit", g["b"], None, None),  # a supplier only with a DMC sheet
        (None, None, date(2027, 1, 5), MAKER),  # a confirmation needs a source
        ("dmc_sheet", None, date(2027, 1, 5), None),  # confirmed on and by go together
        ("phone", None, None, None),
    ):
        with pytest.raises(asyncpg.CheckViolationError):
            await app_conn.execute(
                "UPDATE booking.hotels SET facts_source = $2, facts_source_supplier_id = $3::uuid,"
                " facts_confirmed_on = $4, facts_confirmed_by = $5 WHERE id = $1::uuid",
                g["hotel"],
                *bad,
            )
    version = await app_conn.fetchval("SELECT supply_version FROM booking.hotels WHERE id = $1::uuid", g["hotel"])
    assert version == before  # none of the property-master columns is a supply.hotel.v1 fact
    assert await app_conn.fetchval(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'supplier_bookings_hotel_ref_chk'"
    )


async def test_aliases_unique_per_room_type_and_supplier(app_conn: asyncpg.Connection) -> None:
    g = await _graph(app_conn)
    insert = (
        "INSERT INTO booking.hotel_room_type_aliases (room_type_id, supplier_id, alias, created_by)"
        " VALUES ($1::uuid, $2::uuid, $3, $4) RETURNING id::text"
    )
    alias = await app_conn.fetchval(insert, g["room"], g["b"], "Deluxe Bay View", MAKER)
    assert await app_conn.fetchval(insert, g["room"], None, "Deluxe Bay View", MAKER)  # any-DMC alias apart
    with pytest.raises(asyncpg.UniqueViolationError):
        await app_conn.fetchval(insert, g["room"], g["b"], " deluxe bay view ", MAKER)
    await app_conn.execute("DELETE FROM booking.hotel_room_type_aliases WHERE id = $1::uuid", alias)


async def test_owner_clean_up_cascades(admin: asyncpg.Connection, app_conn: asyncpg.Connection) -> None:
    g = await _graph(app_conn)
    await app_conn.execute(
        "INSERT INTO booking.supplier_contract_seasons (supplier_contract_id, name, date_from, date_to, created_by)"
        " VALUES ($1::uuid, 'Low', DATE '2027-05-01', DATE '2027-09-30', $2)",
        g["ka"],
        MAKER,
    )
    await app_conn.execute(
        "INSERT INTO booking.hotel_child_policies (supplier_contract_id, hotel_id, age_min, age_max, basis, net_amount,"
        " created_by) VALUES ($1::uuid, $2::uuid, 0, 5, 'sharing', 0, $3)",
        g["ka"],
        g["hotel"],
        MAKER,
    )
    await app_conn.execute(
        "UPDATE booking.supplier_contracts SET status = 'published', published_by = $2, published_at = now()"
        " WHERE id = $1::uuid",
        g["ka"],
        CHECKER,
    )
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE bk_owner")
        await admin.execute("DELETE FROM booking.hotel_room_types WHERE hotel_id = $1::uuid", g["hotel"])
        await admin.execute("DELETE FROM booking.hotels WHERE id = $1::uuid", g["hotel"])
        await admin.execute("DELETE FROM booking.supplier_contracts WHERE id = ANY($1::uuid[])", [g["ka"], g["kb"]])
    for table in ("hotel_supplier_links", "hotel_child_policies", "supplier_contract_seasons"):
        assert await admin.fetchval(f"SELECT count(*) FROM booking.{table} WHERE created_by = $1", MAKER) == 0  # noqa: S608
