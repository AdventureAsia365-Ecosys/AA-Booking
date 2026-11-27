"""AA-1159 — My work band: tiles by capability, the route's gate, the per-tile fallback and the shared list filters.
AA-1190: the price version / add-on / season / holiday approval tiles, trip checks split into yours and unassigned, the
price setup progress card.

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

from app import audit, my_work
from app.agency_billing import entity_changes
from app.agency_billing import invoices as agency_invoices
from app.agency_billing import payments as agency_payments
from app.auth.staff import PERMISSIONS, Staff, current_staff, get_pool
from app.b2b import desk
from app.catalog import price_version_queue, price_workbench
from app.errors import Unavailable
from app.main import create_app
from app.ops import bill_filters, supplier_bills, trip_checks
from app.pricing import seasons_admin
from app.settings import Settings
from app.suppliers import bank_accounts
from app.suppliers import service as suppliers
from app.supply import addons, contract_expiry, restrictions
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
                "trip_checks_unassigned",  # AA-1190
                "b2b_on_request",
                "supplier_bills_overdue",
                "supplier_bills_due_soon",
                "agency_invoices_overdue",
                "contracts_ending_soon",  # AA-1177
                "supplier_bills_to_approve",
                "supplier_bills_to_pay",
                "agency_payments_to_approve",
                "stop_sales_to_review",
                "suppliers_to_activate",
                "billing_changes_to_approve",
                "supplier_bank_to_approve",  # AA-1158 part 2
                "price_versions_to_approve",  # AA-1190
                "addons_to_publish",
                "season_changes_to_approve",
                "holiday_events_to_approve",
                "owner_self_approvals",
            ],
        ),
        (
            CONTRACTING,
            [
                "my_open_cases",
                "trip_checks_overdue",
                "trip_checks_unassigned",
                "b2b_on_request",
                "supplier_bills_overdue",
                "supplier_bills_due_soon",
                "contracts_ending_soon",  # AA-1177
                "supplier_bills_to_approve",
                "stop_sales_to_review",
                "suppliers_to_activate",
                "price_versions_to_approve",
                "addons_to_publish",
                "season_changes_to_approve",
                "holiday_events_to_approve",
            ],
        ),
        (
            SALES,
            [
                "my_open_cases",
                "trip_checks_overdue",
                "trip_checks_unassigned",
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
                "billing_changes_to_approve",
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

    monkeypatch.setattr(bill_filters, "due_soon_days", _days)
    monkeypatch.setattr(
        agency_invoices,
        "queue",
        _queue({"count": 4, "oldest_at": stamp, "earliest_due": date(2026, 10, 1)}, calls, "invoices"),
    )
    monkeypatch.setattr(
        agency_payments, "queue", _queue({"count": 5, "oldest_at": stamp, "earliest_due": None}, calls, "payments")
    )
    monkeypatch.setattr(
        restrictions,
        "review_queue",
        _queue({"count": 6, "oldest_at": stamp, "earliest_due": None}, calls, "stop_sales"),
    )
    # AA-1158: pending suppliers, billing entity changes, the month's business-owner self-approvals.
    monkeypatch.setattr(suppliers, "queue", _queue({"count": 7, "oldest_at": stamp}, calls, "suppliers"))
    monkeypatch.setattr(entity_changes, "queue", _queue({"count": 8, "oldest_at": stamp}, calls, "entity_changes"))
    monkeypatch.setattr(bank_accounts, "queue", _queue({"count": 9, "oldest_at": stamp}, calls, "bank_accounts"))
    # AA-1177: the supplier contracts ending within the policy window (days = the window).
    monkeypatch.setattr(
        contract_expiry,
        "queue_count",
        _queue({"count": 2, "earliest_due": date(2026, 12, 20), "days": 60}, calls, "contracts_ending"),
    )
    # AA-1190: price versions, add-ons, season and holiday changes waiting for a second person.
    monkeypatch.setattr(
        price_version_queue, "approval_queue", _queue({"count": 10, "oldest_at": stamp}, calls, "price_versions")
    )
    monkeypatch.setattr(addons, "publish_queue", _queue({"count": 11, "oldest_at": stamp}, calls, "addons"))
    monkeypatch.setattr(
        seasons_admin, "seasons_waiting_queue", _queue({"count": 12, "oldest_at": stamp}, calls, "seasons")
    )
    monkeypatch.setattr(
        seasons_admin, "events_waiting_queue", _queue({"count": 13, "oldest_at": stamp}, calls, "events")
    )
    monkeypatch.setattr(
        audit,
        "owner_self_approvals_month",
        _queue(
            {"count": 9, "oldest_at": stamp, "filter": {"owner_self_approved": "true", "from": "2026-11-01"}},
            calls,
            "owner_self",
        ),
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
    # AA-1190: overdue trip checks split into the caller's own and the unowned ones (the page's owner filter)
    assert tiles["trip_checks_overdue"]["filter"] == {"due": "overdue", "owner": "me"}
    assert tiles["trip_checks_unassigned"]["filter"] == {"due": "overdue", "owner": "none"}
    assert [kw for name, kw in stubbed if name == "checks"] == [{"assignee": "a-1"}, {"assignee": "none"}]
    assert tiles["price_versions_to_approve"]["filter"] == {"pending_versions": "me"}
    assert tiles["price_versions_to_approve"]["count"] == 10 and tiles["addons_to_publish"]["count"] == 11
    assert tiles["addons_to_publish"]["filter"] == {"approvable": "me"}
    assert [kw for name, kw in stubbed if name == "addons"] == [{"approvable_by": "a-1"}]
    assert tiles["season_changes_to_approve"]["filter"] == {"approvable": "me"}
    assert tiles["season_changes_to_approve"]["count"] == 12
    assert tiles["holiday_events_to_approve"]["filter"] == {"approvable": "me"}
    assert tiles["holiday_events_to_approve"]["group"] == "approve"
    assert tiles["supplier_bills_due_soon"]["days"] == 3
    assert tiles["supplier_bills_due_soon"]["filter"] == {"bill_status": "open", "bill_due": "soon"}
    assert tiles["agency_payments_to_approve"]["filter"] == {"tab": "payments", "approvable": "me"}
    assert tiles["supplier_bills_to_approve"]["filter"] == {"bill_status": "received", "approvable": "me"}
    assert tiles["suppliers_to_activate"]["filter"] == {"status": "pending", "approvable": "me"}
    assert tiles["suppliers_to_activate"]["count"] == 7 and tiles["billing_changes_to_approve"]["count"] == 8
    assert tiles["supplier_bank_to_approve"]["count"] == 9
    assert tiles["supplier_bank_to_approve"]["filter"] == {"bank": "pending", "approvable": "me"}
    assert tiles["contracts_ending_soon"] | {"capability": None} == {
        "key": "contracts_ending_soon",
        "capability": None,
        "group": "work",
        "filter": {"contracts": "ending"},
        "available": True,
        "count": 2,
        "oldest_at": None,
        "earliest_due": "2026-12-20",
        "days": 60,
    }
    assert tiles["owner_self_approvals"]["filter"] == {"owner_self_approved": "true", "from": "2026-11-01"}
    assert tiles["stop_sales_to_review"] | {"oldest_at": None} == {
        "key": "stop_sales_to_review",
        "capability": "stop_sale.approve",
        "group": "approve",
        "filter": {"review": "me"},
        "available": True,
        "count": 6,
        "oldest_at": None,
        "earliest_due": None,
        "days": None,
    }
    # Each counter got the list filter of its tile (the caller's own id for "mine").
    assert [kw for name, kw in stubbed if name == "cases"] == [
        {"statuses": list(my_work.OPEN_CASE_STATUSES), "assignee": "a-1"}
    ]
    bill_calls = [kw for name, kw in stubbed if name == "bills"]
    assert bill_calls == [
        {"status": "open", "overdue": True},
        {"status": "open", "due_soon_days": 3},
        {"status": "received", "actionable_by": "a-1", "owner_approve": False, "owner_pay": False},
        {"status": "approved", "actionable_by": "a-1", "owner_approve": False, "owner_pay": False},
    ]
    assert [kw for name, kw in stubbed if name == "invoices"] == [{"status": "overdue"}]
    # The approval tiles count only what the caller may act on (maker-checker), the page's approvable / review=me.
    assert [kw for name, kw in stubbed if name == "payments"] == [
        {"state": "recorded", "actionable_by": "a-1", "owner": False}
    ]
    assert [kw for name, kw in stubbed if name == "stop_sales"] == [{"reviewable_by": "a-1"}]


def test_a_role_with_no_tile_capability_is_refused(make_client, stubbed) -> None:
    r = make_client(CONTENT).get("/booking/my-work/summary")
    assert r.status_code == 403 and r.json()["detail"] == "forbidden"
    assert stubbed == []


def test_only_the_callers_tiles_are_counted(make_client, stubbed) -> None:
    body = make_client(SALES).get("/booking/my-work/summary").json()
    assert [t["key"] for t in body["tiles"]] == _keys(SALES)
    assert {name for name, _ in stubbed} == {"cases", "checks", "desk", "bills"}


# ── Price setup progress (AA-1190) ───────────────────────────────────────────


def test_the_price_setup_card_is_for_the_price_setup_makers() -> None:
    assert my_work.may_see_price_setup(ADMIN) and my_work.may_see_price_setup(CONTRACTING)
    assert not my_work.may_see_price_setup(SALES) and not my_work.may_see_price_setup(FINANCE)
    assert not my_work.may_see_price_setup(CONTENT)


def test_the_price_setup_card_counts_the_workbench_steps(make_client, monkeypatch) -> None:
    def row(step: str) -> dict[str, Any]:
        return {"next_step": step, "missing": [], "provisional": None, "status": "priced", "reason": None}

    rows = [row("add_components"), row("load_rates"), row("load_rates"), row("done")]

    class _Facts:
        degraded: list[str] = []

    async def _rows(_conn: Any, _on: Any) -> tuple[list[dict[str, Any]], Any]:
        return rows, _Facts()

    monkeypatch.setattr(price_workbench, "rows", _rows)
    monkeypatch.setattr(price_workbench.price_readiness, "summary", lambda _rows: {})
    body = make_client(CONTRACTING).get("/booking/my-work/price-setup").json()
    assert body["steps"] == list(price_workbench.NEXT_STEPS)
    assert body["total"] == 4 and body["degraded"] is False
    assert body["by_next_step"]["load_rates"] == 2 and body["by_next_step"]["add_components"] == 1
    assert body["by_next_step"]["done"] == 1 and body["by_next_step"]["recompute"] == 0


def test_the_price_setup_card_refuses_other_roles(make_client) -> None:
    r = make_client(SALES).get("/booking/my-work/price-setup")
    assert r.status_code == 403 and r.json()["detail"] == "forbidden"


@pytest.mark.asyncio
async def test_the_new_approval_queues_share_their_lists_where() -> None:
    stamp = datetime(2026, 11, 2, 8, 0)
    assert price_version_queue.PENDING_WHERE.startswith("v.status = 'pending'")
    assert "v.created_by IS DISTINCT FROM $1" in price_version_queue.PENDING_WHERE
    where, args = addons._offer_where(approvable_by="u-5")
    assert where[-1] == "o.status = 'draft' AND NOT ($1 = ANY (o.writers))" and args == ["u-5"]
    offers = ScriptedConn().on("count(*)::int AS count", {"count": 2, "oldest_at": stamp})
    assert await addons.publish_queue(offers, approvable_by="u-5") == {"count": 2, "oldest_at": stamp}
    assert addons.OFFER_SQL.strip()[:40] in offers.calls[0][0] and offers.calls[0][1] == ("u-5",)
    assert seasons_admin.RANGE_WAITING in seasons_admin.CALENDARS_SQL
    # AA-1190: the season tile and the Seasons page's per-calendar column share the approvable filters
    for fragment in (seasons_admin.RANGE_APPROVABLE, seasons_admin.DEFAULT_APPROVABLE):
        assert fragment in seasons_admin.SEASONS_APPROVABLE_SQL
        assert fragment in seasons_admin.CALENDARS_APPROVABLE_SQL
    assert seasons_admin.EVENT_APPROVABLE in seasons_admin.EVENTS_APPROVABLE_SQL


# ── AA-1190: the season and holiday tiles follow the approve paths' maker-checker ──


def _placeholders_match(conn: ScriptedConn) -> None:
    """Each recorded query takes exactly the arguments it was given (asyncpg refuses a count mismatch; the fake
    does not)."""
    for sql, args in conn.calls:
        used = {int(n) for n in re.findall(r"\$(\d+)", sql)}
        assert used == set(range(1, len(args) + 1)), (sql[:80], args)


@pytest.mark.parametrize(
    ("staff", "owner", "expected"),
    [
        (CONTRACTING, False, ("u-5", False, True)),  # reviews stop-sales, never self-approves
        (CONTRACTING, True, ("u-5", True, True)),  # the business owner while the switch is on
        (SALES, False, ("u-1", False, False)),  # no stop_sale.approve: its stop-sale ranges are left out
    ],
)
@pytest.mark.asyncio
async def test_the_season_and_holiday_queues_pass_the_approver(monkeypatch, staff, owner, expected) -> None:
    async def _owner(_conn: Any, _staff: Staff, kind: str = "approve") -> bool:
        return owner

    monkeypatch.setattr(seasons_admin.self_approval, "may_self_approve", _owner)
    stamp = datetime(2026, 11, 2, 8, 0)
    conn = ScriptedConn().on("AS oldest_at", {"count": 3, "oldest_at": stamp})
    assert await seasons_admin.seasons_waiting_queue(conn, staff) == {"count": 3, "oldest_at": stamp}
    assert await seasons_admin.events_waiting_queue(conn, staff) == {"count": 3, "oldest_at": stamp}
    assert [args for _, args in conn.calls] == [expected, expected[:2]]  # an event has no stop-sale
    _placeholders_match(conn)


def test_the_approvable_filters_mirror_the_approve_paths() -> None:
    ranges = seasons_admin.RANGE_APPROVABLE
    # a proposal: never one's own, unless the business owner may self-approve a season range (never a stop-sale)
    assert "r.created_by IS DISTINCT FROM $1::text OR ($2::boolean AND r.kind = 'season')" in ranges
    # a removal: never one's own request, no self-approval
    assert "r.end_requested_by IS DISTINCT FROM $1::text)" in ranges
    # a stop-sale range (and its removal) only for a stop-sale reviewer
    assert ranges.startswith("((r.kind <> 'stop_sale' OR $3::boolean)")
    assert "c.default_requested_by IS DISTINCT FROM $1::text OR $2::boolean" in seasons_admin.DEFAULT_APPROVABLE
    events = seasons_admin.EVENT_APPROVABLE
    assert "e.created_by IS DISTINCT FROM $1::text OR $2::boolean" in events
    assert "e.end_requested_by IS DISTINCT FROM $1::text)" in events


@pytest.mark.asyncio
async def test_the_holiday_list_approvable_view(monkeypatch) -> None:
    async def _owner(*_args: Any, **_kw: Any) -> bool:
        return False

    monkeypatch.setattr(seasons_admin.self_approval, "may_self_approve", _owner)
    conn = ScriptedConn()
    await seasons_admin.list_events(conn, CONTRACTING, "vn", 2027, approvable=True)
    _placeholders_match(conn)
    listed = [(sql, args) for sql, args in conn.calls if "FROM booking.holiday_events e" in sql]
    assert len(listed) == 1 and listed[0][1] == ("u-5", False, "VN", 2027)
    assert " ".join(seasons_admin.EVENT_APPROVABLE.split()) in listed[0][0]
    # without season.approve the view is empty (no query of the events)
    assert FINANCE.can("pricing.read") and not FINANCE.can("season.approve")
    none = ScriptedConn()
    assert (await seasons_admin.list_events(none, FINANCE, None, None, approvable=True))["items"] == []
    assert not [sql for sql, _ in none.calls if "FROM booking.holiday_events e" in sql]


def test_a_tile_that_cannot_be_read_says_so_and_the_others_load(make_client, stubbed, monkeypatch) -> None:
    async def _no_policy(_conn: Any) -> int:
        raise Unavailable("policy_unavailable", "no value in force")

    monkeypatch.setattr(bill_filters, "due_soon_days", _no_policy)
    body = make_client(SALES).get("/booking/my-work/summary").json()
    tiles = {t["key"]: t for t in body["tiles"]}
    assert tiles["supplier_bills_due_soon"]["available"] is False and tiles["supplier_bills_due_soon"]["count"] is None
    assert tiles["supplier_bills_overdue"]["available"] is True and tiles["supplier_bills_overdue"]["count"] == 3


# ── The shared list filters ──────────────────────────────────────────────────


def test_bill_filter_due_soon_is_open_not_overdue_and_within_the_window() -> None:
    clause, args = bill_filters.bill_filter(status="open", due_soon_days=3)
    assert "b.status IN ('received', 'approved')" in clause
    assert "NOT bal.overdue AND b.due_date <= current_date + $1::int" in clause
    assert args == [3]
    clause, args = bill_filters.bill_filter(status="received", overdue=True)
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


def test_approvable_by_me_leaves_out_what_the_caller_entered_changed_or_recorded() -> None:
    clause, args = bill_filters.bill_filter(status="received", actionable_by="u-5")
    maker = "b.created_by <> $2 AND b.facts_edited_by IS DISTINCT FROM $2"
    assert clause == (
        f"true AND b.status = $1 AND (CASE WHEN b.status = 'approved' THEN ({maker}"
        f" AND b.approved_by IS DISTINCT FROM $2) ELSE ({maker}) END)"
    )
    assert args == ["received", "u-5"]
    # AA-1158: the business owner sees her own received bills and the bills she approved (to pay, flagged).
    clause, _ = bill_filters.bill_filter(status="received", actionable_by="u-5", owner_approve=True, owner_pay=True)
    assert clause == f"true AND b.status = $1 AND (CASE WHEN b.status = 'approved' THEN ({maker}) ELSE (true) END)"
    clause, _ = agency_payments.payment_filter(state="recorded", actionable_by="u-7", owner=True)
    assert clause == "true AND p.status = $1"
    clause, args = agency_payments.payment_filter(state="recorded", actionable_by="u-7")
    assert clause == "true AND p.recorded_by <> $1 AND p.status = $2" and args == ["u-7", "recorded"]


def test_restriction_review_filter_is_the_stop_sales_waiting_for_a_second_person() -> None:
    clause, args = restrictions.restriction_filter(review="waiting")
    assert clause == f"true AND ({restrictions.WAITING})" and args == []
    assert "r.status = 'pending'" in restrictions.WAITING and "r.end_requested_by IS NOT NULL" in restrictions.WAITING
    clause, args = restrictions.restriction_filter(review="me", reviewer="u-5")
    assert clause.endswith("(CASE WHEN r.status = 'pending' THEN r.created_by ELSE r.end_requested_by END) <> $1")
    assert args == ["u-5"]
    k = "11111111-1111-4111-8111-111111111111"
    clause, args = restrictions.restriction_filter(contract_id=k, kind="stop_sale", active_only=True)
    assert "r.supplier_contract_id = $1::uuid" in clause and "r.inventory_kind = $2" in clause
    assert "k.status = 'published'" in clause and args == [k, "stop_sale"]
    with pytest.raises(Exception, match="review"):
        restrictions.restriction_filter(review="lost")


@pytest.mark.asyncio
async def test_the_restriction_list_counts_and_orders_a_review_by_waiting_time() -> None:
    conn = ScriptedConn().on("SELECT count(*)", 4).on("ORDER BY", [])
    out = await restrictions.list_restrictions(conn, review="me", reviewer="u-5", limit=10)
    assert out == {"items": [], "total": 4}
    listed = [(sql, args) for sql, args in conn.calls if "ORDER BY" in sql]
    assert f"ORDER BY {restrictions.WAITING_SINCE} ASC" in listed[0][0] and listed[0][1] == ("u-5", 10)
    stamp = datetime(2026, 11, 2, 8, 0)
    counted = ScriptedConn().on("count(*)::int AS count", {"count": 1, "oldest_at": stamp})
    out = await restrictions.review_queue(counted, reviewable_by="u-5")
    assert out == {"count": 1, "oldest_at": stamp, "earliest_due": None}
    assert restrictions.WAITING in counted.calls[0][0] and counted.calls[0][1] == ("u-5",)


def test_bill_filter_supplier_case_and_search_keep_the_list_rules() -> None:
    sup = "11111111-1111-4111-8111-111111111111"
    clause, args = bill_filters.bill_filter(supplier=sup, case="tc-000001", q="ks", due_before=date(2026, 11, 30))
    assert "b.supplier_id = $1::uuid" in clause and "tc.code = $2" in clause and "b.due_date <= $3" in clause
    assert args[:3] == [sup, "TC-000001", date(2026, 11, 30)]
    clause, args = bill_filters.bill_filter(case=sup, overdue=False, status="all")
    assert "b.trip_case_id = $1::uuid" in clause and "NOT bal.overdue" in clause and args == [sup]
    with pytest.raises(Exception, match="supplier"):
        bill_filters.bill_filter(supplier="not-an-id")
    with pytest.raises(Exception, match="status"):
        bill_filters.bill_filter(status="lost")


@pytest.mark.asyncio
async def test_each_queue_counts_over_its_lists_from_and_where() -> None:
    stamp = datetime(2026, 11, 2, 8, 0)
    row = {"count": 2, "oldest_at": stamp, "earliest_due": date(2026, 11, 3)}
    conn = ScriptedConn().on("count(*)::int AS count", row)
    assert (await supplier_bills.queue(conn, status="received"))["count"] == 2
    assert (await agency_invoices.queue(conn, status="overdue"))["earliest_due"] == date(2026, 11, 3)
    paid = ScriptedConn().on("count(*)::int AS count", {"count": 1, "oldest_at": stamp})
    assert await agency_payments.queue(paid, state="recorded") == {"count": 1, "oldest_at": stamp, "earliest_due": None}
    cases = ScriptedConn().on("count(*)::int AS count", {"count": 3, "oldest_at": stamp})
    assert (await trip_cases.case_queue(cases, statuses=["building"], assignee="none"))["count"] == 3
    assert "NOT EXISTS (SELECT 1 FROM booking.staff_assignments" in cases.calls[0][0]
    checks = ScriptedConn().on("count(*)::int AS count", {"count": 1, "earliest_due": date(2026, 11, 3)})
    assert (await trip_checks.overdue_queue(checks))["oldest_at"] is None
    assert trip_checks.OVERDUE in checks.calls[0][0]
    lines = ScriptedConn().on("count(*)::int AS count", {"count": 0, "oldest_at": None})
    assert (await desk.on_request_queue(lines))["count"] == 0
    assert desk.STATE_WHERE["open"] in lines.calls[0][0]
    with pytest.raises(Exception, match="state"):
        await desk.on_request_queue(lines, state="lost")


def test_the_frontend_price_setup_steps_are_workbench_steps() -> None:
    text = FRONTEND_TABLE.read_text(encoding="utf-8")
    block = text.split("export const PRICE_SETUP_STEPS", 1)[1].split("];", 1)[0]
    steps = re.findall(r'step: "([a-z_]+)"', block)
    assert steps and len(steps) == len(set(steps))
    # in the workbench's order, every step a plan can still take (never the not-served or done ones)
    assert steps == [s for s in price_workbench.NEXT_STEPS if s in steps]
    assert set(steps) == set(price_workbench.NEXT_STEPS) - {price_workbench.NOT_SERVED, price_workbench.DONE}
