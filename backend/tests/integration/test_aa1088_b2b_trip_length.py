"""AA-1088 (M13 of the S278 system audit) on a real PostgreSQL, as role ``booking``, through the HTTP API. Reuses the
AA-973 / AA-974 fixtures: the plan prices day 1 only; the catalog says the tour is 4 days.

- the confirm stores the trip case's end = start + 4 - 1 (was end = start), and the tour item spans days 1-4;
- the agency's confirmation and vouchers show the same end date, also for a case stored before AA-1088 (end = start);
- the staff case detail and the case list read the B2B case as the tour sold, not "Custom trip".

Dates are relative to today (lessons.md S245). Names and places are invented."""

from __future__ import annotations

from datetime import date, timedelta

import asyncpg
import httpx
import pytest

from app.db import Database
from tests.integration.test_bk020_b2b import (  # noqa: F401 — fixtures admin, client, plan are used by name
    DAY1,
    DAY1_DATE,
    MAKER,
    SOURCE_ID,
    _departure,
    _hold_body,
    _m2m,
    _ok,
    _plan_line,
    _quote,
    _staff,
    admin,
    client,
    plan,
)
from tests.integration.test_bk021_b2b import rules  # noqa: F401 — fixture used by name
from tests.staff_tokens import LocalCis, LocalM2M

FIXTURES = (admin, client, plan, rules)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

Client = tuple[httpx.AsyncClient, LocalCis, LocalM2M, Database]
TOUR_DAYS = 4
LAST_DAY = DAY1_DATE + timedelta(days=TOUR_DAYS - 1)


async def test_a_b2b_booking_covers_the_whole_tour(
    client: Client, admin: asyncpg.Connection, plan: dict[str, str], rules: dict[str, str]
) -> None:
    c, cis, m2m, _ = client
    await admin.execute("UPDATE catalog.v_tour SET duration_days = $2 WHERE source_id = $1", SOURCE_ID, TOUR_DAYS)
    dep = await _departure(c, cis, DAY1, 6)
    quote = await _ok(await _quote(c, m2m, "TL-1", [_plan_line("tour", plan["plan"], dep["id"], DAY1)]), 201)
    hold = await _ok(
        await c.post("/b2b/v1/holds", json=_hold_body(quote, "TL-1"), headers=_m2m(m2m, "TL-1-hold-key")), 201
    )
    booking = await _ok(
        await c.post(
            f"/b2b/v1/holds/{hold['hold_id']}/confirm",
            json={"price_hash": quote["price_hash"]},
            headers=_m2m(m2m, "TL-1-confirm-key"),
        ),
        201,
    )
    case_id = booking["booking_id"]

    case = await admin.fetchrow("SELECT start_date, end_date FROM booking.trip_cases WHERE id = $1::uuid", case_id)
    assert (case["start_date"], case["end_date"]) == (DAY1_DATE, LAST_DAY)
    item = await admin.fetchrow(
        "SELECT it.day_from, it.day_to FROM booking.trip_case_version_itinerary it"
        " JOIN booking.trip_case_versions v ON v.id = it.trip_case_version_id"
        " WHERE v.trip_case_id = $1::uuid AND it.position = 1",
        case_id,
    )
    assert (item["day_from"], item["day_to"]) == (1, TOUR_DAYS)

    doc = await _ok(await c.get(f"/b2b/v1/bookings/{case_id}/confirmation", headers=_m2m(m2m)))
    assert (date.fromisoformat(doc["start_date"]), date.fromisoformat(doc["end_date"])) == (DAY1_DATE, LAST_DAY)
    vouchers = await _ok(await c.get(f"/b2b/v1/bookings/{case_id}/vouchers", headers=_m2m(m2m)))
    assert date.fromisoformat(vouchers["end_date"]) == LAST_DAY

    # A case confirmed before AA-1088 stored end = start: the documents still show the whole tour.
    await admin.execute("UPDATE booking.trip_cases SET end_date = start_date WHERE id = $1::uuid", case_id)
    doc = await _ok(await c.get(f"/b2b/v1/bookings/{case_id}/confirmation", headers=_m2m(m2m)))
    assert date.fromisoformat(doc["end_date"]) == LAST_DAY
    page = await c.get(f"/b2b/v1/bookings/{case_id}/confirmation", params={"format": "html"}, headers=_m2m(m2m))
    assert page.status_code == 200 and LAST_DAY.strftime("%d %b %Y") in page.text

    # The staff case reads as the tour sold, not "Custom trip".
    detail = await _ok(await c.get(f"/trip-cases/{booking['trip_case_code']}", headers=_staff(cis, MAKER)))
    assert detail["channel"] == "b2b" and detail["legs"] == [] and detail["trip_title"] == "Hanoi 1D"
    listed = await _ok(await c.get("/trip-cases", params={"q": booking["trip_case_code"]}, headers=_staff(cis, MAKER)))
    assert [i["trip_title"] for i in listed["items"]] == ["Hanoi 1D"]
