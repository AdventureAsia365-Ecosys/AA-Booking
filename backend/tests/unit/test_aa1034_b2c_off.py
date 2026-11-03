"""AA-1034 R0 (root ADR 0019 d8): with B2C_INTAKE_ENABLED off (the default) no staff route creates a traveller record.

Every route that would insert into ``booking.customers`` answers 410 ``b2b_only`` before the service is called; reading,
editing and archiving customers and opening a case for an existing customer keep working.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import b2c
from app.auth.staff import Staff, current_staff, get_pool
from app.catalog import trip_plans
from app.customers import service as customers
from app.main import create_app
from app.settings import Settings, load_settings
from app.trip_cases import service
from tests.unit.fakes import ScriptedConn

SALES = Staff("u-1", None, "Sales One", "sales", True, "reviewer")
CUSTOMER_ID = "55555555-5555-4555-8555-555555555555"
LEAD = {"name": "Ira Example", "phone": "+84 100 200"}


class Recorder:
    def __init__(self, result: Any) -> None:
        self.calls: list[Any] = []
        self.result = result

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(args[1:])
        return self.result


def client(**settings: Any) -> TestClient:
    app = create_app(Settings(database_url=None, environment="test", **settings))
    app.dependency_overrides[current_staff] = lambda: SALES
    app.dependency_overrides[get_pool] = lambda: ScriptedConn()
    return TestClient(app)


@pytest.fixture
def stubs(monkeypatch: pytest.MonkeyPatch) -> dict[str, Recorder]:
    recs = {
        "create_lead": Recorder({"id": CUSTOMER_ID}),
        "get_customer": Recorder({"id": CUSTOMER_ID}),
        "update_customer": Recorder({"changed": []}),
        "create_case": Recorder({"code": "TC-000001"}),
        "use_for_trip_case": Recorder({"trip_case": {"code": "TC-000002"}}),
    }
    monkeypatch.setattr(customers, "create_lead", recs["create_lead"])
    monkeypatch.setattr(customers, "get_customer", recs["get_customer"])
    monkeypatch.setattr(customers, "update_customer", recs["update_customer"])
    monkeypatch.setattr(service, "create_case", recs["create_case"])
    monkeypatch.setattr(trip_plans, "use_for_trip_case", recs["use_for_trip_case"])
    return recs


def test_the_flag_is_off_by_default() -> None:
    assert Settings(database_url=None).b2c_intake_enabled is False
    assert load_settings({}).b2c_intake_enabled is False
    assert load_settings({"B2C_INTAKE_ENABLED": "true"}).b2c_intake_enabled is True
    assert b2c.INTAKE_KINDS == {"bk_inquiry_pull", "bk_inquiry_process"}


def test_new_lead_answers_410_b2b_only(stubs: dict[str, Recorder]) -> None:
    r = client().post("/booking/customers", json=LEAD)
    assert r.status_code == 410
    assert r.json()["detail"] == "b2b_only" and "partner agencies" in r.json()["message"]
    assert stubs["create_lead"].calls == []


def test_case_with_a_new_customer_answers_410(stubs: dict[str, Recorder]) -> None:
    r = client().post(
        "/booking/trip-cases", json={"new_customer": {"name": "Ira Example", "email": "ira@example.test"}}
    )
    assert r.status_code == 410 and r.json()["detail"] == "b2b_only"
    assert stubs["create_case"].calls == []


def test_trip_plan_use_with_a_new_customer_answers_410(stubs: dict[str, Recorder]) -> None:
    r = client().post(
        "/booking/catalog/trip-plans/66666666-6666-4666-8666-666666666666/trip-cases",
        json={"new_customer": {"name": "Ira Example", "phone": "+841"}},
    )
    assert r.status_code == 410 and r.json()["detail"] == "b2b_only"
    assert stubs["use_for_trip_case"].calls == []


def test_existing_customer_paths_keep_working(stubs: dict[str, Recorder]) -> None:
    c = client()
    assert c.post("/booking/trip-cases", json={"customer_id": CUSTOMER_ID}).status_code == 201
    assert c.patch("/booking/customers/CUS-000001", json={"name": "Redacted"}).status_code == 200
    assert stubs["create_case"].calls and stubs["update_customer"].calls


def test_the_flag_on_restores_the_intake(stubs: dict[str, Recorder]) -> None:
    assert client(b2c_intake_enabled=True).post("/booking/customers", json=LEAD).status_code == 201
    assert stubs["create_lead"].calls
