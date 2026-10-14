"""bk-008 (AA-822) on a real PostgreSQL, as role ``booking``: supplier codes and checks, no deletes, the
contract maker-checker CHECK and the published-contract lock trigger.

Schema-level only (no API code): it ships with the migration-only PR so the DB rules are proven before apply.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date

import asyncpg
import pytest
import pytest_asyncio

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MAKER = "it-822-maker"
CHECKER = "it-822-checker"
EXPIRE = (
    "UPDATE booking.supplier_contracts SET status = 'expired', expired_by = $2, expired_at = now() WHERE id = $1::uuid"
)


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    await conn.execute(
        "INSERT INTO booking.staff_users (cis_user_id, role, active)"
        " VALUES ($1, 'contracting', true), ($2, 'admin', true) ON CONFLICT (cis_user_id) DO NOTHING",
        MAKER,
        CHECKER,
    )
    await conn.execute("DELETE FROM booking.supplier_contracts")
    await conn.execute("DELETE FROM booking.suppliers")
    try:
        yield conn
    finally:
        # The owner may delete; role booking may not (tested below).
        await conn.execute("DELETE FROM booking.supplier_contracts")
        await conn.execute("DELETE FROM booking.suppliers")
        await conn.close()


@pytest_asyncio.fixture
async def app_conn(migrated_booking_dsn: str, admin: asyncpg.Connection) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_booking_dsn)
    try:
        yield conn
    finally:
        await conn.close()


async def _supplier(conn: asyncpg.Connection, name: str = "Hanoi Emerald Hotel", **cols: object) -> str:
    values = {"company_name": name, "categories": ["hotel"], "country_code": "VN", "created_by": MAKER, **cols}
    names = ", ".join(values)
    marks = ", ".join(f"${i + 1}" for i in range(len(values)))
    return await conn.fetchval(
        f"INSERT INTO booking.suppliers ({names}) VALUES ({marks}) RETURNING id::text",  # noqa: S608 — test columns
        *values.values(),
    )


async def _contract(conn: asyncpg.Connection, supplier_id: str) -> str:
    return await conn.fetchval(
        "INSERT INTO booking.supplier_contracts (supplier_id, title, valid_from, valid_to, currency, created_by)"
        " VALUES ($1::uuid, '2027 rates', $2, $3, 'VND', $4) RETURNING id::text",
        supplier_id,
        date(2027, 1, 1),
        date(2027, 12, 31),
        MAKER,
    )


async def test_registered_and_mongolia_seeded(app_conn: asyncpg.Connection) -> None:
    assert await app_conn.fetchval("SELECT 1 FROM booking.v_schema_versions WHERE version = 'bk-008'") == 1
    assert await app_conn.fetchval("SELECT name FROM booking.countries WHERE iso2 = 'MN'") == "Mongolia"


async def test_supplier_code_and_defaults(app_conn: asyncpg.Connection) -> None:
    sid = await _supplier(app_conn)
    row = await app_conn.fetchrow("SELECT code, status, updated_at FROM booking.suppliers WHERE id = $1::uuid", sid)
    assert row["code"].startswith("SUP-") and len(row["code"]) == 10
    assert row["status"] == "active"


@pytest.mark.parametrize(
    ("cols", "constraint"),
    [
        ({"categories": []}, "suppliers_categories_chk"),
        ({"categories": ["hotel", "spa"]}, "suppliers_categories_chk"),
        ({"website": "ftp://x"}, "suppliers_website_chk"),
        ({"contact_email": "not-an-email"}, "suppliers_email_chk"),
        ({"status": "deleted"}, "suppliers_status_chk"),
    ],
)
async def test_supplier_checks(app_conn: asyncpg.Connection, cols: dict, constraint: str) -> None:
    with pytest.raises(asyncpg.CheckViolationError) as exc:
        await _supplier(app_conn, **cols)
    assert exc.value.constraint_name == constraint


async def test_supplier_name_unique_per_country(app_conn: asyncpg.Connection) -> None:
    await _supplier(app_conn, "Blue Lotus Travel")
    with pytest.raises(asyncpg.UniqueViolationError):
        await _supplier(app_conn, "  blue lotus travel ")
    await _supplier(app_conn, "Blue Lotus Travel", country_code="LA")  # same name, other country: fine


async def test_unknown_country_is_refused(app_conn: asyncpg.Connection) -> None:
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await _supplier(app_conn, country_code="ZZ")


async def test_app_cannot_delete(app_conn: asyncpg.Connection) -> None:
    sid = await _supplier(app_conn)
    await _contract(app_conn, sid)
    for table in ("supplier_contracts", "suppliers"):
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await app_conn.execute(f"DELETE FROM booking.{table}")  # noqa: S608 — fixed names


async def test_maker_cannot_publish(app_conn: asyncpg.Connection) -> None:
    cid = await _contract(app_conn, await _supplier(app_conn))
    publish = (
        "UPDATE booking.supplier_contracts SET status = 'published', published_by = $2, published_at = now()"
        " WHERE id = $1::uuid"
    )
    with pytest.raises(asyncpg.CheckViolationError) as exc:
        await app_conn.execute(publish, cid, MAKER)
    assert exc.value.constraint_name == "supplier_contracts_maker_checker_chk"
    # The last editor is the maker now: they cannot publish, the original creator (who checks the edit) can.
    await app_conn.execute(
        "UPDATE booking.supplier_contracts SET payment_terms = '30 days', updated_by = $2 WHERE id = $1::uuid",
        cid,
        CHECKER,
    )
    with pytest.raises(asyncpg.CheckViolationError):
        await app_conn.execute(publish, cid, CHECKER)
    await app_conn.execute(publish, cid, MAKER)
    assert (
        await app_conn.fetchval("SELECT status FROM booking.supplier_contracts WHERE id = $1::uuid", cid) == "published"
    )


async def test_published_contract_is_locked_except_expiry(app_conn: asyncpg.Connection) -> None:
    cid = await _contract(app_conn, await _supplier(app_conn))
    with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):  # a draft cannot jump to expired
        await app_conn.execute(
            EXPIRE,
            cid,
            CHECKER,
        )
    await app_conn.execute(
        "UPDATE booking.supplier_contracts SET status = 'published', published_by = $2, published_at = now()"
        " WHERE id = $1::uuid",
        cid,
        CHECKER,
    )
    with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
        await app_conn.execute("UPDATE booking.supplier_contracts SET currency = 'USD' WHERE id = $1::uuid", cid)
    with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):  # back to draft is not a thing
        await app_conn.execute(
            "UPDATE booking.supplier_contracts SET status = 'draft', published_by = NULL, published_at = NULL"
            " WHERE id = $1::uuid",
            cid,
        )
    await app_conn.execute(
        EXPIRE,
        cid,
        MAKER,
    )
    with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):  # expired is final
        await app_conn.execute("UPDATE booking.supplier_contracts SET notes = 'x' WHERE id = $1::uuid", cid)


async def test_contract_checks(app_conn: asyncpg.Connection) -> None:
    sid = await _supplier(app_conn)
    with pytest.raises(asyncpg.CheckViolationError) as exc:
        await app_conn.execute(
            "INSERT INTO booking.supplier_contracts (supplier_id, title, valid_from, valid_to, currency, created_by)"
            " VALUES ($1::uuid, 'x', '2027-02-01', '2027-01-01', 'USD', $2)",
            sid,
            MAKER,
        )
    assert exc.value.constraint_name == "supplier_contracts_validity_chk"
    with pytest.raises(asyncpg.CheckViolationError) as exc:
        await app_conn.execute(
            "INSERT INTO booking.supplier_contracts (supplier_id, title, valid_from, currency, created_by)"
            " VALUES ($1::uuid, 'x', '2027-01-01', 'usd', $2)",
            sid,
            MAKER,
        )
    assert exc.value.constraint_name == "supplier_contracts_currency_chk"
