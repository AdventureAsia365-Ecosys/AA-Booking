"""AA-682 S4 (bk-030) supplier bills and payouts without a database: the Python branches of
``app.ops.supplier_bills`` (actions and approve blockers, idempotency replay / conflict, refusals before the database,
maker-checker, payout limits, card refusal, cancel and void, lists, KPIs, billable bookings, the cost vs billed panel),
the bill / payout document branches of ``app.ops.files``, the ``supplier_bill.due`` reminders of
``app.ops.lifecycle``, the permissions and the routes, driven through :class:`ScriptedConn`. The SQL itself (guards,
settle, view) is covered on a real PostgreSQL in tests/integration/test_bk030_*.py."""

from __future__ import annotations

import base64
import json
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import asyncpg
import pytest

from app.auth.staff import PERMISSIONS, PREVIEW_PERMISSIONS, Staff
from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.ops import files as ops_files
from app.ops import lifecycle, money
from app.ops import supplier_bills as bills
from app.routers import supplier_bills as router
from tests.unit.fakes import ScriptedConn

CONTRACTING = Staff("c-1", None, "Contracting One", "contracting", True, "reviewer")
CHECKER = Staff("c-2", None, "Contracting Two", "contracting", True, "reviewer")
ADMIN = Staff("a-1", None, "Admin", "admin", True, "admin")
SALES = Staff("s-1", None, "Sales", "sales", True, "reviewer")
CONTENT = Staff("w-1", None, "Writer", "content_creator", True, "content")
BILL_ID = "00000000-0000-4000-8000-0000000f0001"
SUP_ID = "00000000-0000-4000-8000-0000000a0001"
SB_ID = "00000000-0000-4000-8000-0000000b0001"
SB2_ID = "00000000-0000-4000-8000-0000000b0002"
CASE_ID = "00000000-0000-4000-8000-0000000c0001"
PAYOUT_ID = "00000000-0000-4000-8000-0000000e0001"
FILE_1 = "00000000-0000-4000-8000-0000000000f1"
FILE_2 = "00000000-0000-4000-8000-0000000000f2"
FILE_3 = "00000000-0000-4000-8000-0000000000f3"
TODAY = date(2026, 11, 2)
KEY = "key-123456"

BILL_ROW = "FROM booking.supplier_bills b JOIN booking.suppliers s"
LOCK = "SELECT 1 FROM booking.supplier_bills WHERE id = $1::uuid FOR UPDATE"
LINES = "FROM booking.supplier_bill_items it JOIN booking.supplier_bookings sb ON sb.id = it.supplier_booking_id JOIN"
PAYOUTS = "FROM booking.supplier_payouts o LEFT JOIN"
FILES = "FROM booking.ops_files f"
STORED_BILL = "request_sha256 FROM booking.supplier_bills WHERE idempotency_key"
STORED_PAYOUT = "request_sha256 FROM booking.supplier_payouts WHERE bill_id"
SUPPLIER = "company_name FROM booking.suppliers WHERE id"
CASE = "FROM booking.trip_cases WHERE"
INSERT_BILL = "INSERT INTO booking.supplier_bills"
INSERT_LINE = "INSERT INTO booking.supplier_bill_items"
INSERT_PAYOUT = "INSERT INTO booking.supplier_payouts"
AUDIT = "INSERT INTO booking.audit_logs"
NOTIFY_STAFF = "VALUES ('staff', NULL, $1"
STATUS_AFTER = "SELECT status FROM booking.supplier_bills WHERE id"


def bill_row(**over: Any) -> dict[str, Any]:
    return {
        "id": BILL_ID,
        "code": "BIL-000001",
        "supplier_id": SUP_ID,
        "supplier_name": "Delta Hotels",
        "trip_case_id": CASE_ID,
        "trip_case_code": "TC-000001",
        "trip_case_channel": "b2c",
        "bill_number": "D-42",
        "currency": "USD",
        "amount": "380.00",
        "issue_date": TODAY,
        "due_date": TODAY + timedelta(days=20),
        "status": "received",
        "notes": None,
        "document_file_id": None,
        "created_by": CONTRACTING.cis_user_id,
        "created_by_name": "Contracting One",
        "approved_by": None,
        "approved_by_name": None,
        "approved_at": None,
        "paid_at": None,
        "cancelled_by": None,
        "cancelled_by_name": None,
        "cancelled_at": None,
        "cancel_reason": None,
        "created_at": None,
        "updated_at": None,
        "paid_amount": "0.00",
        "balance": "380.00",
        "live_payouts": 0,
        "line_count": 2,
        "lines_amount": "380.00",
        "overdue": False,
        **over,
    }


def payout_row(**over: Any) -> dict[str, Any]:
    return {
        "id": PAYOUT_ID,
        "code": "SPO-000001",
        "bill_id": BILL_ID,
        "amount": "100.00",
        "currency": "USD",
        "method": "bank_transfer",
        "paid_on": TODAY,
        "reference": "TT-1",
        "note": None,
        "evidence_file_id": None,
        "state": "live",
        "recorded_by": ADMIN.cis_user_id,
        "recorded_by_name": "Admin",
        "recorded_at": None,
        "voided_by": None,
        "voided_by_name": None,
        "voided_at": None,
        "void_reason": None,
        **over,
    }


def conn_for(bill: dict[str, Any] | None = None, **rules: Any) -> ScriptedConn:
    conn = ScriptedConn(
        [
            (BILL_ROW, bill if bill is not None else bill_row()),
            (LINES, []),
            (PAYOUTS, []),
            (FILES, []),
            (SUPPLIER, {"id": SUP_ID, "company_name": "Delta Hotels"}),
            (CASE, CASE_ID),
            (INSERT_BILL, {"id": BILL_ID, "code": "BIL-000001"}),
            (INSERT_PAYOUT, PAYOUT_ID),
            (STATUS_AFTER, "approved"),
        ]
    )
    for fragment, answer in rules.items():
        conn.on(fragment, answer)
    return conn


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch: pytest.MonkeyPatch) -> None:
    async def tz(conn: Any) -> str:
        return "Asia/Ho_Chi_Minh"

    async def today(conn: Any, zone: str) -> date:
        return TODAY

    monkeypatch.setattr(lifecycle, "ops_timezone", tz)
    monkeypatch.setattr(lifecycle, "ops_today", today)


def audits(conn: ScriptedConn) -> list[str]:
    return [args[3] for args in conn.sql(AUDIT)]


def bill_body(**over: Any) -> dict[str, Any]:
    return {
        "supplier_id": SUP_ID,
        "trip_case": "TC-000001",
        "bill_number": "D-42",
        "currency": "USD",
        "amount": Decimal("380.00"),
        "issue_date": TODAY,
        "due_date": TODAY + timedelta(days=20),
        "notes": "rooms",
        "lines": [
            {"supplier_booking_id": SB_ID, "amount": Decimal("300.00")},
            {"supplier_booking_id": SB2_ID, "amount": Decimal("80.00")},
        ],
        **over,
    }


# ── Permissions and shapes ───────────────────────────────────────────────────


def test_permissions_follow_the_design() -> None:
    assert PERMISSIONS["bill.read"] == frozenset({"admin", "sales", "contracting"})
    assert PERMISSIONS["bill.write"] == PERMISSIONS["bill.approve"] == frozenset({"admin", "contracting"})
    assert PERMISSIONS["payout.write"] == frozenset({"admin"})
    assert not {"bill.read", "bill.write", "bill.approve", "payout.write"} & PREVIEW_PERMISSIONS
    assert not Staff("a-9", None, "A", "admin", True, "admin", preview=True).can("bill.read")
    assert not CONTENT.can("bill.read") and not SALES.can("bill.write") and not CONTRACTING.can("payout.write")


def test_routes_depend_on_their_permission() -> None:
    wanted = {
        ("GET", "/booking/supplier-bills"): "bill.read",
        ("POST", "/booking/supplier-bills"): "bill.write",
        ("POST", "/booking/supplier-bills/{bill}/approve"): "bill.approve",
        ("POST", "/booking/supplier-bills/{bill}/cancel"): "bill.write",
        ("POST", "/booking/supplier-bills/{bill}/payouts"): "payout.write",
        ("POST", "/booking/supplier-payouts/{payout}/void"): "payout.write",
        ("GET", "/booking/trip-cases/{ref}/supplier-costs"): "bill.read",
    }
    seen = {}
    for route in router.router.routes:
        for method in route.methods:
            dep = route.dependant.dependencies[0] if route.dependant.dependencies else None
            perms = {
                c.cell_contents
                for d in route.dependant.dependencies
                for c in (getattr(d.call, "__closure__", None) or ())
                if isinstance(c.cell_contents, str) and "." in c.cell_contents
            }
            seen[(method, route.path)] = perms
            assert dep is not None
    for key, perm in wanted.items():
        assert perm in seen[key], (key, seen.get(key))


def test_actions_and_approve_blockers() -> None:
    received = bills.bill_out(bill_row(), CHECKER)
    assert received["approve_blockers"] == [] and received["actions"] == {
        "edit": True,
        "attach": True,
        "approve": True,
        "cancel": True,
        "pay": False,
    }
    mine = bills.bill_out(bill_row(lines_amount="370.00"), CONTRACTING)
    assert mine["approve_blockers"] == ["maker_checker", "lines_mismatch"] and not mine["actions"]["approve"]
    assert bills.bill_out(bill_row(line_count=0), SALES)["approve_blockers"] == ["permission", "no_lines"]
    approved = bills.bill_out(bill_row(status="approved", live_payouts=1, balance="280.00"), ADMIN)
    assert approved["actions"] == {"edit": False, "attach": False, "approve": False, "cancel": False, "pay": True}
    assert approved["supplier"] == {"id": SUP_ID, "name": "Delta Hotels"}
    assert approved["created_by"] == {"id": CONTRACTING.cis_user_id, "name": "Contracting One"}
    assert approved["approved_by"] is None and approved["trip_case"]["code"] == "TC-000001"
    paid_by_creator = bills.bill_out(bill_row(status="approved", created_by=ADMIN.cis_user_id), ADMIN)
    assert paid_by_creator["actions"]["pay"] is False
    statement = bills.bill_out(bill_row(trip_case_id=None, trip_case_code=None, trip_case_channel=None), SALES)
    assert statement["trip_case"] is None and statement["actions"]["cancel"] is False
    p = bills.payout_out(payout_row(), ADMIN)
    assert p["needs_evidence"] and p["actions"] == {"void": True, "attach": True}
    assert p["recorded_by"] == {"id": ADMIN.cis_user_id, "name": "Admin"} and p["voided_by"] is None
    v = bills.payout_out(payout_row(state="voided", evidence_file_id="f"), CHECKER, [{"id": "f"}])
    assert not v["needs_evidence"] and v["actions"] == {"void": False, "attach": False} and v["files"] == [{"id": "f"}]


def test_jsonable_and_text_helpers() -> None:
    assert bills.jsonable({"a": [Decimal("1.50")]}) == {"a": ["1.50"]}
    assert bills._text("  a   b ", 3) == "a b" and bills._text(None, 3) is None and bills._text("   ", 3) is None
    assert bills._note("  x  ") == "x" and bills._note(None) is None and bills._note("  ") is None
    assert bills._json('[{"a": 1}]') == [{"a": 1}] and bills._json([1]) == [1]
    with pytest.raises(Invalid) as exc:
        bills._no_card("the reference", "4111-1111-1111-1111")
    assert exc.value.code == "card_number_not_allowed"


def test_lines_and_dates_rules() -> None:
    out = bills._lines([{"supplier_booking_id": SB_ID, "amount": "300"}], "USD")
    assert out == [{"supplier_booking_id": SB_ID, "amount": Decimal("300.00")}]
    for raw, code in (
        ([{"supplier_booking_id": "SB-1", "amount": "1"}], "invalid_line"),
        (
            [{"supplier_booking_id": SB_ID, "amount": "1"}, {"supplier_booking_id": SB_ID, "amount": "2"}],
            "invalid_line",
        ),
        ([{"supplier_booking_id": SB_ID, "amount": "0"}], "invalid_line"),
        ([{"supplier_booking_id": SB_ID, "amount": "1.5"}], "invalid_amount"),
    ):
        with pytest.raises(Invalid) as exc:
            bills._lines(raw, "VND" if raw[0]["amount"] == "1.5" else "USD")
        assert exc.value.code == code
    many = [{"supplier_booking_id": f"00000000-0000-4000-8000-{i:012d}", "amount": "1"} for i in range(51)]
    with pytest.raises(Invalid) as exc:
        bills._lines(many, "USD")
    assert exc.value.code == "too_many_lines"
    assert bills._dates(TODAY, TODAY, TODAY) == (TODAY, TODAY)
    for issue, due in (
        (None, TODAY),
        (TODAY + timedelta(days=5), TODAY + timedelta(days=6)),
        (TODAY, TODAY - timedelta(days=1)),
    ):
        with pytest.raises(Invalid):
            bills._dates(issue, due, TODAY)


def test_body_hash_ignores_line_order() -> None:
    a = bills._body_for_hash(bill_body())
    b = bills._body_for_hash(bill_body(lines=list(reversed(bill_body()["lines"]))))
    assert money.request_hash(a) == money.request_hash(b)
    assert money.request_hash(a) != money.request_hash(bills._body_for_hash(bill_body(amount=Decimal("381"))))


# ── Reads ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_detail_hides_net_without_pricing_and_lists_payouts() -> None:
    line = {"supplier_booking_id": SB_ID, "amount": "300.00", "net_amount": "300.00", "net_currency": "USD"}
    conn = conn_for(**{LINES: [line], PAYOUTS: [payout_row()]})
    out = await bills.detail(conn, CONTRACTING, "bil-000001")
    assert out["lines"][0]["net_amount"] == "300.00" and out["payouts"][0]["code"] == "SPO-000001"
    assert conn.sql(BILL_ROW)[0] == ("BIL-000001",)
    no_pricing = Staff("x-1", None, "X", "content_creator", True, "content")
    out = await bills.detail(conn_for(**{LINES: [dict(line)]}), no_pricing, BILL_ID)
    assert out["lines"][0]["net_amount"] is None and out["lines"][0]["net_currency"] is None
    with pytest.raises(NotFound) as exc:
        await bills.detail(ScriptedConn(), CONTRACTING, BILL_ID)
    assert exc.value.code == "supplier_bill_not_found"


@pytest.mark.asyncio
async def test_list_filters_and_paging() -> None:
    conn = conn_for(**{BILL_ROW: [bill_row()], "SELECT count(*)": 1})
    out = await bills.list_bills(
        conn,
        SALES,
        status="open",
        supplier=SUP_ID,
        case="tc-000001",
        overdue=True,
        due_before=TODAY,
        q="d-42",
        limit=10,
        offset=0,
    )
    assert out["total"] == 1 and out["items"][0]["code"] == "BIL-000001"
    flat, args = conn.calls[0]
    assert "b.status IN ('received', 'approved')" in flat and "bal.overdue" in flat and "c2.code = $2" in flat
    assert args == (SUP_ID, "TC-000001", TODAY, "%d-42%")
    conn = conn_for(**{BILL_ROW: [], "SELECT count(*)": 0})
    await bills.list_bills(conn, SALES, status="paid", case=CASE_ID, overdue=False)
    assert "NOT bal.overdue" in conn.calls[0][0] and "b.trip_case_id = $2::uuid" in conn.calls[0][0]
    await bills.list_bills(conn_for(**{BILL_ROW: [], "SELECT count(*)": 0}), SALES, status="all")
    for kwargs, code in (
        ({"status": "bogus"}, "invalid_filter"),
        ({"supplier": "Delta"}, "invalid_filter"),
        ({"limit": 0}, "invalid_page"),
        ({"offset": -1}, "invalid_page"),
    ):
        with pytest.raises(Invalid) as exc:
            await bills.list_bills(ScriptedConn(), SALES, **kwargs)
        assert exc.value.code == code


@pytest.mark.asyncio
async def test_summary_per_currency() -> None:
    rows = [
        {"status": "received", "currency": "USD", "amount": Decimal("10"), "balance": Decimal("10"), "overdue": True},
        {
            "status": "approved",
            "currency": "VND",
            "amount": Decimal("500"),
            "balance": Decimal("300"),
            "overdue": False,
        },
        {"status": "paid", "currency": "USD", "amount": Decimal("5"), "balance": Decimal("0"), "overdue": False},
        {"status": "cancelled", "currency": "USD", "amount": Decimal("5"), "balance": Decimal("0"), "overdue": False},
    ]
    paid = [{"currency": "USD", "amount": Decimal("5"), "n": 1}]
    conn = ScriptedConn([("JOIN booking.v_supplier_bill_balance bal", rows), ("GROUP BY currency", paid)])
    out = await bills.summary(conn)
    assert out["to_approve"] == {"count": 1, "by_currency": [{"currency": "USD", "amount": "10"}]}
    assert out["to_pay"] == {"count": 1, "by_currency": [{"currency": "VND", "amount": "300"}]}
    assert out["overdue"]["count"] == 1 and out["paid_this_month"]["count"] == 1
    assert out["bills"] == {"received": 1, "approved": 1, "paid": 1, "cancelled": 1}
    assert out["month_start"] == TODAY.replace(day=1)
    assert conn.sql("GROUP BY currency")[0] == (TODAY.replace(day=1), TODAY)


@pytest.mark.asyncio
async def test_billable_bookings() -> None:
    row = {
        "id": SB_ID,
        "code": "SB-1",
        "net_amount": "300.00",
        "net_currency": "USD",
        "billed": '[{"currency": "USD", "amount": "100.00"}]',
    }
    conn = ScriptedConn([("FROM booking.supplier_bookings sb JOIN booking.trip_cases tc", [row])])
    out = await bills.billable(conn, CONTRACTING, supplier=SUP_ID, case="tc-000001", q="room")
    assert out["items"][0]["billed"] == [{"currency": "USD", "amount": "100.00"}]
    assert conn.calls[0][1] == (SUP_ID, "TC-000001", "%room%")
    conn = ScriptedConn([("FROM booking.supplier_bookings sb JOIN booking.trip_cases tc", [dict(row)])])
    out = await bills.billable(conn, CONTENT, supplier=SUP_ID, case=CASE_ID)
    assert out["items"][0]["net_amount"] is None and "tc.id = $2::uuid" in conn.calls[0][0]
    with pytest.raises(Invalid):
        await bills.billable(ScriptedConn(), CONTRACTING, supplier="Delta")


@pytest.mark.asyncio
async def test_case_costs_variance() -> None:
    rows = [
        {  # billed as expected
            "id": SB_ID,
            "code": "SB-1",
            "status": "confirmed",
            "item_description": "Room",
            "service_date": TODAY,
            "net_amount": Decimal("300.00"),
            "net_currency": "USD",
            "supplier_id": SUP_ID,
            "supplier_name": "Delta",
            "billed": json.dumps([{"currency": "USD", "amount": "320.00", "bills": ["BIL-1"]}]),
        },
        {  # billed in another currency
            "id": SB2_ID,
            "code": "SB-2",
            "status": "confirmed",
            "item_description": "Kayak",
            "service_date": None,
            "net_amount": Decimal("80.00"),
            "net_currency": "USD",
            "supplier_id": SUP_ID,
            "supplier_name": "Delta",
            "billed": [{"currency": "VND", "amount": "2000000", "bills": ["BIL-2"]}],
        },
        {  # not billed yet
            "id": "sb-3",
            "code": "SB-3",
            "status": "confirmed",
            "item_description": "Guide",
            "service_date": None,
            "net_amount": None,
            "net_currency": None,
            "supplier_id": SUP_ID,
            "supplier_name": "Delta",
            "billed": "[]",
        },
    ]
    conn = ScriptedConn(
        [
            ("FROM booking.supplier_bookings sb JOIN booking.suppliers s", rows),
            (CASE, {"id": CASE_ID, "code": "TC-1", "channel": "b2c"}),
        ]
    )
    out = await bills.case_costs(conn, CONTRACTING, "tc-1")
    first, second, third = out["lines"]
    assert first["variance"] == "20.00" and not first["currency_mismatch"] and not first["unbilled"]
    assert second["variance"] is None and second["currency_mismatch"] is True
    assert third["unbilled"] is True and third["variance"] is None
    assert out["totals"] == [
        {"currency": "USD", "net": "380.00", "billed": "320.00", "variance": "-60.00"},
        {"currency": "VND", "net": "0", "billed": "2000000", "variance": "2000000"},
    ]
    with pytest.raises(Forbidden):
        await bills.case_costs(ScriptedConn(), CONTENT, "TC-1")
    with pytest.raises(NotFound):
        await bills.case_costs(ScriptedConn(), CONTRACTING, CASE_ID)


# ── Create / update ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_writes_bill_lines_and_audit() -> None:
    conn = conn_for()
    out, replayed = await bills.create(conn, CONTRACTING, bill_body(), KEY)
    assert not replayed and out["code"] == "BIL-000001"
    (insert,) = conn.sql(INSERT_BILL)
    assert insert[:8] == (SUP_ID, CASE_ID, "D-42", "USD", Decimal("380.00"), TODAY, TODAY + timedelta(days=20), "rooms")
    assert insert[8] == KEY and insert[10] == CONTRACTING.cis_user_id
    assert [a[1] for a in conn.sql(INSERT_LINE)] == [SB_ID, SB2_ID]
    assert audits(conn) == ["supplier_bill.created"]


@pytest.mark.asyncio
async def test_create_replay_conflict_and_refusals() -> None:
    with pytest.raises(Invalid) as exc:
        await bills.create(conn_for(), CONTRACTING, bill_body(), None)
    assert exc.value.code == "idempotency_key_required"
    sha = money.request_hash(bills._body_for_hash(bill_body()))
    conn = conn_for(**{STORED_BILL: {"id": BILL_ID, "request_sha256": sha}})
    out, replayed = await bills.create(conn, CONTRACTING, bill_body(), KEY)
    assert replayed and conn.sql(INSERT_BILL) == []
    conn = conn_for(**{STORED_BILL: {"id": BILL_ID, "request_sha256": "f" * 64}})
    with pytest.raises(Conflict) as exc:
        await bills.create(conn, CONTRACTING, bill_body(), KEY)
    assert exc.value.code == "idempotency_conflict"
    cases: list[tuple[dict[str, Any], dict[str, Any], type[Exception], str]] = [
        ({"supplier_id": "nope"}, {}, NotFound, "supplier_not_found"),
        ({}, {SUPPLIER: None}, NotFound, "supplier_not_found"),
        ({"currency": "US"}, {}, Invalid, "invalid_currency"),
        ({"amount": Decimal("0")}, {}, Invalid, "invalid_amount"),
        ({"bill_number": "   "}, {}, Invalid, "bill_number_required"),
        ({"due_date": TODAY - timedelta(days=1)}, {}, Invalid, "invalid_dates"),
        ({}, {CASE: None}, NotFound, "trip_case_not_found"),
        ({}, {INSERT_BILL: _unique("supplier_bills_number_uq")}, Conflict, "duplicate_bill_number"),
        ({}, {INSERT_BILL: _unique("supplier_bills_idempotency_uq")}, Conflict, "idempotency_conflict"),
        (
            {},
            {INSERT_LINE: asyncpg.CheckViolationError("supplier booking SB-1 is pending")},
            Conflict,
            "invalid_bill_line",
        ),
        ({}, {INSERT_LINE: asyncpg.ForeignKeyViolationError("fk")}, NotFound, "supplier_booking_not_found"),
    ]
    for body, rules, error, code in cases:
        with pytest.raises(error) as exc:
            await bills.create(conn_for(**rules), CONTRACTING, bill_body(**body), KEY)
        assert exc.value.code == code, (body, rules)
    out, _ = await bills.create(conn_for(), CONTRACTING, bill_body(trip_case=None, lines=[]), KEY)
    assert out["code"] == "BIL-000001"


def _unique(constraint: str) -> asyncpg.UniqueViolationError:
    exc = asyncpg.UniqueViolationError("duplicate")
    exc.constraint_name = constraint  # type: ignore[attr-defined]
    return exc


@pytest.mark.asyncio
async def test_update_received_bill_fields_and_lines() -> None:
    old_lines = [{"supplier_booking_id": SB_ID, "amount": "380.00"}]
    conn = conn_for(**{LINES: old_lines})
    changes = {
        "bill_number": " D-43 ",
        "currency": "usd",
        "amount": Decimal("400"),
        "due_date": TODAY + timedelta(days=30),
        "notes": "x",
        "lines": [{"supplier_booking_id": SB2_ID, "amount": Decimal("400")}],
    }
    out = await bills.update(conn, CONTRACTING, BILL_ID, changes)
    assert out["changed"] == ["amount", "bill_number", "due_date", "notes", "lines"]
    (upd,) = conn.sql("UPDATE booking.supplier_bills SET")
    assert upd[2:] == (Decimal("400.00"), "D-43", TODAY + timedelta(days=30), "x")
    assert conn.sql("DELETE FROM booking.supplier_bill_items") == [(BILL_ID,)]
    assert audits(conn) == ["supplier_bill.updated"]
    assert conn.sql(LOCK)  # the bill row is locked first
    same = conn_for(**{LINES: old_lines})
    out = await bills.update(same, CONTRACTING, BILL_ID, {"lines": [{"supplier_booking_id": SB_ID, "amount": "380"}]})
    assert out["changed"] == [] and audits(same) == [] and same.sql("DELETE FROM") == []
    switched = await bills.update(conn_for(), CONTRACTING, BILL_ID, {"currency": "EUR"})
    assert switched["changed"] == ["currency"]


@pytest.mark.asyncio
async def test_update_refusals() -> None:
    cases: list[tuple[dict[str, Any], dict[str, Any], type[Exception], str]] = [
        (bill_row(status="cancelled"), {"notes": "x"}, Conflict, "supplier_bill_closed"),
        (bill_row(status="approved"), {"amount": Decimal("1")}, Conflict, "supplier_bill_locked"),
        (bill_row(), {"bill_number": "  "}, Invalid, "bill_number_required"),
        (bill_row(), {"currency": "U1"}, Invalid, "invalid_currency"),
        (bill_row(), {"amount": Decimal("0")}, Invalid, "invalid_amount"),
        (bill_row(), {"issue_date": TODAY + timedelta(days=9)}, Invalid, "invalid_dates"),
    ]
    for row, changes, error, code in cases:
        with pytest.raises(error) as exc:
            await bills.update(conn_for(row), CONTRACTING, BILL_ID, changes)
        assert exc.value.code == code
    approved = await bills.update(conn_for(bill_row(status="approved")), CONTRACTING, BILL_ID, {"notes": "ok"})
    assert approved["changed"] == ["notes"]
    for error, code in (
        (_unique("supplier_bills_number_uq"), "duplicate_bill_number"),
        (asyncpg.CheckViolationError("x"), "invalid_supplier_bill"),
    ):
        with pytest.raises(Conflict) as exc:
            await bills.update(
                conn_for(**{"UPDATE booking.supplier_bills SET": error}), CONTRACTING, BILL_ID, {"bill_number": "Z"}
            )
        assert exc.value.code == code


# ── Approve / cancel ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_approve_maker_checker_and_lines() -> None:
    conn = conn_for()
    out = await bills.approve(conn, CHECKER, BILL_ID)
    assert out["code"] == "BIL-000001" and audits(conn) == ["supplier_bill.approved"]
    assert conn.sql("SET status = 'approved'")[0] == (BILL_ID, CHECKER.cis_user_id)
    assert conn.sql(NOTIFY_STAFF)  # payers are told
    with pytest.raises(Forbidden) as exc:
        await bills.approve(conn_for(), CONTRACTING, BILL_ID)
    assert exc.value.code == "maker_checker"
    for row in (bill_row(line_count=0), bill_row(lines_amount="1.00")):
        with pytest.raises(Conflict) as exc:
            await bills.approve(conn_for(row), CHECKER, BILL_ID)
        assert exc.value.code == "bill_not_ready"
    with pytest.raises(Conflict) as exc:
        await bills.approve(conn_for(bill_row(status="cancelled")), CHECKER, BILL_ID)
    assert exc.value.code == "supplier_bill_closed"
    again = conn_for(bill_row(status="paid"))
    await bills.approve(again, CHECKER, BILL_ID)
    assert again.sql("SET status = 'approved'") == []
    with pytest.raises(Conflict) as exc:
        await bills.approve(
            conn_for(**{"SET status = 'approved'": asyncpg.CheckViolationError("sum")}), CHECKER, BILL_ID
        )
    assert exc.value.code == "bill_not_ready"


@pytest.mark.asyncio
async def test_cancel_rules() -> None:
    with pytest.raises(Invalid):
        await bills.cancel(conn_for(), CONTRACTING, BILL_ID, "  ")
    conn = conn_for()
    await bills.cancel(conn, CONTRACTING, BILL_ID, "entered twice")
    assert conn.sql("SET status = 'cancelled'")[0] == (BILL_ID, CONTRACTING.cis_user_id, "entered twice")
    assert audits(conn) == ["supplier_bill.cancelled"]
    done = conn_for(bill_row(status="cancelled"))
    await bills.cancel(done, CONTRACTING, BILL_ID, "again")
    assert done.sql("SET status = 'cancelled'") == []
    for row in (bill_row(status="paid"), bill_row(status="approved", live_payouts=1)):
        with pytest.raises(Conflict) as exc:
            await bills.cancel(conn_for(row), CONTRACTING, BILL_ID, "x")
        assert exc.value.code == "bill_has_payouts"
    with pytest.raises(Conflict) as exc:
        await bills.cancel(
            conn_for(**{"SET status = 'cancelled'": asyncpg.CheckViolationError("x")}), CONTRACTING, BILL_ID, "x"
        )
    assert exc.value.code == "supplier_bill_not_cancellable"


# ── Payouts ──────────────────────────────────────────────────────────────────


def payout_body(**over: Any) -> dict[str, Any]:
    return {
        "amount": Decimal("100.00"),
        "method": "bank_transfer",
        "paid_on": TODAY,
        "reference": "TT-1",
        "note": None,
        **over,
    }


def payout_conn(bill: dict[str, Any] | None = None, **rules: Any) -> ScriptedConn:
    return conn_for(bill or bill_row(status="approved"), **{"WHERE o.id = $1::uuid": payout_row(), **rules})


@pytest.mark.asyncio
async def test_record_payout_writes_and_audits() -> None:
    conn = payout_conn()
    out, replayed = await bills.record_payout(conn, ADMIN, BILL_ID, payout_body(), KEY)
    assert not replayed and out["code"] == "SPO-000001" and out["bill"]["code"] == "BIL-000001"
    (insert,) = conn.sql(INSERT_PAYOUT)
    assert insert[:7] == (BILL_ID, Decimal("100.00"), "USD", "bank_transfer", TODAY, "TT-1", None)
    assert insert[7] == KEY and insert[9] == ADMIN.cis_user_id
    assert audits(conn) == ["supplier_payout.recorded"]


@pytest.mark.asyncio
async def test_record_payout_replay_and_refusals() -> None:
    sha = money.request_hash(bills._payout_hash(payout_body()))
    conn = payout_conn(**{STORED_PAYOUT: {"id": PAYOUT_ID, "request_sha256": sha}})
    out, replayed = await bills.record_payout(conn, ADMIN, BILL_ID, payout_body(), KEY)
    assert replayed and conn.sql(INSERT_PAYOUT) == []
    # A replay is answered even once the bill is paid (read-before-retry).
    paid = payout_conn(bill_row(status="paid"), **{STORED_PAYOUT: {"id": PAYOUT_ID, "request_sha256": sha}})
    assert (await bills.record_payout(paid, ADMIN, BILL_ID, payout_body(), KEY))[1] is True
    cases: list[tuple[dict[str, Any] | None, dict[str, Any], dict[str, Any], type[Exception], str]] = [
        (None, {STORED_PAYOUT: {"id": PAYOUT_ID, "request_sha256": "0" * 64}}, {}, Conflict, "idempotency_conflict"),
        (bill_row(status="received"), {}, {}, Conflict, "bill_not_payable"),
        (bill_row(status="approved", created_by=ADMIN.cis_user_id), {}, {}, Forbidden, "maker_checker"),
        (None, {}, {"method": "crypto"}, Invalid, "invalid_payout"),
        (None, {}, {"amount": Decimal("0")}, Invalid, "invalid_amount"),
        (None, {}, {"amount": Decimal("380.01")}, Conflict, "payout_exceeds_balance"),
        (None, {}, {"paid_on": TODAY + timedelta(days=3)}, Invalid, "invalid_paid_on"),
        (None, {}, {"paid_on": None}, Invalid, "invalid_paid_on"),
        (None, {}, {"reference": "4111 1111 1111 1111"}, Invalid, "card_number_not_allowed"),
        (None, {}, {"note": "card 5500 0000 0000 0004"}, Invalid, "card_number_not_allowed"),
        (
            None,
            {"SELECT code FROM booking.supplier_payouts WHERE method": "SPO-000009"},
            {},
            Conflict,
            "duplicate_reference",
        ),
        (None, {INSERT_PAYOUT: asyncpg.CheckViolationError("exceed")}, {}, Conflict, "invalid_payout"),
    ]
    for row, rules, body, error, code in cases:
        with pytest.raises(error) as exc:
            await bills.record_payout(payout_conn(row, **rules), ADMIN, BILL_ID, payout_body(**body), KEY)
        assert exc.value.code == code, code
    with pytest.raises(Invalid) as exc:
        await bills.record_payout(payout_conn(), ADMIN, BILL_ID, payout_body(), "short")
    assert exc.value.code == "idempotency_key_required"
    dup = payout_conn(**{"SELECT code FROM booking.supplier_payouts WHERE method": "SPO-000009"})
    await bills.record_payout(dup, ADMIN, BILL_ID, payout_body(allow_duplicate_reference=True), KEY)
    assert dup.sql(INSERT_PAYOUT)


@pytest.mark.asyncio
async def test_void_payout() -> None:
    lookup = "bill_id::text AS bill_id FROM booking.supplier_payouts WHERE"
    row = {"id": PAYOUT_ID, "code": "SPO-000001", "amount": Decimal("100"), "currency": "USD", "voided_at": None}
    conn = payout_conn(
        **{lookup: {"id": PAYOUT_ID, "bill_id": BILL_ID}, "voided_at FROM booking.supplier_payouts": row}
    )
    out = await bills.void_payout(conn, ADMIN, "spo-000001", "bank returned it")
    assert out["bill"]["code"] == "BIL-000001" and audits(conn) == ["supplier_payout.voided"]
    assert conn.sql(lookup)[0] == ("SPO-000001",)
    calls = [flat for flat, _ in conn.calls]
    assert calls.index(next(c for c in calls if LOCK in c)) < calls.index(
        next(c for c in calls if "voided_at FROM booking.supplier_payouts" in c)
    )  # bill first, then the payout
    done = payout_conn(
        **{
            lookup: {"id": PAYOUT_ID, "bill_id": BILL_ID},
            "voided_at FROM booking.supplier_payouts": {**row, "voided_at": TODAY},
        }
    )
    await bills.void_payout(done, ADMIN, PAYOUT_ID, "again")
    assert done.sql("SET voided_at = now()") == []
    with pytest.raises(Invalid):
        await bills.void_payout(payout_conn(), ADMIN, PAYOUT_ID, " ")
    with pytest.raises(NotFound):
        await bills.void_payout(payout_conn(), ADMIN, PAYOUT_ID, "x")
    bad = payout_conn(
        **{
            lookup: {"id": PAYOUT_ID, "bill_id": BILL_ID},
            "voided_at FROM booking.supplier_payouts": row,
            "SET voided_at = now()": asyncpg.CheckViolationError("bill"),
        }
    )
    with pytest.raises(Conflict) as exc:
        await bills.void_payout(bad, ADMIN, PAYOUT_ID, "x")
    assert exc.value.code == "payout_not_voidable"


# ── Files of bills and payouts ───────────────────────────────────────────────

PDF = b"%PDF-1.4\n%%EOF\n"
BUCKET = "aa-booking-dev-ops-docs-000000000000"
BILL_PARENT = "FROM booking.supplier_bills b LEFT JOIN booking.trip_cases tc"
PAYOUT_PARENT = "FROM booking.supplier_payouts o JOIN booking.supplier_bills b"


class Store:
    bucket = BUCKET

    def __init__(self) -> None:
        self.puts: list[str] = []

    async def put(self, key: str, data: bytes, content_type: str, sha256: str) -> None:
        self.puts.append(key)

    async def download_url(self, key: str, file_name: str, content_type: str) -> str:
        return "https://signed.example/x"


def bill_parent(**over: Any) -> dict[str, Any]:
    return {
        "id": BILL_ID,
        "code": "BIL-000001",
        "status": "received",
        "trip_case_id": None,
        "voucher_file_id": None,
        "case_code": "-",
        "channel": "b2c",
        **over,
    }


def payout_parent(**over: Any) -> dict[str, Any]:
    return {
        "id": PAYOUT_ID,
        "code": "SPO-000001",
        "status": "live",
        "trip_case_id": CASE_ID,
        "voucher_file_id": None,
        "case_code": "TC-1",
        "channel": "b2c",
        "bill_id": BILL_ID,
        "bill_code": "BIL-000001",
        **over,
    }


def file_conn(**rules: Any) -> ScriptedConn:
    file_row = {
        "id": FILE_1,
        "kind": "supplier_bill",
        "entity_type": "supplier_bill",
        "entity_id": BILL_ID,
        "trip_case_id": None,
        "s3_key": "k",
        "content_type": "application/pdf",
        "bytes": 1,
        "sha256": "0" * 64,
        "file_name": "bill.pdf",
        "status": "active",
        "removed_reason": None,
        "removed_at": None,
        "uploaded_by": None,
        "uploaded_by_name": None,
        "created_at": None,
    }
    conn = ScriptedConn(
        [
            (BILL_PARENT, bill_parent()),
            (PAYOUT_PARENT, payout_parent()),
            ("AS same FROM booking.ops_files", {"active": 0, "same": 0}),
            ("WHERE f.id = $1::uuid", file_row),
        ]
    )
    for fragment, answer in rules.items():
        conn.on(fragment, answer)
    return conn


def _upload_args(kind: str, entity_type: str, entity_id: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "file_name": "doc.pdf",
        "content_type": "application/pdf",
        "data_base64": base64.b64encode(PDF).decode(),
    }


@pytest.mark.asyncio
async def test_upload_bill_document_and_payout_evidence_become_current() -> None:
    conn = file_conn()
    await ops_files.upload(conn, Store(), CONTRACTING, **_upload_args("supplier_bill", "supplier_bill", BILL_ID))
    (insert,) = conn.sql("INSERT INTO booking.ops_files")
    assert insert[4] is None  # a statement bill has no case
    assert conn.sql("UPDATE booking.supplier_bills SET document_file_id = $2::uuid")[0][0] == BILL_ID
    assert any(flat.endswith("FOR UPDATE OF b") for flat, _ in conn.calls)
    conn = file_conn()
    await ops_files.upload(conn, Store(), ADMIN, **_upload_args("payout_evidence", "supplier_payout", PAYOUT_ID))
    assert conn.sql("UPDATE booking.supplier_payouts SET evidence_file_id = $2::uuid")[0][0] == PAYOUT_ID
    flats = [flat for flat, _ in conn.calls]
    assert any("FROM booking.supplier_bills WHERE id = $1::uuid FOR UPDATE" in f for f in flats)  # bill, then payout
    assert any(f.endswith("FOR UPDATE OF o") for f in flats)


@pytest.mark.asyncio
async def test_upload_refusals_for_bills_and_payouts() -> None:
    cases = [
        (
            CONTRACTING,
            {BILL_PARENT: bill_parent(channel="b2b")},
            ("supplier_bill", "supplier_bill", BILL_ID),
            "document_not_allowed_b2b",
        ),
        (
            CONTRACTING,
            {BILL_PARENT: bill_parent(status="approved")},
            ("supplier_bill", "supplier_bill", BILL_ID),
            "supplier_bill_closed",
        ),
        (
            ADMIN,
            {PAYOUT_PARENT: payout_parent(status="voided")},
            ("payout_evidence", "supplier_payout", PAYOUT_ID),
            "payout_closed",
        ),
        (CONTRACTING, {}, ("payout_evidence", "supplier_payout", PAYOUT_ID), "forbidden"),
        (SALES, {}, ("supplier_bill", "supplier_bill", BILL_ID), "forbidden"),
    ]
    for staff, rules, (kind, entity_type, entity_id), code in cases:
        with pytest.raises((Invalid, Conflict, Forbidden)) as exc:
            await ops_files.upload(file_conn(**rules), Store(), staff, **_upload_args(kind, entity_type, entity_id))
        assert exc.value.code == code


@pytest.mark.asyncio
async def test_remove_bill_document_and_payout_evidence() -> None:
    received = file_conn(**{BILL_PARENT: bill_parent(voucher_file_id=FILE_1)})
    await ops_files.remove(received, CONTRACTING, FILE_1, "wrong file")
    flats = [flat for flat, _ in received.calls]
    detach = next(i for i, f in enumerate(flats) if "UPDATE booking.supplier_bills SET document_file_id = NULL" in f)
    removed = next(i for i, f in enumerate(flats) if "UPDATE booking.ops_files SET status = 'removed'" in f)
    assert detach < removed
    for status in ("approved", "paid"):
        with pytest.raises(Conflict) as exc:
            await ops_files.remove(
                file_conn(**{BILL_PARENT: bill_parent(status=status, voucher_file_id=FILE_1)}), CONTRACTING, FILE_1, "x"
            )
        assert exc.value.code == "document_locked"
    cancelled = file_conn(**{BILL_PARENT: bill_parent(status="cancelled", voucher_file_id=FILE_1)})
    await ops_files.remove(cancelled, CONTRACTING, FILE_1, "x")
    assert not any("document_file_id = NULL" in f for f, _ in cancelled.calls)
    payout_file = {
        "id": FILE_2,
        "kind": "payout_evidence",
        "entity_type": "supplier_payout",
        "entity_id": PAYOUT_ID,
        "trip_case_id": CASE_ID,
        "s3_key": "k",
        "content_type": "application/pdf",
        "bytes": 1,
        "sha256": "0" * 64,
        "file_name": "slip.pdf",
        "status": "active",
        "removed_reason": None,
        "removed_at": None,
        "uploaded_by": None,
        "uploaded_by_name": None,
        "created_at": None,
    }
    live = file_conn(**{"WHERE f.id = $1::uuid": payout_file, PAYOUT_PARENT: payout_parent(voucher_file_id=FILE_2)})
    with pytest.raises(Conflict) as exc:
        await ops_files.remove(live, ADMIN, FILE_2, "x")
    assert exc.value.code == "evidence_locked"
    old = file_conn(**{"WHERE f.id = $1::uuid": payout_file, PAYOUT_PARENT: payout_parent(voucher_file_id=FILE_3)})
    await ops_files.remove(old, ADMIN, FILE_2, "superseded")
    assert old.sql("UPDATE booking.ops_files SET status = 'removed'")


# ── Reminders ────────────────────────────────────────────────────────────────


def test_bill_reminder_keys_and_titles() -> None:
    row = {
        "id": BILL_ID,
        "code": "BIL-1",
        "status": "received",
        "due_date": TODAY + timedelta(days=2),
        "currency": "USD",
        "balance": Decimal("380.00"),
        "supplier_name": "Delta",
    }
    key, title, message = lifecycle.bill_reminder(row, TODAY)
    assert key == f"supplier_bill.due:{BILL_ID}:{(TODAY + timedelta(days=2)).isoformat()}:soon"
    assert title == "BIL-1: approve 380.00 USD to Delta" and "another person approves" in message
    key, title, message = lifecycle.bill_reminder(
        {**row, "status": "approved", "due_date": TODAY - timedelta(days=1)}, TODAY
    )
    assert key.endswith(":overdue") and title.startswith("BIL-1: pay (overdue)") and "payout" in message
    _, title, _ = lifecycle.bill_reminder({**row, "status": "approved"}, TODAY)
    assert title.startswith("BIL-1: pay 380")
    _, title, _ = lifecycle.bill_reminder({**row, "due_date": TODAY - timedelta(days=1)}, TODAY)
    assert "approve (overdue)" in title


@pytest.mark.asyncio
async def test_bill_reminders_write_once_to_the_shared_queue() -> None:
    row = {
        "id": BILL_ID,
        "code": "BIL-1",
        "status": "approved",
        "due_date": TODAY,
        "currency": "USD",
        "balance": Decimal("1"),
        "supplier_name": "Delta",
    }
    conn = ScriptedConn(
        [("FROM booking.supplier_bills b JOIN booking.v_supplier_bill_balance", [row, row]), (NOTIFY_STAFF, "n-1")]
    )
    conn.on(NOTIFY_STAFF, lambda *a: "n-1" if len(conn.sql(NOTIFY_STAFF)) == 1 else None)
    out = await lifecycle._bill_reminders(conn, TODAY, 3, 100)
    assert out == {"bills_due": 2, "bills_notified": 1}
    assert conn.calls[0][1] == (TODAY + timedelta(days=3), 100)
    assert lifecycle.DEFAULTS["supplier_bill_due_soon_days"] == 3


# ── Routes ───────────────────────────────────────────────────────────────────


def route_client(staff: Staff, monkeypatch: pytest.MonkeyPatch) -> Any:
    from fastapi.testclient import TestClient

    from app.auth.staff import current_staff, get_pool
    from app.main import create_app
    from app.settings import Settings

    calls: list[tuple[str, Any]] = []

    def stub(name: str, result: Any) -> None:
        async def fn(*args: Any, **kwargs: Any) -> Any:
            calls.append((name, args[2:] if len(args) > 2 else kwargs))
            return result

        monkeypatch.setattr(bills, name, fn)

    stub("list_bills", {"items": [], "total": 0})
    stub("summary", {"bills": {}})
    stub("billable", {"items": []})
    stub("detail", {"id": BILL_ID})
    stub("update", {"id": BILL_ID, "changed": []})
    stub("approve", {"id": BILL_ID, "status": "approved"})
    stub("cancel", {"id": BILL_ID, "status": "cancelled"})
    stub("void_payout", {"id": PAYOUT_ID, "state": "voided"})
    stub("case_costs", {"lines": []})
    replay = {"n": 0}

    async def create(conn: Any, staff: Any, body: dict[str, Any], key: Any) -> tuple[dict[str, Any], bool]:
        replay["n"] += 1
        calls.append(("create", body))
        return {"id": BILL_ID}, replay["n"] > 1

    async def record_payout(conn: Any, staff: Any, ident: str, body: dict[str, Any], key: Any) -> Any:
        calls.append(("record_payout", body))
        return {"id": PAYOUT_ID}, key == "replayed-key"

    monkeypatch.setattr(bills, "create", create)
    monkeypatch.setattr(bills, "record_payout", record_payout)
    app = create_app(Settings(database_url=None, environment="test"))
    app.dependency_overrides[current_staff] = lambda: staff
    app.dependency_overrides[get_pool] = lambda: ScriptedConn()
    return TestClient(app), calls


def test_routes_pass_bodies_and_status_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    c, calls = route_client(ADMIN, monkeypatch)
    base = "/booking/supplier-bills"
    assert c.get(base, params={"supplier_id": SUP_ID, "status": "open", "overdue": "true"}).status_code == 200
    assert calls[-1][1]["supplier"] == SUP_ID and calls[-1][1]["status"] == "open"
    assert c.get(f"{base}/summary").status_code == 200
    assert c.get(f"{base}/billable", params={"supplier_id": SUP_ID}).status_code == 200
    assert c.get(f"{base}/billable").status_code == 422  # supplier_id is required
    body = {
        "supplier_id": SUP_ID,
        "bill_number": "D-1",
        "currency": "USD",
        "amount": "10.00",
        "issue_date": TODAY.isoformat(),
        "due_date": TODAY.isoformat(),
        "lines": [{"supplier_booking_id": SB_ID, "amount": "10.00"}],
    }
    first = c.post(base, json=body, headers={"Idempotency-Key": KEY})
    assert first.status_code == 201 and first.json()["replayed"] is False
    assert calls[-1][1]["lines"] == [{"supplier_booking_id": SB_ID, "amount": Decimal("10.00")}]
    assert c.post(base, json=body, headers={"Idempotency-Key": KEY}).status_code == 200
    assert c.post(base, json={**body, "currency": "usd"}).status_code == 422
    assert c.post(base, json={**body, "extra": 1}).status_code == 422
    assert c.get(f"{base}/BIL-000001").status_code == 200
    assert c.patch(f"{base}/{BILL_ID}", json={"notes": "x"}).status_code == 200
    assert calls[-1][1][1] == {"notes": "x"}
    assert (
        c.patch(f"{base}/{BILL_ID}", json={"lines": [{"supplier_booking_id": SB_ID, "amount": "1"}]}).status_code == 200
    )
    assert c.post(f"{base}/{BILL_ID}/approve").status_code == 200
    assert c.post(f"{base}/{BILL_ID}/cancel", json={"reason": "x"}).status_code == 200
    assert c.post(f"{base}/{BILL_ID}/cancel", json={}).status_code == 422
    pay = {"amount": "10.00", "method": "cash", "paid_on": TODAY.isoformat()}
    assert c.post(f"{base}/{BILL_ID}/payouts", json=pay, headers={"Idempotency-Key": KEY}).status_code == 201
    assert c.post(f"{base}/{BILL_ID}/payouts", json=pay, headers={"Idempotency-Key": "replayed-key"}).status_code == 200
    assert c.post(f"{base}/{BILL_ID}/payouts", json={**pay, "method": "crypto"}).status_code == 422
    assert c.post(f"/booking/supplier-payouts/{PAYOUT_ID}/void", json={"reason": "x"}).status_code == 200
    assert c.get("/booking/trip-cases/TC-000001/supplier-costs").status_code == 200


def test_routes_refuse_roles_without_the_permission(monkeypatch: pytest.MonkeyPatch) -> None:
    c, _ = route_client(SALES, monkeypatch)
    assert c.get("/booking/supplier-bills").status_code == 200
    assert c.post(f"/booking/supplier-bills/{BILL_ID}/approve").status_code == 403
    c, _ = route_client(CONTRACTING, monkeypatch)
    pay = {"amount": "10.00", "method": "cash", "paid_on": TODAY.isoformat()}
    assert c.post(f"/booking/supplier-bills/{BILL_ID}/payouts", json=pay).status_code == 403
    c, _ = route_client(CONTENT, monkeypatch)
    assert c.get("/booking/supplier-bills").status_code == 403
