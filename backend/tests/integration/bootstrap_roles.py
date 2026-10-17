"""Give a throwaway PostgreSQL the role shape of the Dev RDS before the integration tests run.

Usage (from backend/, once per fresh database; idempotent)::

    python -m tests.integration.bootstrap_roles <superuser DSN> [<admin DSN>]

The first argument is a SUPERUSER DSN (e.g. ``postgresql://postgres:<pw>@localhost:5432/booking_ci``) for
the same database as the admin DSN the tests use; the admin DSN (``aa_cis_admin``, its password is set
here) defaults to the one ``conftest.py`` reads from the environment. The superuser is used only here.
The tests themselves then log in as ``aa_cis_admin`` WITHOUT superuser, like the RDS master
(``rds_superuser`` is not a superuser): a superuser skips every membership, ownership and schema-CREATE
check, so an owner transfer such as bk-014 would pass CI and fail only on Dev (X1 plan S5, review A5).

Roles created (X1 plan, AA-Ecosys docs/audits/2026-10-17-S241-x1/x1-plan.md):

* ``aa_cis_admin`` LOGIN NOSUPERUSER CREATEROLE CREATEDB, owner of the test database (the DB Migrate login);
* ``bk_owner`` NOLOGIN, granted to ``aa_cis_admin`` (Infra ``roles/001``, X1 S3);
* ``booking`` LOGIN with the CI-only password from ``conftest.py`` (on Dev Nghiep sets it out of band).

The grants ``bk_owner`` needs on ``shared`` (CIS 243, X1 S4) are in ``stub_shared.sql``, which the tests
apply as ``aa_cis_admin``.
"""

from __future__ import annotations

import asyncio
import sys
from urllib.parse import unquote, urlsplit

import asyncpg

from tests.integration.conftest import ADMIN_DSN, BOOKING_TEST_PASSWORD

MASTER = "aa_cis_admin"
OWNER = "bk_owner"
APP = "booking"


async def _ensure_role(conn: asyncpg.Connection, name: str, attributes: str, password: str | None) -> None:
    exists = await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", name)
    verb = "ALTER" if exists else "CREATE"
    pw = "" if password is None else " PASSWORD " + await conn.fetchval("SELECT quote_literal($1)", password)
    await conn.execute(f"{verb} ROLE {name} {attributes}{pw}")


async def bootstrap(superuser_dsn: str, admin_dsn: str) -> None:
    admin = urlsplit(admin_dsn)
    target = urlsplit(superuser_dsn)
    if admin.username != MASTER:
        raise SystemExit(f"the admin DSN must log in as {MASTER}, got {admin.username!r}")
    if target.path != admin.path:
        raise SystemExit("the superuser DSN and the admin DSN must name the same database")
    if target.username == MASTER:
        raise SystemExit(f"{MASTER} must not be the container superuser: bootstrap as another superuser (postgres)")

    try:
        conn = await asyncpg.connect(superuser_dsn)
    except (asyncpg.InvalidAuthorizationSpecificationError, asyncpg.InvalidPasswordError) as exc:
        raise SystemExit(
            f"cannot log in as {target.username!r} ({exc}). In CI the postgres service must be started with the "
            f"bootstrap superuser 'postgres', not {MASTER} (ci.yml, Integration Tests service env)."
        ) from exc
    try:
        if not await conn.fetchval("SELECT rolsuper FROM pg_roles WHERE rolname = current_user"):
            raise SystemExit(f"{target.username!r} is not a superuser")
        if await conn.fetchval("SELECT rolsuper FROM pg_roles WHERE rolname = $1", MASTER):
            raise SystemExit(
                f"{MASTER} is a superuser (it is the container's bootstrap user, which cannot lose SUPERUSER): "
                f"start the postgres service with the bootstrap user 'postgres' instead"
            )
        await _ensure_role(
            conn,
            MASTER,
            "LOGIN NOSUPERUSER CREATEROLE CREATEDB NOBYPASSRLS NOREPLICATION",
            unquote(admin.password or ""),
        )
        database = admin.path.lstrip("/")
        await conn.execute(f'ALTER DATABASE "{database}" OWNER TO {MASTER}')
        await _ensure_role(conn, OWNER, "NOLOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB NOBYPASSRLS", None)
        await conn.execute(f"GRANT {OWNER} TO {MASTER}")
        await _ensure_role(conn, APP, "LOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB NOBYPASSRLS", BOOKING_TEST_PASSWORD)
    finally:
        await conn.close()
    print(f"bootstrap ok: {MASTER} (no superuser, member of {OWNER}), {OWNER} NOLOGIN, {APP} LOGIN")


def main(argv: list[str]) -> None:
    if len(argv) not in (2, 3):
        raise SystemExit(f"usage: python -m tests.integration.bootstrap_roles <superuser DSN> [<admin DSN>]\n{__doc__}")
    admin_dsn = argv[2] if len(argv) == 3 else ADMIN_DSN
    if not admin_dsn:
        raise SystemExit("no admin DSN: pass it as the second argument or set the one conftest.py reads")
    asyncio.run(bootstrap(argv[1], admin_dsn))


if __name__ == "__main__":
    main(sys.argv)
