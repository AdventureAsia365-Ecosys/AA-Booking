"""AA-1088 (M13 of the S278 system audit) without a database: a B2B trip plan line covers the tour's catalog days, so
the confirmation and the vouchers show end = start + duration_days - 1 (they showed end = start for a tour whose plan
prices only day 1), and a B2B case reads as the tour sold instead of "Custom trip". Names and places are invented."""

from __future__ import annotations

from datetime import date
from typing import Any

import asyncpg
import pytest

from app.b2b import confirmation, plan_span, vouchers
from app.trip_cases import sql
from tests.unit.fakes import ScriptedConn
from tests.unit.test_aa1037_g_extras import BOOKING_ROW, CASE, LINE_ROWS, NOW, PLAN, confirmation_conn

START = date(2027, 3, 1)
TOUR_KEY = "invented-tour-key-6d"


def _line(component_id: str, service_date: date = START, nights: int | None = None) -> dict[str, Any]:
    return {"component_id": component_id, "service_date": service_date, "nights": nights}


def _plan_conn(components_day: int = 1, duration: Any = 6, catalog: Any = None) -> ScriptedConn:
    conn = ScriptedConn()
    conn.on("FROM booking.trip_plans p", [{"id": PLAN, "source_id": TOUR_KEY, "days": components_day}])
    conn.on(
        "FROM catalog.v_tour", catalog if catalog is not None else [{"source_id": TOUR_KEY, "duration_days": duration}]
    )
    return conn


@pytest.mark.asyncio
async def test_plan_days_take_the_catalog_length_over_the_priced_day() -> None:
    days, source = await plan_span.plan_days(_plan_conn(components_day=1, duration=6), [PLAN])
    assert days == {PLAN: 6} and source == {PLAN: TOUR_KEY}
    # a plan priced past the catalog length keeps its own days; a missing or silly length counts as 1
    assert (await plan_span.plan_days(_plan_conn(components_day=8, duration=6), [PLAN]))[0] == {PLAN: 8}
    assert (await plan_span.plan_days(_plan_conn(components_day=2, duration=None), [PLAN]))[0] == {PLAN: 2}
    assert (await plan_span.plan_days(_plan_conn(components_day=1, duration=99999), [PLAN]))[0] == {PLAN: 366}
    assert await plan_span.plan_days(ScriptedConn(), []) == ({}, {})


@pytest.mark.asyncio
async def test_a_catalog_error_falls_back_to_the_components() -> None:
    conn = _plan_conn(components_day=3, catalog=asyncpg.PostgresError("catalog unreadable"))
    assert (await plan_span.plan_days(conn, [PLAN]))[0] == {PLAN: 3}


def test_line_span_and_trip_end() -> None:
    days = {PLAN: 6}
    tour = _line(f"tp:{PLAN}")
    room = _line("rt:30000000-0000-4000-8000-0000000000b1", date(2027, 3, 3), nights=2)
    assert plan_span.line_span(tour, days) == 6 and plan_span.line_span(room, days) == 2
    assert plan_span.line_span(_line("not-a-component", nights=None), days) == 1
    assert plan_span.trip_end([tour, room], days) == date(2027, 3, 6)  # start + 6 - 1
    assert plan_span.trip_end([room], days) == date(2027, 3, 4)
    assert plan_span.trip_end([], days) is None


def _doc_conn(stored_end: date) -> ScriptedConn:
    conn = confirmation_conn({**BOOKING_ROW, "start_date": START, "end_date": stored_end})
    rows = [{**LINE_ROWS[0], "component_id": f"tp:{PLAN}"}, *LINE_ROWS[1:]]
    conn.on("JOIN booking.b2b_hold_lines hl", rows)
    conn.on("FROM booking.trip_plans p", [{"id": PLAN, "source_id": TOUR_KEY, "days": 1}])
    conn.on("FROM catalog.v_tour", [{"source_id": TOUR_KEY, "duration_days": 6}])
    return conn


@pytest.mark.asyncio
async def test_confirmation_shows_the_whole_tour_for_a_case_stored_with_end_equal_start() -> None:
    doc = await confirmation.load(_doc_conn(START), CASE, partner_id="p-1", now=NOW)
    assert doc is not None and doc["start_date"] == START and doc["end_date"] == date(2027, 3, 6)
    assert "01 Mar 2027 to 06 Mar 2027" in confirmation.render_html(doc)
    # a later stored end (staff extended the trip) is kept
    later = await confirmation.load(_doc_conn(date(2027, 3, 9)), CASE, partner_id="p-1", now=NOW)
    assert later is not None and later["end_date"] == date(2027, 3, 9)


@pytest.mark.asyncio
async def test_vouchers_carry_the_same_end_date() -> None:
    conn = _doc_conn(START)
    conn.on("SELECT quote_id::text FROM booking.b2b_holds", "30000000-0000-4000-8000-000000000003")
    conn.on("JOIN booking.suppliers s ON s.id = sb.supplier_id", [])
    doc = await vouchers.load(conn, CASE, partner_id="p-1", now=NOW)
    assert doc is not None and doc["end_date"] == date(2027, 3, 6)


def test_a_b2b_case_without_legs_reads_as_the_tour_sold() -> None:
    shape = sql.trip_shape(None, [], START, headline="  Misty   Ridge Loop ")
    assert shape["trip_title"] == "Misty Ridge Loop" and shape["leg_count"] == 0
    assert sql.trip_shape(None, [], START)["trip_title"] is None  # still a custom trip without a headline
    assert sql.trip_shape(None, [], START, headline="   ")["trip_title"] is None
    leg = {"position": 1, "source_tour_id": "t-1", "day_from": 1, "day_to": 3}
    assert sql.trip_shape(None, [leg], START, headline="Other")["trip_title"] == "AA tour t-1"  # legs win
    assert "tc.channel = 'b2b'" in sql.B2B_HEADLINE and "ORDER BY v.version_number" in sql.B2B_HEADLINE


@pytest.mark.asyncio
async def test_the_confirm_stores_the_whole_tour_as_the_case_end() -> None:
    """The tour (plan priced on days 1-3) is 7 days in the catalog: the case ends on start + 6, its tour item spans
    days 1-7 (was 1-3, the highest priced day)."""
    from tests.unit.test_b2b_hold_flow import START as FLOW_START
    from tests.unit.test_b2b_hold_flow import confirm, confirm_conn

    conn = confirm_conn(tours=[{"source_id": "BT-7D", "title": "Lantern Ridge Trek", "duration_days": 7}])
    await confirm(conn)
    [case] = conn.sql("INSERT INTO booking.trip_cases")
    assert case[2] == FLOW_START and (case[3] - case[2]).days == 6
    items = conn.sql("INSERT INTO booking.trip_case_version_itinerary")
    assert items[0][3:5] == (1, 7)
