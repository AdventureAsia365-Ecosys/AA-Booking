"""AA-1037 (booking.b2b.v1 1.5): the agency's own AA invoices and billing status through the B2B API, with a scripted
connection — partner scoping in the SQL arguments, drafts never asked for, the field allow-list (no staff note, void
reason, staff names, bank reference, document or bill-to in the JSON) and the Problem errors.

Real SQL (partner A cannot read partner B's invoice, a draft is 404) is in
tests/integration/test_aa1037_b2b_invoices.py. Names and places are invented."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.agency_billing import invoices as staff_invoices
from app.b2b import invoices
from app.main import create_app
from tests.staff_tokens import OTHER_M2M_CLIENT, LocalCis, LocalM2M
from tests.unit.fakes import ScriptedConn
from tests.unit.test_b2b_api import PARTNER, TENANT
from tests.unit.test_pricing_api import ScriptedDatabase

INV = "41000000-0000-4000-8000-000000000001"
HOLD = "41000000-0000-4000-8000-000000000002"
CASE = "41000000-0000-4000-8000-000000000003"
OTHER_TENANT = "41000000-0000-4000-8000-0000000000bb"
NUMBER = "AAT-2026-000007"
SECRET_WORDS = ("Ask the owner again", "wrong departure", "fin-maker", "fin-checker", "TRX-5521", "statement line 9",
                "accounts@agency.example", "Agency One Billing Co")  # fmt: skip

STAFF_ROW: dict[str, Any] = {
    "id": INV,
    "number": NUMBER,
    "party_tenant_id": TENANT,
    "invoice_kind": "settlement",
    "status": "issued",
    "period_start": date(2026, 10, 1),
    "period_end": date(2026, 11, 1),  # exclusive
    "currency": "USD",
    "subtotal": Decimal("700.00"),
    "tax_rule_id": None,
    "tax_rate": Decimal("0.0000"),
    "tax_amount": Decimal("0.00"),
    "total": Decimal("700.00"),
    "billing_entity_id": "41000000-0000-4000-8000-000000000009",
    "issuer": {
        "code": "LBT",
        "legal_entity": "Lantern Bay Travel Ltd",
        "tax_code": "0100000001",
        "registered_office": "3 Paper Lane, Old Quarter",
        "country_iso2": "VN",
        "invoice_inbox": "billing@lanternbay.example",
        "payment_instructions": "Transfer to the account on the contract.",
        "tax_line": "VAT 0%",
        "a_later_key": "stays inside Booking",
    },
    "bill_to": {
        "agency_name": "Agency One",
        "source": "cis",
        "legal_entity_name": "Agency One Billing Co",
        "billing_email": "accounts@agency.example",
    },  # fmt: skip
    "footer_wording": "Prices exclude tax.",
    "issued_on": date(2026, 11, 2),
    "due_date": date(2026, 11, 16),
    "issued_at": datetime(2026, 11, 2, 9, tzinfo=UTC),
    "issued_by": "fin-maker",
    "e_invoice_no": "C26TLB-00031",
    "e_invoice_recorded_by": "fin-maker",
    "e_invoice_recorded_at": datetime(2026, 11, 3, 9, tzinfo=UTC),
    "staff_note": "Ask the owner again",
    "voided_at": None,
    "voided_by": None,
    "void_reason": None,
    "source": "job",
    "created_by": "job:bk_settlement_draft",
    "created_at": datetime(2026, 11, 1, 1, tzinfo=UTC),
    "updated_at": datetime(2026, 11, 3, 9, tzinfo=UTC),
    "agency_name": "Agency One",
    "paid": Decimal("300.00"),
    "waiting": Decimal("100.00"),
    "balance": Decimal("400.00"),
    "overdue_days": 0,
}
LINE_ROWS = [
    {"line_no": 1, "line_role": "deposit", "product": None, "plan_key": None, "source_hold_id": HOLD,
     "description": "Deposit 30 % for the Misty Ridge loop", "quantity": Decimal("1.00"),
     "unit_price": Decimal("300.00"), "amount": Decimal("300.00"), "voided": False, "created_by": "fin-maker",
     "created_at": datetime(2026, 11, 1, tzinfo=UTC), "trip_case_code": "TC-000412", "booking_id": CASE},
    {"line_no": 2, "line_role": "adjustment", "product": None, "plan_key": None, "source_hold_id": None,
     "description": "Goodwill credit", "quantity": Decimal("1.00"), "unit_price": Decimal("-20.00"),
     "amount": Decimal("-20.00"), "voided": False, "created_by": "fin-maker",
     "created_at": datetime(2026, 11, 1, tzinfo=UTC), "trip_case_code": None, "booking_id": None},
]  # fmt: skip
PAYMENT_ROWS = [{"received_on": date(2026, 11, 4), "amount": Decimal("300.00"), "currency": "USD"}]
STATUS_ROW = {
    "status": "warn",
    "oldest_overdue_days": 12,
    "overdue_invoices": 1,
    "overdue_amount": '{"USD": 400.00}',
    "paid_through": date(2026, 10, 31),
    "warn_after_days": 10,
    "suspend_after_days": 30,
}


def _conn(partner: Any = PARTNER) -> ScriptedConn:
    conn = ScriptedConn()
    conn.on("FROM booking.b2b_partners WHERE tenant_id", partner)
    return conn


def _client(conn: ScriptedConn, m2m: LocalM2M) -> TestClient:
    app = create_app(m2m.settings(), ScriptedDatabase(conn), LocalCis().verifier(), m2m_verifier=m2m.verifier())
    return TestClient(app, raise_server_exceptions=False)


def _headers(m2m: LocalM2M, tenant: str = TENANT, **token: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {m2m.token(**token)}", "X-AA-Tenant-Id": tenant}


def _invoice_conn(**over: Any) -> ScriptedConn:
    conn = _conn()
    conn.on("WHERE i.number = $1 AND i.party_tenant_id = $2::uuid", {**STAFF_ROW, **over})
    conn.on("FROM booking.agency_invoice_line l", LINE_ROWS)
    conn.on("FROM booking.agency_payment WHERE invoice_id = $1::uuid AND status = 'approved'", PAYMENT_ROWS)
    return conn


def _leaks(body: Any) -> list[str]:
    text = json.dumps(body, default=str)
    keys = ("staff_note", "void_reason", "_by", "reference", "document_ref", "bill_to", "waiting", "party_tenant_id",
            "agency_name", "\"id\"", "created_at", "source", "trip_case_code", "plan_key", "a_later_key")  # fmt: skip
    return [k for k in keys if k in text] + [w for w in SECRET_WORDS if w in text]


def test_detail_is_scoped_by_the_callers_tenant_and_allow_listed() -> None:
    m2m, conn = LocalM2M(), _invoice_conn()
    resp = _client(conn, m2m).get(f"/booking/b2b/v1/invoices/{NUMBER}", headers=_headers(m2m))
    assert resp.status_code == 200, resp.text
    assert resp.headers["cache-control"] == "no-store"
    body = resp.json()
    [args] = conn.sql("WHERE i.number = $1 AND i.party_tenant_id = $2::uuid")
    assert args == (NUMBER, TENANT, ["issued", "paid", "void"])  # the partner of the token, never a draft
    assert _leaks(body) == []
    assert body["number"] == NUMBER and body["status"] == "issued" and body["invoice_kind"] == "settlement"
    assert (body["period_start"], body["period_end"]) == ("2026-10-01", "2026-10-31")
    assert (body["total"], body["paid"], body["balance"], body["tax_rate"]) == ("700.00", "300.00", "400.00", "0.0000")
    assert body["e_invoice_no"] == "C26TLB-00031" and body["overdue_days"] == 0
    assert body["issuer"]["payment_instructions"] == "Transfer to the account on the contract."
    assert set(body["issuer"]) == set(invoices.ISSUER_KEYS)
    assert body["lines"][0] == {"line_no": 1, "line_role": "deposit", "description": LINE_ROWS[0]["description"],
                                "quantity": "1.00", "unit_price": "300.00", "amount": "300.00", "hold_id": HOLD,
                                "booking_id": CASE, "voided": False}  # fmt: skip
    assert body["lines"][1]["amount"] == "-20.00" and body["lines"][1]["hold_id"] is None
    assert body["payments"] == [{"received_on": "2026-11-04", "amount": "300.00", "currency": "USD"}]


def test_void_invoice_shows_no_reason() -> None:
    m2m = LocalM2M()
    voided = {"voided_at": datetime(2026, 11, 4, tzinfo=UTC), "voided_by": "fin-checker",
              "void_reason": "wrong departure"}  # fmt: skip
    conn = _invoice_conn(status="void", paid=Decimal(0), balance=Decimal(0), waiting=Decimal(0), **voided)
    body = _client(conn, m2m).get(f"/booking/b2b/v1/invoices/{NUMBER}", headers=_headers(m2m)).json()
    assert body["status"] == "void" and body["balance"] == "0.00" and _leaks(body) == []


@pytest.mark.parametrize("number", ["AAS-2026-000001", "INV-000123", "41000000-0000-4000-8000-000000000001", "x" * 64])
def test_not_found_for_other_numbers_without_or_with_a_read(number: str) -> None:
    m2m, conn = LocalM2M(), _conn()  # no invoice row answers: another agency's number, a draft id, junk
    resp = _client(conn, m2m).get(f"/booking/b2b/v1/invoices/{number}", headers=_headers(m2m))
    assert resp.status_code == 404 and resp.json() == {
        "error": "invoice_not_found",
        "message": "no invoice of this agency with this number",
    }
    reads = conn.sql("WHERE i.number = $1 AND i.party_tenant_id = $2::uuid")
    if staff_invoices.NUMBER_RE.fullmatch(number):
        assert reads == [(number, TENANT, ["issued", "paid", "void"])]
    else:
        assert reads == []  # not an invoice number: refused before any read (an id never finds an invoice here)


def test_list_is_scoped_paged_and_never_lists_drafts() -> None:
    m2m, conn = LocalM2M(), _conn()
    conn.on("SELECT count(*)", 1)
    conn.on("ORDER BY i.created_at DESC", [STAFF_ROW])
    client = _client(conn, m2m)
    resp = client.get("/booking/b2b/v1/invoices", params={"kind": "settlement", "status": "issued", "limit": 10,
                                                           "offset": 20}, headers=_headers(m2m))  # fmt: skip
    assert resp.status_code == 200, resp.text
    assert resp.headers["cache-control"] == "no-store"
    body = resp.json()
    assert (body["total"], body["limit"], body["offset"]) == (1, 10, 20)
    [item] = body["items"]
    assert "lines" not in item and "issuer" not in item and _leaks(body) == []
    assert item["number"] == NUMBER and item["period_end"] == "2026-10-31"
    [count] = conn.sql("SELECT count(*)")
    [page] = conn.sql("ORDER BY i.created_at DESC")
    assert count == ("settlement", "issued", ["issued", "paid", "void"], TENANT)
    assert page == (*count, 10, 20)

    conn.calls.clear()
    assert client.get("/booking/b2b/v1/invoices", headers=_headers(m2m)).status_code == 200
    [count] = conn.sql("SELECT count(*)")
    assert count == (["issued", "paid", "void"], TENANT)  # no status asked: still never a draft


@pytest.mark.parametrize(
    "params",
    [{"status": "draft"}, {"status": "overdue"}, {"status": "all"}, {"kind": "other"}, {"limit": 0},
     {"limit": 101}, {"offset": -1}, {"offset": 10_001}],
)  # fmt: skip
def test_list_refuses_other_filters(params: dict[str, Any]) -> None:
    m2m, conn = LocalM2M(), _conn()
    resp = _client(conn, m2m).get("/booking/b2b/v1/invoices", params=params, headers=_headers(m2m))
    assert resp.status_code == 422 and resp.json()["error"] == "invalid"
    assert conn.sql("FROM booking.agency_invoice") == []


def test_billing_status_of_the_caller() -> None:
    m2m, conn = LocalM2M(), _conn()
    conn.on("FROM booking.v_tenant_billing_status", STATUS_ROW)
    resp = _client(conn, m2m).get("/booking/b2b/v1/billing-status", headers=_headers(m2m))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "status": "warn",
        "holds_paused": False,
        "overdue_invoices": 1,
        "oldest_overdue_days": 12,
        "overdue_amount": [{"amount": "400.00", "currency": "USD"}],
        "warn_after_days": 10,
        "suspend_after_days": 30,
        "paid_through": "2026-10-31",
    }
    assert conn.sql("FROM booking.v_tenant_billing_status") == [(TENANT,)]


def test_billing_status_without_a_row_is_ok() -> None:
    m2m = LocalM2M()
    body = _client(_conn(), m2m).get("/booking/b2b/v1/billing-status", headers=_headers(m2m)).json()
    assert body["status"] == "ok" and body["paid_through"] is None and body["holds_paused"] is False


@pytest.mark.parametrize("path", ["/booking/b2b/v1/invoices", f"/booking/b2b/v1/invoices/{NUMBER}",
                                  "/booking/b2b/v1/billing-status"])  # fmt: skip
def test_auth_chain_before_any_invoice_read(path: str) -> None:
    m2m = LocalM2M()
    conn = _invoice_conn()
    client = _client(conn, m2m)
    assert client.get(path).json()["error"] == "unauthorized"
    assert client.get(path, headers=_headers(m2m, scope="booking.b2b/quote")).json()["error"] == "forbidden"
    assert client.get(path, headers=_headers(m2m, client_id=OTHER_M2M_CLIENT)).status_code == 403
    # A tenant with no active partner (or another agency's tenant on this client) learns nothing.
    unknown = _conn(partner=None)
    resp = _client(unknown, m2m).get(path, headers=_headers(m2m, tenant=OTHER_TENANT))
    assert resp.status_code == 403 and resp.json()["error"] == "unknown_partner"
    assert unknown.sql("FROM booking.agency_invoice") == [] and unknown.sql("v_tenant_billing_status") == []
    suspended = _conn(partner={**PARTNER, "status": "suspended"})
    assert _client(suspended, m2m).get(path, headers=_headers(m2m)).json()["error"] == "unknown_partner"
    assert all("agency_invoice" not in sql for sql, _ in conn.calls)


def test_staff_read_still_answers_every_status() -> None:
    """The partner filter is opt-in: the staff list keeps drafts and the overdue / waiting filters."""

    async def run() -> list[tuple[Any, ...]]:
        conn = ScriptedConn().on("SELECT count(*)", 0)
        await staff_invoices.list_invoices(conn, status="draft")
        await staff_invoices.list_invoices(conn, status="waiting")
        return conn.sql("SELECT count(*)")

    import asyncio

    assert asyncio.run(run()) == [("draft",), ()]


def test_summary_and_detail_shapes() -> None:
    out = invoices.detail({**STAFF_ROW, "lines": [], "payments": [], "issuer": None, "tax_rate": Decimal("0.07")})
    assert out["issuer"] is None and out["tax_rate"] == "0.0700" and out["lines"] == [] and _leaks(out) == []
    assert set(invoices.summary(STAFF_ROW)) == {
        "number", "invoice_kind", "status", "period_start", "period_end", "currency", "subtotal", "tax_amount",
        "total", "paid", "balance", "issued_on", "due_date", "overdue_days", "e_invoice_no",
    }  # fmt: skip
    assert invoices.amount(None) == "0.00" and invoices.amount(Decimal("1500000")) == "1500000.00"
