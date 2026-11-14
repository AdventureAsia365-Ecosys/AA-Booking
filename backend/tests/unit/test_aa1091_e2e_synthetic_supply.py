"""AA-1091 (S280, root ADR 0006a A10e-3): the synthetic supply of the Dev-only E2E pricing pair
(:mod:`app.auth.e2e_staff`).

Dev has no supplier rates for the tours the live check prices, so the pricing maker loads a SYNTHETIC supplier through
the real supply routes, and the checker publishes its contract (maker-checker). Through the real app with local RSA
keys + fake JWKS and a scripted pool (no network, no DB):

- the create bodies carry the marked name and only the listed facts (no contact, address, note, link), a contract
  moves only by ``{"status": "published" | "expired"}`` alone, a deactivation carries no reason: else 403
  ``e2e_read_only`` before any DB access;
- every supply write except the supplier create reaches the route only on the maker's synthetic supplier (path record,
  or the supplier the create body names; UUID or code), else 403 ``e2e_synthetic_only`` before the staff row sync;
- the checker's only supply write is the contract status;
- a real contracting member is unaffected.

The SQL itself runs on PostgreSQL in tests/integration/test_aa1091_e2e_synthetic_supply.py.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException

from app.auth import e2e_staff
from tests.staff_tokens import BOOKING_STAFF_CLIENT, LocalCis
from tests.unit.fakes import ScriptedConn
from tests.unit.test_aa911_preview_cap import IDENTITY_SELECT, auth, cognito_row
from tests.unit.test_aa1091_e2e_pricing_pair import CHECKER, MAKER, _client, _detail, _row, _staff_calls, _token

SUPPLIER = "7c1e2d3f-4a5b-4c6d-8e7f-9a0b1c2d3e4f"
HOTEL = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
SERVICE = "2b3c4d5e-6f7a-4b8c-9d0e-1f2a3b4c5d6e"
CONTRACT = "3c4d5e6f-7a8b-4c9d-8e1f-2a3b4c5d6e7f"
PLACE = "4d5e6f7a-8b9c-4d0e-9f1a-2b3c4d5e6f7a"
GUARD = "s.company_name LIKE 'SYNTHETIC%' AS is_synthetic"

SUPPLIER_BODY = {"company_name": "SYNTHETIC supply S280 (not real)", "categories": ["hotel", "activity"]}
HOTEL_BODY = {"supplier_id": SUPPLIER, "name": "SYNTHETIC hotel 1 (not real)", "destination_id": PLACE}
SERVICE_BODY = {
    "supplier_id": SUPPLIER,
    "service_type": "activity",
    "title": "SYNTHETIC activity 1 (not real)",
    "unit": "person",
    "source_id": "0c35c80e-571f-45b6-8ce4-5bf63bb8972a",
}
ROOM_BODY = {"name": "Double", "max_occupancy": 2}
SHEET_BODY = {"data_base64": "UEsDBA==", "filename": "synthetic.xlsx"}

CREATE_SUPPLIER = ("POST", "/booking/suppliers", SUPPLIER_BODY)
CREATE_HOTEL = ("POST", "/booking/hotels", HOTEL_BODY)
CREATE_SERVICE = ("POST", "/booking/services", SERVICE_BODY)
CREATE_ROOM = ("POST", f"/booking/hotels/{HOTEL}/room-types", ROOM_BODY)
RATE_SHEET = ("POST", f"/booking/suppliers/{SUPPLIER}/rate-sheets?dry_run=true", SHEET_BODY)
PUBLISH = ("PATCH", f"/booking/contracts/{CONTRACT}", {"status": "published"})
EXPIRE = ("PATCH", f"/booking/contracts/{CONTRACT}", {"status": "expired"})
OFF_SUPPLIER = ("POST", f"/booking/suppliers/{SUPPLIER}/deactivate", None)
OFF_HOTEL = ("POST", f"/booking/hotels/{HOTEL}/deactivate", None)
OFF_SERVICE = ("POST", f"/booking/services/{SERVICE}/deactivate", None)
# (request, the record key the guard looks up) for every write checked against the maker's supplier.
CHECKED = [
    (CREATE_HOTEL, SUPPLIER),
    (CREATE_SERVICE, SUPPLIER),
    (CREATE_ROOM, HOTEL),
    (RATE_SHEET, SUPPLIER),
    (PUBLISH, CONTRACT),
    (EXPIRE, CONTRACT),
    (OFF_SUPPLIER, SUPPLIER),
    (OFF_HOTEL, HOTEL),
    (OFF_SERVICE, SERVICE),
]


@pytest.fixture(autouse=True)
def dev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "dev")


def _guard(conn: ScriptedConn) -> list[tuple[Any, ...]]:
    return conn.sql(GUARD)


# ── the module ──────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("write", "body", "ok"),
    [
        (e2e_staff.SUPPLIER_CREATE, SUPPLIER_BODY, True),
        (e2e_staff.SUPPLIER_CREATE, {**SUPPLIER_BODY, "country_code": "JP"}, True),
        (e2e_staff.SUPPLIER_CREATE, {**SUPPLIER_BODY, "company_name": "Okinawa Blue Tours"}, False),
        (e2e_staff.SUPPLIER_CREATE, {**SUPPLIER_BODY, "company_name": "synthetic lower case"}, False),
        (e2e_staff.SUPPLIER_CREATE, {**SUPPLIER_BODY, "contact_email": "ops@example.com"}, False),
        (e2e_staff.SUPPLIER_CREATE, {**SUPPLIER_BODY, "internal_notes": "x"}, False),
        (e2e_staff.SUPPLIER_CREATE, {"company_name": "SYNTHETIC x"}, False),
        (e2e_staff.HOTEL_CREATE, HOTEL_BODY, True),
        (e2e_staff.HOTEL_CREATE, {**HOTEL_BODY, "name": "Harbour View Inn"}, False),
        (e2e_staff.HOTEL_CREATE, {**HOTEL_BODY, "address": "1-2 Kumoji"}, False),
        (e2e_staff.HOTEL_CREATE, {"name": "SYNTHETIC hotel"}, False),
        (e2e_staff.SERVICE_CREATE, SERVICE_BODY, True),
        (e2e_staff.SERVICE_CREATE, {**SERVICE_BODY, "title": 7}, False),
        (e2e_staff.SERVICE_CREATE, {**SERVICE_BODY, "inclusions": ["lunch"]}, False),
        (e2e_staff.ROOM_TYPE_CREATE, ROOM_BODY, True),
        (e2e_staff.ROOM_TYPE_CREATE, {**ROOM_BODY, "internal_notes": "x"}, False),
        (e2e_staff.ROOM_TYPE_CREATE, {}, False),
        (e2e_staff.CONTRACT_STATUS, {"status": "published"}, True),
        (e2e_staff.CONTRACT_STATUS, {"status": "expired"}, True),
        (e2e_staff.CONTRACT_STATUS, {"status": "draft"}, False),
        (e2e_staff.CONTRACT_STATUS, {"status": "published", "notes": "x"}, False),
        (e2e_staff.CONTRACT_STATUS, {"valid_to": "2027-10-31"}, False),
        (e2e_staff.SUPPLIER_DEACTIVATE, None, True),
        (e2e_staff.HOTEL_DEACTIVATE, {}, True),
        (e2e_staff.SERVICE_DEACTIVATE, {"reason": "done"}, False),
        (e2e_staff.SUPPLIER_CREATE, None, False),
        (e2e_staff.HOTEL_CREATE, [HOTEL_BODY], False),
    ],
)
def test_the_supply_body_checks(write: tuple[str, str], body: Any, ok: bool) -> None:
    writer = e2e_staff.E2E_WRITERS[MAKER]
    if ok:
        e2e_staff.check_body(writer, *write, body)
    else:
        with pytest.raises(HTTPException) as err:
            e2e_staff.check_body(writer, *write, body)
        assert err.value.detail == "e2e_read_only"


def test_every_supply_record_is_bound_to_the_pricing_maker() -> None:
    supply = {e2e_staff.SUPPLIER_SYNTHETIC_SQL, e2e_staff.HOTEL_SYNTHETIC_SQL, e2e_staff.SERVICE_SYNTHETIC_SQL}
    supply.add(e2e_staff.CONTRACT_SYNTHETIC_SQL)
    for route, (_, sql) in e2e_staff.SYNTHETIC_SQL.items():
        if sql in supply:
            assert e2e_staff.SYNTHETIC_SQL_ARGS[route] == (MAKER,), route
            assert GUARD in " ".join(sql.split())
    assert dict(e2e_staff.BODY_SUPPLIER) == {"/booking/hotels": "supplier_id", "/booking/services": "supplier_id"}
    assert e2e_staff.NEW_RECORD == {"/booking/suppliers"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route", "params", "sql_fragment", "key"),
    [
        (e2e_staff.SUPPLIER_DEACTIVATE[1], {"supplier_ref": "sup-000042"}, "WHERE s.code = $1", "SUP-000042"),
        (e2e_staff.HOTEL_DEACTIVATE[1], {"hotel_ref": "HTL-000901"}, "WHERE r.code = $1", "HTL-000901"),
        (e2e_staff.SERVICE_DEACTIVATE[1], {"service_ref": " SVC-000007 "}, "WHERE r.code = $1", "SVC-000007"),
        (e2e_staff.ROOM_TYPE_CREATE[1], {"hotel_ref": HOTEL.upper()}, "WHERE r.id = $1::uuid", HOTEL),
    ],
)
async def test_a_code_or_uuid_reference_is_looked_up(
    route: str, params: dict[str, Any], sql_fragment: str, key: str
) -> None:
    conn = ScriptedConn().on(sql_fragment, True)
    assert await e2e_staff.is_synthetic_record(conn, route, params) is True
    assert conn.sql(sql_fragment) == [(key, MAKER)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route", "params"),
    [
        (e2e_staff.HOTEL_DEACTIVATE[1], {"hotel_ref": "SVC-000001"}),  # another kind's code
        (e2e_staff.SUPPLIER_DEACTIVATE[1], {"supplier_ref": "Okinawa Blue Tours"}),
        (e2e_staff.CONTRACT_STATUS[1], {"contract_id": "SUP-000001"}),  # a contract is a UUID only
        (e2e_staff.SERVICE_DEACTIVATE[1], {}),
    ],
)
async def test_a_bad_reference_counts_as_real_without_a_query(route: str, params: dict[str, Any]) -> None:
    conn = ScriptedConn().on(GUARD, True)
    assert await e2e_staff.is_synthetic_record(conn, route, params) is False
    assert conn.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [None, {}, {"supplier_id": None}, {"supplier_id": "Okinawa Blue Tours"}])
async def test_a_create_without_a_supplier_reference_counts_as_real(body: Any) -> None:
    conn = ScriptedConn().on(GUARD, True)
    assert await e2e_staff.is_synthetic_record(conn, e2e_staff.HOTEL_CREATE[1], {}, body) is False
    assert conn.calls == []


# ── the real app ────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/booking/suppliers", {**SUPPLIER_BODY, "company_name": "Okinawa Blue Tours"}),
        ("POST", "/booking/hotels", {**HOTEL_BODY, "address": "somewhere"}),
        ("PATCH", f"/booking/contracts/{CONTRACT}", {"status": "published", "title": "x"}),
        ("POST", f"/booking/suppliers/{SUPPLIER}/deactivate", {"reason": "done"}),
        ("PATCH", f"/booking/suppliers/{SUPPLIER}", {"company_name": "SYNTHETIC renamed"}),
        ("POST", f"/booking/suppliers/{SUPPLIER}/contracts", {"title": "x"}),
        ("POST", f"/booking/contracts/{CONTRACT}/rates", {"net_amount": 1}),
        ("PATCH", f"/booking/hotels/{HOTEL}", {"name": "SYNTHETIC x"}),
        ("POST", f"/booking/hotels/{HOTEL}/media", {}),
        ("POST", f"/booking/suppliers/{SUPPLIER}/activate", None),
    ],
)
def test_other_supply_writes_or_bodies_are_403_before_any_db_access(method: str, path: str, body: Any) -> None:
    cis, conn = LocalCis(), ScriptedConn()
    with _client(cis, conn) as c:
        r = c.request(method, path, json=body, headers=auth(_token(cis, MAKER)))
    assert (r.status_code, r.json()["detail"]) == (403, "e2e_read_only")
    assert conn.calls == []


@pytest.mark.parametrize("request_", [w for w, _ in CHECKED if w not in (PUBLISH, EXPIRE)] + [CREATE_SUPPLIER])
def test_the_checker_makes_no_supply_write_but_the_contract_status(request_: tuple[str, str, Any]) -> None:
    method, path, body = request_
    cis, conn = LocalCis(), ScriptedConn()
    with _client(cis, conn) as c:
        r = c.request(method, path, json=body, headers=auth(_token(cis, CHECKER)))
    assert (r.status_code, r.json()["detail"]) == (403, "e2e_read_only")
    assert conn.calls == []


@pytest.mark.parametrize("synthetic", [False, None])
@pytest.mark.parametrize(("request_", "key"), CHECKED)
def test_supply_writes_off_the_makers_supplier_are_refused_before_the_staff_sync(
    request_: tuple[str, str, Any], key: str, synthetic: Any
) -> None:
    method, path, body = request_
    cis = LocalCis()
    conn = ScriptedConn().on(GUARD, synthetic).on(IDENTITY_SELECT, _row(MAKER))
    with _client(cis, conn) as c:
        r = c.request(method, path, json=body, headers=auth(_token(cis, MAKER)))
    assert (r.status_code, r.json()["detail"]) == (403, "e2e_synthetic_only"), path
    assert _guard(conn) == [(key, MAKER)]
    assert _staff_calls(conn) == [] and len(conn.calls) == 1


@pytest.mark.parametrize("email", [MAKER, CHECKER])
def test_allowed_supply_writes_on_the_makers_supplier_reach_the_route(email: str) -> None:
    cis = LocalCis()
    writes = CHECKED if email == MAKER else [(PUBLISH, CONTRACT), (EXPIRE, CONTRACT)]
    for (method, path, body), key in writes:
        conn = ScriptedConn().on(GUARD, True).on(IDENTITY_SELECT, _row(email))
        with _client(cis, conn) as c:
            r = c.request(method, path, json=body, headers=auth(_token(cis, email)))
        detail = _detail(r)
        assert r.status_code != 422, r.text
        assert not isinstance(detail, str) or not detail.startswith("e2e_"), path
        flat = [sql for sql, _ in conn.calls]
        first_staff = flat.index(_staff_calls(conn)[0])
        assert _guard(conn) == [(key, MAKER)]
        assert flat.index(next(s for s in flat if GUARD in s)) < first_staff
        assert len(flat) > first_staff + 1, path  # the route ran past the staff dependency


def test_the_supplier_create_reaches_the_route_on_its_body_check_alone() -> None:
    cis = LocalCis()
    conn = ScriptedConn().on(IDENTITY_SELECT, _row(MAKER))
    method, path, body = CREATE_SUPPLIER
    with _client(cis, conn) as c:
        r = c.request(method, path, json=body, headers=auth(_token(cis, MAKER)))
    detail = _detail(r)
    assert not isinstance(detail, str) or not detail.startswith("e2e_"), r.text
    assert _guard(conn) == []
    assert len(conn.calls) > len(_staff_calls(conn))  # the route ran


def test_a_real_contracting_member_is_unaffected() -> None:
    """Control: a company contracting user writes supply with any name and no E2E check."""
    cis = LocalCis()
    conn = ScriptedConn().on(IDENTITY_SELECT, {**cognito_row("contracting"), "cis_role": None})
    token = cis.workforce.token(["booking-contracting"], client_id=BOOKING_STAFF_CLIENT)
    with _client(cis, conn) as c:
        r = c.post(
            "/booking/suppliers",
            json={"company_name": "Okinawa Blue Tours", "categories": ["hotel"]},
            headers=auth(token),
        )
        c.patch(f"/booking/contracts/{CONTRACT}", json={"title": "x"}, headers=auth(token))
    detail = _detail(r)
    assert not isinstance(detail, str) or not detail.startswith("e2e_"), r.text
    assert _guard(conn) == []
