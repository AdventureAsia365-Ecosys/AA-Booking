"""AA-823 hotels, room types, services and supplier rates: route permissions and bodies (services stubbed), and the
service branches on a scripted connection. Real SQL (bk-010 checks, guard triggers, the end-to-end default net and
pricing) is covered by tests/integration/test_bk010_*.py."""

from __future__ import annotations

import json
from datetime import date, time
from decimal import Decimal as D
from typing import Any

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.auth.staff import Staff, current_staff, get_pool
from app.errors import Conflict, Invalid, NotFound
from app.main import create_app
from app.settings import Settings
from app.supply import rates, records
from app.supply.records import HOTEL, SERVICE
from app.trip_cases import versions
from app.trip_cases.versions import Cost, Item
from tests.unit.fakes import ScriptedConn

ADMIN = Staff("a-1", None, "Admin", "admin", True, "admin")
SALES = Staff("u-1", None, "Sales One", "sales", True, "reviewer")
CONTRACTING = Staff("u-5", None, "Contracts", "contracting", True, "reviewer")
CONTENT = Staff("u-9", None, "Writer", "content_creator", True, "content")
SUP = "11111111-1111-4111-8111-111111111111"
HID = "22222222-2222-4222-8222-222222222222"
RID = "33333333-3333-4333-8333-333333333333"
SID = "44444444-4444-4444-8444-444444444444"
KID = "55555555-5555-4555-8555-555555555555"
XID = "66666666-6666-4666-8666-666666666666"
PLACE = "d0000000-0000-4000-8000-000000000001"


# ── Routes ───────────────────────────────────────────────────────────────────


class Recorder:
    def __init__(self, result: Any = None) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.result = {"id": HID} if result is None else result

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args[1:], kwargs))
        return self.result


def client(staff: Staff) -> TestClient:
    app = create_app(Settings(database_url=None, environment="test"))
    app.dependency_overrides[current_staff] = lambda: staff
    app.dependency_overrides[get_pool] = lambda: ScriptedConn()
    return TestClient(app)


@pytest.fixture
def stubs(monkeypatch: pytest.MonkeyPatch) -> dict[str, Recorder]:
    recs: dict[str, Recorder] = {}
    for module, names in (
        (
            records,
            (
                "list_records",
                "create_record",
                "get_record",
                "update_record",
                "set_status",
                "create_room_type",
                "update_room_type",
                "resolve",
                "search_options",
            ),
        ),
        (rates, ("list_rates", "create_rate", "update_rate", "delete_rate", "rate_in_force")),
    ):
        for name in names:
            recs[name] = Recorder()
            monkeypatch.setattr(module, name, recs[name])
    recs["get_contract"] = Recorder({"id": KID})
    monkeypatch.setattr("app.routers.supply.suppliers.get_contract", recs["get_contract"])
    return recs


WRITES = [
    ("post", "/booking/hotels", {"supplier_id": "SUP-000001", "name": "H"}),
    ("patch", f"/booking/hotels/{HID}", {"name": "H2"}),
    ("post", f"/booking/hotels/{HID}/deactivate", None),
    ("post", f"/booking/hotels/{HID}/activate", {"reason": "back"}),
    ("post", f"/booking/hotels/{HID}/room-types", {"name": "Deluxe"}),
    ("patch", f"/booking/room-types/{RID}", {"status": "inactive"}),
    ("post", "/booking/services", {"supplier_id": SUP, "service_type": "meal", "title": "Lunch"}),
    ("patch", f"/booking/services/{SID}", {"unit": "group"}),
    ("post", f"/booking/services/{SID}/deactivate", {"reason": "closed"}),
    ("post", f"/booking/services/{SID}/activate", None),
    ("post", f"/booking/contracts/{KID}/rates", {"service_id": SID, "net_amount": "10"}),
    ("patch", f"/booking/rates/{XID}", {"net_amount": "11"}),
    ("delete", f"/booking/rates/{XID}", None),
]


@pytest.mark.parametrize(("method", "path", "body"), WRITES)
def test_writes_need_supply_write(stubs: dict[str, Recorder], method: str, path: str, body: Any) -> None:
    for staff in (SALES, CONTENT):
        assert client(staff).request(method, path, json=body).status_code == 403
    for staff in (CONTRACTING, ADMIN):
        assert client(staff).request(method, path, json=body).status_code in (200, 201, 204)


READS = [
    "/booking/hotels?q=x&status=active&supplier=SUP-1&country=VN&star_rating=4&sort=-name",
    f"/booking/hotels/{HID}",
    f"/booking/hotels/{HID}/rates",
    "/booking/services?type=meal&destination_id=x",
    f"/booking/services/{SID}",
    f"/booking/services/{SID}/rates",
    f"/booking/contracts/{KID}/rates",
    "/booking/supply/options?q=emerald&kind=room_type",
    f"/booking/supply/rate-in-force?on=2027-01-01&service_id={SID}&pax=2",
    f"/booking/supply/rate-in-force?on=2027-01-01&hotel_room_type_id={RID}",
]


@pytest.mark.parametrize("path", READS)
def test_reads_are_open_to_sales_not_content(stubs: dict[str, Recorder], path: str) -> None:
    assert client(SALES).get(path).status_code == 200
    assert client(CONTENT).get(path).status_code == 403


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/booking/hotels", {"supplier_id": SUP, "name": "H", "description": "lovely"}),  # facts only (Q17)
        ("/booking/hotels", {"supplier_id": SUP, "name": "H", "photo_url": "https://x"}),
        ("/booking/hotels", {"supplier_id": SUP, "name": "H", "star_rating": 6}),
        ("/booking/hotels", {"supplier_id": SUP, "name": "H", "links": [{"url": "ftp://x"}]}),
        ("/booking/hotels", {"name": "H"}),
        ("/booking/services", {"supplier_id": SUP, "service_type": "spa", "title": "S"}),
        ("/booking/services", {"supplier_id": SUP, "service_type": "meal", "title": "S", "unit": "room_night"}),
        (f"/booking/contracts/{KID}/rates", {"net_amount": "1"}),
        (f"/booking/contracts/{KID}/rates", {"service_id": SID, "hotel_room_type_id": RID, "net_amount": "1"}),
        (f"/booking/contracts/{KID}/rates", {"service_id": SID, "net_amount": "-1"}),
        (f"/booking/contracts/{KID}/rates", {"service_id": SID, "net_amount": "1", "meal_plan": "XX"}),
    ],
)
def test_body_validation(stubs: dict[str, Recorder], path: str, body: dict[str, Any]) -> None:
    assert client(CONTRACTING).post(path, json=body).status_code == 422
    assert not stubs["create_record"].calls and not stubs["create_rate"].calls


def test_hotel_payload_passes_strings(stubs: dict[str, Recorder]) -> None:
    r = client(CONTRACTING).post(
        "/booking/hotels", json={"supplier_id": SUP, "name": "H", "destination_id": PLACE, "check_in_time": "14:00"}
    )
    assert r.status_code == 201
    (kind, _staff, data), _ = stubs["create_record"].calls[0]
    assert kind is HOTEL and data["destination_id"] == PLACE and data["check_in_time"] == time(14, 0)


def test_item_cannot_book_two_things() -> None:
    app = create_app(Settings(database_url=None, environment="test"))
    app.dependency_overrides[current_staff] = lambda: SALES
    app.dependency_overrides[get_pool] = lambda: ScriptedConn()
    body = {"items": [{"title": "x", "day_from": 1, "day_to": 1, "hotel_room_type_id": RID, "service_id": SID}]}
    assert TestClient(app).put("/booking/trip-cases/TC-1/versions/1/items", json=body).status_code == 422


# ── Records ──────────────────────────────────────────────────────────────────


def supplier_row(**kw: Any) -> dict[str, Any]:
    return {
        "id": SUP,
        "code": "SUP-000001",
        "company_name": "Red River",
        "status": "active",
        "categories": ["hotel"],
    } | kw


def hotel_row(**kw: Any) -> dict[str, Any]:
    return {
        "id": HID,
        "code": "HTL-000001",
        "supplier_id": SUP,
        "supplier_code": "SUP-000001",
        "supplier_name": "Red River",
        "supplier_status": "active",
        "destination_id": None,
        "country_code": "VN",
        "country_name": "Vietnam",
        "internal_notes": None,
        "status": "active",
        "name": "Emerald",
        "hotel_style": None,
        "star_rating": 4,
        "address": None,
        "latitude": None,
        "longitude": None,
        "check_in_time": None,
        "check_out_time": None,
        "amenities": ["Pool"],
        "links": "[]",
        "room_type_count": 0,
        "rates_in_force": 0,
    } | kw


def service_row(**kw: Any) -> dict[str, Any]:
    return {
        "id": SID,
        "code": "SVC-000001",
        "supplier_id": SUP,
        "supplier_code": "SUP-000001",
        "supplier_status": "active",
        "destination_id": None,
        "country_code": None,
        "internal_notes": None,
        "status": "active",
        "service_type": "activity",
        "title": "Food walk",
        "unit": "person",
        "source_system": None,
        "source_id": None,
        "max_group_size": None,
        "duration_minutes": None,
        "inclusions": [],
    } | kw


def room_row(**kw: Any) -> dict[str, Any]:
    return (
        {"id": RID, "hotel_id": HID, "name": "Deluxe", "max_occupancy": 2, "bed_types": ["Queen"]}
        | {
            "internal_notes": None,
            "status": "active",
        }
        | kw
    )


def hotel_conn(**hotel: Any) -> ScriptedConn:
    return ScriptedConn(
        [
            ("WHERE t.hotel_id = $1::uuid ORDER BY", [room_row()]),
            ("FROM booking.hotel_room_types t WHERE t.id", room_row()),
            (
                "FROM booking.audit_logs l",
                [
                    {"id": "l1", "at": None, "actor_ref": "u-5", "actor_name": "C", "action": "a"}
                    | {"entity_type": "hotel", "entity_label": "x", "summary": "s"}
                ],
            ),
            ("FROM booking.hotels r JOIN", hotel_row(**hotel)),
            ("FROM booking.services r JOIN", service_row()),
            ("FROM booking.suppliers s", supplier_row(categories=["hotel", "activity", "transport"])),
            ("INSERT INTO booking.hotels", {"id": HID, "code": "HTL-000001"}),
            ("INSERT INTO booking.services", {"id": SID, "code": "SVC-000001"}),
            ("INSERT INTO booking.hotel_room_types", RID),
            ("SELECT 1 FROM booking.countries", 1),
            (
                "WHERE d.destination_id = $1::uuid",
                {"destination_id": PLACE, "name": "Hanoi", "country": "Vietnam"} | {"country_code": "VN"},
            ),
            ("d.destination_id = ANY", []),
        ]
    )


@pytest.mark.asyncio
async def test_list_records_filters_and_errors() -> None:
    conn = ScriptedConn([("SELECT count(*) FROM booking.hotels r", 1), ("FROM booking.hotels r JOIN", [hotel_row()])])
    out = await records.list_records(
        conn,
        HOTEL,
        q="em",
        status="active",
        supplier=SUP,
        country="vn",
        destination_id=PLACE,
        star_rating=4,
        sort="-star_rating",
    )
    assert out["total"] == 1 and out["items"][0]["links"] == [] and out["items"][0]["destination"] is None
    flat = conn.calls[0][0]
    assert "r.supplier_id = $2::uuid" in flat and "r.star_rating = $5" in flat
    conn = ScriptedConn([("SELECT count(*) FROM booking.services r", 0), ("FROM booking.services r JOIN", [])])
    await records.list_records(conn, SERVICE, supplier="sup-000001", service_type="meal", sort="title")
    assert "sp.code = $1" in conn.calls[0][0]
    for kwargs, code in (
        ({"status": "gone"}, "unknown_status"),
        ({"service_type": "spa"}, "unknown_type"),
        ({"sort": "colour"}, "unknown_sort"),
    ):
        with pytest.raises(Invalid) as exc:
            await records.list_records(ScriptedConn(), SERVICE, **kwargs)
        assert exc.value.code == code
    with pytest.raises(NotFound):
        await records.list_records(ScriptedConn(), HOTEL, destination_id="not-a-uuid")


@pytest.mark.asyncio
async def test_get_record_and_not_found() -> None:
    hotel = await records.get_record(hotel_conn(), HOTEL, "htl-000001")
    assert hotel["room_types"][0]["bed_types"] == ["Queen"] and hotel["recent_activity"][0]["actor"]["id"] == "u-5"
    with pytest.raises(NotFound):
        await records.resolve(ScriptedConn(), SERVICE, SID)


@pytest.mark.asyncio
async def test_create_hotel_derives_country_and_audits() -> None:
    conn = hotel_conn(destination_id=PLACE)
    data = {
        "supplier_id": "SUP-000001",
        "name": " Emerald ",
        "destination_id": PLACE,
        "amenities": ["Pool", "pool", " "],
        "links": [{"url": " https://x.test "}, {"label": "nothing"}],
    }
    await records.create_record(conn, HOTEL, CONTRACTING, data)
    insert = conn.sql("INSERT INTO booking.hotels")[0]
    assert insert[:2] == (SUP, "u-5") and "Emerald" in insert and ["Pool"] in insert
    assert json.dumps([{"label": "https://x.test", "url": "https://x.test"}]) in insert and "VN" in insert
    assert json.loads(conn.sql("INSERT INTO booking.audit_logs")[0][7])["code"] == "HTL-000001"


@pytest.mark.asyncio
async def test_create_record_refusals() -> None:
    with pytest.raises(Invalid) as exc:
        await records.create_record(hotel_conn(), HOTEL, CONTRACTING, {"supplier_id": SUP, "name": "  "})
    assert exc.value.code == "name_required"
    with pytest.raises(Invalid) as exc:
        await records.create_record(hotel_conn(), SERVICE, CONTRACTING, {"supplier_id": SUP, "title": "T"})
    assert exc.value.code == "service_type_required"
    inactive = hotel_conn().on("FROM booking.suppliers s", supplier_row(status="inactive"))
    with pytest.raises(Conflict):
        await records.create_record(inactive, HOTEL, CONTRACTING, {"supplier_id": SUP, "name": "H"})
    with pytest.raises(Invalid) as exc:
        await records.create_record(
            hotel_conn(), SERVICE, CONTRACTING, {"supplier_id": SUP, "title": "T", "service_type": "guide"}
        )
    assert exc.value.code == "supplier_category_missing"
    with pytest.raises(Invalid) as exc:
        await records.create_record(hotel_conn(), HOTEL, CONTRACTING, {"supplier_id": SUP, "name": "H", "latitude": 1})
    assert exc.value.code == "geo_pair"
    dup = hotel_conn().on("INSERT INTO booking.hotels", asyncpg.UniqueViolationError("dup"))
    with pytest.raises(Conflict) as exc:
        await records.create_record(dup, HOTEL, CONTRACTING, {"supplier_id": SUP, "name": "H"})
    assert exc.value.code == "hotel_exists"


@pytest.mark.asyncio
async def test_create_service_sets_source_system() -> None:
    conn = hotel_conn()
    await records.create_record(
        conn,
        SERVICE,
        CONTRACTING,
        {"supplier_id": SUP, "title": "Walk", "service_type": "transfer", "source_id": " c-1 ", "inclusions": ["A"]},
    )
    insert = conn.sql("INSERT INTO booking.services")[0]
    assert "c-1" in insert and "cis" in insert


@pytest.mark.asyncio
async def test_update_record_paths() -> None:
    with pytest.raises(Invalid) as exc:
        await records.update_record(hotel_conn(), HOTEL, CONTRACTING, HID, {"supplier_id": SUP})
    assert exc.value.code == "not_editable"
    same = await records.update_record(hotel_conn(), HOTEL, CONTRACTING, HID, {"name": " Emerald "})
    assert same["changed"] == []
    conn = hotel_conn()
    out = await records.update_record(conn, HOTEL, CONTRACTING, HID, {"name": "Jade", "star_rating": 5, "links": []})
    assert out["changed"] == ["name", "star_rating"]
    assert conn.sql("UPDATE booking.hotels SET")[0] == (HID, "Jade", 5, "u-5")
    # A place in Vietnam keeps the country: clearing it is a no-op.
    keep = await records.update_record(
        hotel_conn(destination_id=PLACE), HOTEL, CONTRACTING, HID, {"country_code": None}
    )
    assert keep["changed"] == []
    dup = hotel_conn().on("UPDATE booking.hotels", asyncpg.UniqueViolationError("dup"))
    with pytest.raises(Conflict):
        await records.update_record(dup, HOTEL, CONTRACTING, HID, {"name": "Other"})


@pytest.mark.asyncio
async def test_service_unit_and_type_changes() -> None:
    with_rates = hotel_conn().on("SELECT 1 FROM booking.supplier_rates", 1)
    with pytest.raises(Conflict) as exc:
        await records.update_record(with_rates, SERVICE, CONTRACTING, SID, {"unit": "group"})
    assert exc.value.code == "service_has_rates"
    conn = hotel_conn()
    out = await records.update_record(conn, SERVICE, CONTRACTING, SID, {"service_type": "transfer", "unit": "vehicle"})
    assert out["changed"] == ["service_type", "unit"]
    with pytest.raises(Invalid):
        await records.update_record(hotel_conn(), SERVICE, CONTRACTING, SID, {"service_type": None})


@pytest.mark.asyncio
async def test_set_status_paths() -> None:
    with pytest.raises(Invalid):
        await records.set_status(hotel_conn(), HOTEL, CONTRACTING, HID, "deleted")
    assert (await records.set_status(hotel_conn(), HOTEL, CONTRACTING, HID, "active"))["changed"] == []
    conn = hotel_conn()
    out = await records.set_status(conn, HOTEL, CONTRACTING, HID, "inactive", " closed ")
    assert (
        out["changed"] == ["status"]
        and json.loads(conn.sql("INSERT INTO booking.audit_logs")[0][7])["reason"] == "closed"
    )
    orphan = hotel_conn(status="inactive", supplier_status="inactive")
    with pytest.raises(Conflict) as exc:
        await records.set_status(orphan, HOTEL, CONTRACTING, HID, "active")
    assert exc.value.code == "supplier_inactive"
    await records.set_status(hotel_conn(status="inactive"), HOTEL, CONTRACTING, HID, "active")


@pytest.mark.asyncio
async def test_room_types() -> None:
    conn = hotel_conn()
    await records.create_room_type(conn, CONTRACTING, HID, {"name": " Suite ", "bed_types": ["King", "king"]})
    assert conn.sql("INSERT INTO booking.hotel_room_types")[0][2:] == ("Suite", ["King"])
    with pytest.raises(Invalid):
        await records.create_room_type(hotel_conn(), CONTRACTING, HID, {"name": " "})
    with pytest.raises(Conflict) as exc:
        await records.create_room_type(hotel_conn(status="inactive"), CONTRACTING, HID, {"name": "S"})
    assert exc.value.code == "hotel_inactive"
    dup = hotel_conn().on("INSERT INTO booking.hotel_room_types", asyncpg.UniqueViolationError("dup"))
    with pytest.raises(Conflict):
        await records.create_room_type(dup, CONTRACTING, HID, {"name": "Deluxe"})

    with pytest.raises(Invalid):
        await records.update_room_type(hotel_conn(), CONTRACTING, RID, {"hotel_id": HID})
    with pytest.raises(NotFound):
        await records.update_room_type(hotel_conn(), CONTRACTING, "nope", {"name": "x"})
    assert (await records.update_room_type(hotel_conn(), CONTRACTING, RID, {"name": "Deluxe"}))["changed"] == []
    conn = hotel_conn()
    out = await records.update_room_type(conn, CONTRACTING, RID, {"status": "inactive", "internal_notes": " n "})
    assert out["changed"] == ["internal_notes", "status"]
    with pytest.raises(Invalid):
        await records.update_room_type(hotel_conn(), CONTRACTING, RID, {"status": "gone"})
    off = hotel_conn(status="inactive").on("FROM booking.hotel_room_types t WHERE t.id", room_row(status="inactive"))
    with pytest.raises(Conflict):
        await records.update_room_type(off, CONTRACTING, RID, {"status": "active"})
    dup = hotel_conn().on("UPDATE booking.hotel_room_types", asyncpg.UniqueViolationError("dup"))
    with pytest.raises(Conflict):
        await records.update_room_type(dup, CONTRACTING, RID, {"name": "Suite"})


@pytest.mark.asyncio
async def test_search_options() -> None:
    conn = ScriptedConn([("UNION ALL", [{"kind": "service", "id": SID}])])
    assert (await records.search_options(conn, q="walk", kind="service", limit=99))["items"][0]["id"] == SID
    assert conn.calls[0][1][2:] == (records.MAX_OPTIONS, None)
    await records.search_options(conn, supplier_id=SUP)
    assert conn.calls[1][1][3] == SUP
    with pytest.raises(Invalid):
        await records.search_options(conn, kind="hotel")


# ── Rates ────────────────────────────────────────────────────────────────────


def contract_row(**kw: Any) -> dict[str, Any]:
    return {
        "id": KID,
        "supplier_id": SUP,
        "title": "2027",
        "status": "draft",
        "currency": "VND",
        "valid_from": date(2027, 1, 1),
        "valid_to": date(2027, 12, 31),
    } | kw


def rate_row(**kw: Any) -> dict[str, Any]:
    return {
        "id": XID,
        "contract_id": KID,
        "hotel_room_type_id": None,
        "service_id": SID,
        "service_code": "SVC-000001",
        "service_title": "Walk",
        "season": None,
        "valid_from": date(2027, 1, 1),
        "valid_to": date(2027, 12, 31),
        "unit": "person",
        "meal_plan": None,
        "pax_min": None,
        "pax_max": None,
        "net_amount": "300000.00",
        "currency": "VND",
        "notes": None,
    } | kw


def rate_conn(contract: dict[str, Any] | None = None, rate: dict[str, Any] | None = None) -> ScriptedConn:
    return ScriptedConn(
        [
            ("FROM booking.supplier_contracts k LEFT JOIN", contract or contract_row()),
            ("FROM booking.suppliers s", supplier_row()),
            ("FROM booking.services s WHERE s.id", {"supplier_id": SUP, "unit": "person"}),
            ("FROM booking.hotel_room_types t JOIN booking.hotels h", {"supplier_id": SUP, "unit": "room_night"}),
            ("INSERT INTO booking.supplier_rates", XID),
            ("WHERE x.id = $1::uuid", rate or rate_row()),
        ]
    )


@pytest.mark.asyncio
async def test_list_and_get_rates() -> None:
    for kwargs, fragment in (
        ({"contract_id": KID}, "x.supplier_contract_id = $1"),
        ({"hotel_id": HID}, "t.hotel_id = $1"),
        ({"service_id": SID}, "x.service_id = $1"),
    ):
        conn = ScriptedConn([("FROM booking.supplier_rates x", [rate_row()])])
        assert len((await rates.list_rates(conn, **kwargs))["items"]) == 1 and fragment in conn.calls[0][0]
    with pytest.raises(NotFound):
        await rates.get_rate(ScriptedConn(), XID)
    with pytest.raises(NotFound):
        await rates.get_rate(ScriptedConn(), "x")
    conn = ScriptedConn([("LIMIT 1", rate_row())])
    assert (await rates.rate_in_force(conn, service_id=SID, on=date(2027, 3, 1), pax=2))["id"] == XID
    assert await rates.rate_in_force(ScriptedConn(), room_type_id=RID, on=date(2027, 3, 1)) is None


def test_quantity_and_default_cost() -> None:
    assert rates.quantity("room_night", day_from=2, day_to=2, pax=3) == 1
    assert rates.quantity("room_night", day_from=1, day_to=4, pax=3) == 3
    assert rates.quantity("person", day_from=1, day_to=1, pax=None) == 1
    assert rates.quantity("day", day_from=1, day_to=3, pax=5) == 3
    assert rates.quantity("vehicle", day_from=1, day_to=3, pax=5) == 1
    room = rate_row(
        unit="room_night",
        hotel_room_type_id=RID,
        hotel_code="HTL-1",
        hotel_name="Emerald",
        room_type_name="Deluxe",
        meal_plan="BB",
        season="Low",
        net_amount="1200000.00",
    )
    cost = rates.default_cost(room, day_from=1, day_to=3, pax=2)
    assert (cost.net_amount, cost.net_currency, cost.supplier_rate_id) == (D("2400000.00"), "VND", XID)
    assert cost.supplier_note == "HTL-1 Emerald · Deluxe BB Low: 1200000.00 VND x 2 night"
    assert rates.default_cost(rate_row(), day_from=1, day_to=1, pax=3).supplier_note.endswith("x 3 pax")


@pytest.mark.asyncio
async def test_create_rate_defaults_to_the_contract_and_touches_its_maker() -> None:
    conn = rate_conn()
    await rates.create_rate(conn, CONTRACTING, KID, {"service_id": SID, "net_amount": D("300000"), "valid_from": None})
    insert = conn.sql("INSERT INTO booking.supplier_rates")[0]
    assert insert[:6] == (KID, None, SID, "person", "VND", "u-5") and date(2027, 1, 1) in insert
    assert conn.sql("SET updated_by = $2") == [(KID, "u-5")]
    assert json.loads(conn.sql("INSERT INTO booking.audit_logs")[0][7])["unit"] == "person"
    room = rate_conn(
        rate=rate_row(hotel_room_type_id=RID, service_id=None, unit="room_night", hotel_code="H")
        | {"hotel_name": "E", "room_type_name": "D"}
    )
    await rates.create_rate(
        room, CONTRACTING, KID, {"hotel_room_type_id": RID, "net_amount": D("1"), "meal_plan": "bb"}
    )
    assert "BB" in room.sql("INSERT INTO booking.supplier_rates")[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("conn_kw", "data", "error", "code"),
    [
        (
            {"contract": contract_row(status="published")},
            {"service_id": SID, "net_amount": 1},
            Conflict,
            "contract_locked",
        ),
        ({}, {"net_amount": 1}, Invalid, "rate_target"),
        ({}, {"service_id": SID}, Invalid, "net_amount_required"),
        ({}, {"service_id": SID, "net_amount": 1, "meal_plan": "BB"}, Invalid, "meal_plan_rooms_only"),
        ({}, {"hotel_room_type_id": RID, "net_amount": 1, "meal_plan": "XX"}, Invalid, "unknown_meal_plan"),
        ({}, {"service_id": SID, "net_amount": 1, "valid_to": date(2026, 1, 1)}, Invalid, "invalid_validity"),
        ({}, {"service_id": SID, "net_amount": 1, "valid_to": None}, Invalid, "rate_outside_contract"),
        ({}, {"service_id": SID, "net_amount": 1, "pax_min": 3, "pax_max": 2}, Invalid, "invalid_pax_band"),
    ],
)
async def test_create_rate_refusals(conn_kw: dict, data: dict, error: type, code: str) -> None:
    with pytest.raises(error) as exc:
        await rates.create_rate(rate_conn(**conn_kw), CONTRACTING, KID, data)
    assert exc.value.code == code


@pytest.mark.asyncio
async def test_create_rate_target_checks() -> None:
    missing = rate_conn().on("FROM booking.services s WHERE s.id", None)
    with pytest.raises(Invalid) as exc:
        await rates.create_rate(missing, CONTRACTING, KID, {"service_id": SID, "net_amount": 1})
    assert exc.value.code == "unknown_rate_target"
    other = rate_conn().on("FROM booking.services s WHERE s.id", {"supplier_id": "other", "unit": "person"})
    with pytest.raises(Invalid) as exc:
        await rates.create_rate(other, CONTRACTING, KID, {"service_id": SID, "net_amount": 1})
    assert exc.value.code == "rate_supplier_mismatch"
    backstop = rate_conn().on("INSERT INTO booking.supplier_rates", asyncpg.CheckViolationError("guard"))
    with pytest.raises(Invalid) as exc:
        await rates.create_rate(backstop, CONTRACTING, KID, {"service_id": SID, "net_amount": 1})
    assert exc.value.code == "invalid_rate"


@pytest.mark.asyncio
async def test_update_and_delete_rate() -> None:
    with pytest.raises(Invalid):
        await rates.update_rate(rate_conn(), CONTRACTING, XID, {"currency": "USD"})
    same = await rates.update_rate(rate_conn(), CONTRACTING, XID, {"net_amount": D("300000"), "season": None})
    assert same["changed"] == []
    conn = rate_conn()
    out = await rates.update_rate(conn, CONTRACTING, XID, {"net_amount": D("320000"), "season": " High "})
    assert out["changed"] == ["net_amount", "season"]
    assert conn.sql("UPDATE booking.supplier_rates SET")[0] == (XID, D("320000"), "High", "u-5")
    with pytest.raises(Invalid):
        await rates.update_rate(rate_conn(), CONTRACTING, XID, {"valid_from": None})
    backstop = rate_conn().on("UPDATE booking.supplier_rates", asyncpg.CheckViolationError("guard"))
    with pytest.raises(Invalid):
        await rates.update_rate(backstop, CONTRACTING, XID, {"notes": "x"})

    conn = rate_conn()
    await rates.delete_rate(conn, CONTRACTING, XID)
    assert conn.sql("DELETE FROM booking.supplier_rates") == [(XID,)]
    used = rate_conn().on("DELETE FROM booking.supplier_rates", asyncpg.ForeignKeyViolationError("used"))
    with pytest.raises(Conflict):
        await rates.delete_rate(used, CONTRACTING, XID)


# ── Version items booking supply ─────────────────────────────────────────────

CASE_ID = "77777777-7777-4777-8777-777777777777"
VERSION_ID = "88888888-8888-4888-8888-888888888888"
ITEM = "99999999-9999-4999-8999-999999999999"


def version_conn(stored: list[dict[str, Any]] | None = None) -> ScriptedConn:
    case = {"id": CASE_ID, "code": "TC-1", "status": "building", "start_date": date(2027, 3, 10), "pax": 2}
    version = {"id": VERSION_ID, "version_number": 1, "is_approved": False, "is_sent_to_customer": False}
    return ScriptedConn(
        [
            ("FROM booking.trip_cases tc", case),
            ("FROM booking.trip_case_versions v", version | {"total_price": None, "badge": "draft"}),
            ("SELECT id::text AS id, position, title", stored or []),
            ("SELECT id::text AS id, position FROM", [{"id": ITEM, "position": 1}]),
            ("FROM booking.hotel_room_types t JOIN booking.hotels h", [{"id": RID, "active": True}]),
            ("FROM booking.services s WHERE s.id = ANY", [{"id": SID, "active": False}]),
            ("k.status <> 'draft'", [{"id": XID}]),
        ]
    )


def stored_item(**kw: Any) -> dict[str, Any]:
    return {
        "id": ITEM,
        "position": 1,
        "title": "Stay",
        "day_from": 1,
        "day_to": 3,
        "source_tour_id": None,
        "notes": None,
        "hotel_room_type_id": None,
        "service_id": None,
        "costs": "[]",
        "supply": None,
    } | kw


@pytest.mark.asyncio
async def test_item_takes_the_rate_in_force() -> None:
    room = rate_row(unit="room_night", hotel_room_type_id=RID, service_id=None, hotel_code="H", hotel_name="E")
    conn = version_conn().on("LIMIT 1", room | {"room_type_name": "D", "net_amount": "100.00"})
    out = await versions.replace_items(
        conn, SALES, "TC-1", 1, [Item("Stay", 1, 3, hotel_room_type_id=RID), Item("Free day", 4, 4)]
    )
    assert out["rate_defaults"] == [
        {"position": 1, "on": date(2027, 3, 10), "rate_id": XID, "net_amount": "200.00", "net_currency": "VND"}
    ]
    assert conn.sql("INSERT INTO booking.version_item_costs")[0][1:3] == (D("200.00"), "VND")


@pytest.mark.asyncio
async def test_item_without_a_rate_in_force_is_reported() -> None:
    conn = version_conn(stored=[stored_item()])
    out = await versions.replace_items(conn, SALES, "TC-1", 1, [Item("Stay", 2, 3, id=ITEM, hotel_room_type_id=RID)])
    assert out["rate_defaults"] == [{"position": 1, "on": date(2027, 3, 11), "rate_id": None}]
    assert conn.sql("INSERT INTO booking.version_item_costs") == []


@pytest.mark.asyncio
async def test_item_supply_checks() -> None:
    with pytest.raises(Invalid) as exc:
        await versions.replace_items(version_conn(), SALES, "TC-1", 1, [Item("Walk", 1, 1, service_id=SID)])
    assert exc.value.code == "supply_inactive"
    # Already booked before: an inactive service stays on the item.
    kept = version_conn(
        stored=[stored_item(service_id=SID, costs=json.dumps([{"net_amount": "1", "net_currency": "VND"}]))]
    )
    await versions.replace_items(kept, SALES, "TC-1", 1, [Item("Walk", 1, 1, id=ITEM, service_id=SID)])
    unknown = version_conn().on("FROM booking.services s WHERE s.id = ANY", [])
    with pytest.raises(Invalid) as exc:
        await versions.replace_items(unknown, SALES, "TC-1", 1, [Item("Walk", 1, 1, service_id=SID)])
    assert exc.value.code == "unknown_supply"
    draft = version_conn().on("k.status <> 'draft'", [])
    with pytest.raises(Invalid) as exc:
        await versions.replace_items(
            draft, SALES, "TC-1", 1, [Item("Walk", 1, 1, costs=(Cost(D("1"), "VND", None, XID),))]
        )
    assert exc.value.code == "unknown_rate"
