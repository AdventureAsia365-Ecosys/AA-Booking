"""The real migrations pass the guard, and the guard catches what it promises to catch."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.migration_guard import MASTER_ROLE, OWNER_ROLE, OWNER_TRANSFER, check_file, check_sql, migration_role

MIGRATIONS = Path(__file__).resolve().parents[3] / "migrations"

GOOD = """
BEGIN;
CREATE SCHEMA IF NOT EXISTS booking;
CREATE TABLE IF NOT EXISTS booking.t (id uuid PRIMARY KEY);
INSERT INTO shared.schema_versions (version, applied_at, description)
VALUES ('bk-002', now(), 'x') ON CONFLICT (version) DO NOTHING;
COMMIT;
"""


def _rules(sql: str, name: str = "002_example.sql") -> set[str]:
    return {v.rule for v in check_sql(name, sql)}


def test_migrations_folder_is_not_empty() -> None:
    assert sorted(p.name for p in MIGRATIONS.glob("*.sql")), "no migrations found"


@pytest.mark.parametrize("path", sorted(MIGRATIONS.glob("*.sql")), ids=lambda p: p.name)
def test_every_migration_passes_the_guard(path: Path) -> None:
    violations = check_file(path)
    assert not violations, "\n".join(str(v) for v in violations)


# Numbers taken by an open parallel branch: a gap is allowed only for these. Remove an entry once that
# branch is merged (the file is then here and the gap is gone).
RESERVED_ON_OTHER_BRANCHES: dict[int, str] = {16: "AA-680 bk-016 staff Cognito identity (feat/aa680-cognito-staff)"}


def test_migration_numbers_are_unique_and_contiguous() -> None:
    numbers = [int(p.name[:3]) for p in MIGRATIONS.glob("*.sql")]
    assert len(numbers) == len(set(numbers)), "two migrations share a number"
    gaps = set(range(1, max(numbers) + 1)) - set(numbers)
    assert gaps <= set(RESERVED_ON_OTHER_BRANCHES), f"unexpected gap(s) {sorted(gaps)}"


def test_good_example_passes() -> None:
    assert _rules(GOOD) == set()


@pytest.mark.parametrize(
    ("sql", "rule"),
    [
        (GOOD.replace("BEGIN;", ""), "transaction"),
        (GOOD.replace("COMMIT;", ""), "transaction"),
        (GOOD.replace("'bk-002'", "'bk-001'"), "register"),
        (GOOD.replace(" ON CONFLICT (version) DO NOTHING", ""), "register"),
        (GOOD.replace("booking.t ", "public.t "), "create-target"),
        (GOOD.replace("CREATE SCHEMA IF NOT EXISTS booking", "CREATE SCHEMA tripplanner"), "create-schema"),
        (GOOD.replace("COMMIT;", "CREATE INDEX i ON shared.job (id);\nCOMMIT;"), "create-target"),
        (
            GOOD.replace("COMMIT;", "ALTER TABLE gold_aa_internal.published_tours ADD COLUMN x int;\nCOMMIT;"),
            "alter-target",
        ),
        (GOOD.replace("COMMIT;", "DROP TABLE tripplanner.outbox;\nCOMMIT;"), "drop-target"),
        (GOOD.replace("COMMIT;", "UPDATE shared.tenants SET name = 'x';\nCOMMIT;"), "dml-target"),
        (GOOD.replace("COMMIT;", "TRUNCATE shared.job;\nCOMMIT;"), "dml-target"),
        (GOOD.replace("COMMIT;", "GRANT SELECT ON shared.destinations TO booking;\nCOMMIT;"), "grant"),
        (GOOD.replace("COMMIT;", "GRANT SELECT ON booking.t TO tripplanner;\nCOMMIT;"), "grant"),
        (GOOD.replace("COMMIT;", "GRANT SELECT ON booking.t TO PUBLIC;\nCOMMIT;"), "no-public-grant"),
        (GOOD.replace("COMMIT;", "SELECT * FROM shared.admin_users;\nCOMMIT;"), "foreign-schema"),
        (GOOD.replace("COMMIT;", "CREATE ROLE booking LOGIN PASSWORD 'secret';\nCOMMIT;"), "no-password"),
        (GOOD.replace("COMMIT;", "CREATE ROLE booking LOGIN;\nCOMMIT;"), "no-login"),
        (GOOD.replace("COMMIT;", "CREATE ROLE other NOLOGIN;\nCOMMIT;"), "create-role"),
        (GOOD.replace("COMMIT;", "ALTER ROLE booking NOLOGIN;\nCOMMIT;"), "no-alter-role"),
        (GOOD.replace("COMMIT;", "CREATE EXTENSION pgcrypto;\nCOMMIT;"), "no-extension"),
        (
            GOOD.replace(
                "COMMIT;",
                "CREATE FUNCTION booking.f() RETURNS int LANGUAGE sql SECURITY DEFINER AS $$ SELECT 1 $$;\nCOMMIT;",
            ),
            "no-security-definer",
        ),
        (
            GOOD.replace(
                "COMMIT;", "ALTER DEFAULT PRIVILEGES IN SCHEMA booking GRANT SELECT ON TABLES TO booking;\nCOMMIT;"
            ),
            "default-privileges",
        ),
        (
            GOOD.replace("COMMIT;", "DO $$ BEGIN CREATE TABLE public.sneaky (id int); END $$;\nCOMMIT;"),
            "create-target",
        ),
    ],
)
def test_guard_catches(sql: str, rule: str) -> None:
    assert rule in _rules(sql)


def test_comments_and_strings_do_not_trigger_rules() -> None:
    sql = GOOD.replace(
        "COMMIT;",
        "-- GRANT SELECT ON shared.job TO booking; CREATE ROLE x LOGIN PASSWORD 'p'\n"
        "COMMENT ON TABLE booking.t IS 'reads shared.job one day; DROP TABLE public.x';\nCOMMIT;",
    )
    assert _rules(sql) == set()


def test_function_grant_with_several_arguments() -> None:
    ok = GOOD.replace("COMMIT;", "GRANT EXECUTE ON FUNCTION booking.f(int, text) TO booking;\nCOMMIT;")
    assert _rules(ok) == set()
    bad = GOOD.replace("COMMIT;", "GRANT EXECUTE ON FUNCTION booking.f(int, text), shared.g() TO booking;\nCOMMIT;")
    assert "grant" in _rules(bad)


def test_trigger_event_lists_are_not_dml() -> None:
    trigger = (
        "CREATE OR REPLACE TRIGGER t_lock BEFORE INSERT OR UPDATE OR DELETE ON booking.t "
        "FOR EACH ROW EXECUTE FUNCTION booking.f();\n"
    )
    assert _rules(GOOD.replace("COMMIT;", trigger + "COMMIT;")) == set()
    elsewhere = (
        "CREATE OR REPLACE TRIGGER t_lock BEFORE UPDATE OR DELETE ON shared.job "
        "FOR EACH ROW EXECUTE FUNCTION booking.f();\n"
    )
    assert "create-target" in _rules(GOOD.replace("COMMIT;", elsewhere + "COMMIT;"))


def test_views_must_be_read_only_for_the_app() -> None:
    view = "CREATE OR REPLACE VIEW booking.v AS SELECT 1 AS one;\n"
    assert "view-read-only" in _rules(GOOD.replace("COMMIT;", view + "COMMIT;"))
    revoked = view + "REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON booking.v FROM booking;\n"
    assert _rules(GOOD.replace("COMMIT;", revoked + "COMMIT;")) == set()


@pytest.mark.parametrize(
    ("statement", "ok"),
    [
        ("GRANT USAGE ON SCHEMA booking TO cis_app;", True),
        ("GRANT SELECT ON booking.v_ops_a, booking.v_ops_b TO cis_app;", True),
        ("GRANT SELECT ON TABLE booking.v_ops_a TO cis_app;", True),
        ("REVOKE SELECT ON booking.v_ops_a FROM cis_app;", True),
        ("REVOKE USAGE ON SCHEMA booking FROM cis_app;", True),
        ("GRANT SELECT ON booking.customers TO cis_app;", False),  # a table, not a v_* view
        ("GRANT SELECT ON booking.v_ops_a, booking.trip_cases TO cis_app;", False),
        ("GRANT INSERT ON booking.v_ops_a TO cis_app;", False),
        ("GRANT SELECT, UPDATE ON booking.v_ops_a TO cis_app;", False),
        ("GRANT CREATE ON SCHEMA booking TO cis_app;", False),
        ("GRANT ALL ON SCHEMA booking TO cis_app;", False),
        ("GRANT SELECT ON ALL TABLES IN SCHEMA booking TO cis_app;", False),
        ("GRANT EXECUTE ON FUNCTION booking.f() TO cis_app;", False),
        ("GRANT SELECT ON booking.v_ops_a TO aa_app_user;", False),  # not a consumer role
    ],
)
def test_consumer_role_gets_only_contract_view_reads(statement: str, ok: bool) -> None:
    """bk-017 (AA-858): another app's runtime role may read booking.v_* views, nothing else."""
    rules = _rules(GOOD.replace("COMMIT;", statement + "\nCOMMIT;"))
    if ok:
        assert rules == set()
    else:
        assert rules & {"consumer-grant", "grant"}


def test_bad_filename() -> None:
    assert _rules(GOOD, name="2_Example.sql") == {"filename"}


# X1 (AA-Ecosys docs/audits/2026-10-17-S241-x1/x1-plan.md S5): the migration role is per file. DB Migrate runs a
# file as the live owner of schema booking: the RDS master up to bk-014, bk_owner from bk-015 on.


def _numbered(sql: str, number: int) -> tuple[str, str]:
    return f"{number:03d}_example.sql", sql.replace("'bk-002'", f"'bk-{number:03d}'")


def _dp(role: str, verb: str = "GRANT") -> str:
    target = "TO booking" if verb == "GRANT" else "FROM booking"
    return f"ALTER DEFAULT PRIVILEGES FOR ROLE {role} IN SCHEMA booking {verb} SELECT ON TABLES {target};\n"


def test_migration_role_per_file() -> None:
    assert OWNER_TRANSFER == 14
    assert [migration_role(n) for n in (1, 13, 14, 15, 99)] == [MASTER_ROLE] * 3 + [OWNER_ROLE] * 2
    assert (MASTER_ROLE, OWNER_ROLE) == ("aa_cis_admin", "bk_owner")


@pytest.mark.parametrize(
    ("number", "role", "verb", "ok"),
    [
        (13, "aa_cis_admin", "GRANT", True),
        (13, "aa_cis_admin", "REVOKE", True),
        (13, "bk_owner", "GRANT", False),
        (14, "bk_owner", "GRANT", True),
        (14, "bk_owner", "REVOKE", True),
        (14, "aa_cis_admin", "REVOKE", True),  # bk-014 drops the old rows ...
        (14, "aa_cis_admin", "GRANT", False),  # ... and never re-creates them
        (15, "bk_owner", "GRANT", True),
        (15, "bk_owner", "REVOKE", True),
        (15, "aa_cis_admin", "GRANT", False),  # would fail as bk_owner: "must be member of role aa_cis_admin"
        (15, "aa_cis_admin", "REVOKE", False),
        (15, "booking", "GRANT", False),
    ],
)
def test_default_privileges_follow_the_file_migration_role(number: int, role: str, verb: str, ok: bool) -> None:
    name, sql = _numbered(GOOD.replace("COMMIT;", _dp(role, verb) + "COMMIT;"), number)
    assert ("default-privileges" not in _rules(sql, name)) is ok


@pytest.mark.parametrize(
    ("number", "statement", "ok"),
    [
        (14, "ALTER SCHEMA booking OWNER TO bk_owner;", True),
        (14, "ALTER TABLE booking.t OWNER TO bk_owner;", True),
        (14, "ALTER VIEW booking.v OWNER TO bk_owner;", True),
        (14, "ALTER SEQUENCE booking.s OWNER TO bk_owner;", True),
        (14, "ALTER FUNCTION booking.f() OWNER TO bk_owner;", True),
        (14, "ALTER TABLE booking.t OWNER TO aa_cis_admin;", False),
        (14, "ALTER TABLE booking.t OWNER TO booking;", False),
        (13, "ALTER TABLE booking.t OWNER TO bk_owner;", False),
        (15, "ALTER TABLE booking.t OWNER TO bk_owner;", False),
        (15, "ALTER SCHEMA booking OWNER TO aa_cis_admin;", False),
        (15, "REASSIGN OWNED BY aa_cis_admin TO bk_owner;", False),
        (14, "DROP OWNED BY booking;", False),
    ],
)
def test_owner_changes_only_in_bk014_to_bk_owner(number: int, statement: str, ok: bool) -> None:
    name, sql = _numbered(GOOD.replace("COMMIT;", statement + "\nCOMMIT;"), number)
    assert ("owner" not in _rules(sql, name)) is ok


@pytest.mark.parametrize(
    "statement",
    [
        "SET ROLE bk_owner;",
        "SET LOCAL ROLE bk_owner;",
        "SET SESSION ROLE bk_owner;",
        "RESET ROLE;",
        "RESET ALL;",
        "SET SESSION AUTHORIZATION aa_cis_admin;",
        "RESET SESSION AUTHORIZATION;",
        "SELECT set_config('role', 'aa_cis_admin', true);",
        "SELECT set_config( E'role', 'aa_cis_admin', true);",
        "DO $$ BEGIN PERFORM set_config('role', 'aa_cis_admin', true); END $$;",
        "DO $$ BEGIN RESET ROLE; END $$;",
    ],
)
def test_role_switches_are_refused(statement: str) -> None:
    for number in (13, 14, 15):
        name, sql = _numbered(GOOD.replace("COMMIT;", statement + "\nCOMMIT;"), number)
        assert "no-set-role" in _rules(sql, name), (number, statement)


def test_other_set_config_names_are_fine() -> None:
    name, sql = _numbered(GOOD.replace("COMMIT;", "SELECT set_config('bk014.acl', 'x', true);\nCOMMIT;"), 14)
    assert _rules(sql, name) == set()
