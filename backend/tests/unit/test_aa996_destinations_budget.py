"""AA-996 S255: the trip-plans list destinations read is bounded.

Live check 28/10 (role booking on Dev): ``catalog.v_tour_place`` keyed by ``tour_id = ANY`` took 380 ms for a page of
25 and 994 ms for 100, against the 1 s ``statement_timeout`` of a list extra. The read now goes in chunks of
``DESTINATION_CHUNK`` tours, every chunk of a page inside ``DESTINATIONS_BUDGET_MS`` of wall time, and a tour
version's places are cached per process. These tests drive the read with a fake clock: each scripted statement
advances it by a chosen duration, so the budget arithmetic is checked without a slow database."""

from __future__ import annotations

from typing import Any

import asyncpg
import pytest

from app.catalog import trip_plans
from tests.unit.fakes import ScriptedConn
from tests.unit.test_trip_plans import SALES

DESTINATIONS = "FROM catalog.v_tour_place"
TIMEOUT = "SET LOCAL statement_timeout = "


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(trip_plans, "_now", fake)
    monkeypatch.setattr(
        trip_plans,
        "DESTINATIONS_CACHE",
        trip_plans.TtlCache(trip_plans.DESTINATIONS_TTL_S, trip_plans.DESTINATIONS_CACHE_MAX, clock=fake),
    )
    return fake


def _rows(n: int) -> list[dict[str, Any]]:
    return [
        {"source_system": "cis", "source_id": f"s-{i}", "tour_id": f"t-{i}", "plan_id": None,
         "cover_photo_path": "/p/own", "price_hint_amount": None}
        for i in range(n)
    ]  # fmt: skip


def _conn(n: int, clock: FakeClock, *, ms_per_read: float = 400, fail_on: int | None = None) -> ScriptedConn:
    """A list page of ``n`` rows whose destination reads each take ``ms_per_read`` and return two places per tour.
    Like the server, a read longer than the statement timeout it runs under is cancelled at that timeout; read number
    ``fail_on`` (1-based) fails at once."""
    state: dict[str, Any] = {"reads": 0, "timeout_ms": None}

    def places(ids: list[str]) -> Any:
        state["reads"] += 1
        if fail_on == state["reads"]:
            return asyncpg.InsufficientPrivilegeError("permission denied for view v_tour_place")
        timeout = state["timeout_ms"]
        if timeout is not None and ms_per_read > timeout:
            clock.now += timeout / 1000
            return asyncpg.QueryCanceledError("canceling statement due to statement timeout")
        clock.now += ms_per_read / 1000
        return [{"key": t, "places": [f"{t} town", "Shared"]} for t in ids]

    conn = (
        ScriptedConn()
        .on("ORDER BY", _rows(n))
        .on("SELECT count(*)", n)
        .on(DESTINATIONS, places)
        .on("FROM catalog.v_tour_photo", [])
        .on("AS u(plan_id)", [])
    )
    original = conn.execute

    async def execute(sql: str, *args: Any) -> str:
        if sql.startswith(TIMEOUT):
            state["timeout_ms"] = int(sql.removeprefix(TIMEOUT))
        return await original(sql, *args)

    conn.execute = execute  # type: ignore[method-assign]
    return conn


def _destination_timeouts(conn: ScriptedConn) -> list[int]:
    """The statement_timeout each destinations read ran under (the SET LOCAL right before it)."""
    out, last = [], None
    for flat, _ in conn.calls:
        if flat.startswith(TIMEOUT):
            last = int(flat.removeprefix(TIMEOUT))
        elif DESTINATIONS in flat:
            out.append(last)
    return out


def test_the_sql_returns_at_most_the_places_the_list_shows() -> None:
    assert f"[1:{trip_plans.MAX_DESTINATIONS}] AS places" in trip_plans.DESTINATIONS_SQL
    assert "p.tour_id = ANY($1::uuid[])" in trip_plans.DESTINATIONS_SQL


def test_the_chunk_fits_the_extra_timeout_at_the_measured_cost() -> None:
    # ~10 ms a tour on Dev (380 ms / 25, 994 ms / 100): a chunk must leave at least 2x headroom under the timeout.
    measured_ms_per_tour = 994 / 100
    assert trip_plans.DESTINATION_CHUNK * measured_ms_per_tour * 2 <= trip_plans.EXTRA_TIMEOUT_MS
    assert trip_plans.DESTINATIONS_BUDGET_MS < 5000  # the pool's command_timeout (app.db)
    assert trip_plans.MAX_PAGE % trip_plans.DESTINATION_CHUNK == 0


@pytest.mark.asyncio
async def test_a_page_of_100_reads_in_chunks_of_25(clock: FakeClock) -> None:
    conn = _conn(100, clock)
    out = await trip_plans.list_plans(conn, SALES, limit=100)
    reads = conn.sql(DESTINATIONS)
    assert [len(ids) for (ids,) in reads] == [25, 25, 25, 25]
    assert sorted(t for (ids,) in reads for t in ids) == sorted(f"t-{i}" for i in range(100))
    assert out["degraded"] == [] and all(item["destinations"] for item in out["items"])
    assert out["items"][7]["destinations"] == ["t-7 town", "Shared"]
    # every read ran under its own timeout, never above the extra budget
    assert all(t is not None and t <= trip_plans.EXTRA_TIMEOUT_MS for t in _destination_timeouts(conn))


@pytest.mark.asyncio
async def test_a_page_of_25_is_still_one_read(clock: FakeClock) -> None:
    conn = _conn(25, clock)
    await trip_plans.list_plans(conn, SALES)
    assert len(conn.sql(DESTINATIONS)) == 1


@pytest.mark.asyncio
async def test_the_wall_budget_bounds_the_whole_read(clock: FakeClock) -> None:
    # Worst case that still answers: each read takes the full 1 s extra timeout. Two reads use the page budget, the
    # rest are not started: never more than the budget in all, and the rows already read keep their places.
    conn = _conn(100, clock, ms_per_read=1000)
    start = clock.now
    out = await trip_plans.list_plans(conn, SALES, limit=100)
    assert (clock.now - start) * 1000 <= trip_plans.DESTINATIONS_BUDGET_MS
    assert _destination_timeouts(conn) == [1000, 1000]
    assert all(i["destinations"] for i in out["items"][:50]) and out["degraded"] == ["destinations"]
    assert all(i["destinations"] is None for i in out["items"][50:])


@pytest.mark.asyncio
async def test_a_chunk_gets_only_what_is_left_of_the_budget(clock: FakeClock) -> None:
    conn = _conn(100, clock, ms_per_read=700)  # 700 + 700, then 600 left: the third read is cut there
    start = clock.now
    out = await trip_plans.list_plans(conn, SALES, limit=100)
    assert _destination_timeouts(conn) == [1000, 1000, 600]
    assert (clock.now - start) * 1000 == pytest.approx(trip_plans.DESTINATIONS_BUDGET_MS)
    assert sum(1 for i in out["items"] if i["destinations"]) == 50


@pytest.mark.asyncio
async def test_a_read_slower_than_the_timeout_costs_one_timeout_not_four(clock: FakeClock) -> None:
    conn = _conn(100, clock, ms_per_read=10_000)
    start = clock.now
    out = await trip_plans.list_plans(conn, SALES, limit=100)
    assert (clock.now - start) * 1000 == pytest.approx(trip_plans.EXTRA_TIMEOUT_MS) and len(conn.sql(DESTINATIONS)) == 1
    assert all(i["destinations"] is None for i in out["items"]) and out["degraded"] == ["destinations"]


@pytest.mark.asyncio
async def test_a_chunk_that_does_not_fit_is_not_started_and_logged(
    clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    conn = _conn(100, clock, ms_per_read=960)  # 960 + 960 = 1920: 80 ms left, under MIN_CHUNK_MS
    with caplog.at_level("WARNING", logger="app.catalog.trip_plans"):
        out = await trip_plans.list_plans(conn, SALES, limit=100)
    assert len(conn.sql(DESTINATIONS)) == 2
    assert sum(1 for i in out["items"] if i["destinations"] is None) == 50
    (line,) = [r.getMessage() for r in caplog.records if "trip_plan_list_extra_partial" in r.getMessage()]
    assert "reason=budget" in line and "unresolved=50" in line


@pytest.mark.asyncio
async def test_a_failing_chunk_stops_the_read_and_keeps_the_rows_already_read(
    clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    conn = _conn(100, clock, fail_on=2)
    with caplog.at_level("WARNING", logger="app.catalog.trip_plans"):
        out = await trip_plans.list_plans(conn, SALES, limit=100)
    assert len(conn.sql(DESTINATIONS)) == 2  # no third read after a failure
    items = out["items"]
    assert all(i["destinations"] for i in items[:25]) and all(i["destinations"] is None for i in items[25:])
    assert out["degraded"] == ["destinations"]
    lines = [r.getMessage() for r in caplog.records if "trip_plan_list_extra_unavailable" in r.getMessage()]
    assert len(lines) == 1 and "extra=destinations" in lines[0]


@pytest.mark.asyncio
async def test_a_second_list_reads_the_places_from_the_cache_until_they_expire(clock: FakeClock) -> None:
    first = _conn(25, clock)
    await trip_plans.list_plans(first, SALES)
    assert len(first.sql(DESTINATIONS)) == 1
    again = _conn(25, clock)
    out = await trip_plans.list_plans(again, SALES)
    assert again.sql(DESTINATIONS) == [] and out["items"][0]["destinations"] == ["t-0 town", "Shared"]
    assert out["degraded"] == []
    # a bigger page reads only the tours it has not seen
    bigger = _conn(30, clock)
    await trip_plans.list_plans(bigger, SALES, limit=30)
    assert bigger.sql(DESTINATIONS) == [([f"t-{i}" for i in range(25, 30)],)]
    clock.now += trip_plans.DESTINATIONS_TTL_S + 1
    expired = _conn(25, clock)
    await trip_plans.list_plans(expired, SALES)
    assert len(expired.sql(DESTINATIONS)) == 1


@pytest.mark.asyncio
async def test_a_tour_without_places_is_cached_as_empty_and_a_failed_one_is_not(clock: FakeClock) -> None:
    conn = (
        ScriptedConn().on(DESTINATIONS, [])  # neither tour has a place
    )
    assert await trip_plans._destinations(conn, ["t-1", "t-2", "t-1"]) == {"t-1": [], "t-2": []}
    assert trip_plans.DESTINATIONS_CACHE.get_many(["t-1", "t-2"]) == {"t-1": [], "t-2": []}
    failing = ScriptedConn().on(DESTINATIONS, TimeoutError())
    assert await trip_plans._destinations(failing, ["t-3"]) == {"t-3": None}
    assert trip_plans.DESTINATIONS_CACHE.get_many(["t-3"]) == {}
    assert await trip_plans._destinations(ScriptedConn(), []) == {}


def test_the_cache_is_bounded_and_drops_the_oldest_first() -> None:
    clock = FakeClock()
    cache = trip_plans.TtlCache(60, 3, clock=clock)
    cache.put_many({"a": 1, "b": 2})
    clock.now += 30
    cache.put_many({"c": 3, "d": 4})
    assert len(cache) == 3 and cache.get_many("abcd") == {"b": 2, "c": 3, "d": 4}
    clock.now += 31  # b expired, c and d not yet
    cache.put_many({"e": 5})
    assert cache.get_many("abcde") == {"c": 3, "d": 4, "e": 5}
    off = trip_plans.TtlCache(0, 3, clock=clock)
    off.put_many({"a": 1})
    assert len(off) == 0
    cache.clear()
    assert len(cache) == 0
