"""AA-975 (bk-022) schema on a real PostgreSQL: registration and re-run no-op, the settings rows, grants (the outbox is
insert-through-columns + stamp-only for the app, no delete; incidents insert + clear only), owner bk_owner, the
business-day SLA function, the CHECKs (codes only, declined <=> decline_code, one open incident per departure), the
backfill of the answer deadline on on-request lines confirmed before bk-022, and no free-text column on the outbox or
the incidents (an event or an incident can never carry traveller data)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import asyncpg
import pytest
import pytest_asyncio

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[3] / "migrations" / "022_b2b_event_feed_desk.sql"
TABLES = ("outbox", "departure_incidents")
FUNCTIONS = (
    "b2b_answer_due",
    "outbox_departure_event",
    "b2b_holds_outbox",
    "b2b_hold_lines_outbox",
    "b2b_cancellations_outbox",
    "departures_outbox",
    "departure_incidents_outbox",
)
ICT = ZoneInfo("Asia/Ho_Chi_Minh")


class _Rollback(Exception):
    """Raised at the end of a test transaction so its rows are rolled back."""


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    try:
        yield conn
    finally:
        await conn.close()


async def test_registered_and_rerun_is_a_no_op(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 FROM booking.v_schema_versions WHERE version = 'bk-022'") == 1
    before = await admin.fetchval("SELECT count(*) FROM pg_catalog.pg_constraint")
    triggers = await admin.fetchval("SELECT count(*) FROM pg_catalog.pg_trigger WHERE NOT tgisinternal")
    await admin.execute(MIGRATION.read_text(encoding="utf-8"))
    assert await admin.fetchval("SELECT count(*) FROM pg_catalog.pg_constraint") == before
    assert await admin.fetchval("SELECT count(*) FROM pg_catalog.pg_trigger WHERE NOT tgisinternal") == triggers
    settings = dict(
        await admin.fetch(
            "SELECT key, value::text FROM booking.settings"
            " WHERE key IN ('b2b_on_request_answer_business_days', 'b2b_ops_timezone')"
        )
    )
    assert settings == {"b2b_on_request_answer_business_days": "1", "b2b_ops_timezone": '"Asia/Ho_Chi_Minh"'}


async def test_grants_and_owner(admin: asyncpg.Connection) -> None:
    async def table(name: str, priv: str) -> bool:
        return await admin.fetchval("SELECT has_table_privilege('booking', $1, $2)", f"booking.{name}", priv)

    async def column(name: str, col: str, priv: str) -> bool:
        return await admin.fetchval("SELECT has_column_privilege('booking', $1, $2, $3)", f"booking.{name}", col, priv)

    for name in TABLES:
        assert await table(name, "SELECT")
        for priv in ("DELETE", "TRUNCATE"):
            assert not await table(name, priv), (name, priv)
    # outbox: insert only the trigger columns, update only the stamp.
    assert not await table("outbox", "INSERT") and not await table("outbox", "UPDATE")
    assert await column("outbox", "event_type", "INSERT") and not await column("outbox", "feed_seq", "INSERT")
    assert await column("outbox", "feed_seq", "UPDATE") and await column("outbox", "sequenced_at", "UPDATE")
    for col in ("event_type", "partner_id", "hold_id", "occurred_at", "event_id"):
        assert not await column("outbox", col, "UPDATE"), col
    # incidents: insert, then only the clear columns change.
    assert await table("departure_incidents", "INSERT") and not await table("departure_incidents", "UPDATE")
    assert await column("departure_incidents", "cleared_at", "UPDATE")
    assert not await column("departure_incidents", "incident_code", "UPDATE")
    assert await admin.fetchval("SELECT has_sequence_privilege('booking', 'booking.outbox_feed_seq', 'USAGE')")
    owners = await admin.fetch(
        "SELECT c.relname, c.relowner::regrole::text AS owner FROM pg_class c JOIN pg_namespace n"
        " ON n.oid = c.relnamespace WHERE n.nspname = 'booking' AND c.relname = ANY($1::text[])",
        [*TABLES, "outbox_feed_seq"],
    )
    assert {r["owner"] for r in owners} == {"bk_owner"} and len(owners) == 3
    procs = await admin.fetch(
        "SELECT proname, proowner::regrole::text AS owner, prosecdef FROM pg_proc"
        " WHERE pronamespace = 'booking'::regnamespace AND proname = ANY($1::text[])",
        list(FUNCTIONS),
    )
    assert {p["proname"] for p in procs} == set(FUNCTIONS)
    assert {p["owner"] for p in procs} == {"bk_owner"} and not any(p["prosecdef"] for p in procs)


async def test_no_free_text_column_on_events_or_incidents(admin: asyncpg.Connection) -> None:
    """Linear AA-975 E: every text column of the outbox and the incidents is a CHECKed code or an id-like ref."""
    rows = await admin.fetch(
        "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'booking'"
        " AND table_name = ANY($1::text[]) AND data_type = 'text' ORDER BY 1, 2",
        list(TABLES),
    )
    assert {(r["table_name"], r["column_name"]) for r in rows} == {
        ("outbox", "stream"),
        ("outbox", "event_type"),
        ("outbox", "line_ref"),
        ("outbox", "incident_code"),
        ("outbox", "decline_code"),
        ("outbox", "entity_type"),  # bk-023 (AA-835): CHECKed hotel | service
        ("departure_incidents", "incident_code"),
        ("departure_incidents", "flagged_by"),
        ("departure_incidents", "cleared_by"),
    }
    for bad in ("Mr Synthetic fell ill", "weather "):
        with pytest.raises(asyncpg.CheckViolationError):
            await admin.execute(
                "INSERT INTO booking.outbox (stream, event_type, partner_id, departure_id, incident_code)"
                " VALUES ('b2b', 'departure.incident', gen_random_uuid(), gen_random_uuid(), $1)",
                bad,
            )


@pytest.mark.parametrize(
    ("start", "days", "due"),
    [
        (datetime(2026, 10, 21, 10, 0), 1, datetime(2026, 10, 22, 10, 0)),  # Wednesday → Thursday
        (datetime(2026, 10, 23, 15, 0), 1, datetime(2026, 10, 26, 15, 0)),  # Friday → Monday, same time
        (datetime(2026, 10, 24, 9, 30), 1, datetime(2026, 10, 27, 0, 0)),  # Saturday → clock from Monday 00:00
        (datetime(2026, 10, 25, 23, 0), 2, datetime(2026, 10, 28, 0, 0)),  # Sunday, 2 days → Wednesday 00:00
        (datetime(2026, 10, 22, 8, 0), 0, datetime(2026, 10, 22, 8, 0)),  # 0 days: the start itself
    ],
)
async def test_answer_due_counts_business_days_in_the_ops_zone(
    admin: asyncpg.Connection, start: datetime, days: int, due: datetime
) -> None:
    got = await admin.fetchval(
        "SELECT booking.b2b_answer_due($1, $2, 'Asia/Ho_Chi_Minh')", start.replace(tzinfo=ICT), days
    )
    assert got == due.replace(tzinfo=ICT)


async def test_answer_due_is_strict_and_capped(admin: asyncpg.Connection) -> None:
    """A NULL argument returns NULL at once (no endless loop); more than 10 business days count as 10."""
    start = datetime(2026, 10, 21, 10, 0, tzinfo=ICT)  # Wednesday
    assert await admin.fetchval("SELECT booking.b2b_answer_due(NULL, 1, 'Asia/Ho_Chi_Minh')") is None
    assert await admin.fetchval("SELECT booking.b2b_answer_due($1, NULL, 'Asia/Ho_Chi_Minh')", start) is None
    assert await admin.fetchval("SELECT booking.b2b_answer_due($1, 1, NULL)", start) is None
    capped = await admin.fetchval("SELECT booking.b2b_answer_due($1, 10000, 'Asia/Ho_Chi_Minh')", start)
    assert capped == datetime(2026, 11, 4, 10, 0, tzinfo=ICT)  # 10 business days: two weeks later
    assert await admin.fetchval("SELECT booking.b2b_answer_due($1, -3, 'Asia/Ho_Chi_Minh')", start) == start


async def test_hold_line_checks_and_one_open_incident(admin: asyncpg.Connection) -> None:
    """declined <=> a decline code from the list; answered_by needs answered_at; one open incident per departure."""
    with pytest.raises(_Rollback):
        async with admin.transaction():
            await admin.execute(
                "INSERT INTO booking.staff_users (cis_user_id, role, active, name) VALUES ('it-022', 'sales', true,"
                " 'it-022') ON CONFLICT (cis_user_id) DO NOTHING"
            )
            plan = await admin.fetchval(
                "INSERT INTO booking.trip_plans (source_system, source_id, sellable) VALUES ('cis',"
                " 'e1000000-0000-4000-8000-000000000022', true) RETURNING id"
            )
            dep = await admin.fetchval(
                "INSERT INTO booking.departures (trip_plan_id, start_date, capacity) VALUES ($1, $2, 4) RETURNING id",
                plan,
                (datetime.now() + timedelta(days=40)).date(),
            )
            await admin.execute(
                "INSERT INTO booking.departure_incidents (departure_id, incident_code, flagged_by)"
                " VALUES ($1, 'road', 'it-022')",
                dep,
            )
            with pytest.raises(asyncpg.UniqueViolationError):
                async with admin.transaction():
                    await admin.execute(
                        "INSERT INTO booking.departure_incidents (departure_id, incident_code, flagged_by)"
                        " VALUES ($1, 'weather', 'it-022')",
                        dep,
                    )
            with pytest.raises(asyncpg.CheckViolationError):
                async with admin.transaction():
                    await admin.execute(
                        "INSERT INTO booking.departure_incidents (departure_id, incident_code, flagged_by)"
                        " VALUES ($1, 'a traveller name', 'it-022')",
                        dep,
                    )
            await admin.execute(
                "UPDATE booking.departure_incidents SET cleared_at = now(), cleared_by = 'it-022'"
                " WHERE departure_id = $1",
                dep,
            )
            await admin.execute(  # cleared: a new one may open
                "INSERT INTO booking.departure_incidents (departure_id, incident_code, flagged_by)"
                " VALUES ($1, 'weather', 'it-022')",
                dep,
            )
            checks = dict(
                await admin.fetch(
                    "SELECT conname, pg_get_constraintdef(oid) AS def FROM pg_constraint"
                    " WHERE conrelid = 'booking.b2b_hold_lines'::regclass"
                    " AND conname IN ('b2b_hold_lines_decline_chk', 'b2b_hold_lines_answer_chk')"
                )
            )
            assert "decline_code IS NOT NULL" in checks["b2b_hold_lines_decline_chk"]
            assert "supplier_unavailable" in checks["b2b_hold_lines_decline_chk"]
            assert "answered_at IS NOT NULL" in checks["b2b_hold_lines_answer_chk"]
            raise _Rollback


async def test_backfill_sets_the_deadline_of_older_on_request_lines(admin: asyncpg.Connection) -> None:
    """The backfill statement of bk-022, replayed inside a transaction on a synthetic pre-bk-022 row."""
    sql = MIGRATION.read_text(encoding="utf-8")
    backfill = sql[sql.index("UPDATE booking.b2b_hold_lines l") : sql.index("-- ── Grants")].strip()
    with pytest.raises(_Rollback):
        async with admin.transaction():
            await admin.execute("ALTER TABLE booking.b2b_hold_lines DISABLE TRIGGER USER")
            partner = await admin.fetchval(
                "INSERT INTO booking.b2b_partners (tenant_id, agency_name, m2m_client_id)"
                " VALUES (gen_random_uuid(), 'Agency 022', 'client022') RETURNING id"
            )
            plan = await admin.fetchval(
                "INSERT INTO booking.trip_plans (source_system, source_id, sellable) VALUES ('cis',"
                " 'e1000000-0000-4000-8000-000000000122', true) RETURNING id"
            )
            quote = await admin.fetchval(
                "INSERT INTO booking.b2b_quotes (partner_id, m2m_client_id, proposal_ref, proposal_version, currency,"
                " fx_rate_per_usd, fx_buffer_pct, fx_as_of, fx_source, pricing_rule_valid_from, margin_applied,"
                " card_fee_pct, rounding, priced_on, total_b2b_price, price_hash, valid_until) VALUES ($1, 'client022',"
                " 'P-022', 1, 'USD', 1, 0, current_date, 'usd', current_date, 0.15, 0, 'cent', current_date, 100,"
                " repeat('a', 64), now() + interval '1 day') RETURNING id",
                partner,
            )
            assert plan is not None
            case = await admin.fetchval(
                "INSERT INTO booking.trip_cases (channel, party_tenant_id, source_system, source_id, status,"
                " idempotency_key) VALUES ('b2b', gen_random_uuid(), 'crm', 'P-022', 'confirmed', 'it-022-case')"
                " RETURNING id"
            )
            confirmed_at = datetime(2026, 10, 23, 15, 0, tzinfo=ICT)  # a Friday afternoon
            hold = await admin.fetchval(
                "INSERT INTO booking.b2b_holds (partner_id, m2m_client_id, quote_id, proposal_ref, proposal_version,"
                " idempotency_key, status, expires_at, created_at, confirmed_at, trip_case_id) VALUES ($1, 'client022',"
                " $2, 'P-022', 1, 'it-022-hold', 'confirmed', $3::timestamptz + interval '1 day', $3, $3, $4)"
                " RETURNING id",
                partner,
                quote,
                confirmed_at,
                case,
            )
            await admin.execute(
                "INSERT INTO booking.b2b_hold_lines (hold_id, line_no, line_ref, seats, held_as, status)"
                " VALUES ($1, 1, 'a', 0, 'on_request', 'on_request'), ($1, 2, 'b', 0, 'available', 'confirmed')",
                hold,
            )
            await admin.execute(backfill)
            got = dict(
                await admin.fetch("SELECT line_ref, answer_due_at FROM booking.b2b_hold_lines WHERE hold_id = $1", hold)
            )
            assert got == {"a": datetime(2026, 10, 26, 15, 0, tzinfo=ICT), "b": None}
            raise _Rollback
