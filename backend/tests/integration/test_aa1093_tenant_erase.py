"""AA-1093 lane L3 on PostgreSQL 15 with the RDS role shape, as role ``booking``: the Booking step of the tenant
offboarding erasure (:mod:`app.b2b.tenant_erase`, bk-057) and the ``bk_tenant_pull`` skip of an erased agency.

``catalog.v_tenant_account_v1`` (CIS 357 + 401 ``is_synthetic``) and ``catalog.v_tenant_erasure_v1`` (the CIS saga's
request feed, contract tenant.erasure.v1) are stood in by plain tables around each test. Three agencies: a synthetic one
(erased), another synthetic one (must stay untouched) and the pilot. Each has a partner row, a confirmed B2B case with a
case note, a trip check note, a status note, a notification, audit rows and a draft agency invoice with its bill-to.
Covers: the DRY RUN counts and writes nothing, its hash is stable and moves with a row; a stale hash is refused and
recorded; the apply anonymises exactly the agency's rows (money rows and the bill-to untouched, the other agency
untouched), writes one ``tenant.erased`` audit row and the applied log row, a second plan finds nothing; the pilot and a
non-synthetic agency outside prod are refused; the saga path (schedule -> DRY RUN job -> report view -> apply job);
the pull keeps the erased name. All names are invented.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
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
    "CREATE TABLE catalog.v_tenant_erasure_v1 (request_id uuid, tenant_id uuid, phase text, approved_plans jsonb);"
    " GRANT SELECT ON catalog.v_tenant_erasure_v1 TO booking;"
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


async def _owner_cleanup(admin: asyncpg.Connection, agencies: dict[uuid.UUID, dict[str, Any]]) -> None:
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE bk_owner")
        tenants = list(agencies)
        cases = [a["case"] for a in agencies.values()]
        entities = cases + [a["partner"] for a in agencies.values()] + [str(t) for t in tenants]
        await admin.execute("DELETE FROM booking.job WHERE kind = $1", tenant_erase.KIND)
        await admin.execute("DELETE FROM booking.tenant_erasure_log WHERE tenant_id = ANY($1::uuid[])", tenants)
        await admin.execute("DELETE FROM booking.notifications WHERE entity_id = ANY($1::uuid[])", cases)
        await admin.execute(
            "DELETE FROM booking.audit_logs WHERE entity_id = ANY($1::uuid[]) OR entity_id IN"
            " (SELECT id FROM booking.trip_case_checks WHERE trip_case_id = ANY($2::uuid[]))",
            entities,
            cases,
        )
        await admin.execute("DELETE FROM booking.agency_invoice WHERE party_tenant_id = ANY($1::uuid[])", tenants)
        await admin.execute("DELETE FROM booking.trip_case_status_history WHERE trip_case_id = ANY($1::uuid[])", cases)
        await admin.execute(
            "DELETE FROM booking.audit_logs WHERE entity_id IN"
            " (SELECT id FROM booking.supplier_bookings WHERE trip_case_id = ANY($1::uuid[]))",
            cases,
        )
        await admin.execute("DELETE FROM booking.supplier_bookings WHERE trip_case_id = ANY($1::uuid[])", cases)
        sups = [a["supplier"] for a in agencies.values() if "supplier" in a]
        hotels, suppliers = [x["hotel"] for x in sups], [x["supplier"] for x in sups]
        await admin.execute("DELETE FROM booking.hotel_room_types WHERE hotel_id = ANY($1::uuid[])", hotels)
        await admin.execute("DELETE FROM booking.services WHERE supplier_id = ANY($1::uuid[])", suppliers)
        await admin.execute("DELETE FROM booking.hotels WHERE id = ANY($1::uuid[])", hotels)
        await admin.execute("DELETE FROM booking.suppliers WHERE id = ANY($1::uuid[])", suppliers)
        await admin.execute("DELETE FROM booking.trip_cases WHERE id = ANY($1::uuid[])", cases)
        await admin.execute("DELETE FROM booking.b2b_partners WHERE tenant_id = ANY($1::uuid[])", tenants)


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    for sql in DROPS:
        await conn.execute(sql)
    await conn.execute(ACCOUNT_STUB)
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


async def _agency(admin: asyncpg.Connection, pool: asyncpg.Pool, tenant: uuid.UUID) -> dict[str, Any]:
    name = NAMES[tenant]
    partner = await admin.fetchval(
        "INSERT INTO booking.b2b_partners (tenant_id, agency_name, status, billing_currency, m2m_client_id,"
        " projected_at) VALUES ($1, $2, 'suspended', 'USD', $3, now()) RETURNING id::text",
        tenant,
        name,
        CLIENT,
    )
    case = (await ops.b2b_case(admin, tag=f"1093-{str(tenant)[-2:]}", status="confirmed"))["id"]
    await admin.execute(
        "UPDATE booking.trip_cases SET party_tenant_id = $2, notes = $3 WHERE id = $1::uuid",
        case,
        tenant,
        f"Call {name} about the invented lead traveller",
    )
    await admin.execute(
        "UPDATE booking.trip_case_checks SET note = 'invented check note' WHERE trip_case_id = $1::uuid"
        " AND code = 'pre_trip_recheck'",
        case,
    )
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE bk_owner")
        await admin.execute(
            "INSERT INTO booking.trip_case_status_history (trip_case_id, from_status, to_status, note)"
            " VALUES ($1::uuid, 'confirmed', 'confirmed', 'invented status note')",
            case,
        )
    sup = await ops.supply(admin, f"Erase {str(tenant)[-2:]}")
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
        " subtotal, total, source, created_by, idempotency_key, bill_to)"
        " VALUES ($1, 'settlement', DATE '2026-10-01', DATE '2026-11-01', 'USD', 100, 100, 'job', 'job:test', $2,"
        " $3::jsonb) RETURNING id::text",
        tenant,
        f"it-1093:{tenant}",
        json.dumps(BILL_TO),
    )
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "INSERT INTO booking.notifications (recipient_type, type, title, message, entity_type, entity_id, data)"
            " VALUES ('staff', 'trip_check.due', 'Trip check due', $2, 'trip_case', $1::uuid, $3::jsonb)",
            case,
            f"{name}: check due",
            json.dumps({"agency": name, "reminder_key": "due:1"}),
        )
        for entity_type, entity_id, label in (("trip_case", case, "TC-1093"), ("b2b_partner", partner, name)):
            await audit.record(
                conn,
                actor_type="system",
                actor_ref="it-1093",
                action=f"{entity_type}.updated",
                entity_type=entity_type,
                entity_id=entity_id,
                before={"name": name},
                after={"name": f"{name} Ltd"},
                entity_label=label,
                summary="invented change",
            )
    return {"partner": partner, "case": case, "invoice": invoice, "supplier": sup}


@pytest_asyncio.fixture
async def agencies(admin: asyncpg.Connection, pool: asyncpg.Pool) -> AsyncIterator[dict[uuid.UUID, dict[str, Any]]]:
    made: dict[uuid.UUID, dict[str, Any]] = {}
    try:
        for tenant in (ERASED, OTHER, PILOT):
            made[tenant] = await _agency(admin, pool, tenant)
        yield made
    finally:
        await _owner_cleanup(admin, made)


STATE_SQL = """
SELECT p.agency_name, p.status, tc.notes,
       (SELECT note FROM booking.trip_case_checks
         WHERE trip_case_id = tc.id AND code = 'pre_trip_recheck') AS check_note,
       (SELECT array_agg(note ORDER BY seq) FROM booking.trip_case_status_history WHERE trip_case_id = tc.id) AS hist,
       (SELECT message FROM booking.notifications WHERE entity_id = tc.id) AS message,
       (SELECT data::text FROM booking.notifications WHERE entity_id = tc.id) AS data,
       (SELECT array_agg(coalesce(before::text, '') || '|' || coalesce(after::text, '') || '|'
                         || coalesce(entity_label, '') ORDER BY entity_type)
          FROM booking.audit_logs WHERE entity_id IN (tc.id, p.id)) AS audit,
       (SELECT bill_to::text || total::text || status FROM booking.agency_invoice WHERE id = $3::uuid) AS money,
       (SELECT coalesce(notes, '') || '|' || coalesce(cancel_reason, '') || '|' || status
          FROM booking.supplier_bookings WHERE trip_case_id = tc.id) AS supplier
  FROM booking.b2b_partners p, booking.trip_cases tc
 WHERE p.tenant_id = $1 AND tc.id = $2::uuid
"""


async def _state(conn: asyncpg.Connection, tenant: uuid.UUID, agency: dict[str, Any]) -> dict[str, Any]:
    return dict(await conn.fetchrow(STATE_SQL, tenant, agency["case"], agency["invoice"]))


async def test_dry_run_counts_writes_nothing_and_its_hash_is_stable(
    pool: asyncpg.Pool, agencies: dict[uuid.UUID, dict[str, Any]]
) -> None:
    async with pool.acquire() as conn:
        before = await _state(conn, ERASED, agencies[ERASED])
        planned = await tenant_erase.plan(conn, ERASED)
        steps = {s["step"]: s["count"] for s in planned["steps"]}
        assert steps == {
            "trip_cases": 1,
            "trip_case_status_history": 1,  # the invented status note (the case's own insert row has none)
            "supplier_bookings": 1,
            "trip_case_checks": 1,
            "notifications": 1,
            "b2b_partners": 1,
            "audit_logs": 2,
        }
        assert planned["kept"]["agency_invoice"] == 1
        assert await _state(conn, ERASED, agencies[ERASED]) == before
        assert (await tenant_erase.plan(conn, ERASED))["plan_hash"] == planned["plan_hash"]
        # a changed row moves the hash
        await conn.execute("UPDATE booking.trip_cases SET notes = 'another invented note' WHERE id = $1::uuid",
                           agencies[ERASED]["case"])  # fmt: skip
        assert (await tenant_erase.plan(conn, ERASED))["plan_hash"] != planned["plan_hash"]
        assert await tenant_erase.guard_result(conn, ERASED, prod=False) == {"apply_allowed": True, "refused": None}


async def test_a_stale_hash_is_refused_and_recorded(
    pool: asyncpg.Pool, agencies: dict[uuid.UUID, dict[str, Any]]
) -> None:
    request = uuid.uuid4()
    async with pool.acquire() as conn:
        before = await _state(conn, ERASED, agencies[ERASED])
        with pytest.raises(tenant_erase.PlanChanged):
            await tenant_erase.apply(conn, ERASED, "f" * 64, request_id=request, prod=False, source="cli")
        assert await _state(conn, ERASED, agencies[ERASED]) == before
        row = await conn.fetchrow(
            "SELECT status, detail, counts FROM booking.tenant_erasure_log WHERE request_id = $1 AND mode = 'apply'",
            request,
        )
        assert (row["status"], row["detail"]) == ("refused", "plan_changed")


async def test_apply_anonymises_exactly_the_agency(
    admin: asyncpg.Connection, pool: asyncpg.Pool, agencies: dict[uuid.UUID, dict[str, Any]]
) -> None:
    request = uuid.uuid4()
    async with pool.acquire() as conn:
        other_before = await _state(conn, OTHER, agencies[OTHER])
        pilot_before = await _state(conn, PILOT, agencies[PILOT])
        mine_before = await _state(conn, ERASED, agencies[ERASED])
        planned = await tenant_erase.plan(conn, ERASED)
        done = await tenant_erase.apply(conn, ERASED, planned["plan_hash"], request_id=request, prod=False,
                                        source="cli")  # fmt: skip
        assert done["outcome"] == "applied" and done["plan_hash"] == planned["plan_hash"]
        mine = await _state(conn, ERASED, agencies[ERASED])
        assert mine["agency_name"] == f"Erased agency {str(ERASED)[:8]}" and mine["status"] == "suspended"
        assert mine["notes"] is None and mine["check_note"] is None and mine["message"] is None
        assert all(n is None for n in mine["hist"])
        assert json.loads(mine["data"]) == {"reminder_key": "due:1"}
        assert mine["supplier"] == "|Erased|cancelled"
        marker = '{"redacted": "tenant_erased"}'
        assert sorted(mine["audit"]) == sorted([f"{marker}|{marker}|", f"{marker}|{marker}|TC-1093"])
        assert mine["money"] == mine_before["money"]  # the bill-to snapshot and the money stay (accounting exception)
        assert await _state(conn, OTHER, agencies[OTHER]) == other_before
        assert await _state(conn, PILOT, agencies[PILOT]) == pilot_before
        erased = await admin.fetchrow(
            "SELECT entity_label, after->>'plan_hash' AS plan_hash, after->>'request_id' AS request_id"
            " FROM booking.audit_logs WHERE action = 'tenant.erased' AND entity_id = $1",
            ERASED,
        )
        assert dict(erased) == {"entity_label": f"Erased agency {str(ERASED)[:8]}",
                                "plan_hash": planned["plan_hash"], "request_id": str(request)}  # fmt: skip
        log = await conn.fetchrow(
            "SELECT status, source, finished_at FROM booking.tenant_erasure_log"
            " WHERE request_id = $1 AND mode = 'apply'",
            request,
        )
        assert (log["status"], log["source"]) == ("applied", "cli") and log["finished_at"] is not None
        again = await tenant_erase.plan(conn, ERASED)
        assert again["rows"] == 0
        repeat = await tenant_erase.apply(conn, ERASED, again["plan_hash"], request_id=request, prod=False,
                                          source="cli")  # fmt: skip
        assert repeat["outcome"] == "already_applied"
        assert await tenant_erase.erased_tenants(conn) >= {ERASED}


async def test_the_pilot_and_a_real_agency_outside_prod_are_refused(
    admin: asyncpg.Connection, pool: asyncpg.Pool, agencies: dict[uuid.UUID, dict[str, Any]]
) -> None:
    await admin.execute("UPDATE catalog.v_tenant_account_v1 SET is_synthetic = false WHERE tenant_id = $1", OTHER)
    async with pool.acquire() as conn:
        pilot_before = await _state(conn, PILOT, agencies[PILOT])
        other_before = await _state(conn, OTHER, agencies[OTHER])
        for tenant, code, prod in ((PILOT, "pilot_tenant", True), (OTHER, "not_synthetic", False)):
            planned = await tenant_erase.plan(conn, tenant)
            with pytest.raises(tenant_erase.Refused) as info:
                await tenant_erase.apply(conn, tenant, planned["plan_hash"], request_id=uuid.uuid4(), prod=prod,
                                         source="cli")  # fmt: skip
            assert info.value.code == code
        assert await _state(conn, PILOT, agencies[PILOT]) == pilot_before
        assert await _state(conn, OTHER, agencies[OTHER]) == other_before
        # in prod a real agency may be erased (the pilot never): the guard passes
        assert (await tenant_erase.guard_result(conn, OTHER, prod=True))["apply_allowed"] is True
        # an unknown tenant or an unreadable account view: refused (fail closed)
        assert (await tenant_erase.guard_result(conn, uuid.uuid4(), prod=True))["refused"] == "tenant_unknown"
    await admin.execute(DROPS[0])
    liveness.reset_cache()
    async with pool.acquire() as conn:
        assert (await tenant_erase.guard_result(conn, ERASED, prod=False))["refused"] == "account_unknown"


async def test_the_saga_path_and_the_pull_skip(
    admin: asyncpg.Connection, pool: asyncpg.Pool, agencies: dict[uuid.UUID, dict[str, Any]]
) -> None:
    request = uuid.uuid4()
    await admin.execute(REQUEST_STUB)
    await admin.execute("INSERT INTO catalog.v_tenant_erasure_v1 VALUES ($1, $2, 'plan', '{}'::jsonb)", request, ERASED)
    liveness.reset_cache()
    handler = tenant_erase.build_handler(apply_enabled=True, environment="dev")

    async def run_queued(mode: str) -> dict[str, Any]:
        row = await admin.fetchrow(
            "SELECT id::text, payload FROM booking.job WHERE kind = $1 AND payload->>'mode' = $2",
            tenant_erase.KIND,
            mode,
        )
        payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
        return await handler(
            pool, Job(id=row["id"], kind=tenant_erase.KIND, payload=payload, attempt=1, max_attempts=3)
        )

    queued = await tenant_erase.EraseSchedule(apply_enabled=True)(pool)
    assert queued and len(queued) == 1
    planned = await run_queued("dry_run")
    assert planned["outcome"] == "planned" and planned["apply_allowed"] is True
    report = await admin.fetchrow(
        "SELECT plan_hash, status, counts FROM booking.v_tenant_erasure_report_v1 WHERE request_id = $1"
        " AND mode = 'dry_run'",
        request,
    )
    assert (report["plan_hash"], report["status"]) == (planned["plan_hash"], "planned")
    # the saga approves the combined fingerprint: phase apply with the Booking hash
    await admin.execute(
        "UPDATE catalog.v_tenant_erasure_v1 SET phase = 'apply', approved_plans = $2::jsonb WHERE request_id = $1",
        request,
        json.dumps({"booking": planned["plan_hash"]}),
    )
    assert await tenant_erase.EraseSchedule(apply_enabled=False)(pool) == []  # the kill switch off: no apply job
    assert len(await tenant_erase.EraseSchedule(apply_enabled=True)(pool)) == 1
    applied = await run_queued("apply")
    assert applied["outcome"] == "applied"
    # CIS still holds the tenant (CIS erases last): the pull never writes the CIS name back
    pulled = await tenant_pull.run(pool, CLIENT)
    assert pulled["erased_skipped"] == 1
    async with pool.acquire() as conn:
        assert (await _state(conn, ERASED, agencies[ERASED]))["agency_name"] == f"Erased agency {str(ERASED)[:8]}"
