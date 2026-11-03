"""bk-036 (AA-1034 R0, root ADR 0019 d8): role booking can no longer INSERT into the B2C customer tables.

SELECT, UPDATE and DELETE stay (staff read the frozen rows; retention and erasure redact and delete them). The guard
refuses while INSERT is still granted on a table that got a row in the last hour; once revoked, a re-run is a no-op.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from tests.integration.conftest import B2C_TABLES

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[3] / "migrations" / "036_b2c_customer_insert_revoke.sql"


class _Rollback(Exception):
    """Raised at the end of a test transaction so its rows are rolled back."""


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    try:
        yield conn
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def app_conn(migrated_booking_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_booking_dsn)
    try:
        yield conn
    finally:
        await conn.close()


def _guard_block() -> str:
    """The file's DO block alone (its BEGIN / COMMIT would end a test transaction)."""
    m = re.search(r"^DO \$\$\n.*?^END \$\$;$", MIGRATION.read_text(encoding="utf-8"), re.M | re.S)
    assert m, "bk-036 has no DO block"
    return m.group(0)


async def _privileges(admin: asyncpg.Connection, table: str) -> dict[str, bool]:
    row = await admin.fetchrow(
        """
        SELECT has_table_privilege('booking', $1, 'INSERT') AS ins,
               has_table_privilege('booking', $1, 'SELECT') AS sel,
               has_table_privilege('booking', $1, 'UPDATE') AS upd,
               has_table_privilege('booking', $1, 'DELETE') AS del,
               has_any_column_privilege('booking', $1, 'UPDATE') AS col_upd
        """,
        f"booking.{table}",
    )
    return dict(row)


async def test_registered(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 AS ok FROM booking.v_schema_versions WHERE version = 'bk-036'") == 1


@pytest.mark.parametrize("table", B2C_TABLES)
async def test_insert_revoked_read_update_delete_kept(admin: asyncpg.Connection, table: str) -> None:
    got = await _privileges(admin, table)
    assert got["ins"] is False
    assert got["sel"] is True and got["col_upd"] is True
    if table == "customer_identities":
        # bk-024: append-only apart from re-pointing customer_id (column UPDATE), never a DELETE.
        assert got["upd"] is False and got["del"] is False
    else:
        assert got["upd"] is True and got["del"] is True


async def test_app_role_insert_is_refused_42501(app_conn: asyncpg.Connection) -> None:
    with pytest.raises(asyncpg.InsufficientPrivilegeError) as info:
        await app_conn.execute(
            "INSERT INTO booking.customers (name, email, source) VALUES ('Ira Example', $1, 'manual')",
            f"{uuid.uuid4().hex}@example.test",
        )
    assert info.value.sqlstate == "42501"


async def test_app_role_still_redacts_and_deletes(admin: asyncpg.Connection, app_conn: asyncpg.Connection) -> None:
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE bk_owner")
        customer = await admin.fetchval(
            "INSERT INTO booking.customers (name, email, source) VALUES ('Ira Example', $1, 'manual') RETURNING id",
            f"{uuid.uuid4().hex}@example.test",
        )
    try:
        assert await app_conn.execute("UPDATE booking.customers SET name = NULL WHERE id = $1", customer) == "UPDATE 1"
        assert await app_conn.execute("DELETE FROM booking.customers WHERE id = $1", customer) == "DELETE 1"
    finally:
        await admin.execute("SET ROLE bk_owner")
        await admin.execute("DELETE FROM booking.customers WHERE id = $1", customer)
        await admin.execute("RESET ROLE")


async def test_rerun_is_a_no_op_even_with_recent_rows(admin: asyncpg.Connection) -> None:
    acl = {
        t: await admin.fetchval("SELECT relacl::text AS acl FROM pg_class WHERE oid = $1::regclass", f"booking.{t}")
        for t in B2C_TABLES
    }
    with pytest.raises(_Rollback):
        async with admin.transaction():
            await admin.execute("SET LOCAL ROLE bk_owner")
            await admin.execute(
                "INSERT INTO booking.customers (name, email, source) VALUES ('Ira Example', $1, 'manual')",
                f"{uuid.uuid4().hex}@example.test",
            )
            # INSERT is already revoked, so the guard does not look at the fresh row.
            await admin.execute(_guard_block())
            raise _Rollback
    await admin.execute("SET ROLE bk_owner")
    try:
        await admin.execute(MIGRATION.read_text(encoding="utf-8"))
    finally:
        await admin.execute("RESET ROLE")
    for t in B2C_TABLES:
        assert (
            await admin.fetchval("SELECT relacl::text AS acl FROM pg_class WHERE oid = $1::regclass", f"booking.{t}")
            == acl[t]
        )


@pytest.mark.parametrize("table", B2C_TABLES)
async def test_guard_refuses_a_recent_row_while_insert_is_granted(admin: asyncpg.Connection, table: str) -> None:
    with pytest.raises(asyncpg.RaiseError, match=rf"bk-036: booking\.{table} got a row in the last hour"):
        async with admin.transaction():
            await admin.execute("SET LOCAL ROLE bk_owner")
            await admin.execute(f"GRANT INSERT ON booking.{table} TO booking")
            customer = await admin.fetchval(
                "INSERT INTO booking.customers (name, email, source, created_at)"
                " VALUES ('Ira Example', $1, 'manual', now() - interval '2 hours') RETURNING id",
                f"{uuid.uuid4().hex}@example.test",
            )
            if table == "customer_identities":
                await admin.execute(
                    "INSERT INTO booking.customer_identities (iss, sub, customer_id) VALUES ($1, $2, $3)",
                    "https://issuer.example.test",
                    uuid.uuid4().hex,
                    customer,
                )
            elif table == "customer_otps":
                await admin.execute(
                    "INSERT INTO booking.customer_otps (customer_id, otp_hash, channel, expires_at)"
                    " VALUES ($1, 'h', 'email', now() + interval '5 minutes')",
                    customer,
                )
            elif table == "customer_sessions":
                await admin.execute(
                    "INSERT INTO booking.customer_sessions (customer_id, refresh_token_hash, expires_at)"
                    " VALUES ($1, $2, now() + interval '1 day')",
                    customer,
                    uuid.uuid4().hex,
                )
            else:
                await admin.execute("UPDATE booking.customers SET created_at = now() WHERE id = $1", customer)
            await admin.execute(_guard_block())


async def test_refuses_another_role(admin: asyncpg.Connection) -> None:
    with pytest.raises(asyncpg.RaiseError, match="expected to run as bk_owner"):
        await admin.execute(_guard_block())
