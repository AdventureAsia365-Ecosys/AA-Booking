"""AA-1149 part 1 end to end on a real PostgreSQL (role ``booking``), the Done-when of the issue:

- a 5-day tour (a hotel on days 1-4, a transfer every day, a guide on day 3 from a second supplier) sold to an agency:
  the quote stores 10 parts; Generate makes nothing while the desk has not accepted the on-request tour line, then 10
  pending supplier bookings across 2 suppliers on the right dates; editing the tour afterwards changes nothing sold
  and Generate again makes 0;
- pre_departure needs every part confirmed or waived (the supply state stays confirming, then booked);
- the agency vouchers list the confirmed component bookings under the tour line;
- the tour missing the night-2 hotel is refused for agencies (422 invalid naming N1); an exception with a 10-character
  reason is refused, one with a reason unblocks the quote, and the confirm adds the custom trip check; a missing rate
  can never be covered;
- a component whose supplier is no longer active is booked with another active supplier (201; the old supplier's
  room type / service is dropped, the new one's service is accepted, the old one's refused);
- an exception of the longest length the table allows (90 days, with the policy maximum at 90) passes the CHECK;
- the readiness report carries the can-operate status; a tour on a fixed departure is not checked and keeps the
  supply state none (its costs are booked per departure).

Reuses the AA-973 / AA-974 fixtures (``admin``, ``client``, ``plan``, ``rules``)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio

from app.catalog import sell_exceptions
from app.db import Database
from tests.integration.test_bk020_b2b import (  # noqa: F401 — fixtures admin, client, plan are used by name
    ADMIN,
    CHECKER,
    DAY1,
    DAY1_DATE,
    MAKER,
    PUBLISH,
    SALES,
    SOURCE_ID,
    VALID_FROM,
    VALID_TO,
    _departure,
    _insert,
    _m2m,
    _ok,
    _plan_line,
    _staff,
    admin,
    client,
    plan,
)
from tests.integration.test_bk021_b2b import rules  # noqa: F401 — fixture used by name
from tests.integration.test_bk022_b2b import _book
from tests.staff_tokens import LocalCis, LocalM2M

FIXTURES = (admin, client, plan, rules)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

Client = tuple[httpx.AsyncClient, LocalCis, LocalM2M, Database]
REASON = "The guesthouse for night 2 is booked by hand by the operations team."


@pytest_asyncio.fixture
async def tour(admin: asyncpg.Connection, client: Client, rules: dict[str, str]) -> AsyncIterator[dict[str, str]]:  # noqa: F811
    """The 5-day tour: the plan's room on days 1-4 and transfer on days 1-5 (supplier 1), a guide on day 3 (supplier
    2, 40 USD a group)."""
    c, cis, _, _ = client
    guide_co = await _insert(admin, "suppliers", company_name="Guide Co 1149", categories=["guide"], country_code="VN",
                             created_by=MAKER)  # fmt: skip
    contract = await _insert(admin, "supplier_contracts", supplier_id=guide_co, title="Contract 1149",
                             valid_from=VALID_FROM, valid_to=VALID_TO, currency="USD", created_by=MAKER)  # fmt: skip
    guide = await _insert(admin, "services", supplier_id=guide_co, service_type="guide", title="Hanoi guide",
                          created_by=MAKER)  # fmt: skip
    await _insert(admin, "supplier_rates", supplier_contract_id=contract, valid_from=VALID_FROM, valid_to=VALID_TO,
                  service_id=guide, unit="group", net_amount=40, currency="USD", created_by=MAKER)  # fmt: skip
    await admin.execute(PUBLISH, contract, CHECKER)
    await admin.execute("UPDATE catalog.v_tour SET duration_days = 5 WHERE source_id = $1", SOURCE_ID)
    await _components(c, cis, rules["room"], rules["service"], guide)
    try:
        yield {**rules, "guide": guide, "guide_co": guide_co}
    finally:
        async with admin.transaction():
            await admin.execute("SET LOCAL ROLE bk_owner")
            await admin.execute("DELETE FROM booking.trip_case_part_waivers")
            await admin.execute("DELETE FROM booking.supplier_bookings")
            await admin.execute("UPDATE booking.b2b_quote_lines SET operable_override_id = NULL")
            await admin.execute("DELETE FROM booking.trip_plan_operable_overrides")
            await admin.execute("DELETE FROM booking.audit_logs WHERE action LIKE 'trip_plan.%'")


async def _components(
    c: httpx.AsyncClient, cis: LocalCis, room: str, transfer: str, guide: str, *, rooms: tuple[int, ...] = (1, 2, 3, 4)
) -> None:
    items = [{"day_no": d, "hotel_room_type_id": room} for d in rooms]
    items += [{"day_no": d, "service_id": transfer} for d in range(1, 6)]
    items.append({"day_no": 3, "service_id": guide})
    await _ok(await c.put(f"/catalog/trip-plans/{SOURCE_ID}/components", json={"items": items},
                          headers=_staff(cis, MAKER)))  # fmt: skip


def _tour(plan_id: str, ref: str = "tour") -> dict[str, Any]:
    return {"line_ref": ref, "component_id": f"tp:{plan_id}", "service_date": DAY1, "pax": {"adult": 2}}


async def _accept_if_on_request(c: httpx.AsyncClient, cis: LocalCis, booking: dict[str, Any]) -> None:
    line = next(x for x in booking["lines"] if x["line_ref"] == "tour")
    if line["status"] == "on_request":
        url = f"/b2b-desk/on-request/{booking['hold_id']}/lines/tour/accept"
        await _ok(await c.post(url, headers=_staff(cis, SALES)))


async def _facts(admin: asyncpg.Connection, case_id: str) -> list[tuple[Any, ...]]:  # noqa: F811
    rows = await admin.fetch(
        "SELECT quote_part_id::text AS part, supplier_id::text AS supplier, service_date, net_amount, quantity,"
        " item_description FROM booking.supplier_bookings WHERE trip_case_id = $1::uuid ORDER BY quote_part_id",
        case_id,
    )
    return [tuple(r) for r in rows]


async def test_a_sold_tour_becomes_one_supplier_booking_per_component(
    client: Client,
    admin: asyncpg.Connection,  # noqa: F811
    tour: dict[str, str],
) -> None:
    c, cis, m2m, _ = client
    sales = _staff(cis, SALES)
    booking = await _book(c, m2m, "SPLIT-1", [_tour(tour["plan"])])
    case_id = booking["booking_id"]
    quote_id = await admin.fetchval("SELECT quote_id::text FROM booking.b2b_holds WHERE id = $1::uuid",
                                    booking["hold_id"])  # fmt: skip
    assert await admin.fetchval("SELECT count(*) FROM booking.b2b_quote_line_parts WHERE quote_id = $1::uuid",
                                quote_id) == 10  # fmt: skip
    gen = f"/trip-cases/{case_id}/supplier-bookings/generate"
    if next(x for x in booking["lines"] if x["line_ref"] == "tour")["status"] == "on_request":
        waiting = await _ok(await c.post(gen, headers=sales))
        assert waiting["created"] == 0
        assert {i["skip_reason"] for i in waiting["unbooked_items"]} == {"line_not_confirmed"}
        await _accept_if_on_request(c, cis, booking)

    made = await _ok(await c.post(gen, headers=sales))
    assert made["created"] == 10
    items = made["items"]
    assert len({i["supplier"]["id"] for i in items}) == 2
    assert all(i["quote_part_id"] and i["status"] == "pending" and i["b2b_line"] is None for i in items)
    by_day = sorted((i["tour_day"], i["service_date"]) for i in items)
    assert by_day[0] == (1, DAY1) and by_day[-1] == (5, (DAY1_DATE + timedelta(days=4)).isoformat())
    rooms = [i for i in items if i["hotel_room_type_id"]]
    assert len(rooms) == 4 and all(r["quantity"] == 1 and r["nights"] == 1 for r in rooms)
    assert all("Double ×1, 1 night" in r["item_description"] for r in rooms)
    assert made["supply"]["items_to_book"] == 10 and made["supply"]["state"] == "confirming"
    before = await _facts(admin, case_id)

    # The tour changes after the sale: nothing sold moves, Generate makes nothing new.
    await _components(c, cis, tour["room"], tour["service"], tour["guide"], rooms=(1, 3))
    again = await _ok(await c.post(gen, headers=sales))
    assert again["created"] == 0 and await _facts(admin, case_id) == before

    # Supply: every part confirmed or waived before pre_departure.
    first, *rest = items
    await _ok(await c.post(f"/supplier-bookings/{first['id']}/cancel", json={"reason": "Not needed"}, headers=sales))
    waive_url = f"/trip-cases/{case_id}/supplier-bookings/parts/{first['quote_part_id']}/waive"
    assert (await c.post(waive_url, json={"reason": "short"}, headers=sales)).status_code == 422
    waived = await _ok(await c.post(waive_url, json={"reason": "The agency dropped this service after the sale."},
                                    headers=sales))  # fmt: skip
    assert waived["supply"]["items_to_book"] == 9
    for item in rest:
        await _ok(await c.post(f"/supplier-bookings/{item['id']}/request", headers=sales))
        await _ok(await c.post(f"/supplier-bookings/{item['id']}/confirm", json={"confirmation_ref": "OK-1"},
                               headers=sales))  # fmt: skip
    page = await _ok(await c.get(f"/trip-cases/{case_id}/supplier-bookings", headers=sales))
    assert page["supply"]["state"] == "booked" and page["supply"]["items_booked"] == 9

    # The agency vouchers list the confirmed components under the tour line.
    vouchers = await _ok(await c.get(f"/b2b/v1/bookings/{case_id}/vouchers", headers=_m2m(m2m)))
    assert len(vouchers["vouchers"]) == 9 and {v["line_ref"] for v in vouchers["vouchers"]} == {"tour"}


async def test_a_component_of_a_supplier_no_longer_active_is_booked_with_another_supplier(
    client: Client,
    admin: asyncpg.Connection,  # noqa: F811
    tour: dict[str, str],
) -> None:
    c, cis, m2m, _ = client
    sales = _staff(cis, SALES)
    booking = await _book(c, m2m, "SWAP-1", [_tour(tour["plan"])])
    case_id = booking["booking_id"]
    await _accept_if_on_request(c, cis, booking)
    # The supplier leaves after the sale (the API refuses while its contract is in force, so set it directly).
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE bk_owner")
        await admin.execute("UPDATE booking.suppliers SET status = 'inactive' WHERE id = $1::uuid", tour["guide_co"])
    made = await _ok(await c.post(f"/trip-cases/{case_id}/supplier-bookings/generate", headers=sales))
    assert made["created"] == 9
    [guide_part] = [i for i in made["unbooked_items"] if i["skip_reason"] == "supplier_not_active"]
    assert guide_part["supplier"]["id"] == tour["guide_co"]

    other_co = await _insert(admin, "suppliers", company_name="Guide Co 1149 B", categories=["guide"],
                             country_code="VN", created_by=MAKER)  # fmt: skip
    other_guide = await _insert(admin, "services", supplier_id=other_co, service_type="guide", title="Hanoi guide B",
                                created_by=MAKER)  # fmt: skip
    url = f"/trip-cases/{case_id}/supplier-bookings"
    part = guide_part["quote_part_id"]
    # The old supplier's service named with the new supplier is refused; the same supplier is still inactive.
    wrong = await c.post(url, json={"supplier_id": other_co, "quote_part_id": part, "service_id": tour["guide"]},
                         headers=sales)  # fmt: skip
    assert wrong.status_code == 422 and wrong.json()["detail"] == "supply_not_of_supplier", wrong.text
    same = await c.post(url, json={"supplier_id": tour["guide_co"], "quote_part_id": part}, headers=sales)
    assert same.status_code == 409 and same.json()["detail"] == "supplier_inactive", same.text

    # Another active supplier with only a description: 201, no room type or service of the old supplier.
    swapped = await _ok(await c.post(url, json={"supplier_id": other_co, "quote_part_id": part,
                                                "item_description": "Hanoi guide, day 3 (replacement)"},
                                     headers=sales), 201)  # fmt: skip
    assert swapped["supplier"]["id"] == other_co and swapped["quote_part_id"] == part
    assert swapped["hotel_room_type_id"] is None and swapped["service_id"] is None
    assert swapped["item_description"] == "Hanoi guide, day 3 (replacement)"
    net = "SELECT net_amount FROM booking.supplier_bookings WHERE id = $1::uuid"
    assert await admin.fetchval(net, swapped["id"]) == 40  # the part's net as sold; staff edit it on the booking

    # Cancelled, then booked again with the new supplier's own service.
    await _ok(await c.post(f"/supplier-bookings/{swapped['id']}/cancel", json={"reason": "Wrong guide"},
                           headers=sales))  # fmt: skip
    again = await _ok(await c.post(url, json={"supplier_id": other_co, "quote_part_id": part,
                                              "service_id": other_guide}, headers=sales), 201)  # fmt: skip
    assert again["service_id"] == other_guide and again["hotel_room_type_id"] is None
    page = await _ok(await c.get(url, headers=sales))
    assert page["supply"]["items_to_book"] == 10 and not page["unbooked_items"]


async def test_an_inactive_supplier_is_not_requested_and_an_inactive_item_is_booked_by_description(
    client: Client,
    admin: asyncpg.Connection,  # noqa: F811
    tour: dict[str, str],
) -> None:
    """Review r2: the guide's service is deactivated after the sale (Generate skips it, item_not_active; booking it with
    the same supplier names a description, not the old service), then its supplier is deactivated while that booking
    is pending: request() refuses it (409 supplier_inactive)."""
    c, cis, m2m, _ = client
    sales = _staff(cis, SALES)
    booking = await _book(c, m2m, "ITEM-1", [_tour(tour["plan"])])
    case_id = booking["booking_id"]
    await _accept_if_on_request(c, cis, booking)
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE bk_owner")
        await admin.execute("UPDATE booking.services SET status = 'inactive' WHERE id = $1::uuid", tour["guide"])
    made = await _ok(await c.post(f"/trip-cases/{case_id}/supplier-bookings/generate", headers=sales))
    assert made["created"] == 9
    [guide_part] = [i for i in made["unbooked_items"] if i["skip_reason"] == "item_not_active"]
    url = f"/trip-cases/{case_id}/supplier-bookings"
    body = {"supplier_id": tour["guide_co"], "quote_part_id": guide_part["quote_part_id"]}
    booked = await _ok(await c.post(url, json=body, headers=sales), 201)
    assert booked["service_id"] is None and booked["item_description"] == guide_part["supply_label"]

    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE bk_owner")
        await admin.execute("UPDATE booking.suppliers SET status = 'inactive' WHERE id = $1::uuid", tour["guide_co"])
    refused = await c.post(f"/supplier-bookings/{booked['id']}/request", headers=sales)
    assert refused.status_code == 409 and refused.json()["detail"] == "supplier_inactive", refused.text
    status = "SELECT status FROM booking.supplier_bookings WHERE id = $1::uuid"
    assert await admin.fetchval(status, booked["id"]) == "pending"


async def test_an_exception_of_the_longest_length_passes_the_table_check(
    client: Client,
    admin: asyncpg.Connection,  # noqa: F811
    tour: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The policy maximum may be 90 days (the bk-065 CHECK's outer bound): a 90-day exception is stored, its expiry
    exactly created_at + 90 days on the transaction clock."""
    c, cis, _, _ = client

    async def policy(_conn: asyncpg.Connection) -> tuple[int, int]:
        return 14, 90

    monkeypatch.setattr(sell_exceptions, "_policy", policy)
    await _components(c, cis, tour["room"], tour["service"], tour["guide"], rooms=(1, 3, 4))
    exc_url = f"/catalog/trip-plans/{SOURCE_ID}/sell-exception"
    body = {"rules": ["N1"], "reason": REASON, "days": 90}
    allowed = await _ok(await c.post(exc_url, json=body, headers=_staff(cis, ADMIN)))
    assert allowed["status"] == "exception"
    assert await admin.fetchval(
        "SELECT expires_at - created_at FROM booking.trip_plan_operable_overrides WHERE revoked_at IS NULL"
    ) == timedelta(days=90)


async def test_a_night_without_a_hotel_blocks_until_an_exception(
    client: Client,
    admin: asyncpg.Connection,  # noqa: F811
    tour: dict[str, str],
) -> None:
    c, cis, m2m, _ = client
    boss, maker = _staff(cis, ADMIN), _staff(cis, MAKER)
    await _components(c, cis, tour["room"], tour["service"], tour["guide"], rooms=(1, 3, 4))
    url = f"/catalog/trip-plans/{SOURCE_ID}/operability"
    check = await _ok(await c.get(url, headers=boss))
    assert check["status"] == "blocked"
    assert [f["key"] for f in check["findings"] if f["level"] == "block"] == ["N1:night:2"]
    assert check["overridable"] == {"N1": ["N1:night:2"]} and check["exception_default_days"] == 14

    body = {"proposal_ref": "GAP-1", "proposal_version": 1, "currency": "USD", "lines": [_tour(tour["plan"])]}
    refused = await c.post("/b2b/v1/quotes", json=body, headers=_m2m(m2m))
    assert refused.status_code == 422, refused.text
    assert refused.json()["message"] == "line tour: this tour cannot be sold to agencies yet: night 2 has no hotel (N1)"

    exc_url = f"/catalog/trip-plans/{SOURCE_ID}/sell-exception"
    assert (await c.post(exc_url, json={"rules": ["N1"], "reason": "too short"}, headers=boss)).status_code == 422
    assert (await c.post(exc_url, json={"rules": ["N1"], "reason": REASON}, headers=maker)).status_code == 403
    assert (await c.post(exc_url, json={"rules": ["N3"], "reason": REASON}, headers=boss)).status_code == 422
    too_long = await c.post(exc_url, json={"rules": ["N1"], "reason": REASON, "days": 31}, headers=boss)
    assert too_long.status_code == 422
    allowed = await _ok(await c.post(exc_url, json={"rules": ["N1"], "reason": REASON}, headers=boss))
    assert allowed["status"] == "exception" and allowed["exception"]["findings"] == ["N1:night:2"]

    booking = await _book(c, m2m, "GAP-2", [_tour(tour["plan"])])
    checks = await admin.fetch(
        "SELECT title FROM booking.trip_case_checks WHERE trip_case_id = $1::uuid AND code = 'custom'",
        booking["booking_id"],
    )
    assert [r["title"] for r in checks] == [
        "Sold under a sale exception: book by hand what is missing (night 2 has no hotel)"
    ]

    # A second exception replaces the first; a revoke ends it and the tour blocks again.
    replaced = await _ok(await c.post(exc_url, json={"rules": ["N1"], "reason": REASON, "days": 3}, headers=boss))
    assert replaced["exception"]["id"] != allowed["exception"]["id"]
    assert (
        await admin.fetchval("SELECT count(*) FROM booking.trip_plan_operable_overrides WHERE revoked_at IS NULL") == 1
    )
    revoked = await _ok(await c.post(f"{exc_url}/revoke", json={"reason": "Hotel contract signed."}, headers=boss))
    assert revoked["status"] == "blocked" and revoked["exception"] is None

    # A night marked "no hotel needed" is tour data, not an exception: contracting may set it, with a reason (stored
    # on the plan and audited), since it lifts N1 with no expiry.
    nights_url = f"/catalog/trip-plans/{SOURCE_ID}/no-hotel-nights"
    why = "Night 2 is on the overnight train to Sapa."
    unexplained = await c.put(nights_url, json={"nights": [2]}, headers=maker)
    assert unexplained.status_code == 422
    nights = await _ok(await c.put(nights_url, json={"nights": [2], "reason": why}, headers=maker))
    assert nights["status"] == "warnings" and nights["no_hotel_nights"] == [2]
    assert nights["no_hotel_nights_reason"] == why
    audited = await admin.fetch(
        "SELECT after->>'reason' AS reason FROM booking.audit_logs WHERE action = 'trip_plan.no_hotel_nights_set'"
    )
    assert [r["reason"] for r in audited] == [why]
    bad = await c.put(nights_url, json={"nights": [5], "reason": why}, headers=maker)
    assert bad.status_code == 422

    report = await _ok(await c.get("/catalog/price-readiness?operable=warnings", headers=boss))
    assert [i["tour_key"] for i in report["items"]] == [SOURCE_ID]
    assert report["items"][0]["operable"]["status"] == "warnings"
    assert report["summary"]["by_operable"]["warnings"] >= 1


async def test_a_missing_rate_is_never_covered(
    client: Client,
    admin: asyncpg.Connection,  # noqa: F811
    tour: dict[str, str],
) -> None:
    c, cis, _, _ = client
    unrated = await _insert(admin, "services", supplier_id=tour["guide_co"], service_type="guide", title="New guide",
                            created_by=MAKER)  # fmt: skip
    await _components(c, cis, tour["room"], tour["service"], unrated)
    check = await _ok(await c.get(f"/catalog/trip-plans/{SOURCE_ID}/operability", headers=_staff(cis, ADMIN)))
    assert check["status"] == "blocked" and check["overridable"] == {}
    assert check["findings"][0]["key"].startswith("N2:day:3:")
    exc = {"rules": ["N1"], "reason": REASON}
    refused = await c.post(f"/catalog/trip-plans/{SOURCE_ID}/sell-exception", json=exc, headers=_staff(cis, ADMIN))
    assert refused.status_code == 422 and refused.json()["detail"] == "rule_not_failing"


async def test_a_fixed_departure_tour_is_not_checked_and_needs_no_component_booking(
    client: Client,
    admin: asyncpg.Connection,  # noqa: F811
    tour: dict[str, str],
) -> None:
    c, cis, m2m, _ = client
    await _components(c, cis, tour["room"], tour["service"], tour["guide"], rooms=(1,))  # nights 2-4 have no hotel
    dep = await _departure(c, cis, DAY1, 10)
    booking = await _book(c, m2m, "DEP-1", [_plan_line("tour", tour["plan"], dep["id"])])
    made = await _ok(await c.post(f"/trip-cases/{booking['booking_id']}/supplier-bookings/generate",
                                  headers=_staff(cis, SALES)))  # fmt: skip
    assert made["created"] == 0 and made["supply"]["state"] == "none" and made["unbooked_items"] == []
