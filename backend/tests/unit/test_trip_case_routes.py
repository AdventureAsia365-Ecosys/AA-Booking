"""HTTP wiring of /booking/trip-cases*: auth dependency, body validation, service calls, error mapping."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.auth.staff import Staff, current_staff, get_pool
from app.errors import Conflict, NotFound
from app.main import create_app
from app.routers import trip_cases as routes
from app.settings import Settings
from tests.unit.fakes import ScriptedConn

SALES = Staff("u-1", None, "Sales One", "sales", True, "reviewer")
CONTRACTING = Staff("u-5", None, "Contracts", "contracting", True, "reviewer")


class Recorder:
    def __init__(self, result: Any = None) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.result = result if result is not None else {"ok": True}

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args[1:], kwargs))  # drop the connection
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


@pytest.fixture
def make_client():
    def _make(staff: Staff = SALES) -> TestClient:
        app = create_app(Settings(database_url=None, environment="test"))
        conn = ScriptedConn()
        app.dependency_overrides[current_staff] = lambda: staff
        app.dependency_overrides[get_pool] = lambda: conn
        return TestClient(app)

    return _make


def test_list_passes_filters_and_resolves_me(make_client, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = Recorder({"items": [], "total": 0, "limit": 5, "offset": 0})
    monkeypatch.setattr(routes.service, "list_cases", rec)
    r = make_client().get("/booking/trip-cases?status=new_inquiry,discovery&status=building&assignee=me&limit=5")
    assert r.status_code == 200
    assert rec.calls[0][1] == {
        "statuses": ["new_inquiry", "discovery", "building"],
        "assignee": "u-1",
        "limit": 5,
        "offset": 0,
    }


def test_list_rejects_out_of_range_paging(make_client) -> None:
    assert make_client().get("/booking/trip-cases?limit=500").status_code == 422


def test_detail_maps_not_found(make_client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routes.service, "get_case", Recorder(NotFound("trip_case_not_found", "nope")))
    r = make_client().get("/booking/trip-cases/TC-000404")
    assert r.status_code == 404
    assert r.json() == {"detail": "trip_case_not_found", "message": "nope"}


def test_patch_sends_only_fields_that_were_set(make_client, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = Recorder()
    monkeypatch.setattr(routes.service, "update_case", rec)
    r = make_client().patch("/booking/trip-cases/TC-000001", json={"pax": 3, "notes": None})
    assert r.status_code == 200
    assert rec.calls[0][0] == (SALES, "TC-000001", {"pax": 3, "notes": None})


@pytest.mark.parametrize(
    "body", [{"currency": "usd"}, {"pax": 0}, {"status": "paid"}, {"start_date": "tomorrow"}], ids=str
)
def test_patch_validation(make_client, body: dict[str, Any]) -> None:
    assert make_client().patch("/booking/trip-cases/TC-000001", json=body).status_code == 422


def test_contracting_can_read_but_not_write(make_client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routes.service, "get_case", Recorder())
    client = make_client(CONTRACTING)
    assert client.get("/booking/trip-cases/TC-000001").status_code == 200
    r = client.post("/booking/trip-cases/TC-000001/status", json={"to_status": "discovery"})
    assert r.status_code == 403


def test_status_conflict_carries_allowed_transitions(make_client, monkeypatch: pytest.MonkeyPatch) -> None:
    err = Conflict("illegal_transition", "new_inquiry -> paid", allowed_transitions=["cancelled", "discovery"])
    monkeypatch.setattr(routes.service, "change_status", Recorder(err))
    r = make_client().post("/booking/trip-cases/TC-000001/status", json={"to_status": "paid", "reason": "x"})
    assert r.status_code == 409
    assert r.json()["allowed_transitions"] == ["cancelled", "discovery"]


def test_assign_requires_the_user_field(make_client, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = Recorder()
    monkeypatch.setattr(routes.service, "assign", rec)
    client = make_client()
    assert client.post("/booking/trip-cases/TC-000001/assign", json={}).status_code == 422
    assert client.post("/booking/trip-cases/TC-000001/assign", json={"cis_user_id": None}).status_code == 200
    assert rec.calls[0][0] == (SALES, "TC-000001", None, "sales")


def test_create_version_with_and_without_body(make_client, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = Recorder({"version_number": 1})
    monkeypatch.setattr(routes.versions, "create_version", rec)
    client = make_client()
    assert client.post("/booking/trip-cases/TC-000001/versions").status_code == 201
    assert client.post("/booking/trip-cases/TC-000001/versions", json={"notes": "v"}).status_code == 201
    assert [c[0][2] for c in rec.calls] == [None, "v"]


def test_items_get_and_put(make_client, monkeypatch: pytest.MonkeyPatch) -> None:
    get_rec, put_rec = Recorder({"items": []}), Recorder({"items": []})
    monkeypatch.setattr(routes.versions, "get_items", get_rec)
    monkeypatch.setattr(routes.versions, "replace_items", put_rec)
    client = make_client()
    assert client.get("/booking/trip-cases/TC-000001/versions/2/items").status_code == 200
    assert get_rec.calls[0][0] == ("TC-000001", 2)
    body = {"items": [{"title": "Hanoi", "day_from": 1, "day_to": 2, "source_tour_id": "t-1"}]}
    assert client.put("/booking/trip-cases/TC-000001/versions/2/items", json=body).status_code == 200
    staff, ref, number, items = put_rec.calls[0][0]
    assert (staff, ref, number) == (SALES, "TC-000001", 2)
    assert items[0] == routes.versions.Item("Hanoi", 1, 2, "t-1", None)


@pytest.mark.parametrize(
    "item",
    [
        {"title": "", "day_from": 1, "day_to": 1},
        {"title": "x", "day_from": 3, "day_to": 2},
        {"title": "x", "day_from": 0, "day_to": 1},
        {"title": "x", "day_from": 1, "day_to": 1, "price": 10},
    ],
    ids=["empty-title", "days-reversed", "day-zero", "extra-field"],
)
def test_items_validation(make_client, item: dict[str, Any]) -> None:
    r = make_client().put("/booking/trip-cases/TC-000001/versions/1/items", json={"items": [item]})
    assert r.status_code == 422


def test_version_number_must_be_positive(make_client) -> None:
    assert make_client().get("/booking/trip-cases/TC-000001/versions/0/items").status_code == 422
