"""AA-1091 (S279, root ADR 0006a A10e-2): the synthetic-plan and E2E-maker-version SQL of the Dev-only pricing pair
(:mod:`app.auth.e2e_staff`) on real PostgreSQL, as role ``booking``.

``catalog.v_tenant_account_v1`` (CIS 357 + 401) is stood in by a plain table of 401's exact columns (as
``test_aa1087_e2e_writers``). Covers:

- a plan no agency used, and a plan only a synthetic agency quoted, answer True (by tour key, by many keys, and a
  price version the E2E maker created on it);
- a plan a real agency quoted, or holds an allotment on one of its departures, answers False, alone or among
  synthetic ones; an unknown key makes the many-keys answer False;
- a version another person created answers False, even on a synthetic plan; an unknown version answers False;
- without the ``is_synthetic`` column (CIS 401 not applied) every answer is False (fail closed).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import date, timedelta
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from app.auth import e2e_staff
from tests.integration import ops

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

CIS_357_COLUMNS = (
    "tenant_id uuid, name text, account_status text, suspend_reason text, in_service boolean, kind text, "
    "is_pilot boolean, trade_book boolean"
)
CIS_401_COLUMNS = CIS_357_COLUMNS + ", is_synthetic boolean"
DROP = "DROP TABLE IF EXISTS catalog.v_tenant_account_v1"
SYNTHETIC = uuid.UUID("00000000-0000-4000-8000-0000000a1091")
REAL = uuid.UUID("00000000-0000-4000-8000-0000000b1091")
NOTE = "it-1091-e2e"
MAKER_KEY = "it-1091-maker"
OTHER_KEY = "it-1091-other"
TOURS = {
    "unused": "11111111-1091-4000-8000-000000000001",
    "synthetic_quote": "11111111-1091-4000-8000-000000000002",
    "real_quote": "11111111-1091-4000-8000-000000000003",
    "real_allotment": "11111111-1091-4000-8000-000000000004",
}
START = date(2031, 3, 1)


async def _stub(conn: asyncpg.Connection, columns: str) -> None:
    await conn.execute(DROP)
    await conn.execute(f"CREATE TABLE catalog.v_tenant_account_v1 ({columns})")
    await conn.execute("GRANT SELECT ON catalog.v_tenant_account_v1 TO booking")
    if "is_synthetic" in columns:
        for tid, synthetic in ((SYNTHETIC, True), (REAL, False)):
            await conn.execute(
                "INSERT INTO catalog.v_tenant_account_v1 VALUES ($1, 'Mossbank Journeys', 'active', NULL, true,"
                " 'agency', false, true, $2)",
                tid,
                synthetic,
            )


async def _purge(conn: asyncpg.Connection) -> None:
    async with conn.transaction():
        await conn.execute("SET LOCAL ROLE bk_owner")
        plans = "(SELECT id FROM booking.trip_plans WHERE source_id = ANY($1::text[]))"
        keys = list(TOURS.values())
        await conn.execute(f"DELETE FROM booking.price_list_versions WHERE trip_plan_id IN {plans}", keys)
        await conn.execute(
            "DELETE FROM booking.departure_allotments WHERE departure_id IN"
            f" (SELECT id FROM booking.departures WHERE trip_plan_id IN {plans})",
            keys,
        )
        await conn.execute(f"DELETE FROM booking.departures WHERE trip_plan_id IN {plans}", keys)
        await conn.execute(
            "DELETE FROM booking.b2b_quote_lines WHERE quote_id IN"
            " (SELECT id FROM booking.b2b_quotes WHERE proposal_ref = $1)",
            NOTE,
        )
        await conn.execute("DELETE FROM booking.b2b_quotes WHERE proposal_ref = $1", NOTE)
        await conn.execute("DELETE FROM booking.b2b_partners WHERE agency_name LIKE 'it-1091 %'")
        await conn.execute("DELETE FROM booking.trip_plans WHERE source_id = ANY($1::text[])", keys)
        await conn.execute(
            "DELETE FROM booking.staff_users WHERE cis_user_id = ANY($1::text[])", [MAKER_KEY, OTHER_KEY]
        )
    await conn.execute(DROP)


async def _seed(conn: asyncpg.Connection) -> dict[str, Any]:
    """Four plans (one unused, one quoted by the synthetic agency, one quoted by the real agency, one with a real
    agency's allotment on a departure) and per plan a draft version by the E2E maker and one by another person."""
    out: dict[str, Any] = {"plans": {}, "maker": {}, "other": {}}
    await ops.staff(conn, (MAKER_KEY, "contracting"), (OTHER_KEY, "contracting"))
    await conn.execute(
        "UPDATE booking.staff_users SET email = $2 WHERE cis_user_id = $1", MAKER_KEY, e2e_staff.E2E_PRICING_MAKER_EMAIL
    )
    await conn.execute("UPDATE booking.staff_users SET email = 'lena@adventure.asia' WHERE cis_user_id = $1", OTHER_KEY)
    async with conn.transaction():
        await conn.execute("SET LOCAL ROLE bk_owner")
        partners = {}
        for label, tid in (("synthetic", SYNTHETIC), ("real", REAL)):
            partners[label] = await conn.fetchval(
                "INSERT INTO booking.b2b_partners (tenant_id, agency_name, m2m_client_id)"
                " VALUES ($1, $2, 'aa1091client') RETURNING id",
                tid,
                f"it-1091 {label}",
            )
        for name, key in TOURS.items():
            plan = await conn.fetchval(
                "INSERT INTO booking.trip_plans (source_system, source_id, sellable) VALUES ('cis', $1, false)"
                " RETURNING id",
                key,
            )
            out["plans"][name] = plan
            for who, creator in (("maker", MAKER_KEY), ("other", OTHER_KEY)):
                out[who][name] = await conn.fetchval(
                    "INSERT INTO booking.price_list_versions (trip_plan_id, version_no, label, tariff_year,"
                    " travel_from, travel_to, created_by) VALUES ($1, $2, 'Synthetic draft', 2031, $3, $4, $5)"
                    " RETURNING id",
                    plan,
                    1 if who == "maker" else 2,
                    START,
                    START + timedelta(days=365),
                    creator,
                )
        for name, label in (("synthetic_quote", "synthetic"), ("real_quote", "real")):
            quote = await conn.fetchval(
                "INSERT INTO booking.b2b_quotes (partner_id, m2m_client_id, proposal_ref, proposal_version, currency,"
                " fx_rate_per_usd, fx_as_of, fx_source, pricing_rule_valid_from, margin_applied, card_fee_pct,"
                " rounding, priced_on, total_b2b_price, price_hash, valid_until)"
                " VALUES ($1, 'aa1091client', $2, 1, 'USD', 1, current_date, 'base', current_date, 0.15, 0.03,"
                " 'line_ceil_0.01', current_date, 10, $3, now() + interval '1 day') RETURNING id",
                partners[label],
                NOTE,
                "b" * 64,
            )
            await conn.execute(
                "INSERT INTO booking.b2b_quote_lines (quote_id, line_no, line_ref, component_id, component_kind,"
                " service_date, pax_adult, pax_child, pax_infant, rooms, unit, availability, b2b_price, net_cost_usd,"
                " supplier_rate_ids, agency_discount_pct) VALUES ($1, 1, 'L1', $2, 'trip_plan', $3, 2, 0, 0, '[]',"
                " 'per_line', 'available', 100, 80, '{}', 0)",
                quote,
                f"tp:{out['plans'][name]}",
                START,
            )
        departure = await conn.fetchval(
            "INSERT INTO booking.departures (trip_plan_id, start_date, capacity) VALUES ($1, $2, 12) RETURNING id",
            out["plans"]["real_allotment"],
            START,
        )
        await conn.execute(
            "INSERT INTO booking.departure_allotments (departure_id, partner_id, seats, release_days_before,"
            " created_by) VALUES ($1, $2, 4, 14, $3)",
            departure,
            partners["real"],
            OTHER_KEY,
        )
    return out


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
async def booking(admin: asyncpg.Connection, migrated_booking_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_booking_dsn)
    try:
        yield conn
    finally:
        await conn.close()


async def _plan(booking: asyncpg.Connection, name: str) -> bool:
    return await e2e_staff.is_synthetic_record(booking, e2e_staff.PLAN_COMPONENTS[1], {"source_id": TOURS[name]})


async def _version(booking: asyncpg.Connection, version_id: Any) -> bool:
    route = e2e_staff.PRICE_VERSION_APPROVE[1]
    return await e2e_staff.is_synthetic_record(booking, route, {"version_id": str(version_id)})


async def test_plans_no_real_agency_used_answer_true_and_the_others_false(
    admin: asyncpg.Connection, booking: asyncpg.Connection
) -> None:
    await _stub(admin, CIS_401_COLUMNS)
    await _seed(admin)
    assert {name: await _plan(booking, name) for name in TOURS} == {
        "unused": True,
        "synthetic_quote": True,
        "real_quote": False,
        "real_allotment": False,
    }
    assert await e2e_staff.plans_synthetic(booking, [TOURS["unused"], TOURS["synthetic_quote"]])
    assert not await e2e_staff.plans_synthetic(booking, [TOURS["unused"], TOURS["real_quote"]])
    assert not await e2e_staff.plans_synthetic(booking, [TOURS["unused"], "11111111-1091-4000-8000-0000000000ff"])
    route = e2e_staff.PRICE_WORKBENCH_ACTION[1]
    body = {"action": "make_sellable", "tour_keys": [TOURS["unused"], TOURS["synthetic_quote"]]}
    assert await e2e_staff.is_synthetic_record(booking, route, {}, body)
    assert not await e2e_staff.is_synthetic_record(booking, route, {}, {**body, "tour_keys": [TOURS["real_quote"]]})


async def test_only_the_e2e_makers_versions_of_synthetic_plans_answer_true(
    admin: asyncpg.Connection, booking: asyncpg.Connection
) -> None:
    await _stub(admin, CIS_401_COLUMNS)
    rows = await _seed(admin)
    maker = {name: await _version(booking, vid) for name, vid in rows["maker"].items()}
    assert maker == {"unused": True, "synthetic_quote": True, "real_quote": False, "real_allotment": False}
    other = {name: await _version(booking, vid) for name, vid in rows["other"].items()}
    assert not any(other.values())
    assert not await _version(booking, uuid.uuid4())


async def test_without_cis_401_every_answer_is_false(admin: asyncpg.Connection, booking: asyncpg.Connection) -> None:
    await _stub(admin, CIS_357_COLUMNS)
    rows = await _seed(admin)
    assert not any([await _plan(booking, name) for name in TOURS])
    assert not await e2e_staff.plans_synthetic(booking, [TOURS["unused"]])
    assert not await _version(booking, rows["maker"]["unused"])
