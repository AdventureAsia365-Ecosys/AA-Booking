"""AA-822 suppliers: route permissions and body validation (service stubbed), normalisation, and the catalog
place check failing closed on writes and soft on reads."""

from __future__ import annotations

from typing import Any

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app import catalog
from app.auth.staff import Staff, current_staff, get_pool
from app.errors import Invalid, Unavailable
from app.main import create_app
from app.settings import Settings
from app.suppliers import service
from tests.unit.fakes import ScriptedConn

ADMIN = Staff("a-1", None, "Admin", "admin", True, "admin")
SALES = Staff("u-1", None, "Sales One", "sales", True, "reviewer")
CONTRACTING = Staff("u-5", None, "Contracts", "contracting", True, "reviewer")
CONTENT = Staff("u-9", None, "Writer", "content_creator", True, "content")
SUPPLIER_ID = "11111111-1111-4111-8111-111111111111"


class Recorder:
    def __init__(self, result: Any = None) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.result = {"id": SUPPLIER_ID, "code": "SUP-000001"} if result is None else result

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args[1:], kwargs))  # drop the connection
        return self.result


def client(staff: Staff) -> TestClient:
    app = create_app(Settings(database_url=None, environment="test"))
    app.dependency_overrides[current_staff] = lambda: staff
    app.dependency_overrides[get_pool] = lambda: ScriptedConn()
    return TestClient(app)


@pytest.fixture
def stubs(monkeypatch: pytest.MonkeyPatch) -> dict[str, Recorder]:
    recs: dict[str, Recorder] = {}
    for name in (
        "list_suppliers",
        "summary",
        "create_supplier",
        "get_supplier",
        "update_supplier",
        "set_status",
        "list_contracts",
        "create_contract",
        "get_contract",
        "update_contract",
    ):
        recs[name] = Recorder()
        monkeypatch.setattr(service, name, recs[name])
    return recs


WRITES = [
    ("post", "/booking/suppliers", {"company_name": "A", "categories": ["hotel"]}),
    ("patch", f"/booking/suppliers/{SUPPLIER_ID}", {"contact_name": "B"}),
    ("post", f"/booking/suppliers/{SUPPLIER_ID}/deactivate", None),
    ("post", f"/booking/suppliers/{SUPPLIER_ID}/activate", None),
    (
        "post",
        f"/booking/suppliers/{SUPPLIER_ID}/contracts",
        {"title": "T", "valid_from": "2027-01-01", "currency": "VND"},
    ),
    ("patch", f"/booking/contracts/{SUPPLIER_ID}", {"status": "published"}),
]


@pytest.mark.parametrize(("method", "path", "body"), WRITES)
def test_writes_need_supply_write(stubs: dict[str, Recorder], method: str, path: str, body: Any) -> None:
    for staff in (SALES, CONTENT):
        assert client(staff).request(method, path, json=body).status_code == 403
    for staff in (CONTRACTING, ADMIN):
        assert client(staff).request(method, path, json=body).status_code in (200, 201)


@pytest.mark.parametrize(
    "path",
    [
        "/booking/suppliers",
        "/booking/suppliers/summary",
        f"/booking/suppliers/{SUPPLIER_ID}",
        f"/booking/suppliers/{SUPPLIER_ID}/contracts",
        f"/booking/contracts/{SUPPLIER_ID}",
    ],
)
def test_reads_are_open_to_sales_not_content(stubs: dict[str, Recorder], path: str) -> None:
    assert client(SALES).get(path).status_code == 200
    assert client(CONTRACTING).get(path).status_code == 200
    assert client(CONTENT).get(path).status_code == 403


def test_summary_is_not_read_as_a_supplier_ref(stubs: dict[str, Recorder]) -> None:
    client(SALES).get("/booking/suppliers/summary")
    assert len(stubs["summary"].calls) == 1 and not stubs["get_supplier"].calls


@pytest.mark.parametrize(
    "body",
    [
        {"company_name": "", "categories": ["hotel"]},
        {"company_name": "A", "categories": []},
        {"company_name": "A", "categories": ["spa"]},
        {"company_name": "A", "categories": ["hotel"], "website": "ftp://x"},
        {"company_name": "A", "categories": ["hotel"], "contact_email": "nope"},
        {"company_name": "A", "categories": ["hotel"], "destination_id": "not-a-uuid"},
        {"company_name": "A", "categories": ["hotel"], "country_code": "VNM"},
        {"company_name": "A", "categories": ["hotel"], "status": "inactive"},  # status has its own routes
    ],
)
def test_supplier_body_validation(stubs: dict[str, Recorder], body: dict[str, Any]) -> None:
    assert client(CONTRACTING).post("/booking/suppliers", json=body).status_code == 422
    assert not stubs["create_supplier"].calls


def test_patch_passes_only_sent_fields(stubs: dict[str, Recorder]) -> None:
    r = client(CONTRACTING).patch(f"/booking/suppliers/{SUPPLIER_ID}", json={"website": None, "region": "North"})
    assert r.status_code == 200
    (_, ref, data), _ = stubs["update_supplier"].calls[0]
    assert ref == SUPPLIER_ID and data == {"website": None, "region": "North"}


def test_contract_body_validation(stubs: dict[str, Recorder]) -> None:
    c = client(CONTRACTING)
    path = f"/booking/suppliers/{SUPPLIER_ID}/contracts"
    base = {"title": "T", "valid_from": "2027-01-01", "currency": "VND"}
    assert c.post(path, json={**base, "valid_to": "2026-12-31"}).status_code == 422
    assert c.post(path, json={**base, "currency": "VN"}).status_code == 422
    assert c.post(path, json={"title": "T", "currency": "VND"}).status_code == 422
    assert c.patch(f"/booking/contracts/{SUPPLIER_ID}", json={"status": "draft"}).status_code == 422
    assert c.post(path, json=base).status_code == 201


def test_normalize_trims_orders_and_lowers() -> None:
    out = service.normalize(
        {
            "company_name": "  Blue Lotus ",
            "categories": ["meal", "hotel", "meal"],
            "contact_email": "A@B.Example",
            "website": "  ",
            "country_code": " vn ",
        }
    )
    assert out == {
        "company_name": "Blue Lotus",
        "categories": ["hotel", "meal"],
        "contact_email": "a@b.example",
        "website": None,
        "country_code": "VN",
    }
    with pytest.raises(Invalid):
        service.normalize({"company_name": "   "})


@pytest.mark.asyncio
async def test_catalog_fails_closed_on_writes_and_soft_on_reads() -> None:
    conn = ScriptedConn([("catalog.v_destination", asyncpg.UndefinedTableError("missing"))])
    with pytest.raises(Unavailable):
        await catalog.require_destination(conn, SUPPLIER_ID)
    assert await catalog.destinations_by_id(conn, [SUPPLIER_ID]) == {}
    with pytest.raises(Unavailable):
        await catalog.search_destinations(conn, q="han")


@pytest.mark.asyncio
async def test_unknown_place_is_422() -> None:
    conn = ScriptedConn([("catalog.v_destination", None)])
    with pytest.raises(Invalid) as exc:
        await catalog.require_destination(conn, SUPPLIER_ID)
    assert exc.value.code == "unknown_destination"
