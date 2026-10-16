"""bk-013 (AA-751, X6) on a real PostgreSQL: booking.job catches up with the job table template. Schema only; the
template comparison test and the queue behaviour that uses the new columns come with the B2 app change.

The session DB has already had every migration applied twice (conftest + test_bk001's re-run); the convergence test
below applies them once more itself, so it does not depend on test order."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import asyncpg
import pytest
import pytest_asyncio

from tests.integration.conftest import apply_migrations

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

# Column -> (format_type, NOT NULL, default expression as pg_get_expr prints it).
NEW_COLUMNS = {
    "cancel_requested": ("boolean", True, "false"),
    "progress": ("jsonb", True, "'{}'::jsonb"),
    "cost_usd": ("numeric(12,6)", True, "0"),
    "parent_job_id": ("uuid", False, None),
}
# Index -> definition after "ON booking.job " (pg_indexes.indexdef), same as the template / CIS shared.job.
TEMPLATE_INDEXES = {
    "job_parent_idx": "USING btree (parent_job_id) WHERE (parent_job_id IS NOT NULL)",
    "job_running_idx": "USING btree (kind, locked_until) WHERE (status = 'running'::text)",
    "job_queued_due_idx": "USING btree (run_after, created_at) WHERE (status = 'queued'::text)",
    "job_finished_idx": "USING btree (finished_at, id) WHERE (finished_at IS NOT NULL)",
    "job_kind_created_idx": "USING btree (kind, created_at DESC)",
    "job_idempotency_key_uq": "USING btree (idempotency_key) WHERE (idempotency_key IS NOT NULL)",
}
# bk-002 names, replaced by job_queued_due_idx / job_running_idx.
OLD_INDEXES = {"job_due_idx", "job_lease_idx"}
# Booking-only extra: the bk_inquiry_pull singleton (bk-002).
BOOKING_EXTRA_INDEXES = {"job_active_pull_uq"}
RELOPTIONS = {"autovacuum_vacuum_scale_factor=0.02", "autovacuum_vacuum_threshold=50"}


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    try:
        yield conn
    finally:
        await conn.close()


async def _shape(conn: asyncpg.Connection) -> dict[str, object]:
    columns = {
        r["attname"]: (r["type"], r["attnotnull"], r["dflt"])
        for r in await conn.fetch(
            "SELECT a.attname, format_type(a.atttypid, a.atttypmod) AS type, a.attnotnull,"
            "       pg_get_expr(d.adbin, d.adrelid) AS dflt"
            "  FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum"
            " WHERE a.attrelid = 'booking.job'::regclass AND a.attnum > 0 AND NOT a.attisdropped"
        )
    }
    indexes = {
        r["indexname"]: r["indexdef"]
        for r in await conn.fetch(
            "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'booking' AND tablename = 'job'"
        )
    }
    fks = [
        (r["col"], r["ref"], r["confdeltype"])
        for r in await conn.fetch(
            "SELECT a.attname AS col, c.confrelid::regclass::text AS ref, c.confdeltype::text AS confdeltype"
            "  FROM pg_constraint c JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)"
            " WHERE c.conrelid = 'booking.job'::regclass AND c.contype = 'f' ORDER BY 1"
        )
    ]
    reloptions = await conn.fetchval(
        "SELECT coalesce(reloptions, '{}') FROM pg_class WHERE oid = 'booking.job'::regclass"
    )
    return {"columns": columns, "indexes": indexes, "fks": fks, "reloptions": set(reloptions)}


def _assert_template_shape(shape: dict[str, object]) -> None:
    columns = shape["columns"]
    assert isinstance(columns, dict)
    for name, expected in NEW_COLUMNS.items():
        assert columns.get(name) == expected, f"{name}: {columns.get(name)!r} != {expected!r}"

    indexes = shape["indexes"]
    assert isinstance(indexes, dict)
    for name, tail in TEMPLATE_INDEXES.items():
        assert name in indexes, f"{name} missing: {sorted(indexes)}"
        assert indexes[name].endswith(f"ON booking.job {tail}"), indexes[name]
    assert not OLD_INDEXES & set(indexes), f"old bk-002 index names still present: {sorted(OLD_INDEXES & set(indexes))}"
    assert set(indexes) == set(TEMPLATE_INDEXES) | BOOKING_EXTRA_INDEXES | {"job_pkey"}, sorted(indexes)

    # Exactly one FK (a re-run must not add a second one), self-referencing, ON DELETE NO ACTION as on CIS.
    assert shape["fks"] == [("parent_job_id", "booking.job", "a")]
    assert RELOPTIONS <= shape["reloptions"]  # type: ignore[operator]


async def test_bk013_is_registered(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 FROM booking.v_schema_versions WHERE version = 'bk-013'") == 1


async def test_job_has_the_template_columns_indexes_fk_and_reloptions(admin: asyncpg.Connection) -> None:
    _assert_template_shape(await _shape(admin))


async def test_another_full_pass_converges_to_the_same_shape(admin: asyncpg.Connection) -> None:
    before = await _shape(admin)
    await apply_migrations(admin)  # bk-002 re-creates job_due_idx / job_lease_idx; bk-013 must drop them again
    after = await _shape(admin)
    assert after == before
    _assert_template_shape(after)


async def test_new_columns_default_on_insert_and_parent_fk_is_enforced(migrated_booking_dsn: str) -> None:
    conn = await asyncpg.connect(migrated_booking_dsn)  # role booking: the app's table-level grants cover the columns
    tr = conn.transaction()
    await tr.start()
    try:
        parent = await conn.fetchrow(
            "INSERT INTO booking.job (kind, created_by) VALUES ('bk_x6_schema_test', 'test_bk013')"
            " RETURNING id, cancel_requested, progress, cost_usd, parent_job_id"
        )
        assert parent["cancel_requested"] is False
        assert parent["progress"] == "{}"
        assert parent["cost_usd"] == 0
        assert parent["parent_job_id"] is None

        child = await conn.fetchval(
            "INSERT INTO booking.job (kind, created_by, parent_job_id) VALUES ('bk_x6_schema_test', 'test_bk013', $1)"
            " RETURNING parent_job_id",
            parent["id"],
        )
        assert child == parent["id"]

        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute(
                "INSERT INTO booking.job (kind, created_by, parent_job_id)"
                " VALUES ('bk_x6_schema_test', 'test_bk013', $1)",
                uuid.uuid4(),
            )
    finally:
        await tr.rollback()
        await conn.close()
