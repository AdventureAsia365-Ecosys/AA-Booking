"""bk-004 (AA-810) on a real PostgreSQL, as role ``booking``: seed, append-only policy tables,
FX checks, the approval lock on version item costs, the repository SQL and the quote-preview path.

Needs bk-003 (staff_users, trip_case_versions, trip_case_version_itinerary) applied before bk-004.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import date, timedelta
from decimal import Decimal as D

import asyncpg
import httpx
import pytest
import pytest_asyncio

from app.auth.staff import Staff
from app.db import Database
from app.main import create_app
from app.pricing import repository
from app.pricing.engine import Channel
from app.settings import Settings
from app.trip_cases import versions
from app.trip_cases.versions import Item
from tests.integration.conftest import MIGRATIONS
from tests.staff_tokens import LocalCis

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

SEED_DAY = date(2026, 10, 14)
BK004 = next(p for p in MIGRATIONS if p.name.startswith("004_"))
STAFF_ID = "it-staff-810"


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    # Other modules TRUNCATE booking.staff_users CASCADE, which empties pricing_rules (FK created_by). bk-004
    # is a no-op when re-run except for its ON CONFLICT DO NOTHING seed, so re-applying it restores the seed.
    await conn.execute(BK004.read_text(encoding="utf-8"))
    # Policy tables are append-only for role booking; the migration role (owner) cleans test rows.
    await conn.execute("DELETE FROM booking.pricing_rules WHERE valid_from <> $1", SEED_DAY)
    await conn.execute("DELETE FROM booking.fx_rates")
    await conn.execute(
        "INSERT INTO booking.staff_users (cis_user_id, role, active) VALUES ($1, 'contracting', true)"
        " ON CONFLICT (cis_user_id) DO NOTHING",
        STAFF_ID,
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


async def test_seed_matches_adr_0005(app_conn: asyncpg.Connection) -> None:
    rows = await app_conn.fetch(
        "SELECT channel, target_margin, floor_margin, peak_uplift_pts, card_fee_pct, created_by"
        " FROM booking.pricing_rules WHERE valid_from = $1 ORDER BY channel",
        SEED_DAY,
    )
    assert [(r["channel"], r["target_margin"], r["floor_margin"]) for r in rows] == [
        ("b2b", D("0.1500"), D("0.1000")),
        ("b2c_tailor_made", D("0.3000"), D("0.2000")),
        ("fixed_departure", D("0.2500"), D("0.1800")),
    ]
    assert {(r["peak_uplift_pts"], r["card_fee_pct"], r["created_by"]) for r in rows} == {
        (D("0.0300"), D("0.0300"), None)
    }


async def test_policy_tables_are_append_only(app_conn: asyncpg.Connection) -> None:
    await app_conn.execute(
        "INSERT INTO booking.pricing_rules (channel, target_margin, floor_margin, valid_from, created_by)"
        " VALUES ('b2b', 0.16, 0.10, $1, $2)",
        SEED_DAY + timedelta(days=1),
        STAFF_ID,
    )
    await app_conn.execute(
        "INSERT INTO booking.fx_rates (currency, rate_per_usd, buffer_pct, as_of, source, created_by)"
        " VALUES ('EUR', 0.92, 0.02, $1, 'test', $2)",
        SEED_DAY,
        STAFF_ID,
    )
    for sql in (
        "UPDATE booking.pricing_rules SET target_margin = 0.5",
        "DELETE FROM booking.pricing_rules",
        "UPDATE booking.fx_rates SET rate_per_usd = 1",
        "DELETE FROM booking.fx_rates",
        "TRUNCATE booking.fx_rates",
    ):
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await app_conn.execute(sql)


@pytest.mark.parametrize(
    "values",
    [
        "('b2b', 0.10, 0.15, 0, DATE '2030-01-01')",  # floor above target
        "('b2b', 0.90, 0.10, 0.10, DATE '2030-01-02')",  # margin + card fee >= 100%
        "('b2b', 1.00, 0.10, 0, DATE '2030-01-03')",  # target margin 100%
        "('retail', 0.30, 0.20, 0, DATE '2030-01-04')",  # unknown channel
    ],
)
async def test_pricing_rule_checks(app_conn: asyncpg.Connection, values: str) -> None:
    with pytest.raises(asyncpg.CheckViolationError):
        await app_conn.execute(
            "INSERT INTO booking.pricing_rules (channel, target_margin, floor_margin, card_fee_pct, valid_from)"
            f" VALUES {values}"
        )


@pytest.mark.parametrize(
    "values",
    [
        "('USD', 1, 0, 'x')",  # USD is the base, never a row
        "('EUR', 0, 0, 'x')",  # rate must be positive
        "('EUR', 0.9, 1, 'x')",  # buffer below 100%
        "('EUR', 0.9, 0, ' ')",  # a source is required
        "('eur', 0.9, 0, 'x')",  # ISO code, upper case
    ],
)
async def test_fx_rate_checks(app_conn: asyncpg.Connection, values: str) -> None:
    with pytest.raises(asyncpg.CheckViolationError):
        await app_conn.execute(
            "INSERT INTO booking.fx_rates (currency, rate_per_usd, buffer_pct, source, as_of)"
            f" VALUES {values[:-1]}, DATE '2026-10-01')"
        )


async def test_repository_picks_row_in_force(app_conn: asyncpg.Connection) -> None:
    await app_conn.execute(
        "INSERT INTO booking.pricing_rules (channel, target_margin, floor_margin, valid_from)"
        " VALUES ('b2b', 0.17, 0.10, $1)",
        SEED_DAY + timedelta(days=30),
    )
    for as_of, rate in (
        (SEED_DAY - timedelta(days=2), "0.90"),
        (SEED_DAY, "0.92"),
        (SEED_DAY + timedelta(days=1), "5"),
    ):
        await app_conn.execute(
            "INSERT INTO booking.fx_rates (currency, rate_per_usd, buffer_pct, as_of, source)"
            " VALUES ('EUR', $1::numeric, 0.02, $2, 'test')",
            rate,
            as_of,
        )
    dated = await repository.rule_in_force(app_conn, Channel.B2B, SEED_DAY)
    assert dated is not None and dated.rule.target_margin == D("0.15") and dated.valid_from == SEED_DAY
    later = await repository.rule_in_force(app_conn, Channel.B2B, SEED_DAY + timedelta(days=31))
    assert later is not None and later.rule.target_margin == D("0.17")
    assert await repository.rule_in_force(app_conn, Channel.B2B, SEED_DAY - timedelta(days=1)) is None

    rates = await repository.fx_rates_in_force(app_conn, ["EUR", "GBP", "USD"], SEED_DAY)
    assert list(rates) == ["EUR"]
    assert rates["EUR"].rate.rate_per_usd == D("0.92") and rates["EUR"].as_of == SEED_DAY


async def _version_item(admin: asyncpg.Connection) -> tuple[uuid.UUID, uuid.UUID]:
    customer_id = await admin.fetchval(
        "INSERT INTO booking.customers (email) VALUES ($1) RETURNING id", f"{uuid.uuid4().hex}@example.test"
    )
    case_id = await admin.fetchval("INSERT INTO booking.trip_cases (customer_id) VALUES ($1) RETURNING id", customer_id)
    version_id = await admin.fetchval(
        "INSERT INTO booking.trip_case_versions (trip_case_id, version_number) VALUES ($1, 1) RETURNING id", case_id
    )
    item_id = await admin.fetchval(
        "INSERT INTO booking.trip_case_version_itinerary (trip_case_version_id, position, title, day_from, day_to)"
        " VALUES ($1, 1, 'Hanoi hotel', 1, 2) RETURNING id",
        version_id,
    )
    return version_id, item_id


async def test_version_item_costs_locked_after_approval(
    admin: asyncpg.Connection, app_conn: asyncpg.Connection
) -> None:
    version_id, item_id = await _version_item(admin)
    cost_id = await app_conn.fetchval(
        "INSERT INTO booking.version_item_costs (version_item_id, net_amount, net_currency, entered_by)"
        " VALUES ($1, 17780000, 'VND', $2) RETURNING id",
        item_id,
        STAFF_ID,
    )
    await app_conn.execute("UPDATE booking.version_item_costs SET supplier_note = 'net, 2 pax' WHERE id = $1", cost_id)
    with pytest.raises(asyncpg.CheckViolationError):
        await app_conn.execute(
            "INSERT INTO booking.version_item_costs (version_item_id, net_amount, net_currency) VALUES ($1, -1, 'USD')",
            item_id,
        )
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await app_conn.execute(
            "INSERT INTO booking.version_item_costs (version_item_id, net_amount, net_currency, entered_by)"
            " VALUES ($1, 1, 'USD', 'nobody')",
            item_id,
        )

    await admin.execute(
        "UPDATE booking.trip_case_versions SET is_approved = true, approved_at = now() WHERE id = $1", version_id
    )
    for sql, args in (
        ("INSERT INTO booking.version_item_costs (version_item_id, net_amount, net_currency) VALUES ($1, 1, 'USD')",
         (item_id,)),
        ("UPDATE booking.version_item_costs SET net_amount = 1 WHERE id = $1", (cost_id,)),
        ("DELETE FROM booking.version_item_costs WHERE id = $1", (cost_id,)),
    ):  # fmt: skip
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
            await app_conn.execute(sql, *args)


async def test_quote_preview_end_to_end(admin: asyncpg.Connection, migrated_booking_dsn: str) -> None:
    await admin.execute(
        "INSERT INTO booking.fx_rates (currency, rate_per_usd, buffer_pct, as_of, source)"
        " VALUES ('EUR', 0.92, 0.02, DATE '2026-10-01', 'test'), ('VND', 25400, 0, DATE '2026-10-01', 'test')"
    )
    settings = Settings(database_url=migrated_booking_dsn, db_ssl="disable", environment="test", git_sha="ci")
    db, cis = Database(settings), LocalCis()
    app = create_app(settings, db, cis.verifier())
    # Real staff auth (AA-681): a CIS-signed JWT for the active contracting row the fixture made, and one for
    # a content writer, provisioned on first use (inactive) and refused.
    contracting = {"Authorization": f"Bearer {cis.token(sub=STAFF_ID, cis_role='reviewer')}"}
    writer = {"Authorization": f"Bearer {cis.token(sub='it-writer-810', cis_role='content')}"}
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://booking") as client:
            body = {
                "channel": "b2c_tailor_made",
                "currency": "EUR",
                "items": [{"net_amount": "17780000", "net_currency": "VND"}],
            }
            anonymous = await client.post("/booking/pricing/quote-preview", json=body)
            refused = await client.post("/booking/pricing/quote-preview", json=body, headers=writer)
            r = await client.post("/booking/pricing/quote-preview", json=body, headers=contracting)
    finally:
        await db.close()
    assert anonymous.status_code == 401
    assert refused.status_code == 403
    assert r.status_code == 200, r.text
    body = r.json()
    assert D(body["net_total_usd"]) == D("700")
    assert D(body["total"]) == D("985")
    assert body["below_floor"] is False
    assert [fx["currency"] for fx in body["fx_rates"]] == ["EUR", "VND"]


async def test_item_costs_survive_an_item_save(admin: asyncpg.Connection, migrated_booking_dsn: str) -> None:
    """D2 (AA-681 W1) x bk-004: a PUT of the item list keeps item ids, so the cost lines on kept items survive;
    only an item left out of the list loses its costs (ON DELETE CASCADE)."""
    version_id, kept_id = await _version_item(admin)
    case_ref = await admin.fetchval(
        "SELECT c.code FROM booking.trip_cases c JOIN booking.trip_case_versions v ON v.trip_case_id = c.id"
        " WHERE v.id = $1",
        version_id,
    )
    dropped_id = await admin.fetchval(
        "INSERT INTO booking.trip_case_version_itinerary (trip_case_version_id, position, title, day_from, day_to)"
        " VALUES ($1, 2, 'Halong cruise', 3, 4) RETURNING id",
        version_id,
    )
    await admin.execute("UPDATE booking.staff_users SET role = 'sales' WHERE cis_user_id = $1", STAFF_ID)
    pool = await asyncpg.create_pool(migrated_booking_dsn, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            for item_id, amount in ((kept_id, 17780000), (kept_id, 2540000), (dropped_id, 300)):
                await conn.execute(
                    "INSERT INTO booking.version_item_costs (version_item_id, net_amount, net_currency, entered_by)"
                    " VALUES ($1, $2, 'VND', $3)",
                    item_id,
                    amount,
                    STAFF_ID,
                )
            staff = Staff(STAFF_ID, None, None, "sales", True, "reviewer")
            async with conn.transaction():
                saved = await versions.replace_items(
                    conn,
                    staff,
                    case_ref,
                    1,
                    [
                        Item("Day trip", 1, 1),
                        Item("Hanoi hotel, 2 nights", 1, 2, id=str(kept_id)),
                    ],
                )
            costs = await conn.fetch(
                "SELECT version_item_id, net_amount FROM booking.version_item_costs"
                " WHERE version_item_id = ANY($1::uuid[]) ORDER BY net_amount",
                [kept_id, dropped_id],
            )
    finally:
        await pool.close()
    assert [(i["position"], i["title"]) for i in saved["items"]] == [(1, "Day trip"), (2, "Hanoi hotel, 2 nights")]
    assert saved["items"][1]["id"] == str(kept_id)
    assert [(c["version_item_id"], c["net_amount"]) for c in costs] == [
        (kept_id, D("2540000")),
        (kept_id, D("17780000")),
    ]
