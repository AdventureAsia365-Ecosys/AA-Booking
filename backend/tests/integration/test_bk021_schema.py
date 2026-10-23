"""AA-974 (bk-021) schema on a real PostgreSQL: registration, re-run no-op, the settings rows, grants (both new tables
append-only for the app), owner bk_owner, the seeded default B2B cancellation policy (approved), the tiers CHECK,
maker-checker on cancellation-policy approvals, the extend columns' CHECK, the cancellation amounts CHECK, the backfill
of the policy on holds confirmed before bk-021, and no personal-data-looking column."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[3] / "migrations" / "021_b2b_commercial_rules.sql"
TABLES = ("cancellation_policies", "b2b_cancellations")
SEED = "00000000-0000-4000-8000-0000000bc021"
MAKER = "it-021-maker"


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    await conn.execute(MIGRATION.read_text(encoding="utf-8"))  # other modules may empty policy_approvals
    await conn.execute(
        "INSERT INTO booking.staff_users (cis_user_id, role, active, name) VALUES ($1, 'contracting', true, $1)"
        " ON CONFLICT (cis_user_id) DO NOTHING",
        MAKER,
    )
    try:
        yield conn
    finally:
        await conn.close()


class _Rollback(Exception):
    """Raised at the end of a test transaction so its rows are rolled back."""


async def test_registered_and_rerun_is_a_no_op(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 FROM booking.v_schema_versions WHERE version = 'bk-021'") == 1
    before = await admin.fetchval("SELECT count(*) FROM pg_catalog.pg_constraint")
    policies = await admin.fetchval("SELECT count(*) FROM booking.cancellation_policies")
    await admin.execute(MIGRATION.read_text(encoding="utf-8"))
    assert await admin.fetchval("SELECT count(*) FROM pg_catalog.pg_constraint") == before
    assert await admin.fetchval("SELECT count(*) FROM booking.cancellation_policies") == policies
    settings = dict(
        await admin.fetch(
            "SELECT key, value::text FROM booking.settings"
            " WHERE key IN ('b2b_hold_extend_hours', 'b2b_rehold_limit_per_day')"
        )
    )
    assert settings == {"b2b_hold_extend_hours": "72", "b2b_rehold_limit_per_day": "3"}


async def test_grants_and_owner(admin: asyncpg.Connection) -> None:
    def has(table: str, priv: str) -> Any:
        return admin.fetchval("SELECT has_table_privilege('booking', $1, $2)", f"booking.{table}", priv)

    for table in TABLES:
        for priv in ("SELECT", "INSERT"):
            assert await has(table, priv), (table, priv)
        for priv in ("UPDATE", "DELETE", "TRUNCATE"):
            assert not await has(table, priv), (table, priv)
    owners = await admin.fetch(
        "SELECT c.relname, c.relowner::regrole::text AS owner FROM pg_class c"
        " JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'booking' AND c.relname = ANY($1::text[])",
        list(TABLES),
    )
    assert {r["owner"] for r in owners} == {"bk_owner"} and len(owners) == len(TABLES)
    assert (
        await admin.fetchval(
            "SELECT proowner::regrole::text FROM pg_proc WHERE proname = 'cancellation_policy_maker_checker'"
        )
        == "bk_owner"
    )


async def test_seed_default_policy_is_approved(admin: asyncpg.Connection) -> None:
    row = await admin.fetchrow(
        "SELECT p.channel, p.trip_plan_id, p.tiers, a.approved_by FROM booking.cancellation_policies p"
        " JOIN booking.policy_approvals a ON a.cancellation_policy_id = p.id WHERE p.id = $1::uuid",
        SEED,
    )
    assert row is not None and row["channel"] == "b2b" and row["trip_plan_id"] is None
    assert row["approved_by"] == "seed:bk-021"
    tiers = json.loads(row["tiers"])
    assert [(t["min_days"], t["fee_pct"]) for t in tiers] == [(60, 0), (30, 25), (15, 50), (7, 75), (0, 100)]


@pytest.mark.parametrize(
    "tiers",
    [
        [],  # empty
        [{"min_days": 30, "fee_pct": 10}],  # no 0-day tier
        [{"min_days": 0, "fee_pct": 120}],  # above 100 %
        [{"min_days": 0, "fee_pct": -1}],  # negative
        [{"min_days": 0, "fee_pct": 100}, {"min_days": 900, "fee_pct": 0}],  # beyond 730 days
        [{"min_days": 0, "fee_pct": "100"}],  # not a number
        [{"min_days": 0}],  # fee missing
        [{"min_days": 0, "fee_pct": 100}, {"fee_pct": 0}],  # min_days missing
        {"min_days": 0, "fee_pct": 100},  # not an array
    ],
)
async def test_tiers_check(admin: asyncpg.Connection, tiers: Any) -> None:
    with pytest.raises(asyncpg.CheckViolationError):
        await admin.execute(
            "INSERT INTO booking.cancellation_policies (valid_from, title, tiers)"
            " VALUES ('2030-01-01', 'Bad', $1::jsonb)",
            json.dumps(tiers),
        )


async def test_policy_scope_unique_and_maker_checker(admin: asyncpg.Connection) -> None:
    good = json.dumps([{"min_days": 10, "fee_pct": 0}, {"min_days": 0, "fee_pct": 100}])
    try:
        async with admin.transaction():
            policy = await admin.fetchval(
                "INSERT INTO booking.cancellation_policies (valid_from, title, tiers, created_by)"
                " VALUES ('2031-01-01', 'Default 2031', $1::jsonb, $2) RETURNING id::text",
                good,
                MAKER,
            )
            with pytest.raises(asyncpg.UniqueViolationError):  # one default row per channel and day
                async with admin.transaction():
                    await admin.execute(
                        "INSERT INTO booking.cancellation_policies (valid_from, title, tiers)"
                        " VALUES ('2031-01-01', 'Again', $1::jsonb)",
                        good,
                    )
            with pytest.raises(asyncpg.CheckViolationError, match="maker-checker"):
                async with admin.transaction():
                    await admin.execute(
                        "INSERT INTO booking.policy_approvals (entity_type, cancellation_policy_id, approved_by)"
                        " VALUES ('cancellation_policy', $1::uuid, $2)",
                        policy,
                        MAKER,
                    )
            with pytest.raises(asyncpg.CheckViolationError):  # entity type must match the column
                async with admin.transaction():
                    await admin.execute(
                        "INSERT INTO booking.policy_approvals (entity_type, cancellation_policy_id, approved_by)"
                        " VALUES ('fx_rate', $1::uuid, 'someone')",
                        policy,
                    )
            await admin.execute(
                "INSERT INTO booking.policy_approvals (entity_type, cancellation_policy_id, approved_by)"
                " VALUES ('cancellation_policy', $1::uuid, 'checker')",
                policy,
            )
            with pytest.raises(asyncpg.UniqueViolationError):  # one approval per row
                async with admin.transaction():
                    await admin.execute(
                        "INSERT INTO booking.policy_approvals (entity_type, cancellation_policy_id, approved_by)"
                        " VALUES ('cancellation_policy', $1::uuid, 'checker2')",
                        policy,
                    )
            raise _Rollback
    except _Rollback:
        pass


async def test_extend_and_cancellation_checks(admin: asyncpg.Connection) -> None:
    tenant = str(uuid.uuid4())
    try:
        async with admin.transaction():
            partner = await admin.fetchval(
                "INSERT INTO booking.b2b_partners (tenant_id, agency_name, m2m_client_id)"
                " VALUES ($1::uuid, 'Schema Agency 21', 'client021') RETURNING id::text",
                tenant,
            )
            quote = await admin.fetchval(
                "INSERT INTO booking.b2b_quotes (partner_id, m2m_client_id, proposal_ref, proposal_version, currency,"
                " fx_rate_per_usd, fx_as_of, fx_source, pricing_rule_valid_from, margin_applied, card_fee_pct,"
                " rounding, priced_on, total_b2b_price, price_hash, valid_until)"
                " VALUES ($1::uuid, 'client021', 'p-21', 1, 'VND',"
                " 25000, current_date, 'synthetic', current_date, 0.15, 0.03, 'line_ceil_1000', current_date, 2000000,"
                " $2, now() + interval '1 day') RETURNING id::text",
                partner,
                "b" * 64,
            )
            hold = await admin.fetchval(
                "INSERT INTO booking.b2b_holds (partner_id, m2m_client_id, quote_id, proposal_ref, proposal_version,"
                " idempotency_key, expires_at) VALUES ($1::uuid, 'client021', $2::uuid, 'p-21', 1, 'key-021-1', $3)"
                " RETURNING id::text",
                partner,
                quote,
                datetime.now(UTC) + timedelta(hours=1),
            )
            bad_extends = (
                "extended_count = 1",  # count without the time
                "extended_at = now(), extend_reason = 'payment_claimed', extend_idempotency_key = 'ext-key-1'",
                "extended_count = 1, extended_at = now(), extend_reason = 'because',"
                " extend_idempotency_key = 'ext-key-1'",
                "extended_count = 1, extended_at = now(), extend_reason = 'client_recheck'",  # no key
            )
            for assignment in bad_extends:
                with pytest.raises(asyncpg.CheckViolationError):
                    async with admin.transaction():
                        await admin.execute(
                            f"UPDATE booking.b2b_holds SET {assignment} WHERE id = $1::uuid",  # noqa: S608 — literals
                            hold,
                        )
            await admin.execute(
                "UPDATE booking.b2b_holds SET extended_count = 1, extended_at = now(), extend_reason ="
                " 'payment_claimed', extend_idempotency_key = 'ext-key-1', cancellation_policy_id = $2::uuid"
                " WHERE id = $1::uuid",
                hold,
                SEED,
            )
            case = await admin.fetchval(
                "INSERT INTO booking.trip_cases (customer_id, status, source_system, source_id, channel,"
                " party_tenant_id) VALUES (NULL, 'confirmed', 'crm', 'p-21', 'b2b', $1::uuid) RETURNING id::text",
                tenant,
            )

            async def cancel(fee: int, refund: int, reason: str = "client_cancelled") -> None:
                await admin.execute(
                    "INSERT INTO booking.b2b_cancellations (hold_id, trip_case_id, partner_id, m2m_client_id,"
                    " policy_id, reason_code, idempotency_key, departure_date, days_before_departure, fee_pct,"
                    " currency, total_b2b_price, fee_amount, refundable_amount)"
                    " VALUES ($1::uuid, $2::uuid, $3::uuid, 'client021', $4::uuid, $5, 'cancel-021', current_date,"
                    " 20, 50, 'VND', 2000000, $6, $7)",
                    hold,
                    case,
                    partner,
                    SEED,
                    reason,
                    fee,
                    refund,
                )

            for fee, refund, reason in ((1_000_000, 999_999, "client_cancelled"), (1, 1_999_999, "whim")):
                with pytest.raises(asyncpg.CheckViolationError):
                    async with admin.transaction():
                        await cancel(fee, refund, reason)
            await cancel(1_000_000, 1_000_000)
            with pytest.raises(asyncpg.UniqueViolationError):  # one cancellation per booking
                async with admin.transaction():
                    await cancel(1_000_000, 1_000_000)
            raise _Rollback
    except _Rollback:
        pass


def _backfill_sql() -> str:
    """The backfill statement of the migration file, so the test runs the shipped text inside its own transaction."""
    text = MIGRATION.read_text(encoding="utf-8")
    start = text.index("UPDATE booking.b2b_holds SET cancellation_policy_id")
    return text[start : text.index(";", start) + 1]


async def test_backfill_locks_the_seed_policy_on_confirmed_holds(admin: asyncpg.Connection) -> None:
    """Holds confirmed before bk-021 get the seeded default locked; other holds and already locked ones keep theirs."""
    tenant = str(uuid.uuid4())
    try:
        async with admin.transaction():
            partner = await admin.fetchval(
                "INSERT INTO booking.b2b_partners (tenant_id, agency_name, m2m_client_id)"
                " VALUES ($1::uuid, 'Schema Agency 21b', 'client021b') RETURNING id::text",
                tenant,
            )
            quote = await admin.fetchval(
                "INSERT INTO booking.b2b_quotes (partner_id, m2m_client_id, proposal_ref, proposal_version, currency,"
                " fx_rate_per_usd, fx_as_of, fx_source, pricing_rule_valid_from, margin_applied, card_fee_pct,"
                " rounding, priced_on, total_b2b_price, price_hash, valid_until)"
                " VALUES ($1::uuid, 'client021b', 'p-21b', 1, 'USD', 1, current_date, 'usd', current_date, 0.15, 0.03,"
                " 'line_ceil_0.01', current_date, 100, $2, now() + interval '1 day') RETURNING id::text",
                partner,
                "c" * 64,
            )
            later = await admin.fetchval(
                "INSERT INTO booking.cancellation_policies (channel, valid_from, title, tiers)"
                " VALUES ('b2b', DATE '2026-09-01', 'Backdated', '[{\"min_days\": 0, \"fee_pct\": 10}]')"
                " RETURNING id::text"
            )

            async def hold(version: int, status: str, policy: str | None = None) -> str:
                case = None
                if status == "confirmed":
                    case = await admin.fetchval(
                        "INSERT INTO booking.trip_cases (customer_id, status, source_system, source_id, channel,"
                        " party_tenant_id) VALUES (NULL, 'confirmed', 'crm', $1, 'b2b', $2::uuid) RETURNING id::text",
                        f"p-21b-{version}",
                        tenant,
                    )
                return await admin.fetchval(
                    "INSERT INTO booking.b2b_holds (partner_id, m2m_client_id, quote_id, proposal_ref,"
                    " proposal_version, idempotency_key, expires_at, status, confirmed_at, trip_case_id,"
                    " cancellation_policy_id) VALUES ($1::uuid, 'client021b', $2::uuid, 'p-21b', $3, $4, $5, $6,"
                    " CASE WHEN $6 = 'confirmed' THEN now() END, $7::uuid, $8::uuid) RETURNING id::text",
                    partner,
                    quote,
                    version,
                    f"key-021b-{version}",
                    datetime.now(UTC) + timedelta(hours=1),
                    status,
                    case,
                    policy,
                )

            legacy = await hold(1, "confirmed")
            locked = await hold(2, "confirmed", later)
            active = await hold(3, "active")
            await admin.execute(_backfill_sql())
            await admin.execute(_backfill_sql())  # re-runnable
            got = dict(
                await admin.fetch(
                    "SELECT id::text, cancellation_policy_id::text FROM booking.b2b_holds WHERE id = ANY($1::uuid[])",
                    [legacy, locked, active],
                )
            )
            assert got == {legacy: SEED, locked: later, active: None}
            raise _Rollback
    except _Rollback:
        pass


async def test_no_personal_data_columns_in_new_tables(admin: asyncpg.Connection) -> None:
    cols = await admin.fetch(
        "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'booking'"
        " AND table_name = ANY($1::text[])",
        list(TABLES),
    )
    banned = ("name", "email", "phone", "passport", "birth", "dob", "traveller", "traveler", "supplier_net")
    assert [r for r in cols if any(b in r["column_name"] for b in banned)] == []
