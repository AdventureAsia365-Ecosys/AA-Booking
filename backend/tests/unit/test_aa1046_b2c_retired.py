"""AA-1046 (root ADR 0019 d8; AA-1034 R0 and AA-1037 before it): the B2C half of Booking is retired.

- The B2C-only routes are gone (customers, customer invoices and payments, the billing summary, the customer routes
  ``/booking/customer/v1``): 404 / 405, nothing reached.
- A staff-made case (``POST /booking/trip-cases``) or a new case from a trip plan answers 410 ``b2b_only`` before any
  SQL: such a case was always a B2C case for a traveller customer.
- Routes shared with B2B cases read only the case's ``channel``: a ``b2c`` case (kept history) answers 410, a ``b2b``
  case (or an unknown ref, left to the service's own 404) reaches the service. There is no switch any more.
- The lifecycle and reminder jobs leave b2c cases alone and send no customer payment reminder; the B2C job kinds are
  retired (no handler, a leftover queued row is cancelled).
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
from app.main import create_app
from app.ops import files as ops_files
from app.ops import lifecycle, supplier_bookings, trip_checks
from app.settings import Settings
from app.trip_cases import service, versions
from tests.unit.fakes import ScriptedConn

ADMIN = Staff("u-1", None, "Admin One", "admin", True, "admin")
BOOKING_ID = "11111111-1111-4111-8111-111111111111"
CHECK_ID = "22222222-2222-4222-8222-222222222222"
FILE_ID = "33333333-3333-4333-8333-333333333333"
PAYMENT_ID = "44444444-4444-4444-8444-444444444444"
PLAN = "66666666-6666-4666-8666-666666666666"

CASE_SQL = "SELECT channel FROM booking.trip_cases"
BOOKING_SQL = "FROM booking.supplier_bookings sb JOIN booking.trip_cases"
CHECK_SQL = "FROM booking.trip_case_checks k JOIN booking.trip_cases"
FILE_SQL = "FROM booking.ops_files f LEFT JOIN booking.trip_cases"

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
    (service, "update_case", {"ok": True}),
    (service, "change_status", {"ok": True}),
    (service, "assign", {"ok": True}),
    (versions, "create_version", {"ok": True}),
    (versions, "update_version", {"ok": True}),
    (versions, "delete_version", None),
    (versions, "replace_items", {"ok": True}),
    (versions, "price_version", {"ok": True}),
    (versions, "approve_version", {"ok": True}),
    (trip_plans, "use_for_trip_case", {"mode": "new_version"}),
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

# The retired B2C-only routes (method, path, body): gone from the app.
REMOVED = [
    ("GET", "/booking/customers", None),
    ("POST", "/booking/customers", {"name": "Invented Lead", "email": "lead@example.test"}),
    ("GET", "/booking/customers/CUS-000001", None),
    ("PATCH", "/booking/customers/CUS-000001", {"name": "Redacted"}),
    ("DELETE", "/booking/customers/CUS-000001", None),
    ("GET", "/booking/invoices", None),
    ("GET", "/booking/invoices/INV-000001", None),
    ("POST", "/booking/invoices/INV-000001/send", None),
    ("POST", "/booking/invoices/INV-000001/payments", {"amount": "100"}),
    ("GET", "/booking/payments", None),
    ("GET", "/booking/payments/PMT-000001", None),
    ("POST", "/booking/payments/PMT-000001/verify", None),
    ("GET", "/booking/billing/summary", None),
    ("GET", "/booking/trip-cases/TC-000001/billing", None),
    ("POST", "/booking/trip-cases/TC-000001/invoices", {}),
    ("POST", "/booking/trip-cases/TC-000001/versions/1/send", {"subject": "Your trip", "message": "Hello"}),
    ("GET", "/booking/customer/v1/payments", None),
    ("GET", "/booking/customer/v1/invoices/INV-000001", None),
]

# Routes that would start a new (B2C) case: refused before any read.
NEW_CASE = [
    ("POST", "/booking/trip-cases", {"customer_id": PAYMENT_ID}),
    ("POST", "/booking/trip-cases", None),
    ("POST", f"/booking/catalog/trip-plans/{PLAN}/trip-cases", {}),
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


def client(conn: ScriptedConn | None = None) -> TestClient:
    app = create_app(Settings(database_url=None, environment="test"))
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


@pytest.mark.parametrize(("method", "path", "body"), REMOVED)
def test_retired_b2c_routes_are_gone(stubs: dict[str, Recorder], method: str, path: str, body: Any) -> None:
    conn = ScriptedConn()
    r = call(client(conn), method, path, body)
    assert r.status_code in (404, 405), (method, path, r.status_code)
    assert called(stubs) == [] and conn.calls == []


@pytest.mark.parametrize(("method", "path", "body"), NEW_CASE)
def test_a_new_staff_made_case_answers_410_before_any_read(
    stubs: dict[str, Recorder], method: str, path: str, body: Any
) -> None:
    conn = ScriptedConn()
    assert_gone(call(client(conn), method, path, body))
    assert called(stubs) == []
    assert conn.calls == []  # refused before any SQL


def test_the_retired_customer_routes_are_not_in_the_openapi_document() -> None:
    paths = create_app(Settings(database_url=None, environment="test")).openapi()["paths"]
    assert not [p for p in paths if p.startswith(("/booking/customer", "/booking/invoices", "/booking/payments"))]
    assert "/booking/billing/summary" not in paths
    assert "/booking/trip-cases/{case_ref}/versions/{version_number}/send" not in paths


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


# ── Jobs ─────────────────────────────────────────────────────────────────────


def test_lifecycle_handlers_run_b2b_only() -> None:
    handlers = lifecycle.build_handlers()
    assert handlers == {
        lifecycle.KIND: lifecycle.handle_lifecycle,
        lifecycle.REMINDERS_KIND: lifecycle.handle_reminders,
    }


def test_the_b2c_kinds_are_retired() -> None:
    assert b2c.RETIRED_KINDS == {"bk_inquiry_pull", "bk_inquiry_process", "bk_notify_email"}


@pytest.mark.asyncio
async def test_lifecycle_candidates_skip_b2c_cases(monkeypatch: pytest.MonkeyPatch) -> None:
    async def rules(_: Any) -> dict[str, int]:
        return {"pre_departure_days": 7, "emergency_contact_keep_days": 90}

    async def tz(_: Any) -> str:
        return "UTC"

    async def no_clear(*_: Any, **__: Any) -> dict[str, int]:
        return {"cleared_checks": 0, "cleared_cases": 0}

    monkeypatch.setattr(lifecycle, "policy", rules)
    monkeypatch.setattr(lifecycle, "ops_timezone", tz)

    async def today(*_: Any) -> date:
        return date(2026, 11, 5)

    monkeypatch.setattr(lifecycle, "_clear", no_clear)
    monkeypatch.setattr(lifecycle, "ops_today", today)
    conn = ScriptedConn()
    job = type("J", (), {"payload": {"mode": "dry_run"}})()
    out = await lifecycle.handle_lifecycle(conn, job)
    (args,) = conn.sql("FROM booking.trip_cases tc WHERE tc.deleted_at IS NULL AND tc.status = ANY")
    assert args[-1] is False
    assert out["b2c"] == "frozen"


@pytest.mark.asyncio
async def test_reminders_leave_b2c_cases_alone_and_send_no_customer_reminder(monkeypatch: pytest.MonkeyPatch) -> None:
    async def rules(_: Any) -> dict[str, int]:
        return {"supplier_answer_days": 2, "trip_check_reminder_days": 3, "supplier_bill_due_soon_days": 5}

    async def tz(_: Any) -> str:
        return "UTC"

    monkeypatch.setattr(lifecycle, "policy", rules)
    monkeypatch.setattr(lifecycle, "ops_timezone", tz)
    conn = ScriptedConn()
    out = await lifecycle.run_reminders(conn, today=date(2026, 11, 5))
    (waiting,) = conn.sql("FROM booking.supplier_bookings sb")
    (checks,) = conn.sql("FROM booking.trip_case_checks k")
    assert waiting[-1] is False and checks[-1] is False
    assert not [c for c in conn.calls if "invoice" in str(c[0]) or "payment_schedule" in str(c[0])]
    assert "payments_notified" not in out and "customer_rows" not in out
    assert out["b2c"] == "frozen"


# Routers whose every write touches a trip case (a b2c case among them): each write route must declare the 410, so a
# new write route there without a guard fails here (the guard itself is tested per route above).
GUARDED_PREFIXES = (
    "/booking/trip-cases",
    "/booking/trip-checks",
    "/booking/supplier-bookings",
    "/booking/ops-files",
)


def test_every_write_on_trip_cases_documents_the_410() -> None:
    spec = create_app(Settings(database_url=None, environment="test")).openapi()
    writes = [
        (method.upper(), path, op)
        for path, ops in spec["paths"].items()
        if path.startswith(GUARDED_PREFIXES) or path.endswith("/trip-plans/{source_id}/trip-cases")
        for method, op in ops.items()
        if method in {"post", "patch", "put", "delete"}
    ]
    assert len(writes) >= len(CASE_ROUTES)
    missing = [(m, p) for m, p, op in writes if "410" not in op.get("responses", {})]
    assert missing == []
