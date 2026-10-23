"""AA-974 commercial rules with a scripted connection: VND quotes (rounding, hash, stored rule), the fee schedule
(validation, tiers, fee arithmetic), cancel with fee + dry run, extend once, the re-hold limit, the staff policy
routes and the new B2B routes' input checks. Real SQL (locks, constraints, the trip case transition, policy in force):
tests/integration/test_bk021_b2b.py."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D
from typing import Any

import asyncpg
import pytest

from app.b2b import cancellation, holds, quotes
from app.b2b.cancellation import Tier, TierError, compute, fee_pct_for, parse_tiers
from app.b2b.errors import B2BError
from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.pricing import repository
from app.pricing.engine import Channel, PricingRule
from tests.staff_tokens import LocalM2M
from tests.unit.fakes import ScriptedConn
from tests.unit.test_b2b_api import PARTNER_ID, _client, _conn, _headers, _problem, _quote_body
from tests.unit.test_b2b_hold_flow import (
    CALLER,
    CASE,
    DEP,
    HELD_LINES,
    HOLD,
    HOLD_BY_ID,
    HOLD_BY_VERSION,
    HOLD_FIND,
    NOW,
    PLAN,
    QUOTE,
    START,
    dep_row,
    hold_row,
    place,
    place_conn,
    quote_lines,
)
from tests.unit.test_pricing_policy import ADMIN, CONTRACTING, SALES
from tests.unit.test_pricing_policy import _client as staff_client

STANDARD = [
    {"min_days": 60, "fee_pct": 0},
    {"min_days": 30, "fee_pct": 25},
    {"min_days": 15, "fee_pct": 50},
    {"min_days": 7, "fee_pct": 75},
    {"min_days": 0, "fee_pct": 100},
]
POLICY_ID = "00000000-0000-4000-8000-0000000bc021"
PLAN_POLICY = "00000000-0000-4000-8000-0000000bc099"


# ── VND quotes ───────────────────────────────────────────────────────────────


def test_round_line_and_rule_per_currency() -> None:
    assert quotes.round_line(D("2439024.39"), "VND") == D("2440000.00")
    assert quotes.round_line(D("2440000"), "VND") == D("2440000.00")  # already a multiple: unchanged
    assert quotes.round_line(D("97.5609"), "USD") == D("97.57")
    assert quotes.rounding_rule("VND") == "line_ceil_1000"
    assert quotes.rounding_rule("EUR") == quotes.ROUNDING == "line_ceil_0.01"
    assert "VND" in quotes.QUOTE_CURRENCIES and "JPY" not in quotes.QUOTE_CURRENCIES
    assert quotes.money(D("3050000.00"), "VND") == {"amount": "3050000", "currency": "VND"}


def test_vnd_quote_rounds_lines_to_1000_and_stores_the_rule() -> None:
    m2m, conn = LocalM2M(), _conn()
    resp = _client(conn, m2m).post(
        "/booking/b2b/v1/quotes", json=_quote_body(currency="VND"), headers=_headers(m2m.token())
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # Room: 2 nights x 1,000,000 VND = 80 USD net → / 0.82 = 97.56 USD → x 25,000 = 2,439,024.39 → 2,440,000.
    # Transfer: 2 x 10 USD = 20 USD → 24.39 USD → 609,756.10 → 610,000. Total = the exact sum.
    assert [line["b2b_price"] for line in body["lines"]] == [
        {"amount": "2440000", "currency": "VND"},
        {"amount": "610000", "currency": "VND"},
    ]
    assert body["total_b2b_price"] == {"amount": "3050000", "currency": "VND"}
    assert body["fx"]["quote_currency"] == "VND" and body["fx"]["rate_per_usd"] == "25000.00000000"
    [insert] = conn.sql("INSERT INTO booking.b2b_quotes")
    assert insert[4] == "VND" and insert[12] == "line_ceil_1000" and insert[14] == D("3050000.00")
    # The VND rate in force on the pricing day (UTC today) is the one asked for.
    [fx_call] = [args for args in conn.sql("FROM booking.fx_rates") if "VND" in args[0]]
    assert fx_call[1] == datetime.now(UTC).date()


def test_rounding_rule_is_part_of_the_hash() -> None:
    rule = repository.DatedRule(PricingRule(Channel.B2B, D("0.15"), D("0.10"), D("0"), D("0.03")), date(2026, 10, 14))
    fx = quotes.FxLock("VND", D("25000"), D("0"), date(2026, 10, 23), "synthetic")
    args = {"currency": "VND", "fx": fx, "rule": rule, "margin": D("0.15"), "lines": [], "total": D("0")}
    assert quotes.price_hash(**args) != quotes.price_hash(**args, rounding="line_ceil_1000")
    assert quotes.price_hash(**args) == quotes.price_hash(**args, rounding=quotes.ROUNDING)


# ── Fee schedule ─────────────────────────────────────────────────────────────


def test_parse_tiers_sorts_and_normalises() -> None:
    tiers = parse_tiers('[{"min_days": 0, "fee_pct": 100}, {"min_days": 30, "fee_pct": 25.5}]')
    assert tiers == (Tier(30, D("25.50")), Tier(0, D("100.00")))
    assert cancellation.tiers_out(tiers) == [{"min_days": 30, "fee_pct": "25.50"}, {"min_days": 0, "fee_pct": "100.00"}]
    assert cancellation.tiers_json(tiers) == '[{"min_days": 30, "fee_pct": 25.5}, {"min_days": 0, "fee_pct": 100.0}]'


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ([], "1 to 20 tiers"),
        ({"min_days": 0, "fee_pct": 1}, "1 to 20 tiers"),
        ([{"min_days": n, "fee_pct": 100} for n in range(21)], "1 to 20 tiers"),
        (["x"], "{min_days, fee_pct}"),
        ([{"min_days": 0, "fee_pct": 1, "extra": 1}], "{min_days, fee_pct}"),
        ([{"min_days": -1, "fee_pct": 1}], "whole number"),
        ([{"min_days": 1.5, "fee_pct": 1}], "whole number"),
        ([{"min_days": True, "fee_pct": 1}], "whole number"),
        ([{"min_days": 731, "fee_pct": 1}], "whole number"),
        ([{"min_days": 0, "fee_pct": 101}], "fee_pct"),
        ([{"min_days": 0, "fee_pct": "abc"}], "fee_pct"),
        ([{"min_days": 0, "fee_pct": True}], "fee_pct"),
        ([{"min_days": 0, "fee_pct": "12.345"}], "fee_pct"),
        ([{"min_days": 0, "fee_pct": "NaN"}], "fee_pct"),
        ([{"min_days": 0, "fee_pct": 10}, {"min_days": 0, "fee_pct": 20}], "unique"),
        ([{"min_days": 10, "fee_pct": 10}], "min_days 0"),
        ([{"min_days": 30, "fee_pct": 50}, {"min_days": 0, "fee_pct": 20}], "must not fall"),
    ],
)
def test_parse_tiers_refusals(raw: Any, message: str) -> None:
    with pytest.raises(TierError, match=message.replace("{", r"\{").replace("}", r"\}")):
        parse_tiers(raw)


@pytest.mark.parametrize(
    ("days", "pct"),
    [(400, "0"), (60, "0"), (59, "25"), (30, "25"), (29, "50"), (15, "50"), (14, "75"), (7, "75"), (6, "100")]
    + [(0, "100"), (-3, "100")],
)
def test_fee_pct_by_days_before_departure(days: int, pct: str) -> None:
    assert fee_pct_for(parse_tiers(STANDARD), days) == D(pct)


def test_compute_fee_in_the_quote_currency() -> None:
    tiers = parse_tiers(STANDARD)
    fee = compute(tiers, total=D("3050000"), currency="VND", departure=date(2027, 3, 21), today=date(2027, 3, 1))
    assert (fee.days_before, fee.fee_pct, fee.fee, fee.refundable) == (20, D("50.00"), D("1525000"), D("1525000"))
    usd = compute(
        parse_tiers([{"min_days": 0, "fee_pct": "33.33"}]),
        total=D("100.01"),
        currency="USD",
        departure=date(2027, 3, 1),
        today=date(2027, 3, 1),
    )
    assert (usd.fee, usd.refundable) == (D("33.33"), D("66.68"))  # half-up to the cent, fee + refund = total
    vnd = compute(
        parse_tiers([{"min_days": 0, "fee_pct": "33.33"}]),
        total=D("1000"),
        currency="VND",
        departure=date(2027, 3, 1),
        today=date(2027, 3, 1),
    )
    assert (vnd.fee, vnd.refundable) == (D("333"), D("667"))  # whole VND
    assert cancellation.money(D("1525000.00"), "VND") == {"amount": "1525000", "currency": "VND"}
    assert cancellation.money(D("12.5"), "EUR") == {"amount": "12.50", "currency": "EUR"}


def test_first_plan_is_the_first_trip_plan_line() -> None:
    lines = quote_lines()
    assert cancellation.first_plan(list(reversed(lines))) == PLAN
    no_plan = [{"line_no": 1, "component_id": "rt:x"}, {"line_no": 2, "component_id": "zz:bad"}]
    assert cancellation.first_plan(no_plan) is None


@pytest.mark.asyncio
async def test_policy_in_force_prefers_the_plan_then_the_default() -> None:
    conn = ScriptedConn().on("FROM booking.cancellation_policies p JOIN booking.policy_approvals", None)
    assert await cancellation.policy_for_lines(conn, quote_lines(), date(2027, 2, 1)) is None
    [args] = conn.sql("FROM booking.cancellation_policies p JOIN booking.policy_approvals")
    assert args == ("b2b", PLAN, date(2027, 2, 1))
    conn.on("FROM booking.cancellation_policies p JOIN", {"id": POLICY_ID, "tiers": str(STANDARD).replace("'", '"')})
    found = await cancellation.policy_in_force(conn, None, date(2027, 2, 1))
    assert found is not None and found.id == POLICY_ID and found.tiers[0] == Tier(60, D("0.00"))


# ── Cancel ───────────────────────────────────────────────────────────────────

BOOKING = {
    "hold_id": HOLD,
    "quote_id": QUOTE,
    "hold_status": "confirmed",
    "confirmed_at": NOW,
    "cancellation_policy_id": POLICY_ID,
    "trip_case_id": CASE,
    "code": "TC-000042",
    "status": "confirmed",
    "start_date": NOW.date() + timedelta(days=20),
}
STANDARD_JSON = str(STANDARD).replace("'", '"')


def cancel_conn(booking: dict[str, Any] | None = None, **over: Any) -> ScriptedConn:
    conn = ScriptedConn()
    conn.on("FROM booking.b2b_holds h JOIN booking.trip_cases c", booking if booking is not None else BOOKING)
    conn.on("FROM booking.b2b_cancellations WHERE hold_id", over.get("stored"))
    conn.on("FROM booking.b2b_quotes WHERE id", {"total_b2b_price": D("1150.00"), "currency": "EUR"})
    conn.on("FROM booking.cancellation_policies p WHERE p.id", {"id": POLICY_ID, "tiers": STANDARD_JSON})
    conn.on("FROM booking.b2b_quote_lines WHERE quote_id", quote_lines())
    conn.on("FROM booking.cancellation_policies p JOIN", over.get("in_force"))
    return conn


async def cancel(conn: ScriptedConn, **kw: Any) -> dict[str, Any]:
    args = {
        "partner_id": PARTNER_ID,
        "client_id": "crm-client",
        "booking_id": CASE,
        "reason_code": "client_cancelled",
        "dry_run": False,
        "idempotency_key": "cancel-0001",
        "now": NOW,
    }
    return await cancellation.cancel_booking(conn, **(args | kw))


EXPECTED_FEE = {
    "fee": {"amount": "575.00", "currency": "EUR"},
    "refundable_b2b_amount": {"amount": "575.00", "currency": "EUR"},
    "fee_basis": {"policy_id": POLICY_ID, "days_before_departure": 20, "fee_pct": "50.00"},
}


@pytest.mark.asyncio
async def test_dry_run_returns_the_scheduled_fee_and_writes_nothing() -> None:
    conn = cancel_conn()
    out = await cancel(conn, dry_run=True)
    assert out == {"booking_id": CASE, "dry_run": True, **EXPECTED_FEE, "cancelled_at": None}
    assert not conn.sql("INSERT INTO") and not conn.sql("UPDATE booking")
    assert "FOR UPDATE" not in next(flat for flat, _ in conn.calls if "JOIN booking.trip_cases c" in flat)


@pytest.mark.asyncio
async def test_cancel_inside_the_fee_window_charges_the_scheduled_fee() -> None:
    conn = cancel_conn()
    out = await cancel(conn)
    assert out == {"booking_id": CASE, "dry_run": False, **EXPECTED_FEE, "cancelled_at": NOW.isoformat()}
    assert "FOR UPDATE OF h" in next(flat for flat, _ in conn.calls if "JOIN booking.trip_cases c" in flat)
    [row] = conn.sql("INSERT INTO booking.b2b_cancellations")
    assert row[:7] == (HOLD, CASE, PARTNER_ID, "crm-client", POLICY_ID, "client_cancelled", "cancel-0001")
    assert row[7:] == (BOOKING["start_date"], 20, D("50.00"), "EUR", D("1150.00"), D("575.00"), D("575.00"), NOW)
    assert conn.sql("SET status = 'cancelled' WHERE hold_id") == [(HOLD,)]
    assert conn.sql("UPDATE booking.trip_cases SET status = 'cancelled'") == [(CASE,)]
    [note] = conn.sql("set_config('booking.status_note'")
    assert note[0] == "B2B cancel by m2m:crm-client (client_cancelled); fee 50.00% = 575.00 EUR"
    [audit] = conn.sql("INSERT INTO booking.audit_logs")
    assert audit[2] == "m2m:crm-client" and audit[3] == "b2b_booking.cancelled" and audit[5] == CASE


@pytest.mark.asyncio
async def test_cancel_of_a_departure_aa_cancelled_is_free() -> None:
    conn = cancel_conn().on("d.status = 'cancelled'", True)
    out = await cancel(conn, reason_code="client_cancelled")
    assert out["fee"] == {"amount": "0.00", "currency": "EUR"} and out["fee_basis"]["fee_pct"] == "0.00"
    assert out["refundable_b2b_amount"] == {"amount": "1150.00", "currency": "EUR"}
    assert out["fee_basis"]["days_before_departure"] == 20 and out["fee_basis"]["policy_id"] == POLICY_ID
    [row] = conn.sql("INSERT INTO booking.b2b_cancellations")
    assert row[9:14] == (D("0"), "EUR", D("1150.00"), D("0"), D("1150.00"))


@pytest.mark.asyncio
async def test_cancel_outside_the_window_is_free() -> None:
    far = {**BOOKING, "start_date": NOW.date() + timedelta(days=90)}
    out = await cancel(cancel_conn(far), dry_run=True)
    assert out["fee"] == {"amount": "0.00", "currency": "EUR"} and out["fee_basis"]["fee_pct"] == "0.00"
    assert out["refundable_b2b_amount"] == {"amount": "1150.00", "currency": "EUR"}


@pytest.mark.asyncio
async def test_cancel_replay_returns_the_stored_cancellation() -> None:
    stored = {
        "policy_id": POLICY_ID,
        "days_before_departure": 20,
        "fee_pct": D("50.00"),
        "currency": "EUR",
        "fee_amount": D("575.00"),
        "refundable_amount": D("575.00"),
        "cancelled_at": NOW,
    }
    for dry_run in (False, True):
        conn = cancel_conn(stored=stored)
        out = await cancel(conn, dry_run=dry_run, idempotency_key="another-key")
        assert out == {"booking_id": CASE, "dry_run": False, **EXPECTED_FEE, "cancelled_at": NOW.isoformat()}
        assert not conn.sql("INSERT INTO") and not conn.sql("UPDATE booking")


@pytest.mark.asyncio
async def test_cancel_without_a_locked_policy_uses_the_one_in_force_on_the_confirm_day() -> None:
    booking = {**BOOKING, "cancellation_policy_id": None}
    conn = cancel_conn(booking, in_force={"id": PLAN_POLICY, "tiers": '[{"min_days": 0, "fee_pct": 10}]'})
    out = await cancel(conn, dry_run=True)
    assert out["fee_basis"] == {"policy_id": PLAN_POLICY, "days_before_departure": 20, "fee_pct": "10.00"}
    [args] = conn.sql("FROM booking.cancellation_policies p JOIN")
    assert args == ("b2b", PLAN, NOW.date())
    with pytest.raises(B2BError) as exc:  # none in force at all
        await cancel(cancel_conn(booking), dry_run=True)
    assert exc.value.error == "unavailable"


@pytest.mark.asyncio
async def test_cancel_locked_policy_row_missing_falls_back() -> None:
    conn = cancel_conn(in_force={"id": PLAN_POLICY, "tiers": '[{"min_days": 0, "fee_pct": 10}]'})
    conn.on("FROM booking.cancellation_policies p WHERE p.id", None)
    assert (await cancel(conn, dry_run=True))["fee_basis"]["policy_id"] == PLAN_POLICY


@pytest.mark.parametrize(
    ("booking_id", "booking", "kw", "error"),
    [
        ("not-a-uuid", BOOKING, {}, "not_found"),
        (CASE, None, {}, "not_found"),
        (CASE, BOOKING, {"reason_code": "whim"}, "invalid"),
        (CASE, {**BOOKING, "status": "cancelled"}, {}, "invalid"),
        (CASE, {**BOOKING, "hold_status": "active"}, {}, "invalid"),
        (CASE, {**BOOKING, "start_date": None}, {}, "unavailable"),
    ],
)
@pytest.mark.asyncio
async def test_cancel_refusals(booking_id: str, booking: Any, kw: dict[str, Any], error: str) -> None:
    conn = cancel_conn(booking if booking is not None else {})
    if booking is None:
        conn.on("FROM booking.b2b_holds h JOIN booking.trip_cases c", None)
    with pytest.raises(B2BError) as exc:
        await cancel(conn, booking_id=booking_id, **kw)
    assert exc.value.error == error
    assert not conn.sql("INSERT INTO booking.b2b_cancellations")


# ── Extend once ──────────────────────────────────────────────────────────────


def extend_conn(hold: dict[str, Any] | None = None, **over: Any) -> ScriptedConn:
    conn = ScriptedConn()
    conn.on("FROM booking.b2b_hold_lines WHERE hold_id", HELD_LINES)
    conn.on("FROM booking.departures d WHERE d.id = ANY", [over.get("dep", dep_row())])
    conn.on("SELECT clock_timestamp()", over.get("clock", NOW))
    conn.on("FROM booking.b2b_hold_lines l", [{"departure_id": DEP, "held": 0, "sold": 0}])
    conn.on("FROM booking.settings", over.get("hours", "72"))
    conn.on(HOLD_BY_ID, hold_row(extended_count=1, expires_at=NOW + timedelta(hours=120)))
    conn.on(HOLD_FIND, hold or hold_row())  # after HOLD_BY_ID, whose fragment the find query also contains
    return conn


async def extend(conn: ScriptedConn, **kw: Any) -> dict[str, Any]:
    args = {"reason": "payment_claimed", "idempotency_key": "extend-0001"}
    return await holds.extend_hold(conn, CALLER, HOLD, **(args | kw))


@pytest.mark.asyncio
async def test_extend_adds_the_setting_hours_once_and_audits() -> None:
    conn = extend_conn()
    out = await extend(conn)
    assert out["extended_count"] == 1 and out["expires_at"] == (NOW + timedelta(hours=120)).isoformat()
    [update] = conn.sql("SET expires_at = $2, extended_count = 1")
    assert update == (HOLD, NOW + timedelta(hours=48 + 72), "payment_claimed", "extend-0001")
    [audit] = conn.sql("INSERT INTO booking.audit_logs")
    assert audit[3] == "b2b_hold.extended"
    # Hold first, then its departures (same lock order as confirm), expiry judged on the clock after the locks.
    flats = [flat for flat, _ in conn.calls]
    hold_at = next(i for i, f in enumerate(flats) if HOLD_FIND in f and "FOR UPDATE" in f)
    dep_at = next(i for i, f in enumerate(flats) if "FROM booking.departures d WHERE d.id = ANY" in f)
    clock_at = flats.index("SELECT clock_timestamp()")
    assert hold_at < dep_at < clock_at


@pytest.mark.asyncio
async def test_extend_is_capped_by_the_departure_start() -> None:
    soon = (NOW + timedelta(days=3)).date()
    conn = extend_conn(dep=dep_row(start_date=soon))
    await extend(conn)
    [update] = conn.sql("SET expires_at = $2, extended_count = 1")
    assert update[1] == datetime(soon.year, soon.month, soon.day, tzinfo=UTC)


@pytest.mark.asyncio
async def test_extend_that_cannot_move_the_expiry_is_extend_limit() -> None:
    start = (NOW + timedelta(days=2)).date()
    capped = hold_row(expires_at=datetime(start.year, start.month, start.day, tzinfo=UTC))
    conn = extend_conn(capped, dep=dep_row(start_date=start))
    with pytest.raises(B2BError) as exc:
        await extend(conn)
    assert exc.value.error == "extend_limit" and "departure starts" in exc.value.message
    assert not conn.sql("SET expires_at")


@pytest.mark.asyncio
async def test_second_extend_is_409_and_a_retry_returns_the_hold() -> None:
    extended = hold_row(extended_count=1, extend_idempotency_key="extend-0001")
    with pytest.raises(B2BError) as exc:
        await extend(extend_conn(extended), idempotency_key="extend-0002")
    assert exc.value.error == "extend_limit" and exc.value.status_code == 409
    conn = extend_conn(extended)
    out = await extend(conn)
    assert out["hold_id"] == HOLD and out["extended_count"] == 1
    assert not conn.sql("SET expires_at")


@pytest.mark.parametrize(
    ("hold", "kw", "clock", "error"),
    [
        (hold_row(status="confirmed"), {}, NOW, "invalid"),
        (hold_row(status="expired", stored="active"), {}, NOW, "hold_expired"),
        (hold_row(status="released"), {}, NOW, "hold_expired"),
        (hold_row(), {"reason": "because"}, NOW, "invalid"),
        (hold_row(), {}, NOW + timedelta(hours=49), "hold_expired"),  # expired while waiting on the locks
    ],
)
@pytest.mark.asyncio
async def test_extend_refusals(hold: dict[str, Any], kw: dict[str, Any], clock: datetime, error: str) -> None:
    conn = extend_conn(hold, clock=clock)
    with pytest.raises(B2BError) as exc:
        await extend(conn, **kw)
    assert exc.value.error == error
    assert not conn.sql("SET expires_at")


@pytest.mark.parametrize(
    ("stored", "hours"), [("96", 96), (None, 72), ("abc", 72), ("0", 72), ("721", 72), ('"24"', 24)]
)
@pytest.mark.asyncio
async def test_extend_hours_setting(stored: Any, hours: int) -> None:
    assert await holds._extend_hours(ScriptedConn().on("FROM booking.settings", stored)) == hours


@pytest.mark.parametrize(("stored", "limit"), [("5", 5), (None, 3), ("abc", 3), ("0", 0), ("101", 3), ("-1", 3)])
@pytest.mark.asyncio
async def test_rehold_limit_setting(stored: Any, limit: int) -> None:
    assert await holds._rehold_limit(ScriptedConn().on("FROM booking.settings", stored)) == limit


# ── Re-hold limit ────────────────────────────────────────────────────────────


REHOLD = "count(*) FILTER (WHERE created_at >= $3)"


def _counts(today: int, first: datetime | None) -> dict[str, Any]:
    return {"today": today, "first": first}


@pytest.mark.parametrize(
    ("counts", "allowed"),
    [
        (_counts(0, None), True),  # the proposal's first hold
        (_counts(3, NOW - timedelta(hours=1)), True),  # first hold today + 2 re-holds → a 3rd re-hold is allowed
        (_counts(4, NOW - timedelta(hours=1)), False),  # first today + 3 re-holds → the 4th re-hold is refused
        (_counts(2, NOW - timedelta(days=2)), True),  # first hold earlier: 2 re-holds today
        (_counts(3, NOW - timedelta(days=2)), False),  # 3 re-holds today already
    ],
)
@pytest.mark.asyncio
async def test_rehold_limit(counts: dict[str, Any], allowed: bool) -> None:
    conn = place_conn()
    conn.on(REHOLD, counts)
    conn.on("FROM booking.settings WHERE key", lambda name: "3" if name == holds.REHOLD_LIMIT_SETTING else "48")
    if allowed:
        assert (await place(conn))["hold_id"] == HOLD
        assert conn.sql("INSERT INTO booking.b2b_holds")
    else:
        with pytest.raises(B2BError) as exc:
            await place(conn)
        assert exc.value.error == "rehold_limit" and exc.value.status_code == 429
        midnight = datetime(NOW.year, NOW.month, NOW.day, tzinfo=UTC) + timedelta(days=1)
        assert exc.value.extra == {"retry_after_s": int((midnight - NOW).total_seconds())}
        assert not conn.sql("INSERT INTO booking.b2b_holds")
    [count_args] = conn.sql(REHOLD)
    assert count_args == (PARTNER_ID, "P-1", datetime(NOW.year, NOW.month, NOW.day, tzinfo=UTC))
    # The count runs under a per-proposal advisory lock taken before the version check.
    flats = [flat for flat, _ in conn.calls]
    lock_at = next(i for i, f in enumerate(flats) if "pg_advisory_xact_lock" in f)
    assert lock_at < next(i for i, f in enumerate(flats) if HOLD_BY_VERSION in f)


@pytest.mark.asyncio
async def test_idempotent_answers_skip_the_rehold_limit() -> None:
    conn = place_conn(current=hold_row())
    conn.on(REHOLD, _counts(99, NOW))
    assert (await place(conn))["hold_id"] == HOLD
    conn = place_conn(replay=hold_row())
    assert (await place(conn))["hold_id"] == HOLD
    assert not conn.sql("pg_advisory_xact_lock")


def test_day_start_is_utc_midnight() -> None:
    from datetime import timezone

    at = datetime(2026, 10, 24, 2, 0, tzinfo=timezone(timedelta(hours=7)))  # 23 Oct 19:00 UTC
    assert holds.day_start(at) == datetime(2026, 10, 23, tzinfo=UTC)


# ── B2B routes ───────────────────────────────────────────────────────────────


def test_extend_and_cancel_routes_check_input_and_scope() -> None:
    m2m = LocalM2M()
    conn = _conn()
    client = _client(conn, m2m)
    key = {"Idempotency-Key": "k" * 8}
    full = {**_headers(m2m.token()), **key}
    _problem(client.post("/booking/b2b/v1/holds/h1/extend", json={"reason": "x"}, headers=full), 422, "invalid")
    _problem(
        client.post(
            "/booking/b2b/v1/holds/h1/extend", json={"reason": "payment_claimed"}, headers=_headers(m2m.token())
        ),
        422,
        "invalid",
    )  # no Idempotency-Key
    read_only = {**_headers(m2m.token("booking.b2b/read")), **key}
    _problem(
        client.post("/booking/b2b/v1/holds/h1/extend", json={"reason": "payment_claimed"}, headers=read_only),
        403,
        "forbidden",
    )
    cancel_url = f"/booking/b2b/v1/bookings/{CASE}/cancel"
    _problem(client.post(cancel_url, json={"reason_code": "whim"}, headers=full), 422, "invalid")
    hold_only = {**_headers(m2m.token("booking.b2b/hold")), **key}
    _problem(client.post(cancel_url, json={"reason_code": "other"}, headers=hold_only), 403, "forbidden")
    assert not conn.sql("UPDATE booking.b2b_holds") and not conn.sql("INSERT INTO booking.b2b_cancellations")


def test_extend_and_cancel_routes_answer_the_contract_shapes() -> None:
    m2m = LocalM2M()
    conn = _conn()
    for fragment, answer in reversed(extend_conn().rules):  # keep the scripted precedence
        conn.on(fragment, answer)
    conn.on("FROM booking.settings", "72")
    client = _client(conn, m2m)
    headers = {**_headers(m2m.token()), "Idempotency-Key": "extend-0001"}
    resp = client.post(f"/booking/b2b/v1/holds/{HOLD}/extend", json={"reason": "client_recheck"}, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["extended_count"] == 1
    for fragment, answer in reversed(cancel_conn().rules):
        conn.on(fragment, answer)
    resp = client.post(
        f"/booking/b2b/v1/bookings/{CASE}/cancel?dry_run=true",
        json={"reason_code": "client_cancelled"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dry_run"] is True and body["cancelled_at"] is None
    assert body["fee_basis"]["policy_id"] == POLICY_ID and body["fee"]["currency"] == "EUR"
    assert not conn.sql("INSERT INTO booking.b2b_cancellations")
    resp = client.post(f"/booking/b2b/v1/bookings/{CASE}/cancel", json={"reason_code": "other"}, headers=headers)
    assert resp.status_code == 200 and resp.json()["dry_run"] is False and resp.json()["cancelled_at"]


# ── Staff policy routes ──────────────────────────────────────────────────────

ROW = {
    "id": PLAN_POLICY,
    "channel": "b2b",
    "trip_plan_id": PLAN,
    "source_id": "BT-7D",
    "valid_from": date(2027, 1, 1),
    "title": "Bhutan 7D",
    "tiers": '[{"min_days": 30, "fee_pct": 0}, {"min_days": 0, "fee_pct": 100}]',
    "note": None,
    "created_at": NOW,
    "created_by": "u-5",
    "created_by_name": "Contracts",
    "approved_by": None,
    "approved_by_name": None,
    "approved_at": None,
    "status": "pending",
}
BODY = {
    "title": "Bhutan 7D",
    "source_id": "BT-7D",
    "valid_from": "2027-01-01",
    "tiers": [{"min_days": 30, "fee_pct": "0"}, {"min_days": 0, "fee_pct": "100"}],
}
URL = "/booking/pricing/cancellation-policies"


def policy_conn(row: dict[str, Any] | None = None) -> ScriptedConn:
    """LIST_SQL answers a list for the list call and the row itself for the by-id read (limit 1)."""
    found = row or ROW
    conn = ScriptedConn()
    conn.on(
        "FROM booking.cancellation_policies p LEFT JOIN booking.trip_plans", lambda *a: found if a[3] == 1 else [found]
    )
    conn.on("SELECT id::text FROM booking.trip_plans", PLAN)
    conn.on("INSERT INTO booking.cancellation_policies", PLAN_POLICY)
    return conn


def test_staff_list_and_create_policy() -> None:
    conn = policy_conn()
    listed = staff_client(SALES, conn).get(URL, params={"source_id": "BT-7D"})
    assert listed.status_code == 200, listed.text
    item = listed.json()["items"][0]
    assert item["tiers"] == [{"min_days": 30, "fee_pct": "0.00"}, {"min_days": 0, "fee_pct": "100.00"}]
    assert item["source_id"] == "BT-7D" and item["can_approve"] is False  # sales may read, not approve
    created = staff_client(CONTRACTING, conn).post(URL, json=BODY)
    assert created.status_code == 201, created.text
    [insert] = conn.sql("INSERT INTO booking.cancellation_policies")
    assert insert[:4] == ("b2b", PLAN, date(2027, 1, 1), "Bhutan 7D")
    assert insert[4] == '[{"min_days": 30, "fee_pct": 0.0}, {"min_days": 0, "fee_pct": 100.0}]' and insert[6] == "u-5"
    [audit] = conn.sql("INSERT INTO booking.audit_logs")
    assert audit[3] == "cancellation_policy.created" and "30+ d 0.00%" in audit[-1]


def test_staff_create_policy_refusals() -> None:
    assert staff_client(SALES, policy_conn()).post(URL, json=BODY).status_code == 403
    bad = {**BODY, "tiers": [{"min_days": 30, "fee_pct": "10"}]}
    resp = staff_client(ADMIN, policy_conn()).post(URL, json=bad)
    assert resp.status_code == 422 and resp.json()["detail"] == "invalid_tiers"
    assert "min_days 0" in resp.json()["message"]
    shape = {**BODY, "tiers": [{"min_days": 0, "fee_pct": "100.555"}]}
    assert staff_client(ADMIN, policy_conn()).post(URL, json=shape).status_code == 422


@pytest.mark.asyncio
async def test_create_policy_service_errors() -> None:
    data = {"title": "T", "source_id": "BT-7D", "valid_from": date(2027, 1, 1), "tiers": STANDARD, "note": " "}
    conn = policy_conn().on("SELECT id::text FROM booking.trip_plans", None)
    with pytest.raises(NotFound):
        await cancellation.create_policy(conn, CONTRACTING, data, today=NOW.date())
    with pytest.raises(Invalid):
        await cancellation.create_policy(policy_conn(), CONTRACTING, {**data, "title": "  "}, today=NOW.date())
    dup = policy_conn().on("INSERT INTO booking.cancellation_policies", asyncpg.UniqueViolationError("dup"))
    with pytest.raises(Conflict):
        await cancellation.create_policy(dup, CONTRACTING, data, today=NOW.date())
    conn = policy_conn({**ROW, "trip_plan_id": None, "source_id": None})
    out = await cancellation.create_policy(conn, CONTRACTING, {**data, "source_id": None}, today=NOW.date())
    assert out["source_id"] is None
    [insert] = conn.sql("INSERT INTO booking.cancellation_policies")
    assert insert[1] is None and insert[5] is None  # the default; blank note stored as NULL
    assert not conn.sql("SELECT id::text FROM booking.trip_plans")


def approve_conn(created_by: str | None = "u-5", approval: str | None = None) -> ScriptedConn:
    conn = policy_conn({**ROW, "status": "in_force", "approved_by": "a-1", "approved_at": NOW})
    conn.on(
        "FROM booking.cancellation_policies p LEFT JOIN booking.policy_approvals",
        {"created_by": created_by, "approval_id": approval},
    )
    return conn


def test_staff_approve_policy() -> None:
    conn = approve_conn()
    resp = staff_client(ADMIN, conn).post(f"{URL}/{PLAN_POLICY}/approve")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "in_force"
    [approval] = conn.sql("VALUES ('cancellation_policy', $1::uuid, $2)")
    assert approval == (PLAN_POLICY, "a-1")
    [audit] = conn.sql("INSERT INTO booking.audit_logs")
    assert audit[3] == "cancellation_policy.approved"


@pytest.mark.asyncio
async def test_staff_approve_policy_refusals() -> None:
    with pytest.raises(Forbidden):
        await cancellation.approve_policy(approve_conn("a-1"), ADMIN, PLAN_POLICY, today=NOW.date())
    with pytest.raises(Conflict):
        await cancellation.approve_policy(approve_conn(approval="x"), ADMIN, PLAN_POLICY, today=NOW.date())
    missing = approve_conn().on("FROM booking.cancellation_policies p LEFT JOIN booking.policy_approvals", None)
    with pytest.raises(NotFound) as exc:
        await cancellation.approve_policy(missing, ADMIN, PLAN_POLICY, today=NOW.date())
    assert exc.value.code == "cancellation_policy_not_found"
    gone = policy_conn().on("FROM booking.cancellation_policies p LEFT JOIN booking.trip_plans", None)
    with pytest.raises(NotFound):
        await cancellation._by_id(gone, ADMIN, PLAN_POLICY, NOW.date())


def test_start_constant_is_the_hold_flow_departure() -> None:
    assert START == dep_row()["start_date"]
