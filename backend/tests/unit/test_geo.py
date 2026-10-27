"""AA-996 Catalog → Countries / Destinations (app/catalog/geo.py, bk-029): the Python branches around the SQL (the SQL
runs against Postgres in tests/integration/test_bk029_geo_api.py) and the HTTP wiring of /booking/catalog/geo."""

from __future__ import annotations

from typing import Any

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.auth.staff import Staff, current_staff, get_pool
from app.catalog import geo
from app.errors import Invalid, NotFound, Unavailable
from app.main import create_app
from app.settings import Settings
from tests.unit.fakes import ScriptedConn

ADMIN = Staff("a-1", None, "Admin", "admin", True, "admin")
SALES = Staff("u-1", None, "Sales One", "sales", True, "reviewer")
CONTENT = Staff("u-9", None, "Writer", "content_creator", True, "content")
PLACE = "d0000000-0000-4000-8000-000000000001"


def place_row(**over: Any) -> dict[str, Any]:
    row = {
        "destination_id": PLACE,
        "name": "Hanoi",
        "country": "Vietnam",
        "country_code": "VN",
        "lat": 21.03,
        "lng": 105.85,
        "cover_photo_path": None,
        "cover_image_url": None,
        "photo_count": 4,
        "status": "active",
        "status_reason": None,
        "internal_notes": None,
        "profile_updated_at": None,
        "profile_updated_by": None,
        "profile_updated_by_name": None,
        "active_suppliers": 2,
        "active_hotels": 1,
        "active_services": 0,
    }
    row.update(over)
    return row


def country_row(**over: Any) -> dict[str, Any]:
    row = {
        "country_code": "VN",
        "name": "Vietnam",
        "destinations": 12,
        "photos": 40,
        "tours": 30,
        "active_suppliers": 5,
        "status": "active",
        "area": None,
        "status_reason": None,
        "internal_notes": None,
        "profile_updated_at": None,
        "profile_updated_by": None,
        "profile_updated_by_name": None,
    }
    row.update(over)
    return row


# ── Service ──────────────────────────────────────────────────────────────────


def test_apply_keeps_absent_fields_and_clears_the_reason_on_activate() -> None:
    before = {"status": "inactive", "status_reason": "closed", "internal_notes": "keep"}
    assert geo._apply(before, {"status": "active"}) == {
        "status": "active",
        "status_reason": None,
        "internal_notes": "keep",
    }
    after = geo._apply(
        {"status": "active", "status_reason": None, "internal_notes": None},
        {"status": "inactive", "status_reason": "  season  ", "internal_notes": "  "},
    )
    assert after == {"status": "inactive", "status_reason": "season", "internal_notes": None}
    with pytest.raises(Invalid):
        geo._apply(before, {"status": "deleted"})


def test_action_and_summary() -> None:
    a = {"status": "active", "status_reason": None}
    i = {"status": "inactive", "status_reason": "flooded"}
    assert geo._action("destination", a, i) == "destination.deactivated"
    assert geo._action("country", i, a) == "country.activated"
    assert geo._action("country", a, a) == "country.updated"
    assert geo._summary("Hanoi", a, i, ["status", "status_reason"]) == "Hanoi deactivated in Booking: flooded"
    assert geo._summary("Hanoi", a, a, ["internal_notes"]) == "Hanoi: internal_notes updated"


@pytest.mark.asyncio
async def test_list_destinations_filters_bind_values_and_sorts() -> None:
    conn = (
        ScriptedConn()
        .on("SELECT count(*) FROM catalog.v_destination d LEFT JOIN booking.countries", 1)
        .on("ORDER BY", [place_row()])
        .on("count(DISTINCT lower(d.country))", {"total": 3, "active": 2, "inactive": 1, "countries": 2})
    )
    out = await geo.list_destinations(conn, q="ha_noi", country="vn", status="Inactive", sort="-photos", limit=500)
    assert out["total"] == 1 and out["limit"] == geo.MAX_PAGE
    assert out["summary"] == {"total": 3, "active": 2, "inactive": 1, "countries": 2}
    page_sql, page_args = next((s, a) for s, a in conn.calls if "ORDER BY" in s)
    assert "d.photo_count DESC" in page_sql
    assert page_args[:3] == ("%ha\\_noi%", "VN", "inactive")
    with pytest.raises(Invalid):
        await geo.list_destinations(ScriptedConn(), sort="password")
    with pytest.raises(Invalid):
        await geo.list_destinations(ScriptedConn(), status="gone")


@pytest.mark.asyncio
async def test_list_destinations_catalog_missing_is_503() -> None:
    conn = ScriptedConn().on("catalog.v_destination", asyncpg.UndefinedTableError("missing"))
    with pytest.raises(Unavailable):
        await geo.list_destinations(conn)


@pytest.mark.asyncio
async def test_destination_detail_404_and_photos_fail_soft() -> None:
    with pytest.raises(NotFound):
        await geo.get_destination(ScriptedConn(), PLACE)
    conn = (
        ScriptedConn()
        .on("WHERE d.destination_id = $1::uuid", place_row())
        .on("catalog.v_destination_photo", asyncpg.UndefinedTableError("no view"))
    )
    out = await geo.get_destination(conn, PLACE)
    assert out["name"] == "Hanoi" and out["photos"] == [] and out["active_suppliers"] == 2


@pytest.mark.asyncio
async def test_update_destination_upserts_and_audits_only_changes() -> None:
    conn = ScriptedConn().on("WHERE d.destination_id = $1::uuid", place_row())
    same = await geo.update_destination(conn, ADMIN, PLACE, {"status": "active"})
    assert same["changed"] == [] and conn.sql("INSERT INTO booking.destination_profiles") == []

    conn = ScriptedConn().on("WHERE d.destination_id = $1::uuid", place_row())
    out = await geo.update_destination(conn, ADMIN, PLACE, {"status": "inactive", "status_reason": "flooded"})
    assert out["changed"] == ["status", "status_reason"]
    assert conn.sql("INSERT INTO booking.destination_profiles") == [(PLACE, "inactive", "flooded", None, "a-1")]
    audit = conn.sql("INSERT INTO booking.audit_logs")
    assert len(audit) == 1 and audit[0][3] == "destination.deactivated" and audit[0][4] == "destination_profile"


@pytest.mark.asyncio
async def test_list_countries_args_and_areas() -> None:
    conn = (
        ScriptedConn()
        .on("ORDER BY c.name", [country_row(area="Indochina"), country_row(country_code="KH", name="Cambodia")])
        .on("NOT EXISTS (SELECT 1 FROM booking.countries", [{"name": "Nowhere", "destinations": 1}])
    )
    out = await geo.list_countries(conn, q="viet", status="inactive", area="Indochina")
    assert out["total"] == 2 and out["areas"] == ["Indochina"] and out["unmapped"][0]["name"] == "Nowhere"
    _, args = next((s, a) for s, a in conn.calls if "ORDER BY c.name" in s)
    assert args == ("%viet%", "vi", "inactive", "Indochina")


@pytest.mark.asyncio
async def test_country_detail_and_update() -> None:
    with pytest.raises(NotFound):
        await geo.get_country(ScriptedConn(), "zz")
    conn = (
        ScriptedConn()
        .on("WHERE c.iso2 = $1", country_row())
        .on("WHERE c.iso2 = $1 ORDER BY lower(d.name)", [place_row()])
        .on(
            "FROM catalog.v_tour t LEFT JOIN booking.trip_plans", [{"source_id": "s-1", "title": "T", "sellable": True}]
        )
        .on("INSERT INTO booking.country_profiles", "c0000000-0000-4000-8000-000000000001")
    )
    out = await geo.get_country(conn, "vn")
    assert out["country_code"] == "VN" and len(out["destination_items"]) == 1 and out["tour_items"][0]["sellable"]
    updated = await geo.update_country(conn, ADMIN, "vn", {"status": "inactive", "area": "Indochina"})
    assert updated["changed"] == ["area", "status"]
    assert conn.sql("INSERT INTO booking.country_profiles") == [("VN", "inactive", None, "Indochina", None, "a-1")]
    audit = conn.sql("INSERT INTO booking.audit_logs")
    assert audit[0][3] == "country.deactivated" and audit[0][8] == "Vietnam (VN)"


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
    monkeypatch.setattr(geo, "list_destinations", listing)
    monkeypatch.setattr(geo, "get_destination", Recorder({"destination_id": PLACE}))
    update_dest = Recorder({"changed": ["status"]})
    monkeypatch.setattr(geo, "update_destination", update_dest)
    monkeypatch.setattr(geo, "list_countries", Recorder({"items": []}))
    monkeypatch.setattr(geo, "get_country", Recorder({"country_code": "VN"}))
    update_country = Recorder({"changed": []})
    monkeypatch.setattr(geo, "update_country", update_country)

    with make_client(CONTENT) as c:  # every staff role reads
        r = c.get("/booking/catalog/geo/destinations?q=ha&country=VN&status=inactive&limit=50&offset=25&sort=name")
        assert r.status_code == 200
        assert listing.calls[0][1] == {
            "q": "ha",
            "country": "VN",
            "status": "inactive",
            "sort": "name",
            "limit": 50,
            "offset": 25,
        }
        assert c.get(f"/booking/catalog/geo/destinations/{PLACE}").status_code == 200
        assert c.get("/booking/catalog/geo/countries?status=active").status_code == 200
        assert c.get("/booking/catalog/geo/countries/VN").status_code == 200
        assert c.get("/booking/catalog/geo/countries/V1").status_code == 422
        assert c.get("/booking/catalog/geo/destinations/not-a-uuid").status_code == 422
        assert c.patch(f"/booking/catalog/geo/destinations/{PLACE}", json={"status": "inactive"}).status_code == 403
    with make_client(SALES) as c:
        assert c.patch("/booking/catalog/geo/countries/VN", json={"status": "inactive"}).status_code == 403

    with make_client(ADMIN) as c:
        r = c.patch(f"/booking/catalog/geo/destinations/{PLACE}", json={"status": "inactive", "status_reason": "x"})
        assert r.status_code == 200
        assert update_dest.calls[0][0] == (ADMIN, PLACE, {"status": "inactive", "status_reason": "x"})
        r = c.patch("/booking/catalog/geo/countries/vn", json={"area": "Indochina", "status": None})
        assert r.status_code == 200
        assert update_country.calls[0][0] == (ADMIN, "vn", {"area": "Indochina"})
        assert c.patch("/booking/catalog/geo/countries/VN", json={"status": "deleted"}).status_code == 422
        assert c.patch("/booking/catalog/geo/countries/VN", json={"color": "red"}).status_code == 422
