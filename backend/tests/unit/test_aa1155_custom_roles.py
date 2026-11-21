"""AA-1155 T5 (root spec R4.1-R4.3, design D2-D4, root ADR 0006a A14 / A10g): the booking-custom resolver, without a
database.

- A WORKFORCE token with ``booking-custom`` and no built-in group gets the custom role the CIS view names; ``/staff/me``
  shows the code, its label and exactly the role's capabilities; the staff row is synced to the code.
- A missing grant, a code that is not custom, an unknown or archived role: 403 ``no_booking_role``. The view or the
  role table unreadable: 403 ``custom_role_unavailable``, while a built-in administrator never reads either.
- One read per ``CACHE_S`` (60 s) per process; capabilities the code does not know or in the approve class are dropped.
- A custom holder is 403 ``forbidden`` on every approve-class write (every route gated by one, and the approvals a
  service checks), before any record is read.
- The A10g maker (Dev only, exact groups): its approve probes are answered 403 ``forbidden`` from its capabilities
  before any record is read; its other writes outside the allow-list are 403 ``e2e_read_only``.
The HTTP flow on PostgreSQL (the view, the tables, a revoked grant): ``tests/integration/test_aa1155_custom_roles.py``.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from collections.abc import Iterator
from typing import Any

import asyncpg
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import app.routers as routers_pkg
from app import db
from app.auth import custom_roles, e2e_staff
from app.auth.staff import PERMISSIONS, Staff, current_staff, get_pool
from app.main import create_app
from app.settings import Settings
from tests.staff_tokens import LocalCis
from tests.unit.fakes import ScriptedConn
from tests.unit.test_aa911_preview_cap import IDENTITY_SELECT, auth, client, cognito_row

GRANT = "FROM shared.v_staff_booking_grant"
ROLE = "FROM booking.staff_role r LEFT JOIN"
CODE = custom_roles.SEED_CODE
ROW = "a0000000-0000-4000-8000-000000001155"
MAKER = e2e_staff.E2E_CUSTOM_MAKER_EMAIL


@pytest.fixture(autouse=True)
def fresh_cache() -> Iterator[None]:
    custom_roles.forget()
    yield
    custom_roles.forget()


def grant_row(code: str | None = CODE, *, owner: bool = False) -> dict[str, Any]:
    return {"booking_role_code": code, "is_owner": owner, "is_business_owner": False}


def role_row(
    code: str = CODE, caps: Any = None, *, archived: bool = False, label: str = custom_roles.SEED_LABEL
) -> dict[str, Any]:
    capabilities = sorted(custom_roles.SEED_CAPABILITIES if caps is None else caps)
    return {"code": code, "label": label, "based_on": "contracting", "archived": archived, "capabilities": capabilities}


def custom_conn(grant: Any = None, role: Any = None, staff_role: str = CODE) -> ScriptedConn:
    return (
        ScriptedConn()
        .on(IDENTITY_SELECT, cognito_row(staff_role))
        .on(GRANT, grant_row() if grant is None else grant)
        .on(ROLE, role_row() if role is None else role)
    )


def custom_token(cis: LocalCis, *, email: str = "thu@adventure.asia", groups: Any = None) -> str:
    return cis.workforce.token(["cis-custom", "booking-custom"] if groups is None else groups, email=email)


def _me(conn: ScriptedConn, cis: LocalCis | None = None, token: str | None = None) -> Any:
    cis = cis or LocalCis()
    with client(cis, conn) as c:
        return c.get("/booking/staff/me", headers=auth(token or custom_token(cis)))


# ── Resolution (R4.1) ────────────────────────────────────────────────────────


def test_a_custom_holder_gets_exactly_its_role_capabilities() -> None:
    r = _me(custom_conn())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["role"] == CODE and body["role_label"] == "Product, ops & accounting"
    assert set(body["permissions"]) == custom_roles.SEED_CAPABILITIES
    assert not any(custom_roles.is_approve_class(p) for p in body["permissions"])


def test_the_grant_is_read_by_the_token_sub() -> None:
    conn = custom_conn()
    cis = LocalCis()
    sub = "5b0c0000-0000-4000-8000-0000000001aa"
    assert _me(conn, cis, cis.workforce.token(["booking-custom"], sub=sub)).status_code == 200
    assert conn.sql(GRANT) == [(sub,)]
    assert conn.sql(ROLE) == [(CODE,)]


def test_the_staff_row_is_synced_to_the_custom_code_and_audited() -> None:
    conn = custom_conn(staff_role="contracting")
    conn.on("FOR UPDATE", cognito_row("contracting")).on(
        "UPDATE booking.staff_users", lambda *a: {**cognito_row(CODE), "role": a[1]}
    )
    assert _me(conn).status_code == 200
    (update,) = conn.sql("UPDATE booking.staff_users")
    assert update[1] == CODE
    (audit_args,) = conn.sql("INSERT INTO booking.audit_logs")
    assert audit_args[3] == "staff.role_synced" and "custom role grant" in audit_args[9]


def test_a_built_in_group_wins_and_never_reads_the_view() -> None:
    conn = custom_conn(staff_role="sales")
    cis = LocalCis()
    r = _me(conn, cis, cis.workforce.token(["booking-sales", "booking-custom"]))
    assert r.status_code == 200 and r.json()["role"] == "sales" and r.json()["role_label"] is None
    assert conn.sql(GRANT) == [] and conn.sql(ROLE) == []


def test_no_booking_group_at_all_is_refused_before_any_read() -> None:
    conn = custom_conn()
    cis = LocalCis()
    r = _me(conn, cis, cis.workforce.token(["cis-custom"]))
    assert r.status_code == 403 and r.json()["detail"] == "no_booking_role"
    assert conn.calls == []


# ── Fail closed for custom holders only (R4.2) ───────────────────────────────


@pytest.mark.parametrize(
    "grant, role",
    [
        (None, role_row()),  # no view row
        (grant_row(None, owner=True), role_row()),  # an owner marker without a Booking grant
        (grant_row("sales"), role_row()),  # a built-in code: the groups decide built-in roles, never the view
        (grant_row("Custom_Bad"), role_row()),
        (grant_row(), None),  # unknown role
        (grant_row(), role_row(archived=True)),
    ],
)
def test_no_usable_custom_role_is_no_booking_role(grant: Any, role: Any) -> None:
    conn = ScriptedConn().on(IDENTITY_SELECT, cognito_row(CODE)).on(GRANT, grant).on(ROLE, role)
    r = _me(conn)
    assert r.status_code == 403 and r.json()["detail"] == "no_booking_role"
    assert conn.sql(IDENTITY_SELECT) == []  # refused before the staff row is read or written


@pytest.mark.parametrize("fragment", [GRANT, ROLE])
def test_an_unreadable_view_or_table_refuses_the_custom_holder(fragment: str) -> None:
    conn = custom_conn().on(fragment, asyncpg.InsufficientPrivilegeError("permission denied"))
    r = _me(conn)
    assert r.status_code == 403 and r.json()["detail"] == "custom_role_unavailable"


def test_an_unreadable_view_never_touches_a_built_in_administrator() -> None:
    conn = (
        ScriptedConn()
        .on(IDENTITY_SELECT, cognito_row("admin"))
        .on(GRANT, asyncpg.InsufficientPrivilegeError("permission denied"))
        .on(ROLE, asyncpg.UndefinedTableError("missing"))
    )
    cis = LocalCis()
    r = _me(conn, cis, cis.workforce.token(["booking-admin"], amr=["pwd", "otp", "mfa"]))
    assert r.status_code == 200 and r.json()["role"] == "admin" and "staff.admin" in r.json()["permissions"]
    assert conn.sql(GRANT) == [] and conn.sql(ROLE) == []


def test_an_error_is_not_cached() -> None:
    conn = custom_conn().on(GRANT, asyncpg.InsufficientPrivilegeError("permission denied"))
    assert _me(conn).status_code == 403
    conn.on(GRANT, grant_row())
    assert _me(conn).status_code == 200


# ── Cache (R4.1: at most 60 s) ───────────────────────────────────────────────


def test_the_cache_bound_is_the_spec_bound() -> None:
    assert custom_roles.CACHE_S == 60.0


def test_one_read_per_bound_then_read_again(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(custom_roles.time, "monotonic", lambda: clock[0])
    conn = custom_conn()
    cis = LocalCis()
    assert _me(conn, cis).status_code == 200
    clock[0] += custom_roles.CACHE_S - 1
    assert _me(conn, cis).status_code == 200
    assert len(conn.sql(GRANT)) == 1 and len(conn.sql(ROLE)) == 1
    # Archived meanwhile: refused once the bound has passed.
    conn.on(ROLE, role_row(archived=True))
    clock[0] += 2
    r = _me(conn, cis)
    assert r.status_code == 403 and r.json()["detail"] == "no_booking_role"
    assert len(conn.sql(GRANT)) == 2 and len(conn.sql(ROLE)) == 2


def test_forget_drops_the_cache() -> None:
    conn = custom_conn()
    assert _me(conn).status_code == 200
    custom_roles.forget()
    assert _me(conn).status_code == 200
    assert len(conn.sql(GRANT)) == 2


# ── Capabilities are narrowed (design D4) ────────────────────────────────────


def test_unknown_and_approve_class_capabilities_are_dropped(caplog: pytest.LogCaptureFixture) -> None:
    caps = {"supply.read", "pricing.approve", "supplier.activate", "made.up", "payout.write"}
    with caplog.at_level(logging.WARNING, logger="app.auth.custom_roles"):
        r = _me(custom_conn(role=role_row(caps=caps)))
    assert r.status_code == 200 and r.json()["permissions"] == ["supply.read"]
    assert any("capability_dropped" in rec.getMessage() for rec in caplog.records)


@pytest.mark.parametrize(
    "capability",
    [
        "pricing.approve",
        "b2b_desk.approve",
        "pricing_policy.approve_finance",
        "supplier.activate",
        "contract.publish",
        "stop_sale.approve",
        "payout.write",
        "payment.verify",
        "billing_config.write",
        "staff.admin",
    ],
)
def test_the_approve_class(capability: str) -> None:
    assert custom_roles.is_approve_class(capability)
    assert capability not in custom_roles.assignable()


@pytest.mark.parametrize("capability", ["pricing.write", "supply.write", "stop_sale.write", "bill.write", "approve.x"])
def test_not_the_approve_class(capability: str) -> None:
    assert not custom_roles.is_approve_class(capability)


def test_the_seed_holds_no_approval_and_only_known_capabilities() -> None:
    assert custom_roles.SEED_CAPABILITIES <= custom_roles.assignable()
    assert custom_roles.SEED_CAPABILITIES <= set(PERMISSIONS)


# ── Approve-class writes: 403 before any read (R4.3) ─────────────────────────


def _routes() -> list[APIRoute]:
    found: list[APIRoute] = []
    for info in pkgutil.iter_modules(routers_pkg.__path__):
        router = getattr(importlib.import_module(f"app.routers.{info.name}"), "router", None)
        found.extend(r for r in getattr(router, "routes", ()) if isinstance(r, APIRoute))
    return found


def _route_gated_approvals() -> list[tuple[str, str, frozenset[str]]]:
    rows = []
    for route in _routes():
        needs = frozenset(p for p in e2e_staff.route_permissions(route) if custom_roles.is_approve_class(p))
        rows.extend((m, route.path, needs) for m in sorted(route.methods - {"GET", "HEAD"}) if needs)
    return sorted(rows)


def test_every_route_gate_is_a_known_permission_and_the_list_is_not_empty() -> None:
    rows = _route_gated_approvals()
    paths = {path for _, path, _ in rows}
    # The approvals the spec names (R4.3) are all route-gated or service probes.
    for expected in (
        "/booking/pricing/rules/{rule_id}/approve",
        "/booking/pricing/fx-rates/{fx_id}/approve",
        "/booking/pricing/cancellation-policies/{policy_id}/approve",
        "/booking/pricing/partner-tier-discounts/{row_id}/approve",
        "/booking/pricing/partner-rate-cards/{row_id}/approve",
        "/booking/b2b-desk/price-adjustments/{adjustment_id}/approve",
        "/booking/catalog/allotments/{allotment_id}/approve",
        "/booking/supply/stop-sales/{restriction_id}/end",
        "/booking/supplier-bills/{bill}/payouts",
        "/booking/billing-entities",
    ):
        assert expected in paths, expected
    for _, _, needs in rows:
        assert needs <= set(PERMISSIONS)


def _custom_staff() -> Staff:
    return Staff(
        "cog:custom-1",
        None,
        "maker",
        CODE,
        True,
        "content_lead",
        cognito_sub="5b0c0000-0000-4000-8000-0000000001aa",
        custom_permissions=custom_roles.SEED_CAPABILITIES,
        role_label=custom_roles.SEED_LABEL,
    )


def _app_client(staff: Staff) -> tuple[TestClient, ScriptedConn]:
    conn = ScriptedConn()
    app = create_app(Settings(database_url=None, environment="test"))
    app.dependency_overrides[current_staff] = lambda: staff
    app.dependency_overrides[get_pool] = lambda: conn
    return TestClient(app), conn


PATH_VALUES = {"decision": "approve", "code": "PRICE_ERRORS"}


def _fill(path: str) -> str:
    """A concrete path for a template: every parameter takes an invented id (UUID, code or a decision word)."""
    out = []
    for part in path.split("/"):
        if part.startswith("{"):
            name = part.strip("{}")
            out.append(
                {"decision": "approve", "code": "PRICE_ERRORS", "tenant_id": ROW, "source_id": ROW}.get(name, ROW)
            )
        else:
            out.append(part)
    return "/".join(out)


@pytest.mark.parametrize("method, path, needs", _route_gated_approvals())
def test_a_custom_holder_is_refused_every_route_gated_approval_before_any_read(
    method: str, path: str, needs: frozenset[str]
) -> None:
    c, conn = _app_client(_custom_staff())
    response = c.request(method, _fill(path), json={})
    assert response.status_code == 403, (path, response.text)
    assert response.json()["detail"] == "forbidden"
    assert conn.calls == []


SERVICE_GATED = [
    ("POST", f"/booking/suppliers/SUP-{'0' * 5}115/activate", None),
    ("POST", f"/booking/hotels/HTL-{'0' * 5}115/activate", None),
    ("POST", f"/booking/services/SVC-{'0' * 5}115/activate", None),
    ("PATCH", f"/booking/contracts/{ROW}", {"status": "published"}),
    ("POST", f"/booking/supply/stop-sales/{ROW}/decision", {"action": "approve"}),
    ("POST", f"/booking/supply/stop-sales/{ROW}/decision", {"action": "reject", "reason": "Not agreed"}),
]


@pytest.mark.parametrize("method, path, body", SERVICE_GATED)
def test_a_custom_holder_is_refused_the_approvals_a_service_checks(method: str, path: str, body: Any) -> None:
    staff = _custom_staff()
    assert staff.can("supply.write") and staff.can("stop_sale.write")  # the route gate passes ...
    c, conn = _app_client(staff)
    response = c.request(method, path, json=body)
    assert response.status_code == 403, (path, response.text)  # ... and the service refuses the approval
    assert response.json()["detail"] == "forbidden"
    assert conn.sql("UPDATE") == [] and conn.sql("INSERT") == []


def test_service_probes_name_the_capability_the_service_checks() -> None:
    assert {key: cap for key, (cap, _) in e2e_staff.SERVICE_PROBES.items()} == {
        e2e_staff.SUPPLIER_ACTIVATE: "supplier.activate",
        e2e_staff.HOTEL_ACTIVATE: "supplier.activate",
        e2e_staff.SERVICE_ACTIVATE: "supplier.activate",
        e2e_staff.CONTRACT_STATUS: "contract.publish",
        e2e_staff.STOP_SALE_DECISION: "stop_sale.approve",
    }
    real = {(m, r.path) for r in _routes() for m in r.methods}
    assert set(e2e_staff.SERVICE_PROBES) <= real


def test_a_custom_holder_reads_what_the_live_check_opens() -> None:
    staff = _custom_staff()
    for permission in ("supply.read", "bill.read", "agency_billing.read"):  # /suppliers, /billing, /agency-billing
        assert staff.can(permission), permission


def test_readiness_waits_for_bk_062() -> None:
    assert "bk-062" in db.REQUIRED_SCHEMA_VERSIONS


# ── A10g: the custom-role maker (Dev only) ───────────────────────────────────


@pytest.fixture
def dev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "dev")


def _maker(method: str, path: str, body: Any = None, *, groups: Any = None) -> tuple[Any, ScriptedConn]:
    cis = LocalCis()
    conn = custom_conn()
    with client(cis, conn) as c:
        r = c.request(method, path, json=body, headers=auth(custom_token(cis, email=MAKER, groups=groups)))
    return r, conn


PROBES = [
    ("POST", f"/booking/pricing/rules/{ROW}/approve", None),
    ("POST", f"/booking/pricing/fx-rates/{ROW}/reject", {"reason": "Not agreed"}),
    ("POST", f"/booking/pricing/cancellation-policies/{ROW}/approve", None),
    ("POST", f"/booking/pricing/partner-tier-discounts/{ROW}/approve", None),
    ("POST", f"/booking/pricing/partner-rate-cards/{ROW}/approve", None),
    ("POST", f"/booking/b2b-desk/price-adjustments/{ROW}/approve", None),
    ("POST", f"/booking/catalog/allotments/{ROW}/approve", None),
    ("POST", f"/booking/supply/stop-sales/{ROW}/end", None),
    ("POST", f"/booking/supplier-bills/{ROW}/payouts", {}),
    ("POST", "/booking/billing-entities", {}),
    *SERVICE_GATED,
]


@pytest.mark.usefixtures("dev")
@pytest.mark.parametrize("method, path, body", PROBES)
def test_the_maker_probe_is_answered_before_any_record_is_read(method: str, path: str, body: Any) -> None:
    r, conn = _maker(method, path, body)
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == "forbidden"
    # Only the resolver read: no synthetic check, no staff row, no record.
    read = [sql for sql, _ in conn.calls if GRANT not in sql and ROLE not in sql]
    assert read == [], read


@pytest.mark.usefixtures("dev")
@pytest.mark.parametrize(
    "method, path, body",
    [
        ("PATCH", f"/booking/contracts/{ROW}", {"status": "expired"}),  # a probe route whose body approves nothing
        ("POST", f"/booking/supply/stop-sales/{ROW}/decision", {"action": "withdraw"}),
        ("POST", f"/booking/suppliers/SUP-{'0' * 5}115/contracts", {}),  # a plain write, not allow-listed
    ],
)
def test_the_maker_never_writes_outside_its_list(method: str, path: str, body: Any) -> None:
    r, conn = _maker(method, path, body)
    assert r.status_code == 403 and r.json()["detail"] == "e2e_read_only", r.text
    assert conn.sql(IDENTITY_SELECT) == []


@pytest.mark.usefixtures("dev")
def test_the_maker_needs_its_exact_groups() -> None:
    r, conn = _maker("POST", f"/booking/pricing/rules/{ROW}/approve", groups=["booking-custom"])
    assert r.status_code == 403 and r.json()["detail"] == "e2e_scope"
    assert conn.calls == []


def test_the_maker_is_refused_outside_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "prod")
    cis = LocalCis()
    conn = custom_conn()
    with client(cis, conn) as c:
        r = c.get("/booking/staff/me", headers=auth(custom_token(cis, email=MAKER)))
    assert r.status_code == 403 and r.json()["detail"] == "e2e_not_allowed_here"
    assert conn.calls == []


@pytest.mark.usefixtures("dev")
def test_the_maker_reads_with_its_custom_role() -> None:
    cis = LocalCis()
    conn = custom_conn()
    with client(cis, conn) as c:
        r = c.get("/booking/staff/me", headers=auth(custom_token(cis, email=MAKER)))
    assert r.status_code == 200 and set(r.json()["permissions"]) == custom_roles.SEED_CAPABILITIES


def test_every_route_gated_approval_is_a_maker_probe() -> None:
    maker = e2e_staff.E2E_WRITERS[MAKER]
    by_path = {r.path: r for r in _routes()}
    for method, path, _ in _route_gated_approvals():
        assert e2e_staff.is_probe(maker, method, by_path[path]), path
    for (method, path), _ in e2e_staff.SERVICE_PROBES.items():
        assert e2e_staff.is_probe(maker, method, by_path[path]), path


def test_other_identities_never_probe() -> None:
    route = {r.path: r for r in _routes()}["/booking/pricing/rules/{rule_id}/approve"]
    for email, writer in e2e_staff.E2E_WRITERS.items():
        if email != MAKER:
            assert not e2e_staff.is_probe(writer, "POST", route), email
            e2e_staff.check_probe(writer, "POST", route, None, frozenset())  # a no-op


def test_a_probe_the_session_could_pass_is_never_run() -> None:
    maker = e2e_staff.E2E_WRITERS[MAKER]
    route = {r.path: r for r in _routes()}["/booking/pricing/rules/{rule_id}/approve"]
    with pytest.raises(Exception) as err:
        e2e_staff.check_probe(maker, "POST", route, None, frozenset({"pricing.approve"}))
    assert getattr(err.value, "detail", None) == "e2e_read_only"
