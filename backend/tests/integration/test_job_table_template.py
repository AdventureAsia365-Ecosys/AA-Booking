"""booking.job conforms to the job table DDL template (AA-751, X6; ADR 0003a decision 2).

The template and the comparator are byte copies of AA-CIS-App ``shared/jobs/`` (``tests/job_template/``, drift-checked
by CI job "Job template copy"). The template is rendered with Booking's placeholders into a scratch schema inside a
transaction that is rolled back, then both tables are read from pg_catalog and compared: columns, CHECKs, PK / FK,
indexes by definition, the updated_at trigger and reloptions. Runs after bk-013 (and, in this suite, after the second
pass of every migration in test_bk001), so it also shows the catch-up converges.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import asyncpg
import pytest
import pytest_asyncio

from tests.job_template.job_table_shape import BOOKING_KIND_PATTERN, compare, load_template, read_shape, render

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

SCRATCH = "x6_tmpl"
# Booking-only index (bk-002): the recurring pull is a singleton. Normalised as the comparator prints it.
PULL_SINGLETON_INDEX = (
    "UNIQUE USING btree (kind) WHERE ((kind = 'bk_inquiry_pull'::text) "
    "AND (status = ANY (ARRAY['queued'::text, 'running'::text])))"
)


def _rendered() -> str:
    return render(
        load_template(), schema=SCRATCH, kind_pattern=BOOKING_KIND_PATTERN, extra_statuses="", max_attempts_default=5
    )


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    tx = conn.transaction()
    await tx.start()
    try:
        await conn.execute(f"CREATE SCHEMA {SCRATCH}")
        await conn.execute(_rendered())
        yield conn
    finally:
        await tx.rollback()
        await conn.close()


async def test_booking_job_matches_the_template(admin: asyncpg.Connection) -> None:
    diffs = compare(
        await read_shape(admin, SCRATCH),
        await read_shape(admin, "booking"),
        allow_extra_indexes=(PULL_SINGLETON_INDEX,),
    )
    assert diffs == [], "\n".join(diffs)


async def test_the_comparison_reports_drift(admin: asyncpg.Connection) -> None:
    """Not vacuous: a missing template index, a missing reloption and an unlisted extra index all show up."""
    await admin.execute("DROP INDEX booking.job_running_idx")
    await admin.execute("ALTER TABLE booking.job RESET (autovacuum_vacuum_threshold)")
    await admin.execute("CREATE INDEX zz_x6_extra ON booking.job (created_by)")
    diffs = "\n".join(compare(await read_shape(admin, SCRATCH), await read_shape(admin, "booking")))
    assert "index missing: USING btree (kind, locked_until) WHERE (status = 'running'::text)" in diffs
    assert "reloption missing: autovacuum_vacuum_threshold=50" in diffs
    assert "index not in template: USING btree (created_by)" in diffs
    assert "index not in template: UNIQUE USING btree (kind) WHERE ((kind = 'bk_inquiry_pull'" in diffs


async def test_read_shape_works_as_role_booking(migrated_booking_dsn: str) -> None:
    """Role booking has "$user" = booking on its search_path; read_shape pins pg_catalog, so the definitions it reads
    as the app role are qualified exactly as the admin's (F15)."""
    conn = await asyncpg.connect(migrated_booking_dsn)
    try:
        shape = await read_shape(conn, "booking")
        assert await conn.fetchval("SELECT current_setting('search_path')") == '"$user", public'
    finally:
        await conn.close()
    assert any("REFERENCES booking.job(id)" in d for _t, _v, d in shape.constraints)
    assert any(" ON booking.job USING btree (kind, locked_until)" in d for d in shape.indexes)
