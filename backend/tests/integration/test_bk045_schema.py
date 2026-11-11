"""bk-045 (AA-1074) on a real PostgreSQL: FX snapshots and booking.fx_plain_rate, departure shared costs (frozen once
ordered), departure-level supplier bookings, supplier purchase orders, departure bills, and the cost / profit views.
A contract file (ALTERs, CHECKs and guards replaced on existing tables). Every test runs in one transaction logged in
as role booking and is rolled back. Names are invented. No import of app code (the migration PR stands alone)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

BK045 = Path(__file__).resolve().parents[3] / "migrations" / "045_finance_costs_orders_profit.sql"
START = date.today() + timedelta(days=60)
MAKER, CHECKER = "it-1074-maker", "it-1074-checker"


@pytest_asyncio.fixture
async def conn(migrated_booking_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    c = await asyncpg.connect(migrated_booking_dsn)
    tx = c.transaction()
    await tx.start()
    for staff_id, role in ((MAKER, "contracting"), (CHECKER, "admin")):
        await c.execute(
            "INSERT INTO booking.staff_users (cis_user_id, role, active, name) VALUES ($1, $2, true, $1)"
            " ON CONFLICT (cis_user_id) DO NOTHING",
            staff_id,
            role,
        )
    try:
        yield c
    finally:
        await tx.rollback()
        await c.close()


async def _fx(c: asyncpg.Connection, currency: str, rate: str, as_of: date, *, approved: bool = True) -> None:
    fid = await c.fetchval(
        "INSERT INTO booking.fx_rates (currency, rate_per_usd, buffer_pct, as_of, source, created_by)"
        " VALUES ($1, $2::numeric, 0.02, $3, 'it-1074', $4) RETURNING id",
        currency,
        rate,
        as_of,
        MAKER,
    )
    if approved:
        await c.execute(
            "INSERT INTO booking.policy_approvals (entity_type, fx_rate_id, approved_by) VALUES ('fx_rate', $1, $2)",
            fid,
            CHECKER,
        )


async def _supplier(c: asyncpg.Connection, name: str) -> dict[str, str]:
    sid = await c.fetchval(
        "INSERT INTO booking.suppliers (company_name, categories, country_code, contact_email)"
        " VALUES ($1, ARRAY['transport'], 'VN', 'desk@example.test') RETURNING id::text",
        f"it-1074 {name}",
    )
    service = await c.fetchval(
        "INSERT INTO booking.services (supplier_id, service_type, title) VALUES ($1::uuid, 'transfer', $2)"
        " RETURNING id::text",
        sid,
        f"{name} coach",
    )
    return {"supplier": sid, "service": service}


async def _departure(c: asyncpg.Connection, tag: str) -> str:
    plan = await c.fetchval(
        "INSERT INTO booking.trip_plans (source_system, source_id, sellable) VALUES ('cis', $1, true) RETURNING id",
        f"it-1074-{tag}-{uuid.uuid4().hex[:6]}",
    )
    return await c.fetchval(
        "INSERT INTO booking.departures (trip_plan_id, start_date, capacity) VALUES ($1, $2, 10) RETURNING id::text",
        plan,
        START,
    )


async def _sold_case(
    c: asyncpg.Connection, departure: str, *, seats: int, total: str, currency: str = "USD", rate: str = "1"
) -> dict[str, Any]:
    """A B2B trip case with a confirmed hold of ``seats`` on the departure; its quote total in ``currency``."""
    tid = uuid.uuid4()
    pid = await c.fetchval(
        "INSERT INTO booking.b2b_partners (tenant_id, agency_name, status, billing_currency, m2m_client_id)"
        " VALUES ($1, $2, 'active', 'USD', 'it1074client') RETURNING id::text",
        tid,
        f"Harbourlight Tours {tid.hex[:4]}",
    )
    case = await c.fetchrow(
        "INSERT INTO booking.trip_cases (channel, party_tenant_id, status, start_date, end_date, pax, currency,"
        " source_system) VALUES ('b2b', $1, 'confirmed', $2, $3, $4, $5, 'crm') RETURNING id::text AS id, code",
        tid,
        START,
        START + timedelta(days=3),
        seats,
        currency,
    )
    quote = await c.fetchval(
        "INSERT INTO booking.b2b_quotes (partner_id, m2m_client_id, proposal_ref, proposal_version, currency,"
        " fx_rate_per_usd, fx_buffer_pct, fx_as_of, fx_source, pricing_rule_valid_from, margin_applied, card_fee_pct,"
        " rounding, priced_on, total_b2b_price, price_hash, valid_until)"
        " VALUES ($1::uuid, 'it1074client', $2, 1, $3, $4::numeric, 0, current_date, 'test', current_date, 0.15, 0,"
        " 'line_ceil_0.01', current_date, $5::numeric, $6, now() + interval '2 days') RETURNING id",
        pid,
        f"PR-{case['code']}",
        currency,
        rate,
        total,
        "b" * 64,
    )
    await c.execute(
        "INSERT INTO booking.b2b_quote_lines (quote_id, line_no, line_ref, component_id, component_kind, service_date,"
        " pax_adult, pax_child, pax_infant, rooms, unit, availability, b2b_price, net_cost_usd, supplier_rate_ids,"
        " departure_id, agency_discount_pct)"
        " VALUES ($1, 1, 'L1', 'tp:x', 'trip_plan', $2, $3, 0, 0, '[]', 'per_person', 'available', $4::numeric,"
        " 400, '{}', $5::uuid, 0)",
        quote,
        START,
        seats,
        total,
        departure,
    )
    hold = await c.fetchval(
        "INSERT INTO booking.b2b_holds (partner_id, m2m_client_id, quote_id, proposal_ref, proposal_version,"
        " idempotency_key, status, expires_at, trip_case_id, confirmed_at)"
        " VALUES ($1::uuid, 'it1074client', $2, $3, 1, $4, 'confirmed', now() + interval '1 day', $5::uuid, now())"
        " RETURNING id::text",
        pid,
        quote,
        f"PR-{case['code']}",
        f"hold-{uuid.uuid4().hex[:12]}",
        case["id"],
    )
    await c.execute(
        "INSERT INTO booking.b2b_hold_lines (hold_id, line_no, line_ref, departure_id, seats, held_as, status)"
        " VALUES ($1::uuid, 1, 'L1', $2::uuid, $3, 'available', 'confirmed')",
        hold,
        departure,
        seats,
    )
    return {"id": case["id"], "code": case["code"], "partner": pid, "hold": hold}


async def _booking(c: asyncpg.Connection, sup: str, *, case: str | None = None, departure: str | None = None,
                   net: str = "300.00", currency: str = "USD") -> str:  # fmt: skip
    return await c.fetchval(
        "INSERT INTO booking.supplier_bookings (trip_case_id, departure_id, supplier_id, item_description,"
        " service_date, net_amount, net_currency) VALUES ($1::uuid, $2::uuid, $3::uuid, 'Coach day', $4,"
        " $5::numeric, $6) RETURNING id::text",
        case,
        departure,
        sup,
        START,
        net,
        currency,
    )


async def _confirm(c: asyncpg.Connection, booking: str, *, rate: str | None = None) -> None:
    await c.execute(
        "UPDATE booking.supplier_bookings SET status = 'requested', requested_at = now() WHERE id = $1::uuid", booking
    )
    await c.execute(
        "UPDATE booking.supplier_bookings SET status = 'confirmed', confirmed_at = now(), confirmation_ref = 'OK-1',"
        " fx_rate_per_usd = $2::numeric, fx_as_of = CASE WHEN $2::numeric IS NULL THEN NULL ELSE current_date END"
        " WHERE id = $1::uuid",
        booking,
        rate,
    )


async def _approved_bill(c: asyncpg.Connection, sup: str, lines: list[tuple[str, str]], *, currency: str = "USD",
                         rate: str | None = None, departure: str | None = None) -> str:  # fmt: skip
    total = sum(Decimal(a) for _, a in lines)
    bill = await c.fetchval(
        "INSERT INTO booking.supplier_bills (supplier_id, departure_id, bill_number, currency, amount, issue_date,"
        " due_date, idempotency_key, request_sha256, created_by, updated_by)"
        " VALUES ($1::uuid, $2::uuid, $3, $4, $5, current_date, current_date + 14, $6, $7, $8, $8) RETURNING id::text",
        sup,
        departure,
        f"INV-{uuid.uuid4().hex[:8]}",
        currency,
        total,
        f"key-{uuid.uuid4().hex}",
        "c" * 64,
        MAKER,
    )
    for booking, amount in lines:
        await c.execute(
            "INSERT INTO booking.supplier_bill_items (bill_id, supplier_booking_id, amount, created_by)"
            " VALUES ($1::uuid, $2::uuid, $3::numeric, $4)",
            bill,
            booking,
            amount,
            MAKER,
        )
    await c.execute(
        "UPDATE booking.supplier_bills SET status = 'approved', approved_by = $2, approved_at = now(),"
        " fx_rate_per_usd = $3::numeric, fx_as_of = CASE WHEN $3::numeric IS NULL THEN NULL ELSE current_date END"
        " WHERE id = $1::uuid",
        bill,
        CHECKER,
        rate,
    )
    return bill


# ── File shape ───────────────────────────────────────────────────────────────


async def test_contract_file_has_no_directive() -> None:
    text = BK045.read_text(encoding="utf-8")
    assert not any(line.startswith("-- migrate:") for line in text.splitlines())
    assert "Class: contract" in text


async def test_registered_and_rerunnable(migrated_admin_dsn: str) -> None:
    c = await asyncpg.connect(migrated_admin_dsn)
    try:
        await c.execute("SET ROLE bk_owner")
        await c.execute(BK045.read_text(encoding="utf-8"))
        await c.execute("RESET ROLE")
        assert await c.fetchval("SELECT count(*) FROM shared.schema_versions WHERE version = 'bk-045'") == 1
    finally:
        await c.close()


async def test_refuses_a_bk045_key_held_by_another_file(migrated_admin_dsn: str) -> None:
    """Two files under one number: the second would record nothing (ON CONFLICT DO NOTHING) while readiness reports
    bk-045. The key guard stops the file instead."""
    c = await asyncpg.connect(migrated_admin_dsn)
    real = await c.fetchval("SELECT description FROM shared.schema_versions WHERE version = 'bk-045'")
    try:
        await c.execute("UPDATE shared.schema_versions SET description = 'another file' WHERE version = 'bk-045'")
        await c.execute("SET ROLE bk_owner")
        with pytest.raises(asyncpg.RaiseError, match="already held by another migration"):
            await c.execute(BK045.read_text(encoding="utf-8"))
        await c.execute("ROLLBACK")
    finally:
        await c.execute("RESET ROLE")
        await c.execute("UPDATE shared.schema_versions SET description = $1 WHERE version = 'bk-045'", real)
        await c.close()
    assert real.startswith("AA-1074:")


async def test_booking_cannot_delete_costs_or_orders(conn: asyncpg.Connection) -> None:
    for table in ("departure_costs", "supplier_orders"):
        assert not await conn.fetchval("SELECT has_table_privilege('booking.' || $1, 'DELETE')", table)
    assert await conn.fetchval("SELECT has_table_privilege('booking.v_case_profit', 'SELECT')")


# ── FX ───────────────────────────────────────────────────────────────────────


async def test_fx_plain_rate_reads_the_approved_rate_in_force(conn: asyncpg.Connection) -> None:
    day = date.today()
    await _fx(conn, "THB", "35.5", day - timedelta(days=10))
    await _fx(conn, "THB", "36.25", day - timedelta(days=2))
    await _fx(conn, "THB", "99", day - timedelta(days=1), approved=False)  # pending: never used
    assert await conn.fetchval("SELECT booking.fx_plain_rate('USD', current_date)") == 1
    assert await conn.fetchval("SELECT booking.fx_plain_rate('THB', current_date)") == Decimal("36.25")
    assert await conn.fetchval("SELECT booking.fx_plain_rate('THB', current_date - 5)") == Decimal("35.5")
    assert await conn.fetchval("SELECT booking.fx_plain_rate('THB', current_date - 30)") is None
    assert await conn.fetchval("SELECT booking.fx_plain_rate('XAF', current_date)") is None


async def test_booking_fx_snapshot_is_fixed_until_a_rerequest(conn: asyncpg.Connection) -> None:
    sup = await _supplier(conn, "Snapshot")
    dep = await _departure(conn, "snap")
    case = await _sold_case(conn, dep, seats=2, total="1000.00")
    booking = await _booking(conn, sup["supplier"], case=case["id"])
    await _confirm(conn, booking, rate="1")
    with pytest.raises(asyncpg.CheckViolationError, match="FX snapshot is fixed"):
        async with conn.transaction():  # a savepoint: the test transaction goes on
            await conn.execute("UPDATE booking.supplier_bookings SET fx_rate_per_usd = 2 WHERE id = $1::uuid", booking)
    await conn.execute(
        "UPDATE booking.supplier_bookings SET status = 'requested', confirmation_ref = NULL, confirmed_at = NULL,"
        " fx_rate_per_usd = NULL, fx_as_of = NULL WHERE id = $1::uuid",
        booking,
    )
    with pytest.raises(asyncpg.CheckViolationError, match="supplier_bookings_fx_chk"):
        async with conn.transaction():  # a savepoint: the test transaction goes on
            await conn.execute("UPDATE booking.supplier_bookings SET fx_rate_per_usd = 2 WHERE id = $1::uuid", booking)


async def test_bill_fx_snapshot_is_set_once_with_the_approval(conn: asyncpg.Connection) -> None:
    sup = await _supplier(conn, "Billfx")
    dep = await _departure(conn, "billfx")
    case = await _sold_case(conn, dep, seats=2, total="1000.00")
    booking = await _booking(conn, sup["supplier"], case=case["id"])
    await _confirm(conn, booking, rate="1")
    bill = await _approved_bill(conn, sup["supplier"], [(booking, "300.00")], rate="1")
    with pytest.raises(asyncpg.CheckViolationError, match="set once"):
        async with conn.transaction():  # a savepoint: the test transaction goes on
            await conn.execute("UPDATE booking.supplier_bills SET fx_rate_per_usd = 2 WHERE id = $1::uuid", bill)
    with pytest.raises(asyncpg.CheckViolationError, match="starts received"):
        async with conn.transaction():  # a savepoint: the test transaction goes on
            await conn.execute(
                "INSERT INTO booking.supplier_bills (supplier_id, bill_number, currency, amount, issue_date, due_date,"
                " idempotency_key, request_sha256, created_by, fx_rate_per_usd, fx_as_of)"
                " VALUES ($1::uuid, 'X-1', 'USD', 1, current_date, current_date, $2, $3, $4, 1, current_date)",
                sup["supplier"],
                f"key-{uuid.uuid4().hex}",
                "c" * 64,
                MAKER,
            )


# ── Departure-level bookings, departure costs, bills ─────────────────────────


async def test_a_booking_names_a_case_or_a_departure(conn: asyncpg.Connection) -> None:
    sup = await _supplier(conn, "Owner")
    dep = await _departure(conn, "owner")
    case = await _sold_case(conn, dep, seats=2, total="500.00")
    with pytest.raises(asyncpg.CheckViolationError, match="supplier_bookings_owner_chk"):
        async with conn.transaction():  # a savepoint: the test transaction goes on
            await _booking(conn, sup["supplier"])
    with pytest.raises(asyncpg.CheckViolationError, match="supplier_bookings_owner_chk"):
        async with conn.transaction():  # a savepoint: the test transaction goes on
            await _booking(conn, sup["supplier"], case=case["id"], departure=dep)
    assert await _booking(conn, sup["supplier"], departure=dep)
    await conn.execute("UPDATE booking.departures SET status = 'cancelled' WHERE id = $1::uuid", dep)
    with pytest.raises(asyncpg.CheckViolationError, match="is cancelled"):
        async with conn.transaction():  # a savepoint: the test transaction goes on
            await _booking(conn, sup["supplier"], departure=dep)


async def _cost(c: asyncpg.Connection, dep: str, sup: str, *, net: str = "200.00") -> str:
    return await c.fetchval(
        "INSERT INTO booking.departure_costs (departure_id, supplier_id, item_description, service_date, basis,"
        " quantity, pricing, unit_amount, net_amount, currency, priced_for_pax, created_by)"
        " VALUES ($1::uuid, $2::uuid, 'Coach for the group', $3, 'group', 1, 'manual', $4::numeric, $4::numeric,"
        " 'USD', 4, $5) RETURNING id::text",
        dep,
        sup,
        START,
        net,
        MAKER,
    )


async def test_departure_cost_freezes_when_its_booking_is_requested(conn: asyncpg.Connection) -> None:
    sup = await _supplier(conn, "Freeze")
    dep = await _departure(conn, "freeze")
    cost = await _cost(conn, dep, sup["supplier"])
    booking = await _booking(conn, sup["supplier"], departure=dep, net="200.00")
    await conn.execute("UPDATE booking.departure_costs SET supplier_booking_id = $2::uuid WHERE id = $1::uuid", cost,
                       booking)  # fmt: skip
    await conn.execute(
        "UPDATE booking.departure_costs SET net_amount = 210, unit_amount = 210 WHERE id = $1::uuid", cost
    )
    await conn.execute(
        "UPDATE booking.supplier_bookings SET status = 'requested', requested_at = now() WHERE id = $1::uuid", booking
    )
    assert await conn.fetchval("SELECT frozen_at IS NOT NULL FROM booking.departure_costs WHERE id = $1::uuid", cost)
    with pytest.raises(asyncpg.CheckViolationError, match="is frozen"):
        async with conn.transaction():  # a savepoint: the test transaction goes on
            await conn.execute("UPDATE booking.departure_costs SET net_amount = 1 WHERE id = $1::uuid", cost)
    await conn.execute("UPDATE booking.departure_costs SET notes = 'kept' WHERE id = $1::uuid", cost)  # notes stay free
    with pytest.raises(asyncpg.CheckViolationError, match="never changes"):
        async with conn.transaction():  # a savepoint: the test transaction goes on
            await conn.execute(
                "UPDATE booking.departure_costs SET supplier_booking_id = NULL WHERE id = $1::uuid", cost
            )


async def test_departure_bill_takes_only_that_departures_bookings(conn: asyncpg.Connection) -> None:
    sup = await _supplier(conn, "Depbill")
    dep, other = await _departure(conn, "depbill"), await _departure(conn, "other")
    case = await _sold_case(conn, other, seats=1, total="100.00")
    mine = await _booking(conn, sup["supplier"], departure=dep)
    theirs = await _booking(conn, sup["supplier"], departure=other)
    await _confirm(conn, mine)
    await _confirm(conn, theirs)
    with pytest.raises(asyncpg.CheckViolationError, match="not a shared cost of the departure"):
        async with conn.transaction():  # a savepoint: the test transaction goes on
            await _approved_bill(conn, sup["supplier"], [(theirs, "50.00")], departure=dep)
    assert await _approved_bill(conn, sup["supplier"], [(mine, "50.00")], departure=dep)
    with pytest.raises(asyncpg.CheckViolationError, match="supplier_bills_scope_chk"):
        async with conn.transaction():  # a savepoint: the test transaction goes on
            await conn.execute(
                "INSERT INTO booking.supplier_bills (supplier_id, trip_case_id, departure_id, bill_number, currency,"
                " amount, issue_date, due_date, idempotency_key, request_sha256, created_by)"
                " VALUES ($1::uuid, $6::uuid, $2::uuid, 'B-2', 'USD', 1, current_date, current_date, $3, $4, $5)",
                sup["supplier"],
                dep,
                f"key-{uuid.uuid4().hex}",
                "c" * 64,
                MAKER,
                case["id"],
            )


# ── Purchase orders ──────────────────────────────────────────────────────────


async def test_order_lines_and_life(conn: asyncpg.Connection) -> None:
    sup, other = await _supplier(conn, "Order"), await _supplier(conn, "Elsewhere")
    dep = await _departure(conn, "order")
    order = await conn.fetchrow(
        "INSERT INTO booking.supplier_orders (supplier_id, currency, service_month, created_by)"
        " VALUES ($1::uuid, 'USD', date_trunc('month', current_date)::date, $2) RETURNING id::text AS id, code",
        sup["supplier"],
        MAKER,
    )
    assert order["code"].startswith("PO-")
    line = await _booking(conn, sup["supplier"], departure=dep)
    stranger = await _booking(conn, other["supplier"], departure=dep)
    with pytest.raises(asyncpg.CheckViolationError, match="supplier and currency"):
        async with conn.transaction():  # a savepoint: the test transaction goes on
            await conn.execute(
                "UPDATE booking.supplier_bookings SET supplier_order_id = $2::uuid WHERE id = $1::uuid",
                stranger,
                order["id"],
            )
    await conn.execute(
        "UPDATE booking.supplier_bookings SET supplier_order_id = $2::uuid WHERE id = $1::uuid", line, order["id"]
    )
    with pytest.raises(asyncpg.CheckViolationError, match="supplier_orders_send_chk"):
        async with conn.transaction():  # a savepoint: the test transaction goes on
            await conn.execute("UPDATE booking.supplier_orders SET status = 'sending' WHERE id = $1::uuid", order["id"])
    await conn.execute(
        "UPDATE booking.supplier_orders SET status = 'sending', snapshot = '{}'::jsonb, total_net = 300,"
        " sent_to = 'desk@example.test', send_requested_at = now(), sent_by = $2 WHERE id = $1::uuid",
        order["id"],
        MAKER,
    )
    await conn.execute(
        "UPDATE booking.supplier_orders SET status = 'sent', sent_at = now(), delivery_ref = 'msg-1'"
        " WHERE id = $1::uuid",
        order["id"],
    )
    with pytest.raises(asyncpg.CheckViolationError, match="stays on it"):
        async with conn.transaction():  # a savepoint: the test transaction goes on
            await conn.execute(
                "UPDATE booking.supplier_bookings SET supplier_order_id = NULL WHERE id = $1::uuid", line
            )
    with pytest.raises(asyncpg.CheckViolationError, match="what the supplier received is fixed"):
        async with conn.transaction():  # a savepoint: the test transaction goes on
            await conn.execute("UPDATE booking.supplier_orders SET total_net = 1 WHERE id = $1::uuid", order["id"])
    with pytest.raises(asyncpg.CheckViolationError, match="not allowed"):
        async with conn.transaction():  # a savepoint: the test transaction goes on
            await conn.execute("UPDATE booking.supplier_orders SET status = 'draft' WHERE id = $1::uuid", order["id"])
    await conn.execute(
        "UPDATE booking.supplier_orders SET status = 'cancelled', cancelled_at = now(), cancelled_by = $2,"
        " cancel_reason = 'Trip moved' WHERE id = $1::uuid",
        order["id"],
        MAKER,
    )
    with pytest.raises(asyncpg.CheckViolationError, match="final"):
        async with conn.transaction():  # a savepoint: the test transaction goes on
            await conn.execute("UPDATE booking.supplier_orders SET note = 'x' WHERE id = $1::uuid", order["id"])


# ── Views ────────────────────────────────────────────────────────────────────


async def test_cost_variance_and_profit_views(conn: asyncpg.Connection) -> None:
    """Departure of 4 sold seats (3 + 1): case A (EUR quote) has its own booking in THB, billed at another rate; a
    shared coach of 200.00 USD is split 150.00 / 50.00."""
    await _fx(conn, "THB", "35", date.today() - timedelta(days=3))
    await _fx(conn, "EUR", "0.8", date.today() - timedelta(days=3))
    sup = await _supplier(conn, "Views")
    dep = await _departure(conn, "views")
    a = await _sold_case(conn, dep, seats=3, total="1600.00", currency="EUR", rate="0.8")
    b = await _sold_case(conn, dep, seats=1, total="500.00")
    own = await _booking(conn, sup["supplier"], case=a["id"], net="7000.00", currency="THB")
    await _confirm(conn, own, rate="35")
    await _approved_bill(conn, sup["supplier"], [(own, "7000.00")], currency="THB", rate="36")
    shared_booking = await _booking(conn, sup["supplier"], departure=dep, net="200.00")
    cost = await _cost(conn, dep, sup["supplier"], net="200.00")
    await conn.execute("UPDATE booking.departure_costs SET supplier_booking_id = $2::uuid WHERE id = $1::uuid", cost,
                       shared_booking)  # fmt: skip

    cv = await conn.fetchrow("SELECT * FROM booking.v_cost_variance WHERE supplier_booking_id = $1::uuid", own)
    assert cv["planned_usd"] == Decimal("200.00")  # 7000 / 35
    assert cv["billed_usd"] == Decimal("194.44")  # 7000 / 36, half up to the cent
    assert cv["variance"] == Decimal("0.00") and cv["variance_usd"] == Decimal("-5.56")
    assert cv["fx_difference_usd"] == Decimal("-5.56")  # same native amount, the THB moved
    assert cv["cost_usd"] == Decimal("194.44") and not cv["fx_missing"]

    shares = {str(r["trip_case_id"]): r for r in await conn.fetch("SELECT * FROM booking.v_departure_cost_share")}
    assert shares[a["id"]]["share_cost_usd"] == Decimal("150.00")
    assert shares[b["id"]]["share_cost_usd"] == Decimal("50.00")

    pa = await conn.fetchrow("SELECT * FROM booking.v_case_profit WHERE trip_case_id = $1::uuid", a["id"])
    assert pa["revenue_usd"] == Decimal("2000.00")  # 1600 EUR / 0.8
    assert pa["cost_usd"] == Decimal("344.44")  # 194.44 billed + 150.00 share
    assert pa["profit_usd"] == Decimal("1655.56") and pa["profit"] == Decimal("1324.45")  # x 0.8, half up
    assert pa["margin"] == Decimal("0.8278") and pa["cost_basis"] == "bookings"
    pb = await conn.fetchrow("SELECT * FROM booking.v_case_profit WHERE trip_case_id = $1::uuid", b["id"])
    assert pb["cost_basis"] == "quote" and pb["cost_usd"] == Decimal("450.00")  # quoted 400 + 50 share

    dp = await conn.fetchrow("SELECT * FROM booking.v_departure_profit WHERE departure_id = $1::uuid", dep)
    assert dp["sold_seats"] == 4 and dp["cases"] == 2
    assert dp["revenue_usd"] == Decimal("2500.00")
    assert dp["shared_cost_usd"] == Decimal("200.00")
    assert dp["profit_usd"] == Decimal("2500.00") - Decimal("194.44") - Decimal("400.00") - Decimal("200.00")
    assert dp["profit_per_seat_usd"] == Decimal("426.39")

    month = await conn.fetchrow("SELECT * FROM booking.v_agency_profit_month WHERE partner_id = $1::uuid", a["partner"])
    assert month["cases"] == 1 and month["profit_usd"] == Decimal("1655.56")


async def test_cancelled_case_earns_the_fee(conn: asyncpg.Connection) -> None:
    dep = await _departure(conn, "cancel")
    case = await _sold_case(conn, dep, seats=2, total="1000.00")
    policy = await conn.fetchval("SELECT id FROM booking.cancellation_policies LIMIT 1")
    if policy is None:
        pytest.skip("no cancellation policy seeded")
    await conn.execute("UPDATE booking.b2b_hold_lines SET status = 'cancelled' WHERE hold_id = $1::uuid", case["hold"])
    await conn.execute(
        "INSERT INTO booking.b2b_cancellations (hold_id, trip_case_id, partner_id, m2m_client_id, policy_id,"
        " reason_code, idempotency_key, departure_date, days_before_departure, fee_pct, currency, total_b2b_price,"
        " fee_amount, refundable_amount)"
        " VALUES ($1::uuid, $2::uuid, $3::uuid, 'it1074client', $4, 'client_cancelled', $5, $6, 60, 25, 'USD',"
        " 1000, 250, 750)",
        case["hold"],
        case["id"],
        case["partner"],
        policy,
        f"cancel-{uuid.uuid4().hex}",
        START,
    )
    row = await conn.fetchrow("SELECT * FROM booking.v_case_profit WHERE trip_case_id = $1::uuid", case["id"])
    assert row["cancelled"] and row["revenue"] == Decimal("250.00") and row["cost_usd"] == Decimal("0")
    assert not await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM booking.v_departure_cost_share WHERE trip_case_id = $1::uuid)", case["id"]
    )
