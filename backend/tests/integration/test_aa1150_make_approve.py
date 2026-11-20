"""AA-1150 (lean staff roles step 1) over HTTP on a real PostgreSQL, as role ``booking``.

The make / approve split: a role that holds the write rights (``supply.write``, ``stop_sale.write``) but not the three
approval capabilities is refused (403 ``forbidden``) when it activates a supplier, publishes a contract, reviews a
stop-sale or approves its end; a holder of the capability does each of them. No built-in role lacks them yet (admin and
contracting keep all three), so the test takes ``contracting`` out of the three capability sets for its duration: the
"maker" is then exactly the later custom maker role.

L7a: a ``finance`` user reads supplier bills, prices, supply, operations, trip cases and the catalog (200) and still
writes none of them (403).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio

from app.auth import staff as staff_auth
from app.db import Database
from app.main import create_app
from app.settings import Settings
from tests.integration.supply import purge_supply
from tests.staff_tokens import LocalCis

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MAKER, OTHER_MAKER, CHECKER, FINANCE = "it-1150-maker", "it-1150-maker-2", "it-1150-checker", "it-1150-finance"
APPROVALS = ("supplier.activate", "contract.publish", "stop_sale.approve")
ENTITIES = ["supplier", "supplier_contract", "supply_restriction", "supplier_rate"]


async def _clean(conn: asyncpg.Connection) -> None:
    await purge_supply(conn)
    await conn.execute("DELETE FROM booking.supplier_contracts")
    await conn.execute("DELETE FROM booking.suppliers")
    await conn.execute("DELETE FROM booking.audit_logs WHERE entity_type = ANY($1::text[])", ENTITIES)


async def _insert(conn: asyncpg.Connection, table: str, **cols: object) -> str:
    names = ", ".join(cols)
    marks = ", ".join(f"${i + 1}" for i in range(len(cols)))
    return await conn.fetchval(
        f"INSERT INTO booking.{table} ({names}) VALUES ({marks}) RETURNING id::text",  # noqa: S608 — test columns
        *cols.values(),
    )


@pytest.fixture
def maker_only_contracting(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """``contracting`` keeps every right but the three approval capabilities (the future custom maker role)."""
    for perm in APPROVALS:
        monkeypatch.setitem(staff_auth.PERMISSIONS, perm, staff_auth.PERMISSIONS[perm] - {"contracting"})
    yield


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    await _clean(conn)
    for staff_id, role in (
        (MAKER, "contracting"),
        (OTHER_MAKER, "contracting"),
        (CHECKER, "admin"),
        (FINANCE, "finance"),
    ):
        await conn.execute(
            "INSERT INTO booking.staff_users (cis_user_id, role, active, name) VALUES ($1, $2, true, $1)"
            " ON CONFLICT (cis_user_id) DO UPDATE SET role = EXCLUDED.role, active = true",
            staff_id,
            role,
        )
    try:
        yield conn
    finally:
        await _clean(conn)
        await conn.close()


@pytest_asyncio.fixture
async def client(
    admin: asyncpg.Connection, migrated_booking_dsn: str
) -> AsyncIterator[tuple[httpx.AsyncClient, LocalCis]]:
    settings = Settings(database_url=migrated_booking_dsn, db_ssl="disable", environment="test", git_sha="ci")
    db, cis = Database(settings), LocalCis()
    app = create_app(settings, db, cis.verifier())
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://booking/booking") as c:
            yield c, cis
    finally:
        await db.close()


def _as(cis: LocalCis, sub: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {cis.token(sub=sub, cis_role='reviewer')}"}


async def _ok(response: httpx.Response, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json() if response.content else None


def _refused(response: httpx.Response) -> None:
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "forbidden", response.text


async def test_a_maker_without_the_approval_capabilities_is_refused_and_a_holder_approves(
    client: tuple[httpx.AsyncClient, LocalCis], admin: asyncpg.Connection, maker_only_contracting: None
) -> None:
    c, cis = client
    maker, other, checker = _as(cis, MAKER), _as(cis, OTHER_MAKER), _as(cis, CHECKER)

    # Creating: a holder's new supplier is active at once; the maker's starts inactive (no way around the approval).
    held = await _ok(
        await c.post("/suppliers", json={"company_name": "Invented Lagoon Kayaks", "categories": ["activity"]},
                     headers=checker),
        201,
    )  # fmt: skip
    assert held["status"] == "active"
    sup = await _ok(
        await c.post("/suppliers", json={"company_name": "Invented Reef Boats", "categories": ["activity"]},
                     headers=maker),
        201,
    )  # fmt: skip
    assert sup["status"] == "inactive"
    assert await admin.fetchval("SELECT status FROM booking.suppliers WHERE id = $1::uuid", sup["id"]) == "inactive"

    # Supplier activation: the maker (supply.write, no supplier.activate) is 403, and so is another maker.
    _refused(await c.post(f"/suppliers/{sup['id']}/activate", headers=maker))
    _refused(await c.post(f"/suppliers/{sup['code']}/activate", headers=other))
    assert await admin.fetchval("SELECT status FROM booking.suppliers WHERE id = $1::uuid", sup["id"]) == "inactive"
    on = await _ok(await c.post(f"/suppliers/{sup['id']}/activate", headers=checker))
    assert on["status"] == "active"

    # Contract publish: the other maker (not the draft's maker) still lacks contract.publish; the checker publishes.
    service = await _insert(
        admin, "services", supplier_id=sup["id"], service_type="activity", unit="person", title="Reef snorkel trip",
        created_by=MAKER,
    )  # fmt: skip
    body = {"title": "2027 trips", "valid_from": "2027-01-01", "valid_to": "2027-12-31", "currency": "USD"}
    k = await _ok(await c.post(f"/suppliers/{sup['id']}/contracts", json=body, headers=maker), 201)
    await _ok(await c.post(f"/contracts/{k['id']}/rates", json={"service_id": service, "net_amount": "25.00"},
                           headers=maker), 201)  # fmt: skip
    _refused(await c.patch(f"/contracts/{k['id']}", json={"status": "published"}, headers=other))
    still = await _ok(await c.get(f"/contracts/{k['id']}", headers=other))
    assert still["status"] == "draft"
    published = await _ok(await c.patch(f"/contracts/{k['id']}", json={"status": "published"}, headers=checker))
    assert published["status"] == "published" and published["published_by"] == CHECKER

    # Stop-sale: the maker sets one (stop_sale.write) and may withdraw its own; reviewing and ending need
    # stop_sale.approve.
    k2 = k["id"]
    stop = {"component_id": f"sv:{service}", "date_from": "2027-06-01", "date_to": "2027-06-02",
            "note": "Supplier e-mail: boat in repair"}  # fmt: skip
    first = await _ok(await c.post(f"/contracts/{k2}/stop-sales", json=stop, headers=maker), 201)
    withdraw = {"action": "withdraw", "reason": "Entered twice"}
    withdrawn = await _ok(await c.post(f"/supply/stop-sales/{first['id']}/decision", json=withdraw, headers=maker))
    assert withdrawn["status"] == "withdrawn"
    ss = await _ok(await c.post(f"/contracts/{k2}/stop-sales", json=stop, headers=maker), 201)
    for action in ("approve", "reject"):
        _refused(await c.post(f"/supply/stop-sales/{ss['id']}/decision", json={"action": action, "reason": "x"},
                              headers=other))  # fmt: skip
    approved = await _ok(await c.post(f"/supply/stop-sales/{ss['id']}/decision", json={"action": "approve"},
                                      headers=checker))  # fmt: skip
    assert approved["status"] == "approved"
    await _ok(await c.post(f"/supply/stop-sales/{ss['id']}/end-request", json={"reason": "Boat repaired"},
                           headers=maker))  # fmt: skip
    _refused(await c.post(f"/supply/stop-sales/{ss['id']}/end", headers=other))
    ended = await _ok(await c.post(f"/supply/stop-sales/{ss['id']}/end", headers=checker))
    assert ended["in_effect"] is False and ended["ended_by"] == CHECKER
    # Expiring a published contract stays a supply.write action (no approval capability).
    await _ok(await c.patch(f"/contracts/{k2}", json={"status": "expired"}, headers=other))

    # Every refusal wrote nothing: one audit row per successful write only.
    refused_actions = await admin.fetchval(
        "SELECT count(*) FROM booking.audit_logs WHERE entity_type = ANY($1::text[]) AND actor_ref = $2",
        ENTITIES,
        OTHER_MAKER,
    )
    assert refused_actions == 1  # the expire above


async def test_finance_reads_bills_prices_supply_ops_cases_and_catalog_but_writes_none(
    client: tuple[httpx.AsyncClient, LocalCis], admin: asyncpg.Connection
) -> None:
    c, cis = client
    fin, checker = _as(cis, FINANCE), _as(cis, CHECKER)
    sup = await _ok(
        await c.post("/suppliers", json={"company_name": "Invented Hill Lodge", "categories": ["hotel"]},
                     headers=checker),
        201,
    )  # fmt: skip
    for path in (
        "/supplier-bills",
        "/supplier-bills/summary",
        "/pricing/rules",
        "/pricing/fx-rates",
        "/suppliers",
        f"/suppliers/{sup['id']}",
        f"/suppliers/{sup['id']}/contracts",
        "/supplier-bookings",
        "/trip-checks",
        "/trip-cases",
        "/trip-cases/summary",
        "/catalog/geo/countries",
    ):
        response = await c.get(path, headers=fin)
        assert response.status_code == 200, (path, response.text)
    for method, path, body in (
        ("POST", "/suppliers", {"company_name": "Invented Other", "categories": ["hotel"]}),
        ("POST", f"/suppliers/{sup['id']}/deactivate", {"reason": "no"}),
        ("POST", f"/suppliers/{sup['id']}/activate", None),
        ("POST", f"/suppliers/{sup['id']}/contracts", {"title": "x", "valid_from": "2027-01-01", "currency": "USD"}),
    ):
        response = await c.request(method, path, json=body, headers=fin)
        assert response.status_code == 403, (method, path, response.text)
