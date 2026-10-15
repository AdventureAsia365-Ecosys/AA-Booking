"""The real migrations pass the guard, and the guard catches what it promises to catch."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.migration_guard import check_file, check_sql

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
RESERVED_ON_OTHER_BRANCHES: dict[int, str] = {}


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


def test_bad_filename() -> None:
    assert _rules(GOOD, name="2_Example.sql") == {"filename"}
