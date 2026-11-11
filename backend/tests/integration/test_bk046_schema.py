"""bk-046 (AA-1073) on a real PostgreSQL: the departure minimum / cutoff / decision columns and their CHECKs, the
departure.at_risk event, and the per-agency allotments (maker-checker, one in force and one pending per agency, append-
only facts, released once with an allotment.released event, ended once). A contract file. Every test runs in one
transaction logged in as role booking and is rolled back. Names are invented. No import of app code (the migration PR
stands alone)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import date, timedelta
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from tests.integration.conftest import apply_migrations

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

BK046 = Path(__file__).resolve().parents[3] / "migrations" / "046_departure_minimum_cutoff_allotments.sql"
START = date.today() + timedelta(days=60)
MAKER, CHECKER = "it-1073-maker", "it-1073-checker"


@pytest_asyncio.fixture
async def conn(migrated_admin_dsn: str, migrated_booking_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    # Tests of bk-022 / bk-023 re-run those files, which put back the older outbox trigger and event CHECK.
    admin = await asyncpg.connect(migrated_admin_dsn)
    try:
        await apply_migrations(admin, only=[46])
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


async def _departure(c: asyncpg.Connection, *, capacity: int = 10, **cols: object) -> str:
    plan = await c.fetchval(
        "INSERT INTO booking.trip_plans (source_system, source_id, sellable) VALUES ('cis', $1, true) RETURNING id",
        f"it-1073-{uuid.uuid4().hex[:8]}",
    )
    dep = await c.fetchval(
        "INSERT INTO booking.departures (trip_plan_id, start_date, capacity) VALUES ($1, $2, $3) RETURNING id::text",
        plan,
        START,
        capacity,
    )
    for col, value in cols.items():
        await c.execute(f"UPDATE booking.departures SET {col} = $2 WHERE id = $1::uuid", dep, value)  # noqa: S608
    return dep


async def _partner(c: asyncpg.Connection, name: str) -> str:
    tid = uuid.uuid4()
    return await c.fetchval(
        "INSERT INTO booking.b2b_partners (tenant_id, agency_name, status, billing_currency, m2m_client_id)"
        " VALUES ($1, $2, 'active', 'USD', 'it1073client') RETURNING id::text",
        tid,
        f"{name} {tid.hex[:4]}",
    )


async def _confirmed_line(c: asyncpg.Connection, partner: str, departure: str, seats: int) -> None:
    """A confirmed hold of ``seats`` on the departure (the agency is then "on" the departure for fan-out)."""
    quote = await c.fetchval(
        "INSERT INTO booking.b2b_quotes (partner_id, m2m_client_id, proposal_ref, proposal_version, currency,"
        " fx_rate_per_usd, fx_buffer_pct, fx_as_of, fx_source, pricing_rule_valid_from, margin_applied, card_fee_pct,"
        " rounding, priced_on, total_b2b_price, price_hash, valid_until)"
        " VALUES ($1::uuid, 'it1073client', $2, 1, 'USD', 1, 0, current_date, 'test', current_date, 0.15, 0,"
        " 'line_ceil_0.01', current_date, 100, $3, now() + interval '2 days') RETURNING id",
        partner,
        f"PR-{uuid.uuid4().hex[:8]}",
        "c" * 64,
    )
    case = await c.fetchval(
        "INSERT INTO booking.trip_cases (channel, party_tenant_id, status, start_date, end_date, pax, currency,"
        " source_system)"
        " VALUES ('b2b', gen_random_uuid(), 'confirmed', $1, $2, $3, 'USD', 'crm') RETURNING id",
        START,
        START + timedelta(days=3),
        seats,
    )
    hold = await c.fetchval(
        "INSERT INTO booking.b2b_holds (partner_id, m2m_client_id, quote_id, proposal_ref, proposal_version,"
        " idempotency_key, status, expires_at, trip_case_id, confirmed_at)"
        " VALUES ($1::uuid, 'it1073client', $2, $3, 1, $4, 'confirmed', now() + interval '1 day', $5, now())"
        " RETURNING id",
        partner,
        quote,
        f"PR-{uuid.uuid4().hex[:8]}",
        f"hold-{uuid.uuid4().hex[:12]}",
        case,
    )
    await c.execute(
        "INSERT INTO booking.b2b_hold_lines (hold_id, line_no, line_ref, departure_id, seats, held_as, status)"
        " VALUES ($1, 1, 'L1', $2::uuid, $3, 'available', 'confirmed')",
        hold,
        departure,
        seats,
    )


async def _allotment(c: asyncpg.Connection, departure: str, partner: str, seats: int = 4, **kw: object) -> str:
    return await c.fetchval(
        "INSERT INTO booking.departure_allotments (departure_id, partner_id, seats, release_days_before, created_by,"
        " replaces_id) VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6::uuid) RETURNING id::text",
        departure,
        partner,
        seats,
        kw.get("release_days_before", 14),
        kw.get("created_by", MAKER),
        kw.get("replaces"),
    )


async def _approve(c: asyncpg.Connection, allotment: str, by: str = CHECKER) -> None:
    await c.execute(
        "UPDATE booking.departure_allotments SET approved_by = $2, approved_at = now() WHERE id = $1::uuid",
        allotment,
        by,
    )


async def _events(c: asyncpg.Connection, departure: str) -> list[tuple[str, str]]:
    rows = await c.fetch(
        "SELECT event_type, partner_id::text AS partner FROM booking.outbox WHERE departure_id = $1::uuid ORDER BY id",
        departure,
    )
    return [(r["event_type"], r["partner"]) for r in rows]


async def test_registered_and_rerun_is_a_no_op(migrated_admin_dsn: str) -> None:
    admin = await asyncpg.connect(migrated_admin_dsn)
    try:
        assert await admin.fetchval("SELECT 1 FROM booking.v_schema_versions WHERE version = 'bk-046'") == 1
        before = await admin.fetchval("SELECT count(*) FROM pg_catalog.pg_constraint")
        triggers = await admin.fetchval("SELECT count(*) FROM pg_catalog.pg_trigger WHERE NOT tgisinternal")
        await apply_migrations(admin, only=[46])
        assert await admin.fetchval("SELECT count(*) FROM pg_catalog.pg_constraint") == before
        assert await admin.fetchval("SELECT count(*) FROM pg_catalog.pg_trigger WHERE NOT tgisinternal") == triggers
        owner = await admin.fetchval(
            "SELECT relowner::regrole::text FROM pg_class WHERE oid = 'booking.departure_allotments'::regclass"
        )
        assert owner == "bk_owner"
        assert BK046.read_text(encoding="utf-8").count("-- migrate:") == 0  # contract: no directive line
    finally:
        await admin.close()


async def test_departure_defaults_and_checks(conn: asyncpg.Connection) -> None:
    dep = await _departure(conn)
    row = await conn.fetchrow(
        "SELECT min_to_operate, sales_cutoff_days, decision_days_before, at_risk_at FROM booking.departures"
        " WHERE id = $1::uuid",
        dep,
    )
    assert dict(row) == {"min_to_operate": 0, "sales_cutoff_days": 0, "decision_days_before": 21, "at_risk_at": None}
    for col, bad in (
        ("min_to_operate", 11),
        ("min_to_operate", -1),
        ("sales_cutoff_days", 91),
        ("decision_days_before", 366),
    ):
        with pytest.raises(asyncpg.CheckViolationError):
            async with conn.transaction():
                await conn.execute(f"UPDATE booking.departures SET {col} = $2 WHERE id = $1::uuid", dep, bad)  # noqa: S608
    # The minimum follows the capacity: capacity below it is refused.
    await conn.execute("UPDATE booking.departures SET min_to_operate = 6 WHERE id = $1::uuid", dep)
    with pytest.raises(asyncpg.CheckViolationError):
        async with conn.transaction():
            await conn.execute("UPDATE booking.departures SET capacity = 5 WHERE id = $1::uuid", dep)
    # At risk needs a minimum.
    with pytest.raises(asyncpg.CheckViolationError):
        async with conn.transaction():
            await conn.execute(
                "UPDATE booking.departures SET min_to_operate = 0, at_risk_at = now() WHERE id = $1::uuid", dep
            )


async def test_at_risk_fans_out_once_to_the_agencies_on_the_departure(conn: asyncpg.Connection) -> None:
    dep = await _departure(conn, min_to_operate=6)
    a, b, idle = (
        await _partner(conn, "Lanternfield Travel"),
        await _partner(conn, "Copperbay Trips"),
        await _partner(conn, "Quietmoor Journeys"),
    )
    await _confirmed_line(conn, a, dep, 2)
    await _confirmed_line(conn, b, dep, 1)
    await conn.execute("UPDATE booking.departures SET at_risk_at = now() WHERE id = $1::uuid", dep)
    events = await _events(conn, dep)
    assert sorted(events) == sorted([("departure.at_risk", a), ("departure.at_risk", b)])
    assert idle not in {p for _, p in events}
    # Stamped once: a later update of the stamp or another column emits nothing more.
    await conn.execute(
        "UPDATE booking.departures SET at_risk_at = now() + interval '1 minute' WHERE id = $1::uuid", dep
    )
    await conn.execute("UPDATE booking.departures SET capacity = 12 WHERE id = $1::uuid", dep)
    assert len(await _events(conn, dep)) == 2
    # The guaranteed move still emits as before.
    await conn.execute("UPDATE booking.departures SET status = 'guaranteed' WHERE id = $1::uuid", dep)
    assert [t for t, _ in await _events(conn, dep)].count("departure.guaranteed") == 2


async def test_at_risk_on_a_guaranteed_departure_emits_nothing(conn: asyncpg.Connection) -> None:
    dep = await _departure(conn, min_to_operate=2)
    a = await _partner(conn, "Saltmarsh Holidays")
    await _confirmed_line(conn, a, dep, 1)
    await conn.execute("UPDATE booking.departures SET status = 'guaranteed' WHERE id = $1::uuid", dep)
    await conn.execute("UPDATE booking.departures SET at_risk_at = now() WHERE id = $1::uuid", dep)
    assert [t for t, _ in await _events(conn, dep)] == ["departure.guaranteed"]


async def test_allotment_maker_checker_and_one_in_force(conn: asyncpg.Connection) -> None:
    dep = await _departure(conn)
    agency = await _partner(conn, "Hollowpine Tours")
    first = await _allotment(conn, dep, agency, 4)
    with pytest.raises(asyncpg.CheckViolationError):  # the maker never approves their own
        async with conn.transaction():
            await _approve(conn, first, by=MAKER)
    with pytest.raises(asyncpg.UniqueViolationError):  # one pending per departure and agency
        async with conn.transaction():
            await _allotment(conn, dep, agency, 3)
    await _approve(conn, first)
    with pytest.raises(asyncpg.CheckViolationError):  # approved once
        async with conn.transaction():
            await _approve(conn, first, by="it-1073-other")
    # A change: a new pending row replacing the old; approving it while the old is in force breaks the unique index,
    # so the app ends the old one first in the same transaction.
    second = await _allotment(conn, dep, agency, 6, replaces=first)
    with pytest.raises(asyncpg.UniqueViolationError):
        async with conn.transaction():
            await _approve(conn, second)
    await conn.execute(
        "UPDATE booking.departure_allotments SET ended_at = now(), ended_by = $2, end_reason = 'replaced'"
        " WHERE id = $1::uuid",
        first,
        CHECKER,
    )
    await _approve(conn, second)
    live = await conn.fetch(
        "SELECT seats FROM booking.departure_allotments WHERE departure_id = $1::uuid AND approved_at IS NOT NULL"
        " AND ended_at IS NULL",
        dep,
    )
    assert [r["seats"] for r in live] == [6]
    # A replacement names a row of the same departure and agency.
    other = await _partner(conn, "Driftwood Escapes")
    with pytest.raises(asyncpg.CheckViolationError):
        async with conn.transaction():
            await _allotment(conn, dep, other, 2, replaces=second)


async def test_allotment_facts_never_change_and_ending_is_final(conn: asyncpg.Connection) -> None:
    dep = await _departure(conn)
    agency = await _partner(conn, "Mistral Ridge Travel")
    row = await _allotment(conn, dep, agency, 3)
    with pytest.raises(asyncpg.InsufficientPrivilegeError):  # role booking has no UPDATE on the facts
        async with conn.transaction():
            await conn.execute("UPDATE booking.departure_allotments SET seats = 5 WHERE id = $1::uuid", row)
    with pytest.raises(asyncpg.CheckViolationError):  # rejected only while pending; withdrawn only once approved
        async with conn.transaction():
            await conn.execute(
                "UPDATE booking.departure_allotments SET ended_at = now(), ended_by = $2, end_reason = 'withdrawn'"
                " WHERE id = $1::uuid",
                row,
                CHECKER,
            )
    await conn.execute(
        "UPDATE booking.departure_allotments SET ended_at = now(), ended_by = $2, end_reason = 'rejected'"
        " WHERE id = $1::uuid",
        row,
        CHECKER,
    )
    with pytest.raises(asyncpg.CheckViolationError):  # ended is final
        async with conn.transaction():
            await _approve(conn, row)
    with pytest.raises(asyncpg.InsufficientPrivilegeError):  # no DELETE
        async with conn.transaction():
            await conn.execute("DELETE FROM booking.departure_allotments WHERE id = $1::uuid", row)


async def test_release_once_emits_to_the_one_agency(conn: asyncpg.Connection) -> None:
    dep = await _departure(conn)
    agency, other = await _partner(conn, "Bramblegate Tours"), await _partner(conn, "Sunfold Voyages")
    await _confirmed_line(conn, other, dep, 2)
    row = await _allotment(conn, dep, agency, 4)
    with pytest.raises(asyncpg.CheckViolationError):  # only an approved allotment is released
        async with conn.transaction():
            await conn.execute("UPDATE booking.departure_allotments SET released_at = now() WHERE id = $1::uuid", row)
    await _approve(conn, row)
    await conn.execute("UPDATE booking.departure_allotments SET released_at = now() WHERE id = $1::uuid", row)
    assert await _events(conn, dep) == [("allotment.released", agency)]
    with pytest.raises(asyncpg.CheckViolationError):  # released once
        async with conn.transaction():
            await conn.execute(
                "UPDATE booking.departure_allotments SET released_at = now() + interval '1 hour' WHERE id = $1::uuid",
                row,
            )
    # A released allotment can still be ended (history kept), and that emits nothing.
    await conn.execute(
        "UPDATE booking.departure_allotments SET ended_at = now(), ended_by = 'system:test', end_reason ="
        " 'departure_cancelled' WHERE id = $1::uuid",
        row,
    )
    assert len(await _events(conn, dep)) == 1


async def test_cancelled_departure_takes_no_allotment(conn: asyncpg.Connection) -> None:
    dep = await _departure(conn)
    agency = await _partner(conn, "Larkspur Lines")
    await conn.execute("UPDATE booking.departures SET status = 'cancelled' WHERE id = $1::uuid", dep)
    with pytest.raises(asyncpg.CheckViolationError):
        async with conn.transaction():
            await _allotment(conn, dep, agency, 2)


async def test_outbox_types_and_allotment_event_shape(conn: asyncpg.Connection) -> None:
    with pytest.raises(asyncpg.InsufficientPrivilegeError):  # the app never writes the outbox by hand ... (no column)
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO booking.outbox (stream, event_type, partner_id, departure_id, occurred_at)"
                " VALUES ('b2b', 'allotment.released', gen_random_uuid(), gen_random_uuid(), now())"
            )
    with pytest.raises(asyncpg.CheckViolationError):  # ... and an allotment event names its departure
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO booking.outbox (stream, event_type, partner_id)"
                " VALUES ('b2b', 'allotment.released', gen_random_uuid())"
            )
    with pytest.raises(asyncpg.CheckViolationError):
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO booking.outbox (stream, event_type, partner_id, departure_id)"
                " VALUES ('b2b', 'departure.unknown', gen_random_uuid(), gen_random_uuid())"
            )


async def test_grants(conn: asyncpg.Connection) -> None:
    async def col(name: str, priv: str) -> bool:
        return await conn.fetchval(
            "SELECT has_column_privilege('booking', 'booking.departure_allotments', $1, $2)", name, priv
        )

    for name in ("approved_by", "approved_at", "released_at", "ended_at", "ended_by", "end_reason"):
        assert await col(name, "UPDATE"), name
    for name in ("seats", "release_days_before", "partner_id", "departure_id", "replaces_id", "created_by"):
        assert not await col(name, "UPDATE"), name
    assert not await conn.fetchval("SELECT has_table_privilege('cis_app', 'booking.departure_allotments', 'SELECT')")
