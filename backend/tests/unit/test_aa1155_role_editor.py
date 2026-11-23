"""AA-1155 T6 (root spec R2.2, R2.3, R4.4): the Booking role editor and the seed allow-list, without a database.

- The bk-062 seed of "Product, ops & accounting", the code's :data:`app.auth.custom_roles.SEED_CAPABILITIES` and the
  code's capability set agree (a drift in any of the three fails here); the migration's approve-class CHECK and the
  code's approve class are the same list.
- ``POST`` / ``PATCH /booking/staff-roles``: ``is_owner`` only (403 ``owner_only`` before any write, the marker read
  from the CIS view, an unreadable view = not an owner); an approve-class or unknown capability is 400 before any
  write; a new role clones the built-in role's capabilities without the approve class; every change writes one audit
  row with the role before and after and drops the resolver cache.
- ``GET /booking/staff-roles``: ``staff.admin`` or ``is_owner``; ``can_edit`` says which.
The SQL on PostgreSQL (the CHECKs, the audit row, the resolver seeing an edit) is
``tests/integration/test_aa1155_custom_roles.py``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.auth import custom_roles, e2e_staff, role_editor
from app.auth.staff import PERMISSIONS, STAFF_ROLES, Staff, current_staff, get_pool, role_permissions
from app.main import create_app
from app.settings import Settings
from tests.unit.fakes import ScriptedConn

MIGRATION = Path(__file__).resolve().parents[3] / "migrations" / "062_custom_staff_roles.sql"
LATER_067 = Path(__file__).resolve().parents[3] / "migrations" / "067_supplier_bank_accounts.sql"
GRANT = "FROM shared.v_staff_booking_grant"
ONE = "WHERE r.code = $1 GROUP BY r.code"
ALL = "GROUP BY r.code ORDER BY r.archived"
INSERT_ROLE = "INSERT INTO booking.staff_role (code"
INSERT_CAPS = "INSERT INTO booking.staff_role_capability"
LOCK = "FROM booking.staff_role WHERE code = $1 FOR UPDATE"
AUDIT = "INSERT INTO booking.audit_logs"
CODE = "custom_zz_harbour_desk"
SUB = "6c0d0000-0000-4000-8000-0000000002bb"
NOW = datetime(2026, 11, 21, 9, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def fresh_cache() -> Iterator[None]:
    custom_roles.forget()
    yield
    custom_roles.forget()


# ── The seed (R2.3) ──────────────────────────────────────────────────────────


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_the_migration_seed_is_the_code_allow_list() -> None:
    section = _sql().split("-- ── 5. Seed")[1].split("-- ── Guards")[0]
    array = re.search(r"unnest\(ARRAY\[(.*?)\]\)", section, re.S)
    assert array is not None
    seeded = set(re.findall(r"'([a-z0-9_.]+)'", array.group(1)))
    # bk-067 (AA-1158 part 2, Q10) adds the entry of supplier bank accounts to the same role.
    later = re.search(
        r"SELECT r\.code, '([a-z_.]+)', 'migration:bk-067'.*?WHERE r\.code = '([a-z_]+)'",
        LATER_067.read_text(encoding="utf-8"),
        re.S,
    )
    assert later is not None and later.group(2) == custom_roles.SEED_CODE
    assert seeded | {later.group(1)} == custom_roles.SEED_CAPABILITIES
    assert f"'{custom_roles.SEED_CODE}', '{custom_roles.SEED_LABEL}', '{custom_roles.SEED_BASED_ON}'" in section


def test_the_seed_is_known_to_the_code_and_holds_no_approval() -> None:
    assert custom_roles.SEED_CAPABILITIES <= set(PERMISSIONS)
    assert not {c for c in custom_roles.SEED_CAPABILITIES if custom_roles.is_approve_class(c)}
    # The S285 audit and Q3: never these.
    assert not custom_roles.SEED_CAPABILITIES & {"customer.admin", "trip_case.assign", "version.write"}
    # AA-1158 Q10: entry of supplier bank accounts only, never their read or approval.
    assert {c for c in custom_roles.SEED_CAPABILITIES if c.startswith("supplier_bank.")} == {"supplier_bank.write"}
    assert len(custom_roles.SEED_CAPABILITIES) == 25


def _approve_check_sql() -> str:
    """The newest migration that (re)defines the approve CHECK (bk-062, re-added by bk-067 with supplier_bank.read)."""
    marker = "staff_role_capability_not_approve_chk CHECK ("
    found = [p for p in sorted(MIGRATION.parent.glob("*.sql")) if marker in p.read_text(encoding="utf-8")]
    return found[-1].read_text(encoding="utf-8").split(marker)[-1].split(");")[0]


def test_the_migration_check_is_the_code_approve_class() -> None:
    check = _approve_check_sql()
    named = set(re.findall(r"'([a-z0-9_.]+)'", check.split("NOT IN")[1]))
    assert named == set(custom_roles.APPROVE_NAMED)
    assert r"\.approve(_[a-z0-9_]+)?$" in check
    assert custom_roles.APPROVE_SUFFIX.pattern == r"\.approve(_[a-z0-9_]+)?$"
    # The TEMPORARY E2E module keeps its own copy (it imports nothing of the auth core): the same list.
    assert e2e_staff._APPROVE_NAMED == custom_roles.APPROVE_NAMED
    assert e2e_staff._APPROVE_SUFFIX.pattern == custom_roles.APPROVE_SUFFIX.pattern


def test_the_code_pattern_is_the_migration_pattern() -> None:
    assert "code ~ '^custom_[a-z][a-z0-9_]{1,46}$'" in _sql()
    assert custom_roles.CUSTOM_CODE.pattern == "^custom_[a-z][a-z0-9_]{1,46}$"
    for built_in in STAFF_ROLES:
        assert not custom_roles.CUSTOM_CODE.fullmatch(built_in)


# ── Validation (R2.2) ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "capabilities, code",
    [
        (["supply.read", "pricing.approve"], "approve_class_capability"),
        (["supplier.activate"], "approve_class_capability"),
        (["contract.publish", "stop_sale.approve"], "approve_class_capability"),
        (["payout.write"], "approve_class_capability"),
        (["billing_config.write"], "approve_class_capability"),
        (["staff.admin"], "approve_class_capability"),
        (["pricing_policy.approve_admin"], "approve_class_capability"),
        (["supply.read", "made.up"], "unknown_capability"),
        (["supplier_bank.approve"], "approve_class_capability"),
        ("supply.read", "invalid_capabilities"),
        ([7], "invalid_capabilities"),
    ],
)
def test_capabilities_are_refused(capabilities: Any, code: str) -> None:
    with pytest.raises(role_editor.BadRequest) as err:
        role_editor.check_capabilities(capabilities)
    assert err.value.code == code and err.value.status_code == 400


def test_every_assignable_capability_is_accepted() -> None:
    assert role_editor.check_capabilities(sorted(custom_roles.assignable())) == custom_roles.assignable()
    assert role_editor.check_capabilities([]) == frozenset()


@pytest.mark.parametrize("code", ["sales", "custom_", "custom_X", "custom_9a", "Custom_ab", "custom_a", 7, None])
def test_bad_codes(code: Any) -> None:
    with pytest.raises(role_editor.BadRequest):
        role_editor.check_code(code)


@pytest.mark.parametrize("label", ["", "   ", "a" * 81, "tab\there", None])
def test_bad_labels(label: Any) -> None:
    with pytest.raises(role_editor.BadRequest):
        role_editor.check_label(label)


@pytest.mark.parametrize("based_on", STAFF_ROLES)
def test_a_clone_keeps_the_built_in_role_without_its_approvals(based_on: str) -> None:
    clone = role_editor.clone_of(based_on)
    assert clone <= role_permissions(based_on)
    assert clone == {c for c in role_permissions(based_on) if not custom_roles.is_approve_class(c)}


# ── The API (R4.4) ───────────────────────────────────────────────────────────


def staff(role: str = "admin", sub: str | None = SUB) -> Staff:
    return Staff(f"cog:{role}-1", None, role, role, True, "admin", cognito_sub=sub)


def view_row(code: str = CODE, caps: Any = ("supply.read",), **over: Any) -> dict[str, Any]:
    return {
        "code": code,
        "label": "Harbour desk",
        "based_on": "contracting",
        "archived": False,
        "capabilities": sorted(caps),
        "holders": 0,
        "created_at": NOW,
        "created_by": "cog:owner-1",
        "updated_at": NOW,
        "updated_by": "cog:owner-1",
        **over,
    }


def conn_for(*, owner: bool, one: Any = None) -> ScriptedConn:
    grant = {"booking_role_code": None, "is_owner": owner, "is_business_owner": False}
    return (
        ScriptedConn()
        .on(GRANT, grant)
        .on(INSERT_ROLE, CODE)
        .on(LOCK, CODE)
        .on(ONE, view_row() if one is None else one)
        .on(ALL, [view_row()])
    )


def _client(who: Staff, conn: ScriptedConn) -> TestClient:
    app = create_app(Settings(database_url=None, environment="test"))
    app.dependency_overrides[current_staff] = lambda: who
    app.dependency_overrides[get_pool] = lambda: conn
    return TestClient(app)


def _writes(conn: ScriptedConn) -> list[str]:
    return [sql for sql, _ in conn.calls if sql.startswith(("INSERT", "UPDATE", "DELETE"))]


NEW = {"code": CODE, "label": "Harbour desk", "based_on": "contracting"}


@pytest.mark.parametrize(
    "method, path, body",
    [
        ("POST", "/booking/staff-roles", NEW),
        ("PATCH", f"/booking/staff-roles/{CODE}", {"label": "Quay desk"}),
        ("PATCH", f"/booking/staff-roles/{CODE}", {"archived": True}),
    ],
)
@pytest.mark.parametrize("who", ["admin", "sales", "no_identity"])
def test_a_non_owner_is_refused_every_write(method: str, path: str, body: Any, who: str) -> None:
    conn = conn_for(owner=False)
    caller = staff("admin", sub=None) if who == "no_identity" else staff(who)
    r = _client(caller, conn).request(method, path, json=body)
    assert r.status_code == 403 and r.json()["detail"] == "owner_only", r.text
    assert _writes(conn) == []


def test_an_unreadable_view_is_not_an_owner() -> None:
    conn = conn_for(owner=True).on(GRANT, asyncpg.InsufficientPrivilegeError("permission denied"))
    r = _client(staff(), conn).post("/booking/staff-roles", json=NEW)
    assert r.status_code == 403 and r.json()["detail"] == "owner_only"
    assert _writes(conn) == []


def test_the_owner_marker_is_read_fresh_on_every_write() -> None:
    conn = conn_for(owner=True)
    c = _client(staff(), conn)
    c.patch(f"/booking/staff-roles/{CODE}", json={"label": "Quay desk"})
    c.patch(f"/booking/staff-roles/{CODE}", json={"label": "Pier desk"})
    assert conn.sql(GRANT) == [(SUB,), (SUB,)]


@pytest.mark.parametrize(
    "body, detail",
    [
        ({**NEW, "capabilities": ["supply.read", "pricing.approve"]}, "approve_class_capability"),
        ({**NEW, "capabilities": ["payout.write"]}, "approve_class_capability"),
        ({**NEW, "capabilities": ["made.up"]}, "unknown_capability"),
        ({**NEW, "code": "sales"}, "invalid_role_code"),
        ({**NEW, "based_on": "custom_other"}, "unknown_base_role"),
        ({**NEW, "label": " "}, "invalid_label"),
    ],
)
def test_an_owner_is_refused_a_bad_role_with_400_before_any_write(body: dict[str, Any], detail: str) -> None:
    conn = conn_for(owner=True)
    r = _client(staff(), conn).post("/booking/staff-roles", json=body)
    assert r.status_code == 400 and r.json()["detail"] == detail, r.text
    assert _writes(conn) == []


@pytest.mark.parametrize(
    "caps, detail", [(["stop_sale.approve"], "approve_class_capability"), (["x.y"], "unknown_capability")]
)
def test_an_owner_is_refused_a_bad_patch_with_400(caps: list[str], detail: str) -> None:
    conn = conn_for(owner=True)
    r = _client(staff(), conn).patch(f"/booking/staff-roles/{CODE}", json={"capabilities": caps})
    assert r.status_code == 400 and r.json()["detail"] == detail
    assert _writes(conn) == []


def test_an_owner_clones_a_built_in_role_audited() -> None:
    clone = role_editor.clone_of("contracting")
    conn = conn_for(owner=True, one=view_row(caps=clone))
    r = _client(staff(), conn).post("/booking/staff-roles", json=NEW)
    assert r.status_code == 201, r.text
    assert r.json()["code"] == CODE and set(r.json()["capabilities"]) == clone
    (insert,) = conn.sql(INSERT_ROLE)
    assert insert == (CODE, "Harbour desk", "contracting", "cog:admin-1")
    (caps,) = conn.sql(INSERT_CAPS)
    assert set(caps[1]) == clone and not any(custom_roles.is_approve_class(c) for c in caps[1])
    (audit_args,) = conn.sql(AUDIT)
    assert audit_args[3] == "staff_role.created" and audit_args[4] == "staff_role"
    assert audit_args[6] is None and json.loads(audit_args[7])["capabilities"] == sorted(clone)


def test_an_owner_ticks_capabilities_audited_before_and_after() -> None:
    before = view_row(caps=("supply.read", "bill.read"))
    after = view_row(caps=("supply.read", "ops.read"))
    answers = iter([before, after])
    conn = conn_for(owner=True).on(ONE, lambda *_: next(answers))
    r = _client(staff(), conn).patch(f"/booking/staff-roles/{CODE}", json={"capabilities": ["supply.read", "ops.read"]})
    assert r.status_code == 200 and r.json()["capabilities"] == ["ops.read", "supply.read"]
    (removed,) = conn.sql("DELETE FROM booking.staff_role_capability")
    assert removed == (CODE, ["bill.read"])
    (added,) = conn.sql(INSERT_CAPS)
    assert added[1] == ["ops.read"]
    (audit_args,) = conn.sql(AUDIT)
    assert audit_args[3] == "staff_role.updated"
    assert json.loads(audit_args[6])["capabilities"] == ["bill.read", "supply.read"]
    assert json.loads(audit_args[7])["capabilities"] == ["ops.read", "supply.read"]
    assert "+ops.read" in audit_args[9] and "-bill.read" in audit_args[9]


def test_an_owner_archives_a_role_and_the_resolver_cache_is_dropped() -> None:
    custom_roles._store(custom_roles._roles, CODE, None)
    answers = iter([view_row(), view_row(archived=True)])
    conn = conn_for(owner=True).on(ONE, lambda *_: next(answers))
    r = _client(staff(), conn).patch(f"/booking/staff-roles/{CODE}", json={"archived": True})
    assert r.status_code == 200 and r.json()["archived"] is True
    (update,) = conn.sql("UPDATE booking.staff_role SET")
    assert update == (CODE, None, True, "cog:admin-1")
    assert custom_roles._roles == {}
    (audit_args,) = conn.sql(AUDIT)
    assert audit_args[9] == "archived"


def test_a_patch_that_changes_nothing_writes_nothing() -> None:
    conn = conn_for(owner=True)
    r = _client(staff(), conn).patch(f"/booking/staff-roles/{CODE}", json={"label": "Harbour desk"})
    assert r.status_code == 200
    assert _writes(conn) == []


def test_an_unknown_role_is_404() -> None:
    conn = conn_for(owner=True).on(LOCK, None)
    r = _client(staff(), conn).patch(f"/booking/staff-roles/{CODE}", json={"archived": True})
    assert r.status_code == 404 and r.json()["detail"] == "role_not_found"


def test_a_taken_code_is_409() -> None:
    conn = conn_for(owner=True).on(INSERT_ROLE, None)
    r = _client(staff(), conn).post("/booking/staff-roles", json=NEW)
    assert r.status_code == 409 and r.json()["detail"] == "role_exists"
    assert conn.sql(INSERT_CAPS) == [] and conn.sql(AUDIT) == []


@pytest.mark.parametrize(
    "role, owner, status, can_edit",
    [
        ("admin", False, 200, False),
        ("admin", True, 200, True),
        ("contracting", True, 200, True),
        ("contracting", False, 403, None),
    ],
)
def test_who_reads_the_editor(role: str, owner: bool, status: int, can_edit: bool | None) -> None:
    r = _client(staff(role), conn_for(owner=owner)).get("/booking/staff-roles")
    assert r.status_code == status, r.text
    if status == 200:
        body = r.json()
        assert body["can_edit"] is can_edit
        assert [b["code"] for b in body["builtin"]] == list(STAFF_ROLES)
        assert body["custom"][0]["code"] == CODE
        flags = {c["name"]: c["assignable"] for c in body["capabilities"]}
        assert set(flags) == set(PERMISSIONS)
        assert {n for n, ok in flags.items() if ok} == custom_roles.assignable()
