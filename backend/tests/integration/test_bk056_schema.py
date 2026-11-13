"""bk-056 (S279 thresholds program, lane L3) on a real PostgreSQL: Booking's threshold and policy values as pricing
policy parameters.

The seed is version 1 of 19 new parameters, approved by the tech lead (S279), each equal to the value in force before
the move: the booking.settings row when it held a whole number inside the range the code accepted, else the code's
default. The moved settings rows say "Moved (bk-056)", the dead B2C rows and ops_docs_max_bytes "Retired (bk-056)";
no row is deleted. The bk-027 trip check seeding trigger reads pre_departure_days from the policy. A re-run is a no-op.
Names and values in the hand-made rows are invented."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import timedelta

import asyncpg
import pytest
import pytest_asyncio

from tests.integration import ops
from tests.integration.conftest import MIGRATIONS, apply_migrations, as_booking_owner

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

SEED = {
    "b2b_hold_hours": 48,
    "b2b_hold_extend_hours": 72,
    "b2b_hold_max_extensions": 1,
    "b2b_rehold_limit_per_day": 3,
    "b2b_on_request_answer_business_days": 1,
    "b2b_on_request_due_soon_hours": 4,
    "b2b_child_age_max": 11,
    "b2b_infant_age_max": 1,
    "departure_decision_days_default": 21,
    "pre_departure_days": 14,
    "supplier_answer_days": 2,
    "trip_check_reminder_days": 3,
    "supplier_bill_due_soon_days": 3,
    "emergency_contact_keep_days": 90,
    "agency_reminder_steps_days": [1, 7, 14],
    "rounding_increment_by_currency": {"AUD": "5", "EUR": "5", "GBP": "5", "SGD": "5", "USD": "5"},
    "retention_job_succeeded_days": 30,
    "retention_job_failed_days": 90,
    "retention_inbox_redact_days": 90,
}
MOVED = (
    "b2b_hold_hours",
    "b2b_hold_extend_hours",
    "b2b_rehold_limit_per_day",
    "b2b_on_request_answer_business_days",
    "b2b_child_age_max",
    "b2b_infant_age_max",
    "pre_departure_days",
    "supplier_answer_days",
    "trip_check_reminder_days",
    "supplier_bill_due_soon_days",
    "emergency_contact_keep_days",
)
RETIRED = (
    "deposit_pct",
    "quote_validity_days",
    "deposit_due_days",
    "balance_due_days_before_start",
    "payment_due_soon_days",
    "payment_overdue_reminder_days",
    "invoice_issuer",
    "payment_instructions",
    "ops_docs_max_bytes",
)
KEPT = ("b2b_ops_timezone", "b2b_ops_contact")
BK056 = next(p for p in MIGRATIONS if p.name.startswith("056_"))
SEED_ROWS = "SELECT id FROM booking.pricing_policy WHERE created_by IN ('seed:bk-056')"
UNDO_SEED = f"""
DELETE FROM booking.pricing_policy_events WHERE policy_id IN ({SEED_ROWS});
DELETE FROM booking.pricing_policy WHERE created_by IN ('seed:bk-056');
"""


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    try:
        yield conn
    finally:
        await conn.close()


async def _seed(conn: asyncpg.Connection) -> dict[str, tuple[object, int, str]]:
    rows = await conn.fetch(
        "SELECT p.param_key, p.value::text AS value, p.version_no, a.actor"
        " FROM booking.pricing_policy p JOIN booking.pricing_policy_events a ON a.policy_id = p.id"
        " AND a.event = 'approved' WHERE p.created_by IN ('seed:bk-056')"
    )
    return {r["param_key"]: (json.loads(r["value"]), r["version_no"], r["actor"]) for r in rows}


async def test_seed_is_todays_value_approved_by_the_tech_lead(admin: asyncpg.Connection) -> None:
    seed = await _seed(admin)
    assert {k: v[0] for k, v in seed.items()} == SEED
    assert {(v[1], v[2]) for v in seed.values()} == {(1, "tech lead S279")}
    submitted = await admin.fetchval(
        f"SELECT count(*) FROM booking.pricing_policy_events WHERE event = 'submitted' AND policy_id IN ({SEED_ROWS})"
    )
    assert submitted == len(SEED)


async def test_settings_rows_are_marked_never_deleted(admin: asyncpg.Connection) -> None:
    rows = {r["key"]: r["description"] for r in await admin.fetch("SELECT key, description FROM booking.settings")}
    for key in MOVED:
        assert rows[key].startswith(f"Moved (bk-056): now the pricing policy parameter {key} "), key
    for key in RETIRED:
        assert rows[key].startswith("Retired (bk-056)"), key
    for key in KEPT:
        assert key in rows and "bk-056" not in (rows[key] or ""), key


async def test_rerun_is_a_no_op(admin: asyncpg.Connection) -> None:
    before = await admin.fetchval("SELECT count(*) FROM booking.pricing_policy_events")
    await apply_migrations(admin, only=[56])
    assert await admin.fetchval("SELECT count(*) FROM booking.pricing_policy_events") == before
    assert {k: v[0] for k, v in (await _seed(admin)).items()} == SEED


async def test_seed_copies_the_settings_value_in_force_else_the_code_default(admin: asyncpg.Connection) -> None:
    """A settings row changed out of band (run-script) is what the code read, so it is the seed; a row outside the
    range the code accepted fell back to the code default, so the default is the seed."""
    keys = ["b2b_hold_hours", "b2b_rehold_limit_per_day", "pre_departure_days"]
    async with as_booking_owner(admin):
        old = await admin.fetch(
            "SELECT key, value::text AS value FROM booking.settings WHERE key = ANY($1::text[])", keys
        )
        await admin.execute(UNDO_SEED)
        await admin.execute(
            "UPDATE booking.settings SET value = CASE key WHEN 'b2b_hold_hours' THEN '36'::jsonb"
            " WHEN 'b2b_rehold_limit_per_day' THEN '500'::jsonb ELSE '\"soon\"'::jsonb END"
            " WHERE key = ANY($1::text[])",
            keys,
        )
    try:
        await apply_migrations(admin, only=[56])
        seed = {k: v[0] for k, v in (await _seed(admin)).items()}
        assert seed["b2b_hold_hours"] == 36  # the row, inside 1-720
        assert seed["b2b_rehold_limit_per_day"] == 3  # 500 is outside 0-100: the code used its default
        assert seed["pre_departure_days"] == 14  # not a number: the code used its default
    finally:
        async with as_booking_owner(admin):
            await admin.execute(UNDO_SEED)
            for row in old:
                await admin.execute(
                    "UPDATE booking.settings SET value = $2::jsonb WHERE key = ANY($1::text[])",
                    [row["key"]],
                    row["value"],
                )
        await apply_migrations(admin, only=[56])
    assert {k: v[0] for k, v in (await _seed(admin)).items()} == SEED


async def test_the_post_check_refuses_a_key_someone_else_made_first(admin: asyncpg.Connection) -> None:
    async with as_booking_owner(admin) as owner:
        await admin.execute(UNDO_SEED)
        await admin.execute(
            "INSERT INTO booking.pricing_policy (param_key, version_no, value, effective_from, reason, created_by)"
            " VALUES ('b2b_hold_hours', 1, '12', DATE '2026-01-01', 'invented clash', 'it-056-maker')"
        )
    login = await admin.fetchval("SELECT session_user")
    try:
        if owner != login:
            await admin.execute(f'SET ROLE "{owner}"')
        with pytest.raises(asyncpg.RaiseError, match="bk-056: 18 of the 19"):
            await admin.execute(BK056.read_text(encoding="utf-8"))
        await admin.execute("ROLLBACK")  # the file's own BEGIN left an aborted transaction
        await admin.execute("RESET ROLE")
    finally:
        async with as_booking_owner(admin):
            await admin.execute("DELETE FROM booking.pricing_policy WHERE created_by IN ('it-056-maker')")
        await apply_migrations(admin, only=[56])
    assert {k: v[0] for k, v in (await _seed(admin)).items()} == SEED


async def _add_version(conn: asyncpg.Connection, version_no: int, value: int, *, approve: bool) -> str:
    """A hand-made pre_departure_days version in force from today (UTC), submitted and, when ``approve``, approved."""
    replaces = await conn.fetchval(
        "SELECT id::text FROM booking.pricing_policy WHERE param_key IN ('pre_departure_days') AND version_no = $1",
        version_no - 1,
    )
    policy_id = await conn.fetchval(
        "INSERT INTO booking.pricing_policy"
        " (param_key, version_no, value, unit, effective_from, reason, replaces_id, created_by)"
        " VALUES ('pre_departure_days', $1, to_jsonb($2::int), 'days', (now() AT TIME ZONE 'UTC')::date,"
        " 'invented change', $3::uuid, 'it-056-maker') RETURNING id::text",
        version_no,
        value,
        replaces,
    )
    await conn.execute(
        "INSERT INTO booking.pricing_policy_events (policy_id, maker, event, actor)"
        " VALUES ($1::uuid, 'it-056-maker', 'submitted', 'it-056-maker')",
        policy_id,
    )
    if approve:
        await conn.execute(
            "INSERT INTO booking.pricing_policy_events (policy_id, maker, event, actor)"
            " VALUES ($1::uuid, 'it-056-maker', 'approved', 'it-056-checker')",
            policy_id,
        )
    return policy_id


async def test_trip_checks_follow_the_approved_pre_departure_days(
    admin: asyncpg.Connection, migrated_booking_dsn: str
) -> None:
    """The seeding trigger reads the approved pre_departure_days in force (not booking.settings, which still says 14):
    a pending newer version does not count, and a new start date moves the open checks by the policy value."""
    mine = "SELECT id FROM booking.pricing_policy WHERE created_by IN ('it-056-maker')"
    await ops.purge_ops(admin)
    async with as_booking_owner(admin):
        await _add_version(admin, 2, 21, approve=True)
        await _add_version(admin, 3, 30, approve=False)  # pending: never in force
    app = await asyncpg.connect(migrated_booking_dsn)
    try:
        assert (
            await admin.fetchval("SELECT value::text FROM booking.settings WHERE key IN ('pre_departure_days')") == "14"
        )
        case = await ops.b2b_case(app, tag="bk056")
        due = {
            r["code"]: r["due_date"]
            for r in await app.fetch(
                "SELECT code, due_date FROM booking.trip_case_checks WHERE trip_case_id = $1::uuid", case["id"]
            )
        }
        assert due == {
            "pre_trip_recheck": ops.START - timedelta(days=21),
            "emergency_contact": None,  # B2B: not applicable
            "insurance": ops.START - timedelta(days=21),
        }
        new_start = ops.START - timedelta(days=2)  # the end date stays after it
        await app.execute("UPDATE booking.trip_cases SET start_date = $2 WHERE id = $1::uuid", case["id"], new_start)
        moved = await app.fetchval(
            "SELECT due_date FROM booking.trip_case_checks WHERE trip_case_id = $1::uuid AND code = 'insurance'",
            case["id"],
        )
        assert moved == new_start - timedelta(days=21)
    finally:
        await app.close()
        await ops.purge_ops(admin)
        async with as_booking_owner(admin):
            await admin.execute(f"DELETE FROM booking.pricing_policy_events WHERE policy_id IN ({mine})")  # noqa: S608
            await admin.execute("DELETE FROM booking.pricing_policy WHERE created_by IN ('it-056-maker')")
