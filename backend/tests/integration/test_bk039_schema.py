"""AA-1029 (bk-039) on a real PostgreSQL: registration and re-run no-op, the new CHECKs, the grants (role ``booking``
writes the new columns and appends history rows, closing them only through ``effective_to``; ``tripplanner`` reads
``booking.v_from_prices`` and nothing else of schema ``booking``), the contract view's shape and row choice (the
cheapest RRP for 2 travellers still valid, USD first; never a net or an agency price), and the DM-6.1 tags.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[3] / "migrations" / "039_tour_price_chain.sql"
SOURCE = "aa102939-0000-4000-8000-000000000001"
OTHER = "aa102939-0000-4000-8000-000000000002"
VIEW_COLUMNS = "source_system,source_id,amount,currency,basis,pax,season,valid_from,valid_to,price_kind,priced_at"
TODAY = datetime.now(UTC).date()


async def _purge(conn: asyncpg.Connection) -> None:
    await conn.execute("SET ROLE bk_owner")
    try:
        for table in ("trip_plan_price_history", "trip_plan_prices"):
            await conn.execute(
                f"DELETE FROM booking.{table} WHERE trip_plan_id IN"  # noqa: S608 — constant table names
                " (SELECT id FROM booking.trip_plans WHERE source_id = ANY($1::text[]))",
                [SOURCE, OTHER],
            )
    finally:
        await conn.execute("RESET ROLE")


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    await _purge(conn)
    try:
        yield conn
    finally:
        await _purge(conn)
        await conn.close()


@pytest_asyncio.fixture
async def app_conn(admin: asyncpg.Connection, migrated_booking_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_booking_dsn)
    try:
        yield conn
    finally:
        await conn.close()


async def _plan(conn: asyncpg.Connection, source: str = SOURCE) -> str:
    return await conn.fetchval(
        "INSERT INTO booking.trip_plans (source_id, sellable) VALUES ($1, false)"
        " ON CONFLICT (source_system, source_id) DO UPDATE SET sellable = false RETURNING id::text",
        source,
    )


ROW_SQL = (
    "INSERT INTO booking.trip_plan_prices (trip_plan_id, position, channel, season, valid_from, valid_to, pax_min,"
    " pax_max, currency, amount, rrp_amount, net_amount, net_currency, priced_on, computed)"
    " VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)"
)


def _row(plan: str, **over: object) -> list[object]:
    row: dict[str, object] = {
        "position": 1,
        "channel": "b2b",
        "season": "Low",
        "valid_from": TODAY,
        "valid_to": TODAY + timedelta(days=90),
        "pax_min": 2,
        "pax_max": 2,
        "currency": "USD",
        "amount": Decimal("800"),
        "rrp_amount": Decimal("1000"),
        "net_amount": Decimal("600"),
        "net_currency": "USD",
        "priced_on": TODAY,
        "computed": True,
    }
    row.update(over)
    return [plan, *row.values()]


async def test_registered_rerunnable_and_owned(admin: asyncpg.Connection) -> None:
    await admin.execute("SET ROLE bk_owner")
    try:
        await admin.execute(MIGRATION.read_text(encoding="utf-8"))  # a second run is a no-op
    finally:
        await admin.execute("RESET ROLE")
    assert await admin.fetchval("SELECT count(*) FROM shared.schema_versions WHERE version = 'bk-039'") == 1
    for rel in ("booking.trip_plan_price_history", "booking.v_from_prices"):
        assert await admin.fetchval("SELECT relowner::regrole::text FROM pg_class WHERE oid = $1::regclass", rel) == (
            "bk_owner"
        )
    checks = await admin.fetch(
        "SELECT conname FROM pg_constraint WHERE conname = ANY($1::text[])",
        ["pricing_rules_rrp_markup_chk", "trip_plans_reference_chk", "trip_plan_prices_computed_chk",
         "trip_plan_prices_rrp_chk"],
    )  # fmt: skip
    assert len(checks) == 4  # the re-run did not add a second copy
    cols = await admin.fetchval(
        "SELECT string_agg(attname, ',' ORDER BY attnum) FROM pg_attribute"
        " WHERE attrelid = 'booking.v_from_prices'::regclass AND attnum > 0 AND NOT attisdropped"
    )
    assert cols == VIEW_COLUMNS


async def test_grants(admin: asyncpg.Connection) -> None:
    history = "booking.trip_plan_price_history"
    for privilege in ("SELECT", "INSERT"):
        assert await admin.fetchval("SELECT has_table_privilege('booking', $1, $2)", history, privilege)
    for privilege in ("UPDATE", "DELETE", "TRUNCATE"):
        assert not await admin.fetchval("SELECT has_table_privilege('booking', $1, $2)", history, privilege)
    assert await admin.fetchval("SELECT has_column_privilege('booking', $1, 'effective_to', 'UPDATE')", history)
    assert not await admin.fetchval("SELECT has_column_privilege('booking', $1, 'amount', 'UPDATE')", history)
    assert await admin.fetchval("SELECT has_table_privilege('tripplanner', 'booking.v_from_prices', 'SELECT')")
    assert await admin.fetchval("SELECT has_schema_privilege('tripplanner', 'booking', 'USAGE')")
    for table in ("booking.trip_plans", "booking.trip_plan_prices", history, "booking.pricing_rules"):
        assert not await admin.fetchval("SELECT has_table_privilege('tripplanner', $1, 'SELECT')", table), table
    # bk-040 (AA-1029 portal slice) grants the view to cis_app too (test_bk040_from_prices_cis_reader.py); the price
    # tables behind it stay closed to it.
    for table in ("booking.trip_plans", "booking.trip_plan_prices", history, "booking.pricing_rules"):
        assert not await admin.fetchval("SELECT has_table_privilege('cis_app', $1, 'SELECT')", table), table
    for privilege in ("INSERT", "UPDATE", "DELETE"):
        assert not await admin.fetchval(
            "SELECT has_table_privilege('booking', 'booking.v_from_prices', $1)", privilege
        ), privilege


async def test_view_serves_the_cheapest_rrp_for_two(app_conn: asyncpg.Connection, admin: asyncpg.Connection) -> None:
    plan = await _plan(app_conn)
    other = await _plan(app_conn, OTHER)
    async with app_conn.transaction():
        await app_conn.execute(ROW_SQL, *_row(plan))  # 2 pax, RRP 1000
        await app_conn.execute(ROW_SQL, *_row(plan, position=2, pax_min=3, pax_max=4, rrp_amount=Decimal("700")))
        await app_conn.execute(
            ROW_SQL, *_row(plan, position=3, season="High", valid_from=TODAY + timedelta(days=91),
                           valid_to=TODAY + timedelta(days=200), rrp_amount=Decimal("1200"))
        )  # fmt: skip
        await app_conn.execute(  # expired: never the from price
            ROW_SQL, *_row(plan, position=4, valid_from=date(2025, 1, 1), valid_to=date(2025, 12, 31),
                           rrp_amount=Decimal("100"))
        )  # fmt: skip
        await app_conn.execute(  # a fixed-departure row carries no RRP and is not read
            ROW_SQL, *_row(plan, position=5, channel="fixed_departure", rrp_amount=None, amount=Decimal("50"))
        )
        await app_conn.execute(ROW_SQL, *_row(other, rrp_amount=None))  # a b2b row without an RRP: no from price
    await admin.execute("SET ROLE tripplanner")
    try:
        rows = await admin.fetch(
            "SELECT source_id, amount::text AS amount, currency, basis, pax, season, price_kind"
            " FROM booking.v_from_prices WHERE source_system = 'cis' AND source_id = ANY($1::text[])",
            [SOURCE, OTHER],
        )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await admin.fetch("SELECT 1 FROM booking.trip_plan_prices LIMIT 1")
    finally:
        await admin.execute("RESET ROLE")
    assert [dict(r) for r in rows] == [
        {"source_id": SOURCE, "amount": "1000.00", "currency": "USD", "basis": "per_person", "pax": 2,
         "season": "Low", "price_kind": "rrp"},
    ]  # fmt: skip


async def test_checks(app_conn: asyncpg.Connection) -> None:
    plan = await _plan(app_conn)
    with pytest.raises(asyncpg.CheckViolationError):  # an RRP on a non-b2b row
        await app_conn.execute(ROW_SQL, *_row(plan, channel="fixed_departure"))
    with pytest.raises(asyncpg.CheckViolationError):  # a computed row without a net cost
        await app_conn.execute(
            ROW_SQL, *_row(plan, net_amount=None, net_currency=None, priced_on=None, rrp_amount=None)
        )
    with pytest.raises(asyncpg.CheckViolationError):  # the RRP markup belongs to the b2b rule
        await app_conn.execute(
            "INSERT INTO booking.pricing_rules (channel, target_margin, floor_margin, peak_uplift_pts, card_fee_pct,"
            " valid_from, rrp_markup_pct) VALUES ('fixed_departure', 0.2, 0.1, 0, 0, '2031-01-01', 0.25)"
        )
    with pytest.raises(asyncpg.CheckViolationError):  # a reference amount without its currency
        await app_conn.execute("UPDATE booking.trip_plans SET reference_amount = 10 WHERE id = $1::uuid", plan)
    with pytest.raises(asyncpg.CheckViolationError):  # reviewed by someone, but no time
        await app_conn.execute("UPDATE booking.trip_plans SET reference_reviewed_by = 'x' WHERE id = $1::uuid", plan)


async def test_history_is_append_only_except_closing(app_conn: asyncpg.Connection) -> None:
    plan = await _plan(app_conn)
    hid = await app_conn.fetchval(
        "INSERT INTO booking.trip_plan_price_history (trip_plan_id, channel, peak, valid_from, valid_to, pax_min,"
        " currency, amount, computed, overridden, change_source, changed_by)"
        " VALUES ($1::uuid, 'b2b', false, $2, $2, 1, 'USD', 10, false, false, 'staff', 'it-1029') RETURNING id",
        plan,
        TODAY,
    )
    await app_conn.execute("UPDATE booking.trip_plan_price_history SET effective_to = now() WHERE id = $1", hid)
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app_conn.execute("UPDATE booking.trip_plan_price_history SET amount = 1 WHERE id = $1", hid)
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app_conn.execute("DELETE FROM booking.trip_plan_price_history WHERE id = $1", hid)
    with pytest.raises(asyncpg.CheckViolationError):
        await app_conn.execute(
            "UPDATE booking.trip_plan_price_history SET effective_to = effective_from - interval '1 day' WHERE id = $1",
            hid,
        )


async def test_commercial_columns_are_tagged(admin: asyncpg.Connection) -> None:
    rows = await admin.fetch(
        "SELECT c.relname || '.' || a.attname AS col, col_description(c.oid, a.attnum) AS doc"
        " FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid"
        " WHERE c.relnamespace = 'booking'::regnamespace AND (c.relname, a.attname) IN"
        " (('pricing_rules', 'rrp_markup_pct'), ('trip_plans', 'reference_price_raw'),"
        "  ('trip_plans', 'reference_amount'),"
        "  ('trip_plan_prices', 'rrp_amount'), ('trip_plan_price_history', 'amount'),"
        "  ('trip_plan_price_history', 'net_amount'))"
    )
    assert len(rows) == 6
    for r in rows:
        assert (r["doc"] or "").startswith("conf:commercial"), r["col"]
