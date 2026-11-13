"""``bk_tenant_erase`` without a database (AA-1093 lane L3, contract tenant.erasure.v1): the plan hash, the SQL shape,
the guards, the reports of every mode, the job and the schedule on a scripted connection. The same rules on
PostgreSQL: tests/integration/test_aa1093_tenant_erase.py."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.b2b import tenant_erase as te
from app.catalog import liveness
from app.jobs.kinds import EXPECTED_SECONDS
from app.jobs.queue import Job
from tests.unit.fakes import VIEW_READY_FRAGMENT, ScriptedConn

TENANT = uuid.UUID("00000000-0000-4000-8000-000000109311")
REQUEST = uuid.UUID("00000000-0000-4000-8000-000000109312")
NOW = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)
DIGEST = "d41d8cd98f00b204e9800998ecf8427e"
OPENING = "00000000-0000-4000-8000-0000001093aa"


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    liveness.reset_cache()


def tables(rows: int = 2, digest: str = DIGEST) -> list[dict[str, Any]]:
    out = [{"table": e.table, "action": e.action, "rows": rows, "digest": digest} for e in te.ENTRIES]
    return out + [{"table": t, "action": "keep", "rows": 1, "digest": None} for t, _ in te.KEPT]


def request(**changes: Any) -> te.Request:
    base: dict[str, Any] = {
        "request_id": REQUEST,
        "tenant_id": TENANT,
        "round": 1,
        "phase": "dry_run",
        "not_before": NOW - timedelta(days=1),
        "approval_expires_at": NOW + timedelta(hours=2),
        "is_synthetic": True,
    }
    return te.Request(**{**base, **changes})


def scripted(*, census_found: int = 0, pii: list[str] | None = None) -> ScriptedConn:
    c = ScriptedConn()
    c.on(VIEW_READY_FRAGMENT, True)
    c.on("FROM catalog.v_tenant_account_v1", {"is_pilot": False, "is_synthetic": True})
    c.on("SELECT count(*)::int AS n", {"n": 2, "digest": DIGEST})
    c.on("SELECT count(*)::int FROM booking.", 1)
    c.on("to_regclass('booking.tenant_erasure_log')", True)
    c.on("FROM booking.v_schema_versions", True)
    c.on("INSERT INTO booking.tenant_erasure_log", {"report_id": "r1", "reported_at": NOW})
    c.on("mode = 'dry_run' AND status = 'ok' AND plan_hash = $3", OPENING)
    c.on("FROM pg_attribute a", [{"col": col} for col in (pii if pii is not None else cleared_columns())])
    c.on("AS pii_rows FROM", census_found)  # the census counts (one OR-ed predicate group per table)
    return c


def cleared_columns() -> list[str]:
    return sorted(f"{t}.{col}" for t, (_, cols) in te.CENSUS.items() for col in cols)


def reports(c: ScriptedConn) -> list[tuple[Any, ...]]:
    return c.sql("INSERT INTO booking.tenant_erasure_log")


def test_plan_hash_follows_the_contract() -> None:
    t = tables()
    lines = sorted(f"{x['table']}|{x['action']}|{x['rows']}|{x['digest'] or ''}" for x in t)
    expected = hashlib.sha256("\n".join([f"tenant.erasure.v1|{TENANT}|1", *lines]).encode()).hexdigest()
    assert te.plan_hash(TENANT, 1, t) == expected and te.HASH_RE.match(expected)
    assert te.plan_hash(TENANT, 1, list(reversed(t))) == expected  # order of the entries does not matter
    assert te.plan_hash(TENANT, 2, t) != expected  # the round binds
    assert te.plan_hash(uuid.UUID(int=7), 1, t) != expected
    assert te.plan_hash(TENANT, 1, tables(rows=3)) != expected
    assert te.plan_hash(TENANT, 1, tables(digest="0" * 32)) != expected


def test_sql_shape() -> None:
    sql = te.digest_sql(te.ENTRIES[0])
    assert "AT TIME ZONE 'UTC'" in sql and 'COLLATE "C"' in sql and "E'\\n'" in sql
    assert "updated_at::text" not in sql
    assert "''" in te.digest_sql(next(e for e in te.ENTRIES if e.version is None))
    assert te.ENTRIES[-1].table == "booking.audit_logs"  # the audit redaction runs after every other entry
    assert {e.action for e in te.ENTRIES} <= {"redact", "anonymise"}
    for entry in te.ENTRIES:
        assert "$1" in entry.where and entry.update.startswith(f"UPDATE {entry.table} SET ")
    # notifications and audit rows follow exactly the bk-057 scope function
    for table in ("booking.notifications", "booking.audit_logs"):
        assert "booking.tenant_erase_scope($1)" in next(e for e in te.ENTRIES if e.table == table).where
    audit_sets = next(e for e in te.ENTRIES if e.table == "booking.audit_logs").sets
    assert "summary" in audit_sets and "entity_label = NULL" in audit_sets
    kept = {t for t, _ in te.KEPT}
    assert {"booking.agency_invoice", "booking.agency_payment", "booking.b2b_desk_bookings"} <= kept
    assert set(te.CENSUS) <= set(te.census_tables())
    assert te.census_sql("booking.b2b_partners") is None
    assert "(note IS NOT NULL) OR" in (te.census_sql("booking.agency_payment") or "")


def test_names_and_environment() -> None:
    assert te.erased_name(TENANT) == "Erased agency 00000000"
    assert te.cli_request_id(TENANT) == te.cli_request_id(TENANT) != te.cli_request_id(uuid.UUID(int=3))
    assert te.is_prod("prod") and te.is_prod(" PROD ")
    assert not te.is_prod(None) and not te.is_prod("dev") and not te.is_prod("")  # fail closed: synthetic only
    assert te.KIND in EXPECTED_SECONDS
    assert te._touched("UPDATE 3") == 3 and te._touched("OK") is None


@pytest.mark.parametrize(
    ("changes", "mode", "prod", "enabled", "code"),
    [
        ({"not_before": NOW + timedelta(seconds=1)}, "dry_run", False, True, "not_due"),
        ({"is_pilot": True}, "dry_run", True, True, "pilot_refused"),
        ({"is_pilot": True}, "verify", True, True, "pilot_refused"),
        ({}, "apply", False, False, "apply_disabled"),
        ({"is_synthetic": False}, "apply", False, True, "not_synthetic"),
        ({"approved_plan_hash": None}, "apply", False, True, "approval_missing"),
        ({"approved_plan_hash": "a" * 64, "approval_expires_at": NOW}, "apply", False, True, "approval_expired"),
        ({"approved_plan_hash": "a" * 64, "approval_expires_at": None}, "apply", False, True, "approval_expired"),
        ({"is_synthetic": False}, "dry_run", False, True, None),  # a DRY RUN of a real agency is allowed
        ({"is_synthetic": False, "approved_plan_hash": "a" * 64}, "apply", True, True, None),  # prod: real agency
        ({"approved_plan_hash": "a" * 64, "approval_expires_at": None, "source": "cli"}, "apply", False, True, None),
    ],
)
def test_guards(changes: dict[str, Any], mode: str, prod: bool, enabled: bool, code: str | None) -> None:
    req = request(**changes)
    if code is None:
        te.guard(req, mode, prod=prod, apply_enabled=enabled, now=NOW)
        return
    with pytest.raises(te.Refused) as info:
        te.guard(req, mode, prod=prod, apply_enabled=enabled, now=NOW)
    assert info.value.code == code


@pytest.mark.asyncio
async def test_dry_run_reports_the_plan_and_writes_nothing() -> None:
    c = scripted()
    out = await te.run_mode(c, request(), "dry_run", prod=False, apply_enabled=False, now=NOW)  # type: ignore[arg-type]
    assert (out["status"], out["error_code"], out["mode"]) == ("ok", None, "dry_run")
    assert out["plan_hash"] == te.plan_hash(TENANT, 1, tables())
    assert not [e for e in te.ENTRIES if c.sql(e.update)]
    (row,) = reports(c)
    assert row[3:7] == ("dry_run", "ok", None, out["plan_hash"]) and row[10] == "cis"
    assert len(json.loads(row[7])) == len(te.ENTRIES) + len(te.KEPT)


@pytest.mark.asyncio
async def test_a_refusal_is_a_report_and_writes_nothing() -> None:
    c = scripted()
    out = await te.run_mode(c, request(is_pilot=True), "dry_run", prod=True, apply_enabled=True, now=NOW)  # type: ignore[arg-type]
    assert (out["status"], out["error_code"]) == ("refused", "pilot_refused")
    assert reports(c)[-1][3:6] == ("dry_run", "refused", "pilot_refused")
    assert not c.sql("SELECT count(*)::int AS n")  # no plan computed


@pytest.mark.asyncio
async def test_apply_opens_the_erasure_with_the_dry_run_report_and_runs_every_entry() -> None:
    c = scripted()
    approved = te.plan_hash(TENANT, 1, tables())
    out = await te.run_mode(c, request(phase="apply", approved_plan_hash=approved), "apply", prod=False,  # type: ignore[arg-type]
                            apply_enabled=True, now=NOW)  # fmt: skip
    assert (out["status"], out["plan_hash"]) == ("ok", approved)
    assert c.sql("set_config($1, $2, true)") == [(te.ERASE_SETTING, OPENING)]
    assert all(len(c.sql(e.update)) == 1 for e in te.ENTRIES)
    (audit_row,) = c.sql("INSERT INTO booking.audit_logs")
    assert audit_row[3] == "tenant.erased" and audit_row[4] == "tenant"
    assert reports(c)[-1][3:7] == ("apply", "ok", None, approved)


@pytest.mark.asyncio
async def test_apply_refuses_a_moved_plan_or_a_missing_dry_run() -> None:
    c = scripted()
    out = await te.run_mode(c, request(phase="apply", approved_plan_hash="f" * 64), "apply", prod=False,  # type: ignore[arg-type]
                            apply_enabled=True, now=NOW)  # fmt: skip
    assert (out["status"], out["error_code"]) == ("refused", "plan_changed")
    assert not [e for e in te.ENTRIES if c.sql(e.update)] and not c.sql("set_config($1, $2, true)")
    missing = scripted().on("mode = 'dry_run' AND status = 'ok' AND plan_hash = $3", None)
    out = await te.run_mode(missing, request(phase="apply", approved_plan_hash="f" * 64), "apply", prod=False,  # type: ignore[arg-type]
                            apply_enabled=True, now=NOW)  # fmt: skip
    assert out["error_code"] == "dry_run_missing"


@pytest.mark.asyncio
async def test_apply_is_refused_when_the_account_view_says_pilot_and_not_repeated() -> None:
    c = scripted().on("FROM catalog.v_tenant_account_v1", {"is_pilot": True, "is_synthetic": True})
    req = request(phase="apply", approved_plan_hash="a" * 64)
    out = await te.run_mode(c, req, "apply", prod=False, apply_enabled=True, now=NOW)  # type: ignore[arg-type]
    assert out["error_code"] == "pilot_refused"
    done = scripted().on("mode = 'apply' AND status = 'ok' LIMIT 1", "r0")
    out = await te.run_mode(done, req, "apply", prod=False, apply_enabled=True, now=NOW)  # type: ignore[arg-type]
    assert out["outcome"] == "already_applied" and not reports(done)


@pytest.mark.asyncio
async def test_a_database_error_is_a_failed_report() -> None:
    import asyncpg

    c = scripted().on("SELECT count(*)::int AS n", asyncpg.PostgresError("boom"))
    out = await te.run_mode(c, request(), "dry_run", prod=False, apply_enabled=True, now=NOW)  # type: ignore[arg-type]
    assert (out["status"], out["error_code"]) == ("failed", "internal_error")


@pytest.mark.asyncio
async def test_verify_reports_the_census() -> None:
    clean = scripted().on("SELECT count(*)::int AS n", {"n": 0, "digest": DIGEST})
    out = await te.run_mode(clean, request(phase="verify"), "verify", prod=False, apply_enabled=False, now=NOW)  # type: ignore[arg-type]
    assert (out["status"], out["error_code"], out["plan_hash"]) == ("ok", None, None)
    assert out["census"] == {"pii_columns_checked": len(cleared_columns()), "pii_rows_found": 0}
    left = scripted(census_found=1).on("SELECT count(*)::int AS n", {"n": 0, "digest": DIGEST})
    out = await te.run_mode(left, request(phase="verify"), "verify", prod=False, apply_enabled=False, now=NOW)  # type: ignore[arg-type]
    assert (out["status"], out["error_code"]) == ("failed", "census_not_zero")
    rows_left = scripted()  # every redact entry still plans 2 rows
    out = await te.run_mode(rows_left, request(phase="verify"), "verify", prod=False, apply_enabled=False, now=NOW)  # type: ignore[arg-type]
    assert out["error_code"] == "census_not_zero"
    unlisted = scripted(pii=[*cleared_columns(), "booking.trip_cases.new_free_text"])
    unlisted.on("SELECT count(*)::int AS n", {"n": 0, "digest": DIGEST})
    out = await te.run_mode(unlisted, request(phase="verify"), "verify", prod=False, apply_enabled=False, now=NOW)  # type: ignore[arg-type]
    assert (out["error_code"], out["blockers"]) == ("census_incomplete", [{"code": "pii_column_unlisted", "count": 1}])


def view_row(**changes: Any) -> dict[str, Any]:
    r = request(**changes)
    return {
        "request_id": r.request_id, "tenant_id": r.tenant_id, "phase": r.phase, "round": r.round,
        "step_status": r.step_status, "not_before": r.not_before, "approved_plan_hash": r.approved_plan_hash,
        "approval_expires_at": r.approval_expires_at, "is_pilot": r.is_pilot, "is_synthetic": r.is_synthetic,
    }  # fmt: skip


@pytest.mark.asyncio
async def test_the_job_rereads_the_request() -> None:
    def job(**payload: Any) -> Job:
        body = {"request_id": str(REQUEST), "tenant_id": str(TENANT), "round": 1, "mode": "dry_run", **payload}
        return Job("j1", te.KIND, body, 1, 3)

    handler = te.build_handler(apply_enabled=False, environment="dev")
    c = scripted().on("AND request_id = $1 ORDER BY updated_at DESC", view_row())
    out = await handler(c, job())  # type: ignore[arg-type]
    assert out["outcome"] == "ok" and "tables" not in out and reports(c)[-1][-1] == "j1"
    moved = scripted().on("AND request_id = $1 ORDER BY updated_at DESC", view_row(round=2))
    assert (await handler(moved, job()))["outcome"] == "superseded" and not reports(moved)  # type: ignore[arg-type]
    gone = scripted().on("AND request_id = $1 ORDER BY updated_at DESC", None)
    assert (await handler(gone, job()))["reason"] == "request_not_found"  # type: ignore[arg-type]
    assert (await handler(c, job(mode="other")))["outcome"] == "invalid_payload"  # type: ignore[arg-type]
    assert (await handler(c, Job("j2", te.KIND, {"tenant_id": "x"}, 1, 3)))["outcome"] == "invalid_payload"  # type: ignore[arg-type]
    no_table = scripted().on("to_regclass('booking.tenant_erasure_log')", False)
    assert (await handler(no_table, job()))["outcome"] == "skipped"  # type: ignore[arg-type]


def requests_conn(rows: list[dict[str, Any]], reported: list[dict[str, Any]] | None = None) -> ScriptedConn:
    c = scripted()
    c.on("SELECT DISTINCT ON (request_id)", rows)
    c.on("SELECT DISTINCT request_id, round, mode", reported or [])
    c.on("INSERT INTO booking.job", lambda *a: f"job-{a[2]}")
    return c


@pytest.mark.asyncio
async def test_schedule_queues_one_job_per_round_and_phase_without_a_report() -> None:
    other, third = uuid.UUID(int=5), uuid.UUID(int=8)
    rows = [
        view_row(),
        view_row(request_id=other, tenant_id=uuid.UUID(int=6), phase="apply", round=2, approved_plan_hash="c" * 64),
        view_row(request_id=third, tenant_id=uuid.UUID(int=9), phase="apply", approved_plan_hash=None),
    ]
    clock = {"now": NOW}
    c = requests_conn(rows)
    hook = te.EraseSchedule(now=lambda: clock["now"])
    queued = await hook(c)  # type: ignore[arg-type]
    assert queued is not None and len(queued) == 2  # no apply without an approved hash
    keys = [args[2] for args in c.sql("INSERT INTO booking.job")]
    assert keys == [f"{te.KIND}:{REQUEST}:1:dry_run", f"{te.KIND}:{other}:2:apply"]
    payloads = [json.loads(args[1]) for args in c.sql("INSERT INTO booking.job")]
    assert payloads[1] == {"request_id": str(other), "tenant_id": str(uuid.UUID(int=6)), "round": 2, "mode": "apply"}
    assert await hook(c) is None  # once per slot
    clock["now"] = NOW + timedelta(seconds=te.SLOT_SECONDS)
    done = requests_conn(rows, [{"request_id": REQUEST, "round": 1, "mode": "dry_run"},
                                {"request_id": other, "round": 2, "mode": "apply"}])  # fmt: skip
    assert await hook(done) == []  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_schedule_without_the_view_queues_nothing() -> None:
    c = requests_conn([]).on(VIEW_READY_FRAGMENT, False)
    assert await te.EraseSchedule(now=lambda: NOW)(c) is None  # type: ignore[arg-type]
    assert c.sql("INSERT INTO booking.job") == []


@pytest.mark.asyncio
async def test_cli_request_fails_closed() -> None:
    known = await te.cli_request(scripted(), TENANT, 1, "apply", "a" * 64)  # type: ignore[arg-type]
    assert (known.source, known.is_pilot, known.is_synthetic) == ("cli", False, True)
    assert known.request_id == te.cli_request_id(TENANT)
    unknown = scripted().on("FROM catalog.v_tenant_account_v1", None)
    assert (await te.cli_request(unknown, TENANT, 1, "apply", "a" * 64)).is_pilot is True  # type: ignore[arg-type]
    assert (await te.cli_request(unknown, TENANT, 1, "dry_run", None)).is_pilot is False  # type: ignore[arg-type]
    pilot = scripted().on("FROM catalog.v_tenant_account_v1", {"is_pilot": True, "is_synthetic": False})
    assert (await te.cli_request(pilot, TENANT, 1, "dry_run", None)).is_pilot is True  # type: ignore[arg-type]
