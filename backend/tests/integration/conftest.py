"""Real-PostgreSQL fixtures. Needs BOOKING_TEST_ADMIN_DSN; every test here is skipped without it.

The admin DSN must log in as ``aa_cis_admin`` (the role DB Migrate runs as on Dev, so default
privileges ``FOR ROLE aa_cis_admin`` behave as in production) and point at a throwaway database
whose name ends in ``_test`` or ``_ci``: the setup drops schema ``booking`` and role ``booking``.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest

ADMIN_DSN = os.environ.get("BOOKING_TEST_ADMIN_DSN", "")
MIGRATIONS = sorted((Path(__file__).resolve().parents[3] / "migrations").glob("*.sql"))
STUB_SHARED = Path(__file__).with_name("stub_shared.sql")
# CI-only value for the step Nghiep does out-of-band on Dev (ALTER ROLE booking WITH LOGIN PASSWORD).
BOOKING_TEST_PASSWORD = "ci-only-not-a-real-password"  # noqa: S105


def _require_dsn() -> str:
    if not ADMIN_DSN:
        pytest.skip("BOOKING_TEST_ADMIN_DSN not set")
    parts = urlsplit(ADMIN_DSN)
    database = parts.path.lstrip("/")
    if not (database.endswith("_test") or database.endswith("_ci")):
        pytest.fail(f"refusing to reset database {database!r}: name must end in _test or _ci")
    if parts.username != "aa_cis_admin":
        pytest.fail("BOOKING_TEST_ADMIN_DSN must log in as aa_cis_admin (the DB Migrate identity)")
    return ADMIN_DSN


def booking_dsn(admin_dsn: str) -> str:
    parts = urlsplit(admin_dsn)
    host = parts.hostname or "localhost"
    netloc = f"booking:{BOOKING_TEST_PASSWORD}@{host}" + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


async def apply_migrations(conn: asyncpg.Connection) -> None:
    """Same call as AA-CIS-Infra db/run_migration.py: the whole file, simple protocol."""
    for path in MIGRATIONS:
        await conn.execute(path.read_text(encoding="utf-8"))


async def _reset_and_migrate(admin_dsn: str) -> None:
    conn = await asyncpg.connect(admin_dsn)
    try:
        await conn.execute("DROP SCHEMA IF EXISTS booking CASCADE")
        if await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = 'booking'"):
            await conn.execute("DROP OWNED BY booking")
            await conn.execute("DROP ROLE booking")
        await conn.execute(STUB_SHARED.read_text(encoding="utf-8"))
        await conn.execute("DELETE FROM shared.schema_versions WHERE version LIKE 'bk-%'")
        await apply_migrations(conn)
        await conn.execute(f"ALTER ROLE booking WITH LOGIN PASSWORD '{BOOKING_TEST_PASSWORD}'")
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def migrated_admin_dsn() -> str:
    dsn = _require_dsn()
    asyncio.run(_reset_and_migrate(dsn))
    return dsn


@pytest.fixture(scope="session")
def migrated_booking_dsn(migrated_admin_dsn: str) -> str:
    return booking_dsn(migrated_admin_dsn)
