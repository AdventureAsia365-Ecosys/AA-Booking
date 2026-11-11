"""AA-1074 (bk-045) finance services on PostgreSQL 15 with the RDS role shape, as role ``booking``:

1. FX snapshots: a supplier booking confirmed in THB takes the approved plain rate of the day; a bill approved later
   takes its own; the case cost panel shows USD per line, the realised FX difference and the totals.
2. Departure shared costs: a rate-priced coach picks the band for the sold seats, re-prices when another hold is
   confirmed (``bk_departure_cost_recompute``), is ordered (a departure-level supplier booking) and freezes once the
   booking is requested; the departure cost view splits it to the cases by seats.
3. Purchase orders: Create orders groups pending bookings per supplier + currency + month, Send fixes the snapshot and
   queues the e-mail job, the job sends (a fake mailer) and requests the lines; the printable order names no agency.
4. Profit per case, departure and agency month.

Each test runs in one transaction rolled back at the end (the job's pool is a stand-in that hands out the same
connection; nested transactions are savepoints). Invented names only.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, timedelta
from decimal import Decimal

import asyncpg
import pytest
import pytest_asyncio

from app import mailer
from app.auth.staff import Staff
from app.errors import Conflict
from app.finance import departure_costs, profit, supplier_orders
from app.ops import supplier_bills, supplier_bookings
from app.settings import Settings

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

START = date.today() + timedelta(days=75)
MAKER, CHECKER = "it-1074-ops", "it-1074-checker"
OPS = Staff(MAKER, None, "Ops One", "contracting", True, "reviewer")
APPROVER = Staff(CHECKER, None, "Admin Two", "admin", True, "reviewer")
FINANCE = Staff("it-1074-fin", None, "Finance Three", "finance", True, "reviewer")


class OneConnPool:
    """A pool stand-in for the jobs: hands out the test's connection (its transactions become savepoints)."""

    def __init__(self, conn: asyncpg.Connection) -> None:
        self.conn = conn

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[asyncpg.Connection]:
        yield self.conn


@pytest_asyncio.fixture
async def conn(migrated_booking_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    c = await asyncpg.connect(migrated_booking_dsn)
    tx = c.transaction()
    await tx.start()
    for staff in (OPS, APPROVER, FINANCE):
        await c.execute(
            "INSERT INTO booking.staff_users (cis_user_id, role, active, name) VALUES ($1, $2, true, $3)"
            " ON CONFLICT (cis_user_id) DO NOTHING",
            staff.cis_user_id,
            staff.role,
            staff.name,
        )
    try:
        yield c
    finally:
        await tx.rollback()
        await c.close()


async def _fx(c: asyncpg.Connection, currency: str, rate: str, as_of: date) -> None:
    fid = await c.fetchval(
        "INSERT INTO booking.fx_rates (currency, rate_per_usd, buffer_pct, as_of, source, created_by)"
        " VALUES ($1, $2::numeric, 0.02, $3, 'it-1074', $4) RETURNING id",
        currency,
        rate,
        as_of,
        MAKER,
    )
    await c.execute(
        "INSERT INTO booking.policy_approvals (entity_type, fx_rate_id, approved_by) VALUES ('fx_rate', $1, $2)",
        fid,
        CHECKER,
    )


async def _supplier(c: asyncpg.Connection, name: str, *, bands: tuple[tuple[int, int, str], ...] = ()) -> dict:
    """A transport supplier with a coach service and, per band (pax_min, pax_max, net), a group rate in USD."""
    sid = await c.fetchval(
        "INSERT INTO booking.suppliers (company_name, categories, country_code, contact_email)"
        " VALUES ($1, ARRAY['transport'], 'VN', 'reservations@example.test') RETURNING id::text",
        f"it-1074 {name}",
    )
    service = await c.fetchval(
        "INSERT INTO booking.services (supplier_id, service_type, title) VALUES ($1::uuid, 'transfer', $2)"
        " RETURNING id::text",
        sid,
        f"{name} coach day",
    )
    if bands:
        contract = await c.fetchval(
            "INSERT INTO booking.supplier_contracts (supplier_id, title, valid_from, currency, status, created_by)"
            " VALUES ($1::uuid, 'Group rates', current_date - 30, 'USD', 'draft', $2) RETURNING id",
            sid,
            MAKER,
        )
        for low, high, net in bands:
            await c.execute(
                "INSERT INTO booking.supplier_rates (supplier_contract_id, service_id, valid_from, unit, pax_min,"
                " pax_max, net_amount, currency, created_by)"
                " VALUES ($1, $2::uuid, current_date - 30, 'group', $3, $4, $5::numeric, 'USD', $6)",
                contract,
                service,
                low,
                high,
                net,
                MAKER,
            )
        await c.execute(
            "UPDATE booking.supplier_contracts SET status = 'published', published_by = $2, published_at = now()"
            " WHERE id = $1",
            contract,
            CHECKER,
        )
    return {"supplier": sid, "service": service}


async def _departure(c: asyncpg.Connection) -> str:
    plan = await c.fetchval(
        "INSERT INTO booking.trip_plans (source_system, source_id, sellable) VALUES ('cis', $1, true) RETURNING id",
        f"it-1074-{uuid.uuid4().hex[:8]}",
    )
    return await c.fetchval(
        "INSERT INTO booking.departures (trip_plan_id, start_date, capacity) VALUES ($1, $2, 20) RETURNING id::text",
        plan,
        START,
    )


async def _sold_case(c: asyncpg.Connection, departure: str, *, seats: int, total: str, agency: str) -> dict:
    tid = uuid.uuid4()
    pid = await c.fetchval(
        "INSERT INTO booking.b2b_partners (tenant_id, agency_name, status, billing_currency, m2m_client_id)"
        " VALUES ($1, $2, 'active', 'USD', 'it1074client') RETURNING id::text",
        tid,
        agency,
    )
    case = await c.fetchrow(
        "INSERT INTO booking.trip_cases (channel, party_tenant_id, status, start_date, end_date, pax, currency,"
        " source_system) VALUES ('b2b', $1, 'confirmed', $2, $3, $4, 'USD', 'crm') RETURNING id::text AS id, code",
        tid,
        START,
        START + timedelta(days=3),
        seats,
    )
    quote = await c.fetchval(
        "INSERT INTO booking.b2b_quotes (partner_id, m2m_client_id, proposal_ref, proposal_version, currency,"
        " fx_rate_per_usd, fx_buffer_pct, fx_as_of, fx_source, pricing_rule_valid_from, margin_applied, card_fee_pct,"
        " rounding, priced_on, total_b2b_price, price_hash, valid_until)"
        " VALUES ($1::uuid, 'it1074client', $2, 1, 'USD', 1, 0, current_date, 'test', current_date, 0.15, 0,"
        " 'line_ceil_0.01', current_date, $3::numeric, $4, now() + interval '2 days') RETURNING id",
        pid,
        f"PR-{case['code']}",
        total,
        "d" * 64,
    )
    hold = await c.fetchval(
        "INSERT INTO booking.b2b_holds (partner_id, m2m_client_id, quote_id, proposal_ref, proposal_version,"
        " idempotency_key, status, expires_at, trip_case_id, confirmed_at)"
        " VALUES ($1::uuid, 'it1074client', $2, $3, 1, $4, 'confirmed', now() + interval '1 day', $5::uuid, now())"
        " RETURNING id::text",
        pid,
        quote,
        f"PR-{case['code']}",
        f"hold-{uuid.uuid4().hex[:12]}",
        case["id"],
    )
    await c.execute(
        "INSERT INTO booking.b2b_hold_lines (hold_id, line_no, line_ref, departure_id, seats, held_as, status)"
        " VALUES ($1::uuid, 1, 'L1', $2::uuid, $3, 'available', 'confirmed')",
        hold,
        departure,
        seats,
    )
    return {"id": case["id"], "code": case["code"], "partner": pid}


async def _pending(c: asyncpg.Connection, case_id: str, sup: str, *, net: str, currency: str, day: date) -> str:
    return await c.fetchval(
        "INSERT INTO booking.supplier_bookings (trip_case_id, supplier_id, item_description, service_date, pax,"
        " net_amount, net_currency, created_by) VALUES ($1::uuid, $2::uuid, 'Riverside lodge, two nights', $3, 2,"
        " $4::numeric, $5, $6) RETURNING id::text",
        case_id,
        sup,
        day,
        net,
        currency,
        MAKER,
    )


def _settings() -> Settings:
    return Settings(
        database_url="", environment="dev", ses_from_address="ops@example.test", staff_notify_email="staff@example.test"
    )


async def test_fx_snapshots_and_case_costs(conn: asyncpg.Connection) -> None:
    await _fx(conn, "THB", "35", date.today() - timedelta(days=5))
    sup = await _supplier(conn, "Lotus Lodge")
    dep = await _departure(conn)
    case = await _sold_case(conn, dep, seats=2, total="900.00", agency="Bluewater Journeys")
    booking = await _pending(conn, case["id"], sup["supplier"], net="7000.00", currency="THB", day=START)
    await supplier_bookings.request(conn, OPS, booking)
    out = await supplier_bookings.confirm(conn, OPS, booking, confirmation_ref="LL-778")
    assert Decimal(str(out["fx_rate_per_usd"])) == Decimal("35") and out["fx_as_of"] is not None

    await _fx(conn, "THB", "36", date.today())  # the baht moved before the bill came
    bill, _ = await supplier_bills.create(
        conn,
        OPS,
        {
            "supplier_id": sup["supplier"],
            "trip_case": case["code"],
            "bill_number": "LL-2026-11",
            "currency": "THB",
            "amount": Decimal("7000.00"),
            "issue_date": date.today(),
            "due_date": date.today() + timedelta(days=10),
            "lines": [{"supplier_booking_id": booking, "amount": Decimal("7000.00")}],
        },
        f"key-{uuid.uuid4().hex}",
    )
    approved = await supplier_bills.approve(conn, APPROVER, bill["id"])
    assert approved["fx_rate_per_usd"] == "36.00000000"

    costs = await supplier_bills.case_costs(conn, OPS, case["code"])
    (line,) = costs["lines"]
    assert line["usd"] == {"planned": "200.00", "billed": "194.44", "variance": "-5.56", "fx_difference": "-5.56",
                           "cost": "194.44", "fx_missing": False}  # fmt: skip
    assert costs["usd"]["cost"] == "194.44" and costs["shared_costs"] == []


async def test_departure_cost_by_sold_seats_order_and_freeze(conn: asyncpg.Connection) -> None:
    sup = await _supplier(conn, "Kestrel Coaches", bands=((1, 9, "300.00"), (10, 29, "480.00")))
    dep = await _departure(conn)
    first = await _sold_case(conn, dep, seats=6, total="1200.00", agency="Fernhill Holidays")
    cost = await departure_costs.create(conn, OPS, dep, {"service_id": sup["service"], "quantity": 2})
    assert (cost["pricing"], cost["basis"], cost["priced_for_pax"]) == ("rate", "group", 6)
    assert cost["net_amount"] == "600.00"  # 300 x 2 coaches for the 1-9 band

    await _sold_case(conn, dep, seats=4, total="800.00", agency="Mapleway Tours")  # 10 sold: the next band
    result = await departure_costs.run(OneConnPool(conn), dep)  # type: ignore[arg-type]
    assert result["repriced"] == 1
    view = await departure_costs.departure_costs(conn, OPS, dep)
    (shared,) = view["shared_costs"]
    assert shared["net_amount"] == "960.00" and shared["priced_for_pax"] == 10
    by_case = {c["code"]: c for c in view["cases"]}
    assert by_case[first["code"]]["seats"] == 6 and by_case[first["code"]]["shared_cost_usd"] == "576.00"
    assert view["departure"]["sold_seats"] == 10 and view["totals_usd"]["shared_cost"] == "960.00"

    ordered = await departure_costs.order(conn, OPS, shared["id"])
    sb = ordered["supplier_booking"]
    assert sb["status"] == "pending" and ordered["actions"]["order"] is False
    await supplier_bookings.request(conn, OPS, sb["id"])  # asked the coach company: the price is fixed
    frozen = (await departure_costs.departure_costs(conn, OPS, dep))["shared_costs"][0]
    assert frozen["frozen_at"] is not None and frozen["actions"]["edit"] is False
    with pytest.raises(Conflict) as exc:
        await departure_costs.update(conn, OPS, shared["id"], {"quantity": 3})
    assert exc.value.code == "departure_cost_frozen"
    with pytest.raises(Conflict) as exc:
        await departure_costs.remove(conn, OPS, shared["id"], "not needed")
    assert exc.value.code == "departure_cost_ordered"


async def test_manual_cost_reprices_its_pending_booking(conn: asyncpg.Connection) -> None:
    sup = await _supplier(conn, "Heron Guides")
    dep = await _departure(conn)
    await _sold_case(conn, dep, seats=3, total="600.00", agency="Saltmarsh Travel")
    cost = await departure_costs.create(
        conn,
        OPS,
        dep,
        {
            "supplier_id": sup["supplier"],
            "basis": "person",
            "unit_amount": Decimal("12.50"),
            "currency": "USD",
            "item_description": "Temple entrance for the group",
        },  # fmt: skip
    )
    assert cost["pricing"] == "manual" and cost["net_amount"] == "37.50"  # 12.50 x 3 seats
    ordered = await departure_costs.order(conn, OPS, cost["id"])
    edited = await departure_costs.update(conn, OPS, cost["id"], {"unit_amount": Decimal("14.00")})
    assert edited["net_amount"] == "42.00" and edited["changed"] == ["unit_amount", "net_amount"]
    booking = await supplier_bookings.get_booking(conn, OPS, ordered["supplier_booking"]["id"])
    assert booking["net_amount"] == Decimal("42.00") and booking["trip_case"] is None
    assert booking["departure"]["id"] == dep
    removed = await departure_costs.remove(conn, OPS, cost["id"], "Entrance included by the guide")
    assert removed["status"] == "cancelled"
    booking = await supplier_bookings.get_booking(conn, OPS, ordered["supplier_booking"]["id"])
    assert booking["status"] == "cancelled"


async def test_purchase_order_create_send_and_answers(conn: asyncpg.Connection) -> None:
    sup = await _supplier(conn, "Orchid Stays")
    dep = await _departure(conn)
    case = await _sold_case(conn, dep, seats=2, total="700.00", agency="Quietcove Agency")
    first = await _pending(conn, case["id"], sup["supplier"], net="120.00", currency="USD", day=START)
    second = await _pending(conn, case["id"], sup["supplier"], net="80.00", currency="USD", day=START)
    other_month = await _pending(conn, case["id"], sup["supplier"], net="50.00", currency="USD",
                                 day=START + timedelta(days=40))  # fmt: skip
    created = await supplier_orders.create_orders(conn, OPS, trip_cases=[case["code"].lower()])
    assert created["lines"] == 3 and len(created["orders"]) == len({START.month, (START + timedelta(days=40)).month})
    again = await supplier_orders.create_orders(conn, OPS, trip_cases=[case["code"]])
    assert again["lines"] == 0  # every pending booking is on an order already
    order = next(o for o in created["orders"] if o["service_month"] == START.replace(day=1))
    detail = await supplier_orders.detail(conn, OPS, order["code"])
    assert {x["id"] for x in detail["lines"]} >= {first, second} and detail["actions"]["send"] is True
    assert other_month not in {x["id"] for x in detail["lines"]} or START.month == (START + timedelta(days=40)).month

    html = await supplier_orders.document(conn, OPS, order["code"])
    assert case["code"] in html and "Quietcove" not in html and "Draft, not sent yet." in html

    sending = await supplier_orders.send(conn, OPS, order["code"], note="Late check-in for the group.")
    assert sending["status"] == "sending" and sending["total_net"] in ("200.00", "250.00")
    job = await conn.fetchval(
        "SELECT payload ->> 'order_id' FROM booking.job WHERE kind = 'bk_supplier_order_send' ORDER BY created_at"
        " DESC LIMIT 1"
    )
    assert job == order["id"]

    sent_mail: list[mailer.Message] = []

    async def fake_send(message: mailer.Message) -> str:
        sent_mail.append(message)
        return "ses-msg-1"

    result = await supplier_orders.run_send(OneConnPool(conn), _settings(), fake_send, order["id"])  # type: ignore[arg-type]
    assert result["outcome"] == "sent" and result["redirected"] is True
    (message,) = sent_mail
    assert message.to == "staff@example.test" and "reservations@example.test" in message.text
    assert "Quietcove" not in message.html and order["code"] in message.subject
    done = await supplier_orders.detail(conn, OPS, order["code"])
    assert done["status"] == "sent" and done["state"] == "sent"
    assert {x["status"] for x in done["lines"]} == {"requested"}
    await supplier_bookings.confirm(conn, OPS, first, confirmation_ref="OS-1")
    assert (await supplier_orders.detail(conn, OPS, order["code"]))["state"] == "partly_confirmed"


async def test_send_refused_by_mail_goes_back_to_draft(conn: asyncpg.Connection) -> None:
    sup = await _supplier(conn, "Bamboo Ferry")
    dep = await _departure(conn)
    case = await _sold_case(conn, dep, seats=1, total="300.00", agency="Pinegrove Trips")
    await _pending(conn, case["id"], sup["supplier"], net="40.00", currency="USD", day=START)
    (order,) = (await supplier_orders.create_orders(conn, OPS, trip_cases=[case["id"]]))["orders"]
    await supplier_orders.send(conn, OPS, order["id"])

    async def refused(message: mailer.Message) -> str:
        raise mailer.MailError("MessageRejected", "address blocked", permanent=True)

    out = await supplier_orders.run_send(OneConnPool(conn), _settings(), refused, order["id"])  # type: ignore[arg-type]
    assert out["outcome"] == "not_sent"
    back = await supplier_orders.detail(conn, OPS, order["id"])
    assert back["status"] == "draft" and back["send_error"].startswith("e-mail refused")
    cancelled = await supplier_orders.cancel(conn, OPS, order["id"], "Supplier changed")
    assert cancelled["status"] == "cancelled" and cancelled["lines"] == []


async def test_profit_reads(conn: asyncpg.Connection) -> None:
    dep = await _departure(conn)
    case = await _sold_case(conn, dep, seats=2, total="1000.00", agency="Copperleaf Voyages")
    page = await profit.cases(conn, start=START, end=START)
    row = next(r for r in page["items"] if r["code"] == case["code"])
    # No quote line and no booking yet: nothing is costed, the whole revenue shows as profit until bookings exist.
    assert row["agency_name"] == "Copperleaf Voyages" and row["cost_basis"] == "bookings"
    assert row["revenue_usd"] == "1000.00" and row["profit_usd"] == "1000.00"
    deps = await profit.departures(conn, start=START, end=START)
    assert any(d["departure_id"] == dep for d in deps["items"])
    months = await profit.agencies(conn, start=START, end=START)
    assert any(m["agency_name"] == "Copperleaf Voyages" for m in months["items"])
