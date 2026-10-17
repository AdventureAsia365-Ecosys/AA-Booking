"""bk-014 (AA-852, X1 plan S6) on a real PostgreSQL: every booking.* object owned by the NOLOGIN role bk_owner,
default privileges FOR ROLE bk_owner only, role booking's privileges unchanged, and the next file (bk-015+), which
DB Migrate runs as bk_owner, works: it can register itself and role booking gets DML on what it creates.

Logged in as the non-superuser aa_cis_admin (member of bk_owner, tests/integration/bootstrap_roles.py), as on RDS.
The session DB has every migration applied (conftest, test_bk001's re-run)."""

from __future__ import annotations

import re

import asyncpg
import pytest

from tests.integration.conftest import MIGRATIONS, as_booking_owner
from tests.migration_guard import MASTER_ROLE, OWNER_ROLE

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

BK014_SQL = next(p for p in MIGRATIONS if p.name.startswith("014_")).read_text(encoding="utf-8")

# bk-014's own object list, parsed from its ALTER ... OWNER TO bk_owner lines: what the tests below check, so a later
# migration that adds objects (bk-015+, created by bk_owner itself) does not change them.
BK014_OBJECTS: dict[str, list[str]] = {
    kind: re.findall(rf"^ALTER {kind} (booking\.\S+) OWNER TO bk_owner;$", BK014_SQL, re.M)
    for kind in ("TABLE", "VIEW", "SEQUENCE", "FUNCTION")
}

LISTED_OWNER_SQL = {
    "TABLE": "SELECT relowner::regrole::text FROM pg_class WHERE oid = $1::regclass AND relkind = 'r'",
    "VIEW": "SELECT relowner::regrole::text FROM pg_class WHERE oid = $1::regclass AND relkind = 'v'",
    "SEQUENCE": "SELECT relowner::regrole::text FROM pg_class WHERE oid = $1::regclass AND relkind = 'S'",
    "FUNCTION": "SELECT proowner::regrole::text FROM pg_proc WHERE oid = $1::regprocedure",
}

OWNED_SQL = """
SELECT 'schema' AS kind, nspname AS name, nspowner::regrole::text AS owner FROM pg_namespace WHERE nspname = 'booking'
UNION ALL
SELECT 'rel:' || relkind::text, relname, relowner::regrole::text FROM pg_class
 WHERE relnamespace = 'booking'::regnamespace
UNION ALL
SELECT 'function', oid::regprocedure::text, proowner::regrole::text FROM pg_proc
 WHERE pronamespace = 'booking'::regnamespace
UNION ALL
SELECT 'type', typname, typowner::regrole::text FROM pg_type WHERE typnamespace = 'booking'::regnamespace"""

OWNER_OF = "SELECT relowner::regrole::text FROM pg_class WHERE oid = $1::regclass"


async def _admin(dsn: str) -> asyncpg.Connection:
    return await asyncpg.connect(dsn)


async def test_bk014_lists_the_objects_its_header_names() -> None:
    """32 tables, 2 views, 5 standalone sequences, 12 functions: a property of bk-014 itself, not of the replay."""
    sizes = {kind: len(names) for kind, names in BK014_OBJECTS.items()}
    assert sizes == {"TABLE": 32, "VIEW": 2, "SEQUENCE": 5, "FUNCTION": 12}
    assert all(len(set(names)) == len(names) for names in BK014_OBJECTS.values())


@pytest.mark.parametrize(
    ("kind", "name"),
    [(kind, name) for kind, names in BK014_OBJECTS.items() for name in names],
    ids=lambda v: v,
)
async def test_every_object_bk014_lists_is_owned_by_bk_owner(migrated_admin_dsn: str, kind: str, name: str) -> None:
    conn = await _admin(migrated_admin_dsn)
    try:
        assert await conn.fetchval(LISTED_OWNER_SQL[kind], name) == OWNER_ROLE
    finally:
        await conn.close()


async def test_nothing_in_booking_is_owned_by_anyone_but_bk_owner(migrated_admin_dsn: str) -> None:
    """Stays true as files are added: bk-015+ run as bk_owner (XD8), so their objects are bk_owner's too."""
    conn = await _admin(migrated_admin_dsn)
    try:
        rows = await conn.fetch(OWNED_SQL)
        assert {(r["kind"], r["name"]) for r in rows if r["owner"] != OWNER_ROLE} == set()
        # The identity sequence follows its table (not in bk-014's list): trip_case_status_history.seq.
        identity = await conn.fetchval("SELECT pg_get_serial_sequence('booking.trip_case_status_history', 'seq')")
        assert await conn.fetchval(OWNER_OF, identity) == OWNER_ROLE
        assert await conn.fetchval("SELECT rolcanlogin FROM pg_roles WHERE rolname = $1", OWNER_ROLE) is False
    finally:
        await conn.close()


async def test_default_privileges_are_bk001_rows_for_bk_owner(migrated_admin_dsn: str) -> None:
    conn = await _admin(migrated_admin_dsn)
    try:
        rows = await conn.fetch(
            "SELECT d.defaclrole::regrole::text AS role, d.defaclobjtype::text AS kind,"
            " a.grantee::regrole::text AS grantee, a.privilege_type AS priv"
            " FROM pg_default_acl d, aclexplode(d.defaclacl) a"
            " WHERE d.defaclnamespace = 'booking'::regnamespace"
        )
        assert {(r["role"], r["kind"], r["grantee"], r["priv"]) for r in rows} == {
            (OWNER_ROLE, "r", "booking", "SELECT"),
            (OWNER_ROLE, "r", "booking", "INSERT"),
            (OWNER_ROLE, "r", "booking", "UPDATE"),
            (OWNER_ROLE, "r", "booking", "DELETE"),
            (OWNER_ROLE, "S", "booking", "USAGE"),
            (OWNER_ROLE, "S", "booking", "SELECT"),
            (OWNER_ROLE, "f", "booking", "EXECUTE"),
        }
    finally:
        await conn.close()


@pytest.mark.parametrize(
    ("obj", "privilege", "expected"),
    [
        ("booking.customers", "SELECT", True),
        ("booking.customers", "DELETE", True),
        ("booking.customers", "TRUNCATE", False),
        ("booking.audit_logs", "INSERT", True),
        ("booking.audit_logs", "UPDATE", False),  # append-only (bk-001)
        ("booking.pricing_rules", "UPDATE", False),  # append-only (bk-004)
        ("booking.v_schema_versions", "SELECT", True),
        ("booking.v_schema_versions", "INSERT", False),  # read-only view
        ("booking.trip_case_status_transitions", "INSERT", False),
        ("booking.hotels", "DELETE", False),  # never deleted (bk-010)
    ],
)
async def test_role_booking_keeps_its_privileges(
    migrated_admin_dsn: str, obj: str, privilege: str, expected: bool
) -> None:
    conn = await _admin(migrated_admin_dsn)
    try:
        assert await conn.fetchval("SELECT has_table_privilege('booking', $1, $2)", obj, privilege) is expected
        assert await conn.fetchval("SELECT has_sequence_privilege('booking', 'booking.customer_code_seq', 'USAGE')")
        assert await conn.fetchval("SELECT has_function_privilege('booking', 'booking.set_updated_at()', 'EXECUTE')")
        assert await conn.fetchval("SELECT has_schema_privilege('booking', 'booking', 'CREATE')") is False
    finally:
        await conn.close()


async def test_next_file_runs_as_bk_owner_and_role_booking_can_use_its_objects(
    migrated_admin_dsn: str, migrated_booking_dsn: str
) -> None:
    """What DB Migrate does with bk-015+ (XD8): SET ROLE bk_owner, create, register (CIS 243 grants)."""
    admin = await _admin(migrated_admin_dsn)
    try:
        async with as_booking_owner(admin) as role:
            assert role == OWNER_ROLE
            await admin.execute("CREATE TABLE booking.zz_bk015_probe (id int)")
            await admin.execute(
                "INSERT INTO shared.schema_versions (version, applied_at, description)"
                " VALUES ('bk-990', now(), 'bk-014 test probe') ON CONFLICT (version) DO NOTHING"
            )
        assert await admin.fetchval(OWNER_OF, "booking.zz_bk015_probe") == OWNER_ROLE
        app = await asyncpg.connect(migrated_booking_dsn)
        try:
            await app.execute("INSERT INTO booking.zz_bk015_probe VALUES (1)")
            assert await app.fetchval("SELECT count(*) FROM booking.zz_bk015_probe") == 1
        finally:
            await app.close()
    finally:
        await admin.execute("DROP TABLE IF EXISTS booking.zz_bk015_probe")
        await admin.execute("DELETE FROM shared.schema_versions WHERE version = 'bk-990'")
        await admin.close()


async def test_master_created_objects_are_no_longer_covered(migrated_admin_dsn: str) -> None:
    """Why the runner must switch: an object the master creates after bk-014 gets no grant for role booking."""
    admin = await _admin(migrated_admin_dsn)
    try:
        await admin.execute("CREATE TABLE booking.zz_master_probe (id int)")
        assert (
            await admin.fetchval("SELECT has_table_privilege('booking', 'booking.zz_master_probe', 'SELECT')") is False
        )
        assert await admin.fetchval(OWNER_OF, "booking.zz_master_probe") == MASTER_ROLE
    finally:
        await admin.execute("DROP TABLE IF EXISTS booking.zz_master_probe")
        await admin.close()


async def test_guard_refuses_an_object_the_list_misses(migrated_admin_dsn: str) -> None:
    """A booking.* object that appeared after the list was written makes bk-014 raise (regenerate the list)."""
    admin = await _admin(migrated_admin_dsn)
    try:
        await admin.execute("CREATE TABLE booking.zz_unlisted (id int)")  # owned by the master, not in bk-014
        with pytest.raises(asyncpg.RaiseError, match="not owned by bk_owner.*zz_unlisted"):
            await admin.execute(BK014_SQL)
        await admin.execute("ROLLBACK")
    finally:
        await admin.execute("DROP TABLE IF EXISTS booking.zz_unlisted")
        await admin.close()


async def test_guard_refuses_without_the_cis_243_grants(migrated_admin_dsn: str) -> None:
    admin = await _admin(migrated_admin_dsn)
    try:
        await admin.execute("REVOKE INSERT ON shared.schema_versions FROM bk_owner")
        with pytest.raises(asyncpg.RaiseError, match="apply CIS 243 first"):
            await admin.execute(BK014_SQL)
        await admin.execute("ROLLBACK")
    finally:
        await admin.execute("GRANT SELECT, INSERT ON shared.schema_versions TO bk_owner")
        await admin.close()


async def test_guard_refuses_to_run_as_bk_owner(migrated_admin_dsn: str) -> None:
    """After the transfer DB Migrate would run a booking/ file as bk_owner (XD8): bk-014 must refuse, clearly."""
    admin = await _admin(migrated_admin_dsn)
    try:
        async with as_booking_owner(admin) as role:
            assert role == OWNER_ROLE
            with pytest.raises(asyncpg.RaiseError, match="bk-014 runs only as the master before the transfer"):
                await admin.execute(BK014_SQL)
            await admin.execute("ROLLBACK")
    finally:
        await admin.close()


async def test_master_owns_nothing_in_booking(migrated_admin_dsn: str) -> None:
    conn = await _admin(migrated_admin_dsn)
    try:
        assert [r for r in await conn.fetch(OWNED_SQL) if r["owner"] == MASTER_ROLE] == []
    finally:
        await conn.close()
