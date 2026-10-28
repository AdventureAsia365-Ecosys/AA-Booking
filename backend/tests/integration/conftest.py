"""Real-PostgreSQL fixtures. Needs BOOKING_TEST_ADMIN_DSN; every test here is skipped without it.

The admin DSN must log in as ``aa_cis_admin`` (the role DB Migrate logs in as on Dev) and point at a
throwaway database whose name ends in ``_test`` or ``_ci``: the setup drops schemas ``booking``,
``tripplanner`` and ``catalog`` there. ``aa_cis_admin`` must have the RDS shape, NOT superuser
(X1 plan S5): run ``python -m tests.integration.bootstrap_roles <superuser DSN>`` once per database
first; it also creates ``bk_owner`` and the login role ``booking``.

Every migration file runs as its migration role (``tests.migration_guard.migration_role``), the way
AA-CIS-Infra ``db/run_migration.py`` picks it (X1 XD8): the live owner of schema ``booking``, so the
login role up to bk-014 and ``SET ROLE bk_owner`` after it.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator, Iterable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest

from app.db import Database, QueryLogger
from app.settings import Settings
from tests.catalog_perf import QueryRecorder, Statement, statement_findings
from tests.migration_guard import MASTER_ROLE, OWNER_ROLE, OWNER_SCHEMA, migration_role

ADMIN_DSN = os.environ.get("BOOKING_TEST_ADMIN_DSN", "")
MIGRATIONS = sorted((Path(__file__).resolve().parents[3] / "migrations").glob("*.sql"))
STUB_SHARED = Path(__file__).with_name("stub_shared.sql")
STUB_TRIPPLANNER = Path(__file__).with_name("stub_tripplanner.sql")
STUB_CATALOG = Path(__file__).with_name("stub_catalog.sql")
# CI-only value for the step Nghiep does out-of-band on Dev (the login password of role booking). Set by
# tests/integration/bootstrap_roles.py.
BOOKING_TEST_PASSWORD = "ci-only-not-a-real-password"  # noqa: S105


def _require_dsn() -> str:
    if not ADMIN_DSN:
        pytest.skip("BOOKING_TEST_ADMIN_DSN not set")
    parts = urlsplit(ADMIN_DSN)
    database = parts.path.lstrip("/")
    if not (database.endswith("_test") or database.endswith("_ci")):
        pytest.fail(f"refusing to reset database {database!r}: name must end in _test or _ci")
    if parts.username != MASTER_ROLE:
        pytest.fail(f"BOOKING_TEST_ADMIN_DSN must log in as {MASTER_ROLE} (the DB Migrate identity)")
    return ADMIN_DSN


ROLE_SHAPE_SQL = """
SELECT r.rolsuper, r.rolcreaterole, r.rolbypassrls,
       pg_has_role(r.rolname, $1, 'MEMBER') AS owner_member,
       EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'booking' AND rolcanlogin) AS app_login,
       EXISTS (SELECT 1 FROM pg_auth_members m JOIN pg_roles g ON g.oid = m.roleid
                WHERE g.rolname = 'cis_app' AND m.member = r.oid) AS cis_reader_member
  FROM pg_roles r WHERE r.rolname = current_user"""


async def _check_role_shape(conn: asyncpg.Connection) -> None:
    """The RDS shape: the master is no superuser and reaches bk_owner by membership (X1 S3/S5)."""
    shape = await conn.fetchrow(ROLE_SHAPE_SQL, OWNER_ROLE)
    expected = {
        "rolsuper": False,
        "rolcreaterole": True,
        "rolbypassrls": False,
        "owner_member": True,
        "app_login": True,
        "cis_reader_member": True,  # bk-017 grants its contract views to cis_app (AA-858)
    }
    if dict(shape) != expected:
        pytest.fail(
            f"{MASTER_ROLE} does not have the RDS shape ({dict(shape)}, expected {expected}): run "
            "`python -m tests.integration.bootstrap_roles <superuser DSN>` on this database first"
        )


async def live_owner(conn: asyncpg.Connection) -> str | None:
    """Owner of schema booking, the role DB Migrate runs the next booking/ file as (None before bk-001)."""
    return await conn.fetchval("SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = $1", OWNER_SCHEMA)


@contextlib.asynccontextmanager
async def as_booking_owner(conn: asyncpg.Connection) -> AsyncIterator[str]:
    """Run as the live owner of schema booking (SET ROLE when the login role is not it), like DB Migrate."""
    login = await conn.fetchval("SELECT session_user")
    owner = await live_owner(conn) or login
    if owner != login:
        await conn.execute(f'SET ROLE "{owner}"')
    try:
        yield owner
    finally:
        if owner != login:
            await conn.execute("RESET ROLE")


STRAY_SQL = """
SELECT c.relname AS name FROM pg_class c
 WHERE c.relnamespace = $1::regnamespace AND c.relowner = (SELECT oid FROM pg_roles WHERE rolname = session_user)
UNION ALL
SELECT p.proname FROM pg_proc p
 WHERE p.pronamespace = $1::regnamespace AND p.proowner = (SELECT oid FROM pg_roles WHERE rolname = session_user)
UNION ALL
SELECT t.typname FROM pg_type t
 WHERE t.typnamespace = $1::regnamespace AND t.typelem = 0 AND t.typrelid = 0
   AND t.typowner = (SELECT oid FROM pg_roles WHERE rolname = session_user)"""


def booking_dsn(admin_dsn: str) -> str:
    parts = urlsplit(admin_dsn)
    host = parts.hostname or "localhost"
    netloc = f"booking:{BOOKING_TEST_PASSWORD}@{host}" + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


async def apply_migrations(
    conn: asyncpg.Connection, *, first_pass: bool = False, only: Iterable[int] | None = None
) -> None:
    """Same call as AA-CIS-Infra db/run_migration.py: the whole file, simple protocol, as its migration role.

    Each file runs as ``migration_role(bk-NNN)``: the login role ``aa_cis_admin`` up to bk-014, ``SET ROLE
    bk_owner`` from bk-015 on. On the first pass that must be the role DB Migrate picks (the live owner of schema
    booking when the file starts, XD8): a mismatch fails here, before the Dev apply. A re-run (the idempotency
    check) uses the same per-file role; DB Migrate itself never re-runs a registered file. After a switched file
    the login role may own nothing in schema booking (the runner's ownership post-check). ``only`` limits the run to
    those bk- numbers, in file order (e.g. ``range(1, 18)`` = the state before bk-018).
    """
    login = await conn.fetchval("SELECT session_user")
    wanted = None if only is None else set(only)
    for path in MIGRATIONS:
        if wanted is not None and int(path.name[:3]) not in wanted:
            continue
        role = migration_role(int(path.name[:3]))
        if first_pass:
            runner_role = await live_owner(conn) or login
            assert runner_role == role, f"{path.name}: DB Migrate would run it as {runner_role}, the guard says {role}"
        if role != login:
            await conn.execute(f'SET ROLE "{role}"')
        try:
            await conn.execute(path.read_text(encoding="utf-8"))
        finally:
            if role != login:
                await conn.execute("RESET ROLE")
        if role != login:
            stray = [r["name"] for r in await conn.fetch(STRAY_SQL, OWNER_SCHEMA)]
            assert not stray, f"{path.name} ran as {role} but left objects owned by {login}: {stray}"


async def _reset_and_migrate(admin_dsn: str) -> None:
    conn = await asyncpg.connect(admin_dsn)
    try:
        await _check_role_shape(conn)
        # Role booking stays (bootstrap_roles.py, as on Dev): its grants go with the dropped objects.
        await conn.execute("DROP SCHEMA IF EXISTS booking CASCADE")
        await conn.execute("DROP SCHEMA IF EXISTS tripplanner CASCADE")
        await conn.execute("DROP SCHEMA IF EXISTS catalog CASCADE")
        await conn.execute(STUB_SHARED.read_text(encoding="utf-8"))
        await conn.execute("DELETE FROM shared.schema_versions WHERE version LIKE 'bk-%'")
        await apply_migrations(conn, first_pass=True)
        await conn.execute(STUB_TRIPPLANNER.read_text(encoding="utf-8"))
        await conn.execute(STUB_CATALOG.read_text(encoding="utf-8"))
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


# ── AA-996 perf guard: every catalog read of every route a test exercises ─────
# Every Database a test builds records its statements; at teardown each statement that reads a parse-backed catalog
# view (v_tour_day, v_tour_place, _tour_day_text) is EXPLAINed once per session against the stub, whose views have the
# live plan shape (stub_catalog.sql). A scan of the itinerary rows without a tour_id condition parses every tour's
# itinerary on Dev (#130: ~1.5 s a read, 503 on the list): the test that ran it fails at teardown.
_explained: set[str] = set()


async def _plan_problems(statements: list[Statement]) -> list[str]:
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        return await statement_findings(conn, statements)
    finally:
        await conn.close()


@pytest.fixture(autouse=True)
def catalog_read_guard(monkeypatch: pytest.MonkeyPatch) -> Iterator[QueryRecorder]:
    recorder = QueryRecorder()
    build = Database.__init__

    def recording_init(self: Database, settings: Settings, query_loggers: Sequence[QueryLogger] = ()) -> None:
        build(self, settings, query_loggers=(*query_loggers, recorder))

    monkeypatch.setattr(Database, "__init__", recording_init)
    yield recorder
    new = {s.flat: s for s in recorder.parse_backed() if s.flat not in _explained}
    if not new or not ADMIN_DSN:
        return
    _explained.update(new)
    # A fresh event loop in its own thread: the test's loop is closed or owned by pytest-asyncio.
    with ThreadPoolExecutor(max_workers=1) as pool:
        problems = pool.submit(asyncio.run, _plan_problems(list(new.values()))).result()
    if problems:
        pytest.fail(
            "catalog read parses every itinerary (AA-996 perf guard; key parse-backed views by tour_id):\n"
            + "\n".join(problems)
        )
