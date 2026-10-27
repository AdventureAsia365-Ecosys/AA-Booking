"""AA-682 S5: My Payments on ``/booking/customer/v1`` without a database — the EXTERNAL verifier (route class
EXTERNAL x {tripplanner, booking-customer}), the settings, the scope SQL, the field allow-list and the 404 rules,
driven through :class:`ScriptedConn`. The scope itself (another customer's invoice, a draft, a B2B case) runs on a
real PostgreSQL in tests/integration/test_bk028_my_payments_api.py."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.auth import customer, identity_config
from app.auth.identity_verifier import AuthUnavailable, InvalidToken
from app.main import create_app
from app.ops import my_payments
from app.settings import Settings, SettingsError, load_settings
from tests.staff_tokens import (
    BOOKING_CUSTOMER_CLIENT,
    BOOKING_STAFF_CLIENT,
    CIS_PORTAL_CLIENT,
    CRM_CLIENT,
    EXTERNAL_ISSUER,
    TRAVELLER_SUB,
    TRIPPLANNER_CLIENT,
    LocalCis,
    LocalExternal,
    LocalM2M,
)
from tests.unit.fakes import ScriptedConn
from tests.unit.test_pricing_api import ScriptedDatabase

CUSTOMER_ID = "00000000-0000-4000-8000-00000000c001"
INV_ID = "00000000-0000-4000-8000-0000000d0001"
IDENTITY = "FROM booking.customer_identities ci"
LIST = "ORDER BY i.issued_at DESC"
ONE = "AND i.invoice_number = $2"
SCHEDULE = "FROM booking.v_payment_schedule_status s"
PAYMENTS = "FROM booking.payments p"
INSTRUCTIONS = "WHERE key = 'payment_instructions'"

# Keys that must never appear in any customer answer (design §5 / §8: staff names, evidence, net, supplier, internal
# notes, bank references, reasons, bill-to snapshot, idempotency data).
FORBIDDEN = re.compile(
    r"(^|_)(net|supplier|suppliers|evidence|note|notes|reference|reason|recorded|verified|voided|sent_by|created_by"
    r"|updated_by|staff|bill_to|email|phone|name|idempotency|request_sha256|margin|cost|customer_id|invoice_id"
    r"|trip_case_id|file_id)(_|$)|^id$"
)


def invoice_row(**over: Any) -> dict[str, Any]:
    return {
        "id": INV_ID,
        "invoice_number": "INV-000003",
        "status": "partially_paid",
        "document_type": "proforma",
        "currency": "USD",
        "total_amount": "2400.00",
        "paid_amount": "720.00",
        "balance": "1680.00",
        "next_due_date": date(2026, 12, 1),
        "overdue_amount": "0.00",
        "issued_at": datetime(2026, 10, 27, 9, 30, tzinfo=UTC),
        "trip_code": "TC-000022",
        "trip_start_date": date(2027, 1, 10),
        "trip_end_date": date(2027, 1, 14),
        "planner_trip_id": "tp-trip-1",
        **over,
    }


SCHEDULE_ROWS = [
    {"invoice_id": INV_ID, "seq": 1, "kind": "deposit", "amount": "720.00", "due_date": date(2026, 10, 30),
     "remaining_amount": "0.00", "state": "paid"},
    {"invoice_id": INV_ID, "seq": 2, "kind": "final", "amount": "1680.00", "due_date": date(2026, 12, 1),
     "remaining_amount": "1680.00", "state": "due"},
]  # fmt: skip
PAYMENT_ROWS = [
    {"invoice_id": INV_ID, "ref": "PMT-000007", "kind": "receipt", "amount": "720.00", "currency": "USD",
     "method": "bank_transfer", "received_on": date(2026, 10, 28)},
]  # fmt: skip


def scripted(*, linked: bool = True, rows: list[dict[str, Any]] | None = None) -> ScriptedConn:
    conn = ScriptedConn()
    conn.on(IDENTITY, CUSTOMER_ID if linked else None)
    conn.on(LIST, rows if rows is not None else [invoice_row()])
    conn.on(ONE, (rows or [invoice_row()])[0] if rows != [] else None)
    conn.on(SCHEDULE, SCHEDULE_ROWS)
    conn.on(PAYMENTS, PAYMENT_ROWS)
    conn.on(INSTRUCTIONS, '"Bank transfer to the account on your payment request."')
    return conn


def client(conn: ScriptedConn, ext: LocalExternal | None = None) -> TestClient:
    ext = ext or LocalExternal()
    cis = LocalCis()
    app = create_app(ext.settings(), ScriptedDatabase(conn), cis.verifier(), customer_verifier=ext.verifier())
    return TestClient(app, raise_server_exceptions=False)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def keys_of(node: Any, out: set[str] | None = None) -> set[str]:
    out = set() if out is None else out
    if isinstance(node, dict):
        for k, v in node.items():
            out.add(k)
            keys_of(v, out)
    elif isinstance(node, list):
        for v in node:
            keys_of(v, out)
    return out


# ── Settings ─────────────────────────────────────────────────────────────────


def test_settings_default_to_the_dev_traveller_clients() -> None:
    s = load_settings({})
    assert s.cognito_tripplanner_client_id == identity_config.DEV_TRIPPLANNER_CLIENT_ID
    assert s.cognito_booking_customer_client_id == identity_config.DEV_BOOKING_CUSTOMER_CLIENT_ID
    cfg = customer.customer_config(s)
    assert cfg.issuer == identity_config.DEV_EXTERNAL_ISSUER and cfg.name == customer.EXTERNAL
    assert cfg.client_ids == frozenset(
        {identity_config.DEV_TRIPPLANNER_CLIENT_ID, identity_config.DEV_BOOKING_CUSTOMER_CLIENT_ID}
    )
    assert (cfg.audience_claim, cfg.token_use) == ("client_id", "access")
    # Never a staff client.
    assert not cfg.client_ids & {s.cognito_booking_staff_client_id, s.cognito_breakglass_client_id}


def test_settings_env_overrides() -> None:
    s = load_settings(
        {"COGNITO_TRIPPLANNER_CLIENT_ID": f" {TRIPPLANNER_CLIENT} ", "COGNITO_BOOKING_CUSTOMER_CLIENT_ID": "abc123"}
    )
    assert (s.cognito_tripplanner_client_id, s.cognito_booking_customer_client_id) == (TRIPPLANNER_CLIENT, "abc123")


@pytest.mark.parametrize(
    "overrides",
    [
        {"cognito_tripplanner_client_id": "bad id!"},
        {"cognito_booking_customer_client_id": ""},
        {"cognito_tripplanner_client_id": BOOKING_STAFF_CLIENT,
         "cognito_booking_staff_client_id": BOOKING_STAFF_CLIENT},
        {"cognito_booking_customer_client_id": CRM_CLIENT, "cognito_m2m_issuer": "https://m2m.test/pool",
         "cognito_m2m_crm_client_id": CRM_CLIENT},
    ],
)  # fmt: skip
def test_settings_refuse_bad_or_shared_traveller_clients(overrides: dict[str, Any]) -> None:
    with pytest.raises(SettingsError):
        Settings(database_url=None, environment="test", **overrides)


# ── Verifier ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("client_id", [TRIPPLANNER_CLIENT, BOOKING_CUSTOMER_CLIENT])
async def test_both_traveller_clients_are_accepted(client_id: str) -> None:
    ext = LocalExternal()
    who = await customer.verify_traveller(ext.verifier(), ext.token(client_id=client_id))
    assert (who.iss, who.sub, who.client_id) == (EXTERNAL_ISSUER, TRAVELLER_SUB, client_id)


@pytest.mark.asyncio
async def test_other_tokens_are_refused() -> None:
    ext = LocalExternal()
    verifier = ext.verifier()
    refused = [
        ext.token(client_id=CIS_PORTAL_CLIENT),  # another client of the same pool
        ext.token(token_use="id"),  # an ID token
        ext.token(exp_in=-120),  # expired
        ext.token(sub=TRIPPLANNER_CLIENT),  # client-credentials shape (sub = client id)
        LocalCis().workforce.token(),  # a staff WORKFORCE token
        LocalCis().token(),  # the CIS bridge token
        LocalM2M().token(),  # an M2M service token
        "not-a-jwt",
    ]
    for token in refused:
        with pytest.raises(InvalidToken):
            await customer.verify_traveller(verifier, token)


@pytest.mark.asyncio
async def test_unloadable_keys_are_unavailable_not_invalid() -> None:
    ext = LocalExternal()
    ext.fail = True
    with pytest.raises(AuthUnavailable):
        await customer.verify_traveller(ext.verifier(), ext.token())


def test_routes_answer_401_without_an_accepted_token() -> None:
    conn = scripted()
    c = client(conn)
    for headers in (
        {},
        bearer(LocalCis().workforce.token(groups=["booking-admin"])),
        bearer(LocalM2M().token()),
        bearer(LocalExternal().token()),  # signed by another key: not this pool's
        {"Authorization": "Basic abc"},
    ):
        for path in ("/booking/customer/v1/payments", "/booking/customer/v1/invoices/INV-000003"):
            r = c.get(path, headers=headers)
            assert r.status_code == 401, (path, r.text)
            assert r.json() == {"detail": "sign_in_required"}
            assert r.headers["cache-control"] == "no-store"
            assert r.headers["www-authenticate"] == "Bearer"
    assert conn.calls == []  # nothing was read before authentication


def test_routes_answer_503_while_the_pool_keys_cannot_load() -> None:
    ext = LocalExternal()
    ext.fail = True
    r = client(scripted(), ext).get("/booking/customer/v1/payments", headers=bearer(ext.token()))
    assert r.status_code == 503 and r.json()["detail"] == "auth_unavailable"
    assert r.headers["cache-control"] == "no-store"


# ── Reads ────────────────────────────────────────────────────────────────────


def test_list_maps_the_identity_and_returns_the_allow_list_only() -> None:
    ext, conn = LocalExternal(), scripted()
    r = client(conn, ext).get("/booking/customer/v1/payments", headers=bearer(ext.token()))
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    assert conn.sql(IDENTITY) == [(EXTERNAL_ISSUER, TRAVELLER_SUB)]
    assert conn.sql(LIST) == [(CUSTOMER_ID, my_payments.MAX_INVOICES)]
    (inv,) = r.json()["invoices"]
    assert tuple(inv) == my_payments.INVOICE_KEYS
    assert inv["trip"] == {"code": "TC-000022", "planner_trip_id": "tp-trip-1", "start_date": "2027-01-10",
                           "end_date": "2027-01-14"}  # fmt: skip
    assert [(s["seq"], s["state"], s["remaining_amount"]) for s in inv["schedule"]] == [
        (1, "paid", "0.00"),
        (2, "due_soon", "1680.00"),
    ]
    assert inv["payments"] == [{"ref": "PMT-000007", "kind": "receipt", "amount": "720.00", "currency": "USD",
                                "method": "bank_transfer", "received_on": "2026-10-28"}]  # fmt: skip
    assert inv["payment_instructions"].startswith("Bank transfer")
    assert (inv["paid_amount"], inv["balance"], inv["next_due_date"]) == ("720.00", "1680.00", "2026-12-01")
    bad = sorted(k for k in keys_of(r.json()) if FORBIDDEN.search(k))
    assert bad == [], bad


def test_unlinked_identity_sees_nothing() -> None:
    ext, conn = LocalExternal(), scripted(linked=False)
    c = client(conn, ext)
    r = c.get("/booking/customer/v1/payments", headers=bearer(ext.token()))
    assert r.status_code == 200 and r.json() == {"invoices": []}
    assert conn.sql(LIST) == []
    r = c.get("/booking/customer/v1/invoices/INV-000003", headers=bearer(ext.token()))
    assert r.status_code == 404 and r.json()["detail"] == "invoice_not_found"
    assert conn.sql(ONE) == []


def test_invoice_by_number_is_scoped_and_normalised() -> None:
    ext, conn = LocalExternal(), scripted()
    r = client(conn, ext).get("/booking/customer/v1/invoices/inv-000003", headers=bearer(ext.token()))
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    assert conn.sql(ONE) == [(CUSTOMER_ID, "INV-000003")]
    assert tuple(r.json()) == my_payments.INVOICE_KEYS


@pytest.mark.parametrize(
    "number",
    ["INV-000099", "TC-000022", "00000000-0000-4000-8000-0000000d0001", "INV-1", "INV-" + "0" * 70, "INV-000003%00"],
)
def test_anything_not_visible_is_404(number: str) -> None:
    ext, conn = LocalExternal(), scripted(rows=[])
    r = client(conn, ext).get(f"/booking/customer/v1/invoices/{number}", headers=bearer(ext.token()))
    assert r.status_code == 404 and r.json() == {"detail": "invoice_not_found", "message": "invoice not found"}
    assert r.headers["cache-control"] == "no-store"
    if number != "INV-000099":
        assert conn.sql(ONE) == [] and conn.sql(IDENTITY) == []  # malformed: no read at all


def test_paid_and_cancelled_requests_hide_the_bank_details() -> None:
    ext = LocalExternal()
    rows = [
        invoice_row(status="paid", paid_amount="2400.00", balance="0.00", next_due_date=None),
        invoice_row(id="00000000-0000-4000-8000-0000000d0002", invoice_number="INV-000004", status="cancelled",
                    paid_amount="0.00", balance=None, next_due_date=date(2026, 11, 1), planner_trip_id=None),
    ]  # fmt: skip
    conn = scripted(rows=rows)
    conn.on(SCHEDULE, [*SCHEDULE_ROWS, {**SCHEDULE_ROWS[1], "invoice_id": rows[1]["id"], "state": "cancelled"}])
    r = client(conn, ext).get("/booking/customer/v1/payments", headers=bearer(ext.token()))
    paid, cancelled = r.json()["invoices"]
    assert paid["payment_instructions"] is None and cancelled["payment_instructions"] is None
    assert cancelled["balance"] is None and cancelled["next_due_date"] is None
    assert cancelled["trip"]["planner_trip_id"] is None
    assert [(s["state"], s["remaining_amount"]) for s in cancelled["schedule"]] == [("cancelled", "0.00")]


def test_instructions_must_be_a_text_setting() -> None:
    assert my_payments._text('"Pay by bank transfer"') == "Pay by bank transfer"
    assert my_payments._text('{"iban": "x"}') is None
    assert my_payments._text('"   "') is None
    assert my_payments._text("not json") is None
    assert my_payments._text(None) is None
    assert len(my_payments._text('"' + "x" * 5000 + '"') or "") == my_payments.MAX_INSTRUCTIONS


def test_scope_sql_pins_the_rules() -> None:
    scope = " ".join(my_payments.OWN_INVOICES.split())
    for rule in (
        "tc.customer_id = $1::uuid",
        "tc.channel = 'b2c'",
        "tc.deleted_at IS NULL",
        "i.bill_to_kind = 'customer'",
        "i.deleted_at IS NULL",
        "i.status <> 'draft'",
    ):
        assert rule in scope, rule
    assert "c.deleted_at IS NULL" in my_payments.CUSTOMER_SQL
    assert "ci.iss = $1 AND ci.sub = $2" in my_payments.CUSTOMER_SQL
    assert "p.verified_at IS NOT NULL AND p.voided_at IS NULL" in " ".join(my_payments.PAYMENTS_SQL.split())
    for sql in (my_payments.INVOICE_COLUMNS, my_payments.SCHEDULE_SQL, my_payments.PAYMENTS_SQL):
        for column in (
            "sent_by",
            "recorded_by",
            "verified_by",
            "evidence",
            "reference",
            "note",
            "bill_to",
            "net_amount",
        ):
            assert column not in sql, (column, sql)


def test_openapi_customer_schemas_carry_no_forbidden_field() -> None:
    app = create_app(LocalExternal().settings(), ScriptedDatabase(ScriptedConn()), LocalCis().verifier())
    spec = app.openapi()
    paths = {p: v for p, v in spec["paths"].items() if p.startswith("/booking/customer/v1/")}
    assert set(paths) == {"/booking/customer/v1/payments", "/booking/customer/v1/invoices/{number}"}
    assert {op["operationId"] for p in paths.values() for op in p.values()} == {"listMyPayments", "getMyInvoice"}
    schemas = spec["components"]["schemas"]
    names, keys = ["Payments", "Invoice"], set()
    seen: set[str] = set()
    while names:
        name = names.pop()
        if name in seen:
            continue
        seen.add(name)
        keys |= set(schemas[name].get("properties", {}))
        names += re.findall(r"#/components/schemas/(\w+)", str(schemas[name]))
    assert seen == {"Payments", "Invoice", "TripRef", "ScheduleRow", "Payment"}
    assert sorted(k for k in keys if FORBIDDEN.search(k)) == []
