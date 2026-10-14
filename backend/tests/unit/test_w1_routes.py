"""HTTP wiring of the Wave 1 routes: permissions, body validation, argument mapping (services are stubbed)."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import audit, reference
from app.auth import admin as staff_admin
from app.auth.staff import Staff, current_staff, get_pool
from app.customers import service as customers
from app.main import create_app
from app.notifications import service as notifications
from app.settings import Settings
from app.trip_cases import service, versions
from tests.unit.fakes import ScriptedConn

ADMIN = Staff("a-1", None, "Admin", "admin", True, "admin")
SALES = Staff("u-1", None, "Sales One", "sales", True, "reviewer")
CONTRACTING = Staff("u-5", None, "Contracts", "contracting", True, "reviewer")
CONTENT = Staff("u-9", None, "Writer", "content_creator", True, "content")
CUSTOMER_ID = "55555555-5555-4555-8555-555555555555"


class Recorder:
    def __init__(self, result: Any = None) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.result = {"ok": True} if result is None else result

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args[1:], kwargs))  # drop the connection
        return self.result


@pytest.fixture
def make_client():
    def _make(staff: Staff = SALES) -> TestClient:
        app = create_app(Settings(database_url=None, environment="test"))
        app.dependency_overrides[current_staff] = lambda: staff
        app.dependency_overrides[get_pool] = lambda: ScriptedConn()
        return TestClient(app)

    return _make


def stub(monkeypatch: pytest.MonkeyPatch, module: Any, name: str, result: Any = None) -> Recorder:
    rec = Recorder(result)
    monkeypatch.setattr(module, name, rec)
    return rec


# ── Trip cases ───────────────────────────────────────────────────────────────


def test_create_case_maps_body_and_requires_write(make_client, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = stub(monkeypatch, service, "create_case", {"code": "TC-000009"})
    r = make_client().post(
        "/booking/trip-cases",
        json={"customer_id": CUSTOMER_ID, "pax_adults": 2, "countries": ["VN"], "initial_status": "discovery"},
    )
    assert r.status_code == 201 and r.json()["code"] == "TC-000009"
    data = rec.calls[0][0][1]
    assert data["customer_id"] == CUSTOMER_ID and data["currency"] == "USD" and data["countries"] == ["VN"]
    assert make_client(CONTRACTING).post("/booking/trip-cases", json={"customer_id": CUSTOMER_ID}).status_code == 403


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"customer_id": CUSTOMER_ID, "new_customer": {"name": "x", "email": "x@y.test"}},
        {"new_customer": {"name": "x"}},
        {"new_customer": {"name": "x", "email": "not-an-email"}},
        {"customer_id": CUSTOMER_ID, "initial_status": "paid"},
        {"customer_id": CUSTOMER_ID, "countries": ["VNM"]},
    ],
)
def test_create_case_validation(make_client, body: dict[str, Any]) -> None:
    assert make_client().post("/booking/trip-cases", json=body).status_code == 422


def test_summary_activity_and_list_params(make_client, monkeypatch: pytest.MonkeyPatch) -> None:
    summary = stub(monkeypatch, service, "summary", {"open": 1})
    activity = stub(monkeypatch, service, "activity", {"items": []})
    listed = stub(monkeypatch, service, "list_cases", {"items": []})
    c = make_client(CONTRACTING)
    assert c.get("/booking/trip-cases/summary").json() == {"open": 1}
    assert summary.calls[0][0][0] is CONTRACTING
    assert c.get("/booking/trip-cases/TC-000001/activity?limit=5&before=x").status_code == 200
    assert activity.calls[0] == (("TC-000001",), {"before": "x", "limit": 5})
    r = c.get(
        f"/booking/trip-cases?q=ann&source=manual&customer_id={CUSTOMER_ID}"
        "&created_from=2026-10-01&created_to=2026-10-14&sort=-updated_at"
    )
    assert r.status_code == 200
    kwargs = listed.calls[0][1]
    assert kwargs["q"] == "ann" and kwargs["customer_id"] == CUSTOMER_ID and kwargs["sort"] == "-updated_at"
    assert kwargs["created_from"] == date(2026, 10, 1)
    assert make_client(CONTENT).get("/booking/trip-cases/summary").status_code == 403


def test_patch_case_accepts_w1_fields(make_client, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = stub(monkeypatch, service, "update_case")
    r = make_client().patch(
        "/booking/trip-cases/TC-1", json={"pax_adults": 2, "pax_children": 1, "countries": ["vn"], "trip_style": "x"}
    )
    assert r.status_code == 200
    assert rec.calls[0][0][2] == {"pax_adults": 2, "pax_children": 1, "countries": ["vn"], "trip_style": "x"}


def test_version_routes(make_client, monkeypatch: pytest.MonkeyPatch) -> None:
    got = stub(monkeypatch, versions, "get_version", {"version_number": 2})
    patched = stub(monkeypatch, versions, "update_version", {"title": "A"})
    deleted = stub(monkeypatch, versions, "delete_version", None)
    sent = stub(monkeypatch, versions, "send_version", {"badge": "sent"})
    approved = stub(monkeypatch, versions, "approve_version", {"badge": "approved"})
    c = make_client()
    assert c.get("/booking/trip-cases/TC-1/versions/2").json() == {"version_number": 2}
    assert got.calls[0][0] == ("TC-1", 2)
    assert c.patch("/booking/trip-cases/TC-1/versions/2", json={"title": "A"}).status_code == 200
    assert patched.calls[0][0][3] == {"title": "A"}
    assert c.patch("/booking/trip-cases/TC-1/versions/2", json={"badge": "x"}).status_code == 422
    assert c.delete("/booking/trip-cases/TC-1/versions/2").status_code == 204 and deleted.calls
    r = c.post(
        "/booking/trip-cases/TC-1/versions/2/send",
        json={"to": "ann@x.test", "subject": "Your trip", "message": "Hi", "valid_until": "2026-10-28"},
    )
    assert r.status_code == 200
    assert sent.calls[0][1] == {
        "to": "ann@x.test",
        "subject": "Your trip",
        "message": "Hi",
        "valid_until": date(2026, 10, 28),
    }
    assert c.post("/booking/trip-cases/TC-1/versions/2/send", json={"subject": "s"}).status_code == 422
    assert c.post("/booking/trip-cases/TC-1/versions/2/approve", json={"note": "phone"}).status_code == 200
    assert c.post("/booking/trip-cases/TC-1/versions/2/approve").status_code == 200
    assert [call[0][3] for call in approved.calls] == ["phone", None]
    assert make_client(CONTRACTING).post("/booking/trip-cases/TC-1/versions/2/approve").status_code == 403


def test_put_items_passes_ids(make_client, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = stub(monkeypatch, versions, "replace_items", {"items": []})
    item_id = "33333333-3333-4333-8333-333333333333"
    r = make_client().put(
        "/booking/trip-cases/TC-1/versions/1/items",
        json={
            "items": [
                {"id": item_id, "title": "A", "day_from": 1, "day_to": 1},
                {"title": "B", "day_from": 2, "day_to": 2},
            ]
        },
    )
    assert r.status_code == 200
    items = rec.calls[0][0][3]
    assert [i.id for i in items] == [item_id, None]


def test_cross_case_versions(make_client, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = stub(monkeypatch, versions, "list_versions", {"items": [], "total": 0})
    r = make_client().get("/booking/versions?badge=sent&assignee=me&updated_to=2026-10-14&sort=-updated_at")
    assert r.status_code == 200
    assert rec.calls[0][1]["assignee"] == "u-1" and rec.calls[0][1]["badge"] == "sent"
    assert make_client(CONTENT).get("/booking/versions").status_code == 403


# ── Customers ────────────────────────────────────────────────────────────────


def test_customer_routes_and_roles(make_client, monkeypatch: pytest.MonkeyPatch) -> None:
    listed = stub(monkeypatch, customers, "list_customers", {"items": [], "total": 0})
    got = stub(monkeypatch, customers, "get_customer", {"id": CUSTOMER_ID})
    created = stub(monkeypatch, customers, "create_lead", {"id": CUSTOMER_ID})
    patched = stub(monkeypatch, customers, "update_customer", {"changed": []})
    archived = stub(monkeypatch, customers, "archive_customer", None)
    c = make_client(CONTRACTING)
    assert c.get("/booking/customers?status=lead&merge_review=true&q=ann").status_code == 200
    assert listed.calls[0][1]["status"] == "lead" and listed.calls[0][1]["merge_review"] is True
    assert c.get("/booking/customers/CUS-000001").status_code == 200 and got.calls[0][0] == ("CUS-000001",)
    assert c.post("/booking/customers", json={"name": "Ann", "phone": "+84 1"}).status_code == 403
    s = make_client(SALES)
    assert s.post("/booking/customers", json={"name": "Ann", "phone": "+84 1"}).status_code == 201
    assert created.calls[0][0][1]["phone"] == "+84 1" and got.calls[-1][0] == (CUSTOMER_ID,)
    assert s.patch("/booking/customers/CUS-1", json={"is_blocked": True}).status_code == 200
    assert patched.calls[0][0][2] == {"is_blocked": True}
    assert s.patch("/booking/customers/CUS-1", json={"verified": True}).status_code == 422
    assert s.delete("/booking/customers/CUS-1").status_code == 403
    assert make_client(ADMIN).delete("/booking/customers/CUS-1").status_code == 204 and archived.calls
    assert make_client(CONTENT).get("/booking/customers").status_code == 403


# ── Notifications, staff, reference ──────────────────────────────────────────


def test_notification_routes(make_client, monkeypatch: pytest.MonkeyPatch) -> None:
    feed = stub(monkeypatch, notifications, "list_feed", {"items": [], "unread_count": 0})
    marked = stub(monkeypatch, notifications, "mark_read", {"marked": 1, "unread_count": 0})
    c = make_client(CONTENT)  # any active staff
    assert c.get("/booking/notifications?unread=true&type=trip_case.assigned&limit=5").status_code == 200
    assert feed.calls[0][1] == {"unread": True, "type": "trip_case.assigned", "q": None, "before": None, "limit": 5}
    note_id = "66666666-6666-4666-8666-666666666666"
    assert c.post("/booking/notifications/read", json={"ids": [note_id]}).status_code == 200
    assert c.post("/booking/notifications/read", json={"all": True}).status_code == 200
    assert [call[1] for call in marked.calls] == [{"ids": [note_id], "all_": False}, {"ids": None, "all_": True}]
    assert c.post("/booking/notifications/read", json={"all": True, "ids": [note_id]}).status_code == 422


def test_staff_routes(make_client, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = stub(monkeypatch, staff_admin, "directory", [{"cis_user_id": "u-1"}])
    detail = stub(monkeypatch, staff_admin, "staff_detail", {"cis_user_id": "u-1"})
    get_prefs = stub(monkeypatch, staff_admin, "get_preferences", {"notify": {}})
    set_prefs = stub(monkeypatch, staff_admin, "set_preferences", {"notify": {"proposal.approved": False}})
    c = make_client(CONTRACTING)
    assert c.get("/booking/staff/directory?role=sales").json() == {"items": [{"cis_user_id": "u-1"}]}
    assert directory.calls[0][1] == {"role": "sales"}
    assert make_client(CONTENT).get("/booking/staff/directory").status_code == 403
    assert c.get("/booking/staff/u-1").status_code == 200 and detail.calls[0][0][1] == "u-1"
    assert c.get("/booking/staff/me/preferences").status_code == 200 and get_prefs.calls
    r = c.put("/booking/staff/me/preferences", json={"notify": {"proposal.approved": False}})
    assert r.status_code == 200 and set_prefs.calls[0][0][1] == {"proposal.approved": False}


def test_reference_and_audit_routes(make_client, monkeypatch: pytest.MonkeyPatch) -> None:
    stub(monkeypatch, reference, "list_settings", {"values": {"deposit_pct": 30}, "items": []})
    stub(monkeypatch, reference, "list_countries", [{"iso2": "VN", "name": "Vietnam"}])
    logs = stub(monkeypatch, audit, "list_logs", {"items": [], "next_before": None})
    c = make_client(CONTENT)
    assert c.get("/booking/settings").json()["values"] == {"deposit_pct": 30}
    assert c.get("/booking/countries").json() == {"items": [{"iso2": "VN", "name": "Vietnam"}]}
    assert c.get("/booking/audit-logs").status_code == 403
    entity = "77777777-7777-4777-8777-777777777777"
    r = make_client(ADMIN).get(f"/booking/audit-logs?actor=u-1&entity_id={entity}&from=2026-10-01&to=2026-10-14")
    assert r.status_code == 200
    assert logs.calls[0][1]["entity_id"] == entity and logs.calls[0][1]["date_to"] == date(2026, 10, 14)
