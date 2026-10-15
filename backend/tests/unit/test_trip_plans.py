"""AA-679 trip plans: the Python branches around the catalog SQL (the SQL itself runs in
tests/integration/test_aa679_trip_plans_api.py against Postgres) and the HTTP wiring of /booking/catalog."""

from __future__ import annotations

from typing import Any

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.auth.staff import Staff, current_staff, get_pool
from app.catalog import trip_plans
from app.errors import Conflict, Invalid, NotFound, Unavailable
from app.main import create_app
from app.settings import Settings
from app.trip_cases import versions
from tests.unit.fakes import ScriptedConn

ADMIN = Staff("a-1", None, "Admin", "admin", True, "admin")
SALES = Staff("u-1", None, "Sales One", "sales", True, "reviewer")
CONTRACTING = Staff("u-5", None, "Contracts", "contracting", True, "reviewer")
CONTENT = Staff("u-9", None, "Writer", "content_creator", True, "content")
SOURCE_ID = "aa679000-0000-4000-8000-00000000000a"


def tour_row(**over: Any) -> dict[str, Any]:
    row = {
        "source_system": "cis",
        "source_id": SOURCE_ID,
        "tour_id": "t-1",
        "sku": "BT-7D",
        "title": "Bhutan Dragon Trails",
        "country": "Bhutan",
        "duration_days": 3,
        "highlights": '["Tiger Nest"]',
        "plan_id": "p-1",
        "sellable": True,
        "trip_style": "Private",
        "default_channel": "b2c_tailor_made",
        "price_hint_amount": "2450.00",
        "price_hint_currency": "USD",
        "price_hint_basis": "per_person",
        "commercial_notes": "net",
        "created_by": "u-5",
        "created_by_name": "Contracts",
        "updated_by": None,
        "updated_by_name": None,
        "commercial_created_at": None,
        "commercial_updated_at": None,
    }
    row.update(over)
    return row


# ── Service ──────────────────────────────────────────────────────────────────


def test_items_from_days_orders_titles_and_skips_bad_days() -> None:
    days = [
        {"day_number": 2, "title": "  "},
        {"day_number": 1, "title": "Arrive"},
        {"day_number": 0, "title": "Pre-day"},
        {"day_number": 400, "title": "Out of range"},
    ]
    items = trip_plans.items_from_days(days, tour_row())
    assert [(i.title, i.day_from, i.day_to, i.source_tour_id) for i in items] == [
        ("Arrive", 1, 1, SOURCE_ID),
        ("Day 2", 2, 2, SOURCE_ID),
    ]


@pytest.mark.parametrize(("duration", "day_to"), [(None, 1), (0, 1), (12, 12), (999, 366)])
def test_items_without_days_cover_the_duration(duration: int | None, day_to: int) -> None:
    items = trip_plans.items_from_days([], tour_row(duration_days=duration, title=None))
    assert [(i.title, i.day_from, i.day_to) for i in items] == [(SOURCE_ID, 1, day_to)]


def test_shape_hides_pricing_from_staff_without_pricing_read() -> None:
    sales = trip_plans._shape(tour_row(), SALES)
    assert sales["price_hint"] == {"amount": "2450.00", "currency": "USD", "basis": "per_person"}
    assert sales["has_commercial_row"] is True and sales["default_channel"] == "b2c_tailor_made"
    content = trip_plans._shape(tour_row(), CONTENT)
    assert content["price_hint"] is None and content["default_channel"] is None and content["sellable"] is True
    untouched = trip_plans._shape(tour_row(plan_id=None, price_hint_amount=None), SALES)
    assert untouched["has_commercial_row"] is False and untouched["price_hint"] is None


def test_plan_writers_can_read_pricing() -> None:
    # PUT …/commercial replaces every field. A writer without pricing.read would get price_hint / default_channel /
    # commercial_notes as null and wipe them on the next save, so every trip_plan.write role must hold pricing.read.
    from app.auth.staff import PERMISSIONS

    assert PERMISSIONS["trip_plan.write"] <= PERMISSIONS["pricing.read"]


@pytest.mark.parametrize(
    ("created", "new", "changed", "summary"),
    [
        (True, {"sellable": True}, ["sellable", "trip_style"], "Marked sellable"),
        (True, {"sellable": False}, ["sellable"], "Commercial details saved (not for sale)"),
        (False, {"sellable": False}, ["sellable"], "Marked not for sale"),
        (False, {"sellable": True}, ["sellable", "trip_style"], "Marked sellable; updated trip_style"),
        (False, {"sellable": True}, ["commercial_notes"], "Updated commercial_notes"),
    ],
)
def test_summary(created: bool, new: dict[str, Any], changed: list[str], summary: str) -> None:
    assert trip_plans._summary(created, new, changed) == summary


@pytest.mark.asyncio
async def test_list_builds_filters_and_rejects_unknown_sort() -> None:
    conn = ScriptedConn().on("SELECT count(*)", 1).on("ORDER BY", [tour_row()])
    out = await trip_plans.list_plans(
        conn, CONTENT, q=" 50% ", country=" Bhutan ", sellable=True, trip_style="Private", sort="-updated_at", limit=500
    )
    assert out["total"] == 1 and out["limit"] == trip_plans.MAX_PAGE and out["items"][0]["price_hint"] is None
    flat, args = next((f, a) for f, a in conn.calls if "ORDER BY" in f)
    assert "t.updated_at DESC" in flat and "t.source_id = $1" in flat and "t.title ILIKE $2" in flat
    assert args[:5] == ("50%", "%50\\%%", "Bhutan", True, "Private")
    with pytest.raises(Invalid):
        await trip_plans.list_plans(ScriptedConn(), SALES, sort="price")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        asyncpg.UndefinedTableError("x"),
        asyncpg.InsufficientPrivilegeError("x"),
        asyncpg.InvalidSchemaNameError("x"),
        asyncpg.QueryCanceledError("x"),  # statement_timeout
        TimeoutError(),  # the pool's client-side command_timeout
    ],
)
async def test_catalog_errors_are_unavailable(error: Exception) -> None:
    conn = ScriptedConn().on("catalog.v_tour", error)
    with pytest.raises(Unavailable):
        await trip_plans.facets(conn)


@pytest.mark.asyncio
async def test_facets_totals() -> None:
    conn = (
        ScriptedConn()
        .on(
            "GROUP BY t.country",
            [
                {"country": "Bhutan", "tours": 2, "sellable": 1},
                {"country": "Laos", "tours": 1, "sellable": 0},
                {"country": None, "tours": 2, "sellable": 1},  # counted in the KPIs, not a country option
            ],
        )
        .on("DISTINCT p.trip_style", [{"trip_style": "Private"}])
    )
    assert await trip_plans.facets(conn) == {
        "total": 5,
        "sellable": 2,
        "countries": [{"country": "Bhutan", "tours": 2, "sellable": 1}, {"country": "Laos", "tours": 1, "sellable": 0}],
        "trip_styles": ["Private"],
    }


@pytest.mark.asyncio
async def test_get_plan_missing_tour_is_404() -> None:
    with pytest.raises(NotFound):
        await trip_plans.get_plan(ScriptedConn(), SALES, SOURCE_ID)


def plan_conn(tour: dict[str, Any] | None = None, stored: dict[str, Any] | None = None) -> ScriptedConn:
    return (
        ScriptedConn()
        .on("FROM catalog.v_tour t LEFT JOIN", tour or tour_row())
        .on("FROM catalog.v_tour_day", [{"day_number": 1, "title": "Arrive", "body": "", "places": None}])
        .on("FROM catalog.v_tour_photo", [])
        .on("FROM booking.trip_cases", 0)
        .on("FROM booking.trip_plans WHERE", stored)
        .on("INSERT INTO booking.trip_plans", {"id": "p-1", "inserted": stored is None})
    )


STORED = {
    "id": "p-1",
    "sellable": True,
    "trip_style": "Private",
    "default_channel": "b2c_tailor_made",
    "price_hint_amount": "2450.00",
    "price_hint_currency": "USD",
    "price_hint_basis": "per_person",
    "commercial_notes": "net",
}
BODY = {
    "sellable": True,
    "trip_style": " Private ",
    "default_channel": "b2c_tailor_made",
    "price_hint": {"amount": "2450.0", "currency": "USD", "basis": "per_person"},
    "commercial_notes": "net",
}


@pytest.mark.asyncio
async def test_save_commercial_without_changes_writes_nothing() -> None:
    conn = plan_conn(stored=STORED)
    out = await trip_plans.save_commercial(conn, CONTRACTING, SOURCE_ID, BODY)
    assert out["changed"] == [] and conn.sql("INSERT INTO booking.trip_plans") == []
    assert conn.sql("INSERT INTO booking.audit_logs") == []
    assert out["days"][0]["places"] == [] and out["highlights"] == ["Tiger Nest"]


@pytest.mark.asyncio
async def test_save_commercial_update_audits_the_changed_fields() -> None:
    conn = plan_conn(stored=STORED)
    out = await trip_plans.save_commercial(conn, CONTRACTING, SOURCE_ID, {**BODY, "commercial_notes": None})
    assert out["changed"] == ["commercial_notes"]
    (audit_args,) = conn.sql("INSERT INTO booking.audit_logs")
    assert (
        audit_args[3] == "trip_plan.updated"
        and audit_args[8] == "BT-7D"
        and audit_args[9] == "Updated commercial_notes"
    )


@pytest.mark.asyncio
async def test_save_commercial_check_violation_is_422() -> None:
    conn = plan_conn().on("INSERT INTO booking.trip_plans", asyncpg.CheckViolationError("trip_plans_price_hint_chk"))
    with pytest.raises(Invalid):
        await trip_plans.save_commercial(conn, CONTRACTING, SOURCE_ID, {"sellable": True})


@pytest.mark.asyncio
async def test_use_for_trip_case_needs_a_sellable_plan() -> None:
    with pytest.raises(Conflict):
        await trip_plans.use_for_trip_case(plan_conn(tour_row(sellable=False)), SALES, SOURCE_ID, {})


@pytest.mark.asyncio
async def test_use_for_trip_case_new_case_and_new_version(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, Any] = {}

    async def create_case(conn: Any, staff: Staff, data: dict[str, Any], *, source: Any) -> dict[str, Any]:
        calls["case"] = (data, source)
        return {"id": "c-1"}

    async def create_version(conn: Any, staff: Staff, ref: str, notes: Any, *, seed: versions.Seed) -> dict[str, Any]:
        calls.setdefault("versions", []).append((ref, notes, seed))
        return {"version_number": 1}

    async def get_case(conn: Any, ref: str) -> dict[str, Any]:
        return {"id": ref}

    monkeypatch.setattr(trip_plans.trip_cases, "create_case", create_case)
    monkeypatch.setattr(trip_plans.trip_cases, "get_case", get_case)
    monkeypatch.setattr(trip_plans.versions, "create_version", create_version)

    conn = plan_conn().on("FROM booking.countries", "BT")
    out = await trip_plans.use_for_trip_case(conn, SALES, SOURCE_ID, {"customer_id": "x", "notes": "case notes"})
    assert out == {"mode": "new_case", "trip_case": {"id": "c-1"}, "version": {"version_number": 1}}
    data, source = calls["case"]
    assert source == ("cis", SOURCE_ID) and data["trip_style"] == "Private" and data["countries"] == ["BT"]
    ref, notes, seed = calls["versions"][0]
    assert ref == "c-1" and notes is None and seed.title == "Bhutan Dragon Trails" and seed.label == "BT-7D"
    assert seed.channel == "b2c_tailor_made" and [i.title for i in seed.items] == ["Arrive"]

    out = await trip_plans.use_for_trip_case(
        plan_conn(tour_row(country=None, sku=None)), SALES, SOURCE_ID, {"trip_case": "TC-000001", "notes": "opt 2"}
    )
    assert out["mode"] == "new_version" and out["trip_case"] == {"id": "TC-000001"}
    assert (
        calls["versions"][1][:2] == ("TC-000001", "opt 2") and calls["versions"][1][2].label == "Bhutan Dragon Trails"
    )


@pytest.mark.asyncio
async def test_country_code_without_country_or_match() -> None:
    assert await trip_plans._country_code(ScriptedConn(), None) == []
    assert await trip_plans._country_code(ScriptedConn(), "Atlantis") == []


# ── Routes ───────────────────────────────────────────────────────────────────


class Recorder:
    def __init__(self, result: Any) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.result = result

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args[1:], kwargs))
        return self.result


def make_client(staff: Staff) -> TestClient:
    app = create_app(Settings(database_url=None, environment="test"))
    app.dependency_overrides[current_staff] = lambda: staff
    app.dependency_overrides[get_pool] = lambda: ScriptedConn()
    return TestClient(app)


def test_routes_map_arguments_and_permissions(monkeypatch: pytest.MonkeyPatch) -> None:
    listing = Recorder({"items": [], "total": 0})
    monkeypatch.setattr(trip_plans, "list_plans", listing)
    monkeypatch.setattr(trip_plans, "facets", Recorder({"total": 0}))
    monkeypatch.setattr(trip_plans, "get_plan", Recorder({"source_id": SOURCE_ID}))
    save = Recorder({"changed": ["sellable"]})
    monkeypatch.setattr(trip_plans, "save_commercial", save)
    use = Recorder({"mode": "new_case"})
    monkeypatch.setattr(trip_plans, "use_for_trip_case", use)

    content = make_client(CONTENT)
    r = content.get("/booking/catalog/trip-plans", params={"q": "bt", "sellable": "false", "limit": 50, "offset": 50})
    assert r.status_code == 200
    assert listing.calls[0][1] == {
        "q": "bt",
        "country": None,
        "sellable": False,
        "trip_style": None,
        "sort": None,
        "limit": 50,
        "offset": 50,
    }
    assert content.get("/booking/catalog/trip-plans", params={"limit": 101}).status_code == 422
    assert content.get("/booking/catalog/trip-plans/facets").status_code == 200
    assert content.get(f"/booking/catalog/trip-plans/{SOURCE_ID.upper()}").json() == {"source_id": SOURCE_ID}
    assert content.get("/booking/catalog/trip-plans/BT-7D").status_code == 422

    body = {"sellable": True, "price_hint": {"amount": "10", "currency": "USD"}}
    url = f"/booking/catalog/trip-plans/{SOURCE_ID}/commercial"
    assert make_client(SALES).put(url, json=body).status_code == 403
    assert content.put(url, json=body).status_code == 403
    assert make_client(CONTRACTING).put(url, json=body).status_code == 200
    data = save.calls[0][0][2]
    assert data["price_hint"]["basis"] == "per_person" and data["trip_style"] is None
    bad_hint = {"sellable": True, "price_hint": {"amount": "0", "currency": "USD"}}
    assert make_client(ADMIN).put(url, json=bad_hint).status_code == 422
    assert make_client(ADMIN).put(url, json={"trip_style": "x"}).status_code == 422  # sellable is required

    use_url = f"/booking/catalog/trip-plans/{SOURCE_ID}/trip-cases"
    customer = "55555555-5555-4555-8555-555555555555"
    assert make_client(SALES).post(use_url, json={"customer_id": customer}).status_code == 201
    assert use.calls[0][0][2]["customer_id"] == customer and use.calls[0][0][2]["currency"] == "USD"
    assert make_client(SALES).post(use_url, json={"trip_case": "TC-000001"}).status_code == 201
    assert make_client(CONTRACTING).post(use_url, json={"customer_id": customer}).status_code == 403
    for invalid in (
        {},
        {"trip_case": "TC-1", "customer_id": customer},
        {"customer_id": customer, "new_customer": {"name": "A", "phone": "123"}},
    ):
        assert make_client(SALES).post(use_url, json=invalid).status_code == 422


def test_use_needs_version_write(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(trip_plans_permissions(), "version.write", frozenset({"admin"}))
    r = make_client(SALES).post(f"/booking/catalog/trip-plans/{SOURCE_ID}/trip-cases", json={"trip_case": "TC-000001"})
    assert r.status_code == 403


def trip_plans_permissions() -> dict[str, frozenset[str]]:
    from app.auth import staff

    return staff.PERMISSIONS
