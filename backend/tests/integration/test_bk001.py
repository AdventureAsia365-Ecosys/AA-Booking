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

# AA-1046: bk-045 dropped the bk-001 B2C tables (customers, customer_otps, customer_sessions, media_files).
BK001_TABLES = {"audit_logs", "inbox_events"}
BK001_DROPPED_BY_BK045 = {"customers", "customer_otps", "customer_sessions", "media_files"}
# Every bk-NNN in migrations/, in order (later migrations are applied too).
VERSIONS = [f"bk-{p.name[:3]}" for p in MIGRATIONS]
# Owner of every booking.* object after the replay: the RDS master until bk-014 is in migrations/, then bk_owner (X1).
OWNER_AFTER_REPLAY = OWNER_ROLE if max(int(p.name[:3]) for p in MIGRATIONS) >= OWNER_TRANSFER else MASTER_ROLE


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
        assert not BK001_DROPPED_BY_BK045 & {r["tablename"] for r in rows}
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
        entity = uuid.uuid4()
        await conn.execute(
            "INSERT INTO booking.audit_logs (actor_type, action, entity_type, entity_id)"
            " VALUES ('system', 'test', 'trip_case', $1)",
            entity,
        )
        await conn.execute(
            "INSERT INTO booking.inbox_events (source, contract, idempotency_key, payload)"
            " VALUES ('tripplanner', 'inquiry.submitted', $1, '{}'::jsonb)",
            uuid.uuid4().hex,
        )
        assert await conn.fetchval("SELECT count(*) FROM booking.audit_logs WHERE entity_id = $1", entity) == 1
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
        "TRUNCATE booking.trip_cases CASCADE",
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
