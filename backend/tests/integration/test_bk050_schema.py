"""bk-050 (AA-1091, root ADR 0005b d15) on a real PostgreSQL: the pricing policy tables and their seed.

Role booking may read and add versions and steps, never change or delete one; the table refuses a self-approval and
a reject without a reason; each step happens once and a version has at most one final step. The seed is version 1 of
every parameter, approved by the tech lead (S278). Every test runs in one transaction as role booking and is rolled
back. Names are invented."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import asyncpg
import pytest
import pytest_asyncio

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

SEED = {
    "tariff_year_start": "11-01",
    "tariff_next_publish_by": "08-31",
    "quote_validity_days": 14,
    "quote_validity_festive_days": 3,
    "surcharge_causes": ["fx", "tax"],
    "surcharge_fx_threshold_pct": "5",
    "surcharge_notice_days": 20,
    "surcharge_free_cancel_pct": "8",
    "recompute_auto_publish_pct": "3",
    "diff_highlight_pct": "10",
    "provisional_uplift_pct": "5",
    "provisional_cis_ref_quotable": False,
    "group_on_request_min_pax": 9,
    "foc_one_per_pax": 15,
    "fit_full_payment_days": 30,
    "group_deposit_pct": "25",
    "group_deposit_days": 56,
    "festive_non_refundable_days": 30,
    "gala_dinner_days": ["12-24", "12-31"],
    "agency_currency_by_country": {"*": "USD", "VN": "VND"},
}


@pytest_asyncio.fixture
async def conn(migrated_booking_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    c = await asyncpg.connect(migrated_booking_dsn)
    tx = c.transaction()
    await tx.start()
    try:
        yield c
    finally:
        await tx.rollback()
        await c.close()


async def _version(c: asyncpg.Connection, maker: str = "it-050-maker") -> str:
    return await c.fetchval(
        "INSERT INTO booking.pricing_policy (param_key, version_no, value, effective_from, reason, replaces_id,"
        " created_by) SELECT 'foc_one_per_pax', 99, '20', current_date, 'Larger groups', id, $1"
        " FROM booking.pricing_policy WHERE param_key = 'foc_one_per_pax' AND version_no = 1 RETURNING id::text",
        maker,
    )


async def _step(c: asyncpg.Connection, vid: str, event: str, actor: str, reason: str | None = None) -> None:
    await c.execute(
        "INSERT INTO booking.pricing_policy_events (policy_id, maker, event, actor, reason)"
        " VALUES ($1::uuid, 'it-050-maker', $2, $3, $4)",
        vid,
        event,
        actor,
        reason,
    )


async def test_seed_is_version_one_of_every_parameter_approved_by_the_tech_lead(conn: asyncpg.Connection) -> None:
    rows = await conn.fetch(
        "SELECT p.param_key, p.value::text AS value, p.version_no, p.created_by, a.actor"
        " FROM booking.pricing_policy p JOIN booking.pricing_policy_events a ON a.policy_id = p.id"
        " AND a.event = 'approved' WHERE p.created_by = 'seed:bk-050'"
    )
    assert {r["param_key"]: json.loads(r["value"]) for r in rows} == SEED
    assert {(r["version_no"], r["actor"]) for r in rows} == {(1, "tech lead S278")}


async def test_versions_are_append_only(conn: asyncpg.Connection) -> None:
    for sql in (
        "UPDATE booking.pricing_policy SET value = '30' WHERE param_key = 'quote_validity_days'",
        "DELETE FROM booking.pricing_policy_events",
        "DELETE FROM booking.pricing_policy WHERE created_by <> 'seed:bk-050'",
    ):
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with conn.transaction():  # a savepoint keeps the outer transaction usable
                await conn.execute(sql)


async def test_maker_checker_and_one_final_step(conn: asyncpg.Connection) -> None:
    vid = await _version(conn)
    for event, actor, reason in (
        ("approved", "it-050-maker", None),  # self-approval
        ("rejected", "it-050-checker", None),  # a reject needs a reason
        ("submitted", "it-050-checker", None),  # only the maker submits
        ("withdrawn", "it-050-checker", None),  # only the maker withdraws
    ):
        with pytest.raises(asyncpg.CheckViolationError):
            async with conn.transaction():
                await _step(conn, vid, event, actor, reason)
    # The maker copied on the step must be the version's maker (composite FK).
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO booking.pricing_policy_events (policy_id, maker, event, actor)"
                " VALUES ($1::uuid, 'it-050-other', 'approved', 'it-050-maker')",
                vid,
            )
    await _step(conn, vid, "submitted", "it-050-maker")
    with pytest.raises(asyncpg.UniqueViolationError):
        async with conn.transaction():
            await _step(conn, vid, "submitted", "it-050-maker")
    await _step(conn, vid, "approved", "it-050-checker")
    with pytest.raises(asyncpg.UniqueViolationError):
        async with conn.transaction():
            await _step(conn, vid, "rejected", "it-050-checker", "Too late")


async def test_version_chain_and_reason_rules(conn: asyncpg.Connection) -> None:
    for sql in (
        # a version after the first replaces one
        "INSERT INTO booking.pricing_policy (param_key, version_no, value, effective_from, reason, created_by)"
        " VALUES ('foc_one_per_pax', 98, '20', current_date, 'x', 'it-050-maker')",
        # a reason is required
        "INSERT INTO booking.pricing_policy (param_key, version_no, value, effective_from, reason, created_by)"
        " VALUES ('new_param', 1, '20', current_date, '  ', 'it-050-maker')",
        # a key is snake case
        "INSERT INTO booking.pricing_policy (param_key, version_no, value, effective_from, reason, created_by)"
        " VALUES ('Bad Key', 1, '20', current_date, 'x', 'it-050-maker')",
    ):
        with pytest.raises(asyncpg.CheckViolationError):
            async with conn.transaction():
                await conn.execute(sql)
    with pytest.raises(asyncpg.UniqueViolationError):
        async with conn.transaction():
            await conn.execute(  # a second version 1 of a key
                "INSERT INTO booking.pricing_policy (param_key, version_no, value, effective_from, reason, created_by)"
                " VALUES ('foc_one_per_pax', 1, '20', current_date, 'x', 'it-050-maker')"
            )
