"""AA-1153 (S289, root ADR 0006a A10e-5): the Dev-only E2E pricing maker links its synthetic hotel to a second
synthetic DMC and enters a desk booking of that hotel's rooms on a synthetic agency (:mod:`app.auth.e2e_staff`,
:mod:`app.auth.e2e_staff_supply`, :mod:`app.auth.e2e_staff_bodies`).

Through the real app with local RSA keys + fake JWKS and a scripted pool (no network, no DB):

- the link body names the DMC only (its own code and the preferred flag allowed); the desk entry body carries the marked
  agency reference and room lines only (no price override, no departure seat, no room's own room type): else 403
  ``e2e_read_only`` before any DB access;
- the link reaches the route only when the hotel (path) and the DMC (body) are both the maker's synthetic supply; the
  desk entry only when the agency is synthetic and every room type is on a synthetic hotel all of whose DMCs are
  synthetic (one query): else 403 ``e2e_synthetic_only`` before the staff row sync;
- the desk entry passes the route gate (``b2b_desk.book``, held for that one request); the checker makes neither write.

The SQL itself runs on PostgreSQL in tests/integration/test_aa1153_e2e_hotel_quote.py. All names are invented.
"""

from __future__ import annotations

from typing import Any

import asyncpg
import pytest
from fastapi import HTTPException

from app.auth import e2e_staff, e2e_staff_supply
from tests.staff_tokens import LocalCis
from tests.unit.fakes import ScriptedConn
from tests.unit.test_aa911_preview_cap import IDENTITY_SELECT, auth
from tests.unit.test_aa1091_e2e_pricing_pair import CHECKER, MAKER, _client, _detail, _row, _staff_calls, _token

HOTEL = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
DMC = "7c1e2d3f-4a5b-4c6d-8e7f-9a0b1c2d3e4f"
PARTNER = "5e6f7a8b-9c0d-4e1f-8a2b-3c4d5e6f7a8b"
ROOM = "6f7a8b9c-0d1e-4f2a-9b3c-4d5e6f7a8b9c"
ROOM2 = "8b9c0d1e-2f3a-4b4c-9d5e-6f7a8b9c0d1e"
SUPPLY_GUARD = "s.company_name LIKE 'SYNTHETIC%' AS is_synthetic"
DESK_GUARD = "WHERE rt.id = ANY($2::uuid[])"

LINK_BODY = {"supplier_id": DMC, "supplier_hotel_code": "SYN-HB-1"}
LINE = {
    "line_ref": "rooms",
    "component_id": f"rt:{ROOM}",
    "service_date": "2031-03-10",
    "nights": 3,
    "pax": {"adult": 2, "child": 1},
    "rooms": [{"occupancy": "double", "count": 1}],
    "meal_plan": "HB",
    "child_ages": [7],
}
DESK_BODY = {"partner_id": PARTNER, "agency_ref": "SYNTHETIC HB check 1", "currency": "USD", "lines": [LINE]}
LINK = ("POST", f"/booking/hotels/{HOTEL}/suppliers", LINK_BODY)
DESK = ("POST", "/booking/b2b-desk/entries", DESK_BODY)
IDEM = {"Idempotency-Key": "synthetic-hb-check-0001"}


@pytest.fixture(autouse=True)
def dev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "dev")


# ── the module ──────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("write", "body", "ok"),
    [
        (e2e_staff.HOTEL_SUPPLIER_LINK, LINK_BODY, True),
        (e2e_staff.HOTEL_SUPPLIER_LINK, {"supplier_id": DMC}, True),
        (e2e_staff.HOTEL_SUPPLIER_LINK, {**LINK_BODY, "is_preferred": True}, True),
        (e2e_staff.HOTEL_SUPPLIER_LINK, {"supplier_hotel_code": "SYN-HB-1"}, False),
        (e2e_staff.HOTEL_SUPPLIER_LINK, {**LINK_BODY, "status": "inactive"}, False),
        (e2e_staff.HOTEL_SUPPLIER_LINK, None, False),
        (e2e_staff.DESK_ENTRY, DESK_BODY, True),
        (e2e_staff.DESK_ENTRY, {**DESK_BODY, "lines": [LINE, {**LINE, "line_ref": "b", "meal_plan": None}]}, True),
        (e2e_staff.DESK_ENTRY, {**DESK_BODY, "agency_ref": "Lotus booking 12"}, False),
        (e2e_staff.DESK_ENTRY, {**DESK_BODY, "override_total": "10.00", "override_reason": "test"}, False),
        (e2e_staff.DESK_ENTRY, {**DESK_BODY, "lines": []}, False),
        (e2e_staff.DESK_ENTRY, {**DESK_BODY, "lines": [{**LINE, "component_id": f"tp:{ROOM}"}]}, False),
        (e2e_staff.DESK_ENTRY, {**DESK_BODY, "lines": [{**LINE, "component_id": f"svc:{ROOM}"}]}, False),
        (e2e_staff.DESK_ENTRY, {**DESK_BODY, "lines": [{**LINE, "departure_id": ROOM}]}, False),
        (
            e2e_staff.DESK_ENTRY,
            {**DESK_BODY, "lines": [{**LINE, "rooms": [{"occupancy": "double", "room_type_id": ROOM2}]}]},
            False,
        ),
        (e2e_staff.DESK_ENTRY, {k: v for k, v in DESK_BODY.items() if k != "partner_id"}, False),
        (e2e_staff.DESK_ENTRY, [DESK_BODY], False),
    ],
)
def test_the_body_checks(write: tuple[str, str], body: Any, ok: bool) -> None:
    writer = e2e_staff.E2E_WRITERS[MAKER]
    if ok:
        e2e_staff.check_body(writer, *write, body)
    else:
        with pytest.raises(HTTPException) as err:
            e2e_staff.check_body(writer, *write, body)
        assert err.value.detail == "e2e_read_only"


def test_the_grant_is_held_by_the_desk_entry_only() -> None:
    maker = e2e_staff.E2E_WRITERS[MAKER]
    assert maker.grants_for(*e2e_staff.DESK_ENTRY) == {"b2b_desk.book"}
    assert maker.grants_for(*e2e_staff.HOTEL_SUPPLIER_LINK) == frozenset()
    assert e2e_staff.RECORD_CHECKED == {e2e_staff.HOTEL_SUPPLIER_LINK[1], e2e_staff.DESK_ENTRY[1]}
    for email, writer in e2e_staff.E2E_WRITERS.items():
        if email != MAKER:
            assert not ({e2e_staff.HOTEL_SUPPLIER_LINK, e2e_staff.DESK_ENTRY} & writer.writes), email


@pytest.mark.asyncio
@pytest.mark.parametrize(("hotel", "dmc", "expected"), [(True, True, True), (True, False, False), (False, True, False)])
async def test_a_link_needs_both_ends_synthetic(hotel: bool, dmc: bool, expected: bool) -> None:
    conn = ScriptedConn().on("FROM booking.hotels r", hotel).on("FROM booking.suppliers s LEFT JOIN", dmc)
    route = e2e_staff.HOTEL_SUPPLIER_LINK[1]
    assert await e2e_staff.is_synthetic_record(conn, route, {"hotel_ref": HOTEL}, LINK_BODY) is expected
    assert conn.sql("FROM booking.hotels r") == [(HOTEL, MAKER)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("params", "body"),
    [
        ({"hotel_ref": "Harbour View Inn"}, LINK_BODY),
        ({"hotel_ref": HOTEL}, {"supplier_id": "Lantern DMC"}),
        ({"hotel_ref": HOTEL}, None),
        ({}, LINK_BODY),
    ],
)
async def test_a_bad_link_reference_counts_as_real_without_a_query(params: dict[str, Any], body: Any) -> None:
    conn = ScriptedConn().on(SUPPLY_GUARD, True)
    assert await e2e_staff.is_synthetic_record(conn, e2e_staff.HOTEL_SUPPLIER_LINK[1], params, body) is False
    assert conn.calls == []


@pytest.mark.asyncio
async def test_the_desk_entry_is_one_query_on_the_agency_and_the_rooms() -> None:
    body = {**DESK_BODY, "lines": [LINE, {**LINE, "line_ref": "b", "component_id": f"RT:{ROOM2.upper()}"}, LINE]}
    conn = ScriptedConn().on(DESK_GUARD, True)
    assert await e2e_staff.is_synthetic_record(conn, e2e_staff.DESK_ENTRY[1], {}, body) is True
    assert conn.sql(DESK_GUARD) == [(PARTNER, sorted([ROOM, ROOM2]), MAKER, 2)]
    assert "catalog.v_tenant_account_v1" in e2e_staff_supply.DESK_ENTRY_SYNTHETIC_SQL
    assert "booking.hotel_supplier_links" in e2e_staff_supply.DESK_ENTRY_SYNTHETIC_SQL


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {**DESK_BODY, "partner_id": "Lotus Travel"},
        {**DESK_BODY, "lines": [{**LINE, "component_id": "rt:not-a-uuid"}]},
        {**DESK_BODY, "lines": []},
        None,
    ],
)
async def test_a_bad_desk_reference_counts_as_real_without_a_query(body: Any) -> None:
    conn = ScriptedConn().on(DESK_GUARD, True)
    assert await e2e_staff.is_synthetic_record(conn, e2e_staff.DESK_ENTRY[1], {}, body) is False
    assert conn.calls == []


@pytest.mark.asyncio
async def test_a_database_error_counts_as_real() -> None:
    class Broken(ScriptedConn):
        async def fetchval(self, sql: str, *args: Any, **kw: Any) -> Any:
            raise asyncpg.PostgresError("down")

    assert await e2e_staff.is_synthetic_record(Broken(), e2e_staff.DESK_ENTRY[1], {}, DESK_BODY) is False


# ── the real app ────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "request_",
    [
        ("POST", f"/booking/hotels/{HOTEL}/suppliers", {**LINK_BODY, "status": "active"}),
        ("POST", "/booking/b2b-desk/entries", {**DESK_BODY, "override_total": "1.00", "override_reason": "x"}),
        ("POST", "/booking/b2b-desk/entries", {**DESK_BODY, "agency_ref": "Lotus booking 12"}),
        ("PATCH", f"/booking/hotels/{HOTEL}/suppliers/{DMC}", {"is_preferred": True}),
        ("POST", f"/booking/b2b-desk/entries/{ROOM}/approve", None),
    ],
)
def test_other_bodies_or_routes_are_403_before_any_db_access(request_: tuple[str, str, Any]) -> None:
    method, path, body = request_
    cis, conn = LocalCis(), ScriptedConn()
    with _client(cis, conn) as c:
        r = c.request(method, path, json=body, headers={**auth(_token(cis, MAKER)), **IDEM})
    assert (r.status_code, r.json()["detail"]) == (403, "e2e_read_only")
    assert conn.calls == []


@pytest.mark.parametrize("request_", [LINK, DESK])
def test_the_checker_makes_neither_write(request_: tuple[str, str, Any]) -> None:
    method, path, body = request_
    cis, conn = LocalCis(), ScriptedConn()
    with _client(cis, conn) as c:
        r = c.request(method, path, json=body, headers={**auth(_token(cis, CHECKER)), **IDEM})
    assert (r.status_code, r.json()["detail"]) == (403, "e2e_read_only")
    assert conn.calls == []


@pytest.mark.parametrize("synthetic", [False, None])
@pytest.mark.parametrize(("request_", "guard"), [(LINK, "FROM booking.hotels r"), (DESK, DESK_GUARD)])
def test_real_records_are_refused_before_the_staff_sync(
    request_: tuple[str, str, Any], guard: str, synthetic: Any
) -> None:
    method, path, body = request_
    cis = LocalCis()
    conn = ScriptedConn().on(guard, synthetic).on(IDENTITY_SELECT, _row(MAKER))
    with _client(cis, conn) as c:
        r = c.request(method, path, json=body, headers={**auth(_token(cis, MAKER)), **IDEM})
    assert (r.status_code, r.json()["detail"]) == (403, "e2e_synthetic_only"), path
    assert _staff_calls(conn) == [] and len(conn.calls) == 1


@pytest.mark.parametrize("request_", [LINK, DESK])
def test_synthetic_writes_reach_the_route(request_: tuple[str, str, Any]) -> None:
    method, path, body = request_
    cis = LocalCis()
    conn = (
        ScriptedConn()
        .on("FROM booking.hotels r", True)
        .on("FROM booking.suppliers s LEFT JOIN", True)
        .on(DESK_GUARD, True)
        .on(IDENTITY_SELECT, _row(MAKER))
    )
    with _client(cis, conn) as c:
        r = c.request(method, path, json=body, headers={**auth(_token(cis, MAKER)), **IDEM})
    detail = _detail(r)
    assert r.status_code != 422, r.text
    assert detail != "forbidden", r.text  # the desk entry's b2b_desk.book gate passed on the request's grant
    assert not isinstance(detail, str) or not detail.startswith("e2e_"), r.text
    flat = [sql for sql, _ in conn.calls]
    assert len(flat) > flat.index(_staff_calls(conn)[0]) + 1, path  # the route ran past the staff dependency
