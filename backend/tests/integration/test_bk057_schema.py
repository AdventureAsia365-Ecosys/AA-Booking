"""AA-1093 lane L3 (bk-057) on a real PostgreSQL: the append-only tenant erasure log, its report view (contract
tenant.erasure.v1 ErasureReportRow), the opening of an erasure and the scoped erase guards.

Registration and re-run no-op; the log has RLS enabled and forced with the bk-049 policy pair; role booking may insert
a report but never update or delete one; the report view has the contract's columns, is read-only for booking and
readable by cis_app, the table is not; audit_logs, trip_case_status_history and agency_payment refuse the redaction
without an opened erasure, with a GUC that names no ok DRY RUN, with a tenant bound and on a row of another tenant, and
let through only the redaction for the tenant of the ok DRY RUN the GUC names. All names are invented.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[3] / "migrations" / "057_tenant_erasure_log.sql"
HASH = "a" * 64
MARKER = '{"redacted": "tenant_erased"}'
MINE, OTHER = (
    uuid.UUID("00000000-0000-4000-8000-000000105701"),
    uuid.UUID("00000000-0000-4000-8000-000000105702"),
)
OPEN = "SELECT set_config('booking.tenant_erase', $1, true)"


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


async def _owner(admin: asyncpg.Connection, *statements: tuple[str, tuple[Any, ...]]) -> None:
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE bk_owner")
        for sql, args in statements:
            await admin.execute(sql, *args)


@pytest_asyncio.fixture
async def clean(admin: asyncpg.Connection) -> AsyncIterator[None]:
    yield
    await _owner(admin, ("DELETE FROM booking.tenant_erasure_log WHERE tenant_id = ANY($1::uuid[])", ([MINE, OTHER],)))


async def _report(
    conn: asyncpg.Connection, tenant: uuid.UUID, mode: str = "dry_run", status: str = "ok", **extra: Any
) -> str:
    code = extra.get("error_code", None if status == "ok" else "plan_changed")
    plan = extra.get("plan_hash", None if mode == "verify" else HASH)
    census = extra.get(
        "census", json.dumps({"pii_columns_checked": 3, "pii_rows_found": 0}) if mode == "verify" else None
    )
    return await conn.fetchval(
        "INSERT INTO booking.tenant_erasure_log (request_id, tenant_id, round, mode, status, error_code, plan_hash,"
        " tables, census, source) VALUES (gen_random_uuid(), $1, 1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, 'cli')"
        " RETURNING report_id::text",
        tenant,
        mode,
        status,
        code,
        plan,
        json.dumps([{"table": "booking.trip_cases", "action": "redact", "rows": 2, "digest": "d" * 32}]),
        census,
    )


async def test_registered_and_rerun_is_a_no_op(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 FROM booking.v_schema_versions WHERE version = 'bk-057'") == 1
    query = (
        "SELECT tgname, tgrelid::regclass::text AS rel FROM pg_trigger WHERE NOT tgisinternal AND tgname IN"
        " ('audit_logs_erase_guard', 'trip_case_status_history_erase_guard', 'agency_payment_guard') ORDER BY tgname"
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
            "SELECT policyname AS name, cmd, roles::text[] AS roles, qual FROM pg_policies"
            " WHERE schemaname = 'booking' AND tablename = 'tenant_erasure_log'"
        )
    }
    assert set(policies) == {"tenant_erasure_log_back_office", "tenant_erasure_log_tenant"}
    assert policies["tenant_erasure_log_back_office"]["cmd"] == "ALL"
    assert policies["tenant_erasure_log_tenant"]["cmd"] == "SELECT"
    for p in policies.values():
        assert sorted(p["roles"]) == ["bk_owner", "booking"]
        assert "NULLIF(current_setting('app.tenant_id'::text, true), ''::text)" in p["qual"]


async def test_the_log_is_append_only_and_holds_contract_values(app: asyncpg.Connection, clean: None) -> None:
    row = await _report(app, MINE)
    await _report(app, OTHER)
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app.execute("UPDATE booking.tenant_erasure_log SET status = 'failed' WHERE report_id = $1::uuid", row)
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app.execute("DELETE FROM booking.tenant_erasure_log WHERE report_id = $1::uuid", row)
    bad: list[dict[str, Any]] = [
        {"status": "planned"},  # the contract statuses only
        {"status": "refused", "error_code": "Free text here"},  # a code, never free text
        {"status": "ok", "error_code": "plan_changed"},  # a code exactly when not ok
        {"mode": "verify", "plan_hash": HASH},  # no hash on verify
        {"mode": "verify", "census": None},  # an ok verify carries its census
        {"mode": "apply", "plan_hash": None},  # an ok apply names its hash
    ]
    for kwargs in bad:
        with pytest.raises(asyncpg.CheckViolationError):
            await _report(app, MINE, **kwargs)
    assert await _report(app, MINE, mode="verify")
    async with app.transaction():
        await app.execute("SELECT set_config('app.tenant_id', $1, true)", str(MINE))
        seen = {r["tenant_id"] for r in await app.fetch("SELECT tenant_id FROM booking.tenant_erasure_log")}
        assert seen == {MINE}
        with pytest.raises(asyncpg.InsufficientPrivilegeError):  # a bound tenant writes no report
            await _report(app, MINE)


async def test_report_view_has_the_contract_columns_and_is_the_only_cis_read(
    admin: asyncpg.Connection, app: asyncpg.Connection, clean: None
) -> None:
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
        "report_id", "request_id", "tenant_id", "app", "round", "mode", "status", "error_code", "plan_hash", "tables",
        "blockers", "census", "job_id", "reported_at",
    ]  # fmt: skip
    await _report(app, MINE)
    await _report(app, OTHER)
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE cis_app")
        rows = await admin.fetch(
            "SELECT to_jsonb(v) AS r FROM booking.v_tenant_erasure_report_v1 v WHERE v.tenant_id = ANY($1::uuid[])",
            [MINE, OTHER],
        )
    reports = [json.loads(r["r"]) for r in rows]
    assert {r["tenant_id"] for r in reports} == {str(MINE), str(OTHER)}
    assert all(r["app"] == "booking" and r["round"] == 1 and r["tables"][0]["rows"] == 2 for r in reports)


@pytest_asyncio.fixture
async def footprint(admin: asyncpg.Connection, clean: None) -> AsyncIterator[dict[str, Any]]:
    """Per tenant: a partner, a B2B case with a status note, a partner audit row and a rejected agency payment."""
    made: dict[str, Any] = {}
    entity = await admin.fetchval(
        "INSERT INTO booking.billing_entity (code, legal_entity) VALUES ('IT057', 'Invented Issuer Ltd')"
        " ON CONFLICT (code) DO UPDATE SET legal_entity = EXCLUDED.legal_entity RETURNING id::text"
    )
    try:
        for tenant, name in ((MINE, "Quillmere Tours"), (OTHER, "Brackenford Trails")):
            partner = await admin.fetchval(
                "INSERT INTO booking.b2b_partners (tenant_id, agency_name, status, billing_currency, m2m_client_id)"
                " VALUES ($1, $2, 'suspended', 'USD', 'crmClient057') RETURNING id::text",
                tenant,
                name,
            )
            case = await admin.fetchval(
                "INSERT INTO booking.trip_cases (channel, party_tenant_id, status, pax, currency, source_system)"
                " VALUES ('b2b', $1, 'confirmed', 2, 'USD', 'crm') RETURNING id::text",
                tenant,
            )
            invoice = await admin.fetchval(
                "INSERT INTO booking.agency_invoice (party_tenant_id, invoice_kind, period_start, period_end, currency,"
                " subtotal, total, source, created_by, idempotency_key) VALUES ($1, 'settlement', DATE '2026-10-01',"
                " DATE '2026-11-01', 'USD', 100, 100, 'job', 'job:test', $2) RETURNING id::text",
                tenant,
                f"it-057:{tenant}",
            )
            await admin.execute(
                "UPDATE booking.agency_invoice SET status = 'issued', number = $2, issued_on = DATE '2026-11-02',"
                " due_date = DATE '2026-11-16', issued_at = now(), issued_by = 'staff:it',"
                " billing_entity_id = $3::uuid,"
                ' issuer = \'{"legal_entity": "Invented Issuer Ltd"}\'::jsonb,'
                ' bill_to = \'{"agency_name": "invented bill-to"}\'::jsonb WHERE id = $1::uuid',
                invoice,
                f"AAT-2026-9057{str(tenant)[-2:]}",
                entity,
            )
            payment = await admin.fetchval(
                "INSERT INTO booking.agency_payment (invoice_id, amount, currency, method, received_on, document_ref,"
                " note, recorded_by, idempotency_key) VALUES ($1::uuid, 50, 'USD', 'bank_transfer', DATE '2026-11-03',"
                " 'slip 1', 'invented payment note', 'staff:maker', $2) RETURNING id::text",
                invoice,
                f"it-057-pay:{tenant}",
            )
            await admin.execute(
                "UPDATE booking.agency_payment SET status = 'rejected', rejected_by = 'staff:checker',"
                " rejected_at = now(), reject_reason = 'invented reject reason' WHERE id = $1::uuid",
                payment,
            )
            await _owner(
                admin,
                (
                    "INSERT INTO booking.trip_case_status_history (trip_case_id, from_status, to_status, note)"
                    " VALUES ($1::uuid, 'confirmed', 'cancelled', 'invented cancel reason')",
                    (case,),
                ),
                (
                    "INSERT INTO booking.audit_logs (actor_type, actor_ref, action, entity_type, entity_id, before,"
                    " after, entity_label, summary) VALUES ('staff', 'it-bk057', 'trip_case.status_changed',"
                    " 'b2b_partner', $1::uuid, $2::jsonb, $2::jsonb, $3, $4)",
                    (partner, json.dumps({"agency_name": name}), name, f"confirmed -> cancelled: {name} called"),
                ),
            )
            made[str(tenant)] = {"partner": partner, "case": case, "invoice": invoice, "payment": payment}
        yield made
    finally:
        for parts in made.values():
            await _owner(
                admin,
                ("DELETE FROM booking.audit_logs WHERE entity_id = $1::uuid", (parts["partner"],)),
                ("DELETE FROM booking.trip_case_status_history WHERE trip_case_id = $1::uuid", (parts["case"],)),
                ("DELETE FROM booking.agency_payment WHERE id = $1::uuid", (parts["payment"],)),
                ("ALTER TABLE booking.agency_invoice DISABLE TRIGGER agency_invoice_guard", ()),
                ("DELETE FROM booking.agency_invoice WHERE id = $1::uuid", (parts["invoice"],)),
                ("ALTER TABLE booking.agency_invoice ENABLE TRIGGER agency_invoice_guard", ()),
                ("DELETE FROM booking.trip_cases WHERE id = $1::uuid", (parts["case"],)),
                ("DELETE FROM booking.b2b_partners WHERE id = $1::uuid", (parts["partner"],)),
            )
        await _owner(admin, ("DELETE FROM booking.billing_entity WHERE code = 'IT057'", ()))


REDACT_AUDIT = (
    f"UPDATE booking.audit_logs SET before = '{MARKER}', after = '{MARKER}', entity_label = NULL,"  # noqa: S608
    " summary = '[redacted]' WHERE entity_id = $1::uuid"
)
CLEAR_NOTE = "UPDATE booking.trip_case_status_history SET note = NULL WHERE trip_case_id = $1::uuid"
REDACT_PAYMENT = "UPDATE booking.agency_payment SET note = NULL, reject_reason = '[redacted]' WHERE id = $1::uuid"


async def _refused(app: asyncpg.Connection, opening: str | None, sql: str, arg: str, error: type[Exception]) -> None:
    with pytest.raises(error):
        async with app.transaction():
            if opening is not None:
                await app.execute(OPEN, opening)
            await app.execute(sql, arg)


async def test_the_guards_refuse_without_a_valid_scoped_opening(
    app: asyncpg.Connection, footprint: dict[str, Any]
) -> None:
    mine, other = footprint[str(MINE)], footprint[str(OTHER)]
    denied = asyncpg.InsufficientPrivilegeError
    for sql, arg in ((REDACT_AUDIT, mine["partner"]), (CLEAR_NOTE, mine["case"])):
        await _refused(app, None, sql, arg, denied)  # no opening: append-only
        await _refused(app, "not-a-report", sql, arg, denied)  # any value: refused
        await _refused(app, str(uuid.uuid4()), sql, arg, denied)  # names no report
    await _refused(
        app, None, REDACT_PAYMENT, mine["payment"], asyncpg.RestrictViolationError
    )  # bk-042: the decision is final
    failed = await _report(app, MINE, status="refused")
    planned_other = await _report(app, OTHER)
    for sql, arg in ((REDACT_AUDIT, mine["partner"]), (CLEAR_NOTE, mine["case"]), (REDACT_PAYMENT, mine["payment"])):
        await _refused(app, failed, sql, arg, denied)  # a refused DRY RUN opens nothing
        await _refused(app, planned_other, sql, arg, denied)  # another tenant's DRY RUN: not this row
    mine_ok = await _report(app, MINE)
    for sql, arg in ((REDACT_AUDIT, other["partner"]), (CLEAR_NOTE, other["case"]), (REDACT_PAYMENT, other["payment"])):
        await _refused(app, mine_ok, sql, arg, denied)  # opened for MINE, row of OTHER
    with pytest.raises(asyncpg.InsufficientPrivilegeError):  # an opening with a tenant bound
        async with app.transaction():
            await app.execute("SELECT set_config('app.tenant_id', $1, true)", str(MINE))
            await app.execute(OPEN, mine_ok)
            await app.execute(CLEAR_NOTE, mine["case"])
    with pytest.raises(asyncpg.InsufficientPrivilegeError):  # columns outside the grant
        await app.execute(
            "UPDATE booking.audit_logs SET action = 'tampered' WHERE entity_id = $1::uuid", mine["partner"]
        )
    async with app.transaction():
        await app.execute(OPEN, mine_ok)
        for sql in (
            'UPDATE booking.audit_logs SET after = \'{"agency_name": "Other"}\' WHERE entity_id = $1::uuid',
            "UPDATE booking.audit_logs SET summary = 'rewritten' WHERE entity_id = $1::uuid",
        ):
            with pytest.raises(asyncpg.CheckViolationError):  # only NULL or the marker
                async with app.transaction():
                    await app.execute(sql, mine["partner"])
        with pytest.raises(asyncpg.CheckViolationError):
            async with app.transaction():
                await app.execute(
                    "UPDATE booking.trip_case_status_history SET note = 'x' WHERE trip_case_id = $1::uuid", mine["case"]
                )
        with pytest.raises(asyncpg.CheckViolationError):  # amounts stay
            async with app.transaction():
                await app.execute("UPDATE booking.agency_payment SET amount = 1, note = NULL WHERE id = $1::uuid",
                                  mine["payment"])  # fmt: skip


async def test_an_opened_erasure_redacts_exactly_its_tenant(app: asyncpg.Connection, footprint: dict[str, Any]) -> None:
    mine, other = footprint[str(MINE)], footprint[str(OTHER)]
    opening = await _report(app, MINE)
    async with app.transaction():
        await app.execute(OPEN, opening)
        assert await app.fetchval("SELECT booking.tenant_erase_tenant()") == MINE
        scope = {r["id"] for r in await app.fetch("SELECT id FROM booking.tenant_erase_scope($1) AS s(id)", MINE)}
        assert {uuid.UUID(mine[k]) for k in ("partner", "case", "invoice", "payment")} <= scope
        assert not {uuid.UUID(other[k]) for k in ("partner", "case", "invoice", "payment")} & scope
        await app.execute(REDACT_AUDIT, mine["partner"])
        await app.execute(CLEAR_NOTE, mine["case"])
        await app.execute(REDACT_PAYMENT, mine["payment"])
    audit = await app.fetchrow(
        "SELECT before::text AS b, after::text AS a, entity_label, summary, action FROM booking.audit_logs"
        " WHERE entity_id = $1::uuid",
        mine["partner"],
    )
    assert dict(audit) == {"b": MARKER, "a": MARKER, "entity_label": None, "summary": "[redacted]",
                           "action": "trip_case.status_changed"}  # fmt: skip
    notes = await app.fetch("SELECT note FROM booking.trip_case_status_history WHERE trip_case_id = $1::uuid",
                            mine["case"])  # fmt: skip
    assert notes and all(r["note"] is None for r in notes)
    pay = await app.fetchrow(
        "SELECT note, reject_reason, status, amount::text AS amount FROM booking.agency_payment WHERE id = $1::uuid",
        mine["payment"],
    )
    assert dict(pay) == {"note": None, "reject_reason": "[redacted]", "status": "rejected", "amount": "50.00"}
    untouched = await app.fetchrow(
        "SELECT (SELECT summary FROM booking.audit_logs WHERE entity_id = $1::uuid) AS summary,"
        " (SELECT note FROM booking.agency_payment WHERE id = $2::uuid) AS note",
        other["partner"],
        other["payment"],
    )
    assert untouched["summary"].endswith("Brackenford Trails called") and untouched["note"] == "invented payment note"
