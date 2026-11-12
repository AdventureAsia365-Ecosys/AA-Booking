"""bk-053 (AA-1091, root ADR 0005b d1 / d2) on a real PostgreSQL: the price list versions and their rows.

- The data step: a plan's closed price history sets become withdrawn past versions, the rows in force become its
  latest version, published, and the hand rows of the matrix point at it; the matrix itself (what the tariff, the RRP
  and the from price read) is unchanged, so every price read before the file answers the same after it.
- The guards: a version starts as a draft, its facts change only while a draft, the status moves only along d2, the
  approver and the rejecter are never the maker, rows change only while a draft, role booking deletes nothing.

Every test runs in one transaction and is rolled back. Names are invented. No import of app code (the migration PR
stands alone)."""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

BK053 = Path(__file__).resolve().parents[3] / "migrations" / "053_price_list_versions.sql"
MAKER = "it-bk053-maker"
CHECKER = "it-bk053-checker"
FROM = date(2031, 1, 1)


@contextlib.asynccontextmanager
async def refused(c: asyncpg.Connection, exc: type[Exception], match: str | None = None) -> AsyncIterator[None]:
    """The statement inside fails with ``exc`` in a savepoint, so the test's transaction goes on."""
    with pytest.raises(exc, match=match):
        async with c.transaction():
            yield


def _body(path: Path) -> str:
    """The file without its own BEGIN / COMMIT, so a test can run it inside a transaction it rolls back."""
    sql = path.read_text(encoding="utf-8")
    head, _, rest = sql.partition("\nBEGIN;\n")
    body, _, _ = rest.rpartition("\nCOMMIT;")
    assert body, "bk-053 is wrapped in BEGIN / COMMIT"
    return body


@pytest_asyncio.fixture
async def owner(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    """The admin login as bk_owner (the role the file runs as), inside a transaction rolled back at the end."""
    c = await asyncpg.connect(migrated_admin_dsn)
    tx = c.transaction()
    await tx.start()
    await c.execute("SET LOCAL ROLE bk_owner")
    try:
        yield c
    finally:
        await tx.rollback()
        await c.close()


@pytest_asyncio.fixture
async def app(migrated_booking_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    # No re-apply of bk-053 here: its data step would version every plan other tests left with prices.
    c = await asyncpg.connect(migrated_booking_dsn)
    tx = c.transaction()
    await tx.start()
    try:
        yield c
    finally:
        await tx.rollback()
        await c.close()


async def _plan(c: asyncpg.Connection) -> str:
    return str(
        await c.fetchval(
            "INSERT INTO booking.trip_plans (source_system, source_id, sellable) VALUES ('cis', $1, true) RETURNING id",
            f"it-bk053-{uuid.uuid4().hex[:8]}",
        )
    )


MATRIX_SQL = """
SELECT position, channel, season, peak, valid_from, valid_to, pax_min, pax_max, currency::text, amount, rrp_amount,
       net_amount, computed, overridden
FROM booking.trip_plan_prices WHERE trip_plan_id = $1::uuid ORDER BY position
"""


async def test_history_and_rows_in_force_become_versions(owner: asyncpg.Connection) -> None:
    plan = await _plan(owner)
    await owner.execute(
        "INSERT INTO booking.staff_users (cis_user_id, role, active, name) VALUES ($1, 'contracting', true, $1)"
        " ON CONFLICT (cis_user_id) DO NOTHING",
        MAKER,
    )
    # The matrix in force: a hand b2b row and a computed fixed_departure row.
    await owner.execute(
        "INSERT INTO booking.trip_plan_prices (trip_plan_id, position, channel, season, peak, valid_from, valid_to,"
        " pax_min, pax_max, currency, amount, rrp_amount, created_by) VALUES"
        " ($1::uuid, 1, 'b2b', 'Green season', true, $2, $3, 2, 2, 'USD', 900, 1125, $4)",
        plan,
        FROM,
        FROM + timedelta(days=89),
        MAKER,
    )
    await owner.execute(
        "INSERT INTO booking.trip_plan_prices (trip_plan_id, position, channel, valid_from, valid_to, pax_min,"
        " currency, amount, net_amount, net_currency, effective_margin, priced_on, computed) VALUES"
        " ($1::uuid, 2, 'fixed_departure', $2, $3, 1, 'USD', 500, 380, 'USD', 0.24, $2, true)",
        plan,
        FROM,
        FROM + timedelta(days=179),
    )
    # Its history: an older set replaced as a whole, then the open set = the rows in force.
    old_at, new_at = datetime(2030, 5, 1, 10, tzinfo=UTC), datetime(2030, 6, 1, 10, tzinfo=UTC)
    await owner.execute(
        "INSERT INTO booking.trip_plan_price_history (trip_plan_id, channel, season, peak, valid_from, valid_to,"
        " pax_min, pax_max, currency, amount, computed, overridden, change_source, changed_by, effective_from,"
        " effective_to) VALUES ($1::uuid, 'b2b', NULL, true, $2, $3, 2, 2, 'USD', 850, false, false, 'staff', $4,"
        " $5::timestamptz, $6::timestamptz)",
        plan,
        FROM,
        FROM + timedelta(days=364),
        MAKER,
        old_at,
        new_at,
    )
    await owner.execute(
        "INSERT INTO booking.trip_plan_price_history (trip_plan_id, channel, season, peak, valid_from, valid_to,"
        " pax_min, pax_max, currency, amount, computed, overridden, change_source, changed_by, effective_from)"
        " VALUES ($1::uuid, 'b2b', 'Green season', true, $2, $3, 2, 2, 'USD', 900, false, false, 'staff', $4,"
        " $5::timestamptz)",
        plan,
        FROM,
        FROM + timedelta(days=89),
        MAKER,
        new_at,
    )
    before = [tuple(r) for r in await owner.fetch(MATRIX_SQL, plan)]

    await owner.execute(_body(BK053))

    versions = await owner.fetch(
        "SELECT id, version_no, status, source, travel_from, travel_to, tariff_year, created_by, approved_by,"
        " withdrawn_by, reason FROM booking.price_list_versions WHERE trip_plan_id = $1::uuid ORDER BY version_no",
        plan,
    )
    assert [(v["version_no"], v["status"]) for v in versions] == [(1, "withdrawn"), (2, "published")]
    past, latest = versions
    assert (past["travel_from"], past["travel_to"]) == (FROM, FROM + timedelta(days=365))
    assert past["withdrawn_by"] == "migration:bk-053" and past["created_by"] == MAKER
    assert (latest["travel_from"], latest["travel_to"]) == (FROM, FROM + timedelta(days=180))
    assert latest["source"] == "staff" and latest["approved_by"] == "migration:bk-053" and latest["reason"]
    # 2031-01-01 is before the 11-01 tariff year start: tariff year 2031.
    assert latest["tariff_year"] == 2031
    past_rows = await owner.fetch(
        "SELECT amount, peak, travel_from, travel_to FROM booking.price_list_version_rows WHERE version_id = $1",
        past["id"],
    )
    assert [(r["amount"], r["peak"], r["travel_to"]) for r in past_rows] == [
        (Decimal("850.00"), True, FROM + timedelta(days=365))
    ]
    rows = await owner.fetch(
        "SELECT channel, computed, peak, travel_to FROM booking.price_list_version_rows WHERE version_id = $1"
        " ORDER BY position",
        latest["id"],
    )
    # The row's own peak flag is kept (read while the plan's calendar has no range, bk-054).
    assert [(r["channel"], r["computed"], r["peak"]) for r in rows] == [
        ("b2b", False, True),
        ("fixed_departure", True, False),
    ]
    # Hand rows point at the version; the cost sheet's computed row stays unversioned.
    pointers = await owner.fetch(
        "SELECT channel, version_id FROM booking.trip_plan_prices WHERE trip_plan_id = $1::uuid ORDER BY position",
        plan,
    )
    assert [(p["channel"], p["version_id"]) for p in pointers] == [("b2b", latest["id"]), ("fixed_departure", None)]
    # The matrix every price read uses is exactly as before.
    assert [tuple(r) for r in await owner.fetch(MATRIX_SQL, plan)] == before

    # Re-run: the plan already has versions, nothing more is written.
    await owner.execute(_body(BK053))
    assert (
        await owner.fetchval("SELECT count(*) FROM booking.price_list_versions WHERE trip_plan_id = $1::uuid", plan)
        == 2
    )


async def _draft(c: asyncpg.Connection, plan: str, no: int = 1) -> str:
    return str(
        await c.fetchval(
            "INSERT INTO booking.price_list_versions (trip_plan_id, version_no, label, tariff_year, travel_from,"
            " travel_to, created_by) VALUES ($1::uuid, $2, 'Test draft', 2031, $3, $4, $5) RETURNING id",
            plan,
            no,
            FROM,
            FROM + timedelta(days=365),
            MAKER,
        )
    )


async def _row(c: asyncpg.Connection, version: str, position: int = 1, computed: bool = False) -> None:
    await c.execute(
        "INSERT INTO booking.price_list_version_rows (version_id, position, channel, travel_from, travel_to, pax_min,"
        " currency, amount, net_amount, net_currency, priced_on, computed) VALUES ($1::uuid, $2, 'b2b', $3, $4, 1,"
        " 'USD', 700, $5, $6, $7, $8)",
        version,
        position,
        FROM,
        FROM + timedelta(days=30),
        Decimal("500") if computed else None,
        "USD" if computed else None,
        FROM if computed else None,
        computed,
    )


SUBMIT_SQL = """
UPDATE booking.price_list_versions SET status = 'pending', reason = 'New rates from the ground operator',
       submitted_at = now(), diff = '{"rows": []}', impact = '{}', diff_sha256 = repeat('a', 64)
WHERE id = $1::uuid
"""


async def test_life_of_a_version_and_its_guards(app: asyncpg.Connection) -> None:
    plan = await _plan(app)
    draft = await _draft(app, plan)
    await _row(app, draft)
    async with refused(app, asyncpg.CheckViolationError, match="no computed row"):
        await _row(app, draft, position=2, computed=True)
    # A draft is edited freely by its maker.
    await app.execute("UPDATE booking.price_list_versions SET label = 'Test draft 2' WHERE id = $1::uuid", draft)
    # Submit needs the reason and the frozen diff.
    async with refused(app, asyncpg.CheckViolationError):
        await app.execute("UPDATE booking.price_list_versions SET status = 'pending' WHERE id = $1::uuid", draft)
    await app.execute(SUBMIT_SQL, draft)
    async with refused(app, asyncpg.CheckViolationError, match="not a draft"):
        await app.execute("UPDATE booking.price_list_versions SET label = 'Later' WHERE id = $1::uuid", draft)
    async with refused(app, asyncpg.CheckViolationError, match="only while it is a draft"):
        await _row(app, draft, position=3)
    async with refused(app, asyncpg.CheckViolationError, match="only while it is a draft"):
        await app.execute("DELETE FROM booking.price_list_version_rows WHERE version_id = $1::uuid", draft)
    # The maker never approves their own version.
    async with refused(app, asyncpg.CheckViolationError):
        await app.execute(
            "UPDATE booking.price_list_versions SET status = 'published', approved_by = $2, approved_at = now()"
            " WHERE id = $1::uuid",
            draft,
            MAKER,
        )
    await app.execute(
        "UPDATE booking.price_list_versions SET status = 'published', approved_by = $2, approved_at = now()"
        " WHERE id = $1::uuid",
        draft,
        CHECKER,
    )
    # published <-> superseded is derived; never back to pending; approved once.
    await app.execute(
        "UPDATE booking.price_list_versions SET status = 'superseded', superseded_at = now() WHERE id = $1::uuid",
        draft,
    )
    await app.execute(
        "UPDATE booking.price_list_versions SET status = 'published', superseded_at = NULL WHERE id = $1::uuid",
        draft,
    )
    async with refused(app, asyncpg.CheckViolationError, match="not a move"):
        await app.execute("UPDATE booking.price_list_versions SET status = 'pending' WHERE id = $1::uuid", draft)
    async with refused(app, asyncpg.CheckViolationError, match="approved once"):
        await app.execute(
            "UPDATE booking.price_list_versions SET approved_by = 'someone else' WHERE id = $1::uuid", draft
        )
    # The checker withdraws a published version with a reason; then it is final.
    await app.execute(
        "UPDATE booking.price_list_versions SET status = 'withdrawn', withdrawn_by = $2, withdrawn_at = now(),"
        " withdraw_reason = 'Wrong currency' WHERE id = $1::uuid",
        draft,
        CHECKER,
    )
    async with refused(app, asyncpg.CheckViolationError, match="final"):
        await app.execute(
            "UPDATE booking.price_list_versions SET status = 'published', withdrawn_by = NULL, withdrawn_at = NULL,"
            " withdraw_reason = NULL WHERE id = $1::uuid",
            draft,
        )
    # Erasure: the label and a reason may become the marker, alone (also on a final version).
    await app.execute("UPDATE booking.price_list_versions SET reason = '[redacted]' WHERE id = $1::uuid", draft)
    await app.execute("UPDATE booking.price_list_versions SET label = '[redacted]' WHERE id = $1::uuid", draft)
    async with refused(app, asyncpg.CheckViolationError, match="final"):
        await app.execute("UPDATE booking.price_list_versions SET label = 'Renamed' WHERE id = $1::uuid", draft)
    async with refused(app, asyncpg.CheckViolationError, match="final"):
        await app.execute(
            "UPDATE booking.price_list_versions SET withdraw_reason = '[redacted]', tariff_year = 2032"
            " WHERE id = $1::uuid",
            draft,
        )
    assert (
        await app.fetchval(
            "SELECT label || '|' || reason || '|' || withdraw_reason FROM booking.price_list_versions"
            " WHERE id = $1::uuid",
            draft,
        )
        == "[redacted]|[redacted]|Wrong currency"
    )


async def test_reject_and_starting_state(app: asyncpg.Connection) -> None:
    plan = await _plan(app)
    async with refused(app, asyncpg.CheckViolationError, match="starts as a draft"):
        await app.execute(
            "INSERT INTO booking.price_list_versions (trip_plan_id, version_no, label, tariff_year, travel_from,"
            " travel_to, created_by, status, approved_by, approved_at, reason, submitted_at, diff, impact,"
            " diff_sha256) VALUES ($1::uuid, 1, 'x', 2031, $2, $3, $4, 'published', $5, now(), 'r', now(), '{}',"
            " '{}', repeat('a', 64))",
            plan,
            FROM,
            FROM + timedelta(days=1),
            MAKER,
            CHECKER,
        )
    draft = await _draft(app, plan)
    await app.execute(SUBMIT_SQL, draft)
    async with refused(app, asyncpg.CheckViolationError):
        await app.execute(
            "UPDATE booking.price_list_versions SET status = 'rejected', rejected_by = $2, rejected_at = now(),"
            " reject_reason = 'Too high' WHERE id = $1::uuid",
            draft,
            MAKER,
        )
    await app.execute(
        "UPDATE booking.price_list_versions SET status = 'rejected', rejected_by = $2, rejected_at = now(),"
        " reject_reason = 'Too high' WHERE id = $1::uuid",
        draft,
        CHECKER,
    )
    # A version of another plan cannot be replaced.
    other = await _plan(app)
    async with refused(app, asyncpg.CheckViolationError, match="same trip plan"):
        await app.execute(
            "INSERT INTO booking.price_list_versions (trip_plan_id, version_no, label, tariff_year, travel_from,"
            " travel_to, created_by, replaces_id) VALUES ($1::uuid, 1, 'x', 2031, $2, $3, $4, $5::uuid)",
            other,
            FROM,
            FROM + timedelta(days=1),
            MAKER,
            draft,
        )


async def test_role_booking_deletes_no_version(app: asyncpg.Connection) -> None:
    plan = await _plan(app)
    draft = await _draft(app, plan)
    async with refused(app, asyncpg.InsufficientPrivilegeError):
        await app.execute("DELETE FROM booking.price_list_versions WHERE id = $1::uuid", draft)
    async with refused(app, asyncpg.InsufficientPrivilegeError):
        await app.execute("UPDATE booking.price_list_versions SET created_by = 'x' WHERE id = $1::uuid", draft)
    async with refused(app, asyncpg.InsufficientPrivilegeError):
        await app.execute("UPDATE booking.price_list_version_rows SET amount = 1 WHERE version_id = $1::uuid", draft)
    # The matrix row may carry a version and a season.
    await app.execute(
        "INSERT INTO booking.trip_plan_prices (trip_plan_id, position, channel, valid_from, valid_to, pax_min,"
        " currency, amount, version_id, season_code) VALUES ($1::uuid, 900, 'b2b', $2, $2, 1, 'USD', 1, $3::uuid,"
        " 'peak')",
        plan,
        FROM,
        draft,
    )
    async with refused(app, asyncpg.CheckViolationError):
        await app.execute(
            "INSERT INTO booking.trip_plan_prices (trip_plan_id, position, channel, valid_from, valid_to, pax_min,"
            " currency, amount, season_code) VALUES ($1::uuid, 901, 'b2b', $2, $2, 1, 'USD', 1, 'shoulder')",
            plan,
            FROM,
        )
