"""AA-996 slice 5 (Figma audit fix): the additive API changes behind the billing detail, staff and audit-log screens.

- ``PATCH /booking/staff/{id}`` takes a ``reason`` (required to deactivate, else optional); it lands in the audit row
  (``after.reason`` + a summary that starts with "Deactivated." / "Activated."), never on the profile.
- ``GET /booking/audit-logs`` takes ``q`` (summary, action, record label or actor name; ILIKE, ``%`` / ``_`` literal).
- A payment carries its ``customer`` (id, code, name); the payment detail adds the trip case facts and, with
  ``customer.read``, the customer's e-mail and phone (Figma PAYMENT DETAILS cards).
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import audit
from app.auth import admin
from app.auth.staff import Staff, current_staff, get_pool
from app.errors import Invalid
from app.main import create_app
from app.ops import payments as pay
from app.settings import Settings
from tests.unit.fakes import ScriptedConn

ADMIN = Staff("a-1", None, "Admin", "admin", True, "admin")
SALES = Staff("u-1", None, "Sales One", "sales", True, "reviewer")
CASE_ID = "c0000000-0000-4000-8000-000000000001"
CUSTOMER_ID = "c0000000-0000-4000-8000-0000000000c1"
AUDIT = "INSERT INTO booking.audit_logs"
STAFF_SELECT = "FROM booking.staff_users WHERE cis_user_id = $1 FOR UPDATE"
STAFF_UPDATE = "UPDATE booking.staff_users SET active"


def staff_row(**over: Any) -> dict[str, Any]:
    row = {
        "cis_user_id": "u-7",
        "email": "seven@example.test",
        "name": "Seven",
        "role": "sales",
        "active": True,
        "cis_role": "reviewer",
        "created_at": None,
        "updated_at": None,
        "last_seen_at": None,
        "cognito_sub": None,
    }
    row.update(over)
    return row


# ── Staff deactivation reason ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deactivate_reason_is_audited() -> None:
    conn = ScriptedConn([(STAFF_SELECT, staff_row()), (STAFF_UPDATE, staff_row(active=False))])
    out = await admin.update_staff(conn, ADMIN, "u-7", active=False, role=None, reason="  Left the company  ")
    assert out["active"] is False
    [row] = conn.sql(AUDIT)
    after = json.loads(row[7])
    assert after == {"cis_user_id": "u-7", "active": False, "role": "sales", "reason": "Left the company"}
    assert row[9].startswith("Deactivated. ") and row[9].endswith(". Reason: Left the company")
    # The reason is never written to the profile row.
    assert all("reason" not in str(args) for args in conn.sql(STAFF_UPDATE))


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", [None, "", "   "])
async def test_deactivate_without_a_reason_is_refused(reason: str | None) -> None:
    conn = ScriptedConn([(STAFF_SELECT, staff_row())])
    with pytest.raises(Invalid) as err:
        await admin.update_staff(conn, ADMIN, "u-7", active=False, role=None, reason=reason)
    assert err.value.code == "reason_required"
    assert conn.sql(AUDIT) == [] and conn.sql(STAFF_UPDATE) == []


@pytest.mark.asyncio
async def test_activate_without_a_reason_keeps_the_old_audit_shape() -> None:
    conn = ScriptedConn([(STAFF_SELECT, staff_row(active=False)), (STAFF_UPDATE, staff_row())])
    await admin.update_staff(conn, ADMIN, "u-7", active=True, role=None)
    [row] = conn.sql(AUDIT)
    assert "reason" not in json.loads(row[7]) and "Reason" not in row[9]
    assert row[9].startswith("Activated. active False -> True")


@pytest.mark.asyncio
async def test_role_change_summary_has_no_access_verb() -> None:
    conn = ScriptedConn([(STAFF_SELECT, staff_row()), (STAFF_UPDATE, staff_row(role="contracting"))])
    await admin.update_staff(conn, ADMIN, "u-7", active=None, role="contracting")
    [row] = conn.sql(AUDIT)
    assert row[9].startswith("active True -> True, role sales -> contracting")


@pytest.mark.asyncio
async def test_reason_without_a_change_writes_nothing() -> None:
    conn = ScriptedConn([(STAFF_SELECT, staff_row(active=False))])
    await admin.update_staff(conn, ADMIN, "u-7", active=False, role=None, reason="again")
    assert conn.sql(AUDIT) == [] and conn.sql(STAFF_UPDATE) == []


def client(staff: Staff, conn: ScriptedConn) -> TestClient:
    app = create_app(Settings(database_url=None, environment="test"))
    app.dependency_overrides[current_staff] = lambda: staff
    app.dependency_overrides[get_pool] = lambda: conn
    return TestClient(app)


def test_patch_staff_passes_the_reason() -> None:
    conn = ScriptedConn([(STAFF_SELECT, staff_row()), (STAFF_UPDATE, staff_row(active=False))])
    r = client(ADMIN, conn).patch("/booking/staff/u-7", json={"active": False, "reason": "Contract ended"})
    assert r.status_code == 200, r.text
    assert json.loads(conn.sql(AUDIT)[0][7])["reason"] == "Contract ended"


def test_patch_staff_deactivate_without_reason_is_422() -> None:
    conn = ScriptedConn([(STAFF_SELECT, staff_row())])
    r = client(ADMIN, conn).patch("/booking/staff/u-7", json={"active": False})
    assert r.status_code == 422 and r.json()["detail"] == "reason_required"


def test_patch_staff_reason_is_bounded() -> None:
    r = client(ADMIN, ScriptedConn()).patch("/booking/staff/u-7", json={"active": False, "reason": "x" * 501})
    assert r.status_code == 422


# ── Audit log search ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_audit_search_matches_summary_action_label_and_actor() -> None:
    conn = ScriptedConn()
    await audit.list_logs(conn, q=" 50%_off ", date_from=date(2026, 1, 1))
    flat, args = conn.calls[0]
    assert "l.summary ILIKE $2 OR l.action ILIKE $2 OR l.entity_label ILIKE $2 OR s.name ILIKE $2" in flat
    assert args[1] == "%50\\%\\_off%"


@pytest.mark.asyncio
async def test_audit_blank_search_adds_no_filter() -> None:
    conn = ScriptedConn()
    await audit.list_logs(conn, q="   ")
    assert "ILIKE" not in conn.calls[0][0]


def test_audit_route_passes_q() -> None:
    conn = ScriptedConn()
    r = client(ADMIN, conn).get("/booking/audit-logs", params={"q": "deactivat"})
    assert r.status_code == 200, r.text
    assert conn.calls[0][1][0] == "%deactivat%"
    assert client(ADMIN, ScriptedConn()).get("/booking/audit-logs", params={"q": "x" * 201}).status_code == 422
    assert client(SALES, ScriptedConn()).get("/booking/audit-logs", params={"q": "x"}).status_code == 403


# ── Payment customer and trip context ────────────────────────────────────────


def payment_row(**over: Any) -> dict[str, Any]:
    row = {
        "id": "p0000000-0000-4000-8000-000000000001",
        "ref": "PMT-000001",
        "state": "verified",
        "method": "bank_transfer",
        "evidence_file_id": "f",
        "recorded_by": "u-1",
        "recorded_by_name": "Sales One",
        "verified_by": "a-1",
        "verified_by_name": "Admin",
        "voided_by": None,
        "voided_by_name": None,
        "invoice_id": "i0000000-0000-4000-8000-000000000001",
        "invoice_number": "INV-000001",
        "invoice_status": "paid",
        "trip_case_id": CASE_ID,
        "trip_case_code": "TC-000001",
        "customer_id": CUSTOMER_ID,
        "customer_code": "CUS-000001",
        "customer_name": "Ann Traveller",
    }
    row.update(over)
    return row


def test_payment_out_carries_the_customer() -> None:
    out = pay.payment_out(payment_row(), SALES)
    assert out["customer"] == {"id": CUSTOMER_ID, "code": "CUS-000001", "name": "Ann Traveller"}
    # A B2B trip has no customer; a row read without the new columns stays valid.
    assert (
        pay.payment_out(payment_row(customer_id=None, customer_code=None, customer_name=None), SALES)["customer"]
        is None
    )
    old = payment_row()
    for k in ("customer_id", "customer_code", "customer_name"):
        old.pop(k)
    assert pay.payment_out(old, SALES)["customer"] is None


def detail_conn() -> ScriptedConn:
    return ScriptedConn(
        [
            ("FROM booking.payments p", payment_row()),
            ("FROM booking.ops_files f", []),
            ("b.balance::text AS balance FROM booking.invoices i", {"currency": "USD", "balance": "0.00"}),
            (
                "FROM booking.trip_cases WHERE id",
                {
                    "status": "paid",
                    "trip_style": "Cultural",
                    "start_date": date(2027, 11, 12),
                    "end_date": date(2027, 11, 26),
                    "pax": 2,
                    "channel": "b2c",
                },
            ),
            ("SELECT email, phone FROM booking.customers", {"email": "ann@example.test", "phone": "+10000000000"}),
        ]
    )


@pytest.mark.asyncio
async def test_payment_detail_adds_trip_and_contact() -> None:
    out = await pay.get_payment(detail_conn(), SALES, "PMT-000001")
    assert out["trip_case"]["code"] == "TC-000001" and out["trip_case"]["trip_style"] == "Cultural"
    assert out["trip_case"]["end_date"] == date(2027, 11, 26) and out["trip_case"]["pax"] == 2
    assert out["customer"]["email"] == "ann@example.test" and out["customer"]["phone"] == "+10000000000"


@pytest.mark.asyncio
async def test_payment_detail_contact_needs_customer_read() -> None:
    conn = detail_conn()
    # A billing reader without customer.read (preview cap has neither; a role matrix change could split them).
    reader = Staff("u-5", None, "Reader", "sales", True, "reviewer")
    reader_no_customer = _without(reader, "customer.read")
    out = await pay.get_payment(conn, reader_no_customer, "PMT-000001")
    assert out["customer"] == {"id": CUSTOMER_ID, "code": "CUS-000001", "name": "Ann Traveller"}
    assert conn.sql("SELECT email, phone FROM booking.customers") == []


def _without(staff: Staff, permission: str) -> Staff:
    class Limited(Staff):
        def can(self, p: str) -> bool:  # noqa: D102
            return p != permission and super().can(p)

    return Limited(**{f: getattr(staff, f) for f in staff.__dataclass_fields__})


@pytest.mark.asyncio
async def test_payment_detail_without_a_customer() -> None:
    conn = detail_conn().on("FROM booking.payments p", payment_row(customer_id=None))
    out = await pay.get_payment(conn, SALES, "PMT-000001")
    assert out["customer"] is None and out["trip_case"]["status"] == "paid"
