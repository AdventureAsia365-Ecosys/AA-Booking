"""AA-1037 R4 extras without a database: G1 (cancellation policy on quotes / holds), G2 (booking confirmation), G4
(credit stop and overdue reminders), G5 (accountant CSV export) and G6 (partner terms clauses and the B2B terms
answer), on scripted connections. The same flows on PostgreSQL: tests/integration/test_aa1037_g_extras.py. Names and
places are invented."""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import mailer
from app.agency_billing import clauses, export, reminders
from app.auth.staff import PERMISSIONS, PREVIEW_PERMISSIONS, Staff, current_staff, get_pool
from app.b2b import cancellation, confirmation, holds, terms
from app.b2b.errors import B2BError
from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.jobs.kinds import EXPECTED_SECONDS
from app.jobs.queue import Job
from app.jobs.worker import PermanentError
from app.main import create_app
from app.settings import Settings
from tests.unit.fakes import ScriptedConn

FIN = Staff("fin-1", None, "Finance One", "finance", True, None)
ADMIN = Staff("adm-1", None, "Admin One", "admin", True, None)
SALES = Staff("u-1", None, "Sales One", "sales", True, "reviewer")
CASE = "30000000-0000-4000-8000-000000000001"
HOLD = "30000000-0000-4000-8000-000000000002"
QUOTE = "30000000-0000-4000-8000-000000000003"
POLICY = "30000000-0000-4000-8000-000000000004"
PLAN = "30000000-0000-4000-8000-000000000005"
INV = "30000000-0000-4000-8000-000000000006"
TENANT = "30000000-0000-4000-8000-0000000000aa"
TIERS = json.dumps(
    [
        {"min_days": 60, "fee_pct": 0},
        {"min_days": 30, "fee_pct": 25},
        {"min_days": 15, "fee_pct": 50},
        {"min_days": 7, "fee_pct": 75},
        {"min_days": 0, "fee_pct": 100},
    ]
)
POLICY_ROW = {"id": POLICY, "trip_plan_id": None, "valid_from": date(2026, 10, 1), "tiers": TIERS}
NOW = datetime(2026, 11, 5, 8, 0, tzinfo=UTC)


def client(staff: Staff, conn: ScriptedConn) -> TestClient:
    app = create_app(Settings(database_url=None, environment="test"))
    app.dependency_overrides[current_staff] = lambda: staff
    app.dependency_overrides[get_pool] = lambda: conn
    return TestClient(app)


# ── G1: the cancellation policy on answers ───────────────────────────────────


def test_policy_out_shape() -> None:
    policy = cancellation._policy(POLICY_ROW)
    out = cancellation.policy_out(policy, basis=cancellation.BASIS_TODAY)
    assert out == {
        "policy_id": POLICY,
        "basis": "in_force_today",
        "tiers": [
            {"min_days": 60, "fee_pct": "0.00"},
            {"min_days": 30, "fee_pct": "25.00"},
            {"min_days": 15, "fee_pct": "50.00"},
            {"min_days": 7, "fee_pct": "75.00"},
            {"min_days": 0, "fee_pct": "100.00"},
        ],
    }
    assert cancellation.policy_out(None, basis=cancellation.BASIS_LOCKED) is None


def test_first_plan_reads_records_without_line_numbers() -> None:
    assert cancellation.first_plan([{"component_id": "sv:x"}, {"component_id": f"tp:{PLAN}"}]) == PLAN
    assert cancellation.first_plan([{"line_no": 2, "component_id": None}]) is None


@pytest.mark.asyncio
async def test_policy_of_booking_locked_else_in_force_at_confirm() -> None:
    conn = ScriptedConn().on("FROM booking.cancellation_policies p WHERE p.id", POLICY_ROW)
    got = await cancellation.policy_of_booking(conn, policy_id=POLICY, quote_id=QUOTE, confirmed_at=NOW)
    assert got is not None and got.id == POLICY and not conn.sql("JOIN booking.policy_approvals")
    conn = ScriptedConn()
    conn.on("FROM booking.b2b_quote_lines", [{"line_no": 1, "component_id": f"tp:{PLAN}"}])
    conn.on("JOIN booking.policy_approvals a", POLICY_ROW)
    got = await cancellation.policy_of_booking(conn, policy_id=None, quote_id=QUOTE, confirmed_at=NOW)
    assert got is not None
    [args] = conn.sql("JOIN booking.policy_approvals a")
    assert args == ("b2b", PLAN, NOW.date(), NOW)


@pytest.mark.asyncio
async def test_hold_policy_locked_on_confirmed_else_today() -> None:
    conn = ScriptedConn().on("FROM booking.cancellation_policies p WHERE p.id", POLICY_ROW)
    hold = {"stored_status": "confirmed", "cancellation_policy_id": POLICY, "quote_id": QUOTE, "confirmed_at": NOW}
    out = await holds._hold_policy(conn, hold, [])
    assert out is not None and out["basis"] == "locked"
    conn = ScriptedConn().on("JOIN booking.policy_approvals a", POLICY_ROW)
    out = await holds._hold_policy(conn, {"stored_status": "active"}, [{"line_no": 1, "component_id": f"tp:{PLAN}"}])
    assert out is not None and out["basis"] == "in_force_today"
    assert await holds._hold_policy(ScriptedConn(), {"stored_status": "active"}, []) is None


# ── G4: credit stop ──────────────────────────────────────────────────────────

CALLER = holds.Caller(partner_id="p-1", tenant_id=TENANT, client_id="crm")


@pytest.mark.asyncio
async def test_credit_stop_only_when_past_due() -> None:
    await holds.check_credit(ScriptedConn(), CALLER)  # no status row: a new agency
    for status in ("ok", "warn"):
        conn = ScriptedConn().on("FROM booking.v_tenant_billing_status", {"status": status})
        await holds.check_credit(conn, CALLER)
    conn = ScriptedConn().on(
        "FROM booking.v_tenant_billing_status",
        {"status": "past_due", "oldest_overdue_days": 31, "overdue_invoices": 2, "suspend_after_days": 30},
    )
    with pytest.raises(B2BError) as exc:
        await holds.check_credit(conn, CALLER)
    assert exc.value.error == "credit_hold_overdue" and exc.value.status_code == 409
    assert "2 AA invoice(s) overdue" in exc.value.message and "31 days" in exc.value.message
    assert conn.sql("FROM booking.v_tenant_billing_status") == [(TENANT,)]


# ── G4: reminders ────────────────────────────────────────────────────────────


def test_parse_steps() -> None:
    assert reminders.parse_steps(None) == (1, 7, 14)
    assert reminders.parse_steps("[10, 3, 3]") == (3, 10)
    assert reminders.parse_steps([2, 5]) == (2, 5)
    for bad in ("[]", "[0]", "[400]", '["7"]', "[true]", "not json", "{}"):
        assert reminders.parse_steps(bad) == (1, 7, 14)


def test_due_step_sends_only_the_highest_step_once() -> None:
    steps = (1, 7, 14)
    assert reminders.due_step(0, steps, 30, []) is None
    assert reminders.due_step(1, steps, 30, []) == (1, "reminder")
    assert reminders.due_step(20, steps, 30, []) == (14, "reminder")  # not three at once
    assert reminders.due_step(20, steps, 30, [14]) is None
    assert reminders.due_step(30, steps, 30, [1, 7, 14]) == (30, "credit_stop")
    assert reminders.due_step(45, steps, 30, [30]) is None
    assert reminders.due_step(7, steps, 7, []) == (7, "credit_stop")  # the credit stop wins a shared day
    assert reminders.due_step(40, steps, None, []) == (14, "reminder")  # no terms: no credit stop step


def overdue_row(**over: Any) -> dict[str, Any]:
    row = {
        "id": INV,
        "number": "AAT-2026-000007",
        "invoice_kind": "settlement",
        "currency": "USD",
        "total": Decimal("1200.00"),
        "balance": Decimal("800.00"),
        "issued_on": date(2026, 10, 1),
        "due_date": date(2026, 10, 15),
        "days_overdue": 21,
        "billing_email": "accounts@saltmarsh-travel.example",
        "issuer": json.dumps(
            {
                "legal_entity": "Lantern Bay Ltd",
                "payment_instructions": "Bank: Harbour Bank\nAccount 0001",
                "invoice_inbox": "billing@lanternbay.example",
            }  # fmt: skip
        ),
        "agency_name": "Saltmarsh Travel",
        "suspend_after_days": 30,
        "done_steps": [7],
    }
    row.update(over)
    return row


def test_render_reminder_and_credit_stop() -> None:
    inv = reminders._overdue(overdue_row())
    msg = reminders.render(inv, step_kind="reminder", to="accounts@saltmarsh-travel.example", environment="prod")
    assert msg.subject == "Payment reminder: invoice AAT-2026-000007 is 21 days overdue"
    assert "800.00 USD" in msg.text and "Harbour Bank" in msg.text and "billing@lanternbay.example" in msg.text
    assert "Test copy" not in msg.text
    stop = reminders.render(
        inv, step_kind="credit_stop", to="staff@x.example", environment="dev", redirected_from=inv.billing_email
    )
    assert stop.subject.startswith("[Dev] New bookings paused: invoice AAT-2026-000007")
    assert "not to accounts@saltmarsh-travel.example" in stop.text and "new booking holds" in stop.text
    plain = reminders._overdue(overdue_row(issuer=None, days_overdue=1))
    one = reminders.render(plain, step_kind="reminder", to="a@b.example", environment="prod")
    assert "1 day overdue" in one.subject and "your AA contact" in one.text and "How to pay" not in one.text
    hostile = reminders._overdue(overdue_row(agency_name="<b>Agency</b>"))
    assert "<b>Agency</b>" not in reminders.render(hostile, step_kind="reminder", to="a@b.example",
                                                   environment="prod").html  # fmt: skip


def mail_settings(**over: Any) -> Settings:
    base = {"database_url": None, "environment": "prod", "ses_from_address": "noreply@lantern.example",
            "staff_notify_email": "staff@lantern.example"}  # fmt: skip
    base.update(over)
    return Settings(**base)


def reminder_conn(rows: list[dict[str, Any]]) -> ScriptedConn:
    conn = ScriptedConn()
    conn.on("FROM booking.settings", None)
    conn.on("FROM booking.agency_invoice i", rows)
    return conn


@pytest.mark.asyncio
async def test_reminder_run_sends_records_and_skips() -> None:
    rows = [
        overdue_row(),  # 21 days, step 7 done: send step 14
        overdue_row(id="i-2", number="AAS-2026-000001", invoice_kind="saas", days_overdue=3, done_steps=[1]),
        overdue_row(id="i-3", number="AAS-2026-000002", billing_email=None, days_overdue=30, done_steps=[]),
    ]
    conn = reminder_conn(rows)
    sent: list[mailer.Message] = []

    async def send(message: mailer.Message) -> str:
        sent.append(message)
        return "ses-1"

    out = await reminders.run(conn, mail_settings(), send, today=date(2026, 11, 5), actor="job:test")
    assert out == {"outcome": "done", "overdue": 3, "sent": 1, "skipped": 1, "failed": 0, "not_due": 1}
    assert [m.to for m in sent] == ["accounts@saltmarsh-travel.example"]
    records = conn.sql("INSERT INTO booking.agency_invoice_reminder")
    assert records[0] == (INV, 14, "reminder", 21, "sent", None, "accounts@saltmarsh-travel.example", False,
                          "ses-1", "job:test")  # fmt: skip
    assert records[1] == ("i-3", 30, "credit_stop", 30, "skipped", "no_billing_email", None, False, None, "job:test")
    assert conn.sql("FROM booking.agency_invoice i") == [(date(2026, 11, 5), reminders.MAX_INVOICES)]


@pytest.mark.asyncio
async def test_reminder_run_redirects_outside_prod_and_skips_without_mail_config() -> None:
    conn = reminder_conn([overdue_row()])

    async def send(message: mailer.Message) -> str:
        return "ses-2"

    out = await reminders.run(conn, mail_settings(environment="dev"), send, today=date(2026, 11, 5), actor="j")
    assert out["sent"] == 1
    [record] = conn.sql("INSERT INTO booking.agency_invoice_reminder")
    assert record[6] == "staff@lantern.example" and record[7] is True
    for settings, reason in (
        (mail_settings(ses_from_address=""), "SES_FROM_ADDRESS not set"),
        (mail_settings(environment="dev", staff_notify_email=""), "STAFF_NOTIFY_EMAIL not set"),
    ):
        quiet = reminder_conn([overdue_row()])
        skipped = await reminders.run(quiet, settings, send, today=date(2026, 11, 5), actor="j")
        assert skipped["outcome"] == "skipped" and skipped["reason"].startswith(reason)
        assert not quiet.sql("FROM booking.agency_invoice i")


@pytest.mark.asyncio
async def test_reminder_run_keeps_the_others_and_raises_on_a_failed_send() -> None:
    conn = reminder_conn([overdue_row(), overdue_row(id="i-2", number="AAS-2026-000003", done_steps=[])])
    calls = 0

    async def send(message: mailer.Message) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise mailer.MailError("Throttling", "slow down", permanent=False)
        return "ses-3"

    with pytest.raises(reminders.ReminderFailed):
        await reminders.run(conn, mail_settings(), send, today=date(2026, 11, 5), actor="j")
    [record] = conn.sql("INSERT INTO booking.agency_invoice_reminder")
    assert record[0] == "i-2"


class FakeSes:
    def __init__(self, error: str | None = None) -> None:
        self.error = error
        self.sent: list[dict[str, Any]] = []

    def send_email(self, **kwargs: Any) -> dict[str, Any]:
        if self.error:
            exc = RuntimeError(self.error)
            exc.response = {"Error": {"Code": self.error}}  # type: ignore[attr-defined]
            raise exc
        self.sent.append(kwargs)
        return {"MessageId": "m-1"}


@pytest.mark.asyncio
async def test_reminder_handler_sends_through_the_mailer() -> None:
    ses = FakeSes()
    handler = reminders.build_handler(mail_settings(), client_factory=lambda region: ses)
    conn = reminder_conn([overdue_row()])
    job = Job(id="j-1", kind=reminders.KIND, payload={"day": "2026-11-05"}, attempt=1, max_attempts=3)
    out = await handler(conn, job)
    assert out["sent"] == 1 and ses.sent[0]["FromEmailAddress"] == "noreply@lantern.example"
    with pytest.raises(PermanentError):
        await handler(conn, Job(id="j-2", kind=reminders.KIND, payload={"day": "05/11"}, attempt=1, max_attempts=3))
    paused = reminders.build_handler(mail_settings(), client_factory=lambda region: FakeSes("SendingPausedException"))
    with pytest.raises(PermanentError):
        await paused(reminder_conn([overdue_row()]), job)
    bounced = reminders.build_handler(mail_settings(), client_factory=lambda region: FakeSes("MessageRejected"))
    with pytest.raises(reminders.ReminderFailed):
        await bounced(reminder_conn([overdue_row()]), job)
    today = await reminders.build_handler(mail_settings(ses_from_address=""))(conn, Job("j", reminders.KIND, {}, 1, 3))
    assert today["outcome"] == "skipped"


@pytest.mark.asyncio
async def test_reminder_schedule_once_a_day() -> None:
    conn = ScriptedConn().on("INSERT INTO booking.job", "job-1")
    schedule = reminders.ReminderSchedule(now=lambda: NOW)
    assert await schedule(conn) == "job-1"
    assert await schedule(conn) is None
    [args] = conn.sql("INSERT INTO booking.job")
    assert args[0] == reminders.KIND and args[2] == f"{reminders.KIND}:2026-11-05"
    assert EXPECTED_SECONDS[reminders.KIND] == 300


@pytest.mark.asyncio
async def test_reminders_for_invoice() -> None:
    conn = ScriptedConn().on("FROM booking.agency_invoice_reminder r", [{"step_days": 7, "outcome": "sent"}])
    assert await reminders.for_invoice(conn, INV) == [{"step_days": 7, "outcome": "sent"}]


# ── G2: booking confirmation ─────────────────────────────────────────────────

BOOKING_ROW = {
    "hold_id": HOLD,
    "quote_id": QUOTE,
    "proposal_ref": "PR-77",
    "proposal_version": 2,
    "confirmed_at": NOW,
    "cancellation_policy_id": POLICY,
    "trip_case_id": CASE,
    "code": "TC-000099",
    "status": "confirmed",
    "start_date": date(2027, 3, 1),
    "end_date": date(2027, 3, 5),
    "agency_name": "Saltmarsh Travel",
}
LINE_ROWS = [
    {"line_no": 1, "line_ref": "L1", "component_kind": "trip_plan", "service_date": date(2027, 3, 1), "nights": None,
     "pax_adult": 2, "pax_child": 1, "pax_infant": 1, "rooms": "[]", "line_status": "confirmed",
     "title": "Misty  Ridge Loop"},
    {"line_no": 2, "line_ref": "L2", "component_kind": "hotel_room", "service_date": date(2027, 3, 3), "nights": 2,
     "pax_adult": 2, "pax_child": 0, "pax_infant": 0,
     "rooms": json.dumps([{"room_type_id": "x", "occupancy": "double", "count": 1}, {"occupancy": "suite"}]),
     "line_status": "available", "title": None},
    {"line_no": 3, "line_ref": "L3", "component_kind": "unknown", "service_date": date(2027, 3, 4), "nights": None,
     "pax_adult": 1, "pax_child": 0, "pax_infant": None, "rooms": None, "line_status": "weird", "title": "<script>"},
]  # fmt: skip


def confirmation_conn(booking: Any = BOOKING_ROW, cancelled_at: Any = None) -> ScriptedConn:
    conn = ScriptedConn()
    conn.on("JOIN booking.b2b_partners p ON p.id = h.partner_id", booking)
    conn.on("JOIN booking.b2b_hold_lines hl", LINE_ROWS)
    conn.on(
        "FROM booking.supplier_bookings sb",
        [
            {"line_ref": "L2", "confirmation_ref": "HX-1"},
            {"line_ref": "L2", "confirmation_ref": "HX-1"},
            {"line_ref": None, "confirmation_ref": "ZZ"},
        ],  # fmt: skip
    )
    conn.on("FROM booking.b2b_cancellations", cancelled_at)
    conn.on("FROM booking.cancellation_policies p WHERE p.id", POLICY_ROW)
    conn.on("FROM booking.billing_entity", "Lantern Bay Ltd")
    return conn


@pytest.mark.asyncio
async def test_confirmation_load_and_public_shape() -> None:
    conn = confirmation_conn()
    doc = await confirmation.load(conn, CASE, partner_id="p-1", now=NOW)
    assert doc is not None
    body = confirmation.public(doc)
    assert "_agency_name" not in body and body["status"] == "confirmed" and body["issuer"] == "Lantern Bay Ltd"
    assert body["travellers"] == {"adult": 2, "child": 1, "infant": 1}
    l1, l2, l3 = body["lines"]
    assert l1["title"] == "Misty Ridge Loop" and l1["kind"] == "trip_plan"
    assert l2["title"] == "Hotel room" and l2["rooms"] == [{"occupancy": "double", "count": 1}]
    assert l2["status"] == "confirmed" and l2["supplier_confirmations"] == ["HX-1"]
    assert l3["kind"] == "activity" and l3["status"] == "on_request" and l3["pax"]["infant"] == 0
    assert body["cancellation_policy"]["basis"] == "locked"
    assert conn.sql("JOIN booking.b2b_partners p ON p.id = h.partner_id") == [(CASE, "p-1")]
    from app.routers.b2b import BookingConfirmation

    BookingConfirmation.model_validate(body)
    assert "traveller" not in json.dumps(body, default=str).replace("travellers", "")


@pytest.mark.asyncio
async def test_confirmation_lookups() -> None:
    assert await confirmation.load(confirmation_conn(None), CASE, partner_id="p-1", now=NOW) is None
    # an agency cannot look a booking up by its code; staff can
    assert await confirmation.load(confirmation_conn(), "TC-000099", partner_id="p-1", now=NOW) is None
    conn = confirmation_conn(cancelled_at=NOW)
    doc = await confirmation.load(conn, "TC-000099", partner_id=None, now=NOW)
    assert doc is not None and doc["status"] == "cancelled"
    assert conn.sql("AND c.code = $1") == [("TC-000099", None)]


def test_tier_rows() -> None:
    tiers = cancellation.tiers_out(cancellation.parse_tiers(TIERS))
    assert confirmation.tier_rows(tiers) == [
        ("60+ days", "0 %"),
        ("30 to 59 days", "25 %"),
        ("15 to 29 days", "50 %"),
        ("7 to 14 days", "75 %"),
        ("0 to 6 days", "100 %"),
    ]
    assert confirmation.tier_rows([{"min_days": 1, "fee_pct": "10.5"}, {"min_days": 0, "fee_pct": "20"}]) == [
        ("1+ days", "10.5 %"),
        ("0 days", "20 %"),
    ]


@pytest.mark.asyncio
async def test_confirmation_html_is_escaped_and_printable() -> None:
    doc = await confirmation.load(confirmation_conn(cancelled_at=NOW), CASE, partner_id=None, now=NOW)
    assert doc is not None
    page = confirmation.render_html(doc)
    assert "<script>" not in page and "&lt;script&gt;" in page
    assert "Booking confirmation TC-000099" in page and "Cancelled" in page and "30 to 59 days" in page
    assert "Saltmarsh Travel" in page and "PR-77 (version 2)" in page and "HX-1" in page
    assert "2 adults, 1 child, 1 infant" in page and "Save as PDF" in page
    resp = confirmation.html_response(doc)
    assert resp.headers["content-disposition"] == 'attachment; filename="booking-confirmation-TC-000099.html"'
    assert resp.headers["content-security-policy"].startswith("default-src 'none'")
    no_policy = {**doc, "cancellation_policy": None, "issuer": None, "status": "confirmed"}
    assert "No cancellation policy is recorded" in confirmation.render_html(no_policy)
    assert confirmation.file_name({"trip_case_code": "../x"}) == "booking-confirmation-x.html"


# ── G6: terms ────────────────────────────────────────────────────────────────


def test_terms_helpers() -> None:
    assert terms.pct_text(Decimal("0.3000")) == "30.00"
    assert terms.money_list('{"VND": 1500000, "USD": 12.5}') == [
        {"amount": "12.50", "currency": "USD"},
        {"amount": "1500000.00", "currency": "VND"},
    ]
    assert terms.credit_out(None)["status"] == "ok" and not terms.credit_out(None)["holds_paused"]
    row = {"status": "past_due", "overdue_invoices": 1, "oldest_overdue_days": 33, "overdue_amount": "{}",
           "warn_after_days": 1, "suspend_after_days": 30}  # fmt: skip
    assert terms.credit_out(row)["holds_paused"] is True


@pytest.mark.asyncio
async def test_partner_terms_answer() -> None:
    conn = ScriptedConn()
    conn.on("FROM booking.billing_terms", {"deposit_pct": Decimal("0.2500"), "balance_days_before_departure": 21,
                                           "settlement_due_days": 10})  # fmt: skip
    conn.on("FROM booking.v_tenant_billing_status", None)
    conn.on("JOIN booking.policy_approvals a", POLICY_ROW)
    conn.on("WHERE status = 'approved'", [{"code": "price_error", "title": "Price errors", "body": "B",
                                           "free_cancel_days": 2, "version": 3, "approved_at": NOW}])  # fmt: skip
    out = await terms.partner_terms(conn, tenant_id=TENANT, today=date(2026, 11, 5))
    assert out["payment"] == {
        "deposit_pct": "25.00",
        "balance_days_before_departure": 21,
        "settlement_due_days": 10,
        "scope": "default",
    }
    assert out["credit"]["status"] == "ok" and out["cancellation_policy"]["policy_id"] == POLICY
    assert [c["code"] for c in out["clauses"]] == ["price_error"]
    from app.routers.b2b import PartnerTerms

    PartnerTerms.model_validate(out)
    empty = await terms.partner_terms(ScriptedConn(), tenant_id=TENANT, today=date(2026, 11, 5))
    assert empty["payment"] is None and empty["clauses"] == [] and empty["cancellation_policy"] is None


CLAUSE = {"id": "40000000-0000-4000-8000-000000000001", "code": "price_error", "position": 10, "title": "Price errors",
          "body": "Old", "free_cancel_days": 2, "status": "draft", "version": 1, "updated_by": "system:bk-044",
          "approved_by": None, "approved_at": None}  # fmt: skip


@pytest.mark.asyncio
async def test_put_clause_creates_or_rewords_as_draft() -> None:
    conn = ScriptedConn()
    conn.on("INSERT INTO booking.partner_terms_clause", {**CLAUSE, "code": "new_one", "body": "New"})
    out = await clauses.put_clause(conn, "new_one", {"title": "T", "body": "New", "free_cancel_days": None}, "fin-1")
    assert out["code"] == "new_one"
    [ins] = conn.sql("INSERT INTO booking.partner_terms_clause")
    assert ins == ("new_one", "T", "New", None, 100, "fin-1")
    [audit] = conn.sql("INSERT INTO booking.audit_logs")
    assert audit[3] == "terms_clause.created"

    conn = ScriptedConn()
    conn.on("FOR UPDATE", {**CLAUSE, "status": "approved", "approved_by": "adm-1", "approved_at": NOW})
    conn.on("UPDATE booking.partner_terms_clause SET heading", {**CLAUSE, "body": "New", "version": 2})
    out = await clauses.put_clause(conn, "price_error", {"title": "Price errors", "body": "New"}, "fin-1")
    assert out["version"] == 2
    [upd] = conn.sql("UPDATE booking.partner_terms_clause SET heading")
    assert upd == ("price_error", "Price errors", "New", None, 10, "fin-1")  # position kept
    [audit] = conn.sql("INSERT INTO booking.audit_logs")
    assert audit[3] == "terms_clause.updated"


@pytest.mark.asyncio
async def test_approve_clause_rules() -> None:
    def conn_with(row: Any) -> ScriptedConn:
        c = ScriptedConn().on("FOR UPDATE", row)
        c.on("SET status = 'approved'", {**CLAUSE, "status": "approved", "approved_by": "adm-1", "approved_at": NOW})
        return c

    with pytest.raises(NotFound):
        await clauses.approve_clause(conn_with(None), "price_error", 1, "adm-1")
    with pytest.raises(Conflict) as exc:
        await clauses.approve_clause(conn_with(CLAUSE), "price_error", 2, "adm-1")
    assert exc.value.code == "clause_changed"
    with pytest.raises(Conflict) as exc:
        await clauses.approve_clause(conn_with({**CLAUSE, "status": "approved"}), "price_error", 1, "adm-1")
    assert exc.value.code == "clause_approved"
    with pytest.raises(Forbidden):
        await clauses.approve_clause(conn_with({**CLAUSE, "updated_by": "adm-1"}), "price_error", 1, "adm-1")
    conn = conn_with(CLAUSE)
    out = await clauses.approve_clause(conn, "price_error", 1, "adm-1")
    assert out["status"] == "approved"
    [audit] = conn.sql("INSERT INTO booking.audit_logs")
    assert audit[3] == "terms_clause.approved"


@pytest.mark.asyncio
async def test_clause_lists() -> None:
    conn = ScriptedConn().on("FROM booking.partner_terms_clause ORDER BY", [CLAUSE])
    assert await clauses.list_clauses(conn) == [CLAUSE]
    assert await clauses.approved(ScriptedConn()) == []


# ── G5: export ───────────────────────────────────────────────────────────────


def test_cells_and_period() -> None:
    assert export.cell(None) == "" and export.cell(Decimal("-12.50")) == "-12.50"
    assert export.cell(date(2026, 10, 1)) == "2026-10-01"
    assert export.cell("=HYPERLINK(1)") == "'=HYPERLINK(1)" and export.cell("-x") == "'-x"
    assert export.cell("Plain") == "Plain"
    export.check_period(date(2026, 1, 1), date(2026, 12, 31))
    for start, end in ((date(2026, 2, 1), date(2026, 1, 1)), (date(2025, 1, 1), date(2026, 1, 2))):
        with pytest.raises(Invalid):
            export.check_period(start, end)
    assert export.file_name("invoices", date(2026, 10, 1), date(2026, 10, 31)) == (
        "agency-invoices-2026-10-01_2026-10-31.csv"
    )


INVOICE_EXPORT_ROW = {
    "number": "AAT-2026-000007", "invoice_kind": "settlement", "status": "issued", "agency_name": "Saltmarsh Travel",
    "tenant_id": TENANT,
    "bill_to": json.dumps({"legal_entity_name": "Saltmarsh Travel Co", "tax_id": "0101", "billing_country": "TH"}),
    "period_start": date(2026, 10, 1), "period_end": date(2026, 11, 1), "issued_on": date(2026, 11, 2),
    "due_date": date(2026, 11, 16), "currency": "USD", "subtotal": Decimal("100.00"), "tax_rate": Decimal("0"),
    "tax_amount": Decimal("0"), "total": Decimal("100.00"), "paid": Decimal("40.00"), "balance": Decimal("60.00"),
    "e_invoice_no": None, "voided_at": None,
}  # fmt: skip
PAYMENT_EXPORT_ROW = {
    "invoice_number": "AAT-2026-000007", "invoice_kind": "settlement", "agency_name": "Saltmarsh Travel",
    "received_on": date(2026, 11, 3), "amount": Decimal("40.00"), "currency": "USD", "method": "bank_transfer",
    "reference": "=SUM(A1)", "document_ref": "statement line 9", "status": "approved", "recorded_by": "fin-1",
    "recorded_at": NOW, "approved_by": "fin-2", "approved_at": NOW, "rejected_by": None, "rejected_at": None,
}  # fmt: skip


@pytest.mark.asyncio
async def test_export_invoices_and_payments() -> None:
    conn = ScriptedConn()
    conn.on("FROM booking.agency_invoice i", [INVOICE_EXPORT_ROW, {**INVOICE_EXPORT_ROW, "bill_to": None,
                                                                  "voided_at": NOW, "status": "void"}])  # fmt: skip
    text, n = await export.export(conn, "invoices", date(2026, 11, 1), date(2026, 11, 30), "fin-1")
    assert n == 2 and text.startswith("﻿")
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    assert rows[0] == list(export.INVOICE_COLUMNS)
    first = dict(zip(rows[0], rows[1], strict=True))
    assert first["bill_to_legal_entity"] == "Saltmarsh Travel Co" and first["period_last_day"] == "2026-10-31"
    assert first["balance"] == "60.00" and first["voided_on"] == ""
    assert dict(zip(rows[0], rows[2], strict=True))["voided_on"] == "2026-11-05"
    [audit] = conn.sql("INSERT INTO booking.audit_logs")
    assert audit[3] == "agency_billing.exported" and json.loads(audit[7])["rows"] == 2

    conn = ScriptedConn().on("FROM booking.agency_payment pm", [PAYMENT_EXPORT_ROW])
    text, n = await export.export(conn, "payments", date(2026, 11, 1), date(2026, 11, 30), "fin-1")
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    pay = dict(zip(rows[0], rows[1], strict=True))
    assert n == 1 and pay["reference"] == "'=SUM(A1)" and pay["amount"] == "40.00" and pay["rejected_at"] == ""
    with pytest.raises(Invalid):
        await export.export(ScriptedConn(), "lines", date(2026, 11, 1), date(2026, 11, 30), "fin-1")


# ── Routes and permissions ───────────────────────────────────────────────────


def test_clause_approval_is_admin_only_and_never_in_preview() -> None:
    assert PERMISSIONS["terms_clause.approve"] == frozenset({"admin"})
    assert "terms_clause.approve" not in PREVIEW_PERMISSIONS


def test_staff_routes() -> None:
    conn = ScriptedConn()
    conn.on("FROM booking.agency_payment pm", [PAYMENT_EXPORT_ROW])
    conn.on("FROM booking.partner_terms_clause ORDER BY", [CLAUSE])
    conn.on("FOR UPDATE", CLAUSE)
    conn.on("SET status = 'approved'", {**CLAUSE, "status": "approved", "approved_by": "adm-1", "approved_at": NOW})
    fin = client(FIN, conn)
    resp = fin.get("/booking/agency-billing/export.csv", params={"what": "payments", "from": "2026-11-01",
                                                                  "to": "2026-11-30"})  # fmt: skip
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/csv")
    assert 'filename="agency-payments-2026-11-01_2026-11-30.csv"' in resp.headers["content-disposition"]
    bad = fin.get("/booking/agency-billing/export.csv", params={"what": "payments", "from": "2026-12-01",
                                                                 "to": "2026-11-01"})  # fmt: skip
    assert bad.status_code == 422
    assert fin.get("/booking/partner-terms-clauses").json()["items"][0]["code"] == "price_error"
    assert fin.post("/booking/partner-terms-clauses/price_error/approve", json={"version": 1}).status_code == 403
    assert client(ADMIN, conn).post("/booking/partner-terms-clauses/price_error/approve",
                                    json={"version": 1}).status_code == 200  # fmt: skip
    assert fin.put("/booking/partner-terms-clauses/Bad-Code", json={"title": "T", "body": "B"}).status_code == 422
    sales = client(SALES, conn)
    assert sales.get("/booking/agency-billing/export.csv", params={"what": "invoices", "from": "2026-11-01",
                                                                    "to": "2026-11-30"}).status_code == 403  # fmt: skip
    assert sales.get("/booking/partner-terms-clauses").status_code == 403


def test_staff_confirmation_route() -> None:
    conn = confirmation_conn()
    sales = client(SALES, conn)
    resp = sales.get("/booking/b2b-desk/bookings/TC-000099/confirmation")
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/html")
    assert "Booking confirmation TC-000099" in resp.text
    body = sales.get("/booking/b2b-desk/bookings/TC-000099/confirmation", params={"format": "json"}).json()
    assert body["agency_name"] == "Saltmarsh Travel" and body["trip_case_code"] == "TC-000099"
    missing = client(SALES, confirmation_conn(None)).get(f"/booking/b2b-desk/bookings/{CASE}/confirmation")
    assert missing.status_code == 404


# ── B2B routes (M2M) ─────────────────────────────────────────────────────────


def test_b2b_confirmation_and_terms_routes() -> None:
    from tests.staff_tokens import LocalM2M
    from tests.unit.test_b2b_api import PARTNER_ID, _client, _conn, _headers

    m2m = LocalM2M()
    conn = _conn()
    conn.on("JOIN booking.b2b_partners p ON p.id = h.partner_id", BOOKING_ROW)
    conn.on("JOIN booking.b2b_hold_lines hl", LINE_ROWS)
    conn.on("FROM booking.cancellation_policies p WHERE p.id", POLICY_ROW)
    c = _client(conn, m2m)
    resp = c.get(f"/booking/b2b/v1/bookings/{CASE}/confirmation", headers=_headers(m2m.token()))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["trip_case_code"] == "TC-000099" and "_agency_name" not in body and "agency_name" not in body
    assert conn.sql("JOIN booking.b2b_partners p ON p.id = h.partner_id")[0] == (CASE, PARTNER_ID)
    page = c.get(f"/booking/b2b/v1/bookings/{CASE}/confirmation", params={"format": "html"},
                 headers=_headers(m2m.token()))  # fmt: skip
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    conn.on("JOIN booking.b2b_partners p ON p.id = h.partner_id", None)
    gone = c.get(f"/booking/b2b/v1/bookings/{CASE}/confirmation", headers=_headers(m2m.token()))
    assert gone.status_code == 404 and gone.json()["error"] == "not_found"
    terms_resp = c.get("/booking/b2b/v1/terms", headers=_headers(m2m.token()))
    assert terms_resp.status_code == 200, terms_resp.text
    assert terms_resp.json()["credit"]["holds_paused"] is False
