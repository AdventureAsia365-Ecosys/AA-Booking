"""AA-1128 B1 (S283 UI audit, Booking row 11): the season calendar step of ``bk_trip_plan_sync`` without a database.

On Dev the step timed out on every run (405/405) while the job said "succeeded". These tests pin the fix: the step
reads its batch size and catalog read timeout from the pricing policy (bk-059), a skipped or cut step makes the run
``degraded`` (never a quiet ``ok``), and a policy without the two values skips the step and says so. A scripted
connection answers by SQL fragment; invented keys only."""

from __future__ import annotations

from datetime import date
from typing import Any

import asyncpg
import pytest

from app.catalog import liveness
from app.catalog import trip_plan_sync as sync
from tests.unit.fakes import POLICY_FRAGMENT, ScriptedConn, policy_rows

TODAY_SQL = "SELECT (now() AT TIME ZONE 'UTC')::date"


def _conn(**policy: str) -> ScriptedConn:
    conn = ScriptedConn([(TODAY_SQL, date(2031, 3, 1))])
    if policy:
        conn.on(POLICY_FRAGMENT, lambda *_: policy_rows(**policy))
    return conn


@pytest.mark.asyncio
async def test_the_step_reads_its_batch_and_timeout_from_the_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[int, str, float]] = []

    async def assign(conn: Any, limit: int, salt: str, read_timeout: float) -> dict[str, int]:
        seen.append((limit, salt, read_timeout))
        return {"picked": 0, "assigned": 0}

    monkeypatch.setattr(sync.seasons_admin, "assign_default_calendars", assign)
    out = await sync._calendars(_conn(season_sync_batch_size="7", season_sync_read_timeout_seconds="3"))  # type: ignore[arg-type]
    assert out == {"picked": 0, "assigned": 0}
    assert [(limit, timeout) for limit, _, timeout in seen] == [(7, 3.0)]
    assert seen[0][1].startswith("bk_trip_plan_sync:slot:")


@pytest.mark.asyncio
async def test_a_policy_without_the_values_skips_the_step(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = ScriptedConn([(TODAY_SQL, date(2031, 3, 1))])
    conn.on(POLICY_FRAGMENT, lambda *_: [r for r in policy_rows() if not r["param_key"].startswith("season_sync_")])

    async def assign(*_: Any) -> dict[str, int]:
        raise AssertionError("must not run without the policy values")

    monkeypatch.setattr(sync.seasons_admin, "assign_default_calendars", assign)
    assert await sync._calendars(conn) == {"skipped": "policy_unavailable"}  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [asyncpg.UndefinedTableError("no view"), asyncpg.InsufficientPrivilegeError("no grant"), TimeoutError()],
)
async def test_a_catalog_error_skips_the_step_and_names_it(
    error: BaseException, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def assign(*_: Any) -> dict[str, int]:
        raise error

    monkeypatch.setattr(sync.seasons_admin, "assign_default_calendars", assign)
    assert await sync._calendars(_conn()) == {"skipped": type(error).__name__}  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("calendars", "degraded"),
    [
        ({"picked": 50, "assigned": 48, "no_tour": 1, "no_destination": 1, "timed_out": 0, "not_tried": 0}, False),
        ({"picked": 0, "assigned": 0, "timed_out": 0, "not_tried": 0}, False),
        ({"skipped": "TimeoutError"}, True),
        ({"skipped": "policy_unavailable"}, True),
        ({"picked": 50, "assigned": 3, "timed_out": 1, "not_tried": 46}, True),
        ({"picked": 1, "assigned": 0, "timed_out": 1, "not_tried": 0}, True),
    ],
)
def test_a_skipped_or_cut_step_is_degraded(calendars: dict[str, Any], degraded: bool) -> None:
    assert sync.calendars_degraded(calendars) is degraded


async def _run(monkeypatch: pytest.MonkeyPatch, calendars: dict[str, Any]) -> dict[str, Any]:
    async def ready(conn: Any, view: liveness.View) -> bool:
        return view == sync.TOUR_VIEW

    async def step(pool: Any) -> dict[str, Any]:
        return calendars

    monkeypatch.setattr(sync.liveness, "view_ready", ready)
    monkeypatch.setattr(sync, "_calendars", step)
    conn = ScriptedConn([(sync.KEYS_SQL.strip(), [{"source_id": "k-1"}])])
    return await sync.run(conn)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_run_reports_degraded_when_the_step_is_skipped(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    out = await _run(monkeypatch, {"skipped": "TimeoutError"})
    assert (out["outcome"], out["degraded"]) == ("degraded", ["calendars"])
    assert out["calendars"] == {"skipped": "TimeoutError"}
    assert "bk_trip_plan_sync degraded" in caplog.text


@pytest.mark.asyncio
async def test_run_reports_ok_when_the_whole_batch_was_tried(monkeypatch: pytest.MonkeyPatch) -> None:
    out = await _run(monkeypatch, {"picked": 2, "assigned": 1, "no_destination": 1, "timed_out": 0, "not_tried": 0})
    assert (out["outcome"], out["degraded"]) == ("ok", [])
    assert out["tours"] == 1
