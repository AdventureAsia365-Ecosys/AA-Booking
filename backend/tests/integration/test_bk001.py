"""bk-001 on a real PostgreSQL 15: registration, re-run, ownership, role privileges, constraints,
and the live /booking/health/ready contract as role ``booking``."""

from __future__ import annotations

import uuid

import asyncpg
import httpx
import pytest

from app.db import Database
from app.main import create_app
from app.settings import Settings
from tests.integration.conftest import MIGRATIONS, apply_migrations, as_booking_owner
from tests.migration_guard import MASTER_ROLE, OWNER_ROLE, OWNER_TRANSFER

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

BK001_TABLES = {"customers", "customer_otps", "customer_sessions", "media_files", "audit_logs", "inbox_events"}
# Every bk-NNN in migrations/, in order (later migrations are applied too).
VERSIONS = [f"bk-{p.name[:3]}" for p in MIGRATIONS]
# Owner of every booking.* object after the replay: the RDS master until bk-014 is in migrations/, then bk_owner (X1).
OWNER_AFTER_REPLAY = OWNER_ROLE if max(int(p.name[:3]) for p in MIGRATIONS) >= OWNER_TRANSFER else MASTER_ROLE


def _email() -> str:
    return f"{uuid.uuid4().hex}@example.test"


async def test_registered_once_and_rerun_is_a_noop(migrated_admin_dsn: str) -> None:
    conn = await asyncpg.connect(migrated_admin_dsn)
    try:
        await apply_migrations(conn)  # second full run must not fail
        rows = await conn.fetch("SELECT version FROM shared.schema_versions WHERE version LIKE 'bk-%' ORDER BY 1")
        assert [r["version"] for r in rows] == VERSIONS
    finally:
        await conn.close()


async def test_migrations_replay_as_a_non_superuser(migrated_admin_dsn: str) -> None:
    """X1 S5: the replay logs in like DB Migrate on RDS, without superuser (which skips every ownership check)."""
    conn = await asyncpg.connect(migrated_admin_dsn)
    try:
        row = await conn.fetchrow(
            "SELECT session_user AS login, rolsuper, pg_has_role(session_user, 'booking', 'MEMBER') AS app_member"
            " FROM pg_roles WHERE rolname = session_user"
        )
        assert (row["login"], row["rolsuper"], row["app_member"]) == (MASTER_ROLE, False, False)
    finally:
        await conn.close()


async def test_tables_exist_and_are_owned_by_the_migration_role(migrated_admin_dsn: str) -> None:
    conn = await asyncpg.connect(migrated_admin_dsn)
    try:
        rows = await conn.fetch("SELECT tablename, tableowner FROM pg_tables WHERE schemaname = 'booking'")
        assert BK001_TABLES <= {r["tablename"] for r in rows}
        assert {r["tableowner"] for r in rows} == {OWNER_AFTER_REPLAY}
        assert await conn.fetchval("SELECT rolcanlogin FROM pg_roles WHERE rolname = 'booking'") is True  # set by test
    finally:
        await conn.close()


async def test_default_privileges_cover_future_tables(migrated_admin_dsn: str, migrated_booking_dsn: str) -> None:
    admin = await asyncpg.connect(migrated_admin_dsn)
    try:
        async with as_booking_owner(admin):  # the role the next migration runs as
            await admin.execute("CREATE TABLE booking.zz_future_probe (id int)")
        app = await asyncpg.connect(migrated_booking_dsn)
        try:
            await app.execute("INSERT INTO booking.zz_future_probe VALUES (1)")
            assert await app.fetchval("SELECT count(*) FROM booking.zz_future_probe") == 1
        finally:
            await app.close()
    finally:
        await admin.execute("DROP TABLE booking.zz_future_probe")
        await admin.close()


async def test_booking_role_reads_and_writes_its_tables(migrated_booking_dsn: str) -> None:
    conn = await asyncpg.connect(migrated_booking_dsn)
    try:
        customer_id = await conn.fetchval("INSERT INTO booking.customers (email) VALUES ($1) RETURNING id", _email())
        await conn.execute(
            "INSERT INTO booking.customer_otps (customer_id, otp_hash, channel, expires_at)"
            " VALUES ($1, 'h', 'email', now() + interval '5 minutes')",
            customer_id,
        )
        await conn.execute(
            "INSERT INTO booking.audit_logs (actor_type, action, entity_type, entity_id)"
            " VALUES ('system', 'test', 'customer', $1)",
            customer_id,
        )
        await conn.execute(
            "INSERT INTO booking.inbox_events (source, contract, idempotency_key, payload)"
            " VALUES ('tripplanner', 'inquiry.submitted', $1, '{}'::jsonb)",
            uuid.uuid4().hex,
        )
        assert await conn.fetchval("SELECT count(*) FROM booking.customers WHERE id = $1", customer_id) == 1
    finally:
        await conn.close()


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE booking.audit_logs SET action = 'tampered'",
        "DELETE FROM booking.audit_logs",
        "INSERT INTO booking.v_schema_versions (version, applied_at) VALUES ('bk-999', now())",
        "DELETE FROM booking.v_schema_versions",
        "SELECT * FROM shared.schema_versions",
        "CREATE TABLE booking.app_created (id int)",
        "CREATE TABLE public.app_created (id int)",
        "TRUNCATE booking.customers CASCADE",
    ],
)
async def test_booking_role_is_least_privilege(migrated_booking_dsn: str, statement: str) -> None:
    conn = await asyncpg.connect(migrated_booking_dsn)
    try:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute(statement)
    finally:
        await conn.close()


async def test_view_shows_only_booking_versions(migrated_booking_dsn: str) -> None:
    conn = await asyncpg.connect(migrated_booking_dsn)
    try:
        versions = [r["version"] for r in await conn.fetch("SELECT version FROM booking.v_schema_versions ORDER BY 1")]
        assert versions == VERSIONS  # the stub CIS row '222' is not visible
    finally:
        await conn.close()


async def test_customer_constraints(migrated_booking_dsn: str) -> None:
    conn = await asyncpg.connect(migrated_booking_dsn)
    try:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute("INSERT INTO booking.customers (name) VALUES ('no contact')")

        # bk-002: email is unique among VERIFIED live customers (leads may repeat an email).
        email = _email()
        insert_verified = "INSERT INTO booking.customers (email, verified) VALUES ($1, true) RETURNING id"
        first = await conn.fetchval(insert_verified, email)
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(insert_verified, email.upper())

        before = await conn.fetchval("SELECT updated_at FROM booking.customers WHERE id = $1", first)
        await conn.execute("UPDATE booking.customers SET deleted_at = now() WHERE id = $1", first)
        after = await conn.fetchval("SELECT updated_at FROM booking.customers WHERE id = $1", first)
        assert after > before  # set_updated_at trigger

        # A soft-deleted customer does not block re-registration.
        await conn.fetchval(insert_verified, email)

        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO booking.customer_otps (customer_id, otp_hash, channel, expires_at)"
                " VALUES ($1, 'h', 'pigeon', now())",
                first,
            )
        key = uuid.uuid4().hex
        insert_event = (
            "INSERT INTO booking.inbox_events (source, contract, idempotency_key, payload)"
            " VALUES ($1, 'inquiry.submitted', $2, '{}'::jsonb)"
        )
        await conn.execute(insert_event, "tripplanner", key)
        await conn.execute(insert_event, "cis", key)  # unique per (source, contract, key), not globally
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(insert_event, "tripplanner", key)
    finally:
        await conn.close()


async def test_ready_endpoint_as_booking_role(migrated_booking_dsn: str) -> None:
    settings = Settings(database_url=migrated_booking_dsn, db_ssl="disable", environment="test", git_sha="ci")
    db = Database(settings)
    app = create_app(settings, db)
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://booking") as client:
            live = await client.get("/booking/health")
            ready = await client.get("/booking/health/ready")
    finally:
        await db.close()
    assert live.status_code == 200
    assert ready.status_code == 200
    body = ready.json()
    # AA-993 M4a: the body also lists every registered bk-NNN, sorted.
    assert {k: body[k] for k in ("status", "db", "schema_version")} == {
        "status": "ok",
        "db": "ok",
        "schema_version": VERSIONS[-1],
    }
    assert body["registered_keys"] == sorted(body["registered_keys"])
    assert set(VERSIONS) <= set(body["registered_keys"])
