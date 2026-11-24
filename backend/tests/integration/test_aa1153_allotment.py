"""AA-1153 part 2 end to end on a real PostgreSQL (role ``booking``), the rest of the issue's Done-when:

- a DMC's rate sheet carries an Inventory row "allotment 2 rooms, release days empty": the import gives it the pricing
  policy default (7 days) and the second person publishes it with the rates;
- quotes on the room type of that DMC: 2 rooms are ``available``, a 3rd room (3 in one line) is ``on_request``
  (``allotment_full``); after an agency books 2 rooms (held available, confirmed at once, no desk line) one more room
  is on request; a night inside the 7 release days is on request (``released``); the other DMC's room on the same
  hotel is not counted against this allotment;
- the B2B desk on-request list names the DMC whose rate priced the line, not the hotel's first DMC;
- the contract exports as a rate sheet (rates with both B7 source numbers, seasons, child prices, inventory) that,
  edited, imports back as a new draft with the diff against the published contract.

Reuses the AA-973 fixtures (``admin``, ``client``, ``plan``) and the AA-1153 part 1 ``lodge``. Names are invented."""

from __future__ import annotations

import io
import json
from datetime import timedelta
from typing import Any

import asyncpg
import pytest
from openpyxl import load_workbook

from app.supply import rate_sheet
from tests.integration.test_aa1153_hotel_contracts import (  # noqa: F401 — fixture lodge is used by name
    Client,
    _harbour_contract,
    _upload,
    lodge,
)
from tests.integration.test_bk020_b2b import (  # noqa: F401 — fixtures admin, client, plan are used by name
    CHECKER,
    DAY1,
    DAY1_DATE,
    MAKER,
    SALES,
    TODAY,
    VALID_FROM,
    VALID_TO,
    _m2m,
    _ok,
    _quote,
    _staff,
    admin,
    client,
    plan,
)
from tests.integration.test_bk022_b2b import _book

FIXTURES = (admin, client, plan, lodge)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ALLOTMENT_ROW = ("HB-LOTUS", "Deluxe Bay View", None, "allotment", VALID_FROM, VALID_TO, 2, None, None, "Two rooms")


def _sheet(*, published: str = "70") -> bytes:
    contract = {"Title": "Harbour 2027 allotment", "Valid from": VALID_FROM.isoformat(),
                "Valid to": VALID_TO.isoformat(), "Currency": "USD"}  # fmt: skip
    rows = [("HB-LOTUS", "Deluxe Bay View", "All year", None, None, "HB", None, None, None, None, published, "20")]
    return rate_sheet.build_workbook(
        contract,
        rows,
        season_rows=[("All year", VALID_FROM.isoformat(), VALID_TO.isoformat())],
        child_rows=[("HB-LOTUS", 6, 11, "sharing", "HB", "15")],
        inventory_rows=[ALLOTMENT_ROW],
    )


def _line(room: str, rooms: int, day: str = DAY1, ref: str = "stay") -> dict[str, Any]:
    return {
        "line_ref": ref,
        "component_id": f"rt:{room}",
        "service_date": day,
        "nights": 2,
        "pax": {"adult": 2 * rooms},
        "rooms": [{"occupancy": "double", "count": rooms}],
        "meal_plan": "HB",
    }


async def _availability(c: Any, m2m: Any, ref: str, lines: list[dict[str, Any]]) -> list[str]:
    quote = await _ok(await _quote(c, m2m, ref, lines), 201)
    return [line["availability"] for line in quote["lines"]]


async def _note(admin: asyncpg.Connection, ref: str) -> dict[str, Any]:
    raw = await admin.fetchval(
        "SELECT l.price_note::text FROM booking.b2b_quote_lines l JOIN booking.b2b_quotes q ON q.id = l.quote_id"
        " WHERE q.proposal_ref = $1",  # one quote per proposal ref in these tests
        ref,
    )
    return json.loads(raw or "{}")


async def _allotment_contract(c: Any, cis: Any, admin: asyncpg.Connection, lodge: dict[str, str]) -> str:  # noqa: F811
    """Harbour (DMC B) imports a sheet with an allotment row (release days empty) and a second person publishes it."""
    await _harbour_contract(c, cis, lodge)  # links Harbour with its code HB-LOTUS and the alias, publishes nothing
    maker, checker = _staff(cis, MAKER), _staff(cis, CHECKER)
    url = f"/suppliers/{lodge['b']}/rate-sheets"
    preview = await _ok(await c.post(f"{url}?dry_run=true", json=_upload(_sheet(), "allot.xlsx"), headers=maker))
    [term] = preview["inventory"]
    assert (term["inventory_kind"], term["units"], term["release_days"], term["release_days_default"]) == (
        "allotment", 2, 7, True,
    )  # fmt: skip
    done = await _ok(await c.post(url, json=_upload(_sheet(), "allot.xlsx"), headers=maker))
    k = done["contract"]["id"]
    stored = await admin.fetchrow(
        "SELECT inventory_kind, units, release_days, status FROM booking.supply_restrictions"
        " WHERE supplier_contract_id = $1::uuid",
        k,
    )
    assert tuple(stored) == ("allotment", 2, 7, "contract")
    await _ok(await c.patch(f"/contracts/{k}", json={"status": "published"}, headers=checker))
    return k


async def test_allotment_of_two_rooms_with_seven_release_days(
    client: Client,
    admin: asyncpg.Connection,  # noqa: F811
    lodge: dict[str, str],  # noqa: F811
) -> None:
    c, cis, m2m, _ = client
    await _allotment_contract(c, cis, admin, lodge)
    room = lodge["room"]

    # Two rooms fit the allotment; a third room in the same line does not.
    assert await _availability(c, m2m, "ALLOT-2", [_line(room, 2)]) == ["available"]
    assert (await _note(admin, "ALLOT-2"))["supply"] == {"inventory": "allotment"}
    assert await _availability(c, m2m, "ALLOT-3", [_line(room, 3)]) == ["on_request"]
    assert set((await _note(admin, "ALLOT-3"))["supply"]) == {"allotment_full"}
    # Two lines of one quote share the allotment: the first takes both rooms, the second is on request.
    two = [_line(room, 2, ref="a"), _line(room, 1, ref="b")]
    assert await _availability(c, m2m, "ALLOT-2+1", two) == ["available", "on_request"]

    # An agency books 2 rooms: held available, confirmed at once (no on-request desk line).
    booked = await _book(c, m2m, "ALLOT-BOOK", [_line(room, 2)])
    assert [line["status"] for line in booked["lines"]] == ["confirmed"]
    # One more room on those nights is now on request; the staff check of DMC B says why.
    assert await _availability(c, m2m, "ALLOT-4", [_line(room, 1)]) == ["on_request"]
    sales = _staff(cis, SALES)
    check = await _ok(await c.get("/supply/availability", headers=sales, params={
        "component_id": f"rt:{room}", "date_from": DAY1, "date_to": DAY1, "supplier_id": lodge["b"]}))  # fmt: skip
    assert check["days"][0]["reason"] == "allotment_full"
    other = await _ok(await c.get("/supply/availability", headers=sales, params={
        "component_id": f"rt:{room}", "date_from": DAY1, "date_to": DAY1, "supplier_id": lodge["a"]}))  # fmt: skip
    assert other["days"][0]["reason"] == "no_terms"  # Lantern (DMC A) has no allotment: its own terms only

    # Inside the release window (7 days before the night) the allotment is back with the DMC: on request.
    soon = (TODAY + timedelta(days=5)).isoformat()
    assert await _availability(c, m2m, "ALLOT-SOON", [_line(room, 1, day=soon)]) == ["on_request"]
    assert set((await _note(admin, "ALLOT-SOON"))["supply"]) == {"released"}
    later = (DAY1_DATE + timedelta(days=10)).isoformat()
    assert await _availability(c, m2m, "ALLOT-LATER", [_line(room, 1, day=later)]) == ["available"]


async def test_desk_names_the_dmc_whose_rate_priced_the_line(
    client: Client,
    admin: asyncpg.Connection,  # noqa: F811
    lodge: dict[str, str],  # noqa: F811
) -> None:
    c, cis, m2m, _ = client
    await _allotment_contract(c, cis, admin, lodge)
    booked = await _book(c, m2m, "ALLOT-DESK", [_line(lodge["room"], 3)])  # 3 rooms > allotment: on request
    assert [line["status"] for line in booked["lines"]] == ["on_request"]
    listing = await _ok(await c.get("/b2b-desk/on-request", headers=_staff(cis, SALES)))
    [item] = [i for i in listing["items"] if i["hold_id"] == booked["hold_id"]]
    assert item["component"]["supplier"]["id"] == lodge["b"]  # Harbour priced it (56 HB < Lantern 65 HB)
    assert item["component"]["supplier"]["name"] == "Harbour DMC"


async def test_contract_exports_and_imports_back_as_a_new_draft(
    client: Client,
    admin: asyncpg.Connection,  # noqa: F811
    lodge: dict[str, str],  # noqa: F811
) -> None:
    c, cis, m2m, _ = client
    k = await _allotment_contract(c, cis, admin, lodge)
    response = await c.get(f"/contracts/{k}/rate-sheet", headers=_staff(cis, SALES))
    assert response.status_code == 200, response.text
    assert response.headers["content-disposition"].endswith('-harbour-2027-allotment.xlsx"')
    data = response.content
    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    hotel = list(wb["Hotel"].iter_rows(values_only=True))
    assert hotel[1][0] == "HB-LOTUS" and hotel[1][1] == "Deluxe"  # the DMC's code, the AA room type name
    assert [float(hotel[1][i]) for i in (8, 10, 11)] == [56.0, 70.0, 20.0]  # Net, Published, Commission %
    inventory = list(wb["Inventory"].iter_rows(values_only=True))
    assert inventory[1][3:4] == ("allotment",) and inventory[1][6:8] == (2, 7)
    wb.close()

    # Round trip: the same file imports cleanly; a new published price shows in the diff against the contract.
    maker = _staff(cis, MAKER)
    url = f"/suppliers/{lodge['b']}/rate-sheets"
    same = await _ok(await c.post(f"{url}?dry_run=true", json=_upload(data, "export.xlsx"), headers=maker))
    assert (same["diff"]["added"], same["diff"]["changed"], same["diff"]["removed"]) == ([], [], [])
    assert same["diff"]["previous_contract"]["id"] == k and len(same["inventory"]) == 1
    assert same["inventory"][0]["release_days_default"] is False  # the export wrote the 7 days down
    edited = _edit_published(data, "75")
    preview = await _ok(await c.post(f"{url}?dry_run=true", json=_upload(edited, "export.xlsx"), headers=maker))
    [changed] = preview["diff"]["changed"]
    assert (changed["old_net"], changed["new_net"]) == ("56.00", "60.00")
    done = await _ok(await c.post(url, json=_upload(edited, "export.xlsx"), headers=maker))
    assert done["contract"]["status"] == "draft" and done["contract"]["id"] != k
    terms = await admin.fetchval(
        "SELECT count(*) FROM booking.supply_restrictions WHERE supplier_contract_id = $1::uuid", done["contract"]["id"]
    )
    assert terms == 1

    # Published, the re-imported contract replaces the terms it copies: still 2 rooms for Harbour, never 2 + 2.
    publish = {"status": "published", "acknowledge_checks": True}
    await _ok(await c.patch(f"/contracts/{done['contract']['id']}", json=publish, headers=_staff(cis, CHECKER)))
    assert await admin.fetchval(
        "SELECT count(*) FROM booking.supplier_contracts WHERE supplier_id = $1::uuid AND status = 'published'"
        " AND id IN ($2::uuid, $3::uuid)",
        lodge["b"],
        k,
        done["contract"]["id"],
    ) == 2  # fmt: skip
    room = lodge["room"]
    assert await _availability(c, m2m, "ALLOT-RT-2", [_line(room, 2)]) == ["available"]
    assert await _availability(c, m2m, "ALLOT-RT-3", [_line(room, 3)]) == ["on_request"]
    assert set((await _note(admin, "ALLOT-RT-3"))["supply"]) == {"allotment_full"}


def _edit_published(data: bytes, value: str) -> bytes:
    wb = load_workbook(io.BytesIO(data))
    ws = wb["Hotel"]
    header = [cell.value for cell in ws[1]]
    ws.cell(row=2, column=header.index("Published") + 1, value=value)
    ws.cell(row=2, column=header.index("Net") + 1).value = None  # cell(value=None) would leave it as it is
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


async def test_export_is_refused_past_the_row_cap(
    client: Client,
    admin: asyncpg.Connection,  # noqa: F811
    lodge: dict[str, str],  # noqa: F811
) -> None:
    c, cis, _, _ = client
    k = await _allotment_contract(c, cis, admin, lodge)
    # Rates + seasons + child prices + inventory = 4 rows; a policy cap of 3 refuses the export (and the import).
    version = await admin.fetchval(
        "SELECT max(version_no) + 1 FROM booking.pricing_policy WHERE param_key = 'rate_sheet_max_rows'"
    )
    current = await admin.fetchval(
        "SELECT id::text FROM booking.pricing_policy WHERE param_key = 'rate_sheet_max_rows'"
        " ORDER BY version_no DESC LIMIT 1"
    )
    policy = await admin.fetchval(
        "INSERT INTO booking.pricing_policy (param_key, version_no, value, unit, effective_from, reason, replaces_id,"
        " created_by) VALUES ('rate_sheet_max_rows', $1, '3'::jsonb, 'rows', $2, 'test cap', $3::uuid, 'it-1153-cap')"
        " RETURNING id::text",
        version,
        TODAY - timedelta(days=1),
        current,
    )
    for event, actor in (("submitted", "it-1153-cap"), ("approved", "it-1153-other")):
        await admin.execute(
            "INSERT INTO booking.pricing_policy_events (policy_id, maker, event, actor) VALUES ($1::uuid, $2, $3, $4)",
            policy,
            "it-1153-cap",
            event,
            actor,
        )
    try:
        refused = await c.get(f"/contracts/{k}/rate-sheet", headers=_staff(cis, SALES))
        assert refused.status_code == 422 and refused.json()["detail"] == "rate_sheet_too_many_rows"
        url = f"/suppliers/{lodge['b']}/rate-sheets?dry_run=true"
        too_big = await c.post(url, json=_upload(_sheet(), "allot.xlsx"), headers=_staff(cis, MAKER))
        assert too_big.status_code == 422 and too_big.json()["detail"] == "rate_sheet_too_many_rows"
    finally:
        await admin.execute("DELETE FROM booking.pricing_policy_events WHERE policy_id = $1::uuid", policy)
        await admin.execute("DELETE FROM booking.pricing_policy WHERE id = $1::uuid", policy)
