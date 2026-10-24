"""AA-908 (bk-024) schema on a real PostgreSQL: registration and re-run no-op, the opening guard, owner bk_owner, the
DM-6.1 tags, grants (role booking: SELECT + INSERT, UPDATE of customer_id only, never DELETE; cis_app: nothing), the
identity key and shape CHECK, ON DELETE CASCADE from the customer, and customers_source_chk with 'traveller_signin'.

The intake behaviour on top of it (inquiry.submitted v1.4) is in test_bk024_intake.py."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[3] / "migrations" / "024_customer_identities.sql"
ISS = "https://cognito-idp.us-west-1.amazonaws.com/us-west-1_TestPool1"


class _Rollback(Exception):
    """Raised at the end of a test transaction so its rows are rolled back."""


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    try:
        yield conn
    finally:
        await conn.close()


async def _customer(conn: asyncpg.Connection, email: str, *, verified: bool = False, source: str = "manual") -> str:
    return await conn.fetchval(
        "INSERT INTO booking.customers (name, email, verified, source) VALUES ('T', $1, $2, $3) RETURNING id::text",
        email,
        verified,
        source,
    )


async def test_registered_and_rerun_is_a_no_op(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 FROM booking.v_schema_versions WHERE version = 'bk-024'") == 1
    before = await admin.fetchval("SELECT count(*) FROM pg_catalog.pg_constraint")
    indexes = await admin.fetchval("SELECT count(*) FROM pg_catalog.pg_index")
    acl = await admin.fetchval("SELECT relacl::text FROM pg_class WHERE oid = 'booking.customer_identities'::regclass")
    await admin.execute("SET ROLE bk_owner")
    try:
        await admin.execute(MIGRATION.read_text(encoding="utf-8"))
    finally:
        await admin.execute("RESET ROLE")
    assert await admin.fetchval("SELECT count(*) FROM pg_catalog.pg_constraint") == before
    assert await admin.fetchval("SELECT count(*) FROM pg_catalog.pg_index") == indexes
    assert (
        await admin.fetchval("SELECT relacl::text FROM pg_class WHERE oid = 'booking.customer_identities'::regclass")
        == acl
    )


async def test_refuses_another_role(admin: asyncpg.Connection) -> None:
    with pytest.raises(asyncpg.RaiseError, match="expected to run as bk_owner"):
        await admin.execute(MIGRATION.read_text(encoding="utf-8"))


async def test_owner_columns_and_tags(admin: asyncpg.Connection) -> None:
    assert (
        await admin.fetchval(
            "SELECT relowner::regrole::text FROM pg_class WHERE oid = 'booking.customer_identities'::regclass"
        )
        == "bk_owner"
    )
    rows = await admin.fetch(
        "SELECT a.attname::text AS name, format_type(a.atttypid, a.atttypmod) AS type, a.attnotnull AS nn,"
        " col_description(a.attrelid, a.attnum) AS comment FROM pg_attribute a"
        " WHERE a.attrelid = 'booking.customer_identities'::regclass AND a.attnum > 0 AND NOT a.attisdropped"
        " ORDER BY a.attnum"
    )
    assert [(r["name"], r["type"], r["nn"]) for r in rows] == [
        ("iss", "text", True),
        ("sub", "text", True),
        ("customer_id", "uuid", True),
        ("created_at", "timestamp with time zone", True),
    ]
    tags = {r["name"]: r["comment"] for r in rows}
    assert tags["iss"].startswith("pii:online_id; ") and tags["sub"].startswith("pii:online_id; ")
    pkey = await admin.fetchval(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'customer_identities_pkey'"
    )
    assert pkey == "PRIMARY KEY (iss, sub)"
    fk = await admin.fetchval(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
        " WHERE conrelid = 'booking.customer_identities'::regclass AND contype = 'f'"
    )
    assert fk == "FOREIGN KEY (customer_id) REFERENCES booking.customers(id) ON DELETE CASCADE"


async def test_grants(admin: asyncpg.Connection) -> None:
    async def table(role: str, priv: str) -> bool:
        return await admin.fetchval("SELECT has_table_privilege($1, 'booking.customer_identities', $2)", role, priv)

    async def column(col: str) -> bool:
        return await admin.fetchval(
            "SELECT has_column_privilege('booking', 'booking.customer_identities', $1, 'UPDATE')", col
        )

    assert await table("booking", "SELECT") and await table("booking", "INSERT")
    for priv in ("UPDATE", "DELETE", "TRUNCATE"):
        assert not await table("booking", priv), priv
    assert await column("customer_id")
    for col in ("iss", "sub", "created_at"):
        assert not await column(col), col
    for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
        assert not await table("cis_app", priv), priv


async def test_source_check_accepts_traveller_signin_only_as_new_value(admin: asyncpg.Connection) -> None:
    definition = await admin.fetchval(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'customers_source_chk'"
    )
    for value in ("manual", "otp", "tripplanner_lead", "traveller_signin"):
        assert f"'{value}'" in definition, value
    with pytest.raises(_Rollback):
        async with admin.transaction():
            await _customer(admin, f"{uuid.uuid4().hex}@example.test", verified=True, source="traveller_signin")
            with pytest.raises(asyncpg.CheckViolationError):
                async with admin.transaction():
                    await _customer(admin, f"{uuid.uuid4().hex}@example.test", source="google")
            raise _Rollback


async def test_identity_key_shape_and_cascade(admin: asyncpg.Connection) -> None:
    sub = str(uuid.uuid4())
    with pytest.raises(_Rollback):
        async with admin.transaction():
            first = await _customer(admin, f"{uuid.uuid4().hex}@example.test", verified=True, source="traveller_signin")
            second = await _customer(admin, f"{uuid.uuid4().hex}@example.test", verified=True)
            ins = "INSERT INTO booking.customer_identities (iss, sub, customer_id) VALUES ($1, $2, $3::uuid)"
            await admin.execute(ins, ISS, sub, first)
            # A customer may hold several identities; one identity never belongs to two customers.
            await admin.execute(ins, ISS, str(uuid.uuid4()), first)
            with pytest.raises(asyncpg.UniqueViolationError):
                async with admin.transaction():
                    await admin.execute(ins, ISS, sub, second)
            bad = (("http://pool.example", "x"), (ISS, ""), (ISS, "x" * 129), ("https://" + "a" * 300, "x"))
            for iss, bad_sub in bad:
                with pytest.raises(asyncpg.CheckViolationError):
                    async with admin.transaction():
                        await admin.execute(ins, iss, bad_sub, first)
            await admin.execute("DELETE FROM booking.customers WHERE id = $1::uuid", first)
            left = await admin.fetchval(
                "SELECT count(*) FROM booking.customer_identities WHERE customer_id = $1::uuid", first
            )
            assert left == 0
            raise _Rollback


async def test_role_booking_inserts_and_repoints_but_never_deletes(migrated_booking_dsn: str) -> None:
    conn = await asyncpg.connect(migrated_booking_dsn)
    try:
        with pytest.raises(_Rollback):
            async with conn.transaction():
                old = await _customer(conn, f"{uuid.uuid4().hex}@example.test", verified=True)
                new = await _customer(conn, f"{uuid.uuid4().hex}@example.test", verified=True)
                sub = str(uuid.uuid4())
                await conn.execute(
                    "INSERT INTO booking.customer_identities (iss, sub, customer_id) VALUES ($1, $2, $3::uuid)",
                    ISS,
                    sub,
                    old,
                )
                await conn.execute(
                    "UPDATE booking.customer_identities SET customer_id = $3::uuid WHERE iss = $1 AND sub = $2",
                    ISS,
                    sub,
                    new,
                )
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    async with conn.transaction():
                        await conn.execute("UPDATE booking.customer_identities SET sub = 'other' WHERE sub = $1", sub)
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    async with conn.transaction():
                        await conn.execute("DELETE FROM booking.customer_identities WHERE sub = $1", sub)
                raise _Rollback
    finally:
        await conn.close()
