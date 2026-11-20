"""AA-1155 T4.1 (root spec R4.3): the approvals that a write capability carried until now, without a database.

- ``pricing.approve`` gates the checker routes of pricing rules, FX rates and cancellation policies (approve, reject),
  agency tier discounts and rate cards (approve) and departure price adjustments (approve, end), which ``pricing.write``
  gated before; ``b2b_desk.approve`` gates approving and ending an agency allotment (``b2b_desk.write`` before);
  ``supplier.activate`` gates activating a hotel or a service (``supply.write`` before).
- Built-in ``admin`` and ``contracting`` hold every one of them. A role that holds every right of contracting except
  the approval class (the later custom maker role, played here by a test role) is refused 403 ``forbidden`` before
  anything is read, at the route and again in the service; it keeps its own writes (make a row, withdraw its own).
The HTTP flow on PostgreSQL is ``tests/integration/test_aa1155_approve_split.py``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.auth import e2e_staff
from app.auth.staff import PERMISSIONS, PREVIEW_PERMISSIONS, STAFF_ROLES, Staff, current_staff, get_pool
from app.b2b import agency_pricing, allotments, cancellation, price_adjustments
from app.errors import Forbidden
from app.main import create_app
from app.pricing import policy
from app.settings import Settings
from app.supply import records
from tests.unit.fakes import ScriptedConn

# The approvals this task splits out, each with the write capability that carried it before.
SPLIT = {"pricing.approve": "pricing.write", "b2b_desk.approve": "b2b_desk.write", "supplier.activate": "supply.write"}
MAKER_ROLE = "maker_test"  # never a real role: the test's stand-in for a custom maker role
ROW = "a0000000-0000-4000-8000-000000001155"
HOTEL, SERVICE = "HTL-000115", "SVC-000115"
TODAY = date(2026, 11, 20)


def _approval_class(perm: str) -> bool:
    """Root spec D4: the approve class a custom role never holds (the Booking part that applies to contracting)."""
    return perm.endswith(".approve") or ".approve_" in perm or perm in ("supplier.activate", "contract.publish")


def _role(role: str) -> Staff:
    return Staff(f"{role}-1", None, role.title(), role, True, "reviewer")


@pytest.fixture
def maker_role(monkeypatch: pytest.MonkeyPatch) -> Staff:
    """A role with every right contracting has except the approval class."""
    for perm, roles in list(PERMISSIONS.items()):
        if "contracting" in roles and not _approval_class(perm):
            monkeypatch.setitem(PERMISSIONS, perm, roles | {MAKER_ROLE})
    return _role(MAKER_ROLE)


# ── Matrix ───────────────────────────────────────────────────────────────────


def test_the_split_approvals_are_admin_and_contracting_only() -> None:
    for perm, write in SPLIT.items():
        assert PERMISSIONS[perm] == frozenset({"admin", "contracting"}), perm
        assert perm not in PREVIEW_PERMISSIONS, perm
        # Each approval sits next to the write that makes the work: holding the approval implies the write today.
        assert PERMISSIONS[perm] <= PERMISSIONS[write], perm
        for role in set(STAFF_ROLES) - {"admin", "contracting"}:
            assert not _role(role).can(perm), (role, perm)


def test_the_maker_role_holds_the_writes_and_no_approval(maker_role: Staff) -> None:
    for perm, write in SPLIT.items():
        assert maker_role.can(write) and not maker_role.can(perm), perm


def test_the_pricing_checker_e2e_identity_keeps_the_approvals() -> None:
    """A10e-2: the pricing checker is booking-contracting, which holds every approval of this split."""
    checker = e2e_staff.E2E_WRITERS[e2e_staff.E2E_PRICING_CHECKER_EMAIL]
    assert checker.groups == frozenset({"booking-contracting"})
    assert all(_role("contracting").can(perm) for perm in SPLIT)


# ── Routes: the gate runs before any read ────────────────────────────────────


def _client(staff: Staff) -> tuple[TestClient, ScriptedConn]:
    conn = ScriptedConn()
    app = create_app(Settings(database_url=None, environment="test"))
    app.dependency_overrides[current_staff] = lambda: staff
    app.dependency_overrides[get_pool] = lambda: conn
    return TestClient(app), conn


REASON = {"reason": "Rate not agreed"}
GATED: tuple[tuple[str, Any], ...] = (
    (f"/booking/pricing/rules/{ROW}/approve", None),
    (f"/booking/pricing/rules/{ROW}/reject", REASON),
    (f"/booking/pricing/fx-rates/{ROW}/approve", None),
    (f"/booking/pricing/fx-rates/{ROW}/reject", REASON),
    (f"/booking/pricing/cancellation-policies/{ROW}/approve", None),
    (f"/booking/pricing/cancellation-policies/{ROW}/reject", REASON),
    (f"/booking/pricing/partner-tier-discounts/{ROW}/approve", None),
    (f"/booking/pricing/partner-rate-cards/{ROW}/approve", None),
    (f"/booking/b2b-desk/price-adjustments/{ROW}/approve", None),
    (f"/booking/b2b-desk/price-adjustments/{ROW}/end", None),
    (f"/booking/catalog/allotments/{ROW}/approve", None),
    (f"/booking/catalog/allotments/{ROW}/end", None),
    (f"/booking/hotels/{HOTEL}/activate", None),
    (f"/booking/services/{SERVICE}/activate", None),
)


@pytest.mark.parametrize("path,body", GATED)
def test_a_maker_without_the_approval_is_403_before_any_read(maker_role: Staff, path: str, body: Any) -> None:
    c, conn = _client(maker_role)
    response = c.post(path, json=body)
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "forbidden"
    assert conn.calls == []


@pytest.mark.parametrize("path,body", GATED)
@pytest.mark.parametrize("role", ["admin", "contracting"])
def test_a_holder_passes_the_gate(role: str, path: str, body: Any) -> None:
    c, _ = _client(_role(role))
    response = c.post(path, json=body)
    assert response.status_code == 404, response.text  # the scripted DB has no such row: the gate let it through


@pytest.mark.parametrize("path,body", GATED)
def test_sales_finance_and_content_are_refused(path: str, body: Any) -> None:
    """Sales held b2b_desk.write (allotment approve and end) before this split; it now proposes only."""
    for role in ("sales", "finance", "content_creator"):
        c, conn = _client(_role(role))
        assert c.post(path, json=body).status_code == 403, role
        assert conn.calls == []


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("POST", f"/booking/pricing/rules/{ROW}/withdraw", None),
        ("POST", f"/booking/pricing/fx-rates/{ROW}/withdraw", None),
        ("POST", f"/booking/pricing/cancellation-policies/{ROW}/withdraw", None),
        ("POST", f"/booking/hotels/{HOTEL}/deactivate", None),
        ("POST", f"/booking/services/{SERVICE}/deactivate", None),
    ],
)
def test_the_maker_keeps_its_own_writes(maker_role: Staff, method: str, path: str, body: Any) -> None:
    c, _ = _client(maker_role)
    assert c.request(method, path, json=body).status_code == 404  # past the gate: the scripted DB has no row


# ── Services: the same check before any read, whatever the caller ────────────

ServiceCall = Callable[[ScriptedConn, Staff], Awaitable[Any]]
SERVICE_CALLS: dict[str, ServiceCall] = {
    "rule_approve": lambda c, s: policy.approve_rule(c, s, ROW, today=TODAY),
    "rule_reject": lambda c, s: policy.close_rule(c, s, ROW, event="rejected", reason="No", today=TODAY),
    "fx_approve": lambda c, s: policy.approve_fx_rate(c, s, ROW, today=TODAY),
    "fx_reject": lambda c, s: policy.close_fx_rate(c, s, ROW, event="rejected", reason="No", today=TODAY),
    "cancellation_approve": lambda c, s: cancellation.approve_policy(c, s, ROW, today=TODAY),
    "cancellation_reject": lambda c, s: cancellation.close_policy(
        c, s, ROW, event="rejected", reason="No", today=TODAY
    ),
    "tier_discount_approve": lambda c, s: agency_pricing.approve_tier_discount(c, s, ROW, today=TODAY),
    "rate_card_approve": lambda c, s: agency_pricing.approve_rate_card(c, s, ROW, today=TODAY),
    "adjustment_approve": lambda c, s: price_adjustments.approve(c, s, ROW),
    "adjustment_end": lambda c, s: price_adjustments.end(c, s, ROW),
    "allotment_approve": lambda c, s: allotments.approve(c, s, ROW, today=TODAY),
    "allotment_end": lambda c, s: allotments.end(c, s, ROW),
    "hotel_activate": lambda c, s: records.set_status(c, records.HOTEL, s, HOTEL, "active"),
    "service_activate": lambda c, s: records.set_status(c, records.SERVICE, s, SERVICE, "active"),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(SERVICE_CALLS))
async def test_the_service_refuses_the_maker_before_any_read(maker_role: Staff, name: str) -> None:
    conn = ScriptedConn()
    with pytest.raises(Forbidden) as exc:
        await SERVICE_CALLS[name](conn, maker_role)
    assert exc.value.code == "forbidden"
    assert conn.calls == []


@pytest.mark.asyncio
async def test_withdrawing_ones_own_row_needs_no_approval(maker_role: Staff) -> None:
    """The maker's withdraw stays pricing.write: it reaches the row (404 here), it is not refused 403."""
    from app.errors import NotFound

    with pytest.raises(NotFound):
        await policy.close_rule(ScriptedConn(), maker_role, ROW, event="withdrawn", reason=None, today=TODAY)


@pytest.mark.asyncio
async def test_deactivating_a_hotel_needs_no_approval(maker_role: Staff) -> None:
    from app.errors import NotFound

    with pytest.raises(NotFound):
        await records.set_status(ScriptedConn(), records.HOTEL, maker_role, HOTEL, "inactive")


# ── Row flags: the UI shows the buttons the API allows ───────────────────────


def _pricing_row(created_by: str) -> dict[str, Any]:
    return {
        "id": ROW, "status": "pending", "created_by": created_by, "created_by_name": "Someone", "created_at": None,
        "approved_by": None, "approved_by_name": None, "approved_at": None, "day_passed": False,
        "closed_by": None, "closed_by_name": None, "closed_at": None, "close_reason": None,
    }  # fmt: skip


def test_pricing_row_flags_follow_the_approval(maker_role: Staff) -> None:
    other = _pricing_row("someone-else")
    assert policy._common(other, maker_role)["can_approve"] is False
    assert policy.closable(other, maker_role)["can_reject"] is False
    assert policy.closable(_pricing_row(maker_role.cis_user_id), maker_role)["can_withdraw"] is True
    for role in ("admin", "contracting"):
        holder = _role(role)
        assert policy._common(other, holder)["can_approve"] is True
        assert policy.closable(other, holder)["can_reject"] is True


def test_price_adjustment_flag_follows_the_approval(maker_role: Staff) -> None:
    row = {
        "id": ROW, "departure_id": ROW, "adjust_pct": None, "amount": None, "currency": None, "reason": "Low season",
        "replaces_id": None, "created_by": "someone-else", "created_by_name": "Someone", "created_at": None,
        "approved_by": None, "approved_by_name": None, "approved_at": None, "ended_by": None, "ended_by_name": None,
        "ended_at": None, "end_reason": None,
    }  # fmt: skip
    assert price_adjustments.adjustment_out(row, maker_role)["can_approve"] is False
    assert price_adjustments.adjustment_out(row, _role("contracting"))["can_approve"] is True
