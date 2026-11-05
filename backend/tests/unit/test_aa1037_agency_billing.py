"""AA-1037 R4 slice 1 without a database: the agency billing rules (periods, SaaS and settlement lines, the schedule),
the service branches on a scripted connection (config, invoices, payments, the draft runs) and the routes (permissions,
validation, wiring). The same flows on PostgreSQL: tests/integration/test_aa1037_agency_billing.py and
test_bk041_agency_billing_schema.py. Names are invented."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.agency_billing import cis, config, drafts, invoices, payments
from app.auth.staff import PERMISSIONS, PREVIEW_PERMISSIONS, Staff, current_staff, get_pool
from app.catalog import liveness
from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.jobs.kinds import EXPECTED_SECONDS
from app.jobs.queue import Job
from app.main import create_app
from app.settings import Settings
from tests.unit.fakes import VIEW_READY_FRAGMENT, ScriptedConn

FIN = Staff("fin-1", None, "Finance One", "finance", True, None)
FIN2 = Staff("fin-2", None, "Finance Two", "finance", True, None)
SALES = Staff("u-1", None, "Sales One", "sales", True, "reviewer")
T1 = uuid.UUID("00000000-0000-4000-8000-000000000a01")
INV = "10000000-0000-4000-8000-000000000001"
PAY = "20000000-0000-4000-8000-000000000001"
TERMS = {"id": "t", "party_tenant_id": None, "deposit_pct": Decimal("0.3000"), "balance_days_before_departure": 30,
         "saas_due_days": 14, "settlement_due_days": 14, "warn_after_days": 1, "suspend_after_days": 30,
         "saas_currency": "USD"}  # fmt: skip
ENTITY = {"id": "e", "code": "LB", "legal_entity": "Lantern Bay Ltd", "tax_code": None, "registered_office": None,
          "country_iso2": "VN", "invoice_inbox": None, "payment_instructions": None, "is_default": True,
          "active": True}  # fmt: skip


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    liveness.reset_cache()


def inv_row(**over: Any) -> dict[str, Any]:
    row = {"id": INV, "number": None, "party_tenant_id": str(T1), "invoice_kind": "saas", "status": "draft",
           "period_start": date(2026, 11, 1), "currency": "USD", "total": Decimal("100.00"), "paid": Decimal(0),
           "waiting": Decimal(0), "agency_name": "Juniper Ridge", "e_invoice_no": None, "issuer": None,
           "bill_to": None}  # fmt: skip
    row.update(over)
    return row


def client(staff: Staff, conn: ScriptedConn) -> TestClient:
    app = create_app(Settings(database_url=None, environment="test"))
    app.dependency_overrides[current_staff] = lambda: staff
    app.dependency_overrides[get_pool] = lambda: conn
    return TestClient(app)


# ── Permissions and wiring ───────────────────────────────────────────────────


def test_permissions_are_admin_and_finance_only() -> None:
    for perm in ("agency_billing.read", "agency_billing.write", "agency_payment.approve", "billing_config.write"):
        assert PERMISSIONS[perm] == frozenset({"admin", "finance"})
        assert perm not in PREVIEW_PERMISSIONS
    assert FIN.can("agency_payment.approve") and not SALES.can("agency_billing.read")


def test_kinds_have_expected_seconds() -> None:
    assert set(drafts.HANDLERS) == {"bk_saas_invoice_draft", "bk_settlement_draft"}
    assert all(k in EXPECTED_SECONDS for k in drafts.HANDLERS)


def test_sales_gets_403_on_every_route() -> None:
    c = client(SALES, ScriptedConn())
    assert c.get("/booking/agency-invoices").status_code == 403
    assert c.get("/booking/billing-terms").status_code == 403
    assert c.post(f"/booking/agency-payments/{PAY}/approve").status_code == 403


# ── Periods ──────────────────────────────────────────────────────────────────


def test_periods() -> None:
    assert drafts.parse_period("2026-12", default=date(2026, 1, 5)) == (date(2026, 12, 1), date(2027, 1, 1))
    assert drafts.parse_period(None, default=date(2026, 2, 17)) == (date(2026, 2, 1), date(2026, 3, 1))
    assert drafts.previous_month(date(2026, 1, 3)) == date(2025, 12, 1)
    assert drafts.period_label(date(2026, 11, 1)) == "November 2026"
    for bad in ("2026-13", "26-11", "x", "1999-01"):
        with pytest.raises(Invalid):
            drafts.parse_period(bad, default=date(2026, 1, 1))


# ── SaaS lines ───────────────────────────────────────────────────────────────


def sub(product: str, plan: str, price: str | None, **over: Any) -> dict[str, Any]:
    row = {"tenant_id": T1, "product": product, "plan_key": plan, "plan_name": plan.split(".")[1].title(),
           "price_usd_monthly": Decimal(price) if price else None, "price_unit": "tenant", "seats": None,
           "seats_included": None, "price_extra_seat_usd": None, "kind": "demo", "billing_country": "TH"}  # fmt: skip
    row.update(over)
    return row


def test_saas_lines() -> None:
    lines, missing = drafts.saas_lines(
        [
            sub("content", "content.growth", "799"),
            sub("crm", "crm.team", "30", price_unit="seat", seats=3),
            sub("tripplanner", "tripplanner.site", "99", seats=5, seats_included=2, price_extra_seat_usd=Decimal(9)),
            sub("content", "content.enterprise", None),
        ],
        date(2026, 11, 1),
    )
    assert missing == 1
    assert [(ln["line_role"], ln["quantity"], ln["unit_price"]) for ln in lines] == [
        ("subscription", 1, Decimal(799)),
        ("subscription", 3, Decimal(30)),
        ("subscription", 1, Decimal(99)),
        ("extra_seats", 3, Decimal(9)),
        ("subscription", 1, Decimal(0)),
    ]
    assert lines[0]["description"] == "Growth (Content), November 2026"
    assert "contract price" in lines[-1]["description"]


# ── Settlement lines ─────────────────────────────────────────────────────────


def hold(**over: Any) -> drafts.Hold:
    base = {"hold_id": "h1", "tenant_id": T1, "trip_case_code": "TC-000101", "proposal_ref": "PR-9",
            "currency": "USD", "total": Decimal("1000.00"), "confirmed_on": date(2026, 9, 10),
            "departure": date(2026, 12, 20), "cancelled_on": None, "fee": None}  # fmt: skip
    base.update(over)
    return drafts.Hold(**base)


def roles(lines: list[dict[str, Any]]) -> list[tuple[str, Decimal]]:
    return [(ln["line_role"], ln["unit_price"]) for ln in lines]


def test_settlement_deposit_then_balance() -> None:
    oct1, nov1, dec1 = date(2026, 10, 1), date(2026, 11, 1), date(2026, 12, 1)
    assert roles(drafts.settlement_lines(hold(), {}, TERMS, oct1)) == [("deposit", Decimal("300.00"))]
    assert drafts.settlement_lines(hold(), {"deposit": Decimal(300)}, TERMS, nov1) == []  # balance due 11-20
    assert roles(drafts.settlement_lines(hold(), {"deposit": Decimal(300)}, TERMS, dec1)) == [
        ("balance", Decimal("700.00"))]  # fmt: skip
    assert drafts.settlement_lines(hold(), {"deposit": Decimal(300), "balance": Decimal(700)}, TERMS, dec1) == []
    line = drafts.settlement_lines(hold(), {}, TERMS, oct1)[0]
    assert line["description"] == "Deposit: TC-000101 (PR-9), departs 2026-12-20"


def test_settlement_full_late_unknown_and_not_yet() -> None:
    oct1 = date(2026, 10, 1)
    assert roles(drafts.settlement_lines(hold(departure=date(2026, 10, 5)), {}, TERMS, oct1)) == [
        ("full", Decimal("1000.00"))]  # fmt: skip
    assert roles(drafts.settlement_lines(hold(departure=None), {}, TERMS, oct1)) == [("full", Decimal("1000.00"))]
    assert drafts.settlement_lines(hold(confirmed_on=date(2026, 10, 2)), {}, TERMS, oct1) == []
    assert drafts.settlement_lines(hold(), {"full": Decimal(1000)}, TERMS, date(2027, 1, 1)) == []
    zero = {**TERMS, "deposit_pct": Decimal(0)}
    assert drafts.settlement_lines(hold(), {}, zero, oct1) == []
    assert roles(drafts.settlement_lines(hold(), {}, zero, date(2026, 12, 1))) == [("full", Decimal("1000.00"))]


def test_settlement_cancellation_and_voided_deposit() -> None:
    dec1 = date(2026, 12, 1)
    cancelled = hold(cancelled_on=date(2026, 10, 3), fee=Decimal("200.00"))
    assert roles(drafts.settlement_lines(cancelled, {"deposit": Decimal(300)}, TERMS, dec1)) == [
        ("cancellation_fee", Decimal("-100.00"))]  # fmt: skip
    assert roles(drafts.settlement_lines(cancelled, {}, TERMS, dec1)) == [("cancellation_fee", Decimal("200.00"))]
    assert drafts.settlement_lines(cancelled, {"cancellation_fee": Decimal(200)}, TERMS, dec1) == []
    assert drafts.settlement_lines(hold(cancelled_on=date(2026, 10, 3), fee=Decimal(0)), {}, TERMS, dec1) == []
    assert roles(drafts.settlement_lines(hold(), {"balance": Decimal(700)}, TERMS, dec1)) == [
        ("deposit", Decimal("300.00"))]  # fmt: skip


# ── Schedule and staff runs ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_schedule_queues_this_month_and_the_closed_month_once_a_day() -> None:
    conn = ScriptedConn([("INSERT INTO booking.job", "job-1")])
    hook = drafts.DraftSchedule(now=lambda: datetime(2026, 11, 5, 3, tzinfo=UTC))
    assert await hook(conn) == "job-1"
    keys = [args[2] for args in conn.sql("INSERT INTO booking.job")]
    assert keys == ["bk_saas_invoice_draft:2026-11", "bk_settlement_draft:2026-10"]
    assert await hook(conn) is None


@pytest.mark.asyncio
async def test_staff_run_payload() -> None:
    conn = ScriptedConn([("INSERT INTO booking.job", "job-9")])
    out = await drafts.enqueue_run(conn, "settlement", "2026-08", str(T1), "staff:fin-1")
    assert out == {"job_id": "job-9", "kind": "bk_settlement_draft", "trigger": "manual", "period": "2026-08",
                   "tenant_id": str(T1)}  # fmt: skip


# ── Draft runs on a scripted connection ──────────────────────────────────────


def saas_conn(*, ready: bool = True, live: Any = None, terms: Any = TERMS) -> ScriptedConn:
    c = ScriptedConn()
    c.on(VIEW_READY_FRAGMENT, ready)
    c.on("FROM catalog.v_tenant_billing_v1", [sub("content", "content.growth", "799")])
    c.on("SELECT tenant_id, agency_name FROM booking.b2b_partners", [{"tenant_id": T1, "agency_name": "Juniper"}])
    c.on("FROM booking.billing_terms", terms)
    c.on("status FROM booking.agency_invoice WHERE party_tenant_id", live)
    c.on("FROM booking.tax_rule", None)
    c.on("SELECT current_date", date(2026, 11, 5))
    c.on("INSERT INTO booking.agency_invoice (", INV)
    c.on("coalesce(max(line_no), 0) FROM", 0)
    c.on("SELECT tax_rate FROM", Decimal(0))
    c.on("coalesce(sum(amount), 0) FROM booking.agency_invoice_line", Decimal(799))
    return c


@pytest.mark.asyncio
async def test_run_saas_paths() -> None:
    assert (await drafts.run_saas(saas_conn(ready=False), period="2026-11", tenant_id=None, actor="j"))["outcome"] == (
        "skipped")  # fmt: skip
    liveness.reset_cache()
    out = await drafts.run_saas(saas_conn(), period="2026-11", tenant_id=None, actor="job:x")
    assert (out["invoices_created"], out["lines_added"]) == (1, 1)
    liveness.reset_cache()
    c = saas_conn(live={"id": INV, "status": "draft"})
    out = await drafts.run_saas(c, period="2026-11", tenant_id=None, actor="job:x")
    assert out["drafts_updated"] == 1 and c.sql("DELETE FROM booking.agency_invoice_line")
    liveness.reset_cache()
    out = await drafts.run_saas(saas_conn(live={"id": INV, "status": "issued"}), period="2026-11", tenant_id=None,
                                actor="job:x")  # fmt: skip
    assert out["period_issued"] == 1
    liveness.reset_cache()
    out = await drafts.run_saas(saas_conn(terms={**TERMS, "saas_currency": "EUR"}), period="2026-11",
                                tenant_id=None, actor="job:x")  # fmt: skip
    assert out["currency_unsupported"] == 1
    liveness.reset_cache()
    with pytest.raises(drafts.DraftFailed):
        await drafts.run_saas(saas_conn(terms=None), period="2026-11", tenant_id=None, actor="job:x")


@pytest.mark.asyncio
async def test_run_settlement_and_handlers() -> None:
    c = saas_conn()
    c.on("FROM booking.b2b_holds h", [{"hold_id": "h1", "tenant_id": T1, "trip_case_code": "TC-1",
                                       "proposal_ref": "PR-1", "currency": "USD", "total_b2b_price": Decimal(400),
                                       "confirmed_on": date(2026, 9, 3), "departure": date(2026, 9, 30),
                                       "cancelled_on": None, "fee_amount": None}])  # fmt: skip
    c.on("GROUP BY source_hold_id, line_role", [])
    job = Job(id="j1", kind=drafts.SETTLEMENT_KIND, payload={"period": "2026-09"}, attempt=1, max_attempts=3)
    out = await drafts.HANDLERS[drafts.SETTLEMENT_KIND](c, job)
    assert (out["invoices_created"], out["lines_added"]) == (1, 1)
    liveness.reset_cache()
    job = Job(id="j2", kind=drafts.SAAS_KIND, payload={"period": "2026-11"}, attempt=1, max_attempts=3)
    assert (await drafts.HANDLERS[drafts.SAAS_KIND](saas_conn(), job))["outcome"] == "ok"
    assert (await drafts.recent_runs(ScriptedConn([("FROM booking.job", [{"id": "j"}])]))) == [{"id": "j"}]


# ── Config ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_config_branches() -> None:
    taken = ScriptedConn([("SELECT 1 FROM booking.billing_entity WHERE code", 1)])
    with pytest.raises(Conflict):
        await config.create_entity(taken, {"code": "LB", "legal_entity": "x"}, "fin-1")
    made = ScriptedConn([("INSERT INTO booking.billing_entity", {**ENTITY, "id": uuid.uuid4()})])
    out = await config.create_entity(made, {"code": "LB", "legal_entity": "x", "is_default": True}, "fin-1")
    assert isinstance(out["id"], str) and made.sql("SET is_default = false") and made.sql("INSERT INTO booking.audit")
    with pytest.raises(NotFound):
        await config.update_entity(ScriptedConn(), "e", {}, "fin-1")
    off = ScriptedConn([("FROM booking.billing_entity WHERE id", ENTITY)])
    with pytest.raises(Invalid):
        await config.update_entity(off, "e", {"active": False}, "fin-1")
    ok = ScriptedConn([("FROM booking.billing_entity WHERE id", {**ENTITY, "is_default": False}),
                       ("UPDATE booking.billing_entity SET legal_entity", ENTITY)])  # fmt: skip
    assert (await config.update_entity(ok, "e", {"is_default": True}, "fin-1"))["code"] == "LB"
    with pytest.raises(Invalid):
        await config.create_tax_rule(ScriptedConn(), {"code": "X", "effective_from": date(2026, 2, 1),
                                                      "effective_to": date(2026, 1, 1)}, "fin-1")  # fmt: skip
    rule = {"id": "r", "code": "VAT", "invoice_kind": None, "recipient_country": None, "rate": Decimal("0.1"),
            "line_wording": "VAT", "footer_wording": None, "effective_from": date(2026, 1, 1), "effective_to": None,
            "active": True}  # fmt: skip
    with pytest.raises(Conflict):
        await config.create_tax_rule(ScriptedConn([("SELECT 1 FROM booking.tax_rule", 1)]),
                                     {"code": "VAT", "effective_from": date(2026, 1, 1)}, "fin-1")  # fmt: skip
    created = ScriptedConn([("INSERT INTO booking.tax_rule", rule)])
    body = {"code": "VAT", "rate": Decimal("0.1"), "line_wording": "VAT", "effective_from": date(2026, 1, 1)}
    assert (await config.create_tax_rule(created, body, "fin-1"))["code"] == "VAT"
    with pytest.raises(NotFound):
        await config.update_tax_rule(ScriptedConn(), "r", {}, "fin-1")
    upd = ScriptedConn([("FROM booking.tax_rule WHERE id", rule), ("UPDATE booking.tax_rule", rule)])
    assert (await config.update_tax_rule(upd, "r", {"rate": Decimal("0.08")}, "fin-1"))["id"] == "r"
    assert await config.pick_tax_rule(ScriptedConn([("FROM booking.tax_rule", rule)]), "saas", "TH",
                                      date(2026, 5, 1)) == rule  # fmt: skip
    assert await config.list_tax_rules(ScriptedConn([("FROM booking.tax_rule", [rule])])) == [rule]
    assert await config.list_entities(ScriptedConn([("FROM booking.billing_entity", [ENTITY])])) == [ENTITY]
    assert await config.default_entity(ScriptedConn()) is None


@pytest.mark.asyncio
async def test_terms_branches() -> None:
    with pytest.raises(Invalid):
        await config.put_terms(ScriptedConn(), None, {**TERMS, "suspend_after_days": 1}, "fin-1")
    with pytest.raises(NotFound):
        await config.put_terms(ScriptedConn(), str(T1), TERMS, "fin-1")
    fresh = ScriptedConn([("SELECT 1 FROM booking.b2b_partners", 1),
                          ("INSERT INTO booking.billing_terms", {**TERMS, "party_tenant_id": T1})])  # fmt: skip
    assert (await config.put_terms(fresh, str(T1), TERMS, "fin-1"))["party_tenant_id"] == str(T1)
    again = ScriptedConn([("FOR UPDATE", TERMS), ("UPDATE booking.billing_terms", TERMS)])
    assert (await config.put_terms(again, None, TERMS, "fin-1"))["id"] == "t"
    with pytest.raises(NotFound):
        await config.delete_terms_override(ScriptedConn(), str(T1), "fin-1")
    gone = ScriptedConn([("DELETE FROM booking.billing_terms", TERMS)])
    await config.delete_terms_override(gone, str(T1), "fin-1")
    assert gone.sql("INSERT INTO booking.audit_logs")
    assert await config.terms_for(ScriptedConn([("FROM booking.billing_terms", TERMS)]), T1) == TERMS
    assert await config.list_terms(ScriptedConn([("FROM booking.billing_terms t", [TERMS])])) == [TERMS]


# ── Invoices ─────────────────────────────────────────────────────────────────

ROW = "FROM booking.agency_invoice i"


@pytest.mark.asyncio
async def test_invoice_refusals() -> None:
    with pytest.raises(NotFound):
        await invoices.get(ScriptedConn(), "AAS-2026-000001")
    issued = ScriptedConn([(ROW, inv_row(status="issued", number="AAS-2026-000001"))])
    with pytest.raises(Conflict, match="only a draft"):
        await invoices.add_line(issued, INV, {"description": "x", "unit_price": Decimal(1)}, "fin-1")
    with pytest.raises(Conflict):
        await invoices.remove_line(issued, INV, 1, "fin-1")
    with pytest.raises(Conflict):
        await invoices.delete_draft(issued, INV, "fin-1")
    with pytest.raises(Invalid):
        await invoices.add_line(issued, INV, {"description": "card 4111 1111 1111 1111", "unit_price": 1}, "f")
    draft = ScriptedConn([(ROW, inv_row())])
    with pytest.raises(Conflict, match="issued invoice"):
        await invoices.patch(draft, INV, {"e_invoice_no": "E1"}, "fin-1")
    with pytest.raises(Conflict, match="already"):
        await invoices.patch(ScriptedConn([(ROW, inv_row(status="paid", e_invoice_no="E0"))]), INV,
                             {"e_invoice_no": "E1"}, "fin-1")  # fmt: skip
    with pytest.raises(Conflict, match="no_lines|without lines"):
        await invoices.issue(draft, INV, "fin-1")
    with pytest.raises(Conflict, match="only an issued"):
        await invoices.void(draft, INV, "x", "fin-1")
    with pytest.raises(Conflict, match="approved payment"):
        await invoices.void(ScriptedConn([(ROW, inv_row(status="issued", paid=Decimal(5)))]), INV, "x", "fin-1")
    with pytest.raises(Conflict, match="waiting"):
        await invoices.void(ScriptedConn([(ROW, inv_row(status="issued", waiting=Decimal(5)))]), INV, "x", "fin-1")
    with pytest.raises(NotFound):
        await invoices.remove_line(draft, INV, 9, "fin-1")


def issue_conn(**over: Any) -> ScriptedConn:
    c = ScriptedConn([(ROW, inv_row(**over)), ("FROM booking.agency_invoice_line", [])])
    c.on("SELECT EXISTS (SELECT 1 FROM booking.agency_invoice_line", True)
    c.on("WHERE is_default AND active", ENTITY)
    c.on("FROM booking.billing_terms", TERMS)
    c.on("SELECT current_date", date(2026, 11, 5))
    c.on(VIEW_READY_FRAGMENT, False)
    c.on("FROM booking.tax_rule", None)
    c.on("SELECT tax_rate FROM", Decimal(0))
    c.on("coalesce(sum(amount), 0) FROM booking.agency_invoice_line", Decimal(100))
    c.on("SELECT total FROM booking.agency_invoice", Decimal(100))
    c.on("INSERT INTO booking.invoice_sequence", 7)
    return c


@pytest.mark.asyncio
async def test_issue_paths() -> None:
    c = issue_conn()
    await invoices.issue(c, INV, "fin-1")
    args = c.sql("SET status = 'issued'")[0]
    assert args[1] == "AAS-2026-000007" and args[4] == date(2026, 11, 19)
    assert '"source": "booking"' in args[7]
    with pytest.raises(Invalid):
        await invoices.issue(issue_conn(), INV, "fin-1", due_date=date(2026, 11, 1))
    neg = issue_conn().on("SELECT total FROM booking.agency_invoice", Decimal(-1))
    with pytest.raises(Conflict, match="below zero"):
        await invoices.issue(neg, INV, "fin-1")
    zero = issue_conn().on("SELECT total FROM booking.agency_invoice", Decimal(0))
    await invoices.issue(zero, INV, "fin-1")
    assert zero.sql("SET status = 'paid'")
    with pytest.raises(Conflict, match="billing entity"):
        await invoices.issue(issue_conn().on("WHERE is_default AND active", None), INV, "fin-1")
    with pytest.raises(Conflict, match="terms"):
        await invoices.issue(issue_conn().on("FROM booking.billing_terms", None), INV, "fin-1")


@pytest.mark.asyncio
async def test_invoice_edits_and_lists() -> None:
    c = ScriptedConn([(ROW, inv_row()), ("coalesce(max(line_no), 0) + 1", 2), ("SELECT tax_rate", Decimal("0.1")),
                      ("coalesce(sum(amount), 0)", Decimal("50.00"))])  # fmt: skip
    await invoices.add_line(c, INV, {"description": "Contract price", "quantity": 2, "unit_price": Decimal(25)}, "f")
    assert c.sql("SET subtotal")[0][1:] == (Decimal("50.00"), Decimal("5.00"))
    c.on("DELETE FROM booking.agency_invoice_line", {"line_role": "adjustment", "description": "x", "amount": 1})
    await invoices.remove_line(c, INV, 2, "fin-1")
    await invoices.delete_draft(c, INV, "fin-1")
    await invoices.patch(ScriptedConn([(ROW, inv_row(status="issued", number="AAS-2026-000002"))]), INV,
                         {"staff_note": " call back ", "e_invoice_no": " E-9 "}, "fin-1")  # fmt: skip
    listed = ScriptedConn([("SELECT count(*)", 1), (ROW, [inv_row(bill_to='{"a": 1}')])])
    out = await invoices.list_invoices(listed, kind="saas", status="overdue", tenant_id=str(T1), q="Jun%")
    assert out["items"][0]["bill_to"] == {"a": 1} and out["total"] == 1
    for status in ("waiting", "draft"):
        await invoices.list_invoices(listed, status=status)
    assert (await invoices.summary(ScriptedConn([("GROUP BY i.currency", [{"currency": "USD"}])])))["by_currency"]
    voiding = ScriptedConn([(ROW, inv_row(status="issued", number="AAS-2026-000003"))])
    await invoices.void(voiding, INV, "wrong plan", "fin-1")
    assert voiding.sql("SET voided = true")


# ── Payments ─────────────────────────────────────────────────────────────────

ISSUED = inv_row(status="issued", number="AAS-2026-000001")
BODY = {"amount": Decimal("40.00"), "method": "bank_transfer", "received_on": date(2026, 11, 5), "reference": "T1",
        "document_ref": "statement line 3", "note": None}  # fmt: skip
STORED = {"id": PAY, "invoice_id": INV, "amount": Decimal("40.00"), "currency": "USD", "method": "bank_transfer",
          "received_on": date(2026, 11, 5), "reference": "T1", "document_ref": "statement line 3", "note": None,
          "status": "recorded", "recorded_by": "fin-1", "reject_reason": None}  # fmt: skip


@pytest.mark.asyncio
async def test_record_payment() -> None:
    with pytest.raises(Invalid):
        await payments.record(ScriptedConn(), INV, BODY, "fin-1", None)
    with pytest.raises(Invalid):
        await payments.record(ScriptedConn(), INV, {**BODY, "note": "4111 1111 1111 1111"}, "fin-1", "key-12345")
    c = ScriptedConn([(ROW, ISSUED), ("INSERT INTO booking.agency_payment", STORED)])
    assert (await payments.record(c, INV, BODY, "fin-1", "key-12345"))["replayed"] is False
    replay = ScriptedConn([(ROW, ISSUED), ("AND idempotency_key = $2", STORED)])
    assert (await payments.record(replay, INV, BODY, "fin-1", "key-12345"))["replayed"] is True
    with pytest.raises(Conflict, match="another payment"):
        await payments.record(replay, INV, {**BODY, "amount": Decimal(41)}, "fin-1", "key-12345")
    with pytest.raises(Conflict, match="issued invoice"):
        await payments.record(ScriptedConn([(ROW, inv_row())]), INV, BODY, "fin-1", "key-12345")
    with pytest.raises(Conflict) as e:
        await payments.record(ScriptedConn([(ROW, {**ISSUED, "waiting": Decimal(70)})]), INV, BODY, "f", "key-12345")
    assert e.value.code == "amount_exceeds_balance"


@pytest.mark.asyncio
async def test_decide_payment() -> None:
    with pytest.raises(NotFound):
        await payments.approve(ScriptedConn(), PAY, "fin-2")
    with pytest.raises(Forbidden):
        await payments.approve(ScriptedConn([("FROM booking.agency_payment WHERE id", STORED)]), PAY, "fin-1")
    decided = ScriptedConn([("FROM booking.agency_payment WHERE id", {**STORED, "status": "approved"})])
    with pytest.raises(Conflict):
        await payments.approve(decided, PAY, "fin-2")
    void_inv = ScriptedConn([("FROM booking.agency_payment WHERE id", STORED), (ROW, inv_row(status="void"))])
    with pytest.raises(Conflict):
        await payments.approve(void_inv, PAY, "fin-2")
    ok = ScriptedConn([("FROM booking.agency_payment WHERE id", STORED), (ROW, ISSUED),
                       ("UPDATE booking.agency_payment", {**STORED, "status": "approved"}),
                       ("SELECT status FROM booking.agency_invoice", "paid")])  # fmt: skip
    assert (await payments.approve(ok, PAY, "fin-2"))["invoice_status"] == "paid"
    with pytest.raises(Invalid):
        await payments.reject(ok, PAY, "4111 1111 1111 1111", "fin-2")
    await payments.reject(ok, PAY, "not on the statement", "fin-2")
    listed = ScriptedConn([("SELECT count(*)", 1), ("FROM booking.agency_payment p", [STORED])])
    assert (await payments.list_payments(listed, state="recorded", invoice="AAS-2026-000001"))["total"] == 1


# ── CIS read ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bill_to_snapshot() -> None:
    assert await cis.bill_to(ScriptedConn([(VIEW_READY_FRAGMENT, False)]), T1, "Juniper") == {
        "agency_name": "Juniper", "source": "booking"}  # fmt: skip
    liveness.reset_cache()
    assert (await cis.bill_to(ScriptedConn([(VIEW_READY_FRAGMENT, True)]), T1, "Juniper"))["source"] == "booking"
    liveness.reset_cache()
    row = {"legal_entity_name": "Juniper Co", "tax_id": "T-1", "billing_email": "a@b.example",
           "billing_address": "1 Lane", "billing_country": "TH"}  # fmt: skip
    got = await cis.bill_to(ScriptedConn([(VIEW_READY_FRAGMENT, True), ("FROM catalog.v_tenant_billing_v1", row)]),
                            T1, "Juniper")  # fmt: skip
    assert got["source"] == "cis" and got["tax_id"] == "T-1"


# ── Routes ───────────────────────────────────────────────────────────────────


def test_routes_wire_through() -> None:
    conn = issue_conn(status="draft")
    conn.on("SELECT count(*)", 0)
    conn.on("ORDER BY i.created_at DESC", [inv_row()])
    conn.on("GROUP BY i.currency", [])
    conn.on("FROM booking.billing_terms t", [])
    conn.on("coalesce(max(line_no), 0) + 1", 1)
    c = client(FIN, conn)
    assert c.get("/booking/agency-invoices", params={"status": "overdue"}).status_code == 200
    assert c.get("/booking/agency-invoices/summary").status_code == 200
    assert c.get(f"/booking/agency-invoices/{INV}").status_code == 200
    assert c.post(f"/booking/agency-invoices/{INV}/issue", json={}).status_code == 200
    assert c.patch(f"/booking/agency-invoices/{INV}", json={"staff_note": "x"}).status_code == 200
    assert c.post(f"/booking/agency-invoices/{INV}/lines", json={"description": "x", "unit_price": "1"}).status_code \
        == 201  # fmt: skip
    assert c.delete(f"/booking/agency-invoices/{INV}").status_code == 204
    assert c.post(f"/booking/agency-invoices/{INV}/void", json={"reason": "x"}).status_code == 409
    assert c.post("/booking/agency-billing/runs", json={"kind": "saas", "period": "2026-13"}).status_code == 422
    for path in ("/booking/agency-payments", "/booking/agency-billing/status", "/booking/agency-billing/runs",
                 "/booking/billing-entities", "/booking/tax-rules", "/booking/billing-terms"):  # fmt: skip
        assert c.get(path).status_code == 200, path
    bad_terms = {**{k: v for k, v in TERMS.items() if k not in ("id", "party_tenant_id")}, "deposit_pct": "0.3",
                 "suspend_after_days": 1}  # fmt: skip
    assert c.put("/booking/billing-terms/default", json=bad_terms).status_code == 422
    assert c.delete(f"/booking/billing-terms/agencies/{T1}").status_code == 204  # scripted: the override exists
    assert c.post(f"/booking/agency-payments/{PAY}/reject", json={"reason": "x"}).status_code == 404
    assert c.post(f"/booking/agency-invoices/{INV}/payments", json={**BODY, "amount": "40.00",
                  "received_on": "2026-11-05"}).status_code == 422  # no Idempotency-Key  # fmt: skip
