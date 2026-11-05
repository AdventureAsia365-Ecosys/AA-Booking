"""AA-1030 G3 on a real PostgreSQL, as role ``booking``: the agency tariff equals what ``POST /quotes`` charges.

Three invented agencies (standard, silver with a catalogue rate card, gold with a plan rate card) read their tariff of
one invented tour (the price matrix the cost sheet computes from the plan's components); every row of every tariff is
then quoted through the B2B API for the row's party and day, and the quote line's ``b2b_price``, discount and rate card
must be the tariff's. The agency read (``GET /b2b/v1/tariff``, JSON and CSV) is the staff read without the staff-only
fields, and two agencies on different tiers get different tariffs.
"""

from __future__ import annotations

import csv
import io
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal as D
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio

from tests.integration.test_aa1030_tiers import TENANT_GOLD, TENANT_SILVER, _m2m, tiers_clean  # noqa: F401
from tests.integration.test_bk019_b2b import (  # noqa: F401 — fixtures used by name
    ADMIN,
    CHECKER,
    MAKER,
    SALES,
    SOURCE_ID,
    _ok,
    _staff,
    admin,
    client,
    supply,
)
from tests.staff_tokens import CRM_CLIENT, LocalCis, LocalM2M

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

TENANT_STANDARD = "e2000000-0000-4000-8000-000000103003"
TODAY = datetime.now(UTC).date()


@pytest_asyncio.fixture
async def matrix_clean(admin: asyncpg.Connection) -> AsyncIterator[None]:  # noqa: F811
    """The plan's matrix and its dated history (test_bk019_b2b's clean-up deletes the plan, which they reference)."""
    yield
    for table in ("trip_plan_price_history", "trip_plan_prices"):
        await admin.execute(
            f"DELETE FROM booking.{table} WHERE trip_plan_id IN"  # noqa: S608 — fixed table names
            " (SELECT id FROM booking.trip_plans WHERE source_id = $1)",
            SOURCE_ID,
        )


async def _card(c: httpx.AsyncClient, cis: LocalCis, body: dict[str, Any]) -> str:
    card = await _ok(await c.post("/pricing/partner-rate-cards", json=body, headers=_staff(cis, MAKER)), 201)
    await _ok(await c.post(f"/pricing/partner-rate-cards/{card['id']}/approve", headers=_staff(cis, CHECKER)))
    return str(card["id"])


async def _quote_row(
    c: httpx.AsyncClient, m2m: LocalM2M, tenant: str, plan_id: str, item: dict[str, Any], currency: str
) -> dict[str, Any]:
    line = {
        "line_ref": "plan",
        "component_id": f"tp:{plan_id}",
        "service_date": item["priced_for_date"],
        "pax": {"adult": item["priced_for_pax"]},
    }
    request = {"proposal_ref": f"TAR-{tenant[-4:]}", "proposal_version": 1, "currency": currency, "lines": [line]}
    return await _ok(await c.post("/b2b/v1/quotes", json=request, headers=_m2m(m2m, tenant)), 201)


async def test_the_tariff_is_what_a_quote_charges(
    client: tuple[httpx.AsyncClient, LocalCis, LocalM2M],  # noqa: F811
    admin: asyncpg.Connection,  # noqa: F811
    supply: dict[str, str],  # noqa: F811
    tiers_clean: None,  # noqa: F811
    matrix_clean: None,
) -> None:
    c, cis, m2m = client
    boss, sales, maker = _staff(cis, ADMIN), _staff(cis, SALES), _staff(cis, MAKER)

    agencies = {
        TENANT_STANDARD: ("Invented Kestrel Tours", "standard"),
        TENANT_SILVER: ("Invented Lotus Travel", "silver"),
        TENANT_GOLD: ("Invented Heron Journeys", "gold"),
    }
    ids: dict[str, str] = {}
    for tenant, (name, tier) in agencies.items():
        body = {"tenant_id": tenant, "agency_name": name, "m2m_client_id": CRM_CLIENT}
        ids[tenant] = (await _ok(await c.post("/b2b-partners", json=body, headers=boss), 201))["id"]
        await _ok(await c.patch(f"/pricing/partner-terms/{ids[tenant]}", json={"tier_code": tier}, headers=boss))

    commercial = {"sellable": True, "price_hint": {"amount": "150.00", "currency": "USD", "basis": "per_person"}}
    await _ok(await c.put(f"/catalog/trip-plans/{SOURCE_ID}/commercial", json=commercial, headers=maker))
    items = {
        "items": [{"day_no": 1, "hotel_room_type_id": supply["room"]}, {"day_no": 1, "service_id": supply["service"]}]
    }
    plan_id = (await _ok(await c.put(f"/catalog/trip-plans/{SOURCE_ID}/components", json=items, headers=maker)))[
        "trip_plan_id"
    ]
    await _ok(await c.post(f"/catalog/trip-plans/{SOURCE_ID}/prices/recompute", headers=maker))

    # Contract rate cards: the silver agency 8 % on the whole catalogue, the gold agency 10 % on this tour.
    since = TODAY.isoformat()
    silver_card = await _card(
        c, cis, {"partner_id": ids[TENANT_SILVER], "source_id": None, "discount_pct": "0.08", "valid_from": since}
    )
    gold_card = await _card(
        c, cis, {"partner_id": ids[TENANT_GOLD], "source_id": SOURCE_ID, "discount_pct": "0.1", "valid_from": since}
    )
    expected = {
        TENANT_STANDARD: (D("0"), None),
        TENANT_SILVER: (D("0.08"), silver_card),
        TENANT_GOLD: (D("0.1"), gold_card),
    }

    url = "/pricing/partner-tariffs/{}"
    assert (await c.get(url.format(ids[TENANT_GOLD]), headers=_m2m(m2m, TENANT_GOLD))).status_code == 401
    tariffs: dict[str, dict[str, Any]] = {}
    for tenant in agencies:
        tariffs[tenant] = await _ok(await c.get(url.format(ids[tenant]), headers=sales))
    # The cost sheet's six pax bands of the 2027 window, all quotable (a room and a transfer with rates in force).
    standard = tariffs[TENANT_STANDARD]
    assert [(i["pax_min"], i["pax_max"]) for i in standard["items"]] == [
        (1, 1), (2, 2), (3, 4), (5, 6), (7, 10), (11, None)
    ]  # fmt: skip
    assert standard["unpriced"] == [] and standard["truncated"] is False
    assert standard["partner"]["tier_code"] == "standard" and standard["basis"] == "per_person_band_min_twin_share"

    # Every row of every tariff = the quote of that row's party on that day (price, discount, rate card).
    for tenant, tariff in tariffs.items():
        discount, card = expected[tenant]
        for item in tariff["items"]:
            assert item["priced_for_date"] == "2027-01-01"
            assert D(item["agency_discount_pct"]) == discount and item["rate_card_id"] == card
            quote = await _quote_row(c, m2m, tenant, plan_id, item, "USD")
            assert quote["lines"][0]["b2b_price"] == item["price"], (tenant, item)
            stored = await admin.fetchrow(
                "SELECT agency_discount_pct, rate_card_id::text AS card FROM booking.b2b_quote_lines"
                " WHERE quote_id = $1::uuid",
                quote["quote_id"],
            )
            assert (stored["agency_discount_pct"], stored["card"]) == (discount, card)
            party = item["priced_for_pax"]
            per_person = D(item["price_per_person"]["amount"])
            assert per_person * party >= D(item["price"]["amount"]) > (per_person - D("0.01")) * party

    # Two partners on different tiers: different tariffs, the bigger discount the lower price, row by row.
    prices = {t: [D(i["price"]["amount"]) for i in tariffs[t]["items"]] for t in agencies}
    assert all(
        s > v > g for s, v, g in zip(prices[TENANT_STANDARD], prices[TENANT_SILVER], prices[TENANT_GOLD], strict=True)
    )

    # A VND tariff is a VND quote too (lines rounded up to 1,000 VND).
    vnd = await _ok(await c.get(url.format(ids[TENANT_SILVER]), params={"currency": "VND"}, headers=sales))
    pair = vnd["items"][1]
    assert pair["price"]["currency"] == "VND" and D(pair["price"]["amount"]) % 1000 == 0
    quote = await _quote_row(c, m2m, TENANT_SILVER, plan_id, pair, "VND")
    assert quote["lines"][0]["b2b_price"] == pair["price"]
    assert (await c.get(url.format(ids[TENANT_SILVER]), params={"currency": "XXX"}, headers=sales)).status_code == 422

    # The agency's own read: the same rows without the staff-only fields; JSON and CSV.
    mine = await _ok(await c.get("/b2b/v1/tariff", headers=_m2m(m2m, TENANT_GOLD)))
    assert mine["tier_code"] == "gold" and "unpriced" not in mine and "partner" not in mine
    staff_gold = tariffs[TENANT_GOLD]["items"]
    assert mine["items"] == [{k: v for k, v in i.items() if k != "rate_card_id"} for i in staff_gold]
    one = await _ok(await c.get("/b2b/v1/tariff", params={"source_id": "no-such-tour"}, headers=_m2m(m2m, TENANT_GOLD)))
    assert one["items"] == []

    res = await c.get("/b2b/v1/tariff", params={"format": "csv"}, headers=_m2m(m2m, TENANT_SILVER))
    assert res.status_code == 200 and res.headers["content-type"].startswith("text/csv")
    assert 'filename="aa-tariff-invented-lotus-travel-usd-' in res.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(res.text)))
    assert [r["price_for_party"] for r in rows] == [i["price"]["amount"] for i in tariffs[TENANT_SILVER]["items"]]
    assert {r["tour_key"] for r in rows} == {SOURCE_ID} and rows[0]["title"] == "Hanoi 2D"
    assert rows[-1]["pax_max"] == "" and rows[0]["agency_discount_pct"] == "0.0800"
    staff_csv = await c.get(url.format(ids[TENANT_SILVER]), params={"format": "csv"}, headers=sales)
    assert staff_csv.text == res.text  # staff send the agency exactly the file it would download

    # A plan taken off sale leaves every tariff.
    await admin.execute("UPDATE booking.trip_plans SET sellable = false WHERE id = $1::uuid", plan_id)
    assert (await _ok(await c.get(url.format(ids[TENANT_GOLD]), headers=sales)))["items"] == []
