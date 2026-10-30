"""AA-996 Catalog → Countries / Destinations (app/catalog/geo.py, bk-029): the Python branches around the SQL (the SQL
runs against Postgres in tests/integration/test_bk029_geo_api.py) and the HTTP wiring of /booking/catalog/geo."""

from __future__ import annotations

from typing import Any

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.auth.staff import Staff, current_staff, get_pool
from app.catalog import geo
from app.errors import Conflict, Invalid, NotFound, Unavailable
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
        "hidden_from_customers": False,
        "hidden_reason": None,
        "merged_into": None,
    }
    row.update(over)
    return row


def country_row(**over: Any) -> dict[str, Any]:
    row = {
        "country_code": "VN",
        "name": "Vietnam",
        "destinations": 12,
        "tours": 30,
        "booking_reference": True,
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
        .on("SELECT count(*) FROM catalog.v_destination d LEFT JOIN catalog.v_destination_redirect rd", 1)
        .on("ORDER BY", [place_row()])
        .on("count(DISTINCT d.country_iso2)", {"total": 3, "active": 2, "inactive": 1, "countries": 2})
    )
    out = await geo.list_destinations(conn, q="ha_noi", country="vn", status="Inactive", sort="-photos", limit=500)
    assert out["total"] == 1 and out["limit"] == geo.MAX_PAGE
    assert out["summary"] == {"total": 3, "active": 2, "inactive": 1, "countries": 2}
    page_sql, page_args = next((s, a) for s, a in conn.calls if "ORDER BY" in s)
    assert "d.photo_count DESC" in page_sql
    assert page_args[:3] == ("%ha\\_noi%", "VN", "inactive")
    assert "d.country_iso2 = $2" in page_sql and "booking.countries" not in page_sql  # CIS's ISO code, no name match
    with pytest.raises(Invalid):
        await geo.list_destinations(ScriptedConn(), sort="password")
    with pytest.raises(Invalid):
        await geo.list_destinations(ScriptedConn(), status="gone")


@pytest.mark.asyncio
async def test_list_destinations_marks_and_filters_places_hidden_from_customers() -> None:
    """AA-997: staff still see a place CIS hides (suspect point) or merged, with the reason; ``visibility`` filters."""
    hidden = place_row(hidden_from_customers=True, hidden_reason="location_unconfirmed")
    conn = (
        ScriptedConn()
        .on("SELECT count(*) FROM", 1)
        .on("ORDER BY", [hidden])
        .on("count(DISTINCT d.country_iso2)", {"total": 3, "active": 3, "inactive": 0, "countries": 1, "hidden": 1})
    )
    out = await geo.list_destinations(conn, visibility="Hidden")
    assert out["items"][0]["lat"] == 21.03 and out["items"][0]["hidden_reason"] == "location_unconfirmed"
    assert out["summary"]["hidden"] == 1
    page_sql, _ = next((s, a) for s, a in conn.calls if "ORDER BY" in s)
    assert f"WHERE {geo.HIDDEN_SQL}" in page_sql
    assert "LEFT JOIN catalog.v_destination_redirect rd ON rd.destination_id = d.destination_id" in page_sql
    assert "'merged'" in page_sql and "'location_unconfirmed'" in page_sql
    assert "rd.keeper_id::text AS merged_into" in page_sql
    summary_sql = next(s for s, _ in conn.calls if "count(DISTINCT d.country_iso2)" in s)
    assert "LEFT JOIN catalog.v_destination_redirect rd" in summary_sql and "AS hidden" in summary_sql
    shown = ScriptedConn().on("SELECT count(*) FROM", 0)
    await geo.list_destinations(shown, visibility="shown", q="ha")
    shown_sql = next(s for s, _ in shown.calls if "ORDER BY" in s)
    assert f"AND NOT {geo.HIDDEN_SQL}" in shown_sql
    with pytest.raises(Invalid):
        await geo.list_destinations(ScriptedConn(), visibility="secret")


def test_hidden_reason_prefers_merged_over_an_unconfirmed_point() -> None:
    """A merged row that is also suspect reads ``merged``: staff fix it by looking at the keeper."""
    assert geo.HIDDEN_REASON_SQL.index("'merged'") < geo.HIDDEN_REASON_SQL.index("'location_unconfirmed'")
    assert "d.geo_status = 'suspect'" in geo.HIDDEN_SQL and "rd.keeper_id IS NOT NULL" in geo.HIDDEN_SQL


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
    assert conn.sql("pg_advisory_xact_lock") == [("booking.destination_profiles", PLACE)]
    audit = conn.sql("INSERT INTO booking.audit_logs")
    assert len(audit) == 1 and audit[0][3] == "destination.deactivated" and audit[0][4] == "destination_profile"


@pytest.mark.asyncio
async def test_list_countries_args_and_areas() -> None:
    conn = (
        ScriptedConn()
        .on("ORDER BY c.name", [country_row(area="Indochina"), country_row(country_code="KH", name="Cambodia")])
        .on("d.country_iso2 IS NULL GROUP BY d.country", [{"name": "Nowhere", "destinations": 1}])
        .on("count(DISTINCT path) AS photos", [{"iso2": "VN", "photos": 7, "cover_photo_path": "/content/photos/v"}])
    )
    out = await geo.list_countries(conn, q="viet", status="inactive", area="Indochina")
    assert out["total"] == 2 and out["areas"] == ["Indochina"] and out["unmapped"][0]["name"] == "Nowhere"
    assert [(i["country_code"], i["photos"], i["cover_photo_path"]) for i in out["items"]] == [
        ("VN", 7, "/content/photos/v"),
        ("KH", 0, None),
    ]
    _, args = next((s, a) for s, a in conn.calls if "ORDER BY c.name" in s)
    assert args == ("%viet%", "inactive", "Indochina")
    sql, _ = next((s, a) for s, a in conn.calls if "ORDER BY c.name" in s)
    assert "c.iso2 = $" not in sql


@pytest.mark.asyncio
async def test_list_countries_matches_the_iso_code_only_for_a_two_letter_query() -> None:
    conn = ScriptedConn()
    await geo.list_countries(conn, q="Myanmar")  # must not also match MY (Malaysia)
    sql, args = next((s, a) for s, a in conn.calls if "ORDER BY c.name" in s)
    assert args == ("%Myanmar%",) and "c.iso2 = $" not in sql
    conn = ScriptedConn()
    await geo.list_countries(conn, q=" vn ")
    sql, args = next((s, a) for s, a in conn.calls if "ORDER BY c.name" in s)
    assert args == ("%vn%", "VN") and "c.iso2 = $2" in sql


@pytest.mark.asyncio
async def test_country_detail_and_update() -> None:
    with pytest.raises(NotFound):
        await geo.get_country(ScriptedConn(), "zz")
    conn = (
        ScriptedConn()
        .on("WHERE c.iso2 = $1", country_row())
        .on("WHERE d.country_iso2 = $1 ORDER BY lower(d.name)", [place_row()])
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
    assert conn.sql("pg_advisory_xact_lock") == [("booking.country_profiles", "VN")]


def country_conn(row: dict[str, Any]) -> ScriptedConn:
    """A conn answering the country row; its place and tour lists are empty."""
    return (
        ScriptedConn()
        .on("WHERE c.iso2 = $1", row)
        .on("ORDER BY lower(d.name)", [])
        .on("FROM catalog.v_tour t LEFT JOIN booking.trip_plans", [])
    )


@pytest.mark.asyncio
async def test_update_country_with_defaults_adds_an_unlisted_country_once() -> None:
    unlisted = country_row(country_code="JP", name="Japan", destinations=0, photos=0, tours=0, active_suppliers=0)
    conn = country_conn(unlisted).on("INSERT INTO booking.country_profiles", "c0000000-0000-4000-8000-000000000002")
    out = await geo.update_country(conn, ADMIN, "jp", {"status": "active"})
    assert out["changed"] == []
    assert conn.sql("INSERT INTO booking.country_profiles") == [("JP", "active", None, None, None, "a-1")]
    audit = conn.sql("INSERT INTO booking.audit_logs")
    assert len(audit) == 1 and audit[0][3] == "country.added" and audit[0][6] is None
    assert audit[0][9] == "Japan added to the Booking country list"

    # Already has Booking state: a no-op body writes nothing.
    stored = {**unlisted, "profile_updated_at": "2026-10-27T00:00:00Z"}
    conn = country_conn(stored)
    assert (await geo.update_country(conn, ADMIN, "JP", {}))["changed"] == []
    assert conn.sql("INSERT INTO booking.country_profiles") == [] and conn.sql("INSERT INTO booking.audit_logs") == []

    # Listed through CIS places (no profile yet): a no-op body writes nothing either.
    conn = country_conn(country_row())
    assert (await geo.update_country(conn, ADMIN, "VN", {}))["changed"] == []
    assert conn.sql("INSERT INTO booking.country_profiles") == []


# ── AA-996 fix: countries from CIS ISO codes, images from CIS photos ─────────────


@pytest.mark.asyncio
async def test_countries_come_from_cis_iso_codes_not_booking_country_names() -> None:
    """Pakistan / Taiwan / Kyrgyzstan were missing: the list started from booking.countries (16 markets) and matched
    place country names to it. It now starts from catalog.v_country and groups places by CIS's country_iso2."""
    pk = country_row(country_code="PK", name="Pakistan", booking_reference=False)
    conn = ScriptedConn().on("ORDER BY c.name", [country_row(), pk])
    out = await geo.list_countries(conn)
    assert [i["country_code"] for i in out["items"]] == ["VN", "PK"]
    sql, _ = next((s, a) for s, a in conn.calls if "ORDER BY c.name" in s)
    assert "FROM catalog.v_country c" in sql and "LEFT JOIN booking.countries bc ON bc.iso2 = c.iso2" in sql
    assert "d.country_iso2 AS iso2" in sql and "lower(d.country)" not in sql
    assert "FROM catalog.v_tour t JOIN catalog.v_country c ON lower(c.name) = lower(t.country)" in sql
    # "Places without a country" = places CIS could not resolve, not places outside Booking's own list.
    unmapped_sql = next(s for s, _ in conn.calls if "GROUP BY d.country ORDER BY" in s)
    assert "d.country_iso2 IS NULL" in unmapped_sql and "booking.countries" not in unmapped_sql


@pytest.mark.asyncio
async def test_country_images_count_distinct_cis_photo_paths_of_places_and_tours() -> None:
    gallery = [{"path": "/content/photos/p1", "caption": "Paro", "destination_id": PLACE}]
    conn = (
        country_conn(country_row(country_code="BT", name="Bhutan"))
        .on("count(DISTINCT path) AS photos", [{"iso2": "BT", "photos": 12, "cover_photo_path": "/content/photos/p1"}])
        .on("SELECT DISTINCT ON (path)", gallery)
    )
    out = await geo.get_country(conn, "bt")
    assert out["photos"] == 12 and out["cover_photo_path"] == "/content/photos/p1" and out["photo_items"] == gallery
    sql, args = next((s, a) for s, a in conn.calls if "count(DISTINCT path)" in s)
    assert args == ("BT",) and "AND iso2 = $1" in sql
    assert "FROM catalog.v_destination_photo dp JOIN catalog.v_destination d" in sql
    assert "FROM catalog.v_tour_photo tp" in sql and "WHERE tp.destination_id IS NULL" in sql
    # A tour photo tied to a place CIS could not give a country counts for the tour's country (else for none).
    assert "pd.country_iso2 IS NOT NULL" in sql
    # Cover = first photo of the best-covered place, then tour; the gallery uses the same order, capped.
    assert "ORDER BY kind, owner_photos DESC, owner, position NULLS LAST, path" in sql
    items_sql, items_args = next((s, a) for s, a in conn.calls if "SELECT DISTINCT ON (path)" in s)
    assert items_args == ("BT", geo.MAX_COUNTRY_PHOTOS) and "WHERE iso2 = $1" in items_sql
    # The CIS photo views missing or not granted: the country still loads, its image count is 0.
    conn = (
        country_conn(country_row(country_code="BT", name="Bhutan"))
        .on("count(DISTINCT path) AS photos", asyncpg.UndefinedTableError("no view"))
        .on("SELECT DISTINCT ON (path)", asyncpg.UndefinedTableError("no view"))
    )
    missing = await geo.get_country(conn, "BT")
    assert missing["photos"] == 0 and missing["cover_photo_path"] is None and missing["photo_items"] == []
    conn = (
        ScriptedConn()
        .on("ORDER BY c.name", [country_row()])
        .on("count(DISTINCT path) AS photos", asyncpg.InsufficientPrivilegeError("no grant"))
    )
    assert (await geo.list_countries(conn))["items"][0]["photos"] == 0


@pytest.mark.asyncio
async def test_update_country_outside_booking_reference_is_409_and_writes_nothing() -> None:
    conn = country_conn(country_row(country_code="TW", name="Taiwan", booking_reference=False))
    with pytest.raises(Conflict) as err:
        await geo.update_country(conn, ADMIN, "tw", {"status": "inactive"})
    assert err.value.code == "country_not_in_booking"
    assert conn.sql("INSERT INTO booking.country_profiles") == [] and conn.sql("INSERT INTO booking.audit_logs") == []


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
        r = c.get(
            "/booking/catalog/geo/destinations?q=ha&country=VN&status=inactive&visibility=hidden"
            "&limit=50&offset=25&sort=name"
        )
        assert r.status_code == 200
        assert listing.calls[0][1] == {
            "q": "ha",
            "country": "VN",
            "status": "inactive",
            "visibility": "hidden",
            "sort": "name",
            "limit": 50,
            "offset": 25,
        }
        assert c.get(f"/booking/catalog/geo/destinations/{PLACE}").status_code == 200
        assert c.get("/booking/catalog/geo/countries?status=active").status_code == 200
        assert c.get("/booking/catalog/geo/countries/VN").status_code == 200
        assert c.get("/booking/catalog/geo/countries/V1").status_code == 422
        assert c.get("/booking/catalog/geo/destinations/not-a-uuid").status_code == 422
        assert c.get("/booking/catalog/geo/destinations?visibility=all").status_code == 422
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
