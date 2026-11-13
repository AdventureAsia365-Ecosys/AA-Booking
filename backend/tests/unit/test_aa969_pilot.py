"""AA-969 part 2 (root ADR 0019 d16) without a database: the pilot tenant read and the retention predicate that leaves
the pilot tenant's rows out. On PostgreSQL: tests/integration/test_aa969_pilot_skip.py."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import asyncpg
import pytest

from app import pilot
from app.catalog import liveness
from app.jobs.queue import Job
from app.ops import lifecycle
from app.retention import jobs, policy, runner
from tests.unit.fakes import ScriptedConn

PILOT = uuid.UUID("00000000-0000-4000-8000-00000000f11e")


def test_uuid_array_literal_takes_validated_ids_only() -> None:
    assert pilot.uuid_array_literal([PILOT, str(PILOT)]) == f"'{{{PILOT},{PILOT}}}'::uuid[]"
    assert pilot.uuid_array_literal([]) == "'{}'::uuid[]"
    with pytest.raises(ValueError):
        pilot.uuid_array_literal(["x'); DROP TABLE booking.job; --"])


def test_without_pilots_adds_the_guard_to_tenant_tables_only() -> None:
    assert policy.without_pilots(policy.INBOX_SPEC, []) is policy.INBOX_SPEC
    assert policy.without_pilots(policy.JOB_SPEC, [PILOT]) is policy.JOB_SPEC  # booking.job holds no tenant data
    spec = policy.without_pilots(policy.INBOX_SPEC, [PILOT])
    (part,) = spec.parts
    assert part.window_days == 0 and spec.windows() == policy.INBOX_SPEC.windows()  # windows unchanged (DM-7.5)
    resolved = policy.without_pilots(
        policy.with_windows(policy.INBOX_SPEC, {"retention_inbox_redact_days": 90}), [PILOT]
    )
    assert resolved.windows() == {"payload_pii": 90}
    assert part.where.startswith("(t.redacted_at IS NULL AND t.status <> 'received') AND NOT EXISTS")
    assert f"pc.party_tenant_id = ANY('{{{PILOT}}}'::uuid[])" in part.where
    assert "pc.inbox_event_id = t.id" in runner.count_sql(spec, part, [])


@pytest.mark.asyncio
async def test_pilot_tenants_reads_the_view_or_nothing() -> None:
    liveness.reset_cache()
    conn = ScriptedConn().on("has_schema_privilege", True).on("WHERE is_pilot", [{"tenant_id": PILOT}])
    assert await pilot.pilot_tenants(conn) == [PILOT]  # type: ignore[arg-type]
    liveness.reset_cache()
    none = ScriptedConn().on("has_schema_privilege", True).on("WHERE is_pilot", [])
    assert await pilot.pilot_tenants(none) == []  # type: ignore[arg-type]
    # fail closed: an unreadable view or a database error is "pilot unknown" (None), not "no pilot"
    liveness.reset_cache()
    missing = ScriptedConn().on("has_schema_privilege", False)
    assert await pilot.pilot_tenants(missing) is None  # type: ignore[arg-type]
    assert not missing.sql("WHERE is_pilot")
    liveness.reset_cache()
    broken = (
        ScriptedConn()
        .on("has_schema_privilege", True)
        .on("WHERE is_pilot", asyncpg.InsufficientPrivilegeError("permission denied"))
    )
    assert await pilot.pilot_tenants(broken) is None  # type: ignore[arg-type]
    liveness.reset_cache()


@pytest.mark.asyncio
async def test_case_lifecycle_clears_nothing_when_the_pilot_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    async def unknown(conn: Any) -> None:
        return None

    monkeypatch.setattr(lifecycle.pilot, "pilot_tenants", unknown)
    pool = ScriptedConn()
    for live in (False, True):
        got = await lifecycle._clear(pool, live=live, case_ids=None, today=dt.date(2026, 11, 5), keep_days=90)  # type: ignore[arg-type]
        assert got == {"cleared_checks": 0, "cleared_cases": 0, "clear_skipped_pilot_unknown": 1}
    assert not pool.sql("booking.trip_case_checks")


@pytest.mark.asyncio
async def test_a_retention_run_of_a_tenant_table_stays_dry_when_the_pilot_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unknown(conn: Any) -> None:
        return None

    async def cutoff(conn: Any, days: int) -> dt.datetime:
        return dt.datetime(2026, 8, 1, tzinfo=dt.UTC)

    async def count_part(conn: Any, spec: Any, part: Any, refs: Any, cut: dt.datetime) -> runner.PartResult:
        return runner.PartResult(part.name, part.window_days, part.archive, cut.isoformat(), candidates=3)

    async def prev_live(conn: Any, kind: str) -> dict[str, Any]:
        return {"windows": {"payload_pii": 90}, "affected_total": 10}  # the bk-056 seed window

    async def no_process(*_: Any, **__: Any) -> None:
        raise AssertionError("nothing may be written while the pilot is unknown")

    audited: list[dict[str, Any]] = []

    async def audit(conn: Any, job: Any, result: dict[str, Any]) -> None:
        audited.append(result)

    monkeypatch.setattr(jobs.pilot, "pilot_tenants", unknown)
    monkeypatch.setattr(jobs.runner, "cutoff", cutoff)
    monkeypatch.setattr(jobs.runner, "count_part", count_part)
    monkeypatch.setattr(jobs.runner, "process_part", no_process)
    monkeypatch.setattr(jobs, "last_live", prev_live)
    monkeypatch.setattr(jobs, "_audit", audit)
    conn = ScriptedConn().on("AT TIME ZONE 'UTC')::date", dt.date(2026, 11, 5)).on("status = 'received'", 0)
    job = Job("00000000-0000-4000-8000-000000000969", policy.INBOX_KIND, {"trigger": "schedule"}, 1, 1)
    result = await jobs._run_locked(
        conn,  # type: ignore[arg-type]
        job,
        policy.INBOX_SPEC,
        {"trigger": "schedule"},
        "schedule",
        0.0,
        100,
        lambda: None,  # type: ignore[arg-type,return-value]
        lambda: "bucket",
        {},
    )
    assert (
        result["mode"] == "dry_run" and result["pilot_unknown"] is True and "pilot tenant unknown" in result["reason"]
    )
    assert result["affected_total"] == 0 and audited and audited[0]["mode"] == "dry_run"
