"""AA-1153 part 1 end to end on a real PostgreSQL (role ``booking``), the Done-when of the issue:

- one hotel with contracts of two DMCs: the second DMC is linked with its own code, its room name is an alias of the
  AA room type, and its "published 70 USD, 20 % commission" sheet imports as 56.00 USD net with both source numbers
  kept (B7), with a season sheet and a child price; the dry run shows the diff and no warning;
- a 3-night half-board quote for 2 adults and a 7-year-old in one double room costs 3 x 56 + 3 x 15 = 213 USD net
  (the cheaper DMC's HB rate, never the cheaper BB rate, plus the DMC's child price for the child the room does not
  hold); the hotel's preferred DMC wins when one is set;
- the booking goes to the DMC of the price used; its hotel confirmation number is kept next to the DMC's ref;
- a duplicate hotel is refused (409 with the candidate), a near one asks for a reason;
- an import whose prices jump against the DMC's previous contract is flagged and its publisher must acknowledge;
- (review fixes) a room line keeps one DMC for all its nights even when another DMC is cheaper on a later night, and
  DMCs in different currencies are compared in USD at the approved FX rate.

Reuses the AA-973 fixtures (``admin``, ``client``, ``plan``). All names are invented."""

from __future__ import annotations

import base64
import json
from datetime import timedelta
from decimal import Decimal
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio

from app.db import Database
from app.supply import rate_sheet
from tests.integration.test_bk020_b2b import (  # noqa: F401 — fixtures admin, client, plan are used by name
    ADMIN,
    CHECKER,
    DAY1,
    DAY1_DATE,
    MAKER,
    PUBLISH,
    SALES,
    VALID_FROM,
    VALID_TO,
    _insert,
    _m2m,
    _ok,
    _staff,
    admin,
    client,
    plan,
)
from tests.integration.test_bk022_b2b import _book
from tests.staff_tokens import LocalCis, LocalM2M

FIXTURES = (admin, client, plan)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

Client = tuple[httpx.AsyncClient, LocalCis, LocalM2M, Database]
LAT, LON = "21.033000", "105.850000"


@pytest_asyncio.fixture
async def lodge(admin: asyncpg.Connection, client: Client, plan: dict[str, str]) -> dict[str, str]:  # noqa: F811
    """Lotus Bay Lodge, first listed by Lantern DMC (BB 50 / HB 65 USD a double night, published), and a second DMC,
    Harbour DMC, that sells it too but has no contract yet."""
    c, cis, _, _ = client
    a = await _insert(admin, "suppliers", company_name="Lantern DMC", categories=["hotel"], country_code="VN",
                      created_by=MAKER)  # fmt: skip
    b = await _insert(admin, "suppliers", company_name="Harbour DMC", categories=["hotel"], country_code="VN",
                      created_by=MAKER)  # fmt: skip
    boss = _staff(cis, ADMIN)
    hotel = await _ok(
        await c.post(
            "/hotels",
            json={"supplier_id": a, "name": "Lotus Bay Lodge", "country_code": "VN", "latitude": LAT, "longitude": LON},
            headers=boss,
        ),  # fmt: skip
        201,
    )
    room = await _ok(
        await c.post(f"/hotels/{hotel['id']}/room-types", json={"name": "Deluxe", "max_occupancy": 2}, headers=boss),
        201,
    )
    contract = await _insert(admin, "supplier_contracts", supplier_id=a, title="Lantern 2027", valid_from=VALID_FROM,
                             valid_to=VALID_TO, currency="USD", created_by=MAKER)  # fmt: skip
    for board, net in (("BB", 50), ("HB", 65)):
        await _insert(admin, "supplier_rates", supplier_contract_id=contract, valid_from=VALID_FROM,
                      valid_to=VALID_TO, hotel_room_type_id=room["id"], unit="room_night", meal_plan=board,
                      net_amount=net, currency="USD", created_by=MAKER)  # fmt: skip
    await admin.execute(PUBLISH, contract, CHECKER)
    return {"a": a, "b": b, "hotel": hotel["id"], "hotel_code": hotel["code"], "room": room["id"]}


def _upload(data: bytes, name: str) -> dict[str, Any]:
    return {"data_base64": base64.b64encode(data).decode(), "filename": name}


def _harbour_sheet(*, title: str, hb: tuple[str, str], bb: tuple[str, str], child: str = "15") -> bytes:
    contract = {"Title": title, "Valid from": VALID_FROM.isoformat(), "Valid to": VALID_TO.isoformat(),
                "Currency": "USD"}  # fmt: skip
    # Hotel code, Room type, Season, Valid from, Valid to, Meal plan, Pax min, Pax max, Net, Notes, Published, Comm %
    rows = [
        ("HB-LOTUS", "Deluxe Bay View", "All year", None, None, "HB", None, None, None, None, *hb),
        ("HB-LOTUS", "Deluxe Bay View", "All year", None, None, "BB", None, None, None, None, *bb),
    ]
    return rate_sheet.build_workbook(
        contract,
        rows,
        season_rows=[("All year", VALID_FROM.isoformat(), VALID_TO.isoformat())],
        child_rows=[("HB-LOTUS", 6, 11, "sharing", "HB", child)],
    )


async def _harbour_contract(c: httpx.AsyncClient, cis: LocalCis, lodge: dict[str, str]) -> dict[str, Any]:
    maker = _staff(cis, MAKER)
    link = {"supplier_id": lodge["b"], "supplier_hotel_code": "HB-LOTUS"}
    linked = await _ok(await c.post(f"/hotels/{lodge['hotel']}/suppliers", json=link, headers=maker), 201)
    assert {x["supplier_id"] for x in linked["items"]} == {lodge["a"], lodge["b"]}
    alias = {"alias": "Deluxe Bay View", "supplier_id": lodge["b"]}
    await _ok(await c.post(f"/room-types/{lodge['room']}/aliases", json=alias, headers=maker), 201)
    sheet = _harbour_sheet(title="Harbour 2027", hb=("70", "20"), bb=("60", "20"))
    url = f"/suppliers/{lodge['b']}/rate-sheets"
    preview = await _ok(await c.post(f"{url}?dry_run=true", json=_upload(sheet, "harbour-2027.xlsx"), headers=maker))
    assert [(r["meal_plan"], r["net_amount"]) for r in preview["rates"]] == [("HB", "56.00"), ("BB", "48.00")]
    assert preview["checks"] == [] and len(preview["diff"]["added"]) == 2
    assert preview["diff"]["previous_contract"] is None
    done = await _ok(await c.post(url, json=_upload(sheet, "harbour-2027.xlsx"), headers=maker))
    assert done["imported"] == 2
    return done["contract"]


async def test_done_when_two_dmcs_net_from_commission_hb_quote_and_booking_to_the_dmc(
    client: Client,
    admin: asyncpg.Connection,  # noqa: F811
    lodge: dict[str, str],
) -> None:
    c, cis, m2m, _ = client
    maker, checker, sales = _staff(cis, MAKER), _staff(cis, CHECKER), _staff(cis, SALES)
    contract = await _harbour_contract(c, cis, lodge)
    k = contract["id"]

    # B7: the commissionable row is stored net, both source numbers kept.
    stored = await admin.fetchrow(
        "SELECT net_amount, published_amount, commission_pct, season, valid_from, valid_to FROM booking.supplier_rates"
        " WHERE supplier_contract_id = $1::uuid AND meal_plan = 'HB'",
        k,
    )
    assert (stored["net_amount"], stored["published_amount"], stored["commission_pct"]) == (
        Decimal("56.00"),
        Decimal("70.00"),
        Decimal("20.00"),
    )
    assert (stored["season"], stored["valid_from"], stored["valid_to"]) == ("All year", VALID_FROM, VALID_TO)
    review = await _ok(await c.get(f"/contracts/{k}/review", headers=sales))
    assert review["acknowledge"] is False and review["import"]["file_name"] == "harbour-2027.xlsx"
    assert [(s["name"]) for s in review["seasons"]] == ["All year"]
    assert [(p["age_min"], p["age_max"], p["net_amount"]) for p in review["child_prices"]] == [(6, 11, "15.00")]
    mine = await c.patch(f"/contracts/{k}", json={"status": "published"}, headers=maker)
    assert mine.status_code == 403 and mine.json()["detail"] == "maker_checker"
    await _ok(await c.patch(f"/contracts/{k}", json={"status": "published"}, headers=checker))

    # The quote: 3 nights half board, 2 adults and a 7-year-old in one double room.
    line = {
        "line_ref": "lodge",
        "component_id": f"rt:{lodge['room']}",
        "service_date": DAY1,
        "nights": 3,
        "pax": {"adult": 2, "child": 1},
        "rooms": [{"occupancy": "double", "count": 1}],
        "meal_plan": "HB",
        "child_ages": [7],
    }
    booking = await _book(c, m2m, "LOTUS-1", [line])
    quote_id = await admin.fetchval("SELECT quote_id::text FROM booking.b2b_holds WHERE id = $1::uuid",
                                    booking["hold_id"])  # fmt: skip
    row = await admin.fetchrow(
        "SELECT net_cost_usd, price_note::text AS note, supplier_rate_ids::text[] AS rate_ids"
        " FROM booking.b2b_quote_lines WHERE quote_id = $1::uuid",
        quote_id,
    )
    assert row["net_cost_usd"] == Decimal("213.0000")  # 3 x 56 + 3 x 15
    note = json.loads(row["note"])
    # The double (max 2) is full: the child is on an extra bed; Harbour gives a sharing price only, which is taken.
    assert note["board"] == {"meal_plan": "HB"}
    assert note["child_room"] == {"children": 1, "priced": 1, "unpriced": 0, "extra_bed": 1}
    harbour_hb = await admin.fetchval(
        "SELECT id::text FROM booking.supplier_rates WHERE supplier_contract_id = $1::uuid AND meal_plan = 'HB'", k
    )
    assert set(row["rate_ids"]) == {harbour_hb}

    # The booking goes to the DMC of the price used, with the hotel's own confirmation number.
    case = booking["booking_id"]
    accept = f"/b2b-desk/on-request/{booking['hold_id']}/lines/lodge/accept"
    if booking["lines"][0]["status"] == "on_request":
        made = await _ok(await c.post(f"/trip-cases/{case}/supplier-bookings/generate", headers=sales))
        if not made["created"]:
            await _ok(await c.post(accept, headers=sales))
            made = await _ok(await c.post(f"/trip-cases/{case}/supplier-bookings/generate", headers=sales))
    else:
        made = await _ok(await c.post(f"/trip-cases/{case}/supplier-bookings/generate", headers=sales))
    [sb] = [i for i in made["items"] if i["hotel_room_type_id"] == lodge["room"]]
    assert sb["supplier"]["id"] == lodge["b"]
    await _ok(await c.post(f"/supplier-bookings/{sb['id']}/request", headers=sales))
    done = await _ok(await c.post(
        f"/supplier-bookings/{sb['id']}/confirm",
        json={"confirmation_ref": "HB-889", "hotel_confirmation_ref": "LBL-55127"},
        headers=_staff(cis, ADMIN),
    ))  # fmt: skip
    assert (done["confirmation_ref"], done["hotel_confirmation_ref"]) == ("HB-889", "LBL-55127")

    # The hotel's preferred DMC wins over a cheaper one (same board).
    await _ok(await c.patch(f"/hotels/{lodge['hotel']}/suppliers/{lodge['a']}", json={"is_preferred": True},
                            headers=maker))  # fmt: skip
    body = {"proposal_ref": "LOTUS-2", "proposal_version": 1, "currency": "USD", "lines": [{**line, "line_ref": "p"}]}
    quote = await _ok(await c.post("/b2b/v1/quotes", json=body, headers=_m2m(m2m)), 201)
    preferred = await admin.fetchval(
        "SELECT net_cost_usd FROM booking.b2b_quote_lines WHERE quote_id = $1::uuid", quote["quote_id"]
    )
    assert preferred == Decimal("195.0000")  # 3 x 65; Lantern gives no child price: the child is not charged
    # No board asked: BB is the default, never the cheapest board of any kind.
    plain = {**line, "line_ref": "bb", "meal_plan": None, "child_ages": None, "pax": {"adult": 2}, "rooms": []}
    body = {"proposal_ref": "LOTUS-3", "proposal_version": 1, "currency": "USD", "lines": [plain]}
    quote = await _ok(await c.post("/b2b/v1/quotes", json=body, headers=_m2m(m2m)), 201)
    bb = await admin.fetchval("SELECT net_cost_usd FROM booking.b2b_quote_lines WHERE quote_id = $1::uuid",
                              quote["quote_id"])  # fmt: skip
    assert bb == Decimal("150.0000")  # 3 x 50, the preferred DMC's BB


async def test_duplicate_hotels_and_the_facts_confirmation(
    client: Client,
    admin: asyncpg.Connection,  # noqa: F811
    lodge: dict[str, str],
) -> None:
    c, cis, _, _ = client
    boss, sales = _staff(cis, ADMIN), _staff(cis, SALES)
    twin = {"supplier_id": lodge["b"], "name": "The Lotus Bay Lodge Hotel", "country_code": "VN",
            "latitude": "21.033100", "longitude": "105.850100"}  # fmt: skip
    refused = await c.post("/hotels", json=twin, headers=boss)
    assert refused.status_code == 409 and refused.json()["detail"] == "hotel_duplicate", refused.text
    assert refused.json()["candidates"][0]["code"] == lodge["hotel_code"]
    near = {**twin, "name": "Lotus Bay Garden", "latitude": "21.036000", "longitude": "105.850000"}
    asked = await c.post("/hotels", json=near, headers=boss)
    assert asked.status_code == 409 and asked.json()["detail"] == "hotel_possible_duplicate", asked.text
    made = await _ok(await c.post("/hotels", json={**near, "not_duplicate_reason": "Another building, opened 2026"},
                                  headers=boss), 201)  # fmt: skip
    assert made["name"] == "Lotus Bay Garden"
    preview = await _ok(await c.get("/hotels/duplicates", params={"name": "Lotus Bay Lodge", "country_code": "VN",
                                                                  "latitude": LAT, "longitude": LON},
                                    headers=sales))  # fmt: skip
    assert preview["items"][0]["verdict"] == "block"

    due = await _ok(await c.get("/hotels", params={"reconfirm": "due", "q": "Lotus"}, headers=sales))
    assert {h["code"] for h in due["items"]} >= {lodge["hotel_code"]}
    confirm = {"source": "dmc_sheet", "supplier_id": lodge["a"]}
    detail = await _ok(await c.post(f"/hotels/{lodge['hotel']}/facts-confirmation", json=confirm, headers=boss))
    assert detail["facts_source"] == "dmc_sheet" and detail["facts_reconfirm_due"] is not None
    due = await _ok(await c.get("/hotels", params={"reconfirm": "due", "q": "Lotus Bay Lodge"}, headers=sales))
    assert lodge["hotel_code"] not in {h["code"] for h in due["items"]}
    refused = await c.post(f"/hotels/{lodge['hotel']}/facts-confirmation", json=confirm, headers=sales)
    assert refused.status_code == 403


async def test_a_price_jump_is_flagged_and_acknowledged_by_the_publisher(
    client: Client,
    admin: asyncpg.Connection,  # noqa: F811
    lodge: dict[str, str],
) -> None:
    c, cis, _, _ = client
    maker, checker = _staff(cis, MAKER), _staff(cis, CHECKER)
    first = await _harbour_contract(c, cis, lodge)
    await _ok(await c.patch(f"/contracts/{first['id']}", json={"status": "published"}, headers=checker))
    # Next year's sheet: HB 70 -> 120 published (+71 %), BB cheaper than HB still; the season covers only half a year.
    later_from = VALID_FROM + timedelta(days=1)
    sheet = rate_sheet.build_workbook(
        {
            "Title": "Harbour 2028",
            "Valid from": later_from.isoformat(),
            "Valid to": VALID_TO.isoformat(),
            "Currency": "USD",
        },  # fmt: skip
        [
            ("HB-LOTUS", "Deluxe Bay View", "All year", None, None, "HB", None, None, None, None, "120", "20"),
            ("HB-LOTUS", "Deluxe Bay View", "All year", None, None, "BB", None, None, None, None, "130", "20"),
        ],
        season_rows=[("All year", later_from.isoformat(), (DAY1_DATE + timedelta(days=60)).isoformat())],
    )
    preview = await _ok(await c.post(f"/suppliers/{lodge['b']}/rate-sheets?dry_run=true",
                                     json=_upload(sheet, "harbour-2028.xlsx"), headers=maker))  # fmt: skip
    codes = sorted({f["code"] for f in preview["checks"]})
    assert codes == ["board_order", "price_jump", "season_gap"], preview["checks"]
    assert preview["diff"]["previous_contract"]["id"] == first["id"]
    done = await _ok(await c.post(f"/suppliers/{lodge['b']}/rate-sheets", json=_upload(sheet, "harbour-2028.xlsx"),
                                  headers=maker))  # fmt: skip
    k = done["contract"]["id"]
    blocked = await c.patch(f"/contracts/{k}", json={"status": "published"}, headers=checker)
    assert blocked.status_code == 409 and blocked.json()["detail"] == "checks_unacknowledged", blocked.text
    stored = json.loads(await admin.fetchval("SELECT checks::text FROM booking.rate_sheet_imports"
                                             " WHERE supplier_contract_id = $1::uuid", k))  # fmt: skip
    assert {f["code"] for f in stored} == {"board_order", "price_jump", "season_gap"}
    body = {"status": "published", "acknowledge_checks": True}
    assert (await _ok(await c.patch(f"/contracts/{k}", json=body, headers=checker)))["status"] == "published"
    # Season overlaps are errors: nothing is imported.
    overlap = rate_sheet.build_workbook(
        {
            "Title": "Harbour bad",
            "Valid from": VALID_FROM.isoformat(),
            "Valid to": VALID_TO.isoformat(),
            "Currency": "USD",
        },  # fmt: skip
        [("HB-LOTUS", "Deluxe", "Low", None, None, "BB", None, None, "40", None)],
        season_rows=[
            ("Low", VALID_FROM.isoformat(), VALID_TO.isoformat()),
            ("High", VALID_FROM.isoformat(), (VALID_FROM + timedelta(days=9)).isoformat()),
        ],  # fmt: skip
    )
    bad = await c.post(f"/suppliers/{lodge['b']}/rate-sheets", json=_upload(overlap, "bad.xlsx"), headers=maker)
    assert bad.status_code == 422 and "season_overlap" in {e["code"] for e in bad.json()["errors"]}, bad.text


async def _second_dmc(admin: asyncpg.Connection, lodge: dict[str, str], *, currency: str, net: int, start: Any) -> str:
    """Harbour DMC linked to the lodge with a published BB contract in ``currency`` from ``start``."""
    await admin.execute(
        "INSERT INTO booking.hotel_supplier_links (hotel_id, supplier_id, created_by) VALUES ($1::uuid, $2::uuid, $3)"
        " ON CONFLICT DO NOTHING",
        lodge["hotel"],
        lodge["b"],
        MAKER,
    )
    k = await _insert(admin, "supplier_contracts", supplier_id=lodge["b"], title=f"Harbour {currency}",
                      valid_from=VALID_FROM, valid_to=VALID_TO, currency=currency, created_by=MAKER)  # fmt: skip
    rate = await _insert(admin, "supplier_rates", supplier_contract_id=k, valid_from=start, valid_to=VALID_TO,
                         hotel_room_type_id=lodge["room"], unit="room_night", meal_plan="BB", net_amount=net,
                         currency=currency, created_by=MAKER)  # fmt: skip
    await admin.execute(PUBLISH, k, CHECKER)
    return rate


async def test_a_room_line_keeps_the_dmc_of_its_first_night(
    client: Client,
    admin: asyncpg.Connection,  # noqa: F811
    lodge: dict[str, str],
) -> None:
    c, _, m2m, _ = client
    # Harbour sells the BB double at 40 from night 2 only; Lantern sells it at 50 every night.
    harbour = await _second_dmc(admin, lodge, currency="USD", net=40, start=DAY1_DATE + timedelta(days=1))
    line = {"line_ref": "r", "component_id": f"rt:{lodge['room']}", "service_date": DAY1, "nights": 3,
            "pax": {"adult": 2}}  # fmt: skip
    body = {"proposal_ref": "LOTUS-DMC", "proposal_version": 1, "currency": "USD", "lines": [line]}
    quote = await _ok(await c.post("/b2b/v1/quotes", json=body, headers=_m2m(m2m)), 201)
    row = await admin.fetchrow(
        "SELECT net_cost_usd, supplier_rate_ids::text[] AS rate_ids FROM booking.b2b_quote_lines"
        " WHERE quote_id = $1::uuid",
        quote["quote_id"],
    )
    assert row["net_cost_usd"] == Decimal("150.0000")  # 3 x 50 from Lantern, never 50 + 40 + 40 across two DMCs
    assert harbour not in row["rate_ids"]
    owners = await admin.fetch(
        "SELECT DISTINCT k.supplier_id::text AS s FROM booking.supplier_rates x"
        " JOIN booking.supplier_contracts k ON k.id = x.supplier_contract_id WHERE x.id = ANY($1::uuid[])",
        row["rate_ids"],
    )
    assert [r["s"] for r in owners] == [lodge["a"]]


async def test_dmcs_in_different_currencies_are_compared_in_usd(
    client: Client,
    admin: asyncpg.Connection,  # noqa: F811
    lodge: dict[str, str],
) -> None:
    c, _, m2m, _ = client
    # Harbour: 1,000,000 VND = 40 USD at the approved 25,000 VND/USD of the plan fixture; Lantern: 50 USD. A raw
    # compare of the two nets would take Lantern's 50.
    harbour = await _second_dmc(admin, lodge, currency="VND", net=1_000_000, start=VALID_FROM)
    line = {"line_ref": "r", "component_id": f"rt:{lodge['room']}", "service_date": DAY1, "nights": 1,
            "pax": {"adult": 2}}  # fmt: skip
    body = {"proposal_ref": "LOTUS-FX", "proposal_version": 1, "currency": "USD", "lines": [line]}
    quote = await _ok(await c.post("/b2b/v1/quotes", json=body, headers=_m2m(m2m)), 201)
    row = await admin.fetchrow(
        "SELECT net_cost_usd, supplier_rate_ids::text[] AS rate_ids FROM booking.b2b_quote_lines"
        " WHERE quote_id = $1::uuid",
        quote["quote_id"],
    )
    assert row["rate_ids"] == [harbour] and row["net_cost_usd"] == Decimal("40.0000")
