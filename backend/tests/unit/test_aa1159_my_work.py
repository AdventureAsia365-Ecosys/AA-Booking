"""AA-1159 — My work band: tiles by capability, the route's gate, the per-tile fallback and the shared list filters.

The counts themselves against the lists they open run on PostgreSQL in tests/integration/test_aa1159_my_work.py.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import my_work
from app.agency_billing import invoices as agency_invoices
from app.agency_billing import payments as agency_payments
from app.auth.staff import PERMISSIONS, Staff, current_staff, get_pool
from app.b2b import desk
from app.errors import Unavailable
from app.main import create_app
from app.ops import supplier_bills, trip_checks
from app.settings import Settings
from app.trip_cases import service as trip_cases
from tests.unit.fakes import ScriptedConn

ADMIN = Staff("a-1", None, "Admin", "admin", True, "admin")
SALES = Staff("u-1", None, "Sales One", "sales", True, "reviewer")
CONTRACTING = Staff("u-5", None, "Product", "contracting", True, "reviewer")
FINANCE = Staff("u-7", None, "Money", "finance", True, "reviewer")
CONTENT = Staff("u-9", None, "Writer", "content_creator", True, "content")

FRONTEND_TABLE = Path(__file__).resolve().parents[3] / "frontend" / "lib" / "myWork.ts"


def _keys(staff: Staff) -> list[str]:
    return [t.key for t in my_work.tiles_for(staff)]


# ── The tile table ───────────────────────────────────────────────────────────


def test_every_tile_capability_is_a_known_permission_and_keys_are_unique() -> None:
    keys = [t.key for t in my_work.TILES]
    assert len(keys) == len(set(keys))
    for tile in my_work.TILES:
        assert tile.capability in PERMISSIONS, tile.key
        assert tile.group in my_work.GROUPS, tile.key
        assert all(isinstance(v, str) and v for v in tile.filter.values()), tile.key


def test_the_frontend_table_lists_the_same_keys_capabilities_and_groups_in_order() -> None:
    text = FRONTEND_TABLE.read_text(encoding="utf-8")
    rows = re.findall(r'key: "([a-z0-9_]+)",\s*capability: "([a-z0-9_.]+)",\s*group: "([a-z]+)"', text)
    assert rows == [(t.key, t.capability, t.group) for t in my_work.TILES]


def test_open_case_statuses_leave_out_only_the_closed_ones() -> None:
    assert set(my_work.OPEN_CASE_STATUSES) == set(trip_cases.STATUSES) - {"completed", "cancelled"}
    tile = next(t for t in my_work.TILES if t.key == "my_open_cases")
    assert tile.filter == {"tab": "mine", "status": ",".join(my_work.OPEN_CASE_STATUSES)}


@pytest.mark.parametrize(
    ("staff", "expected"),
    [
        (
            ADMIN,
            [
                "my_open_cases",
                "trip_checks_overdue",
                "b2b_on_request",
                "supplier_bills_overdue",
                "supplier_bills_due_soon",
                "agency_invoices_overdue",
                "supplier_bills_to_approve",
                "supplier_bills_to_pay",
                "agency_payments_to_approve",
            ],
        ),
        (
            CONTRACTING,
            [
                "my_open_cases",
                "trip_checks_overdue",
                "b2b_on_request",
                "supplier_bills_overdue",
                "supplier_bills_due_soon",
                "supplier_bills_to_approve",
            ],
        ),
        (
            SALES,
            [
                "my_open_cases",
                "trip_checks_overdue",
                "b2b_on_request",
                "supplier_bills_overdue",
                "supplier_bills_due_soon",
            ],
        ),
        (
            FINANCE,
            [
                "my_open_cases",
                "trip_checks_overdue",
                "supplier_bills_overdue",
                "supplier_bills_due_soon",
                "agency_invoices_overdue",
                "agency_payments_to_approve",
            ],
        ),
        (CONTENT, []),
    ],
    ids=["admin", "contracting", "sales", "finance", "content_creator"],
)
def test_tiles_follow_the_capabilities_of_each_built_in_role(staff: Staff, expected: list[str]) -> None:
    assert _keys(staff) == expected


def test_a_capability_decides_a_tile_not_the_role_name(monkeypatch: pytest.MonkeyPatch) -> None:
    # A role that loses bill.approve loses the approval tile; one that gains payout.write gains the pay tile.
    monkeypatch.setitem(PERMISSIONS, "bill.approve", PERMISSIONS["bill.approve"] - {"contracting"})
    monkeypatch.setitem(PERMISSIONS, "payout.write", PERMISSIONS["payout.write"] | {"contracting"})
    keys = _keys(CONTRACTING)
    assert "supplier_bills_to_approve" not in keys and "supplier_bills_to_pay" in keys


def test_a_preview_session_gets_only_the_tiles_of_its_capped_reads() -> None:
    preview = replace(ADMIN, preview=True)
    assert _keys(preview) == ["my_open_cases", "trip_checks_overdue", "b2b_on_request"]


def test_an_inactive_staff_member_has_no_tile() -> None:
    assert _keys(replace(ADMIN, active=False)) == []


# ── The route ────────────────────────────────────────────────────────────────


@pytest.fixture
def make_client():
    def _make(staff: Staff) -> TestClient:
        app = create_app(Settings(database_url=None, environment="test"))
        app.dependency_overrides[current_staff] = lambda: staff
        app.dependency_overrides[get_pool] = lambda: ScriptedConn()
        return TestClient(app)

    return _make


def _queue(result: dict[str, Any], calls: list[tuple[str, dict[str, Any]]], name: str):
    async def _fake(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append((name, kwargs))
        return result

    return _fake


@pytest.fixture
def stubbed(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []
    stamp = datetime(2026, 11, 2, 8, 0)
    monkeypatch.setattr(
        trip_cases, "case_queue", _queue({"count": 2, "oldest_at": stamp, "earliest_due": None}, calls, "cases")
    )
    monkeypatch.setattr(
        trip_checks,
        "overdue_queue",
        _queue({"count": 1, "oldest_at": None, "earliest_due": date(2026, 11, 3)}, calls, "checks"),
    )
    monkeypatch.setattr(
        desk, "on_request_queue", _queue({"count": 0, "oldest_at": None, "earliest_due": None}, calls, "desk")
    )
    monkeypatch.setattr(
        supplier_bills,
        "queue",
        _queue({"count": 3, "oldest_at": stamp, "earliest_due": date(2026, 11, 21)}, calls, "bills"),
    )

    async def _days(_conn: Any) -> int:
        return 3

    monkeypatch.setattr(supplier_bills, "due_soon_days", _days)
    monkeypatch.setattr(
        agency_invoices,
        "queue",
        _queue({"count": 4, "oldest_at": stamp, "earliest_due": date(2026, 10, 1)}, calls, "invoices"),
    )
    monkeypatch.setattr(
        agency_payments, "queue", _queue({"count": 5, "oldest_at": stamp, "earliest_due": None}, calls, "payments")
    )
    return calls


def test_summary_answers_each_tile_with_its_filter(make_client, stubbed) -> None:
    body = make_client(ADMIN).get("/booking/my-work/summary").json()
    tiles = {t["key"]: t for t in body["tiles"]}
    assert body["role"] == "admin" and list(tiles) == _keys(ADMIN)
    assert tiles["my_open_cases"]["count"] == 2 and tiles["my_open_cases"]["oldest_at"] == "2026-11-02T08:00:00"
    assert tiles["trip_checks_overdue"] | {"filter": None} == {
        "key": "trip_checks_overdue",
        "capability": "ops.read",
        "group": "work",
        "filter": None,
        "available": True,
        "count": 1,
        "oldest_at": None,
        "earliest_due": "2026-11-03",
        "days": None,
    }
    assert tiles["supplier_bills_due_soon"]["days"] == 3
    assert tiles["supplier_bills_due_soon"]["filter"] == {"bill_status": "open", "bill_due": "soon"}
    assert tiles["agency_payments_to_approve"]["filter"] == {"tab": "payments"}
    # Each counter got the list filter of its tile (the caller's own id for "mine").
    assert [kw for name, kw in stubbed if name == "cases"] == [
        {"statuses": list(my_work.OPEN_CASE_STATUSES), "assignee": "a-1"}
    ]
    bill_filters = [kw for name, kw in stubbed if name == "bills"]
    assert bill_filters == [
        {"status": "open", "overdue": True},
        {"status": "open", "due_soon_days": 3},
        {"status": "received"},
        {"status": "approved"},
    ]
    assert [kw for name, kw in stubbed if name == "invoices"] == [{"status": "overdue"}]
    assert [kw for name, kw in stubbed if name == "payments"] == [{"state": "recorded"}]


def test_a_role_with_no_tile_capability_is_refused(make_client, stubbed) -> None:
    r = make_client(CONTENT).get("/booking/my-work/summary")
    assert r.status_code == 403 and r.json()["detail"] == "forbidden"
    assert stubbed == []


def test_only_the_callers_tiles_are_counted(make_client, stubbed) -> None:
    body = make_client(SALES).get("/booking/my-work/summary").json()
    assert [t["key"] for t in body["tiles"]] == _keys(SALES)
    assert {name for name, _ in stubbed} == {"cases", "checks", "desk", "bills"}


def test_a_tile_that_cannot_be_read_says_so_and_the_others_load(make_client, stubbed, monkeypatch) -> None:
    async def _no_policy(_conn: Any) -> int:
        raise Unavailable("policy_unavailable", "no value in force")

    monkeypatch.setattr(supplier_bills, "due_soon_days", _no_policy)
    body = make_client(SALES).get("/booking/my-work/summary").json()
    tiles = {t["key"]: t for t in body["tiles"]}
    assert tiles["supplier_bills_due_soon"]["available"] is False and tiles["supplier_bills_due_soon"]["count"] is None
    assert tiles["supplier_bills_overdue"]["available"] is True and tiles["supplier_bills_overdue"]["count"] == 3


# ── The shared list filters ──────────────────────────────────────────────────


def test_bill_filter_due_soon_is_open_not_overdue_and_within_the_window() -> None:
    clause, args = supplier_bills.bill_filter(status="open", due_soon_days=3)
    assert "b.status IN ('received', 'approved')" in clause
    assert "NOT bal.overdue AND b.due_date <= current_date + $1::int" in clause
    assert args == [3]
    clause, args = supplier_bills.bill_filter(status="received", overdue=True)
    assert clause == "true AND b.status = $1 AND bal.overdue" and args == ["received"]


@pytest.mark.asyncio
async def test_the_bills_list_reads_the_due_soon_window_from_the_policy() -> None:
    conn = ScriptedConn().on("SELECT count(*)", 0).on("SELECT (now() AT TIME ZONE 'UTC')::date", date(2026, 11, 20))
    out = await supplier_bills.list_bills(conn, ADMIN, due_soon=True)
    assert out["total"] == 0
    counted = conn.sql("SELECT count(*)")
    assert counted and isinstance(counted[0][-1], int)


def test_case_and_invoice_and_payment_filters_are_the_lists_where() -> None:
    clause, args = trip_cases.case_filter(statuses=["building"], assignee="u-1")
    assert clause.startswith("tc.deleted_at IS NULL AND tc.status = ANY($1::text[]) AND EXISTS")
    assert args == [["building"], "u-1"]
    clause, args = agency_invoices.invoice_filter(status="overdue")
    assert "i.due_date < current_date" in clause and args == []
    clause, args = agency_payments.payment_filter(state="recorded")
    assert clause == "true AND p.status = $1" and args == ["recorded"]
    assert "k.due_date < current_date" in trip_checks.OVERDUE
    assert "{OVERDUE}" not in trip_checks.COUNTS and trip_checks.OVERDUE in trip_checks.COUNTS
    assert desk.STATE_WHERE["open"] == "l.held_as = 'on_request' AND l.status = 'on_request'"
