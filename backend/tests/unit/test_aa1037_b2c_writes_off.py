"""AA-1037 (root ADR 0019 d8, AA-1034 R0 follow-up): with B2C_INTAKE_ENABLED off (the default) no B2C write path is
left.

- Routes that only write B2C records (customer invoices and payments, customer edit / archive, a staff-made case, a
  new case from a trip plan) answer 410 ``b2b_only`` before the service is called and before any SQL.
- Routes shared with B2B cases read only the case's ``channel``: a ``b2c`` case answers 410, a ``b2b`` case (or an
  unknown ref, left to the service's own 404) reaches the service.
- The billing reads stay, with every write affordance turned off.
- The switch on (``b2c_intake_enabled=True``) restores every route.
- The lifecycle and reminder jobs leave B2C rows alone while the switch is off.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import b2c
from app.auth.staff import Staff, current_staff, get_pool
from app.catalog import trip_plans
from app.customers import service as customers
from app.main import create_app
from app.ops import files as ops_files
from app.ops import invoices, lifecycle, payments, supplier_bookings, trip_checks
from app.settings import Settings
from app.trip_cases import service, versions
from tests.unit.fakes import ScriptedConn

ADMIN = Staff("u-1", None, "Admin One", "admin", True, "admin")
BOOKING_ID = "11111111-1111-4111-8111-111111111111"
CHECK_ID = "22222222-2222-4222-8222-222222222222"
FILE_ID = "33333333-3333-4333-8333-333333333333"
PAYMENT_ID = "44444444-4444-4444-8444-444444444444"
CUSTOMER_ID = "55555555-5555-4555-8555-555555555555"
PLAN = "66666666-6666-4666-8666-666666666666"

CASE_SQL = "SELECT channel FROM booking.trip_cases"
BOOKING_SQL = "FROM booking.supplier_bookings sb JOIN booking.trip_cases"
CHECK_SQL = "FROM booking.trip_case_checks k JOIN booking.trip_cases"
FILE_SQL = "FROM booking.ops_files f LEFT JOIN booking.trip_cases"

ADJUSTMENT = {"description": "Late check-out", "unit_price": "10", "reason": "asked by the guest"}
PAYMENT = {"amount": "100", "method": "cash", "received_on": "2026-11-01"}
FILE = {"file_name": "slip.pdf", "content_type": "application/pdf", "data_base64": "JVBERi0="}


class Recorder:
    def __init__(self, result: Any) -> None:
        self.calls: list[Any] = []
        self.result = result

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args[1:], kwargs))
        return self.result


# (module, function, canned result) of every service a guarded route calls.
SERVICES: list[tuple[Any, str, Any]] = [
    (invoices, "create", ({"invoice_number": "INV-000001"}, False)),
    (invoices, "update_draft", {"ok": True}),
    (invoices, "delete_draft", {"ok": True}),
    (invoices, "send", {"ok": True}),
    (invoices, "cancel", {"ok": True}),
    (invoices, "amend", {"ok": True}),
    (invoices, "remind", {"ok": True}),
    (payments, "record", ({"ref": "PMT-000001"}, False)),
    (payments, "update", {"ok": True}),
    (payments, "verify", {"ok": True}),
    (payments, "void", {"ok": True}),
    (customers, "update_customer", {"changed": []}),
    (customers, "archive_customer", None),
    (service, "create_case", {"code": "TC-000009"}),
    (service, "update_case", {"ok": True}),
    (service, "change_status", {"ok": True}),
    (service, "assign", {"ok": True}),
    (versions, "create_version", {"ok": True}),
    (versions, "update_version", {"ok": True}),
    (versions, "delete_version", None),
    (versions, "replace_items", {"ok": True}),
    (versions, "price_version", {"ok": True}),
    (versions, "send_version", {"ok": True}),
    (versions, "approve_version", {"ok": True}),
    (trip_plans, "use_for_trip_case", {"mode": "new_case"}),
    (supplier_bookings, "generate", {"ok": True}),
    (supplier_bookings, "create", {"ok": True}),
    (supplier_bookings, "update", {"ok": True}),
    (supplier_bookings, "request", {"ok": True}),
    (supplier_bookings, "confirm", {"ok": True}),
    (supplier_bookings, "decline", {"ok": True}),
    (supplier_bookings, "cancel", {"ok": True}),
    (supplier_bookings, "rerequest", {"ok": True}),
    (trip_checks, "create_custom", {"ok": True}),
    (trip_checks, "update", {"ok": True}),
    (ops_files, "upload", {"ok": True}),
    (ops_files, "remove", {"ok": True}),
]

# Routes that only exist for B2C: refused whatever the record (method, path, body, expected status when open).
B2C_ONLY = [
    ("POST", "/booking/trip-cases/TC-000001/invoices", {}, 201),
    ("PATCH", "/booking/invoices/INV-000001", {"notes": "x"}, 200),
    ("DELETE", "/booking/invoices/INV-000001", None, 200),
    ("POST", "/booking/invoices/INV-000001/send", None, 200),
    ("POST", "/booking/invoices/INV-000001/cancel", {"reason": "duplicate"}, 200),
    ("POST", "/booking/invoices/INV-000001/amend", {"adjustments": [ADJUSTMENT]}, 200),
    ("POST", "/booking/invoices/INV-000001/remind", None, 200),
    ("POST", "/booking/invoices/INV-000001/payments", PAYMENT, 201),
    ("PATCH", "/booking/payments/PMT-000001", {"note": "x"}, 200),
    ("POST", "/booking/payments/PMT-000001/verify", None, 200),
    ("POST", "/booking/payments/PMT-000001/void", {"reason": "typo"}, 200),
    ("PATCH", "/booking/customers/CUS-000001", {"name": "Redacted"}, 200),
    ("DELETE", "/booking/customers/CUS-000001", None, 204),
    ("POST", "/booking/trip-cases", {"customer_id": CUSTOMER_ID}, 201),
    ("POST", f"/booking/catalog/trip-plans/{PLAN}/trip-cases", {"customer_id": CUSTOMER_ID}, 201),
]

# Routes on one trip case: (method, path, body, expected status when allowed).
CASE_ROUTES = [
    ("PATCH", "/booking/trip-cases/TC-000001", {"notes": "call back"}, 200),
    ("POST", "/booking/trip-cases/TC-000001/status", {"to_status": "cancelled", "reason": "no reply"}, 200),
    ("POST", "/booking/trip-cases/TC-000001/assign", {"cis_user_id": "u-2"}, 200),
    ("POST", "/booking/trip-cases/TC-000001/versions", None, 201),
    ("PATCH", "/booking/trip-cases/TC-000001/versions/1", {"title": "Plan B"}, 200),
    ("DELETE", "/booking/trip-cases/TC-000001/versions/1", None, 204),
    ("PUT", "/booking/trip-cases/TC-000001/versions/1/items", {"items": []}, 200),
    ("POST", "/booking/trip-cases/TC-000001/versions/1/price", {"channel": "b2c_tailor_made"}, 200),
    ("POST", "/booking/trip-cases/TC-000001/versions/1/send", {"subject": "Your trip", "message": "Hello"}, 200),
    ("POST", "/booking/trip-cases/TC-000001/versions/1/approve", None, 200),
    ("POST", f"/booking/catalog/trip-plans/{PLAN}/trip-cases", {"trip_case": "TC-000001"}, 201),
    ("POST", "/booking/trip-cases/TC-000001/supplier-bookings/generate", None, 200),
    ("POST", "/booking/trip-cases/TC-000001/supplier-bookings", {"supplier_id": BOOKING_ID}, 201),
    ("POST", "/booking/trip-cases/TC-000001/checks", {"title": "Visa copies"}, 201),
]

BOOKING_ROUTES = [
    ("PATCH", f"/booking/supplier-bookings/{BOOKING_ID}", {"notes": "x"}, 200),
    ("POST", f"/booking/supplier-bookings/{BOOKING_ID}/request", None, 200),
    ("POST", f"/booking/supplier-bookings/{BOOKING_ID}/confirm", {"confirmation_ref": "CF-1"}, 200),
    ("POST", f"/booking/supplier-bookings/{BOOKING_ID}/decline", {"decline_code": "sold_out"}, 200),
    ("POST", f"/booking/supplier-bookings/{BOOKING_ID}/cancel", {"reason": "trip moved"}, 200),
    ("POST", f"/booking/supplier-bookings/{BOOKING_ID}/rerequest", {"reason": "new dates"}, 200),
    (
        "POST",
        "/booking/ops-files",
        {**FILE, "kind": "supplier_voucher", "entity_type": "supplier_booking", "entity_id": BOOKING_ID},
        201,
    ),
]


@pytest.fixture
def stubs(monkeypatch: pytest.MonkeyPatch) -> dict[str, Recorder]:
    recs: dict[str, Recorder] = {}
    for module, name, result in SERVICES:
        rec = Recorder(result)
        monkeypatch.setattr(module, name, rec)
        recs[f"{module.__name__}.{name}"] = rec
    return recs


def client(conn: ScriptedConn | None = None, **settings: Any) -> TestClient:
    app = create_app(Settings(database_url=None, environment="test", **settings))
    app.dependency_overrides[current_staff] = lambda: ADMIN
    pool = conn or ScriptedConn()
    app.dependency_overrides[get_pool] = lambda: pool
    return TestClient(app)


def call(c: TestClient, method: str, path: str, body: Any) -> Any:
    return c.request(method, path, json=body) if body is not None else c.request(method, path)


def called(stubs: dict[str, Recorder]) -> list[str]:
    return [name for name, rec in stubs.items() if rec.calls]


def assert_gone(r: Any) -> None:
    assert r.status_code == 410, r.text
    assert r.json()["detail"] == "b2b_only" and r.json()["message"] == b2c.MESSAGE


@pytest.mark.parametrize(("method", "path", "body", "_open"), B2C_ONLY)
def test_b2c_only_route_answers_410_before_any_read(
    stubs: dict[str, Recorder], method: str, path: str, body: Any, _open: int
) -> None:
    conn = ScriptedConn()
    assert_gone(call(client(conn), method, path, body))
    assert called(stubs) == []
    assert conn.calls == []  # refused before any SQL


@pytest.mark.parametrize(("method", "path", "body", "status"), B2C_ONLY)
def test_b2c_only_route_reopens_with_the_switch(
    stubs: dict[str, Recorder], method: str, path: str, body: Any, status: int
) -> None:
    r = call(client(b2c_intake_enabled=True), method, path, body)
    assert r.status_code == status, r.text
    assert len(called(stubs)) == 1


@pytest.mark.parametrize(("method", "path", "body", "_status"), CASE_ROUTES)
def test_case_route_refuses_a_b2c_case(
    stubs: dict[str, Recorder], method: str, path: str, body: Any, _status: int
) -> None:
    conn = ScriptedConn().on(CASE_SQL, "b2c")
    assert_gone(call(client(conn), method, path, body))
    assert called(stubs) == []
    assert conn.sql(CASE_SQL) == [("TC-000001",)]  # the case code, upper-cased, read once


@pytest.mark.parametrize(("method", "path", "body", "status"), CASE_ROUTES)
def test_case_route_keeps_a_b2b_case(
    stubs: dict[str, Recorder], method: str, path: str, body: Any, status: int
) -> None:
    r = call(client(ScriptedConn().on(CASE_SQL, "b2b")), method, path, body)
    assert r.status_code == status, r.text
    assert len(called(stubs)) == 1


@pytest.mark.parametrize(("method", "path", "body", "status"), CASE_ROUTES)
def test_case_route_with_the_switch_on_reads_no_channel(
    stubs: dict[str, Recorder], method: str, path: str, body: Any, status: int
) -> None:
    conn = ScriptedConn().on(CASE_SQL, "b2c")
    assert call(client(conn, b2c_intake_enabled=True), method, path, body).status_code == status
    assert conn.sql(CASE_SQL) == []


def test_unknown_case_is_left_to_the_service(stubs: dict[str, Recorder]) -> None:
    r = client(ScriptedConn()).patch("/booking/trip-cases/tc-404", json={"notes": "x"})
    assert r.status_code == 200 and called(stubs) == ["app.trip_cases.service.update_case"]


def test_case_by_uuid_reads_the_id() -> None:
    case_id = str(uuid.uuid4())
    conn = ScriptedConn().on(CASE_SQL, "b2c")
    assert_gone(client(conn).post(f"/booking/trip-cases/{case_id}/status", json={"to_status": "cancelled"}))
    assert conn.sql("WHERE id = $1::uuid") == [(case_id,)]


@pytest.mark.parametrize(("method", "path", "body", "status"), BOOKING_ROUTES)
@pytest.mark.parametrize("channel", ["b2c", "b2b"])
def test_supplier_booking_routes_follow_the_case_channel(
    stubs: dict[str, Recorder], method: str, path: str, body: Any, status: int, channel: str
) -> None:
    conn = ScriptedConn().on(BOOKING_SQL, channel)
    r = call(client(conn), method, path, body)
    if channel == "b2c":
        assert_gone(r)
        assert called(stubs) == []
    else:
        assert r.status_code == status, r.text
    assert conn.sql(BOOKING_SQL) == [(BOOKING_ID,)]


@pytest.mark.parametrize("channel", ["b2c", "b2b"])
def test_trip_check_patch_follows_the_case_channel(stubs: dict[str, Recorder], channel: str) -> None:
    conn = ScriptedConn().on(CHECK_SQL, channel)
    r = client(conn).patch(f"/booking/trip-checks/{CHECK_ID}", json={"status": "done"})
    if channel == "b2c":
        assert_gone(r)
        assert called(stubs) == []
    else:
        assert r.status_code == 200
    assert conn.sql(CHECK_SQL) == [(CHECK_ID,)]


def test_payment_evidence_upload_is_refused_without_a_read(stubs: dict[str, Recorder]) -> None:
    conn = ScriptedConn()
    body = {**FILE, "kind": "payment_evidence", "entity_type": "payment", "entity_id": PAYMENT_ID}
    assert_gone(client(conn).post("/booking/ops-files", json=body))
    assert conn.calls == [] and called(stubs) == []
    assert client(b2c_intake_enabled=True).post("/booking/ops-files", json=body).status_code == 201


def test_supplier_bill_files_stay_open(stubs: dict[str, Recorder]) -> None:
    conn = ScriptedConn()
    body = {**FILE, "kind": "supplier_bill", "entity_type": "supplier_bill", "entity_id": BOOKING_ID}
    assert client(conn).post("/booking/ops-files", json=body).status_code == 201
    assert conn.calls == []


@pytest.mark.parametrize(
    ("row", "refused"),
    [
        ({"entity_type": "payment", "channel": "b2c"}, True),
        ({"entity_type": "payment", "channel": None}, True),
        ({"entity_type": "supplier_booking", "channel": "b2c"}, True),
        ({"entity_type": "supplier_booking", "channel": "b2b"}, False),
        ({"entity_type": "supplier_bill", "channel": None}, False),
        ({"entity_type": "supplier_bill", "channel": "b2c"}, False),  # AA's payable, like its upload
        ({"entity_type": "supplier_payout", "channel": "b2c"}, False),
        (None, False),
    ],
)
def test_file_remove_follows_the_file(stubs: dict[str, Recorder], row: Any, refused: bool) -> None:
    conn = ScriptedConn().on(FILE_SQL, row)
    r = client(conn).post(f"/booking/ops-files/{FILE_ID}/remove", json={"reason": "wrong file"})
    if refused:
        assert_gone(r)
        assert called(stubs) == []
    else:
        assert r.status_code == 200
    assert conn.sql(FILE_SQL) == [(FILE_ID,)]


# ── Billing reads: kept, with no write affordance ────────────────────────────

INVOICE = {
    "invoice_number": "INV-000001",
    "actions": {"edit": True, "send": True, "record_payment": True, "cancel": False},
    "payments": [{"ref": "PMT-000001", "actions": {"verify": True, "void": True}}],
}


def test_frozen_view_turns_every_action_off() -> None:
    out = b2c.frozen_view({"items": [INVOICE], "total": 1})
    assert out["items"][0]["actions"] == {"edit": False, "send": False, "record_payment": False, "cancel": False}
    assert out["items"][0]["payments"][0]["actions"] == {"verify": False, "void": False}
    assert INVOICE["actions"]["edit"] is True  # the input is not modified
    case = b2c.frozen_view({"invoice": None, "invoiceable": True, "reason": None, "history": []})
    assert case["invoiceable"] is False and case["reason"] == "b2b_only"
    blocked = b2c.frozen_view({"invoiceable": False, "reason": "no_approved_version"})
    assert blocked == {"invoiceable": False, "reason": "no_approved_version"}
    assert b2c.frozen_view("text") == "text"


@pytest.mark.parametrize(
    ("path", "module", "name", "result"),
    [
        ("/booking/invoices/INV-000001", invoices, "get_invoice", INVOICE),
        ("/booking/invoices", invoices, "list_invoices", {"items": [INVOICE], "total": 1}),
        ("/booking/trip-cases/TC-000001/billing", invoices, "for_case", {"invoice": INVOICE, "invoiceable": False}),
        ("/booking/payments/PMT-000001", payments, "get_payment", {"ref": "PMT-1", "actions": {"verify": True}}),
        ("/booking/payments", payments, "list_payments", {"items": [{"actions": {"void": True}}], "total": 1}),
    ],
)
def test_billing_reads_stay_and_offer_no_write(
    monkeypatch: pytest.MonkeyPatch, path: str, module: Any, name: str, result: dict[str, Any]
) -> None:
    monkeypatch.setattr(module, name, Recorder(result))
    frozen = client().get(path)
    assert frozen.status_code == 200
    assert "true" not in frozen.text  # no action flag, no invoiceable left on
    opened = client(b2c_intake_enabled=True).get(path)
    assert opened.json() == result


# ── Jobs ─────────────────────────────────────────────────────────────────────


def test_lifecycle_handlers_carry_the_switch() -> None:
    off = lifecycle.build_handlers(False)
    on = lifecycle.build_handlers(True)
    assert set(off) == {lifecycle.KIND, lifecycle.REMINDERS_KIND}
    assert off[lifecycle.KIND].keywords == {"b2c_open": False}
    assert on[lifecycle.REMINDERS_KIND].keywords == {"b2c_open": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("b2c_open", [False, True])
async def test_lifecycle_candidates_skip_b2c_cases_while_frozen(
    monkeypatch: pytest.MonkeyPatch, b2c_open: bool
) -> None:
    async def rules(_: Any) -> dict[str, int]:
        return {"pre_departure_days": 7, "emergency_contact_keep_days": 90}

    async def tz(_: Any) -> str:
        return "UTC"

    async def no_clear(*_: Any, **__: Any) -> dict[str, int]:
        return {"cleared_checks": 0, "cleared_cases": 0}

    monkeypatch.setattr(lifecycle, "policy", rules)
    monkeypatch.setattr(lifecycle, "ops_timezone", tz)
    monkeypatch.setattr(lifecycle, "_clear", no_clear)
    conn = ScriptedConn()
    out = await lifecycle.run_lifecycle(conn, mode="dry_run", today=date(2026, 11, 5), b2c_open=b2c_open)
    assert "OR tc.channel = 'b2b'" in lifecycle.CANDIDATES_SQL
    (args,) = conn.sql("FROM booking.trip_cases tc WHERE tc.deleted_at IS NULL AND tc.status = ANY")
    assert args[-1] is b2c_open
    assert out["b2c"] == ("open" if b2c_open else "frozen")


@pytest.mark.asyncio
@pytest.mark.parametrize("b2c_open", [False, True])
async def test_reminders_leave_b2c_cases_and_customer_invoices_alone(
    monkeypatch: pytest.MonkeyPatch, b2c_open: bool
) -> None:
    async def rules(_: Any) -> dict[str, int]:
        return {"supplier_answer_days": 2, "trip_check_reminder_days": 3, "supplier_bill_due_soon_days": 5}

    async def tz(_: Any) -> str:
        return "UTC"

    payments_ran: list[bool] = []

    async def payment_reminders(_: Any, __: int) -> dict[str, int]:
        payments_ran.append(True)
        return dict(lifecycle.NO_PAYMENT_REMINDERS, payments_notified=1)

    monkeypatch.setattr(lifecycle, "policy", rules)
    monkeypatch.setattr(lifecycle, "ops_timezone", tz)
    monkeypatch.setattr(lifecycle, "_payment_reminders", payment_reminders)
    conn = ScriptedConn()
    out = await lifecycle.run_reminders(conn, today=date(2026, 11, 5), b2c_open=b2c_open)
    (waiting,) = conn.sql("FROM booking.supplier_bookings sb")
    (checks,) = conn.sql("FROM booking.trip_case_checks k")
    assert waiting[-1] is b2c_open and checks[-1] is b2c_open
    assert payments_ran == ([True] if b2c_open else [])
    assert out["payments_notified"] == (1 if b2c_open else 0)
    assert out["b2c"] == ("open" if b2c_open else "frozen")


@pytest.mark.asyncio
async def test_handlers_pass_the_switch_to_the_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []

    async def fake_run(_: Any, **kwargs: Any) -> dict[str, Any]:
        seen.append(kwargs["b2c_open"])
        return {"moves": {}, "blocked": {}, "refused": 0, "cleared_checks": 0}

    async def fake_reminders(_: Any, **kwargs: Any) -> dict[str, Any]:
        seen.append(kwargs["b2c_open"])
        return {
            "supplier_waiting_notified": 0,
            "checks_due_notified": 0,
            "payments_notified": 0,
            "customer_rows": 0,
            "bills_notified": 0,
        }

    monkeypatch.setattr(lifecycle, "run_lifecycle", fake_run)
    monkeypatch.setattr(lifecycle, "run_reminders", fake_reminders)
    job = type("J", (), {"payload": {"trigger": "manual"}})()
    handlers = lifecycle.build_handlers(False)
    await handlers[lifecycle.KIND](ScriptedConn(), job)
    await handlers[lifecycle.REMINDERS_KIND](ScriptedConn(), job)
    assert seen == [False, False]


# Routers whose every write touches a B2C record or a trip case (a b2c case among them): each write route must declare
# the 410, so a new write route there without a guard fails here (the guard itself is tested per route above).
GUARDED_PREFIXES = (
    "/booking/invoices",
    "/booking/payments",
    "/booking/customers",
    "/booking/trip-cases",
    "/booking/trip-checks",
    "/booking/supplier-bookings",
    "/booking/ops-files",
)


def test_every_write_on_b2c_records_and_trip_cases_documents_the_410() -> None:
    spec = create_app(Settings(database_url=None, environment="test")).openapi()
    writes = [
        (method.upper(), path, op)
        for path, ops in spec["paths"].items()
        if path.startswith(GUARDED_PREFIXES) or path.endswith("/trip-plans/{source_id}/trip-cases")
        for method, op in ops.items()
        if method in {"post", "patch", "put", "delete"}
    ]
    assert len(writes) >= len(B2C_ONLY) + len(CASE_ROUTES) - 1
    missing = [(m, p) for m, p, op in writes if "410" not in op.get("responses", {})]
    assert missing == []
