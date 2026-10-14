"""bk-007 (AA-810) on a real PostgreSQL: booking.policy_approvals, the maker-checker trigger, the seed back-fill
and the grants (append-only for role ``booking``; bk-004's policy tables stay append-only)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import date

import asyncpg
import pytest
import pytest_asyncio

from tests.integration.conftest import MIGRATIONS

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

SEED_DAY = date(2026, 10, 14)
BK004 = next(p for p in MIGRATIONS if p.name.startswith("004_"))
BK007 = next(p for p in MIGRATIONS if p.name.startswith("007_"))
MAKER, CHECKER = "it-maker-807", "it-checker-807"


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    # Other modules TRUNCATE booking.staff_users CASCADE, which empties pricing_rules and (by FK) its approvals.
    # Re-applying bk-004 restores the seed; re-applying bk-007 approves the seed rows again (seed:bk-004).
    await conn.execute(BK004.read_text(encoding="utf-8"))
    await conn.execute(BK007.read_text(encoding="utf-8"))
    await conn.execute("DELETE FROM booking.pricing_rules WHERE valid_from <> $1", SEED_DAY)
    await conn.execute("DELETE FROM booking.fx_rates")
    for staff_id in (MAKER, CHECKER):
        await conn.execute(
            "INSERT INTO booking.staff_users (cis_user_id, role, active) VALUES ($1, 'contracting', true)"
            " ON CONFLICT (cis_user_id) DO NOTHING",
            staff_id,
        )
    try:
        yield conn
    finally:
        await conn.execute("DELETE FROM booking.pricing_rules WHERE valid_from <> $1", SEED_DAY)
        await conn.execute("DELETE FROM booking.fx_rates")
        await conn.close()


@pytest_asyncio.fixture
async def app_conn(migrated_booking_dsn: str, admin: asyncpg.Connection) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_booking_dsn)
    try:
        yield conn
    finally:
        await conn.close()


async def _fx_row(conn: asyncpg.Connection, created_by: str | None, as_of: date = SEED_DAY) -> uuid.UUID:
    return await conn.fetchval(
        "INSERT INTO booking.fx_rates (currency, rate_per_usd, buffer_pct, as_of, source, created_by)"
        " VALUES ('EUR', 0.92, 0.02, $1, 'test', $2) RETURNING id",
        as_of,
        created_by,
    )


async def test_seed_rules_are_approved_as_seed(app_conn: asyncpg.Connection) -> None:
    rows = await app_conn.fetch(
        "SELECT r.channel, a.entity_type, a.approved_by FROM booking.pricing_rules r"
        " JOIN booking.policy_approvals a ON a.pricing_rule_id = r.id WHERE r.valid_from = $1 ORDER BY r.channel",
        SEED_DAY,
    )
    assert [(r["channel"], r["entity_type"], r["approved_by"]) for r in rows] == [
        ("b2b", "pricing_rule", "seed:bk-004"),
        ("b2c_tailor_made", "pricing_rule", "seed:bk-004"),
        ("fixed_departure", "pricing_rule", "seed:bk-004"),
    ]


async def test_backfill_is_bounded_by_the_first_apply(admin: asyncpg.Connection) -> None:
    """Rows created after bk-007 was first applied stay pending even when the file runs again."""
    fx_id = await _fx_row(admin, None)
    await admin.execute(BK007.read_text(encoding="utf-8"))
    assert await admin.fetchval("SELECT count(*) FROM booking.policy_approvals WHERE fx_rate_id = $1", fx_id) == 0


async def test_maker_cannot_approve_own_row(app_conn: asyncpg.Connection) -> None:
    fx_id = await _fx_row(app_conn, MAKER)
    with pytest.raises(asyncpg.CheckViolationError, match="maker-checker"):
        await app_conn.execute(
            "INSERT INTO booking.policy_approvals (entity_type, fx_rate_id, approved_by) VALUES ('fx_rate', $1, $2)",
            fx_id,
            MAKER,
        )
    await app_conn.execute(
        "INSERT INTO booking.policy_approvals (entity_type, fx_rate_id, approved_by) VALUES ('fx_rate', $1, $2)",
        fx_id,
        CHECKER,
    )
    # One approval per row.
    with pytest.raises(asyncpg.UniqueViolationError):
        await app_conn.execute(
            "INSERT INTO booking.policy_approvals (entity_type, fx_rate_id, approved_by) VALUES ('fx_rate', $1, 'x')",
            fx_id,
        )

    rule_id = await app_conn.fetchval(
        "INSERT INTO booking.pricing_rules (channel, target_margin, floor_margin, valid_from, created_by)"
        " VALUES ('b2b', 0.16, 0.10, DATE '2030-01-01', $1) RETURNING id",
        MAKER,
    )
    with pytest.raises(asyncpg.CheckViolationError):
        await app_conn.execute(
            "INSERT INTO booking.policy_approvals (entity_type, pricing_rule_id, approved_by)"
            " VALUES ('pricing_rule', $1, $2)",
            rule_id,
            MAKER,
        )


@pytest.mark.parametrize(
    "columns",
    [
        "('fx_rate', NULL, NULL, 'x')",  # no target
        "('pricing_rule', NULL, $1::uuid, 'x')",  # type does not match the FK set
        "('fx_rate', NULL, $1::uuid, ' ')",  # blank approver
        "('rule', NULL, $1::uuid, 'x')",  # unknown entity type
    ],
)
async def test_approval_shape_checks(app_conn: asyncpg.Connection, columns: str) -> None:
    fx_id = await _fx_row(app_conn, None)
    with pytest.raises(asyncpg.CheckViolationError):
        await app_conn.execute(
            "INSERT INTO booking.policy_approvals (entity_type, pricing_rule_id, fx_rate_id, approved_by)"
            f" VALUES {columns}",
            *([fx_id] if "$1" in columns else []),
        )


async def test_approvals_are_append_only(app_conn: asyncpg.Connection) -> None:
    fx_id = await _fx_row(app_conn, MAKER)
    await app_conn.execute(
        "INSERT INTO booking.policy_approvals (entity_type, fx_rate_id, approved_by) VALUES ('fx_rate', $1, $2)",
        fx_id,
        CHECKER,
    )
    for sql in (
        "UPDATE booking.policy_approvals SET approved_by = 'someone'",
        "DELETE FROM booking.policy_approvals",
        "TRUNCATE booking.policy_approvals",
        # bk-004's policy tables are unchanged by bk-007: still no UPDATE / DELETE for role booking.
        "UPDATE booking.fx_rates SET rate_per_usd = 1",
        "DELETE FROM booking.pricing_rules",
    ):
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await app_conn.execute(sql)


async def test_deleting_a_policy_row_as_owner_drops_its_approval(admin: asyncpg.Connection) -> None:
    fx_id = await _fx_row(admin, MAKER)
    await admin.execute(
        "INSERT INTO booking.policy_approvals (entity_type, fx_rate_id, approved_by) VALUES ('fx_rate', $1, $2)",
        fx_id,
        CHECKER,
    )
    await admin.execute("DELETE FROM booking.fx_rates WHERE id = $1", fx_id)
    assert await admin.fetchval("SELECT count(*) FROM booking.policy_approvals WHERE fx_rate_id = $1", fx_id) == 0
