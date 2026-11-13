"""AA-1093 lane L3 (bk-057) on a real PostgreSQL: the tenant erasure log, its report view and the two erase guards.

Registration and re-run no-op; the log has RLS enabled and forced with the bk-049 policy pair (back office unbound,
tenant bound reads its own rows only); role booking may insert and update a log row but never delete one; the report
view is read-only for booking and readable by cis_app, the table is not; audit_logs and trip_case_status_history stay
append-only without ``booking.tenant_erase`` and let through only the redaction with it.
All names are invented.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[3] / "migrations" / "057_tenant_erasure_log.sql"
HASH = "a" * 64
MARKER = '{"redacted": "tenant_erased"}'


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    try:
        yield conn
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def app(migrated_booking_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_booking_dsn)
    try:
        yield conn
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def tenants(admin: asyncpg.Connection) -> AsyncIterator[tuple[uuid.UUID, uuid.UUID]]:
    a, b = uuid.uuid4(), uuid.uuid4()
    try:
        yield a, b
    finally:
        await admin.execute("SET ROLE bk_owner")
        await admin.execute("DELETE FROM booking.tenant_erasure_log WHERE tenant_id = ANY($1::uuid[])", [a, b])
        await admin.execute("RESET ROLE")


async def _log(conn: asyncpg.Connection, tenant: uuid.UUID, mode: str = "dry_run", status: str = "planned") -> str:
    return await conn.fetchval(
        "INSERT INTO booking.tenant_erasure_log (request_id, tenant_id, mode, plan_hash, counts, status, source)"
        " VALUES (gen_random_uuid(), $1, $2, $3, '{\"trip_cases\": 2}'::jsonb, $4, 'cli') RETURNING id::text",
        tenant,
        mode,
        HASH,
        status,
    )


async def test_registered_and_rerun_is_a_no_op(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 FROM booking.v_schema_versions WHERE version = 'bk-057'") == 1
    query = (
        "SELECT tgname, tgrelid::regclass::text AS rel FROM pg_trigger WHERE NOT tgisinternal AND tgname IN"
        " ('audit_logs_erase_guard', 'trip_case_status_history_erase_guard', 'tenant_erasure_log_set_updated_at')"
        " ORDER BY tgname"
    )
    before = [dict(r) for r in await admin.fetch(query)]
    assert len(before) == 3
    await admin.execute("SET ROLE bk_owner")
    await admin.execute(MIGRATION.read_text(encoding="utf-8"))
    await admin.execute("RESET ROLE")
    assert [dict(r) for r in await admin.fetch(query)] == before


async def test_log_has_forced_rls_and_the_policy_pair(admin: asyncpg.Connection) -> None:
    row = await admin.fetchrow(
        "SELECT relrowsecurity AS enabled, relforcerowsecurity AS forced FROM pg_class"
        " WHERE oid = 'booking.tenant_erasure_log'::regclass"
    )
    assert (row["enabled"], row["forced"]) == (True, True)
    policies = {
        r["name"]: r
        for r in await admin.fetch(
            "SELECT policyname AS name, cmd, roles::text[] AS roles, qual, with_check FROM pg_policies"
            " WHERE schemaname = 'booking' AND tablename = 'tenant_erasure_log'"
        )
    }
    assert set(policies) == {"tenant_erasure_log_back_office", "tenant_erasure_log_tenant"}
    assert policies["tenant_erasure_log_back_office"]["cmd"] == "ALL"
    assert policies["tenant_erasure_log_tenant"]["cmd"] == "SELECT"
    for p in policies.values():
        assert sorted(p["roles"]) == ["bk_owner", "booking"]
        assert "NULLIF(current_setting('app.tenant_id'::text, true), ''::text)" in p["qual"]


async def test_booking_writes_but_never_deletes_and_a_bound_tenant_reads_its_own(
    app: asyncpg.Connection, tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    a, b = tenants
    row_a = await _log(app, a)
    await _log(app, b)
    await app.execute(
        "UPDATE booking.tenant_erasure_log SET status = 'stale', detail = 'plan_changed' WHERE id = $1::uuid", row_a
    )
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app.execute("DELETE FROM booking.tenant_erasure_log WHERE id = $1::uuid", row_a)
    with pytest.raises(asyncpg.CheckViolationError):  # a reason code, never free text
        await app.execute("UPDATE booking.tenant_erasure_log SET detail = 'Free text here' WHERE id = $1::uuid", row_a)
    with pytest.raises(asyncpg.CheckViolationError):  # a dry run is never 'applied'
        await _log(app, a, "dry_run", "applied")
    async with app.transaction():
        await app.execute("SELECT set_config('app.tenant_id', $1, true)", str(a))
        seen = {r["tenant_id"] for r in await app.fetch("SELECT tenant_id FROM booking.tenant_erasure_log")}
        assert seen == {a}
        assert await app.execute("UPDATE booking.tenant_erasure_log SET status = 'planned'") == "UPDATE 0"


async def test_report_view_is_read_only_and_the_only_cis_read(admin: asyncpg.Connection) -> None:
    privilege = "SELECT has_table_privilege($1, $2, $3) AS ok"
    assert (await admin.fetchrow(privilege, "cis_app", "booking.v_tenant_erasure_report_v1", "SELECT"))["ok"]
    assert not (await admin.fetchrow(privilege, "cis_app", "booking.tenant_erasure_log", "SELECT"))["ok"]
    assert not (await admin.fetchrow(privilege, "booking", "booking.v_tenant_erasure_report_v1", "INSERT"))["ok"]
    columns = [
        r["attname"]
        for r in await admin.fetch(
            "SELECT attname FROM pg_attribute WHERE attrelid = 'booking.v_tenant_erasure_report_v1'::regclass"
            " AND attnum > 0 AND NOT attisdropped ORDER BY attnum"
        )
    ]
    assert columns == [
        "request_id", "tenant_id", "mode", "plan_hash", "counts", "status", "detail", "created_at", "updated_at",
        "finished_at",
    ]  # fmt: skip


async def test_cis_app_reads_every_report_row_through_the_view(
    admin: asyncpg.Connection, app: asyncpg.Connection, tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    a, b = tenants
    await _log(app, a)
    await _log(app, b)
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE cis_app")
        rows = await admin.fetch(
            "SELECT tenant_id FROM booking.v_tenant_erasure_report_v1 WHERE tenant_id = ANY($1::uuid[])", [a, b]
        )
    assert {r["tenant_id"] for r in rows} == {a, b}


async def _audit_row(app: asyncpg.Connection) -> str:
    entity = str(uuid.uuid4())
    await app.execute(
        "INSERT INTO booking.audit_logs (actor_type, actor_ref, action, entity_type, entity_id, before, after,"
        " entity_label, summary) VALUES ('system', 'it-bk057', 'b2b_partner.projected', 'b2b_partner', $1::uuid,"
        ' \'{"agency_name": "Quillmere Tours"}\', \'{"agency_name": "Quillmere Travel"}\', \'Quillmere Tours\','
        " 'B2B partner projected')",
        entity,
    )
    return entity


async def test_audit_logs_stay_append_only_outside_an_erasure(
    admin: asyncpg.Connection, app: asyncpg.Connection
) -> None:
    entity = await _audit_row(app)
    try:
        await _audit_checks(app, entity)
    finally:
        await admin.execute("SET ROLE bk_owner")
        await admin.execute("DELETE FROM booking.audit_logs WHERE entity_id = $1::uuid", entity)
        await admin.execute("RESET ROLE")


async def _audit_checks(app: asyncpg.Connection, entity: str) -> None:
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app.execute("UPDATE booking.audit_logs SET before = NULL WHERE entity_id = $1::uuid", entity)
    with pytest.raises(asyncpg.InsufficientPrivilegeError):  # no column grant at all
        await app.execute("UPDATE booking.audit_logs SET action = 'tampered' WHERE entity_id = $1::uuid", entity)
    async with app.transaction():
        await app.execute("SELECT set_config('booking.tenant_erase', 'it-bk057', true)")
        with pytest.raises(asyncpg.CheckViolationError):  # only NULL or the marker
            await app.execute(
                'UPDATE booking.audit_logs SET after = \'{"agency_name": "Other"}\' WHERE entity_id = $1::uuid',
                entity,
            )
    async with app.transaction():
        await app.execute("SELECT set_config('booking.tenant_erase', 'it-bk057', true)")
        await app.execute(
            f"UPDATE booking.audit_logs SET before = '{MARKER}', after = '{MARKER}', entity_label = NULL"  # noqa: S608
            " WHERE entity_id = $1::uuid",
            entity,
        )
    row = await app.fetchrow(
        "SELECT before::text AS b, after::text AS a, entity_label, summary FROM booking.audit_logs"
        " WHERE entity_id = $1::uuid",
        entity,
    )
    assert (row["b"], row["a"], row["entity_label"]) == (MARKER, MARKER, None)
    assert row["summary"] == "B2B partner projected"


async def test_status_history_note_is_cleared_only_inside_an_erasure(
    admin: asyncpg.Connection, app: asyncpg.Connection
) -> None:
    case = await admin.fetchval(
        "INSERT INTO booking.trip_cases (channel, party_tenant_id, status, pax, currency, source_system, notes)"
        " VALUES ('b2b', gen_random_uuid(), 'confirmed', 2, 'USD', 'crm', 'it-bk057') RETURNING id::text"
    )
    try:
        await admin.execute("SET ROLE bk_owner")
        await admin.execute(
            "INSERT INTO booking.trip_case_status_history (trip_case_id, from_status, to_status, changed_by, note)"
            " VALUES ($1::uuid, 'confirmed', 'cancelled', NULL, 'invented cancel reason')",
            case,
        )
        await admin.execute("RESET ROLE")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await app.execute(
                "UPDATE booking.trip_case_status_history SET note = NULL WHERE trip_case_id = $1::uuid", case
            )
        async with app.transaction():
            await app.execute("SELECT set_config('booking.tenant_erase', 'it-bk057', true)")
            with pytest.raises(asyncpg.CheckViolationError):
                await app.execute(
                    "UPDATE booking.trip_case_status_history SET note = 'other' WHERE trip_case_id = $1::uuid", case
                )
        async with app.transaction():
            await app.execute("SELECT set_config('booking.tenant_erase', 'it-bk057', true)")
            await app.execute(
                "UPDATE booking.trip_case_status_history SET note = NULL WHERE trip_case_id = $1::uuid", case
            )
        notes = await app.fetch("SELECT note FROM booking.trip_case_status_history WHERE trip_case_id = $1::uuid", case)
        assert notes and all(r["note"] is None for r in notes)
    finally:
        await admin.execute("RESET ROLE")
        await admin.execute("SET ROLE bk_owner")
        await admin.execute("DELETE FROM booking.trip_case_status_history WHERE trip_case_id = $1::uuid", case)
        await admin.execute("DELETE FROM booking.audit_logs WHERE entity_id = $1::uuid", case)
        await admin.execute("DELETE FROM booking.trip_cases WHERE id = $1::uuid", case)
        await admin.execute("RESET ROLE")
