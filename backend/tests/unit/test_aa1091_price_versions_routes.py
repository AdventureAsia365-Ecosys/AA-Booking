"""AA-1091 (bk-054 / bk-055) HTTP layer of price list versions, season calendars, holiday events and a plan's
calendar: each route needs its permission (403 before the service runs), validates its body (422: extra fields, a
malformed diff SHA-256, an empty reason, a bad holiday code), and hands the service the converted values (ids as
text, the decision as a flag, the request body as a dict). The services are stubbed; their rules are in
test_aa1091_price_versions_service.py and test_aa1091_seasons_admin.py."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.auth.staff import Staff, current_staff, get_pool
from app.catalog import price_versions
from app.main import create_app
from app.pricing import seasons_admin
from app.settings import Settings
from tests.unit.fakes import ScriptedConn

MAKER = Staff("maker-1091", None, "Invented Maker", "contracting", True, None)
SALES = Staff("sales-1091", None, "Invented Seller", "sales", True, None)
CONTENT = Staff("content-1091", None, "Invented Writer", "content_creator", True, None)
SOURCE = "aa109100-0000-4000-8000-0000000000b1"
VID = "aa109100-0000-4000-8000-0000000000c3"
CAL = "aa109155-0000-4000-8000-0000000000c1"
EVENT = "aa109155-0000-4000-8000-0000000000e1"
SHA = "ab" * 32

PV = f"/booking/catalog/price-versions/{VID}"
PLAN = f"/booking/catalog/trip-plans/{SOURCE}"
CALS = "/booking/pricing/season-calendars"
EVENTS = "/booking/pricing/holiday-events"
ROW = {"channel": "b2b", "valid_from": "2031-01-01", "valid_to": "2031-03-31", "pax_min": 2, "amount": "600"}
DRAFT = {"travel_from": "2030-11-01", "travel_to": "2031-10-31", "items": [ROW]}
RANGE = {"season_code": "peak", "date_from": "2031-07-01", "date_to": "2031-08-31", "note": "School holidays"}
EVENT_BODY = {
    "country_iso2": "VN",
    "code": "invented_fest",
    "name": "Invented Festival",
    "date_from": "2031-02-10",
    "date_to": "2031-02-13",
    "reason": "Set by the board",
}

SERVICE_CALLS = {
    price_versions: (
        "list_versions",
        "create_draft",
        "pending",
        "get_version",
        "save_draft",
        "submit",
        "approve",
        "reject",
        "withdraw_preview",
        "withdraw",
    ),
    seasons_admin: (
        "plan_calendar",
        "request_plan_calendar",
        "decide_plan_calendar",
        "list_calendars",
        "create_calendar",
        "get_calendar",
        "propose_range",
        "propose_default",
        "decide_default",
        "decide_range",
        "list_events",
        "propose_event",
        "decide_event",
    ),
}


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, tuple[Any, ...], dict[str, Any]]]:
    seen: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
    for module, names in SERVICE_CALLS.items():
        for name in names:

            async def stub(*args: Any, _name: str = name, **kw: Any) -> dict[str, Any]:
                seen.append((_name, args[1:], kw))  # without the connection
                return {"called": _name}

            monkeypatch.setattr(module, name, stub)
    return seen


@pytest.fixture
def as_staff() -> Iterator[Any]:
    clients: list[TestClient] = []

    def make(staff: Staff) -> TestClient:
        app = create_app(Settings(database_url=None, environment="test"))
        app.dependency_overrides[current_staff] = lambda: staff
        app.dependency_overrides[get_pool] = lambda: ScriptedConn()
        client = TestClient(app)
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.close()


def test_every_route_reaches_its_service_with_converted_values(calls: list[Any], as_staff: Any) -> None:
    c = as_staff(MAKER)
    requests = [
        ("get", f"{PLAN}/price-versions", None, 200),
        ("post", f"{PLAN}/price-versions", {"copy_from": "none"}, 201),
        ("get", f"{PLAN}/season-calendar", None, 200),
        ("put", f"{PLAN}/season-calendar", {"season_calendar_id": CAL, "reason": "Inland"}, 200),
        ("put", f"{PLAN}/season-calendar", {"season_calendar_id": None, "reason": "No seasons"}, 200),
        ("post", f"{PLAN}/season-calendar/withdraw", {"reason": "Changed my mind"}, 200),
        ("get", "/booking/catalog/price-versions?status=pending", None, 200),
        ("get", PV, None, 200),
        ("put", PV, DRAFT, 200),
        ("post", f"{PV}/submit", {}, 200),
        ("post", f"{PV}/approve", {"diff_sha256": SHA}, 200),
        ("post", f"{PV}/reject", {"reason": "Too high"}, 200),
        ("get", f"{PV}/withdraw-preview", None, 200),
        ("post", f"{PV}/withdraw", {"reason": "Mistake"}, 200),
        ("get", f"{CALS}?owner_kind=destination", None, 200),
        ("post", CALS, {"owner_kind": "destination", "destination_id": CAL, "name": "Invented Coast"}, 201),
        ("get", f"{CALS}/{CAL}?year=2031", None, 200),
        ("post", f"{CALS}/{CAL}/ranges", RANGE, 201),
        ("post", f"{CALS}/{CAL}/default", {"default_season": "high", "reason": "Busy year"}, 200),
        ("post", f"{CALS}/{CAL}/default/reject", {"reason": "Not this year"}, 200),
        ("post", f"/booking/pricing/season-ranges/{CAL}/end", {"reason": "Moved"}, 200),
        ("get", f"{EVENTS}?country=vn&year=2031", None, 200),
        ("post", EVENTS, EVENT_BODY, 201),
        ("post", f"{EVENTS}/{EVENT}/approve", {}, 200),
    ]
    for method, url, body, status in requests:
        res = c.request(method, url, json=body)
        assert res.status_code == status, (url, res.text)
    by_name: dict[str, list[Any]] = {}
    for name, args, kw in calls:
        by_name.setdefault(name, []).append((args, kw))
    assert by_name["list_versions"] == [((MAKER, SOURCE), {})]
    assert by_name["create_draft"][0][0][2]["copy_from"] == "none"
    assert by_name["plan_calendar"] == [((SOURCE,), {})]
    assert by_name["request_plan_calendar"] == [
        ((MAKER, SOURCE, CAL, "Inland"), {}),
        ((MAKER, SOURCE, None, "No seasons"), {}),
    ]
    assert by_name["decide_plan_calendar"] == [((MAKER, SOURCE, "withdraw", "Changed my mind"), {})]
    assert by_name["pending"] == [((MAKER,), {})]
    assert by_name["get_version"] == [((MAKER, VID), {})]
    saved = by_name["save_draft"][0][0][2]
    assert str(saved["travel_to"]) == "2031-10-31" and saved["items"][0]["pax_min"] == 2
    assert by_name["submit"] == [((MAKER, VID, None), {})]
    assert by_name["approve"] == [((MAKER, VID, SHA), {})]
    assert by_name["reject"] == [((MAKER, VID, "Too high"), {})]
    assert by_name["withdraw"] == [((MAKER, VID, "Mistake"), {})]
    assert by_name["list_calendars"] == [((MAKER, "destination"), {})]
    assert by_name["create_calendar"][0][0][1]["destination_id"] == CAL  # a uuid handed on as text
    assert by_name["get_calendar"] == [((MAKER, CAL, 2031), {})]
    assert by_name["propose_range"][0][0][2]["kind"] == "season"
    assert by_name["propose_default"] == [((MAKER, CAL, "high", "Busy year"), {})]
    assert by_name["decide_default"] == [((MAKER, CAL), {"approve": False, "reason": "Not this year"})]
    assert by_name["decide_range"] == [((MAKER, CAL, "end", "Moved"), {})]
    assert by_name["list_events"] == [((MAKER, "vn", 2031), {})]
    assert by_name["propose_event"][0][0][1]["code"] == "invented_fest"
    assert by_name["decide_event"] == [((MAKER, EVENT, "approve", None), {})]


def test_permissions_are_checked_before_the_service(calls: list[Any], as_staff: Any) -> None:
    sales, content = as_staff(SALES), as_staff(CONTENT)
    # Reading needs pricing.read: a content creator has none of it.
    for url in (f"{PLAN}/price-versions", PV, f"{CALS}", f"{EVENTS}", f"{PLAN}/season-calendar"):
        assert content.get(url).status_code == 403, url
    assert sales.get(PV).status_code == 200
    # Sales read prices but make no version, season or event, and approve nothing.
    for method, url, body in (
        ("post", f"{PLAN}/price-versions", {}),
        ("put", PV, DRAFT),
        ("post", f"{PV}/submit", {}),
        ("post", f"{PV}/approve", {"diff_sha256": SHA}),
        ("post", f"{PV}/reject", {"reason": "No"}),
        ("post", CALS, {"owner_kind": "destination", "destination_id": CAL, "name": "X"}),
        ("post", f"{CALS}/{CAL}/ranges", RANGE),
        ("post", f"{CALS}/{CAL}/default/approve", {}),
        ("put", f"{PLAN}/season-calendar", {"season_calendar_id": None, "reason": "No"}),
        ("post", EVENTS, EVENT_BODY),
    ):
        assert sales.request(method, url, json=body).status_code == 403, url
    assert [name for name, *_ in calls] == ["get_version"]


@pytest.mark.parametrize(
    ("method", "url", "body"),
    [
        ("post", f"{PV}/approve", {"diff_sha256": "not-a-sha"}),
        ("post", f"{PV}/reject", {"reason": ""}),
        ("post", f"{PV}/withdraw", {}),
        ("post", f"{PV}/submit", {"reason": "x" * 301}),
        ("post", f"{PLAN}/price-versions", {"copy_from": "none", "surprise": 1}),
        ("post", f"{PLAN}/price-versions", {"tariff_year": 1999}),
        ("put", PV, {**DRAFT, "items": [{**ROW, "pax_min": 0}]}),
        ("put", PV, {"travel_from": "2030-11-01", "items": []}),
        ("post", f"{CALS}/{CAL}/ranges", {**RANGE, "holiday_code": "Bad Code"}),
        ("post", f"{CALS}/{CAL}/ranges", {**RANGE, "days_before": 61}),
        ("post", f"{CALS}/{CAL}/ranges", {**RANGE, "season_code": "monsoon"}),
        ("post", f"{CALS}/{CAL}/default", {"default_season": "high", "reason": ""}),
        ("post", f"{CALS}/{CAL}/default/maybe", {}),
        ("post", f"/booking/pricing/season-ranges/{CAL}/erase", {"reason": "x"}),
        ("post", f"{EVENTS}/{EVENT}/approve", {"reason": "x" * 301}),
        ("post", EVENTS, {**EVENT_BODY, "country_iso2": "VNM"}),
        ("put", f"{PLAN}/season-calendar", {"season_calendar_id": None}),
        ("post", f"{PLAN}/season-calendar/apply", {}),
        ("get", "/booking/catalog/price-versions?status=draft", None),
        ("get", "/booking/catalog/price-versions/not-a-uuid", None),
        ("get", f"{CALS}/{CAL}?year=1999", None),
    ],
)
def test_malformed_requests_are_422(
    method: str, url: str, body: dict[str, Any] | None, calls: list[Any], as_staff: Any
) -> None:
    assert as_staff(MAKER).request(method, url, json=body).status_code == 422
    assert calls == []
