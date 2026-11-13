"""AA-1093 lane L3 on PostgreSQL 15 with the RDS role shape, as role ``booking``: the Booking step of the tenant
offboarding erasure (:mod:`app.b2b.tenant_erase`, bk-057, contract tenant.erasure.v1) and the ``bk_tenant_pull`` skip of
an erased agency.

``catalog.v_tenant_account_v1`` (CIS 357 + 401) and ``catalog.v_tenant_erasure_v1`` (CIS 410, the contract's
ErasureRequestRow) are stood in by plain tables around each test. Three agencies: a synthetic one (erased), another
synthetic one (must stay untouched) and the pilot. Each has a partner row, a confirmed B2B case with a case note, a
custom trip check whose title names the agency, a check note, a status note with a cancel reason, a notification whose
title names the agency, audit rows (one with the cancel reason in its summary) and an issued agency invoice with a
staff note, its bill-to and a rejected payment with a note.
Covers: the DRY RUN reports the plan and writes nothing, its hash is stable and moves with a row and with the round;
the whole saga path (schedule -> DRY RUN job -> report view -> approval -> apply job -> verify job) anonymises exactly
the agency (money, numbers and the bill-to untouched, the other agency untouched) with a census of 0; a plan that moved
after the DRY RUN is refused and writes nothing; the guards (pilot, not synthetic, kill switch, expired approval, not
due); a later round of the request is superseded; every pii: column is either cleared or kept by rule; the pull keeps
the erased name. All names are invented.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from app import audit
from app.b2b import tenant_erase, tenant_pull
from app.catalog import liveness
from app.jobs.queue import Job
from tests.integration import ops

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ACCOUNT_COLUMNS = (
    "tenant_id uuid, name text, account_status text, suspend_reason text, in_service boolean, kind text, "
    "is_pilot boolean, trade_book boolean, is_synthetic boolean"
)
ACCOUNT_STUB = (
    f"CREATE TABLE catalog.v_tenant_account_v1 ({ACCOUNT_COLUMNS}); "
    "GRANT SELECT ON catalog.v_tenant_account_v1 TO booking;"
)
ACCOUNT_ADD = (
    "INSERT INTO catalog.v_tenant_account_v1 (tenant_id, name, account_status, suspend_reason, in_service, kind,"
    " is_pilot, trade_book, is_synthetic) VALUES ($1, $2, 'offboarding', NULL, false, 'agency', $3, true, $4)"
)
REQUEST_STUB = (
    "CREATE TABLE catalog.v_tenant_erasure_v1 (request_id uuid, tenant_id uuid, phase text, round integer,"
    " opened_at timestamptz, approval_expires_at timestamptz, is_pilot boolean, is_synthetic boolean, app text,"
    " step_status text, not_before timestamptz, approved_plan_hash text, updated_at timestamptz);"
    " GRANT SELECT ON catalog.v_tenant_erasure_v1 TO booking;"
)
REQUEST_ADD = (
    "INSERT INTO catalog.v_tenant_erasure_v1 VALUES ($1, $2, 'dry_run', 1, now() - interval '31 days', NULL, $3, $4,"
    " 'booking', 'active', now() - interval '1 day', NULL, now())"
)
DROPS = ("DROP TABLE IF EXISTS catalog.v_tenant_account_v1", "DROP TABLE IF EXISTS catalog.v_tenant_erasure_v1")
ERASED, OTHER, PILOT = (
    uuid.UUID("00000000-0000-4000-8000-000000109301"),
    uuid.UUID("00000000-0000-4000-8000-000000109302"),
    uuid.UUID("00000000-0000-4000-8000-000000109303"),
)
NAMES = {ERASED: "Willowmere Journeys", OTHER: "Ferncliff Voyages", PILOT: "Pilot Harbour Travel"}
CLIENT = "crmClient1093"
BILL_TO = {"agency_name": "invented bill-to", "source": "cis", "billing_country": "VN"}
MARKER = '{"redacted": "tenant_erased"}'
ENTITY_CODE = "IT1093"


async def _owner(admin: asyncpg.Connection, *statements: tuple[str, tuple[Any, ...]]) -> None:
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE bk_owner")
        for sql, args in statements:
            await admin.execute(sql, *args)


async def _owner_cleanup(admin: asyncpg.Connection, agencies: dict[uuid.UUID, dict[str, Any]]) -> None:
    tenants = list(agencies)
    cases = [a["case"] for a in agencies.values()]
    invoices = [a["invoice"] for a in agencies.values()]
    entities = [*cases, *(a["partner"] for a in agencies.values()), *(str(t) for t in tenants), *invoices,
                *(a["payment"] for a in agencies.values())]  # fmt: skip
    sups = [a["supplier"] for a in agencies.values()]
    await _owner(
        admin,
        ("DELETE FROM booking.job WHERE kind = $1", (tenant_erase.KIND,)),
        ("DELETE FROM booking.tenant_erasure_log WHERE tenant_id = ANY($1::uuid[])", (tenants,)),
        (
            "DELETE FROM booking.notifications WHERE entity_id = ANY($1::uuid[]) OR entity_id IN"
            " (SELECT id FROM booking.trip_case_checks WHERE trip_case_id = ANY($2::uuid[]))",
            (cases, cases),
        ),
        (
            "DELETE FROM booking.audit_logs WHERE entity_id = ANY($1::uuid[]) OR entity_id IN"
            " (SELECT id FROM booking.trip_case_checks WHERE trip_case_id = ANY($2::uuid[]))"
            " OR entity_id IN (SELECT id FROM booking.supplier_bookings WHERE trip_case_id = ANY($2::uuid[]))",
            (entities, cases),
        ),
        ("DELETE FROM booking.agency_payment WHERE invoice_id = ANY($1::uuid[])", (invoices,)),
        ("ALTER TABLE booking.agency_invoice DISABLE TRIGGER agency_invoice_guard", ()),
        ("DELETE FROM booking.agency_invoice WHERE id = ANY($1::uuid[])", (invoices,)),
        ("ALTER TABLE booking.agency_invoice ENABLE TRIGGER agency_invoice_guard", ()),
        ("DELETE FROM booking.billing_entity WHERE code = $1", (ENTITY_CODE,)),
        ("DELETE FROM booking.trip_case_status_history WHERE trip_case_id = ANY($1::uuid[])", (cases,)),
        ("DELETE FROM booking.supplier_bookings WHERE trip_case_id = ANY($1::uuid[])", (cases,)),
        ("DELETE FROM booking.hotel_room_types WHERE hotel_id = ANY($1::uuid[])", ([x["hotel"] for x in sups],)),
        ("DELETE FROM booking.services WHERE supplier_id = ANY($1::uuid[])", ([x["supplier"] for x in sups],)),
        ("DELETE FROM booking.hotels WHERE id = ANY($1::uuid[])", ([x["hotel"] for x in sups],)),
        ("DELETE FROM booking.suppliers WHERE id = ANY($1::uuid[])", ([x["supplier"] for x in sups],)),
        ("DELETE FROM booking.trip_case_checks WHERE trip_case_id = ANY($1::uuid[])", (cases,)),
        ("DELETE FROM booking.trip_cases WHERE id = ANY($1::uuid[])", (cases,)),
        ("DELETE FROM booking.b2b_partners WHERE tenant_id = ANY($1::uuid[])", (tenants,)),
    )


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    for sql in DROPS:
        await conn.execute(sql)
    await conn.execute(ACCOUNT_STUB)
    await conn.execute(REQUEST_STUB)
    for tenant, pilot in ((ERASED, False), (OTHER, False), (PILOT, True)):
        await conn.execute(ACCOUNT_ADD, tenant, NAMES[tenant], pilot, not pilot)
    liveness.reset_cache()
    try:
        yield conn
    finally:
        for sql in DROPS:
            await conn.execute(sql)
        liveness.reset_cache()
        await conn.close()


@pytest_asyncio.fixture
async def pool(admin: asyncpg.Connection, migrated_booking_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    p = await asyncpg.create_pool(migrated_booking_dsn, min_size=1, max_size=3)
    try:
        yield p
    finally:
        await p.close()


async def _agency(admin: asyncpg.Connection, pool: asyncpg.Pool, tenant: uuid.UUID, entity: str) -> dict[str, Any]:
    name, tag = NAMES[tenant], str(tenant)[-2:]
    partner = await admin.fetchval(
        "INSERT INTO booking.b2b_partners (tenant_id, agency_name, status, billing_currency, m2m_client_id,"
        " projected_at) VALUES ($1, $2, 'suspended', 'USD', $3, now()) RETURNING id::text",
        tenant,
        name,
        CLIENT,
    )
    case = (await ops.b2b_case(admin, tag=f"1093-{tag}", status="confirmed"))["id"]
    await admin.execute(
        "UPDATE booking.trip_cases SET party_tenant_id = $2, notes = $3 WHERE id = $1::uuid",
        case,
        tenant,
        f"Call {name} about the invented lead traveller",
    )
    custom = await admin.fetchval(
        "INSERT INTO booking.trip_case_checks (trip_case_id, code, title, status, note) VALUES ($1::uuid, 'custom',"
        " $2, 'open', 'invented check note') RETURNING id::text",
        case,
        f"Ring {name} back",
    )
    await _owner(
        admin,
        (
            "INSERT INTO booking.trip_case_status_history (trip_case_id, from_status, to_status, note)"
            " VALUES ($1::uuid, 'confirmed', 'confirmed', 'invented cancel reason')",
            (case,),
        ),
    )
    sup = await ops.supply(admin, f"Erase {tag}")
    booking_id = await admin.fetchval(
        "INSERT INTO booking.supplier_bookings (trip_case_id, supplier_id, item_description, notes)"
        " VALUES ($1::uuid, $2::uuid, 'Invented boat', 'invented supplier note') RETURNING id::text",
        case,
        sup["supplier"],
    )
    await admin.execute(
        "UPDATE booking.supplier_bookings SET status = 'cancelled', cancel_reason = 'invented reason',"
        " cancelled_at = now() WHERE id = $1::uuid",
        booking_id,
    )
    invoice = await admin.fetchval(
        "INSERT INTO booking.agency_invoice (party_tenant_id, invoice_kind, period_start, period_end, currency,"
        " subtotal, total, source, created_by, idempotency_key, bill_to, staff_note)"
        " VALUES ($1, 'settlement', DATE '2026-10-01', DATE '2026-11-01', 'USD', 100, 100, 'job', 'job:test', $2,"
        " $3::jsonb, $4) RETURNING id::text",
        tenant,
        f"it-1093:{tenant}",
        json.dumps(BILL_TO),
        f"invented finance note about {name}",
    )
    await admin.execute(
        "UPDATE booking.agency_invoice SET status = 'issued', number = $2, issued_on = DATE '2026-11-02',"
        " due_date = DATE '2026-11-16', issued_at = now(), issued_by = 'staff:it', billing_entity_id = $3::uuid,"
        ' issuer = \'{"legal_entity": "Invented Issuer Ltd"}\'::jsonb WHERE id = $1::uuid',
        invoice,
        f"AAT-2026-9193{tag}",
        entity,
    )
    payment = await admin.fetchval(
        "INSERT INTO booking.agency_payment (invoice_id, amount, currency, method, received_on, document_ref, note,"
        " recorded_by, idempotency_key) VALUES ($1::uuid, 40, 'USD', 'bank_transfer', DATE '2026-11-03', 'slip 9',"
        " 'invented payment note', 'staff:maker', $2) RETURNING id::text",
        invoice,
        f"it-1093-pay:{tenant}",
    )
    await admin.execute(
        "UPDATE booking.agency_payment SET status = 'rejected', rejected_by = 'staff:checker', rejected_at = now(),"
        " reject_reason = 'invented reject reason' WHERE id = $1::uuid",
        payment,
    )
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "INSERT INTO booking.notifications (recipient_type, type, title, message, entity_type, entity_id, data)"
            " VALUES ('staff', 'trip_check.due', $4, $2, 'trip_check', $1::uuid, $3::jsonb)",
            custom,
            f"{name}: check due",
            json.dumps({"agency": name, "reminder_key": "due:1"}),
            f"TC: Ring {name} back is due soon",
        )
        for entity_type, entity_id, label, summary in (
            ("trip_case", case, "TC-1093", "confirmed -> cancelled: invented cancel reason"),
            ("b2b_partner", partner, name, "invented change"),
        ):
            await audit.record(
                conn,
                actor_type="system",
                actor_ref="it-1093",
                action="trip_case.status_changed" if entity_type == "trip_case" else "b2b_partner.updated",
                entity_type=entity_type,
                entity_id=entity_id,
                before={"name": name},
                after={"name": f"{name} Ltd"},
                entity_label=label,
                summary=summary,
            )
    return {"partner": partner, "case": case, "invoice": invoice, "payment": payment, "supplier": sup,
            "check": custom}  # fmt: skip


@pytest_asyncio.fixture
async def agencies(admin: asyncpg.Connection, pool: asyncpg.Pool) -> AsyncIterator[dict[uuid.UUID, dict[str, Any]]]:
    made: dict[uuid.UUID, dict[str, Any]] = {}
    entity = await admin.fetchval(
        "INSERT INTO booking.billing_entity (code, legal_entity) VALUES ($1, 'Invented Issuer Ltd')"
        " ON CONFLICT (code) DO UPDATE SET legal_entity = EXCLUDED.legal_entity RETURNING id::text",
        ENTITY_CODE,
    )
    try:
        for tenant in (ERASED, OTHER, PILOT):
            made[tenant] = await _agency(admin, pool, tenant, entity)
        yield made
    finally:
        await _owner_cleanup(admin, made)


STATE_SQL = """
SELECT p.agency_name, p.status, tc.notes,
       (SELECT title || '|' || coalesce(note, '') FROM booking.trip_case_checks WHERE id = $4::uuid) AS check_row,
       (SELECT array_agg(note ORDER BY seq) FROM booking.trip_case_status_history WHERE trip_case_id = tc.id) AS hist,
       (SELECT title || '|' || coalesce(message, '') || '|' || data::text FROM booking.notifications
         WHERE entity_id = $4::uuid) AS notification,
       (SELECT array_agg(coalesce(before::text, '') || '|' || coalesce(after::text, '') || '|'
                         || coalesce(entity_label, '') || '|' || coalesce(summary, '') ORDER BY entity_type)
          FROM booking.audit_logs WHERE entity_id IN (tc.id, p.id)) AS audit,
       (SELECT bill_to::text || total::text || status || number FROM booking.agency_invoice WHERE id = $3::uuid)
         AS money,
       (SELECT coalesce(staff_note, '') FROM booking.agency_invoice WHERE id = $3::uuid) AS staff_note,
       (SELECT coalesce(note, '') || '|' || reject_reason || '|' || status || '|' || amount::text || '|' || document_ref
          FROM booking.agency_payment WHERE invoice_id = $3::uuid) AS payment,
       (SELECT coalesce(notes, '') || '|' || coalesce(cancel_reason, '') || '|' || status
          FROM booking.supplier_bookings WHERE trip_case_id = tc.id) AS supplier
  FROM booking.b2b_partners p, booking.trip_cases tc
 WHERE p.tenant_id = $1 AND tc.id = $2::uuid
"""


async def _state(conn: asyncpg.Connection, tenant: uuid.UUID, agency: dict[str, Any]) -> dict[str, Any]:
    return dict(await conn.fetchrow(STATE_SQL, tenant, agency["case"], agency["invoice"], agency["check"]))


def _tables(report: dict[str, Any]) -> dict[str, int]:
    return {f"{t['table']}:{t['action']}": t["rows"] for t in report["tables"]}


async def _request(admin: asyncpg.Connection, tenant: uuid.UUID, *, pilot: bool = False, synthetic: bool = True):
    request = uuid.uuid5(uuid.NAMESPACE_URL, f"it-1093:{tenant}")
    await admin.execute(REQUEST_ADD, request, tenant, pilot, synthetic)
    liveness.reset_cache()
    return request


async def _run_queued(
    admin: asyncpg.Connection, pool: asyncpg.Pool, request: uuid.UUID, mode: str, *, apply_enabled: bool = True
):
    # One job per (request, round, mode): pick it by that key, never by time.
    row = await admin.fetchrow(
        "SELECT id::text, payload FROM booking.job WHERE kind = $1 AND payload->>'request_id' = $2"
        " AND payload->>'mode' = $3",
        tenant_erase.KIND,
        str(request),
        mode,
    )
    assert row is not None, f"no {mode} job queued for the request"
    payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
    handler = tenant_erase.build_handler(apply_enabled=apply_enabled, environment="dev")
    return await handler(pool, Job(id=row["id"], kind=tenant_erase.KIND, payload=payload, attempt=1, max_attempts=3))


async def test_dry_run_reports_the_plan_and_writes_nothing(
    pool: asyncpg.Pool, agencies: dict[uuid.UUID, dict[str, Any]]
) -> None:
    async with pool.acquire() as conn:
        before = await _state(conn, ERASED, agencies[ERASED])
        planned = await tenant_erase.plan(conn, ERASED, 1)
        rows = _tables(planned)
        assert rows == {
            "booking.trip_cases:redact": 1,
            "booking.trip_case_status_history:redact": 1,  # the invented status note (the insert row has none)
            "booking.supplier_bookings:redact": 1,
            "booking.trip_case_checks:redact": 1,  # the custom check (title + note)
            "booking.b2b_cancellations:redact": 0,
            "booking.agency_invoice:redact": 1,
            "booking.agency_payment:redact": 1,
            "booking.notifications:redact": 1,
            "booking.b2b_partners:anonymise": 1,
            "booking.audit_logs:redact": 2,
            "booking.b2b_quotes:keep": 0,
            "booking.b2b_holds:keep": 0,
            "booking.b2b_desk_bookings:keep": 0,
            "booking.b2b_cancellations:keep": 0,
            "booking.departure_fees:keep": 0,
            "booking.agency_invoice:keep": 1,
            "booking.agency_invoice_reminder:keep": 0,
            "booking.agency_payment:keep": 1,
        }
        assert all(t["digest"] is None for t in planned["tables"] if t["action"] == "keep")
        assert all(len(t["digest"]) == 32 for t in planned["tables"] if t["action"] != "keep")
        assert await _state(conn, ERASED, agencies[ERASED]) == before
        assert (await tenant_erase.plan(conn, ERASED, 1))["plan_hash"] == planned["plan_hash"]
        assert (await tenant_erase.plan(conn, ERASED, 2))["plan_hash"] != planned["plan_hash"]  # the round binds
        # a session in another time zone computes the same hash (digest lines are UTC ISO 8601)
        async with conn.transaction():
            await conn.execute("SET LOCAL TimeZone = 'Asia/Ho_Chi_Minh'")
            assert (await tenant_erase.plan(conn, ERASED, 1))["plan_hash"] == planned["plan_hash"]
        await conn.execute("UPDATE booking.trip_cases SET notes = 'another invented note' WHERE id = $1::uuid",
                           agencies[ERASED]["case"])  # fmt: skip
        assert (await tenant_erase.plan(conn, ERASED, 1))["plan_hash"] != planned["plan_hash"]


async def test_the_saga_path_anonymises_exactly_the_agency(
    admin: asyncpg.Connection, pool: asyncpg.Pool, agencies: dict[uuid.UUID, dict[str, Any]]
) -> None:
    request = await _request(admin, ERASED)
    async with pool.acquire() as conn:
        other_before = await _state(conn, OTHER, agencies[OTHER])
        pilot_before = await _state(conn, PILOT, agencies[PILOT])
        mine_before = await _state(conn, ERASED, agencies[ERASED])
    clock = {"now": datetime(2026, 11, 13, 9, 0, tzinfo=UTC)}
    schedule = tenant_erase.EraseSchedule(now=lambda: clock["now"])

    async def tick() -> list[str] | None:
        clock["now"] += timedelta(seconds=tenant_erase.SLOT_SECONDS)
        return await schedule(pool)

    assert len(await tick() or []) == 1
    planned = await _run_queued(admin, pool, request, "dry_run")
    assert (planned["outcome"], planned["error_code"]) == ("ok", None)
    assert await tick() == []  # reported: not queued again
    report = await admin.fetchrow(
        "SELECT app, round, status, plan_hash, jsonb_array_length(tables) AS n FROM booking.v_tenant_erasure_report_v1"
        " WHERE request_id = $1 AND mode = 'dry_run'",
        request,
    )
    assert dict(report) == {"app": "booking", "round": 1, "status": "ok", "plan_hash": planned["plan_hash"], "n": 18}
    # the tech lead approves the combined fingerprint: phase apply with the Booking hash, valid 24 hours
    await admin.execute(
        "UPDATE catalog.v_tenant_erasure_v1 SET phase = 'apply', approved_plan_hash = $2,"
        " approval_expires_at = now() + interval '24 hours', updated_at = now() WHERE request_id = $1",
        request,
        planned["plan_hash"],
    )
    assert len(await tick() or []) == 1
    applied = await _run_queued(admin, pool, request, "apply")
    assert (applied["outcome"], applied["plan_hash"]) == ("ok", planned["plan_hash"])
    async with pool.acquire() as conn:
        mine = await _state(conn, ERASED, agencies[ERASED])
        assert mine["agency_name"] == f"Erased agency {str(ERASED)[:8]}" and mine["status"] == "suspended"
        assert mine["notes"] is None and mine["check_row"] == "[redacted]|"
        assert all(n is None for n in mine["hist"])
        assert mine["notification"] == '[redacted]||{"reminder_key": "due:1"}'
        assert mine["supplier"] == "|[redacted]|cancelled"
        assert sorted(mine["audit"]) == sorted([f"{MARKER}|{MARKER}||[redacted]"] * 2)  # the cancel reason is gone
        assert mine["money"] == mine_before["money"]  # bill-to, total, status and number stay (accounting exception)
        assert mine["staff_note"] == ""
        assert mine["payment"] == "|[redacted]|rejected|40.00|slip 9"
        assert await _state(conn, OTHER, agencies[OTHER]) == other_before
        assert await _state(conn, PILOT, agencies[PILOT]) == pilot_before
        assert await tenant_erase.erased_tenants(conn) >= {ERASED}
    erased = await admin.fetchrow(
        "SELECT entity_label, after->>'plan_hash' AS plan_hash, after->>'request_id' AS request_id"
        " FROM booking.audit_logs WHERE action = 'tenant.erased' AND entity_id = $1",
        ERASED,
    )
    assert dict(erased) == {"entity_label": f"Erased agency {str(ERASED)[:8]}", "plan_hash": planned["plan_hash"],
                            "request_id": str(request)}  # fmt: skip
    # verify: the census of the pii: columns finds nothing left, every redact entry plans 0 rows
    await admin.execute(
        "UPDATE catalog.v_tenant_erasure_v1 SET phase = 'verify', updated_at = now() WHERE request_id = $1", request
    )
    assert len(await tick() or []) == 1
    verified = await _run_queued(admin, pool, request, "verify")
    assert (verified["outcome"], verified["error_code"], verified["blockers"]) == ("ok", None, [])
    assert verified["census"]["pii_rows_found"] == 0 and verified["census"]["pii_columns_checked"] >= 20
    modes = [r["mode"] for r in await admin.fetch(
        "SELECT mode FROM booking.v_tenant_erasure_report_v1 WHERE request_id = $1 ORDER BY reported_at", request
    )]  # fmt: skip
    assert modes == ["dry_run", "apply", "verify"]
    # CIS still holds the tenant (CIS erases last): the pull never writes the CIS name back
    pulled = await tenant_pull.run(pool, CLIENT)
    assert pulled["erased_skipped"] == 1
    async with pool.acquire() as conn:
        assert (await _state(conn, ERASED, agencies[ERASED]))["agency_name"] == f"Erased agency {str(ERASED)[:8]}"


async def test_a_plan_that_moved_is_refused_and_writes_nothing(
    admin: asyncpg.Connection, pool: asyncpg.Pool, agencies: dict[uuid.UUID, dict[str, Any]]
) -> None:
    request = await _request(admin, ERASED)
    async with pool.acquire() as conn:
        req = await tenant_erase.read_request(conn, request)
        assert req is not None
        dry = await tenant_erase.run_mode(conn, req, "dry_run", prod=False, apply_enabled=True)
        await conn.execute("UPDATE booking.trip_cases SET notes = 'a newer invented note' WHERE id = $1::uuid",
                           agencies[ERASED]["case"])  # fmt: skip
        before = await _state(conn, ERASED, agencies[ERASED])
        approved = tenant_erase.Request(
            **{**req.__dict__, "phase": "apply", "approved_plan_hash": dry["plan_hash"],
               "approval_expires_at": datetime.now(UTC) + timedelta(hours=1)}
        )  # fmt: skip
        refused = await tenant_erase.run_mode(conn, approved, "apply", prod=False, apply_enabled=True)
        assert (refused["status"], refused["error_code"]) == ("refused", "plan_changed")
        assert refused["plan_hash"] != dry["plan_hash"]
        assert await _state(conn, ERASED, agencies[ERASED]) == before
        # an approved hash with no ok DRY RUN behind it opens nothing
        other = tenant_erase.Request(**{**approved.__dict__, "approved_plan_hash": "f" * 64})
        assert (await tenant_erase.run_mode(conn, other, "apply", prod=False, apply_enabled=True))["error_code"] == (
            "dry_run_missing"
        )
        assert await _state(conn, ERASED, agencies[ERASED]) == before


@pytest.mark.parametrize(
    ("tenant", "change", "mode", "code"),
    [
        (PILOT, {"is_pilot": True, "is_synthetic": False}, "dry_run", "pilot_refused"),
        (OTHER, {"is_synthetic": False}, "apply", "not_synthetic"),
        (ERASED, {"approval_expires_at": datetime(2026, 1, 1, tzinfo=UTC)}, "apply", "approval_expired"),
        (ERASED, {"not_before": datetime(2099, 1, 1, tzinfo=UTC)}, "dry_run", "not_due"),
        (ERASED, {}, "apply_off", "apply_disabled"),
    ],
)
async def test_the_guards_refuse_and_write_nothing(
    admin: asyncpg.Connection,
    pool: asyncpg.Pool,
    agencies: dict[uuid.UUID, dict[str, Any]],
    tenant: uuid.UUID,
    change: dict[str, Any],
    mode: str,
    code: str,
) -> None:
    request = await _request(admin, tenant)
    async with pool.acquire() as conn:
        req = await tenant_erase.read_request(conn, request)
        assert req is not None
        dry = await tenant_erase.run_mode(conn, req, "dry_run", prod=False, apply_enabled=True)
        before = await _state(conn, tenant, agencies[tenant])
        run = "apply" if mode == "apply_off" else mode
        target = tenant_erase.Request(
            **{**req.__dict__, "phase": run, "approved_plan_hash": dry.get("plan_hash"),
               "approval_expires_at": datetime.now(UTC) + timedelta(hours=1), **change}
        )  # fmt: skip
        out = await tenant_erase.run_mode(conn, target, run, prod=False, apply_enabled=mode != "apply_off")
        assert (out["status"], out["error_code"]) == ("refused", code)
        assert await _state(conn, tenant, agencies[tenant]) == before
    logged = await admin.fetchval(
        "SELECT count(*) FROM booking.v_tenant_erasure_report_v1 WHERE request_id = $1 AND status = 'refused'"
        " AND error_code = $2",
        request,
        code,
    )
    assert logged == 1


async def test_a_job_of_an_older_round_is_superseded(
    admin: asyncpg.Connection, pool: asyncpg.Pool, agencies: dict[uuid.UUID, dict[str, Any]]
) -> None:
    request = await _request(admin, ERASED)
    await admin.execute("UPDATE catalog.v_tenant_erasure_v1 SET round = 2, updated_at = now() WHERE request_id = $1",
                        request)  # fmt: skip
    handler = tenant_erase.build_handler(apply_enabled=True, environment="dev")
    payload = {"request_id": str(request), "tenant_id": str(ERASED), "round": 1, "mode": "dry_run"}
    out = await handler(pool, Job(id=str(uuid.uuid4()), kind=tenant_erase.KIND, payload=payload, attempt=1,
                                  max_attempts=3))  # fmt: skip
    assert out["outcome"] == "superseded" and out["round"] == 2
    assert await admin.fetchval("SELECT count(*) FROM booking.tenant_erasure_log WHERE request_id = $1", request) == 0


async def test_every_pii_column_is_cleared_or_kept_by_rule(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        tagged = {r["col"] for r in await conn.fetch(tenant_erase.PII_COLUMNS_SQL, tenant_erase.census_tables())}
    cleared = {f"{t}.{c}" for t, (_, cols) in tenant_erase.CENSUS.items() for c in cols}
    assert tagged - cleared - set(tenant_erase.CENSUS_KEPT) == set(), "a pii: column the erasure does not account for"
    assert cleared <= tagged, "a census column that carries no pii: comment (renamed or retagged?)"
    assert set(tenant_erase.CENSUS_KEPT) <= tagged
