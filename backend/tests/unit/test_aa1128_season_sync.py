"""AA-1128 B1 (S283 UI audit, Booking row 11): the season calendar step of ``bk_trip_plan_sync`` without a database.

On Dev the step timed out on every run (405/405) while the job said "succeeded". These tests pin the fix: the step
reads its batch size and catalog read timeout from the pricing policy (bk-059), a skipped or cut step makes the run
``degraded`` (never a quiet ``ok``), and a policy without the two values skips the step and says so. A scripted
connection answers by SQL fragment; invented keys only."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import asyncpg
import pytest

from app.catalog import liveness
from app.catalog import trip_plan_sync as sync
from app.pricing import season_sync
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

    monkeypatch.setattr(sync.season_sync, "assign_default_calendars", assign)
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

    monkeypatch.setattr(sync.season_sync, "assign_default_calendars", assign)
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

    monkeypatch.setattr(sync.season_sync, "assign_default_calendars", assign)
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


# ── The step itself (app.pricing.season_sync) ───────────────────────────────

CAL = "aa112800-0000-4000-8000-0000000000c1"
DEST = "aa112800-0000-4000-8000-0000000000d1"
STAMP = datetime(2031, 2, 1, 9, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("step", "degraded", "skipped"),
    [
        ({"picked": 3, "assigned": 3}, False, None),
        ({"picked": 3, "assigned": 1, "no_tour": 1, "no_destination": 1}, False, None),
        ({"skipped": "TimeoutError"}, True, "TimeoutError"),
        ({"picked": 3, "timed_out": 1, "not_tried": 2}, True, None),
        # a run before AA-1128 wrote no picked / timed_out counts
        ({"assigned": 0, "calendars_created": 0, "no_destination": 0}, False, None),
    ],
)
def test_sync_out_flags_a_skipped_or_cut_step(step: dict[str, Any], degraded: bool, skipped: str | None) -> None:
    out = season_sync.sync_out(STAMP, {"outcome": "ok", "calendars": step})
    assert out is not None
    assert (out["degraded"], out["skipped"]) == (degraded, skipped)


def test_sync_out_without_a_run() -> None:
    assert season_sync.sync_out(None, None) is None


def test_the_batch_never_reads_the_catalog_and_each_catalog_read_is_one_key() -> None:
    """CI guard (AA-1128 B1): the batch query reads booking.trip_plans alone (joining the CIS catalog view for every
    plan before the LIMIT ran past the statement timeout on every Dev run), and each catalog read names one key by
    equality (an ANY() list or a join does not reach the view's per-tour parse, lessons "Dữ liệu")."""
    assert "catalog." not in season_sync.UNASSIGNED_SQL and "JOIN" not in season_sync.UNASSIGNED_SQL.upper()
    for sql in (season_sync.TOUR_ID_SQL, season_sync.DAY_ONE_SQL):
        flat = " ".join(sql.split()).upper()
        assert "ANY(" not in flat.replace(" ", "") and " JOIN " not in flat
        assert "LIMIT 1" in flat


def _assign_conn(
    plans: list[dict[str, Any]], tours: dict[str, Any], places: dict[str, Any], made: list[str]
) -> ScriptedConn:
    def tour_of(system: str, source: str) -> Any:
        return tours.get(source)  # an exception value is raised by ScriptedConn

    def count(dest: str) -> int:
        return int(dest in made)

    def calendar_for(dest: str, name: str, who: str) -> str:
        made.append(dest)
        return CAL

    return ScriptedConn(
        [
            ("WHERE p.season_calendar_id IS NULL ORDER BY md5", plans),
            ("FROM catalog.v_tour t WHERE t.source_system = $1", tour_of),
            ("FROM catalog.v_tour_place pl", lambda tour: places.get(tour)),
            ("SELECT count(*) FROM booking.season_calendars WHERE destination_id", count),
            ("WITH made AS", calendar_for),
        ]
    )


def _plan(n: int) -> dict[str, Any]:
    return {"id": f"p-{n}", "source_system": "cis", "source_id": f"s-{n}"}


@pytest.mark.asyncio
async def test_default_calendars_follow_the_destination_of_day_one() -> None:
    plans = [_plan(1), _plan(2), _plan(3), _plan(4)]
    tours = {"s-1": "t-1", "s-2": "t-2", "s-3": "t-3"}  # s-4: paused in CIS, not in catalog.v_tour
    places = {
        "t-1": {"destination_id": DEST, "destination_name": "Invented Bay"},
        "t-3": {"destination_id": DEST, "destination_name": None},
    }
    made: list[str] = []
    conn = _assign_conn(plans, tours, places, made)
    out = await season_sync.assign_default_calendars(conn, 10, "salt-1", 4.0)
    assert out == {
        "picked": 4,
        "assigned": 2,
        "calendars_created": 1,
        "no_tour": 1,
        "no_destination": 1,
        "timed_out": 0,
        "not_tried": 0,
    }
    assert conn.sql("WITH made AS") == [
        (DEST, "Invented Bay", season_sync.SYSTEM),
        (DEST, "Destination", season_sync.SYSTEM),
    ]
    assert conn.sql("SET season_calendar_id = $2::uuid") == [("p-1", CAL), ("p-3", CAL)]
    assert conn.sql("ORDER BY md5")[0] == (10, "salt-1")
    assert conn.sql("FROM catalog.v_tour t")[0] == ("cis", "s-1")
    # every catalog read carries the policy's read timeout; the booking reads and writes do not
    assert {sql.split(" FROM ")[1].split()[0] for sql, _ in conn.timeouts} == {"catalog.v_tour", "catalog.v_tour_place"}
    assert {t for _, t in conn.timeouts} == {4.0}
    assert out["picked"] == sum(out[k] for k in ("assigned", "no_tour", "no_destination", "timed_out", "not_tried"))


@pytest.mark.asyncio
@pytest.mark.parametrize("cut", [TimeoutError(), asyncpg.QueryCanceledError("canceling statement")])
async def test_a_cut_catalog_read_ends_the_batch_and_counts_the_rest(cut: BaseException) -> None:
    plans = [_plan(1), _plan(2), _plan(3), _plan(4)]
    tours = {"s-1": "t-1", "s-2": cut, "s-3": "t-3", "s-4": "t-4"}
    places = {"t-1": {"destination_id": DEST, "destination_name": "Invented Bay"}}
    conn = _assign_conn(plans, tours, places, [])
    out = await season_sync.assign_default_calendars(conn, 10, "salt-2", 2.0)
    assert (out["picked"], out["assigned"], out["timed_out"], out["not_tried"]) == (4, 1, 1, 2)
    # nothing is read after the cut: a slow catalog would cut every next read too
    assert [args for args in conn.sql("FROM catalog.v_tour t")] == [("cis", "s-1"), ("cis", "s-2")]
    assert conn.sql("SET season_calendar_id = $2::uuid") == [("p-1", CAL)]
