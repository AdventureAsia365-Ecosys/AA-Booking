"""AA-1074 (bk-045) finance: FX helpers, departure shared costs, purchase orders, profit reads and their routes. The SQL
is covered on PostgreSQL by tests/integration/test_aa1074_finance.py and test_bk045_schema.py; here the Python
branches around it (scripted connection). Invented names only."""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import asyncpg
import pytest

from app import mailer
from app.auth.staff import PERMISSIONS, PREVIEW_PERMISSIONS, Staff
from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.finance import departure_costs as costs
from app.finance import profit
from app.finance import supplier_orders as orders
from app.jobs.kinds import EXPECTED_SECONDS
from app.jobs.queue import Job
from app.jobs.worker import PermanentError
from app.pricing import fx
from app.settings import Settings
from tests.unit.fakes import ScriptedConn

pytestmark = pytest.mark.asyncio

OPS = Staff("c-1", None, "Ops One", "contracting", True, "reviewer")
FIN = Staff("f-1", None, "Fin One", "finance", True, "reviewer")
WRITER = Staff("w-1", None, "Writer", "content_creator", True, "content")
SALES = Staff("s-1", None, "Sales One", "sales", True, "reviewer")
ADMIN = Staff("a-1", None, "Admin One", "admin", True, "reviewer")
DEP = "00000000-0000-4000-8000-00000000d001"
COST = "00000000-0000-4000-8000-00000000c001"
SUP = "00000000-0000-4000-8000-00000000a001"
SERVICE = "00000000-0000-4000-8000-00000000e001"
RATE = "00000000-0000-4000-8000-00000000f001"
SB = "00000000-0000-4000-8000-00000000b001"
ORDER = "00000000-0000-4000-8000-000000000a01"
DAY = date(2026, 12, 14)

DEPARTURE = "FROM booking.departures d JOIN booking.trip_plans tp ON tp.id = d.trip_plan_id WHERE d.id"
USAGE = "FROM booking.b2b_hold_lines l JOIN booking.b2b_holds h"
SERVICE_ROW = "FROM booking.services WHERE id"
RATES = "FROM unnest($1::uuid[]"
COST_ROW = "FROM booking.departure_costs dc JOIN booking.suppliers s"
COST_DEP = "SELECT departure_id::text FROM booking.departure_costs"
AUDIT = "INSERT INTO booking.audit_logs"
FX_SQL = "FROM booking.fx_rates f"


def departure_row(**over: Any) -> dict[str, Any]:
    return {"id": DEP, "trip_plan_id": "p-1", "trip_plan_source_id": "tour-1", "start_date": DAY, "capacity": 20,
            "status": "scheduled", **over}  # fmt: skip


def cost_row(**over: Any) -> dict[str, Any]:
    return {
        "id": COST, "departure_id": DEP, "supplier_id": SUP, "supplier_code": "SUP-1", "supplier_name": "Kestrel",
        "service_id": SERVICE, "service_code": "SV-1", "service_title": "Coach day", "supplier_rate_id": RATE,
        "item_description": "Coach day", "service_date": DAY, "basis": "group", "quantity": 2, "pricing": "rate",
        "unit_amount": Decimal("300.00"), "net_amount": Decimal("600.00"), "currency": "USD", "priced_for_pax": 6,
        "priced_at": None, "frozen_at": None, "supplier_booking_id": None, "supplier_booking_code": None,
        "supplier_booking_status": None, "supplier_order_code": None, "status": "active", "cancelled_at": None,
        "cancel_reason": None, "notes": None, "created_by": "c-1", "created_at": None, "updated_at": None,
        "planned_usd": Decimal("600.00"), "billed": False, "billed_usd": None, "cost_usd": Decimal("600.00"),
        **over,
    }  # fmt: skip


def rate(net: str = "300.00", unit: str = "group") -> list[dict[str, Any]]:
    return [{"n": 1, "id": RATE, "unit": unit, "net_amount": net, "currency": "USD", "season": None}]


def cost_conn(row: dict[str, Any] | None = None, *, sold: int = 6, **rules: Any) -> ScriptedConn:
    conn = ScriptedConn(
        [
            (DEPARTURE, departure_row()),
            (USAGE, [{"departure_id": DEP, "held": 0, "sold": sold}]),
            (SERVICE_ROW, {"id": SERVICE, "supplier_id": SUP, "title": "Coach day"}),
            (RATES, rate()),
            ("INSERT INTO booking.departure_costs", COST),
            (COST_ROW, row if row is not None else cost_row()),
            (COST_DEP, DEP),
        ]
    )
    for fragment, answer in rules.items():
        conn.on(fragment, answer)
    return conn


def actions(conn: ScriptedConn) -> list[str]:
    return [args[3] for args in conn.sql(AUDIT)]


# ── Permissions, registry ────────────────────────────────────────────────────


async def test_profit_permission_and_job_kinds() -> None:
    assert PERMISSIONS["profit.read"] == frozenset({"admin", "finance"})
    assert "profit.read" not in PREVIEW_PERMISSIONS
    assert FIN.can("profit.read") and not OPS.can("profit.read")
    assert costs.KIND in EXPECTED_SECONDS and orders.KIND in EXPECTED_SECONDS


# ── FX ───────────────────────────────────────────────────────────────────────


async def test_fx_rounding_rules() -> None:
    assert fx.to_usd("7000.00", "36") == Decimal("194.44")  # 194.444 -> half up to the cent
    assert fx.to_usd("1", "8") == Decimal("0.13")  # 0.125 -> 0.13 (half up, not banker's)
    assert fx.from_usd("1655.56", "0.8") == Decimal("1324.45")
    assert fx.fx_difference_usd("7000.00", booked_rate="35", billed_rate="36") == Decimal("-5.56")
    assert fx.share("200.00", 3, 4) == Decimal("150.00") and fx.share("100.00", 1, 3) == Decimal("33.33")
    with pytest.raises(ValueError):
        fx.to_usd("1", "0")
    with pytest.raises(ValueError):
        fx.share("1", 1, 0)


async def test_fx_plain_rates_and_snapshot() -> None:
    row = {"currency": "THB ", "rate_per_usd": Decimal("35.5"), "buffer_pct": Decimal("0.02"), "as_of": DAY,
           "source": "bank"}  # fmt: skip
    conn = ScriptedConn([(FX_SQL, [row])])
    rates = await fx.plain_rates(conn, ["thb", "USD", ""], DAY)
    assert rates["USD"].rate_per_usd == 1 and rates["THB"].rate_per_usd == Decimal("35.5")  # no buffer
    assert await fx.snapshot(conn, "THB", DAY) == {"fx_rate_per_usd": Decimal("35.5"), "fx_as_of": DAY}
    assert await fx.snapshot(ScriptedConn(), "XAF", DAY) == {"fx_rate_per_usd": None, "fx_as_of": None}
    assert await fx.snapshot(conn, None, DAY) == {"fx_rate_per_usd": None, "fx_as_of": None}


# ── Departure costs ──────────────────────────────────────────────────────────


async def test_price_by_basis() -> None:
    assert costs.price("300.00", basis="group", quantity=2, pax=6) == Decimal("600.00")
    assert costs.price("12.50", basis="person", quantity=1, pax=3) == Decimal("37.50")
    assert costs.price("12.50", basis="person", quantity=1, pax=0) == Decimal("0.00")
    with pytest.raises(ValueError):
        costs.price("1", basis="room_night", quantity=1, pax=1)


async def test_create_rate_cost_for_the_sold_seats() -> None:
    conn = cost_conn()
    out = await costs.create(conn, OPS, DEP, {"service_id": SERVICE, "quantity": 2})
    (insert,) = conn.sql("INSERT INTO booking.departure_costs")
    assert insert[1:4] == (SUP, SERVICE, RATE) and insert[6:][:4] == ("group", 2, "rate", Decimal("300.00"))
    assert insert[10:13] == (Decimal("600.00"), "USD", 6)
    (ask,) = conn.sql(RATES)
    assert ask[3] == [6]  # the pax band of the sold seats
    assert out["supplier"]["name"] == "Kestrel" and out["actions"] == {"edit": True, "order": True, "remove": True}
    assert actions(conn) == ["departure_cost.created"]


async def test_create_manual_cost_and_refusals() -> None:
    conn = cost_conn(sold=3)
    body = {"supplier_id": SUP, "basis": "person", "unit_amount": Decimal("12.50"), "currency": "usd",
            "item_description": "Entrance"}  # fmt: skip
    await costs.create(conn, OPS, DEP, body)
    (insert,) = conn.sql("INSERT INTO booking.departure_costs")
    assert insert[3] is None and insert[8] == "manual" and insert[10] == Decimal("37.50") and insert[11] == "USD"
    cases: list[tuple[ScriptedConn, dict[str, Any], type[Exception], str]] = [
        (cost_conn(**{DEPARTURE: departure_row(status="cancelled")}), body, Conflict, "departure_cancelled"),
        (cost_conn(), {**body, "pricing": "bid"}, Invalid, "invalid_pricing"),
        (cost_conn(), {**body, "quantity": 1000}, Invalid, "invalid_quantity"),
        (cost_conn(), {"basis": "group"}, Invalid, "supplier_required"),
        (cost_conn(), {"service_id": SERVICE, "supplier_id": DEP}, Invalid, "service_supplier_mismatch"),
        (cost_conn(), {"supplier_id": SUP, "pricing": "rate"}, Invalid, "service_required"),
        (cost_conn(**{RATES: []}), {"service_id": SERVICE}, Conflict, "no_rate_in_force"),
        (cost_conn(**{RATES: rate(unit="room_night")}), {"service_id": SERVICE}, Conflict, "rate_not_shared"),
        (cost_conn(), {**body, "basis": "seat"}, Invalid, "invalid_basis"),
        (cost_conn(), {**body, "currency": "U1"}, Invalid, "invalid_currency"),
        (cost_conn(), {**body, "unit_amount": "1.001"}, Invalid, "invalid_amount"),
        (cost_conn(), {**body, "unit_amount": "abc"}, Invalid, "invalid_amount"),
        (
            cost_conn(**{SERVICE_ROW: {"id": SERVICE, "supplier_id": SUP, "title": ""}}),
            {"service_id": SERVICE, "item_description": " "},
            Invalid,
            "description_required",
        ),  # fmt: skip
        (cost_conn(**{DEPARTURE: None}), body, NotFound, "departure_not_found"),
        (cost_conn(**{SERVICE_ROW: None}), {"service_id": SERVICE}, NotFound, "service_not_found"),
    ]
    for c, data, error, code in cases:
        with pytest.raises(error) as exc:
            await costs.create(c, OPS, DEP, data)
        assert exc.value.code == code, code
    with pytest.raises(Forbidden):
        await costs.create(cost_conn(), FIN, DEP, body)  # finance has no pricing.read
    with pytest.raises(NotFound):
        await costs.create(cost_conn(), OPS, "not-a-uuid", body)


async def test_update_reprices_and_moves_a_pending_booking() -> None:
    row = cost_row(pricing="manual", supplier_rate_id=None, basis="person", unit_amount=Decimal("12.50"),
                   net_amount=Decimal("75.00"), supplier_booking_id=SB, supplier_booking_status="pending")  # fmt: skip
    conn = cost_conn(row)
    out = await costs.update(conn, OPS, COST, {"unit_amount": Decimal("14.00"), "notes": " ok "})
    assert out["changed"] == ["notes", "unit_amount", "net_amount"]
    (upd,) = conn.sql("UPDATE booking.departure_costs SET")
    assert upd[2:] == ("ok", Decimal("14.00"), Decimal("168.00"))  # 14 x 2 x 6 sold seats
    (moved,) = conn.sql("UPDATE booking.supplier_bookings SET net_amount")
    assert moved[1:5] == (Decimal("168.00"), "USD", 6, DAY)
    assert actions(conn) == ["departure_cost.updated"]
    unchanged = cost_conn(cost_row())
    assert (await costs.update(unchanged, OPS, COST, {}))["changed"] == []
    refusals: list[tuple[dict[str, Any], dict[str, Any], type[Exception], str]] = [
        (cost_row(status="cancelled"), {"notes": "x"}, Conflict, "departure_cost_closed"),
        (cost_row(frozen_at=datetime(2026, 11, 1)), {"quantity": 3}, Conflict, "departure_cost_frozen"),
        (cost_row(), {"unit_amount": Decimal("1")}, Invalid, "rate_priced"),
        (cost_row(), {"item_description": " "}, Invalid, "description_required"),
        (cost_row(), {"quantity": 0}, Invalid, "invalid_quantity"),
        (cost_row(pricing="manual"), {"basis": "seat"}, Invalid, "invalid_basis"),
        (cost_row(pricing="manual"), {"unit_amount": Decimal("-1")}, Invalid, "invalid_amount"),
    ]
    for r, changes, error, code in refusals:
        with pytest.raises(error) as exc:
            await costs.update(cost_conn(r), OPS, COST, changes)
        assert exc.value.code == code, code
    with pytest.raises(NotFound):
        await costs.update(cost_conn(**{COST_DEP: None}), OPS, COST, {"notes": "x"})


async def test_rate_cost_reprices_to_the_band_of_the_new_seat_count() -> None:
    conn = cost_conn(sold=10, **{RATES: rate("480.00")})
    out = await costs.update(conn, OPS, COST, {"service_date": DAY})
    assert out["changed"] == ["unit_amount", "net_amount", "priced_for_pax"]
    assert conn.sql("UPDATE booking.departure_costs SET")[0][2:] == (Decimal("480.00"), Decimal("960.00"), 10)


async def test_order_and_remove() -> None:
    conn = cost_conn(**{"INSERT INTO booking.supplier_bookings": SB})
    await costs.order(conn, OPS, COST)
    (ins,) = conn.sql("INSERT INTO booking.supplier_bookings")
    assert ins[:3] == (DEP, SUP, SERVICE) and ins[5:8] == (6, Decimal("600.00"), "USD")
    assert conn.sql("SET supplier_booking_id")[0][1] == SB and actions(conn) == ["departure_cost.ordered"]
    ordered = cost_conn(cost_row(supplier_booking_id=SB, supplier_booking_status="pending"))
    assert (await costs.order(ordered, OPS, COST))["supplier_booking"]["id"] == SB
    assert ordered.sql("INSERT INTO booking.supplier_bookings") == []
    with pytest.raises(Conflict):
        await costs.order(cost_conn(cost_row(status="cancelled")), OPS, COST)

    pending = cost_conn(cost_row(supplier_booking_id=SB, supplier_booking_status="pending"))
    await costs.remove(pending, OPS, COST, " no longer needed ")
    assert pending.sql("SET status = 'cancelled', cancel_reason")  # the pending booking goes with it
    assert pending.sql("UPDATE booking.departure_costs SET status = 'cancelled'")[0][2] == "no longer needed"
    with pytest.raises(Conflict) as exc:
        await costs.remove(cost_conn(cost_row(supplier_booking_id=SB, supplier_booking_status="requested",
                                              supplier_booking_code="SB-1")), OPS, COST, "x")  # fmt: skip
    assert exc.value.code == "departure_cost_ordered"
    with pytest.raises(Invalid):
        await costs.remove(cost_conn(), OPS, COST, " ")
    done = cost_conn(cost_row(status="cancelled"))
    assert (await costs.remove(done, OPS, COST, "again"))["status"] == "cancelled" and actions(done) == []


def departure_view_conn() -> ScriptedConn:
    case = {"trip_case_id": "tc-1", "code": "TC-1", "agency_name": "Heron Trails", "revenue": Decimal("2000"),
            "revenue_usd": Decimal("2000"), "cost_usd": Decimal("1000"), "shared_cost_usd": Decimal("360.00"),
            "profit_usd": Decimal("1000"), "margin": Decimal("0.5")}  # fmt: skip
    prof = {"revenue_usd": Decimal("2000"), "case_cost_usd": Decimal("400"), "cost_usd": Decimal("1000"),
            "profit_usd": Decimal("1000"), "profit_per_seat_usd": Decimal("100"), "margin": Decimal("0.5"),
            "fx_missing": False}  # fmt: skip
    return cost_conn(
        **{
            COST_ROW: [cost_row(billed=True, billed_usd=Decimal("620.00")), cost_row(status="cancelled")],
            "FROM booking.v_case_profit p": [case],
            "FROM booking.supplier_bills b": [{"code": "BIL-1"}],
            "FROM booking.v_departure_profit WHERE": prof,
        }
    )


def listing_conn() -> ScriptedConn:
    row = {"id": DEP, "sold_seats": 6, "cost_usd": Decimal("1000"), "revenue_usd": Decimal("2000"),
           "profit_usd": Decimal("1000"), "profit_per_seat_usd": Decimal("100"), "margin": Decimal("0.5")}  # fmt: skip
    return ScriptedConn([("count(*)", 1), ("FROM booking.departures d JOIN", [row])])


async def test_departure_view_and_list() -> None:
    out = await costs.departure_costs(departure_view_conn(), ADMIN, DEP)
    assert out["departure"]["sold_seats"] == 6 and len(out["shared_costs"]) == 2
    assert out["totals_usd"]["shared_billed"] == "620.00" and out["totals_usd"]["profit"] == "1000"
    assert out["cases"][0]["profit_usd"] == "1000" and out["totals_usd"]["margin"] == "0.5"
    empty = await costs.departure_costs(cost_conn(**{COST_ROW: [], "FROM booking.v_departure_profit WHERE": None}),
                                        ADMIN, DEP)  # fmt: skip
    assert empty["totals_usd"]["profit"] == "0" and empty["totals_usd"]["margin"] is None
    page = await costs.list_departures(listing_conn(), ADMIN, start=DAY, end=DAY)
    assert page["total"] == 1 and page["items"][0]["profit_usd"] == "1000"
    with pytest.raises(Invalid):
        await costs.list_departures(ScriptedConn(), OPS, limit=0)


async def test_departure_reads_hide_realised_profit_without_profit_read() -> None:
    """Sales and contracting read the costs (bill.read + pricing.read) but not the realised revenue / profit / margin:
    that is profit.read (admin, finance)."""
    for staff in (SALES, OPS):
        assert not staff.can("profit.read")
        out = await costs.departure_costs(departure_view_conn(), staff, DEP)
        case, totals = out["cases"][0], out["totals_usd"]
        assert all(case[k] is None for k in costs.CASE_PROFIT_FIELDS)
        assert case["cost_usd"] == "1000" and case["shared_cost_usd"] == "360.00" and case["agency_name"]
        assert all(totals[k] is None for k in costs.TOTAL_PROFIT_FIELDS)
        assert totals["shared_billed"] == "620.00" and totals["cost"] == "1000" and totals["case_cost"] == "400"
        page = await costs.list_departures(listing_conn(), staff)
        item = page["items"][0]
        assert all(item[k] is None for k in costs.LIST_PROFIT_FIELDS)
        assert item["cost_usd"] == "1000" and item["sold_seats"] == 6


CATALOG = "FROM catalog.v_tour WHERE source_system = 'cis'"
TOUR_SOURCE = "6f1c2a9e-0000-4000-8000-000000000041"


async def test_departure_reads_carry_the_catalog_tour_title() -> None:
    """S277 UAT fix: the finance departure list, the departure view and the profit per departure name the tour from
    the CIS catalog (one read per page), not the raw catalog key; a missing title or an unreadable catalog leaves
    ``tour_title`` null and the read still answers."""
    row = {"id": DEP, "trip_plan_source_id": TOUR_SOURCE, "sold_seats": 6, "cost_usd": Decimal("1000")}
    other = {"id": COST, "trip_plan_source_id": "tour-unknown", "sold_seats": 0, "cost_usd": None}
    titles = [{"source_id": TOUR_SOURCE, "title": "Lantern Coast Loop"}]
    conn = ScriptedConn([("count(*)", 2), ("FROM booking.departures d JOIN", [row, other]), (CATALOG, titles)])
    page = await costs.list_departures(conn, ADMIN)
    assert [i["tour_title"] for i in page["items"]] == ["Lantern Coast Loop", None]
    assert conn.sql(CATALOG) == [([TOUR_SOURCE, "tour-unknown"],)]  # one catalog read for the page

    broken = ScriptedConn([("count(*)", 1), ("FROM booking.departures d JOIN", [row])])
    broken.on(CATALOG, asyncpg.UndefinedTableError("no catalog"))
    assert (await costs.list_departures(broken, SALES))["items"][0]["tour_title"] is None

    view = departure_view_conn().on(CATALOG, [{"source_id": "tour-1", "title": "Lantern Coast Loop"}])
    out = await costs.departure_costs(view, OPS, DEP)
    assert out["departure"]["tour_title"] == "Lantern Coast Loop"
    assert out["departure"]["trip_plan_source_id"] == "tour-1"
    assert (await costs.departure_costs(departure_view_conn(), OPS, DEP))["departure"]["tour_title"] is None

    profit_row = {"departure_id": DEP, "trip_plan_source_id": TOUR_SOURCE, "profit_usd": Decimal("10")}
    deps = ScriptedConn([("FROM booking.v_departure_profit p", [profit_row]), (CATALOG, titles)])
    prof = await profit.departures(deps)
    assert prof["items"][0]["tour_title"] == "Lantern Coast Loop" and prof["totals"]["profit_usd"] == "10"
    assert (await profit.departures(ScriptedConn(), offset=5))["items"] == []


async def test_recompute_job_and_schedule() -> None:
    conn = cost_conn(
        sold=10,
        **{RATES: rate("480.00"), "SELECT DISTINCT departure_id::text": [{"id": DEP}], COST_ROW: cost_row()},
    )
    conn.on("AND dc.pricing = 'rate' ORDER BY dc.id FOR UPDATE OF dc", [cost_row()])
    out = await costs.handle(conn, Job("j-1", costs.KIND, {"departure_id": DEP}, 1, 3))  # type: ignore[arg-type]
    assert out == {"outcome": "ok", "departures": 1, "costs": 1, "repriced": 1, "unchanged": 0, "failed": 0}
    assert actions(conn) == ["departure_cost.repriced"]
    broken = cost_conn(**{"SELECT DISTINCT departure_id::text": [{"id": DEP}]})
    broken.on("FOR UPDATE OF dc", RuntimeError("boom"))
    with pytest.raises(RuntimeError, match="1 departure"):
        await costs.run(broken)  # type: ignore[arg-type]
    sched = costs.RecomputeSchedule()
    pool = ScriptedConn([("concat_ws", "d1:6:t1|3:x"), ("INSERT INTO booking.job", "job-1")])
    assert await sched(pool) == "job-1"  # type: ignore[arg-type]
    assert await sched(pool) is None  # the same watermark: once  # type: ignore[arg-type]


async def test_schedule_requeues_when_the_seats_go_back() -> None:
    """4 seats sold (job A), a 5th confirmed (job B), then cancelled: back to 4 seats, but the hold change time moved
    on, so the watermark (and the job key) is new and a third job re-prices for 4. The queue's unique key is modelled
    by a set: a key met before gets no job, as booking.job's unique idempotency index does."""
    marks = iter(["d1:4:2026-12-01 10:00:00+00|3:x", "d1:5:2026-12-01 10:05:00+00|3:x",
                  "d1:4:2026-12-01 10:09:00+00|3:x"])  # fmt: skip
    keys: list[str] = []

    async def enqueue(_pool: Any, kind: str, payload: dict[str, Any], *, idempotency_key: str, **_: Any) -> str | None:
        if idempotency_key in keys:
            return None
        keys.append(idempotency_key)
        return f"job-{len(keys)}"

    pool = ScriptedConn()
    pool.on("concat_ws", lambda *_: next(marks))
    sched = costs.RecomputeSchedule()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(costs.queue, "enqueue", enqueue)
        jobs = [await sched(pool) for _ in range(3)]  # type: ignore[arg-type]
    assert jobs == ["job-1", "job-2", "job-3"] and len(set(keys)) == 3


# ── Purchase orders ──────────────────────────────────────────────────────────

ORDER_ROW = "FROM booking.supplier_orders o JOIN booking.suppliers s"
LINES = "FROM booking.supplier_bookings b LEFT JOIN booking.trip_cases tc"


def order_row(**over: Any) -> dict[str, Any]:
    return {
        "id": ORDER, "code": "PO-000001", "supplier_id": SUP, "supplier_code": "SUP-1", "supplier_name": "Orchid",
        "supplier_email": "desk@example.test", "currency": "USD", "service_month": date(2026, 12, 1),
        "status": "draft", "total_net": None, "snapshot": None, "sent_to": None, "redirected": False,
        "send_requested_at": None, "sent_by": None, "sent_at": None, "delivery_ref": None, "send_error": None,
        "note": None, "cancelled_at": None, "cancelled_by": None, "cancel_reason": None, "created_by": "c-1",
        "created_at": None, "updated_at": None, "lines": 2, "line_statuses": ["pending", "pending"],
        "live_net": Decimal("200.00"), **over,
    }  # fmt: skip


def line(code: str = "SB-000001", **over: Any) -> dict[str, Any]:
    return {"id": SB, "code": code, "status": "pending", "item_description": "Lodge, two nights",
            "service_date": DAY, "nights": 2, "pax": 2, "net_amount": Decimal("120.00"), "net_currency": "USD",
            "confirmation_ref": None, "trip_case_id": "tc-1", "trip_case_code": "TC-000001", "departure_id": None,
            "departure_start_date": None, **over}  # fmt: skip


def order_conn(row: dict[str, Any] | None = None, lines: list[dict[str, Any]] | None = None, **rules: Any):
    conn = ScriptedConn([(ORDER_ROW, row if row is not None else order_row()), (LINES, lines or [line()])])
    for fragment, answer in rules.items():
        conn.on(fragment, answer)
    return conn


async def test_order_state_reference_and_rendering() -> None:
    assert orders.order_state("draft", []) == "draft"
    assert orders.order_state("sent", ["requested", "requested"]) == "sent"
    assert orders.order_state("sent", ["confirmed", "requested", "cancelled"]) == "partly_confirmed"
    assert orders.order_state("sent", ["confirmed", "declined"]) == "confirmed"
    assert orders.reference({"trip_case_code": "TC-1"}) == "TC-1"
    assert orders.reference({"departure_start_date": DAY}) == "Group departure 2026-12-14"
    snap = orders.snapshot_lines([line(), line("SB-2", nights=None, pax=None, net_amount=None)])
    assert snap[0]["net_amount"] == "120.00" and snap[1]["net_amount"] is None
    view = {"code": "PO-1", "supplier": {"code": "S", "name": "Orchid <Stays>"}, "currency": "USD",
            "service_month": "2026-12-01", "lines": snap, "total_net": "120.00", "note": "Late arrival"}  # fmt: skip
    html = orders.render_html(view, draft=True, redirect_note="Test copy")
    assert "Orchid &lt;Stays&gt;" in html and "Draft, not sent yet." in html and "Late arrival" in html
    text = orders.render_text(view, redirect_note="Test copy")
    assert "2 night(s) 2 pax" in text and text.startswith("Test copy") and "Total net: 120.00 USD" in text


async def test_create_orders_groups_by_supplier_currency_month() -> None:
    rows = [
        {"id": "b1", "code": "SB-1", "supplier_id": SUP, "net_amount": Decimal("1"), "net_currency": "USD",
         "service_date": DAY},
        {"id": "b2", "code": "SB-2", "supplier_id": SUP, "net_amount": Decimal("1"), "net_currency": "USD",
         "service_date": date(2027, 1, 3)},
        {"id": "b3", "code": "SB-3", "supplier_id": SUP, "net_amount": None, "net_currency": None,
         "service_date": DAY},
        {"id": "b4", "code": "SB-4", "supplier_id": SUP, "net_amount": Decimal("1"), "net_currency": "USD",
         "service_date": None},
    ]  # fmt: skip
    conn = order_conn(**{"WHERE b.status = 'pending'": rows, "INSERT INTO booking.supplier_orders": ORDER})
    conn.on("AND service_month = $3 AND status = 'draft'", lambda s, c, m: ORDER if m == DAY.replace(day=1) else None)
    out = await orders.create_orders(conn, OPS, trip_cases=["tc-000001"], departures=[DEP])
    assert out["lines"] == 2 and out["skipped"] == {"no_net": 1, "no_date": 1}
    assert [o["created"] for o in out["orders"]] == [False, True]
    assert conn.sql("WHERE b.status = 'pending'")[0][0] == ["TC-000001", "tc-000001"]
    assert actions(conn) == ["supplier_order.lines_added", "supplier_order.created"]
    for kwargs, code in (({}, "scope_required"), ({"departures": ["x"]}, "invalid_departure"),
                         ({"trip_cases": ["TC"] * 51}, "scope_too_large")):  # fmt: skip
        with pytest.raises(Invalid) as exc:
            await orders.create_orders(order_conn(), OPS, **kwargs)
        assert exc.value.code == code
    with pytest.raises(Conflict):
        bad = order_conn(**{"WHERE b.status = 'pending'": rows[:1], "SET supplier_order_id = $1::uuid": _check()})
        await orders.create_orders(bad, OPS, trip_cases=["TC-1"])


def _check() -> Exception:
    import asyncpg

    return asyncpg.CheckViolationError("not of the supplier")


async def test_order_reads() -> None:
    out = await orders.detail(order_conn(order_row(snapshot=json.dumps({"code": "PO-000001"}))), OPS, "po-000001")
    assert out["snapshot"] == {"code": "PO-000001"} and out["lines"][0]["code"] == "SB-000001"
    assert out["supplier"]["email"] == "desk@example.test" and out["actions"]["send"] is True
    assert out["line_counts"] == {"pending": 2}
    page = await orders.list_orders(
        order_conn(**{ORDER_ROW: [order_row()], "SELECT count(*) FROM booking.supplier_orders o": 1}),
        OPS,
        status="open",
        supplier=SUP,
    )
    assert page["total"] == 1 and "snapshot" not in page["items"][0]
    for kwargs in ({"status": "lost"}, {"limit": 0}, {"supplier": "Orchid"}):
        with pytest.raises(Invalid):
            await orders.list_orders(order_conn(), OPS, **kwargs)
    with pytest.raises(NotFound):
        await orders.detail(order_conn(**{ORDER_ROW: None}), OPS, ORDER)
    with pytest.raises(Forbidden):
        await orders.detail(order_conn(), FIN, ORDER)
    html = await orders.document(order_conn(), OPS, ORDER)
    assert html.startswith("<!doctype html>") and "TC-000001" in html and "Draft, not sent yet." in html


async def test_send_fixes_the_snapshot_and_queues_the_job() -> None:
    conn = order_conn(lines=[line(), line("SB-2", net_amount=Decimal("80.00")), line("SB-3", status="cancelled")])
    await orders.send(conn, OPS, ORDER, note=" Early check-in ")
    (upd,) = conn.sql("SET status = 'sending'")
    snap = json.loads(upd[1])
    assert [x["code"] for x in snap["lines"]] == ["SB-000001", "SB-2"] and snap["total_net"] == "200.00"
    assert upd[2:] == (Decimal("200.00"), "desk@example.test", "c-1", "Early check-in")
    (job,) = conn.sql("INSERT INTO booking.job")
    assert job[0] == orders.KIND and json.loads(job[1]) == {"order_id": ORDER}
    assert actions(conn) == ["supplier_order.send_requested"]
    twice = order_conn(order_row(status="sending"))
    await orders.send(twice, OPS, ORDER)
    assert twice.sql("SET status = 'sending'") == []
    for row, lines, code in (
        (order_row(status="cancelled"), None, "supplier_order_closed"),
        (order_row(supplier_email=None), None, "supplier_no_email"),
        (order_row(), [line(status="confirmed")], "supplier_order_empty"),
    ):
        with pytest.raises(Conflict) as exc:
            await orders.send(order_conn(row, lines), OPS, ORDER)
        assert exc.value.code == code


async def test_cancel_and_remove_line() -> None:
    conn = order_conn(
        **{"WHERE supplier_order_id = $1::uuid AND status = 'pending' RETURNING code": [{"code": "SB-1"}]}
    )
    await orders.cancel(conn, OPS, ORDER, " supplier closed ")
    assert conn.sql("SET status = 'cancelled'")[0][2] == "supplier closed"
    assert actions(conn) == ["supplier_order.cancelled"]
    done = order_conn(order_row(status="cancelled"))
    await orders.cancel(done, OPS, ORDER, "again")
    assert actions(done) == []
    with pytest.raises(Invalid):
        await orders.cancel(order_conn(), OPS, ORDER, "")
    removed = order_conn(**{"SET supplier_order_id = NULL, updated_by = $3": "SB-000001"})
    await orders.remove_line(removed, OPS, ORDER, SB)
    assert actions(removed) == ["supplier_order.line_removed"]
    with pytest.raises(NotFound):
        await orders.remove_line(order_conn(), OPS, ORDER, SB)
    with pytest.raises(Conflict):
        await orders.remove_line(order_conn(order_row(status="sent")), OPS, ORDER, SB)


def _settings(**over: Any) -> Settings:
    base = {"database_url": None, "environment": "dev", "ses_from_address": "ops@example.test",
            "staff_notify_email": "staff@example.test"}  # fmt: skip
    return Settings(**{**base, **over})


SENDING = order_row(
    status="sending", sent_to="desk@example.test", sent_by="c-1",
    snapshot={"code": "PO-000001", "supplier": {"code": "SUP-1", "name": "Orchid"}, "currency": "USD",
              "service_month": "2026-12-01", "lines": orders.snapshot_lines([line()]), "total_net": "120.00",
              "note": None},
)  # fmt: skip


async def test_run_send_mails_and_requests_the_lines() -> None:
    sent: list[mailer.Message] = []

    async def send_mail(message: mailer.Message) -> str:
        sent.append(message)
        return "msg-1"

    conn = order_conn(SENDING)
    conn.on("WHERE b.supplier_order_id = $1::uuid AND b.status = 'pending'", [{"id": SB, "code": "SB-1",
                                                                              "trip_case_id": "tc-1"}])  # fmt: skip
    case = {"id": "tc-1", "code": "TC-1", "status": "confirmed", "channel": "b2b"}
    conn.on("FROM booking.trip_cases WHERE id = $1::uuid FOR UPDATE", case)
    conn.on("AS asked FROM booking.supplier_bookings", {"live": 1, "asked": 1})
    conn.on("SELECT status FROM booking.trip_cases WHERE id", "confirmed")
    out = await orders.run_send(conn, _settings(), send_mail, ORDER)  # type: ignore[arg-type]
    assert out == {"outcome": "sent", "order": "PO-000001", "requested": 1, "redirected": True}
    (message,) = sent
    assert message.to == "staff@example.test" and "desk@example.test" in message.text
    assert message.subject == "[dev] Purchase order PO-000001"
    assert conn.sql("SET status = 'sent'")[0][1:3] == ("msg-1", True)
    assert conn.sql("SET status = 'requested'")[0] == (SB, "c-1")
    assert "supplier_booking.requested" in actions(conn) and "supplier_order.sent" in actions(conn)
    prod = order_conn(SENDING)
    out = await orders.run_send(prod, _settings(environment="prod"), send_mail, ORDER)  # type: ignore[arg-type]
    assert sent[-1].to == "desk@example.test" and out["redirected"] is False


async def test_run_send_refusals() -> None:
    async def refused(message: mailer.Message) -> str:
        raise mailer.MailError("MessageRejected", "no", permanent=True)

    async def throttled(message: mailer.Message) -> str:
        raise mailer.MailError("TooManyRequestsException", "slow down", permanent=False)

    conn = order_conn(SENDING)
    out = await orders.run_send(conn, _settings(), refused, ORDER)  # type: ignore[arg-type]
    assert out["outcome"] == "not_sent" and conn.sql("SET status = 'draft'")[0][1] == "e-mail refused: MessageRejected"
    with pytest.raises(mailer.MailError):
        await orders.run_send(order_conn(SENDING), _settings(), throttled, ORDER)  # type: ignore[arg-type]
    unset = await orders.run_send(order_conn(SENDING), _settings(ses_from_address=""), refused, ORDER)  # type: ignore[arg-type]
    assert unset["reason"] == "SES_FROM_ADDRESS not set"
    no_inbox = await orders.run_send(order_conn(SENDING), _settings(staff_notify_email=""), refused, ORDER)  # type: ignore[arg-type]
    assert no_inbox["outcome"] == "not_sent"
    skipped = await orders.run_send(order_conn(order_row(status="sent")), _settings(), refused, ORDER)  # type: ignore[arg-type]
    assert skipped["outcome"] == "skipped"


async def test_send_handler() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def send_email(self, **kwargs: Any) -> dict[str, str]:
            self.calls.append(kwargs)
            return {"MessageId": "ses-1"}

    client = Client()
    handle = orders.build_handler(_settings(), client_factory=lambda region: client)
    out = await handle(order_conn(SENDING), Job("j-1", orders.KIND, {"order_id": ORDER}, 1, 5))  # type: ignore[arg-type]
    assert out["outcome"] == "sent" and client.calls[0]["FromEmailAddress"] == "ops@example.test"
    with pytest.raises(PermanentError):
        await handle(order_conn(), Job("j-2", orders.KIND, {"order_id": "x"}, 1, 5))  # type: ignore[arg-type]


# ── Profit ───────────────────────────────────────────────────────────────────


async def test_profit_reads_and_totals() -> None:
    rows = [
        {"code": "TC-1", "revenue_usd": Decimal("1000"), "planned_cost_usd": Decimal("600"),
         "actual_cost_usd": Decimal("0"), "cost_usd": Decimal("600"), "profit_usd": Decimal("400"),
         "fx_missing": False},
        {"code": "TC-2", "revenue_usd": Decimal("500"), "planned_cost_usd": None, "actual_cost_usd": None,
         "cost_usd": Decimal("100"), "profit_usd": Decimal("400"), "fx_missing": True},
    ]  # fmt: skip
    conn = ScriptedConn([("FROM booking.v_case_profit p", rows)])
    out = await profit.cases(conn, start=DAY, end=DAY, limit=1)
    assert out["total"] == 2 and len(out["items"]) == 1
    assert out["totals"]["profit_usd"] == "800" and out["totals"]["margin"] == "0.5333" and out["totals"]["fx_missing"]
    deps = await profit.departures(ScriptedConn([("FROM booking.v_departure_profit p", [])]))
    assert deps["totals"]["margin"] is None and deps["items"] == []
    months = await profit.agencies(ScriptedConn([("FROM booking.v_agency_profit_month m", rows)]))
    assert months["total"] == 2
    with pytest.raises(Invalid):
        await profit.cases(ScriptedConn(), start=DAY, end=date(2026, 1, 1))
    with pytest.raises(Invalid):
        await profit.departures(ScriptedConn(), limit=500)


# ── Routes ───────────────────────────────────────────────────────────────────


def route_client(staff: Staff, monkeypatch: pytest.MonkeyPatch) -> Any:
    from fastapi.testclient import TestClient

    from app.auth.staff import current_staff, get_pool
    from app.main import create_app

    calls: list[tuple[str, Any]] = []

    def stub(module: Any, name: str, result: Any) -> None:
        async def fn(*args: Any, **kwargs: Any) -> Any:
            calls.append((name, (args[2:] if len(args) > 2 else args[1:], kwargs)))
            return result

        monkeypatch.setattr(module, name, fn)

    for name in ("departure_costs", "create", "update", "order", "remove", "list_departures"):
        stub(costs, name, {"ok": name})
    for name in ("list_orders", "create_orders", "detail", "send", "cancel", "remove_line"):
        stub(orders, name, {"ok": name})
    stub(orders, "document", "<!doctype html><p>PO</p>")
    for name in ("cases", "departures", "agencies"):
        stub(profit, name, {"ok": name})
    app = create_app(Settings(database_url=None, environment="test"))
    app.dependency_overrides[current_staff] = lambda: staff
    app.dependency_overrides[get_pool] = lambda: ScriptedConn()
    return TestClient(app), calls


def test_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    admin = Staff("a-1", None, "Admin", "admin", True, "reviewer")
    c, calls = route_client(admin, monkeypatch)
    assert c.get(f"/booking/departures/{DEP}/costs").status_code == 200
    body = {"service_id": SERVICE, "quantity": 2}
    assert c.post(f"/booking/departures/{DEP}/costs", json=body).status_code == 201
    assert calls[-1][1][0][1] == {"service_id": SERVICE, "quantity": 2}
    assert c.post(f"/booking/departures/{DEP}/costs", json={"basis": "room"}).status_code == 422
    assert c.patch(f"/booking/departure-costs/{COST}", json={"quantity": 3}).status_code == 200
    assert calls[-1][1][0][1] == {"quantity": 3}
    assert c.post(f"/booking/departure-costs/{COST}/order").status_code == 200
    assert c.post(f"/booking/departure-costs/{COST}/remove", json={"reason": "x"}).status_code == 200
    assert c.post(f"/booking/departure-costs/{COST}/remove", json={}).status_code == 422
    assert c.get("/booking/finance/departures", params={"from": "2026-12-01"}).status_code == 200
    assert c.get("/booking/supplier-orders", params={"status": "open", "supplier_id": SUP}).status_code == 200
    assert calls[-1][1][1]["supplier"] == SUP
    assert (
        c.post("/booking/supplier-orders", json={"trip_cases": ["TC-000001"], "departures": [DEP]}).status_code == 201
    )
    assert calls[-1][1][1] == {"trip_cases": ["TC-000001"], "departures": [DEP]}
    assert c.get("/booking/supplier-orders/PO-000001").status_code == 200
    doc = c.get("/booking/supplier-orders/PO-000001/document")
    assert doc.status_code == 200 and doc.headers["content-type"].startswith("text/html")
    assert "default-src 'none'" in doc.headers["content-security-policy"]
    assert c.post("/booking/supplier-orders/PO-000001/send", json={"note": "hi"}).status_code == 200
    assert c.post("/booking/supplier-orders/PO-000001/cancel", json={"reason": "x"}).status_code == 200
    assert c.post(f"/booking/supplier-orders/PO-000001/lines/{SB}/remove").status_code == 200
    for path in ("cases", "departures", "agencies"):
        assert c.get(f"/booking/finance/profit/{path}", params={"from": "2026-12-01"}).status_code == 200
    assert c.get("/booking/finance/profit/cases", params={"partner_id": SUP}).status_code == 200


def test_routes_refuse_roles_without_the_permission(monkeypatch: pytest.MonkeyPatch) -> None:
    c, _ = route_client(OPS, monkeypatch)  # contracting: costs and orders, not profit
    assert c.get(f"/booking/departures/{DEP}/costs").status_code == 200
    assert c.get("/booking/finance/profit/cases").status_code == 403
    c, _ = route_client(FIN, monkeypatch)  # finance: profit only
    assert c.get("/booking/finance/profit/agencies").status_code == 200
    assert c.get("/booking/supplier-orders").status_code == 403
    c, _ = route_client(WRITER, monkeypatch)
    assert c.get(f"/booking/departures/{DEP}/costs").status_code == 403


def test_routes_give_sales_no_profit_figures() -> None:
    """Through the routes (real services, scripted DB): a sales user reads the departure costs and the departure list
    without revenue, profit or margin; an admin gets them."""
    from fastapi.testclient import TestClient

    from app.auth.staff import current_staff, get_pool
    from app.main import create_app

    def client(staff: Staff, pool: ScriptedConn) -> TestClient:
        app = create_app(Settings(database_url=None, environment="test"))
        app.dependency_overrides[current_staff] = lambda: staff
        app.dependency_overrides[get_pool] = lambda: pool
        return TestClient(app)

    view = client(SALES, departure_view_conn()).get(f"/booking/departures/{DEP}/costs")
    assert view.status_code == 200
    body = view.json()
    assert {k: body["cases"][0][k] for k in costs.CASE_PROFIT_FIELDS} == dict.fromkeys(costs.CASE_PROFIT_FIELDS)
    assert {k: body["totals_usd"][k] for k in costs.TOTAL_PROFIT_FIELDS} == dict.fromkeys(costs.TOTAL_PROFIT_FIELDS)
    assert body["totals_usd"]["cost"] == "1000"
    listing = client(SALES, listing_conn()).get("/booking/finance/departures")
    assert listing.status_code == 200
    assert {k: listing.json()["items"][0][k] for k in costs.LIST_PROFIT_FIELDS} == dict.fromkeys(
        costs.LIST_PROFIT_FIELDS
    )
    admin = client(ADMIN, listing_conn()).get("/booking/finance/departures").json()
    assert admin["items"][0]["profit_usd"] == "1000" and admin["items"][0]["margin"] == "0.5"
    named = listing_conn().on("FROM catalog.v_tour", [{"source_id": "tour-1", "title": "Lantern Coast Loop"}])
    named.on("FROM booking.departures d JOIN", [{"id": DEP, "trip_plan_source_id": "tour-1", "sold_seats": 6}])
    item = client(ADMIN, named).get("/booking/finance/departures").json()["items"][0]
    assert item["tour_title"] == "Lantern Coast Loop" and item["trip_plan_source_id"] == "tour-1"
