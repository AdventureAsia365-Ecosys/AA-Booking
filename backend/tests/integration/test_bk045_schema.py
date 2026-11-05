"""AA-1030 (bk-045) on a real PostgreSQL: registration and re-run no-op, the seeded tiers and their approved discounts
(root ADR 0019 b15 starting values 0 / 3 / 6 %), the grants (append-only rate tables, a tier's code never renamed),
the maker-checker trigger of ``booking.partner_price_approvals`` and the new CHECKs / FKs.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[3] / "migrations" / "045_partner_price_tiers.sql"
TENANT = "aa103045-0000-4000-8000-000000000001"
MAKER = "aa1030-maker"
CHECKER = "aa1030-checker"


async def _purge(conn: asyncpg.Connection) -> None:
    await conn.execute("SET ROLE bk_owner")
    try:
        await conn.execute(
            "DELETE FROM booking.partner_price_approvals WHERE approved_by = ANY($1::text[])", [MAKER, CHECKER]
        )
        await conn.execute(
            "DELETE FROM booking.partner_rate_cards WHERE created_by = ANY($1::text[])", [MAKER, CHECKER]
        )
        await conn.execute(
            "DELETE FROM booking.partner_tier_discounts WHERE created_by = ANY($1::text[])", [MAKER, CHECKER]
        )
        await conn.execute("DELETE FROM booking.b2b_partners WHERE tenant_id = $1::uuid", TENANT)
    finally:
        await conn.execute("RESET ROLE")


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
async def app_conn(admin: asyncpg.Connection, migrated_booking_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_booking_dsn)
    try:
        yield conn
    finally:
        await conn.close()


async def _partner(conn: asyncpg.Connection) -> str:
    return await conn.fetchval(
        "INSERT INTO booking.b2b_partners (tenant_id, agency_name, status, billing_currency, m2m_client_id)"
        " VALUES ($1::uuid, 'Invented Lantern Travel', 'active', 'USD', 'clientbk045') RETURNING id::text",
        TENANT,
    )


async def test_registered_rerunnable_and_owned(admin: asyncpg.Connection) -> None:
    await admin.execute("SET ROLE bk_owner")
    try:
        await admin.execute(MIGRATION.read_text(encoding="utf-8"))  # a second run is a no-op
    finally:
        await admin.execute("RESET ROLE")
    assert await admin.fetchval("SELECT count(*) FROM shared.schema_versions WHERE version = 'bk-045'") == 1
    for rel in (
        "booking.partner_tiers",
        "booking.partner_tier_discounts",
        "booking.partner_rate_cards",
        "booking.partner_price_approvals",
    ):
        owner = await admin.fetchval("SELECT relowner::regrole::text FROM pg_class WHERE oid = $1::regclass", rel)
        assert owner == "bk_owner", rel
    names = [
        "b2b_partners_tier_fk",
        "b2b_quotes_tier_discount_chk",
        "b2b_quote_lines_discount_chk",
        "b2b_quote_lines_rate_card_fk",
    ]
    rows = await admin.fetch("SELECT conname FROM pg_constraint WHERE conname = ANY($1::text[])", names)
    assert len(rows) == len(names)  # the re-run did not add a second copy
    triggers = await admin.fetchval(
        "SELECT count(*) FROM pg_trigger WHERE tgname = 'partner_price_approvals_maker_checker'"
    )
    assert triggers == 1


async def test_seeded_tiers_are_approved(admin: asyncpg.Connection) -> None:
    rows = await admin.fetch(
        "SELECT t.code, d.discount_pct, a.approved_by FROM booking.partner_tiers t"
        " JOIN booking.partner_tier_discounts d ON d.tier_code = t.code AND d.created_by IS NULL"
        " JOIN booking.partner_price_approvals a ON a.partner_tier_discount_id = d.id ORDER BY t.sort_order"
    )
    assert [(r["code"], r["discount_pct"], r["approved_by"]) for r in rows] == [
        ("standard", Decimal("0.0000"), "seed:bk-045"),
        ("silver", Decimal("0.0300"), "seed:bk-045"),
        ("gold", Decimal("0.0600"), "seed:bk-045"),
    ]


async def test_grants(admin: asyncpg.Connection) -> None:
    for table in ("partner_tier_discounts", "partner_rate_cards", "partner_price_approvals"):
        rel = f"booking.{table}"
        for privilege in ("SELECT", "INSERT"):
            assert await admin.fetchval("SELECT has_table_privilege('booking', $1, $2)", rel, privilege), rel
        for privilege in ("UPDATE", "DELETE", "TRUNCATE"):
            assert not await admin.fetchval("SELECT has_table_privilege('booking', $1, $2)", rel, privilege), rel
    tiers = "booking.partner_tiers"
    assert await admin.fetchval("SELECT has_column_privilege('booking', $1, 'label', 'UPDATE')", tiers)
    assert not await admin.fetchval("SELECT has_column_privilege('booking', $1, 'code', 'UPDATE')", tiers)
    assert not await admin.fetchval("SELECT has_table_privilege('booking', $1, 'DELETE')", tiers)
    assert await admin.fetchval("SELECT has_column_privilege('booking', 'booking.b2b_partners', 'tier_code', 'UPDATE')")
    for consumer in ("cis_app", "tripplanner"):
        assert not await admin.fetchval(
            "SELECT has_table_privilege($1, 'booking.partner_rate_cards', 'SELECT')", consumer
        )


async def test_partner_defaults_to_standard_and_tier_must_exist(app_conn: asyncpg.Connection) -> None:
    partner = await _partner(app_conn)
    assert await app_conn.fetchval("SELECT tier_code FROM booking.b2b_partners WHERE id = $1::uuid", partner) == (
        "standard"
    )
    await app_conn.execute("UPDATE booking.b2b_partners SET tier_code = 'gold' WHERE id = $1::uuid", partner)
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await app_conn.execute("UPDATE booking.b2b_partners SET tier_code = 'platinum' WHERE id = $1::uuid", partner)


async def test_maker_checker_and_checks(app_conn: asyncpg.Connection) -> None:
    partner = await _partner(app_conn)
    card = await app_conn.fetchval(
        "INSERT INTO booking.partner_rate_cards (partner_id, discount_pct, valid_from, valid_to, contract_ref,"
        " created_by) VALUES ($1::uuid, 0.045, DATE '2026-11-01', DATE '2027-10-31', 'Invented contract 7', $2)"
        " RETURNING id::text",
        partner,
        MAKER,
    )
    with pytest.raises(asyncpg.CheckViolationError):  # the maker approves their own row
        await app_conn.execute(
            "INSERT INTO booking.partner_price_approvals (entity_type, partner_rate_card_id, approved_by)"
            " VALUES ('partner_rate_card', $1::uuid, $2)",
            card,
            MAKER,
        )
    await app_conn.execute(
        "INSERT INTO booking.partner_price_approvals (entity_type, partner_rate_card_id, approved_by)"
        " VALUES ('partner_rate_card', $1::uuid, $2)",
        card,
        CHECKER,
    )
    with pytest.raises(asyncpg.UniqueViolationError):  # one approval per row
        await app_conn.execute(
            "INSERT INTO booking.partner_price_approvals (entity_type, partner_rate_card_id, approved_by)"
            " VALUES ('partner_rate_card', $1::uuid, 'someone-else')",
            card,
        )
    with pytest.raises(asyncpg.CheckViolationError):  # a discount of half the price or more
        await app_conn.execute(
            "INSERT INTO booking.partner_tier_discounts (tier_code, discount_pct, valid_from, created_by)"
            " VALUES ('gold', 0.5, DATE '2027-01-01', $1)",
            MAKER,
        )
    with pytest.raises(asyncpg.CheckViolationError):  # valid_to before valid_from
        await app_conn.execute(
            "INSERT INTO booking.partner_rate_cards (partner_id, discount_pct, valid_from, valid_to, created_by)"
            " VALUES ($1::uuid, 0.01, DATE '2027-01-02', DATE '2027-01-01', $2)",
            partner,
            MAKER,
        )
    with pytest.raises(asyncpg.CheckViolationError):  # an approval naming the wrong entity
        await app_conn.execute(
            "INSERT INTO booking.partner_price_approvals (entity_type, partner_tier_discount_id, approved_by)"
            " VALUES ('partner_rate_card', $1::uuid, $2)",
            uuid.uuid4(),
            CHECKER,
        )
