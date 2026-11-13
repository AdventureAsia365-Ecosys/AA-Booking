"""``bk_tenant_erase`` without a database (AA-1093 lane L3): the plan hash, the guards (pilot, synthetic outside prod,
unknown tenant, unreadable view), the refused apply writing nothing, the job modes and the schedule on a scripted
connection. The same rules on PostgreSQL: tests/integration/test_aa1093_tenant_erase.py."""

from __future__ import annotations

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
FP = "d41d8cd98f00b204e9800998ecf8427e"


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    liveness.reset_cache()


def steps(count: int = 1, fingerprint: str = FP) -> list[dict[str, Any]]:
    return [{"step": s.name, "action": s.action, "count": count, "fingerprint": fingerprint} for s in te.STEPS]


def scripted(*, pilot: bool = False, synthetic: bool = True, account: bool = True, ready: bool = True) -> ScriptedConn:
    c = ScriptedConn()
    c.on(VIEW_READY_FRAGMENT, ready)
    c.on("FROM catalog.v_tenant_account_v1", {"is_pilot": pilot, "is_synthetic": synthetic} if account else None)
    c.on("SELECT count(*)::int AS n", {"n": 2, "fp": FP})
    c.on("SELECT count(*) FROM booking.", 1)
    c.on("to_regclass('booking.tenant_erasure_log')", True)
    c.on("FROM booking.v_schema_versions", True)
    return c


def test_plan_hash_is_stable_and_moves_with_any_input() -> None:
    base = te.plan_hash(TENANT, steps())
    assert base == te.plan_hash(TENANT, steps()) and te.HASH_RE.match(base)
    assert te.plan_hash(TENANT, steps(count=2)) != base
    assert te.plan_hash(TENANT, steps(fingerprint="0" * 32)) != base
    assert te.plan_hash(uuid.UUID(int=7), steps()) != base
    # the key order of a step does not matter (canonical JSON)
    reordered = [dict(reversed(list(s.items()))) for s in steps()]
    assert te.plan_hash(TENANT, reordered) == base


def test_fingerprint_reads_no_session_time_zone_and_steps_are_complete() -> None:
    sql = te.fingerprint_sql(te.STEPS[0])
    assert "extract(epoch FROM ts)" in sql and "ORDER BY id" in sql and "ts::text" not in sql
    assert [s.name for s in te.STEPS][-1] == "audit_logs"  # the audit redaction runs after every other step
    names = {s.name for s in te.STEPS}
    assert {"trip_cases", "trip_case_checks", "b2b_partners", "notifications"} <= names
    assert not names & {k for k, _ in te.KEPT}  # money rows are never a step (accounting exception, D3)
    for step in te.STEPS:
        assert "$1" in step.rows and "$1" in step.update


def test_names_and_environment() -> None:
    assert te.erased_name(TENANT) == "Erased agency 00000000"
    assert te.cli_request_id(TENANT) == te.cli_request_id(TENANT) != te.cli_request_id(uuid.UUID(int=3))
    assert te.is_prod("prod") and te.is_prod(" PROD ")
    assert not te.is_prod(None) and not te.is_prod("dev") and not te.is_prod("")  # fail closed: synthetic only
    assert te.KIND in EXPECTED_SECONDS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "prod", "code"),
    [
        ({"pilot": True}, True, "pilot_tenant"),
        ({"synthetic": False}, False, "not_synthetic"),
        ({"account": False}, True, "tenant_unknown"),
        ({"ready": False}, True, "account_unknown"),
        ({"synthetic": False}, True, None),
        ({}, False, None),
    ],
)
async def test_guards(kwargs: dict[str, bool], prod: bool, code: str | None) -> None:
    result = await te.guard_result(scripted(**kwargs), TENANT, prod=prod)  # type: ignore[arg-type]
    assert result == {"apply_allowed": code is None, "refused": code}


@pytest.mark.asyncio
async def test_plan_counts_and_hash() -> None:
    planned = await te.plan(scripted(), TENANT)  # type: ignore[arg-type]
    assert planned["rows"] == 2 * len(te.STEPS)
    assert planned["kept"] == {k: 1 for k, _ in te.KEPT}
    assert planned["plan_hash"] == te.plan_hash(TENANT, steps(count=2))
    assert te.counts(planned)["kept"]["agency_invoice"] == 1


@pytest.mark.asyncio
async def test_a_stale_hash_writes_nothing_and_is_recorded() -> None:
    c = scripted()
    with pytest.raises(te.PlanChanged):
        await te.apply(c, TENANT, "f" * 64, request_id=REQUEST, prod=False, source="cli")  # type: ignore[arg-type]
    assert not [s for s in te.STEPS if c.sql(s.update)]
    assert c.sql("INSERT INTO booking.audit_logs") == []
    refused = c.sql("INSERT INTO booking.tenant_erasure_log")
    assert refused and refused[-1][4:6] == ("refused", "plan_changed")
    assert c.sql("SET status = 'stale'")


@pytest.mark.asyncio
async def test_a_refused_tenant_writes_nothing_but_the_refusal() -> None:
    c = scripted(pilot=True)
    planned = await te.plan(c, TENANT)  # type: ignore[arg-type]
    with pytest.raises(te.Refused) as info:
        await te.apply(c, TENANT, planned["plan_hash"], request_id=REQUEST, prod=True, source="cli")  # type: ignore[arg-type]
    assert info.value.code == "pilot_tenant"
    assert not [s for s in te.STEPS if c.sql(s.update)]
    assert c.sql("INSERT INTO booking.tenant_erasure_log")[-1][4:6] == ("refused", "pilot_tenant")


@pytest.mark.asyncio
async def test_apply_runs_every_step_once_then_the_audit_row() -> None:
    c = scripted()
    planned = await te.plan(c, TENANT)  # type: ignore[arg-type]
    done = await te.apply(c, TENANT, planned["plan_hash"], request_id=REQUEST, prod=False, source="cli")  # type: ignore[arg-type]
    assert done["outcome"] == "applied"
    assert all(len(c.sql(s.update)) == 1 for s in te.STEPS)
    assert c.sql("set_config($1, $2, true)") == [(te.ERASE_SETTING, str(REQUEST))]
    (audit_row,) = c.sql("INSERT INTO booking.audit_logs")
    assert audit_row[3] == "tenant.erased" and audit_row[4] == "tenant"
    assert c.sql("INSERT INTO booking.tenant_erasure_log")[-1][4] == "applied"


@pytest.mark.asyncio
async def test_an_applied_request_is_not_applied_again() -> None:
    c = scripted().on("mode = 'apply' AND status = 'applied'", "a" * 64)
    out = await te.apply(c, TENANT, "b" * 64, request_id=REQUEST, prod=False, source="cli")  # type: ignore[arg-type]
    assert out["outcome"] == "already_applied" and not c.sql(te.STEPS[0].update)


@pytest.mark.asyncio
async def test_the_job_modes() -> None:
    def job(**payload: Any) -> Job:
        return Job("j1", te.KIND, {"request_id": str(REQUEST), "tenant_id": str(TENANT), **payload}, 1, 3)

    off = te.build_handler(apply_enabled=False, environment="dev")
    c = scripted()
    planned = await off(c, job(mode="dry_run"))  # type: ignore[arg-type]
    assert planned["outcome"] == "planned" and planned["apply_allowed"] is True
    assert c.sql("INSERT INTO booking.tenant_erasure_log")[-1][4] == "cis"
    assert (await off(c, job(mode="apply", expect_plan="a" * 64)))["outcome"] == "disabled"  # type: ignore[arg-type]
    assert (await off(c, job(mode="other")))["outcome"] == "invalid_payload"  # type: ignore[arg-type]
    assert (await off(c, Job("j2", te.KIND, {"tenant_id": "x"}, 1, 3)))["outcome"] == "invalid_payload"  # type: ignore[arg-type]
    on = te.build_handler(apply_enabled=True, environment="dev")
    refused = await on(scripted(synthetic=False), job(mode="apply", expect_plan=planned["plan_hash"]))  # type: ignore[arg-type]
    assert refused == {"outcome": "refused", "reason": "not_synthetic", "detail": refused["detail"]}
    no_table = scripted().on("to_regclass('booking.tenant_erasure_log')", False)
    assert (await on(no_table, job(mode="dry_run")))["outcome"] == "skipped"  # type: ignore[arg-type]


def requests_conn(rows: list[dict[str, Any]], logged: list[dict[str, Any]] | None = None) -> ScriptedConn:
    c = scripted()
    c.on("FROM catalog.v_tenant_erasure_v1", rows)
    c.on("SELECT request_id, mode, status FROM booking.tenant_erasure_log", logged or [])
    c.on("INSERT INTO booking.job", lambda *a: f"job-{a[2]}")
    return c


@pytest.mark.asyncio
async def test_schedule_queues_plans_and_approved_applies_once_per_slot() -> None:
    other = uuid.UUID(int=5)
    rows = [
        {"request_id": REQUEST, "tenant_id": TENANT, "phase": "plan", "approved": None},
        {"request_id": other, "tenant_id": uuid.UUID(int=6), "phase": "apply", "approved": "c" * 64},
        {"request_id": uuid.UUID(int=8), "tenant_id": uuid.UUID(int=9), "phase": "apply", "approved": "not-a-hash"},
    ]
    clock = {"now": NOW}
    c = requests_conn(rows)
    hook = te.EraseSchedule(apply_enabled=True, now=lambda: clock["now"])
    queued = await hook(c)  # type: ignore[arg-type]
    assert queued is not None and len(queued) == 2
    keys = [args[2] for args in c.sql("INSERT INTO booking.job")]
    assert keys[0].startswith(f"{te.KIND}:{REQUEST}:dry_run:") and keys[1] == f"{te.KIND}:{other}:apply:{'c' * 64}"
    assert await hook(c) is None  # once per slot
    clock["now"] = NOW + timedelta(seconds=te.SLOT_SECONDS)
    done = requests_conn(rows, [{"request_id": REQUEST, "mode": "dry_run", "status": "planned"},
                                {"request_id": other, "mode": "apply", "status": "applied"}])  # fmt: skip
    assert await hook(done) == []  # a current plan and an applied request queue nothing
    off = te.EraseSchedule(apply_enabled=False, now=lambda: NOW)
    assert len(await off(requests_conn(rows))) == 1  # type: ignore[arg-type,arg-type]


@pytest.mark.asyncio
async def test_schedule_without_the_view_queues_nothing() -> None:
    c = requests_conn([]).on(VIEW_READY_FRAGMENT, False)
    assert await te.EraseSchedule(apply_enabled=True, now=lambda: NOW)(c) is None  # type: ignore[arg-type]
    assert c.sql("INSERT INTO booking.job") == []
