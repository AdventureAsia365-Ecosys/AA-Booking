"""AA-1013: what the CIS catalog says today about proposal items (app.trip_cases.cis_readback), with a scripted
connection. Tour, day and place names are invented.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import asyncpg
import pytest

from app.trip_cases import cis_readback
from tests.catalog_perf import static_findings
from tests.unit.fakes import ScriptedConn

TOUR = "5a000000-0000-4000-8000-000000000001"
OTHER_TOUR = "5a000000-0000-4000-8000-000000000002"
UPDATED = datetime(2027, 3, 4, 5, 6, tzinfo=UTC)
TOURS = "FROM catalog.v_tour WHERE"
DAYS = "FROM catalog.v_tour_day"


def item(
    title: str,
    day_from: int,
    day_to: int | None = None,
    source: str | None = "LR-5D",
    notes: str | None = None,
    costs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "title": title,
        "day_from": day_from,
        "day_to": day_to or day_from,
        "source_tour_id": source,
        "notes": notes,
        "costs": costs or [],
    }


def tour_row(source_id: str = "LR-5D", tour_id: str = TOUR, title: str = "Lantern Ridge Trek — 5 Days") -> dict:
    return {"source_id": source_id, "tour_id": tour_id, "title": title, "updated_at": UPDATED}


def day_rows(*titles: str | None, tour_id: str = TOUR) -> list[dict[str, Any]]:
    return [{"tour_id": tour_id, "day_number": n, "title": t} for n, t in enumerate(titles, start=1)]


def conn_with(tours: Any, days: Any = ()) -> ScriptedConn:
    return ScriptedConn().on(TOURS, tours).on(DAYS, list(days) if not isinstance(days, BaseException) else days)


@pytest.mark.asyncio
async def test_day_by_day_items_are_compared_with_todays_cis_days() -> None:
    items = [item("Arrival at Lantern Ridge", 1), item("Ridge  walk to\nthe upper lake", 2), item("Day 3", 3)]
    conn = conn_with([tour_row()], day_rows("Lantern Ridge arrival", "Ridge walk to the upper lake", "", "Departure"))
    await cis_readback.attach(conn, items)
    assert [i["cis"]["title"] for i in items] == ["Lantern Ridge arrival", "Ridge walk to the upper lake", None]
    # Whitespace does not count as a change; a CIS day without a title has nothing to compare with.
    assert [i["cis"]["changed"] for i in items] == [True, False, False]
    assert items[0]["cis"] == {
        "source_id": "LR-5D",
        "tour_title": "Lantern Ridge Trek — 5 Days",
        "tour_updated_at": UPDATED,
        "day_number": 1,
        "title": "Lantern Ridge arrival",
        "changed": True,
    }
    # One read of each view; the day view keyed by tour_id only (AA-996 perf guard).
    assert conn.sql(TOURS) == [(["LR-5D"],)]
    assert conn.sql(DAYS) == [([TOUR],)]


@pytest.mark.asyncio
async def test_a_tour_placed_later_in_the_trip_lines_up_from_its_first_item() -> None:
    items = [item("Own arrival day", 1, source=None), item("Old day one", 3), item("Old day two", 4)]
    conn = conn_with([tour_row()], day_rows("New day one", "Old day two"))
    await cis_readback.attach(conn, items)
    assert items[0]["cis"] is None
    assert [(i["cis"]["day_number"], i["cis"]["changed"]) for i in items[1:]] == [(1, True), (2, False)]


@pytest.mark.asyncio
async def test_multi_day_items_name_the_tour_but_are_not_compared() -> None:
    """A TripPlanner leg or a B2B trip plan line spans days: tour title only, no day, never 'changed'."""
    items = [item("trip_plan tp:x · line 1", 1, 5), item("Old day one", 6, source=TOUR)]
    conn = conn_with([tour_row()])
    await cis_readback.attach(conn, items)
    assert items[0]["cis"]["tour_title"] == "Lantern Ridge Trek — 5 Days"
    assert (items[0]["cis"]["day_number"], items[0]["cis"]["title"], items[0]["cis"]["changed"]) == (None, None, False)
    # The second item names the same tour by its CIS tour_id: one tour, one group, which has a multi-day item.
    assert items[1]["cis"]["day_number"] is None
    assert not conn.sql(DAYS)


@pytest.mark.asyncio
async def test_source_id_match_wins_and_unknown_keys_get_none() -> None:
    items = [item("A", 1, source="LR-5D"), item("B", 1, source="GONE-1"), item("C", 1, source=OTHER_TOUR)]
    conn = conn_with(
        [tour_row(), tour_row(source_id="SB-1D", tour_id=OTHER_TOUR, title="Salt Bay Day Escape")],
        day_rows("A") + day_rows("Bay arrival", tour_id=OTHER_TOUR),
    )
    await cis_readback.attach(conn, items)
    assert items[1]["cis"] is None
    # A lone one-day item is a day copy when CIS has one day for its tour.
    assert items[2]["cis"]["source_id"] == "SB-1D" and items[2]["cis"]["changed"] is True
    assert items[0]["cis"]["changed"] is False
    assert sorted(conn.sql(DAYS)[0][0]) == sorted([TOUR, OTHER_TOUR])


def b2b_cost(line: int = 1) -> dict[str, Any]:
    note = f"B2B quote q-1 line {line} (1 supplier rates)"
    return {"net_amount": "100.00", "net_currency": "USD", "supplier_note": note}


@pytest.mark.parametrize(
    "items",
    [
        # A B2B confirm of a one-day trip plan: the line is titled with the tour (AA-1013) and noted "Agency line".
        [item("Lantern Ridge Trek — 5 Days", 1, notes="Agency line L1")],
        # An older B2B line: a technical title, no notes, but its cost is a B2B quote line.
        [item("trip_plan tp:x · line L1", 1, costs=[b2b_cost()])],
        # One tour on two one-day B2B lines: never mapped to CIS days 1 and 2.
        [item("Old day one", 1, notes="Agency line L1"), item("Old day two", 2, notes="Agency line L2")],
        # A one-day TripPlanner leg of a multi-day CIS tour, titled from the TripPlanner snapshot.
        [item("Lakeside lantern evening", 1)],
        # A leg without a named snapshot day.
        [item("AA tour LR-5D", 1)],
        # An item that names the tour is not out of date, whatever its shape.
        [item("Lantern  Ridge Trek — 5 Days", 1), item("Old day two", 2)],
    ],
    ids=["b2b-one-day-plan", "b2b-older-line", "b2b-two-lines", "one-day-leg", "leg-fallback", "tour-title"],
)
@pytest.mark.asyncio
async def test_items_that_are_not_a_day_copy_are_never_flagged(items: list[dict[str, Any]]) -> None:
    conn = conn_with([tour_row()], day_rows("Lantern Ridge arrival", "Upper lake walk", "Monastery", "Ridge", "Home"))
    await cis_readback.attach(conn, items)
    for got in items:
        assert got["cis"]["tour_title"] == "Lantern Ridge Trek — 5 Days"
        assert (got["cis"]["day_number"], got["cis"]["title"], got["cis"]["changed"]) == (None, None, False)


@pytest.mark.asyncio
async def test_a_lone_one_day_item_of_a_one_day_tour_is_compared_but_not_its_fallback_title() -> None:
    day_trip = tour_row(source_id="SB-1D", title="Salt Bay Day Escape")
    items = [item("Old bay morning", 1, source="SB-1D")]
    await cis_readback.attach(conn_with([day_trip], day_rows("Salt Bay morning cruise")), items)
    assert (items[0]["cis"]["day_number"], items[0]["cis"]["changed"]) == (1, True)
    items = [item("AA tour SB-1D", 1, source="SB-1D")]
    await cis_readback.attach(conn_with([day_trip], day_rows("Salt Bay morning cruise")), items)
    assert items[0]["cis"]["changed"] is False


@pytest.mark.asyncio
async def test_items_without_a_tour_read_nothing() -> None:
    items = [item("Free day", 1, source=None)]
    conn = ScriptedConn()
    await cis_readback.attach(conn, items)
    assert items[0]["cis"] is None and conn.calls == []


@pytest.mark.parametrize(
    "failure",
    [
        asyncpg.UndefinedTableError("relation does not exist"),
        asyncpg.InsufficientPrivilegeError("denied"),
        TimeoutError(),
    ],
)
@pytest.mark.asyncio
async def test_a_catalog_problem_leaves_every_item_without_cis(failure: BaseException) -> None:
    items = [item("A", 1)]
    await cis_readback.attach(conn_with(failure), items)
    assert items[0]["cis"] is None
    items = [item("A", 1)]
    await cis_readback.attach(conn_with([tour_row()], failure), items)
    assert items[0]["cis"] is None


def test_day_copies_skip_tours_with_a_multi_day_item_and_lone_items_of_multi_day_tours() -> None:
    items = [item("A", 2), item("B", 3), item("C", 4, 5, source="SB-2D"), item("D", 6, source="KF-3D")]
    tours = {
        "LR-5D": tour_row(),
        "SB-2D": tour_row(source_id="SB-2D", tour_id=OTHER_TOUR, title="Salt Bay Escape — 2 Days"),
        "KF-3D": tour_row(source_id="KF-3D", tour_id="kf", title="Kestrel Falls — 3 Days"),
    }
    copies = cis_readback.day_copies(items, tours)
    assert copies == {TOUR: [0, 1], "kf": [3]}
    assert cis_readback.day_numbers(items, copies, {TOUR: 5, "kf": 3}) == {0: 1, 1: 2}
    assert cis_readback.day_numbers(items, copies, {TOUR: 5, "kf": 1}) == {0: 1, 1: 2, 3: 1}


def test_reads_pass_the_parse_backed_view_guard() -> None:
    assert static_findings(cis_readback.DAYS_SQL) == []
    assert static_findings(cis_readback.TOURS_SQL) == []
