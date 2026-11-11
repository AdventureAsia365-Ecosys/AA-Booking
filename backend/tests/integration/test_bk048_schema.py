"""bk-048 (AA-1073 rest) on a real PostgreSQL: the ops sheet columns and departure_staff, departure.updated on a meeting
change, the departure fees (maker-checker, final decision, departure.fee_charged with the amount, the settlement role
departure_fee and one live line per fee), the price adjustments (one in force and one pending, maker-checker, append-
only facts), the early-booking rule columns, the quote line price_note and the occupancy view. A contract file. Every
test runs in one transaction logged in as role booking and is rolled back. Names are invented. No import of app code
(the migration PR stands alone)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import date, time, timedelta
from decimal import Decimal
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from tests.integration.conftest import apply_migrations
from tests.integration.test_bk046_schema import CHECKER, MAKER, _allotment, _approve, _confirmed_line, _partner

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

BK048 = Path(__file__).resolve().parents[3] / "migrations" / "048_departure_board_ops_fees_price_adjustments.sql"
START = date.today() + timedelta(days=75)


@pytest_asyncio.fixture
async def conn(migrated_admin_dsn: str, migrated_booking_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    # Tests of bk-022 / bk-041 / bk-046 / bk-047 re-run those files (older outbox CHECK and trigger, invoice line
    # CHECKs, the outbox column grant): put bk-048 back first.
    admin = await asyncpg.connect(migrated_admin_dsn)
    try:
        await apply_migrations(admin, only=[48])
    finally:
        await admin.close()
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


async def _departure(c: asyncpg.Connection, capacity: int = 10) -> str:
    plan = await c.fetchval(
        "INSERT INTO booking.trip_plans (source_system, source_id, sellable) VALUES ('cis', $1, true) RETURNING id",
        f"it-1073r-{uuid.uuid4().hex[:8]}",
    )
    return await c.fetchval(
        "INSERT INTO booking.departures (trip_plan_id, start_date, capacity) VALUES ($1, $2, $3) RETURNING id::text",
        plan,
        START,
        capacity,
    )


async def _active_hold(c: asyncpg.Connection, partner: str, departure: str, seats: int) -> None:
    quote = await c.fetchval(
        "INSERT INTO booking.b2b_quotes (partner_id, m2m_client_id, proposal_ref, proposal_version, currency,"
        " fx_rate_per_usd, fx_buffer_pct, fx_as_of, fx_source, pricing_rule_valid_from, margin_applied, card_fee_pct,"
        " rounding, priced_on, total_b2b_price, price_hash, valid_until)"
        " VALUES ($1::uuid, 'it1073client', $2, 1, 'USD', 1, 0, current_date, 'test', current_date, 0.15, 0,"
        " 'line_ceil_0.01', current_date, 100, $3, now() + interval '2 days') RETURNING id",
        partner,
        f"PR-{uuid.uuid4().hex[:8]}",
        "d" * 64,
    )
    hold = await c.fetchval(
        "INSERT INTO booking.b2b_holds (partner_id, m2m_client_id, quote_id, proposal_ref, proposal_version,"
        " idempotency_key, status, expires_at) VALUES ($1::uuid, 'it1073client', $2, $3, 1, $4, 'active',"
        " now() + interval '1 day') RETURNING id",
        partner,
        quote,
        f"PR-{uuid.uuid4().hex[:8]}",
        f"hold-{uuid.uuid4().hex[:12]}",
    )
    await c.execute(
        "INSERT INTO booking.b2b_hold_lines (hold_id, line_no, line_ref, departure_id, seats, held_as, status)"
        " VALUES ($1, 1, 'L1', $2::uuid, $3, 'available', 'available')",
        hold,
        departure,
        seats,
    )


async def _events(c: asyncpg.Connection, departure: str) -> list[tuple[str, str]]:
    rows = await c.fetch(
        "SELECT event_type, partner_id::text AS partner FROM booking.outbox WHERE departure_id = $1::uuid ORDER BY id",
        departure,
    )
    return [(r["event_type"], r["partner"]) for r in rows]


async def _fee(c: asyncpg.Connection, departure: str, partner: str, **kw: object) -> str:
    return await c.fetchval(
        "INSERT INTO booking.departure_fees (departure_id, partner_id, reason, seats, price_per_seat, currency, amount,"
        " note, created_by) VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8, $9) RETURNING id::text",
        departure,
        partner,
        kw.get("reason", "min_shortfall"),
        kw.get("seats", 2),
        kw.get("price", Decimal("40.00")),
        kw.get("currency", "USD"),
        kw.get("amount", Decimal("80.00")),
        kw.get("note"),
        kw.get("created_by", MAKER),
    )


async def _adjustment(c: asyncpg.Connection, departure: str, **kw: object) -> str:
    return await c.fetchval(
        "INSERT INTO booking.departure_price_adjustments (departure_id, adjust_pct, amount, currency, reason,"
        " replaces_id, created_by) VALUES ($1::uuid, $2, $3, $4, $5, $6::uuid, $7) RETURNING id::text",
        departure,
        kw.get("pct"),
        kw.get("amount"),
        kw.get("currency"),
        kw.get("reason", "Low season push"),
        kw.get("replaces"),
        kw.get("created_by", MAKER),
    )


async def test_registered_and_rerun_is_a_no_op(migrated_admin_dsn: str) -> None:
    admin = await asyncpg.connect(migrated_admin_dsn)
    try:
        await apply_migrations(admin, only=[48])
        assert await admin.fetchval("SELECT 1 FROM booking.v_schema_versions WHERE version = 'bk-048'") == 1
        before = await admin.fetchval("SELECT count(*) FROM pg_catalog.pg_constraint")
        triggers = await admin.fetchval("SELECT count(*) FROM pg_catalog.pg_trigger WHERE NOT tgisinternal")
        clauses = await admin.fetchval("SELECT count(*) FROM booking.partner_terms_clause")
        await apply_migrations(admin, only=[48])
        assert await admin.fetchval("SELECT count(*) FROM pg_catalog.pg_constraint") == before
        assert await admin.fetchval("SELECT count(*) FROM pg_catalog.pg_trigger WHERE NOT tgisinternal") == triggers
        assert await admin.fetchval("SELECT count(*) FROM booking.partner_terms_clause") == clauses
        for table in ("departure_staff", "departure_fees", "departure_price_adjustments", "v_departure_occupancy"):
            owner = await admin.fetchval(
                "SELECT relowner::regrole::text FROM pg_class WHERE oid = $1::regclass", f"booking.{table}"
            )
            assert owner == "bk_owner", table
        clause = await admin.fetchrow(
            "SELECT status, updated_by FROM booking.partner_terms_clause WHERE code = 'departure_fees'"
        )
        assert dict(clause) == {"status": "draft", "updated_by": "system:bk-048"}  # the business owner approves it
        assert BK048.read_text(encoding="utf-8").count("-- migrate:") == 0  # contract: no directive line
    finally:
        await admin.close()


async def test_ops_sheet_checks_and_departure_updated(conn: asyncpg.Connection) -> None:
    dep = await _departure(conn)
    a, idle = await _partner(conn, "Lanternfield Travel"), await _partner(conn, "Quietmoor Journeys")
    await _confirmed_line(conn, a, dep, 2)
    for col, bad in (("meeting_point", "   "), ("meeting_point", "x" * 301), ("ops_note", "y" * 2001)):
        with pytest.raises(asyncpg.CheckViolationError):
            async with conn.transaction():
                await conn.execute(f"UPDATE booking.departures SET {col} = $2 WHERE id = $1::uuid", dep, bad)  # noqa: S608
    await conn.execute(
        "UPDATE booking.departures SET meeting_point = 'Old Quarter hotel lobby', meeting_time = $2"
        " WHERE id = $1::uuid",
        dep,
        time(7, 30),
    )
    assert await _events(conn, dep) == [("departure.updated", a)]
    # The staff note and an unchanged meeting point send nothing; a new time does.
    await conn.execute("UPDATE booking.departures SET ops_note = 'Coach 2 waits' WHERE id = $1::uuid", dep)
    await conn.execute(
        "UPDATE booking.departures SET meeting_point = 'Old Quarter hotel lobby' WHERE id = $1::uuid", dep
    )
    assert len(await _events(conn, dep)) == 1
    await conn.execute("UPDATE booking.departures SET meeting_time = $2 WHERE id = $1::uuid", dep, time(8, 0))
    assert [t for t, _ in await _events(conn, dep)] == ["departure.updated", "departure.updated"]
    assert idle not in {p for _, p in await _events(conn, dep)}
    # A cancelled departure tells the agencies it is cancelled, not that its meeting point moved.
    await conn.execute(
        "UPDATE booking.departures SET status = 'cancelled', meeting_point = NULL WHERE id = $1::uuid", dep
    )
    assert [t for t, _ in await _events(conn, dep)][-1] == "departure.cancelled"
    assert [t for t, _ in await _events(conn, dep)].count("departure.updated") == 2


async def test_departure_staff_one_kind_of_person(conn: asyncpg.Connection) -> None:
    dep = await _departure(conn)
    supplier = await conn.fetchval(
        "INSERT INTO booking.suppliers (code, company_name, categories, status) VALUES ($1, 'Ridgeway Guides',"
        " ARRAY['guide'], 'active') RETURNING id",
        f"SUP-{uuid.uuid4().hex[:6]}",
    )
    insert = (
        "INSERT INTO booking.departure_staff (departure_id, role, staff_user_id, supplier_id, assigned_by)"
        " VALUES ($1::uuid, $2, $3, $4, $5)"
    )
    await conn.execute(insert, dep, "tour_leader", MAKER, None, CHECKER)
    await conn.execute(insert, dep, "guide", None, supplier, CHECKER)
    for role, staff, sup in (("guide", MAKER, supplier), ("driver", None, None), ("cook", MAKER, None)):
        with pytest.raises(asyncpg.CheckViolationError):
            async with conn.transaction():
                await conn.execute(insert, dep, role, staff, sup, CHECKER)
    with pytest.raises(asyncpg.UniqueViolationError):
        async with conn.transaction():
            await conn.execute(insert, dep, "tour_leader", MAKER, None, CHECKER)
    assert await conn.execute("DELETE FROM booking.departure_staff WHERE departure_id = $1::uuid", dep) == "DELETE 2"


async def test_fee_maker_checker_final_and_event(conn: asyncpg.Connection) -> None:
    dep = await _departure(conn)
    agency = await _partner(conn, "Copperbay Trips")
    with pytest.raises(asyncpg.CheckViolationError):  # amount = seats x price
        async with conn.transaction():
            await _fee(conn, dep, agency, amount=Decimal("81.00"))
    with pytest.raises(asyncpg.CheckViolationError):  # reason other needs a note
        async with conn.transaction():
            await _fee(conn, dep, agency, reason="other")
    with pytest.raises(asyncpg.CheckViolationError):  # starts pending
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO booking.departure_fees (departure_id, partner_id, reason, seats, price_per_seat, currency,"
                " amount, created_by, status, decided_by, decided_at) VALUES ($1::uuid, $2::uuid, 'min_shortfall', 1,"
                " 5, 'USD', 5, $3, 'approved', $4, now())",
                dep,
                agency,
                MAKER,
                CHECKER,
            )
    fee = await _fee(conn, dep, agency)
    decide = "UPDATE booking.departure_fees SET status = $2, decided_by = $3, decided_at = now() WHERE id = $1::uuid"
    with pytest.raises(asyncpg.CheckViolationError):  # never the maker
        async with conn.transaction():
            await conn.execute(decide, fee, "approved", MAKER)
    with pytest.raises(asyncpg.InsufficientPrivilegeError):  # the facts are not even writable by the app
        async with conn.transaction():
            await conn.execute("UPDATE booking.departure_fees SET amount = 1 WHERE id = $1::uuid", fee)
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        async with conn.transaction():
            await conn.execute("DELETE FROM booking.departure_fees WHERE id = $1::uuid", fee)
    await conn.execute(decide, fee, "approved", CHECKER)
    with pytest.raises(asyncpg.CheckViolationError):  # final
        async with conn.transaction():
            await conn.execute(decide, fee, "rejected", "it-1073-other")
    row = await conn.fetchrow(
        "SELECT event_type, partner_id::text AS partner, fee_id::text AS fee, amount, currency FROM booking.outbox"
        " WHERE departure_id = $1::uuid",
        dep,
    )
    assert dict(row) == {
        "event_type": "departure.fee_charged",
        "partner": agency,
        "fee": fee,
        "amount": Decimal("80.00"),
        "currency": "USD",
    }
    # A rejected fee emits nothing.
    other = await _fee(conn, dep, agency, reason="other", note="Late name change of the group")
    await conn.execute(decide, other, "rejected", CHECKER)
    assert len(await _events(conn, dep)) == 1


async def test_settlement_line_bills_a_fee_once(conn: asyncpg.Connection) -> None:
    dep = await _departure(conn)
    agency = await _partner(conn, "Saltmarsh Holidays")
    fee = await _fee(conn, dep, agency)
    await conn.execute(
        "UPDATE booking.departure_fees SET status = 'approved', decided_by = $2, decided_at = now()"
        " WHERE id = $1::uuid",
        fee,
        CHECKER,
    )
    invoice = await conn.fetchval(
        "INSERT INTO booking.agency_invoice (party_tenant_id, invoice_kind, period_start, period_end, currency,"
        " tax_rate, source, created_by, idempotency_key) SELECT tenant_id, 'settlement', '2026-10-01',"
        " '2026-11-01', 'USD', 0, 'job', 'it-1073', $1 FROM booking.b2b_partners WHERE id = $2::uuid RETURNING id",
        f"it-1073-{uuid.uuid4().hex[:8]}",
        agency,
    )
    line = (
        "INSERT INTO booking.agency_invoice_line (invoice_id, invoice_kind, line_no, line_role, source_fee_id,"
        " description, quantity, unit_price, amount, created_by) VALUES ($1, 'settlement', $2, $3, $4::uuid,"
        " 'Departure fee', 1, 80, 80, 'it-1073')"
    )
    with pytest.raises(asyncpg.CheckViolationError):  # a departure_fee line names its fee
        async with conn.transaction():
            await conn.execute(line, invoice, 1, "departure_fee", None)
    with pytest.raises(asyncpg.CheckViolationError):  # only that role names a fee
        async with conn.transaction():
            await conn.execute(line, invoice, 1, "adjustment", fee)
    await conn.execute(line, invoice, 1, "departure_fee", fee)
    with pytest.raises(asyncpg.UniqueViolationError):  # billed once while not voided
        async with conn.transaction():
            await conn.execute(line, invoice, 2, "departure_fee", fee)


async def test_price_adjustments_one_in_force_maker_checker(conn: asyncpg.Connection) -> None:
    dep = await _departure(conn)
    for bad in (
        {"pct": Decimal("-0.1"), "amount": Decimal("10"), "currency": "USD"},
        {},
        {"pct": Decimal("-0.6")},
        {"amount": Decimal("10")},
        {"pct": Decimal("0")},
        {"pct": Decimal("0.1"), "reason": " "},
    ):
        with pytest.raises(asyncpg.CheckViolationError):
            async with conn.transaction():
                await _adjustment(conn, dep, **bad)
    first = await _adjustment(conn, dep, pct=Decimal("-0.1000"))
    approve = "UPDATE booking.departure_price_adjustments SET approved_by = $2, approved_at = now() WHERE id = $1::uuid"
    with pytest.raises(asyncpg.CheckViolationError):
        async with conn.transaction():
            await conn.execute(approve, first, MAKER)
    with pytest.raises(asyncpg.UniqueViolationError):  # one pending per departure
        async with conn.transaction():
            await _adjustment(conn, dep, amount=Decimal("-15.00"), currency="USD")
    await conn.execute(approve, first, CHECKER)
    second = await _adjustment(conn, dep, amount=Decimal("-15.00"), currency="USD", replaces=first)
    with pytest.raises(asyncpg.UniqueViolationError):  # one in force: the old ends first
        async with conn.transaction():
            await conn.execute(approve, second, CHECKER)
    with pytest.raises(asyncpg.InsufficientPrivilegeError):  # the facts are not writable by the app
        async with conn.transaction():
            await conn.execute(
                "UPDATE booking.departure_price_adjustments SET approved_at = approved_at, reason = 'x'"
                " WHERE id = $1::uuid",
                first,
            )
    await conn.execute(
        "UPDATE booking.departure_price_adjustments SET ended_at = now(), ended_by = $2, end_reason = 'replaced'"
        " WHERE id = $1::uuid",
        first,
        CHECKER,
    )
    await conn.execute(approve, second, CHECKER)
    with pytest.raises(asyncpg.CheckViolationError):  # ended is final
        async with conn.transaction():
            await conn.execute(
                "UPDATE booking.departure_price_adjustments SET end_reason = 'withdrawn' WHERE id = $1::uuid", first
            )
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        async with conn.transaction():
            await conn.execute("DELETE FROM booking.departure_price_adjustments WHERE id = $1::uuid", first)


async def test_early_booking_rule_columns(conn: asyncpg.Connection) -> None:
    insert = (
        "INSERT INTO booking.pricing_rules (channel, target_margin, floor_margin, peak_uplift_pts, card_fee_pct,"
        " valid_from, created_by, early_days, early_discount_pts)"
        " VALUES ($1, 0.20, 0.10, 0.05, 0.03, $2, 'it-1073-maker', $3, $4)"
    )
    day = date(2099, 1, 1) + timedelta(days=uuid.uuid4().int % 300)
    await conn.execute(insert, "b2b", day, 60, Decimal("0.03"))
    await conn.execute(insert, "fixed_departure", day, None, None)
    for channel, days, pts in (
        ("b2c_tailor_made", 60, Decimal("0.03")),
        ("b2b", 60, None),
        ("b2b", 0, Decimal("0.03")),
        ("b2b", 60, Decimal("0.25")),  # more than the target margin
    ):
        with pytest.raises(asyncpg.CheckViolationError):
            async with conn.transaction():
                await conn.execute(insert, channel, day + timedelta(days=400), days, pts)


async def test_quote_line_price_note_is_an_object(conn: asyncpg.Connection) -> None:
    agency = await _partner(conn, "Hollowpine Tours")
    quote = await conn.fetchval(
        "INSERT INTO booking.b2b_quotes (partner_id, m2m_client_id, proposal_ref, proposal_version, currency,"
        " fx_rate_per_usd, fx_buffer_pct, fx_as_of, fx_source, pricing_rule_valid_from, margin_applied, card_fee_pct,"
        " rounding, priced_on, total_b2b_price, price_hash, valid_until)"
        " VALUES ($1::uuid, 'it1073client', 'PR-NOTE', 1, 'USD', 1, 0, current_date, 'test', current_date, 0.15, 0,"
        " 'line_ceil_0.01', current_date, 100, $2, now() + interval '2 days') RETURNING id",
        agency,
        "e" * 64,
    )
    line = (
        "INSERT INTO booking.b2b_quote_lines (quote_id, line_no, line_ref, component_id, component_kind, service_date,"
        " pax_adult, pax_child, pax_infant, rooms, unit, availability, b2b_price, net_cost_usd, supplier_rate_ids,"
        " agency_discount_pct, price_note) VALUES ($1, $2, $3, 'tp:x', 'trip_plan', current_date, 2, 0, 0, '[]',"
        " 'per_line', 'available', 100, 80, '{}', 0, $4::jsonb)"
    )
    await conn.execute(line, quote, 1, "a", '{"early_booking": {"days": 60, "pts": "0.03"}}')
    await conn.execute(line, quote, 2, "b", None)
    with pytest.raises(asyncpg.CheckViolationError):
        async with conn.transaction():
            await conn.execute(line, quote, 3, "c", '["below_floor"]')


async def test_occupancy_view_counts_like_the_app(conn: asyncpg.Connection) -> None:
    dep = await _departure(conn, capacity=10)
    sold, live, allotted = (
        await _partner(conn, "Lanternfield Travel"),
        await _partner(conn, "Copperbay Trips"),
        await _partner(conn, "Quietmoor Journeys"),
    )
    await _confirmed_line(conn, sold, dep, 3)
    await _active_hold(conn, live, dep, 2)
    allotment = await _allotment(conn, dep, allotted, 4)
    await _approve(conn, allotment)
    await conn.execute("UPDATE booking.departures SET min_to_operate = 5, at_risk_at = now() WHERE id = $1::uuid", dep)
    await conn.execute(
        "INSERT INTO booking.departure_incidents (departure_id, incident_code, flagged_by)"
        " VALUES ($1::uuid, 'road', $2)",
        dep,
        MAKER,
    )
    row = await conn.fetchrow("SELECT * FROM booking.v_departure_occupancy WHERE departure_id = $1::uuid", dep)
    assert (row["held"], row["sold"], row["allotted"], row["allotment_unused"], row["free"]) == (2, 3, 4, 4, 1)
    assert row["occupancy_pct"] == Decimal("50.0")
    assert (row["agencies_count"], row["at_risk"], row["open_incident_code"]) == (2, True, "road")
    await conn.execute("UPDATE booking.departures SET status = 'cancelled' WHERE id = $1::uuid", dep)
    row = await conn.fetchrow(
        "SELECT free, at_risk FROM booking.v_departure_occupancy WHERE departure_id = $1::uuid", dep
    )
    assert dict(row) == {"free": 0, "at_risk": False}


async def test_erasure_marks_staff_text_only(migrated_admin_dsn: str, conn: asyncpg.Connection) -> None:
    """DM-6.7: as bk_owner, a fee note and an adjustment reason may be set to [redacted], alone (even when final)."""
    admin = await asyncpg.connect(migrated_admin_dsn)
    tx = admin.transaction()
    await tx.start()
    try:
        await admin.execute("SET LOCAL ROLE bk_owner")
        dep = await _departure(admin)
        agency = await _partner(admin, "Hollowpine Tours")
        fee = await _fee(admin, dep, agency, reason="other", note="Agency asked by phone")
        await admin.execute(
            "UPDATE booking.departure_fees SET status = 'approved', decided_by = $2, decided_at = now()"
            " WHERE id = $1::uuid",
            fee,
            CHECKER,
        )
        adj = await _adjustment(admin, dep, pct=Decimal("0.05"))
        await admin.execute("UPDATE booking.departure_fees SET note = '[redacted]' WHERE id = $1::uuid", fee)
        await admin.execute(
            "UPDATE booking.departure_price_adjustments SET reason = '[redacted]' WHERE id = $1::uuid", adj
        )
        with pytest.raises(asyncpg.CheckViolationError):  # facts stay fixed even for the owner
            async with admin.transaction():
                await admin.execute("UPDATE booking.departure_fees SET seats = 3 WHERE id = $1::uuid", fee)
        with pytest.raises(asyncpg.CheckViolationError):  # and another text is refused
            async with admin.transaction():
                await admin.execute(
                    "UPDATE booking.departure_price_adjustments SET reason = 'other text' WHERE id = $1::uuid", adj
                )
    finally:
        await tx.rollback()
        await admin.close()
