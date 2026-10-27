"""AA-682 S3 (bk-028) billing flows without a database: the Python branches of ``app.ops.invoices`` and
``app.ops.payments`` (idempotency replay / conflict, refusals before the database, the schedule and amendment rules,
maker-checker, evidence, refunds, the case auto-advance and the notifications) driven through :class:`ScriptedConn`.
The SQL itself (money guard, settle, sums, views) is covered on a real PostgreSQL in
tests/integration/test_bk028_*.py."""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import asyncpg
import pytest

from app.auth.staff import Staff
from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.ops import invoices as inv
from app.ops import money
from app.ops import payments as pay
from tests.unit.fakes import ScriptedConn

SALES = Staff("u-1", None, "Sales One", "sales", True, "reviewer")
ADMIN = Staff("u-2", None, "Admin Two", "admin", True, "admin")
CONTRACTING = Staff("u-3", None, "Contracting", "contracting", True, "reviewer")
CASE_ID = "00000000-0000-4000-8000-0000000c0001"
INV_ID = "00000000-0000-4000-8000-0000000d0001"
PAY_ID = "00000000-0000-4000-8000-0000000e0001"
TODAY = date(2026, 11, 2)
KEY = "key-123456"

CASE = "tc.customer_id::text AS customer_id FROM booking.trip_cases tc"
FIND = "status, deleted_at FROM booking.invoices WHERE"
LOCK_INV = "customer_id::text AS customer_id, deleted_at FROM booking.invoices"
STORED_INV = "request_sha256 FROM booking.invoices WHERE trip_case_id"
STORED_PAY = "request_sha256 FROM booking.payments WHERE invoice_id"
LIVE = "WHERE trip_case_id = $1::uuid AND status <> 'cancelled' AND deleted_at IS NULL"
APPROVED = "WHERE v.trip_case_id = $1::uuid AND v.is_approved AND v.deleted_at IS NULL"
SETTINGS = "SELECT value FROM booking.settings WHERE key = $1"
INSERT_INV = "INSERT INTO booking.invoices"
INSERT_ITEM = "INSERT INTO booking.invoice_items"
INSERT_SCHED = "INSERT INTO booking.payment_schedule"
INSERT_PAY = "INSERT INTO booking.payments"
AUDIT = "INSERT INTO booking.audit_logs"
NOTIFY_CUSTOMER = "VALUES ('customer', $1::uuid"
NOTIFY_STAFF = "VALUES ('staff', NULL, $1"
SEND_ROW = "total_amount, customer_id::text AS customer_id FROM booking.invoices"
SCHED_SUM = "coalesce(sum(amount), 0) AS total FROM booking.payment_schedule"
CUSTOMER = "SELECT name, email, phone, address, code FROM booking.customers"
CASE_UPDATE = "UPDATE booking.trip_cases SET status"
NET = "booking.invoice_net_paid($1::uuid)"
PAYMENT_ROW = "LEFT JOIN booking.staff_users vo ON vo.cis_user_id = p.voided_by"
INVOICE_ROW = "b.oldest_overdue_date FROM booking.invoices i"

SETTINGS_VALUES = {
    "deposit_pct": 30,
    "deposit_due_days": 3,
    "balance_due_days_before_start": 30,
    "b2b_ops_timezone": "Asia/Ho_Chi_Minh",
    "invoice_issuer": {"name": "AA"},
    "payment_instructions": "Bank",
}


def _setting(key: str) -> str | None:
    return json.dumps(SETTINGS_VALUES[key]) if key in SETTINGS_VALUES else None


def case_row(**over: Any) -> dict[str, Any]:
    return {
        "id": CASE_ID,
        "code": "TC-000001",
        "status": "confirmed",
        "channel": "b2c",
        "start_date": TODAY + timedelta(days=60),
        "customer_id": "cus-1",
        **over,
    }


def inv_row(**over: Any) -> dict[str, Any]:
    return {
        "id": INV_ID,
        "invoice_number": "INV-000001",
        "trip_case_id": CASE_ID,
        "status": "sent",
        "currency": "USD",
        "total_amount": Decimal("2400.00"),
        "version_id": "ver-1",
        "customer_id": "cus-1",
        "deleted_at": None,
        **over,
    }


def pay_row(**over: Any) -> dict[str, Any]:
    return {
        "id": PAY_ID,
        "ref": "PMT-000001",
        "kind": "receipt",
        "amount": "720.00",
        "currency": "USD",
        "method": "bank_transfer",
        "received_on": TODAY,
        "reference": "TX-1",
        "note": None,
        "received_amount": None,
        "received_currency": None,
        "evidence_file_id": "file-1",
        "state": "recorded",
        "recorded_by": "u-1",
        "recorded_by_name": "Sales One",
        "recorded_at": None,
        "verified_by": None,
        "verified_by_name": None,
        "verified_at": None,
        "voided_by": None,
        "voided_by_name": None,
        "voided_at": None,
        "void_reason": None,
        "created_at": None,
        "updated_at": None,
        "invoice_id": INV_ID,
        "invoice_number": "INV-000001",
        "invoice_status": "sent",
        "trip_case_id": CASE_ID,
        "trip_case_code": "TC-000001",
        **over,
    }


def conn_for(
    *,
    case: dict[str, Any] | None = None,
    invoice: dict[str, Any] | None = None,
    payment: dict[str, Any] | None = None,
    case_status: str = "confirmed",
    invoice_status_for_track: str | None = None,
) -> ScriptedConn:
    c = case or case_row()
    i = invoice or inv_row()
    return ScriptedConn(
        [
            (CASE, c),
            (
                FIND,
                {
                    "id": i["id"],
                    "invoice_number": i["invoice_number"],
                    "trip_case_id": CASE_ID,
                    "status": i["status"],
                    "deleted_at": None,
                },
            ),  # fmt: skip
            (LOCK_INV, i),
            (SETTINGS, _setting),
            ("pg_timezone_names", True),
            ("(now() AT TIME ZONE $1)::date", TODAY),
            ("AS asked FROM booking.supplier_bookings", {"live": 0, "asked": 0}),
            ("SELECT status FROM booking.trip_cases WHERE id", case_status),
            ("SELECT status FROM booking.invoices WHERE trip_case_id", invoice_status_for_track),
            (PAYMENT_ROW, payment or pay_row()),
            ("FROM booking.ops_files f", []),
        ]
    )


@pytest.fixture(autouse=True)
def stub_details(monkeypatch: pytest.MonkeyPatch) -> None:
    async def detail(conn: Any, staff: Any, invoice_id: str) -> dict[str, Any]:
        return {"id": invoice_id, "detail": True}

    async def one(conn: Any, staff: Any, payment_id: str) -> dict[str, Any]:
        return {"id": payment_id, "one": True, "invoice": {}}

    monkeypatch.setattr(inv, "detail", detail)
    monkeypatch.setattr(pay, "_one", one)


def audits(conn: ScriptedConn) -> list[str]:
    return [args[3] for args in conn.sql(AUDIT)]


# ── Invoice create ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_needs_a_key_and_replays_or_refuses_another_body() -> None:
    with pytest.raises(Invalid) as exc:
        await inv.create(conn_for(), SALES, "TC-000001", {}, None)
    assert exc.value.code == "idempotency_key_required"
    sha = money.request_hash(inv._body_for_hash({}))
    conn = conn_for().on(STORED_INV, {"id": INV_ID, "request_sha256": sha})
    out, replayed = await inv.create(conn, SALES, "TC-000001", {}, KEY)
    assert replayed and out == {"id": INV_ID, "detail": True} and conn.sql(INSERT_INV) == []
    conn = conn_for().on(STORED_INV, {"id": INV_ID, "request_sha256": "f" * 64})
    with pytest.raises(Conflict) as exc:
        await inv.create(conn, SALES, "TC-000001", {}, KEY)
    assert exc.value.code == "idempotency_conflict"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rules", "code"),
    [
        ({CASE: case_row(channel="b2b")}, "b2b_no_invoice"),
        ({LIVE: {"id": INV_ID, "invoice_number": "INV-000001", "status": "sent"}}, "invoice_exists"),
        ({CASE: case_row(status="paid")}, "case_not_invoiceable"),
        ({}, "no_approved_version"),
        (
            {
                APPROVED: {
                    "id": "v",
                    "version_number": 1,
                    "title": None,
                    "total_price": None,
                    "currency": None,
                    "fx_rate_locked": None,
                }
            },
            "version_not_priced",
        ),  # fmt: skip
    ],
)
async def test_create_refusals(rules: dict[str, Any], code: str) -> None:
    conn = conn_for()
    for fragment, answer in rules.items():
        conn.on(fragment, answer)
    with pytest.raises(Conflict) as exc:
        await inv.create(conn, SALES, "TC-000001", {}, KEY)
    assert exc.value.code == code


def priced(conn: ScriptedConn, total: str = "2400.00") -> ScriptedConn:
    return conn.on(
        APPROVED,
        {
            "id": "ver-1",
            "version_number": 1,
            "title": "Classic",
            "total_price": Decimal(total),
            "currency": "USD",
            "fx_rate_locked": "1",
        },  # fmt: skip
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"adjustments": [{"description": "x", "unit_price": "1", "reason": ""}]}, "reason_required"),
        ({"adjustments": [{"description": "", "unit_price": "1", "reason": "r"}]}, "description_required"),
        ({"adjustments": [{"description": "x", "unit_price": "0", "reason": "r"}]}, "invalid_amount"),
        (
            {"adjustments": [{"description": "x", "quantity": "0", "unit_price": "1", "reason": "r"}]},
            "invalid_quantity",
        ),
        ({"adjustments": [{"description": "x", "unit_price": "1", "reason": "r"}] * 21}, "too_many_adjustments"),
        ({"adjustments": [{"description": "x", "unit_price": "-2400", "reason": "r"}]}, "invalid_total"),
        ({"schedule": [{"kind": "final", "amount": "10", "due_date": TODAY}]}, "schedule_mismatch"),
    ],
)
async def test_create_invalid_input(body: dict[str, Any], code: str) -> None:
    with pytest.raises(Invalid) as exc:
        await inv.create(priced(conn_for()), SALES, "TC-000001", body, KEY)
    assert exc.value.code == code


@pytest.mark.asyncio
async def test_create_draft_with_default_schedule_and_send_now() -> None:
    conn = priced(conn_for()).on(INSERT_INV, INV_ID)
    conn.on(SEND_ROW, {"id": INV_ID, "invoice_number": "INV-000001", "status": "draft", "currency": "USD",
                       "total_amount": Decimal("2500.00"), "customer_id": "cus-1"})  # fmt: skip
    conn.on(SCHED_SUM, {"n": 2, "total": Decimal("2500.00")})
    conn.on(CUSTOMER, {"name": None, "email": "a@example.test", "phone": None, "address": "1 Road", "code": "CUS-1"})
    conn.on("SELECT status FROM booking.invoices WHERE trip_case_id", "sent")
    body = {"adjustments": [{"description": "Transfer", "unit_price": "100", "reason": "asked"}], "send_now": True}
    out, replayed = await inv.create(conn, SALES, "TC-000001", body, KEY)
    assert out == {"id": INV_ID, "detail": True} and not replayed
    items = conn.sql(INSERT_ITEM)
    assert items[0][3] == Decimal("2400.00") and items[1][5] == Decimal("100.00")
    rows = conn.sql(INSERT_SCHED)
    assert [(r[2], r[3], r[4]) for r in rows] == [
        ("deposit", Decimal("750.00"), TODAY + timedelta(days=3)),
        ("final", Decimal("1750.00"), TODAY + timedelta(days=30)),
    ]
    assert audits(conn) == ["invoice.created", "invoice.sent", "trip_case.status_changed"]
    assert conn.sql(CASE_UPDATE) == [(CASE_ID, "invoice_sent")]
    (sent,) = conn.sql("UPDATE booking.invoices SET status = 'sent'")
    assert sent[2] == "a@example.test" and sent[4] == "1 Road"  # name falls back to the e-mail
    (row,) = conn.sql(NOTIFY_CUSTOMER)
    assert row[1] == "invoice.sent" and "no e-mail" not in row[3]


@pytest.mark.asyncio
async def test_create_race_lost_is_invoice_exists() -> None:
    conn = priced(conn_for()).on(INSERT_INV, asyncpg.UniqueViolationError("invoices_live_per_case_uq"))
    with pytest.raises(Conflict) as exc:
        await inv.create(conn, SALES, "TC-000001", {"schedule": None}, KEY)
    assert exc.value.code == "invoice_exists"


# ── Draft edit, delete, send ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_draft_branches() -> None:
    with pytest.raises(Conflict) as exc:
        await inv.update_draft(conn_for(), SALES, "INV-000001", {"adjustments": []})
    assert exc.value.code == "invoice_sent"
    with pytest.raises(Conflict) as exc:
        await inv.update_draft(conn_for(invoice=inv_row(status="cancelled")), SALES, "INV-000001", {"notes": "x"})
    assert exc.value.code == "invoice_closed"
    sent = conn_for().on("SELECT total_amount FROM booking.invoices WHERE id", Decimal("2400.00"))
    out = await inv.update_draft(sent, SALES, "INV-000001", {"notes": " paid in two "})
    assert out["changed"] == ["notes"] and sent.sql("UPDATE booking.invoices SET notes")[0][2] == "paid in two"
    draft = conn_for(invoice=inv_row(status="draft")).on("SELECT total_amount FROM booking.invoices WHERE id",
                                                         Decimal("2300.00"))  # fmt: skip
    out = await inv.update_draft(
        draft, SALES, "INV-000001", {"adjustments": [{"description": "d", "unit_price": "-100", "reason": "r"}]}
    )
    assert out["changed"] == ["adjustments"]
    assert draft.sql("DELETE FROM booking.invoice_items") and len(draft.sql(INSERT_SCHED)) == 2
    zero = conn_for(invoice=inv_row(status="draft")).on("SELECT total_amount FROM booking.invoices WHERE id", 0)
    with pytest.raises(Invalid):
        await inv.update_draft(zero, SALES, "INV-000001", {"adjustments": []})
    untouched = conn_for(invoice=inv_row(status="draft")).on("SELECT total_amount FROM booking.invoices WHERE id", 1)
    assert (await inv.update_draft(untouched, SALES, "INV-000001", {}))["changed"] == []


@pytest.mark.asyncio
async def test_delete_send_and_not_found() -> None:
    with pytest.raises(Conflict):
        await inv.delete_draft(conn_for(), SALES, "INV-000001")
    draft = conn_for(invoice=inv_row(status="draft"))
    assert (await inv.delete_draft(draft, SALES, "INV-000001"))["deleted"]
    assert audits(draft) == ["invoice.deleted"]
    assert await inv.send(conn_for(), SALES, "INV-000001") == {"id": INV_ID, "detail": True}  # already sent
    with pytest.raises(Conflict):
        await inv.send(conn_for(invoice=inv_row(status="cancelled")), SALES, "INV-000001")
    gone = ScriptedConn([(FIND, None)])
    with pytest.raises(NotFound):
        await inv.get_invoice(gone, SALES, "INV-000009")
    with pytest.raises(NotFound):
        await inv.lock_invoice(ScriptedConn([(FIND, {"id": INV_ID, "trip_case_id": CASE_ID, "deleted_at": None}),
                                             (CASE, case_row())]), "INV-000001")  # fmt: skip


@pytest.mark.asyncio
async def test_send_refusals() -> None:
    draft = inv_row(status="draft")
    send_row = {"id": INV_ID, "invoice_number": "INV-000001", "status": "draft", "currency": "USD",
                "total_amount": Decimal("2400.00"), "customer_id": "cus-1"}  # fmt: skip
    with pytest.raises(Conflict) as exc:
        await inv.send(conn_for(case=case_row(status="cancelled"), invoice=draft).on(SEND_ROW, send_row), SALES, "x")
    assert exc.value.code == "case_not_invoiceable"
    conn = conn_for(invoice=draft).on(SEND_ROW, send_row).on(SCHED_SUM, {"n": 1, "total": Decimal("10")})
    with pytest.raises(Invalid) as exc:
        await inv.send(conn, SALES, "x")
    assert exc.value.code == "schedule_mismatch"
    conn = conn_for(invoice=draft).on(SEND_ROW, send_row).on(SCHED_SUM, {"n": 2, "total": Decimal("2400.00")})
    conn.on(CUSTOMER, {"name": "A", "email": None, "phone": None, "address": None, "code": "CUS-1"})
    conn.on("UPDATE booking.invoices SET status = 'sent'", asyncpg.CheckViolationError("version"))
    with pytest.raises(Conflict) as exc:
        await inv.send(conn, SALES, "x")
    assert exc.value.code == "invoice_not_sendable"


# ── Cancel, amend, remind ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_branches() -> None:
    with pytest.raises(Invalid):
        await inv.cancel(conn_for(), SALES, "INV-000001", " ")
    assert await inv.cancel(conn_for(invoice=inv_row(status="cancelled")), SALES, "x", "r") == {
        "id": INV_ID,
        "detail": True,
    }
    with pytest.raises(Conflict) as exc:
        await inv.cancel(conn_for(invoice=inv_row(status="draft")), SALES, "x", "r")
    assert exc.value.code == "invoice_draft"
    paid = conn_for().on("SELECT net_paid, unverified_count", {"net_paid": Decimal("720"), "unverified_count": 0})
    with pytest.raises(Conflict) as exc:
        await inv.cancel(paid, SALES, "x", "r")
    assert exc.value.code == "invoice_has_payments" and exc.value.extra["net_paid"] == "720"
    clear = {"net_paid": Decimal("0"), "unverified_count": 0}
    raced = conn_for().on("SELECT net_paid, unverified_count", clear)
    raced.on("SET status = 'cancelled'", asyncpg.CheckViolationError("refund"))
    with pytest.raises(Conflict):
        await inv.cancel(raced, SALES, "x", "r")
    ok = conn_for(case_status="invoice_sent").on("SELECT net_paid, unverified_count", clear)
    await inv.cancel(ok, SALES, "x", "trip changed")
    assert audits(ok) == ["invoice.cancelled", "trip_case.status_changed"]
    assert ok.sql(CASE_UPDATE) == [(CASE_ID, "confirmed")]
    closed = conn_for(case=case_row(status="cancelled")).on("SELECT net_paid, unverified_count", clear)
    await inv.cancel(closed, SALES, "x", "r")
    assert closed.sql(CASE_UPDATE) == []


def amend_conn(net: str, rows: list[tuple[int, str, str, date]], *, status: str = "partially_paid") -> ScriptedConn:
    conn = conn_for(invoice=inv_row(status=status), case_status="payment_pending",
                    invoice_status_for_track="partially_paid")  # fmt: skip
    conn.on(NET, Decimal(net))
    conn.on("seq, kind, amount, due_date FROM booking.payment_schedule",
            [{"id": f"s{seq}", "seq": seq, "kind": kind, "amount": Decimal(a), "due_date": d}
             for seq, kind, a, d in rows])  # fmt: skip
    conn.on("SELECT max(position) FROM booking.invoice_items", 1)
    conn.on("SELECT status FROM booking.invoices WHERE id", status)
    return conn


LINE = [{"description": "Extra night", "unit_price": "100", "reason": "asked"}]
ROWS = [(1, "deposit", "720", TODAY), (2, "final", "1680", TODAY + timedelta(days=30))]


@pytest.mark.asyncio
async def test_amend_moves_the_difference_to_the_last_open_row() -> None:
    conn = amend_conn("720", ROWS)
    await inv.amend(conn, SALES, "INV-000001", {"adjustments": LINE})
    assert conn.sql("DELETE FROM booking.payment_schedule WHERE id = ANY") == [(["s2"],)]
    (row,) = conn.sql(INSERT_SCHED)
    assert (row[1], row[2], row[3]) == (2, "final", Decimal("1780.00"))
    # The amendment window opens before the first write and closes (the sums check) after the last one.
    window = [args[1] for args in conn.sql(inv.AMENDING_SQL)]
    assert window == [True, False]
    opened, closed = [i for i, (flat, _) in enumerate(conn.calls) if flat == inv.AMENDING_SQL]
    money_writes = [
        i
        for i, (flat, _) in enumerate(conn.calls)
        if flat.startswith(("INSERT INTO booking.invoice_items", INSERT_SCHED, "DELETE FROM booking.payment_schedule"))
    ]
    assert money_writes and opened < min(money_writes) and max(money_writes) < closed
    assert "invoice.amended" in audits(conn)
    assert conn.sql(NOTIFY_CUSTOMER)[0][1] == "invoice.updated"


@pytest.mark.asyncio
async def test_amend_branches() -> None:
    with pytest.raises(Conflict):
        await inv.amend(conn_for(invoice=inv_row(status="draft")), SALES, "x", {"adjustments": LINE})
    with pytest.raises(Invalid) as exc:
        await inv.amend(amend_conn("0", ROWS), SALES, "x", {"adjustments": []})
    assert exc.value.code == "adjustments_required"
    with pytest.raises(Invalid) as exc:
        await inv.amend(amend_conn("0", ROWS), SALES, "x", {"adjustments": [{**LINE[0], "unit_price": "-2400"}]})
    assert exc.value.code == "invalid_total"
    # Everything covered: a positive difference is a new row; a negative one needs a schedule.
    full = amend_conn("2400", ROWS, status="paid")
    await inv.amend(full, SALES, "x", {"adjustments": LINE})
    (row,) = full.sql(INSERT_SCHED)
    assert (row[2], row[3]) == ("other", Decimal("100.00"))
    with pytest.raises(Invalid) as exc:
        await inv.amend(amend_conn("2400", ROWS, status="paid"), SALES, "x",
                        {"adjustments": [{**LINE[0], "unit_price": "-100"}]})  # fmt: skip
    assert exc.value.code == "schedule_required"
    with pytest.raises(Invalid) as exc:
        await inv.amend(amend_conn("720", ROWS), SALES, "x", {"adjustments": [{**LINE[0], "unit_price": "-1700"}]})
    assert exc.value.code == "schedule_required"
    # A given schedule covers the rest of the new total and comes after the paid rows.
    early = [{"kind": "final", "amount": "1780", "due_date": TODAY - timedelta(days=1)}]
    with pytest.raises(Invalid) as exc:
        await inv.amend(amend_conn("720", ROWS), SALES, "x", {"adjustments": LINE, "schedule": early})
    assert exc.value.code == "invalid_schedule"
    given = amend_conn("720", ROWS)
    rows = [{"kind": "second", "amount": "780", "due_date": TODAY + timedelta(days=5)},
            {"kind": "final", "amount": "1000", "due_date": TODAY + timedelta(days=30)}]  # fmt: skip
    await inv.amend(given, SALES, "x", {"adjustments": LINE, "schedule": rows})
    assert [r[1] for r in given.sql(INSERT_SCHED)] == [2, 3]
    with pytest.raises(Invalid) as exc:
        await inv.amend(amend_conn("720", ROWS), SALES, "x", {"adjustments": LINE, "schedule": []})
    assert exc.value.code == "schedule_required"
    # Closing the window runs the sums check: a mismatch is a 409, not a failed commit.
    bad = amend_conn("720", ROWS).on(
        inv.AMENDING_SQL, lambda _id, opening, _by: None if opening else asyncpg.CheckViolationError("sums")
    )
    with pytest.raises(Conflict) as exc:
        await inv.amend(bad, SALES, "x", {"adjustments": LINE})
    assert exc.value.code == "invalid_amendment"
    taken = amend_conn("720", ROWS).on(INSERT_SCHED, asyncpg.UniqueViolationError("seq"))
    with pytest.raises(Conflict):
        await inv.amend(taken, SALES, "x", {"adjustments": LINE})


@pytest.mark.asyncio
async def test_remind_once_a_day() -> None:
    with pytest.raises(Conflict) as exc:
        await inv.remind(conn_for(invoice=inv_row(status="paid")), SALES, "x")
    assert exc.value.code == "nothing_due"
    conn = conn_for().on("SELECT balance, next_due_date", {"balance": Decimal("1680.00"), "next_due_date": TODAY})
    conn.on("ON CONFLICT DO NOTHING RETURNING id::text", "n-1")
    out = await inv.remind(conn, SALES, "x")
    assert out["reminded"] is True and audits(conn) == ["invoice.reminded"]
    again = conn_for().on("SELECT balance, next_due_date", {"balance": Decimal("1680.00"), "next_due_date": None})
    assert (await inv.remind(again, SALES, "x"))["reminded"] is False


# ── Case cancel / reopen, reads ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_case_cancel_cascade_reopen_and_badge() -> None:
    conn = ScriptedConn(
        [
            ("SET deleted_at = now() WHERE trip_case_id", [{"id": "d1", "invoice_number": "INV-000002"}]),
            (
                "status IN ('sent', 'partially_paid', 'paid')",
                {"id": INV_ID, "invoice_number": "INV-000001", "status": "sent"},
            ),  # fmt: skip
        ]
    )
    assert await inv.on_case_cancelled(conn, case_id=CASE_ID, case_code="TC-000001") == {
        "deleted_drafts": 1,
        "to_settle": 1,
    }
    assert conn.sql(NOTIFY_STAFF)[0][1] == "invoice.case_cancelled"
    quiet = ScriptedConn()
    assert await inv.on_case_cancelled(quiet, case_id=CASE_ID, case_code="TC-000001") == {
        "deleted_drafts": 0,
        "to_settle": 0,
    }
    assert quiet.sql(AUDIT) == []
    assert await inv.blocks_reopen(ScriptedConn([(LIVE, {"invoice_number": "INV-000001"})]), CASE_ID) == "INV-000001"
    assert await inv.blocks_reopen(ScriptedConn([("p.verified_at IS NULL", "PMT-000001")]), CASE_ID) == "PMT-000001"
    assert await inv.money_badge(ScriptedConn(), CASE_ID) is None
    assert (await inv.money_badge(ScriptedConn([("b.unverified_count FROM", {"status": "sent"})]), CASE_ID)) == {
        "status": "sent"
    }


def summary_row(**over: Any) -> dict[str, Any]:
    return {
        "id": INV_ID,
        "invoice_number": "INV-000001",
        "status": "sent",
        "document_type": "proforma",
        "bill_to_kind": "customer",
        "currency": "USD",
        "total_amount": "2400.00",
        "trip_case_id": CASE_ID,
        "trip_case_code": "TC-000001",
        "trip_case_status": "invoice_sent",
        "trip_case_channel": "b2c",
        "trip_start_date": None,
        "customer_id": "cus-1",
        "customer_code": "CUS-000001",
        "customer_name": "Ann",
        "bill_to_name": "Ann",
        "bill_to_email": None,
        "bill_to_address": None,
        "notes": None,
        "terms": None,
        "issued_at": None,
        "sent_by": "u-1",
        "sent_by_name": "Sales One",
        "cancelled_at": None,
        "cancelled_by": None,
        "cancel_reason": None,
        "deleted_at": None,
        "created_by": "u-1",
        "created_at": None,
        "updated_at": None,
        "version_id": "ver-1",
        "version_number": 1,
        "version_title": None,
        "fx_rate_locked": "1",
        "verified_received": "0.00",
        "verified_refunded": "0.00",
        "net_paid": "0.00",
        "unverified_amount": "0.00",
        "unverified_count": 0,
        "balance": "2400.00",
        "next_due_date": None,
        "overdue_amount": "720.00",
        "oldest_overdue_date": None,
        **over,
    }


@pytest.mark.asyncio
async def test_list_invoices_filters() -> None:
    conn = ScriptedConn([("SELECT count(*)", 1), (INVOICE_ROW, [summary_row()])])
    out = await inv.list_invoices(conn, SALES, status="open", overdue=True, case=CASE_ID, q="ann",
                                  start=TODAY, end=TODAY)  # fmt: skip
    assert (
        out["total"] == 1 and out["items"][0]["overdue"] is True and out["items"][0]["trip_case"]["code"] == "TC-000001"
    )
    (count_sql, args) = next((s, a) for s, a in conn.calls if "SELECT count(*)" in s)
    assert "i.status IN ('sent', 'partially_paid')" in count_sql and "b.overdue_amount > 0" in count_sql
    assert args[0] == CASE_ID and args[1] == "%ann%"
    conn = ScriptedConn([("SELECT count(*)", 0)])
    await inv.list_invoices(conn, SALES, status="paid", overdue=False, case="tc-000001")
    assert conn.calls[0][1] == ("paid", "TC-000001")
    for kw in ({"status": "nope"}, {"limit": 0}):
        with pytest.raises(Invalid):
            await inv.list_invoices(ScriptedConn(), SALES, **kw)
    no_customer = inv.summary_out(summary_row(customer_id=None, sent_by=None, overdue_amount="0.00"))
    assert no_customer["customer"] is None and no_customer["sent_by"] is None and no_customer["overdue"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "live", "version", "reason"),
    [
        (case_row(channel="b2b"), None, None, "b2b_no_invoice"),
        (case_row(), {"id": INV_ID, "invoice_number": "INV-000001", "status": "sent"}, None, "invoice_exists"),
        (case_row(status="building"), None, None, "case_not_invoiceable"),
        (case_row(), None, None, "no_approved_version"),
        (
            case_row(),
            None,
            {"id": "v", "version_number": 1, "title": None, "total_price": None, "currency": None},
            "version_not_priced",
        ),  # fmt: skip
        (
            case_row(),
            None,
            {"id": "v", "version_number": 1, "title": "T", "total_price": Decimal("2400"), "currency": "USD"},
            None,
        ),  # fmt: skip
    ],
)
async def test_case_billing_reasons(
    case: dict[str, Any], live: dict[str, Any] | None, version: dict[str, Any] | None, reason: str | None
) -> None:
    conn = conn_for(case=case).on(LIVE, live).on(APPROVED, version).on("i.status = 'cancelled' ORDER BY", [])
    out = await inv.for_case(conn, SALES, "TC-000001")
    assert out["reason"] == reason and out["invoiceable"] is (reason is None)
    assert (out["defaults"] is not None) is bool(version and version["total_price"])
    assert (out["invoice"] is not None) is bool(live)


@pytest.mark.asyncio
async def test_summary_kpis_per_currency_and_usd() -> None:
    rows = [
        {"currency": "USD", "fx_rate_locked": Decimal(1), "status": "sent", "balance": Decimal("100"),
         "overdue_amount": Decimal("50"), "received": Decimal("0"), "unverified_amount": Decimal("0"),
         "unverified_count": 0},
        {"currency": "EUR", "fx_rate_locked": Decimal("0.5"), "status": "partially_paid", "balance": Decimal("10"),
         "overdue_amount": Decimal("0"), "received": Decimal("5"), "unverified_amount": Decimal("3"),
         "unverified_count": 1},
        {"currency": "GBP", "fx_rate_locked": None, "status": "paid", "balance": Decimal("0"),
         "overdue_amount": Decimal("0"), "received": Decimal("7"), "unverified_amount": Decimal("0"),
         "unverified_count": 0},
        {"currency": "USD", "fx_rate_locked": None, "status": "draft", "balance": Decimal("9"),
         "overdue_amount": Decimal("0"), "received": Decimal("0"), "unverified_amount": Decimal("0"),
         "unverified_count": 0},
    ]  # fmt: skip
    conn = conn_for().on("JOIN booking.v_invoice_balance b ON b.invoice_id = i.id WHERE i.deleted_at IS NULL", rows)
    out = await inv.summary(conn)
    assert out["outstanding"]["by_currency"] == [
        {"currency": "EUR", "amount": "10"},
        {"currency": "USD", "amount": "100"},
    ]
    assert out["outstanding"]["usd"] == "120.00" and out["overdue"]["count"] == 1
    assert out["received_this_month"]["unconverted"] == 1 and out["awaiting_verification"]["count"] == 1
    assert out["invoices"]["draft"] == 1 and out["month_start"] == date(2026, 11, 1)


# ── Payments ─────────────────────────────────────────────────────────────────

RECEIPT = {"kind": "receipt", "amount": Decimal("720"), "method": "bank_transfer", "received_on": TODAY,
           "reference": "TX-1"}  # fmt: skip


@pytest.mark.asyncio
async def test_record_replay_conflict_and_refusals() -> None:
    sha = money.request_hash(pay._body_for_hash(RECEIPT))
    out, replayed = await pay.record(conn_for().on(STORED_PAY, {"id": PAY_ID, "request_sha256": sha}), SALES, "x",
                                     RECEIPT, KEY)  # fmt: skip
    assert replayed and out == {"id": PAY_ID, "one": True, "invoice": {}}
    with pytest.raises(Conflict) as exc:
        await pay.record(conn_for().on(STORED_PAY, {"id": PAY_ID, "request_sha256": "0" * 64}), SALES, "x", RECEIPT,
                         KEY)  # fmt: skip
    assert exc.value.code == "idempotency_conflict"
    with pytest.raises(Conflict) as exc:
        await pay.record(conn_for(invoice=inv_row(status="draft")), SALES, "x", RECEIPT, KEY)
    assert exc.value.code == "invoice_not_payable"
    cases = [
        ({**RECEIPT, "method": "cheque"}, "invalid_payment"),
        ({**RECEIPT, "amount": "0"}, "invalid_amount"),
        ({**RECEIPT, "received_on": TODAY + timedelta(days=5)}, "invalid_received_on"),
        ({**RECEIPT, "reference": "4111 1111 1111 1111"}, "card_number_not_allowed"),
        ({**RECEIPT, "note": "card 5555555555554444"}, "card_number_not_allowed"),
        ({**RECEIPT, "received_amount": "1"}, "received_pair"),
        ({**RECEIPT, "received_amount": "0", "received_currency": "vnd"}, "invalid_amount"),
    ]
    for body, code in cases:
        with pytest.raises(Invalid) as exc:
            await pay.record(conn_for(), SALES, "x", body, KEY)
        assert exc.value.code == code, body
    refund = conn_for().on(NET, Decimal("100"))
    with pytest.raises(Conflict) as exc:
        await pay.record(refund, SALES, "x", {**RECEIPT, "kind": "refund"}, KEY)
    assert exc.value.code == "refund_exceeds_paid"
    dup = conn_for().on(NET, Decimal("0")).on("lower(reference) = lower($2)", "PMT-000009")
    with pytest.raises(Conflict) as exc:
        await pay.record(dup, SALES, "x", RECEIPT, KEY)
    assert exc.value.code == "duplicate_reference" and exc.value.extra["payment"] == "PMT-000009"
    bad = conn_for().on(NET, Decimal("0")).on(INSERT_PAY, asyncpg.CheckViolationError("currency"))
    with pytest.raises(Conflict):
        await pay.record(bad, SALES, "x", {**RECEIPT, "allow_duplicate_reference": True}, KEY)


@pytest.mark.asyncio
async def test_record_writes_the_payment_audit_and_a_staff_notice() -> None:
    conn = conn_for().on(NET, Decimal("0")).on(INSERT_PAY, PAY_ID)
    body = {**RECEIPT, "method": "cash", "reference": None, "note": "first", "received_amount": "18000000",
            "received_currency": "vnd"}  # fmt: skip
    out, replayed = await pay.record(conn, SALES, "INV-000001", body, KEY)
    assert out == {"id": PAY_ID, "one": True, "invoice": {}} and not replayed
    (args,) = conn.sql(INSERT_PAY)
    assert args[8:10] == (Decimal("18000000.00"), "VND")
    assert audits(conn) == ["payment.recorded"]
    after = json.loads(conn.sql(AUDIT)[0][7])
    assert after["note"] == "(set)" and after["reference"] is None
    assert conn.sql(NOTIFY_STAFF)[0][1] == "payment.recorded"


@pytest.mark.asyncio
async def test_update_payment() -> None:
    with pytest.raises(Conflict):
        await pay.update(conn_for(payment=pay_row(state="verified")), SALES, "PMT-000001", {"note": "x"})
    with pytest.raises(Invalid):
        await pay.update(conn_for(), SALES, "PMT-000001", {"reference": "4111111111111111"})
    conn = conn_for()
    out = await pay.update(conn, SALES, "PMT-000001", {"reference": "TX-1", "note": " checked "})
    assert out["changed"] == ["note"] and audits(conn) == ["payment.updated"]
    assert (await pay.update(conn_for(), SALES, "PMT-000001", {}))["changed"] == []


@pytest.mark.asyncio
async def test_verify_branches() -> None:
    assert await pay.verify(conn_for(payment=pay_row(state="verified")), ADMIN, "x") == {
        "id": PAY_ID,
        "one": True,
        "invoice": {},
    }
    with pytest.raises(Conflict):
        await pay.verify(conn_for(payment=pay_row(state="voided")), ADMIN, "x")
    with pytest.raises(Forbidden) as exc:
        await pay.verify(conn_for(), SALES, "x")
    assert exc.value.code == "maker_checker"
    with pytest.raises(Conflict):
        await pay.verify(conn_for(invoice=inv_row(status="cancelled")), ADMIN, "x")
    with pytest.raises(Invalid) as exc:
        await pay.verify(conn_for(payment=pay_row(evidence_file_id=None)), ADMIN, "x")
    assert exc.value.code == "evidence_required"
    refund = conn_for(payment=pay_row(kind="refund", method="cash")).on(NET, Decimal("10"))
    with pytest.raises(Conflict) as exc:
        await pay.verify(refund, ADMIN, "x")
    assert exc.value.code == "refund_exceeds_paid"
    bad = conn_for().on("SET verified_by = $2", asyncpg.CheckViolationError("maker"))
    with pytest.raises(Conflict):
        await pay.verify(bad, ADMIN, "x")


@pytest.mark.asyncio
async def test_verify_settles_advances_and_flags_overpayment() -> None:
    conn = conn_for(case_status="invoice_sent", invoice_status_for_track="paid")
    conn.on("SELECT i.status, b.balance FROM booking.invoices i", {"status": "paid", "balance": Decimal("-5.00")})
    conn.on("SELECT status FROM booking.invoices WHERE id", "sent")
    await pay.verify(conn, ADMIN, "PMT-000001")
    assert audits(conn) == ["payment.verified", "trip_case.status_changed"]
    assert conn.sql(CASE_UPDATE) == [(CASE_ID, "paid")]
    assert [r[1] for r in conn.sql(NOTIFY_STAFF)] == ["invoice.overpaid"]


@pytest.mark.asyncio
async def test_actors_come_from_the_principal_never_the_body() -> None:
    """Review S253: recorded_by / verified_by / voided_by are the signed-in staff member; a body naming someone else
    changes nothing (and the HTTP models refuse such a field outright)."""
    from pydantic import ValidationError  # noqa: PLC0415

    from app.routers import billing  # noqa: PLC0415

    conn = conn_for().on(NET, Decimal("0")).on(INSERT_PAY, PAY_ID)
    body = {**RECEIPT, "recorded_by": "u-2", "verified_by": "u-2", "verified_at": "2026-11-01"}
    await pay.record(conn, SALES, "INV-000001", body, KEY)
    (args,) = conn.sql(INSERT_PAY)
    assert args[12] == SALES.cis_user_id and "u-2" not in args[:12]

    conn = conn_for(case_status="invoice_sent", invoice_status_for_track="partially_paid")
    conn.on("SELECT i.status, b.balance FROM booking.invoices i", {"status": "partially_paid", "balance": Decimal(1)})
    conn.on("SELECT status FROM booking.invoices WHERE id", "sent")
    await pay.verify(conn, ADMIN, "PMT-000001")
    assert conn.sql("SET verified_by = $2") == [(PAY_ID, ADMIN.cis_user_id)]

    conn = conn_for().on("SELECT status FROM booking.invoices WHERE id", "sent")
    await pay.void(conn, ADMIN, "PMT-000001", "typo")
    assert conn.sql("SET voided_at = now(), voided_by = $2") == [(PAY_ID, ADMIN.cis_user_id, "typo")]

    for model, data in (
        (billing.NewPayment, {"amount": "1", "method": "cash", "received_on": "2026-11-01", "recorded_by": "u-2"}),
        (billing.PaymentChanges, {"verified_by": "u-2"}),
        (billing.ReasonIn, {"reason": "x", "voided_by": "u-2"}),
    ):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            model.model_validate(data)


def test_verify_route_needs_payment_verify() -> None:
    """Review S253: POST /payments/{ref}/verify depends on require("payment.verify") (admin only, never sales)."""
    from app.auth.staff import PERMISSIONS  # noqa: PLC0415
    from app.routers import billing  # noqa: PLC0415

    (route,) = [r for r in billing.router.routes if getattr(r, "path", "").endswith("/payments/{ref}/verify")]
    staff_dep = next(d for d in route.dependant.dependencies if d.name == "staff")
    gate = staff_dep.call
    closure = dict(zip(gate.__code__.co_freevars, (c.cell_contents for c in gate.__closure__ or ()), strict=True))
    assert "payment.verify" in closure.values()
    assert PERMISSIONS["payment.verify"] == frozenset({"admin"})


def test_adjustment_amount_in_minor_units() -> None:
    """A line amount must fit the currency (1.5 x 1001 VND = 1501.50 could never be scheduled in whole VND)."""
    line = {"description": "Extra", "reason": "asked", "quantity": "1.5", "unit_price": "1001"}
    with pytest.raises(Invalid, match="more decimals than VND allows"):
        inv._adjustments([line], "VND")
    (out,) = inv._adjustments([{**line, "unit_price": "1000"}], "VND")
    assert out["amount"] == Decimal("1500.00")
    (out,) = inv._adjustments([{**line, "unit_price": "-0.05", "quantity": "0.5"}], "USD")
    assert out["amount"] == Decimal("-0.03")


@pytest.mark.asyncio
async def test_void_branches_and_balance_reopened_after_departure() -> None:
    with pytest.raises(Invalid):
        await pay.void(conn_for(), SALES, "x", "")
    assert await pay.void(conn_for(payment=pay_row(state="voided")), SALES, "x", "r") == {
        "id": PAY_ID,
        "one": True,
        "invoice": {},
    }
    with pytest.raises(Forbidden):
        await pay.void(conn_for(payment=pay_row(state="verified")), SALES, "x", "r")
    bad = conn_for().on("SET voided_at = now()", asyncpg.CheckViolationError("final"))
    with pytest.raises(Conflict):
        await pay.void(bad, SALES, "x", "r")
    # The payments of a cancelled invoice are final (its net verified paid stays 0).
    for state in ("recorded", "verified"):
        with pytest.raises(Conflict) as exc:
            await pay.void(conn_for(invoice=inv_row(status="cancelled"), payment=pay_row(state=state)), ADMIN, "x", "r")
        assert exc.value.code == "invoice_closed"
    # Voiding a verified receipt never takes the net paid below 0: void the verified refund first.
    overdraw = conn_for(payment=pay_row(state="verified")).on(NET, Decimal("0"))
    with pytest.raises(Conflict) as exc:
        await pay.void(overdraw, ADMIN, "x", "r")
    assert exc.value.code == "void_would_overdraw"
    assert overdraw.sql("SET voided_at = now()") == []
    # A verified refund may be voided whatever the net (it only raises it); the net is not even read.
    refund = conn_for(payment=pay_row(state="verified", kind="refund"))
    await pay.void(refund, ADMIN, "x", "wrong case")
    assert refund.sql(NET) == [] and len(refund.sql("SET voided_at = now()")) == 1
    travelling = conn_for(payment=pay_row(state="verified"), case_status="on_trip").on(NET, Decimal("720"))
    travelling.on("SELECT status FROM booking.invoices WHERE id", "partially_paid")
    await pay.void(travelling, ADMIN, "x", "bounced")
    assert travelling.sql(CASE_UPDATE) == []
    assert [r[1] for r in travelling.sql(NOTIFY_STAFF)] == ["invoice.balance_reopened"]
    cancelled = conn_for(case_status="cancelled").on("SELECT status FROM booking.invoices WHERE id", "sent")
    await pay.void(cancelled, SALES, "x", "typo")
    assert cancelled.sql(CASE_UPDATE) == []


@pytest.mark.asyncio
async def test_payment_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = ScriptedConn([("SELECT count(*)", 1), (PAYMENT_ROW, [pay_row()])])
    out = await pay.list_payments(conn, SALES, state="recorded", kind="receipt", method="cash", invoice="inv-000001",
                                  case=CASE_ID, q="tx", start=TODAY, end=TODAY)  # fmt: skip
    assert out["total"] == 1 and out["items"][0]["invoice"]["number"] == "INV-000001"
    assert conn.calls[0][1][:5] == ("recorded", "receipt", "cash", "INV-000001", CASE_ID)
    by_uuid = ScriptedConn([("SELECT count(*)", 0)])
    await pay.list_payments(by_uuid, SALES, invoice=INV_ID, case="tc-000001")
    assert by_uuid.calls[0][1] == (INV_ID, "TC-000001")
    for kw in ({"state": "x"}, {"limit": 500}):
        with pytest.raises(Invalid):
            await pay.list_payments(ScriptedConn(), SALES, **kw)
    listed = await pay.for_invoice(ScriptedConn([(PAYMENT_ROW, [pay_row()])]), SALES, INV_ID)
    assert listed[0]["actions"]["edit"] and not listed[0]["actions"]["verify"]
    assert listed[0]["actions"]["void"]
    closed = await pay.for_invoice(
        ScriptedConn([(PAYMENT_ROW, [pay_row(state="verified", invoice_status="cancelled")])]), ADMIN, INV_ID
    )
    assert not closed[0]["actions"]["void"]  # a cancelled invoice's payments are final
    with pytest.raises(NotFound):
        await pay.get_payment(ScriptedConn(), SALES, "PMT-000404")
    reads = ScriptedConn([(PAYMENT_ROW, pay_row()), ("b.balance::text AS balance FROM booking.invoices i",
                                                      {"currency": "USD", "balance": "1680.00"})])  # fmt: skip
    out = await pay.get_payment(reads, SALES, "PMT-000001")
    assert out["invoice"] == {"currency": "USD", "balance": "1680.00"}


@pytest.mark.asyncio
async def test_real_detail_and_payment_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()
    conn = ScriptedConn(
        [
            (INVOICE_ROW, summary_row()),
            ("FROM booking.invoice_items WHERE invoice_id", [{"id": "l1", "amount": Decimal("2400.00")}]),
            ("FROM booking.v_payment_schedule_status s", []),
            (PAYMENT_ROW, [pay_row()]),
            ("FROM booking.ops_files f", []),
            (SETTINGS, _setting),
        ]
    )
    out = await inv.detail(conn, ADMIN, INV_ID)
    assert out["items"][0]["amount"] == "2400.00" and out["issuer"] == {"name": "AA"}
    assert out["payments"][0]["actions"]["verify"] and out["actions"]["record_payment"]
    with pytest.raises(NotFound):
        await inv.detail(ScriptedConn(), ADMIN, INV_ID)
    one = await pay._one(ScriptedConn([(PAYMENT_ROW, pay_row()), ("FROM booking.ops_files f", [])]), ADMIN, PAY_ID)
    assert one["ref"] == "PMT-000001" and one["files"] == []
    assert inv.jsonable({"a": [Decimal("1.50")]}) == {"a": ["1.50"]}
    assert not inv.actions(summary_row(status="sent"), CONTRACTING)["record_payment"]
