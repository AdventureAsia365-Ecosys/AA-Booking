"""bk-051 (AA-1091, S278) on a real PostgreSQL: the approved margins and the individual cancellation scale.

The session replay undoes bk-051 right after it runs (tests/integration/conftest.py ``S278_SEED_UNDO``) so the other
pricing tests keep their pre-S278 numbers; this module applies it again with ``keep_s278_seed=True``, checks what it
wrote, re-runs it (a no-op) and undoes it again."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal as D

import asyncpg
import pytest
import pytest_asyncio

from app.b2b import cancellation
from app.pricing import engine, repository
from tests.integration.conftest import (
    MIGRATIONS,
    S278_SEED_CANCELLATION,
    S278_SEED_RULES,
    S278_SEED_UNDO,
    apply_migrations,
    as_booking_owner,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    try:
        await apply_migrations(conn, only=[51], keep_s278_seed=True)
        yield conn
    finally:
        async with as_booking_owner(conn):
            await conn.execute(S278_SEED_UNDO)
        await conn.close()


async def test_margins_and_scale_are_approved_rows(admin: asyncpg.Connection) -> None:
    rules = {
        r["channel"]: r
        for r in await admin.fetch(
            "SELECT r.channel, r.target_margin, r.floor_margin, r.valid_from, r.created_by, a.approved_by"
            " FROM booking.pricing_rules r JOIN booking.policy_approvals a ON a.pricing_rule_id = r.id"
            " WHERE r.id = ANY($1::uuid[])",
            list(S278_SEED_RULES),
        )
    }
    assert set(rules) == {"b2b", "fixed_departure"}
    assert (rules["b2b"]["target_margin"], rules["b2b"]["floor_margin"]) == (D("0.2000"), D("0.1200"))
    fixed = rules["fixed_departure"]
    assert (fixed["target_margin"], fixed["floor_margin"]) == (D("0.2500"), D("0.1500"))
    assert all(r["approved_by"] == "tech lead S278" and r["created_by"] is None for r in rules.values())

    today = datetime.now(UTC).date()
    in_force = await repository.rule_in_force(admin, engine.Channel.B2B, today)
    assert in_force is not None and in_force.rule.target_margin == D("0.2000")

    row = await admin.fetchrow(
        "SELECT p.tiers::text AS tiers, a.approved_by FROM booking.cancellation_policies p"
        " JOIN booking.policy_approvals a ON a.cancellation_policy_id = p.id WHERE p.id = $1::uuid",
        S278_SEED_CANCELLATION,
    )
    assert row["approved_by"] == "tech lead S278"
    tiers = cancellation.parse_tiers(row["tiers"])
    fees = {days: cancellation.fee_pct_for(tiers, days) for days in (40, 31, 30, 22, 21, 15, 14, 8, 7, 2, 1, 0)}
    assert fees == {
        40: D("0"), 31: D("0"), 30: D("10"), 22: D("10"), 21: D("25"), 15: D("25"),
        14: D("50"), 8: D("50"), 7: D("75"), 2: D("75"), 1: D("100"), 0: D("100"),
    }  # fmt: skip

    setting = "SELECT description FROM booking.settings WHERE key IN ('b2b_quote_valid_hours')"
    retired = await admin.fetchval(setting)
    assert retired.startswith("Retired (bk-051)")

    # Re-run: no second row, no second approval.
    await apply_migrations(admin, only=[51], keep_s278_seed=True)
    count = await admin.fetchval(
        "SELECT count(*) FROM booking.policy_approvals WHERE pricing_rule_id = ANY($1::uuid[])", list(S278_SEED_RULES)
    )
    assert count == 2


# Invented values no seed or other test uses, so a copied draft would show.
DRAFT_RULE = "00000000-0000-4000-8000-0000000b51d1"
FUTURE_RULE = "00000000-0000-4000-8000-0000000b51d2"
CLASH_RULE = "00000000-0000-4000-8000-0000000b51d3"
CLASH_POLICY = "00000000-0000-4000-8000-0000000b51d4"
HAND_ROWS = (DRAFT_RULE, FUTURE_RULE, CLASH_RULE)
BK051 = next(p for p in MIGRATIONS if p.name.startswith("051_"))

IN_FORCE_SQL = """
SELECT r.peak_uplift_pts, r.card_fee_pct, r.rrp_markup_pct, r.child_pct, r.infant_flat_usd, r.early_days
FROM booking.pricing_rules r JOIN booking.policy_approvals a ON a.pricing_rule_id = r.id
WHERE r.channel = $1 AND r.valid_from <= DATE '2026-11-12'
ORDER BY r.valid_from DESC, r.created_at DESC LIMIT 1"""


@pytest_asyncio.fixture
async def bare(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    """A connection on the migrated database without the S278 rows; removes the rows a test adds by hand."""
    conn = await asyncpg.connect(migrated_admin_dsn)
    try:
        async with as_booking_owner(conn):
            await conn.execute(S278_SEED_UNDO)
        yield conn
    finally:
        async with as_booking_owner(conn):
            await conn.execute(
                "DELETE FROM booking.policy_approvals WHERE pricing_rule_id = ANY($1::uuid[])"
                " OR cancellation_policy_id = $2::uuid",
                list(HAND_ROWS),
                CLASH_POLICY,
            )
            await conn.execute("DELETE FROM booking.pricing_rules WHERE id = ANY($1::uuid[])", list(HAND_ROWS))
            await conn.execute("DELETE FROM booking.cancellation_policies WHERE id = $1::uuid", CLASH_POLICY)
            await conn.execute(S278_SEED_UNDO)
        await conn.close()


async def _add_rule(conn: asyncpg.Connection, rule_id: str, valid_from: str, *, approved: bool) -> None:
    async with as_booking_owner(conn):
        await conn.execute(
            "INSERT INTO booking.pricing_rules (id, channel, target_margin, floor_margin, peak_uplift_pts,"
            " card_fee_pct, valid_from, rrp_markup_pct, child_pct, infant_flat_usd, early_days, early_discount_pts)"
            " VALUES ($1::uuid, 'b2b', 0.31, 0.11, 0.0917, 0.0413, $2::date, 0.37, 0.41, 13, 43, 0.07)",
            rule_id,
            datetime.strptime(valid_from, "%Y-%m-%d").date(),
        )
        if approved:
            await conn.execute(
                "INSERT INTO booking.policy_approvals (entity_type, pricing_rule_id, approved_by)"
                " VALUES ('pricing_rule', $1::uuid, 'Second Checker')",
                rule_id,
            )


async def _run_bk051(conn: asyncpg.Connection) -> None:
    """The whole file as its migration role (bk_owner), simple protocol; a failure rolls the file back."""
    async with as_booking_owner(conn):
        try:
            await conn.execute(BK051.read_text(encoding="utf-8"))
        except asyncpg.PostgresError:
            await conn.execute("ROLLBACK")
            raise


async def test_copies_only_the_approved_rule_in_force(bare: asyncpg.Connection) -> None:
    expected = dict(await bare.fetchrow(IN_FORCE_SQL, "b2b"))
    # A newer staff draft no second person approved, and an approved rule scheduled after 2026-11-12.
    await _add_rule(bare, DRAFT_RULE, "2026-11-11", approved=False)
    await _add_rule(bare, FUTURE_RULE, "2027-03-01", approved=True)

    await _run_bk051(bare)

    copied = await bare.fetchrow(
        "SELECT peak_uplift_pts, card_fee_pct, rrp_markup_pct, child_pct, infant_flat_usd, early_days"
        " FROM booking.pricing_rules WHERE id = $1::uuid",
        S278_SEED_RULES[0],
    )
    assert dict(copied) == expected
    assert copied["peak_uplift_pts"] != D("0.0917") and copied["early_days"] != 43


async def test_a_staff_rule_on_the_same_day_fails_the_file(bare: asyncpg.Connection) -> None:
    await _add_rule(bare, CLASH_RULE, "2026-11-12", approved=False)

    with pytest.raises(asyncpg.UniqueViolationError):
        await _run_bk051(bare)

    written = await bare.fetchval(
        "SELECT count(*) FROM booking.pricing_rules WHERE id = ANY($1::uuid[])", list(S278_SEED_RULES)
    )
    assert written == 0  # rolled back: no S278 rule for the other channel either


async def test_a_staff_cancellation_schedule_on_the_same_day_fails_the_file(bare: asyncpg.Connection) -> None:
    async with as_booking_owner(bare):
        await bare.execute(
            "INSERT INTO booking.cancellation_policies (id, channel, valid_from, title, tiers)"
            " VALUES ($1::uuid, 'b2b', DATE '2026-11-12', 'Hand entered', '[{\"min_days\": 0, \"fee_pct\": 40}]')",
            CLASH_POLICY,
        )

    with pytest.raises(asyncpg.UniqueViolationError):
        await _run_bk051(bare)

    assert await bare.fetchval(
        "SELECT count(*) FROM booking.pricing_rules WHERE id = ANY($1::uuid[])", list(S278_SEED_RULES)
    ) == 0  # fmt: skip


async def test_a_channel_without_an_approved_rule_fails_the_post_check(bare: asyncpg.Connection) -> None:
    # Hide every approved fixed_departure rule in force on 2026-11-12 inside a transaction that is rolled back.
    async with as_booking_owner(bare):
        tx = bare.transaction()
        await tx.start()
        try:
            await bare.execute(
                "DELETE FROM booking.policy_approvals a USING booking.pricing_rules r"
                " WHERE a.pricing_rule_id = r.id AND r.channel = 'fixed_departure'"
            )
            body = BK051.read_text(encoding="utf-8").replace("BEGIN;", "", 1).replace("COMMIT;", "", 1)
            with pytest.raises(asyncpg.RaiseError, match="bk-051: 2 of the 3"):
                await bare.execute(body)
        finally:
            await tx.rollback()
