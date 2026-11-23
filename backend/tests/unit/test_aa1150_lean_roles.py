"""AA-1150 (lean staff roles step 1) without a database.

- L7a: ``finance`` reads trip cases, prices, supply, operations, supplier bills and the catalog, and its write rights
  are exactly what they were (no payout, tech lead decision Q4).
- The make / approve split: ``supplier.activate``, ``contract.publish`` and ``stop_sale.approve`` are their own
  capabilities. Built-in ``admin`` and ``contracting`` hold all three (the A10e-2 pricing checker and the CIS "Pricing
  team" preset keep working); a role holding every other right of contracting but not these (the future custom maker
  role, played here by a test role) is refused 403 before anything is read, and a holder passes the gate.
The HTTP flow on PostgreSQL is ``tests/integration/test_aa1150_make_approve.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.auth import e2e_staff
from app.auth.staff import PERMISSIONS, PREVIEW_PERMISSIONS, STAFF_ROLES, Staff, current_staff, get_pool
from app.errors import Forbidden
from app.main import create_app
from app.pricing import seasons_admin
from app.settings import Settings
from app.suppliers import service as suppliers_service
from tests.unit.fakes import ScriptedConn

APPROVALS = ("supplier.activate", "contract.publish", "stop_sale.approve")
FINANCE_READS = ("bill.read", "pricing.read", "supply.read", "ops.read", "trip_case.read", "catalog.read")
# Finance's rights before AA-1150 (AA-1035 R2, AA-1037, AA-1074, AA-1091): the reads above are the only additions.
FINANCE_BEFORE = frozenset(
    {
        "billing.read",
        "billing.write",
        "agency_billing.read",
        "agency_billing.write",
        "agency_payment.approve",
        "billing_config.write",
        "profit.read",
        "pricing_policy.read",
        "pricing_policy.write",
        "pricing_policy.approve_finance",
    }
)
MAKER_ROLE = "maker_test"  # never a real role: the test's stand-in for a custom maker role
SUPPLIER = "SUP-000001"
CONTRACT = "c0000000-0000-4000-8000-000000001150"
STOP = "50000000-0000-4000-8000-000000001150"


def _role(role: str) -> Staff:
    return Staff(f"{role}-1", None, role.title(), role, True, "reviewer")


@pytest.fixture
def maker_role(monkeypatch: pytest.MonkeyPatch) -> Staff:
    """A role with every right contracting has except the three approval capabilities."""
    for perm, roles in list(PERMISSIONS.items()):
        if "contracting" in roles and perm not in APPROVALS:
            monkeypatch.setitem(PERMISSIONS, perm, roles | {MAKER_ROLE})
    return _role(MAKER_ROLE)


# ── Matrix ───────────────────────────────────────────────────────────────────


def test_finance_gets_the_six_reads_and_no_new_write() -> None:
    fin = _role("finance")
    assert all(fin.can(p) for p in FINANCE_READS)
    # AA-1158 (bk-064): finance also approves billing entity changes (bank details, default issuer).
    # AA-1158 part 2 (bk-067): finance enters and reads supplier bank accounts (approval stays admin).
    # AA-1151 (bk-068): every role reads the add-on offers (no net shown); finance writes none of them.
    assert set(fin.permissions) == FINANCE_BEFORE | set(FINANCE_READS) | {"billing_config.approve", "addon.read"} | {
        "supplier_bank.read",
        "supplier_bank.write",
    }
    for perm in (
        *APPROVALS,
        "payout.write",
        "bill.write",
        "bill.approve",
        "supply.write",
        "ops.write",
        "supplier_bank.approve",
    ):
        assert not fin.can(perm), perm


def test_the_approval_capabilities_are_admin_and_contracting_only() -> None:
    for perm in APPROVALS:
        assert PERMISSIONS[perm] == frozenset({"admin", "contracting"}), perm
        assert perm not in PREVIEW_PERMISSIONS
    # Each approval sits next to the write that makes the work: holding the approval implies the write today.
    assert PERMISSIONS["supplier.activate"] <= PERMISSIONS["supply.write"]
    assert PERMISSIONS["contract.publish"] <= PERMISSIONS["supply.write"]
    assert PERMISSIONS["stop_sale.approve"] <= PERMISSIONS["stop_sale.write"]
    for role in set(STAFF_ROLES) - {"admin", "contracting"}:
        assert not any(_role(role).can(p) for p in APPROVALS), role


def test_the_pricing_checker_e2e_identity_still_publishes_contracts() -> None:
    """A10e-2 / A10e-3: the pricing checker (booking-contracting) publishes the maker's synthetic contract."""
    checker = e2e_staff.E2E_WRITERS[e2e_staff.E2E_PRICING_CHECKER_EMAIL]
    assert checker.groups == frozenset({"booking-contracting"})
    assert e2e_staff.CONTRACT_STATUS in checker.writes
    assert _role("contracting").can("contract.publish")


# ── Routes: the gate runs before any read ────────────────────────────────────


def _client(staff: Staff) -> tuple[TestClient, ScriptedConn]:
    conn = ScriptedConn()
    app = create_app(Settings(database_url=None, environment="test"))
    app.dependency_overrides[current_staff] = lambda: staff
    app.dependency_overrides[get_pool] = lambda: conn
    return TestClient(app), conn


GATED: tuple[tuple[str, str, Any], ...] = (
    ("POST", f"/booking/suppliers/{SUPPLIER}/activate", None),
    ("PATCH", f"/booking/contracts/{CONTRACT}", {"status": "published"}),
    ("POST", f"/booking/supply/stop-sales/{STOP}/decision", {"action": "approve"}),
    ("POST", f"/booking/supply/stop-sales/{STOP}/decision", {"action": "reject", "reason": "Not closed"}),
    ("POST", f"/booking/supply/stop-sales/{STOP}/end", None),
)


@pytest.mark.parametrize("method,path,body", GATED)
def test_a_maker_without_the_capability_is_403_before_any_read(
    maker_role: Staff, method: str, path: str, body: Any
) -> None:
    c, conn = _client(maker_role)
    response = c.request(method, path, json=body)
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "forbidden"
    assert conn.calls == []


@pytest.mark.parametrize("method,path,body", GATED)
@pytest.mark.parametrize("role", ["admin", "contracting"])
def test_a_holder_passes_the_gate(role: str, method: str, path: str, body: Any) -> None:
    c, _ = _client(_role(role))
    response = c.request(method, path, json=body)
    assert response.status_code == 404, response.text  # the scripted DB has no such record: the gate let it through


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("POST", f"/booking/suppliers/{SUPPLIER}/deactivate", None),
        ("PATCH", f"/booking/contracts/{CONTRACT}", {"status": "expired"}),
        ("POST", f"/booking/supply/stop-sales/{STOP}/decision", {"action": "withdraw", "reason": "Entered twice"}),
        ("POST", f"/booking/supply/stop-sales/{STOP}/end-request", {"reason": "Supplier reopened"}),
        ("DELETE", f"/booking/supply/stop-sales/{STOP}/end-request", None),
    ],
)
def test_the_maker_keeps_its_own_writes(maker_role: Staff, method: str, path: str, body: Any) -> None:
    c, _ = _client(maker_role)
    assert c.request(method, path, json=body).status_code == 404


@pytest.mark.parametrize("method,path,body", GATED)
def test_finance_and_sales_are_refused(method: str, path: str, body: Any) -> None:
    for role in ("finance", "sales"):
        c, conn = _client(_role(role))
        assert c.request(method, path, json=body).status_code == 403, role
        assert conn.calls == []


# ── Season calendar stop-sale ranges ─────────────────────────────────────────


def test_a_stop_sale_range_review_needs_stop_sale_approve(maker_role: Staff) -> None:
    stop_range, season_range = {"kind": "stop_sale"}, {"kind": "season"}
    with pytest.raises(Forbidden) as exc:
        seasons_admin._approve_stop(maker_role, stop_range)
    assert exc.value.code == "forbidden"
    seasons_admin._approve_stop(maker_role, season_range)  # a season range keeps season.approve alone
    for role in ("admin", "contracting"):
        seasons_admin._approve_stop(_role(role), stop_range)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["approve", "reject", "end_approve", "end_reject"])
async def test_the_range_decision_route_refuses_the_maker_on_a_stop_sale_range(maker_role: Staff, action: str) -> None:
    status = "pending" if action in ("approve", "reject") else "approved"
    row = {
        "id": STOP, "calendar_id": CONTRACT, "kind": "stop_sale", "status": status, "created_by": "someone-else",
        "end_requested_by": "someone-else", "end_requested_at": "2026-11-20", "ended_at": None,
    }  # fmt: skip
    conn = ScriptedConn().on("FROM booking.season_ranges r", row).on("FROM booking.season_calendars", {"id": CONTRACT})
    with pytest.raises(Forbidden) as exc:
        await seasons_admin.decide_range(conn, maker_role, STOP, action, "A reason")
    assert exc.value.code == "forbidden"
    assert not conn.sql("UPDATE booking.season_ranges")


# ── Creating a supplier ──────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "status"), [("admin", "pending"), ("contracting", "pending"), (MAKER_ROLE, "pending")]
)
async def test_a_new_supplier_is_active_only_for_a_holder_of_supplier_activate(
    maker_role: Staff, role: str, status: str
) -> None:
    """AA-1158 (R5): every new supplier starts pending, whoever creates it (the create writes the status itself: the
    column default is ``active`` until the part 2 migration)."""
    staff = maker_role if role == MAKER_ROLE else _role(role)
    conn = ScriptedConn().on("INSERT INTO booking.suppliers", {"id": CONTRACT, "code": SUPPLIER})
    out = await suppliers_service.create_supplier(
        conn, staff, {"company_name": "Invented Cove Boats", "categories": ["activity"]}
    )
    assert out == {"id": CONTRACT, "code": SUPPLIER}
    (sql, args), *_ = [(flat, a) for flat, a in conn.calls if "INSERT INTO booking.suppliers" in flat]
    columns = sql.split("(", 1)[1].split(")", 1)[0].split(", ")
    assert dict(zip(columns, args, strict=False))["status"] == status
