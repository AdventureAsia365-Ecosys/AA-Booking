"""AA-973 hold / departure services with a scripted connection: placing (replay, proposal-version guard, hash, quote
validity, sold out, expiry cap, unique-violation retry), read / release, confirm into a B2B trip case (lines, items,
costs, approval), the re-check under the departure locks, the staff departure rules, the B2B departure listing and
the expiry job. The SQL itself (locks, counts, constraints) runs against Postgres in
tests/integration/test_bk020_b2b.py.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D
from typing import Any

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.auth.staff import Staff, current_staff, get_pool
from app.b2b import departures, expiry, holds
from app.b2b.errors import B2BError
from app.errors import Conflict, Invalid, NotFound
from app.main import create_app
from app.settings import Settings
from tests.staff_tokens import CRM_CLIENT, LocalM2M
from tests.unit.fakes import ScriptedConn
from tests.unit.test_b2b_api import PARTNER_ID, TENANT, _client, _conn, _headers

PLAN = "8e000000-0000-4000-8000-000000000a01"
ROOM = "8e000000-0000-4000-8000-000000000a02"
SERVICE = "8e000000-0000-4000-8000-000000000a03"
DEP = "9f000000-0000-4000-8000-000000000b01"
QUOTE = "9a000000-0000-4000-8000-000000000001"
OTHER_QUOTE = "9a000000-0000-4000-8000-000000000002"
HOLD = "9b000000-0000-4000-8000-000000000001"
CASE = "9c000000-0000-4000-8000-000000000001"
HASH = "a" * 64
NOW = datetime(2027, 2, 1, 9, 0, tzinfo=UTC)
START = date(2027, 3, 1)
CALLER = holds.Caller(partner_id=PARTNER_ID, tenant_id=TENANT, client_id="crm-client")
ADMIN = Staff("a-1", None, "Admin", "admin", True, "admin")
SALES = Staff("u-1", None, "Sales One", "sales", True, "reviewer")

HOLD_BY_KEY = "h.idempotency_key = $2"
HOLD_BY_VERSION = "h.status IN ('active', 'confirmed') FOR UPDATE"
HOLD_BY_ID = "FROM booking.b2b_holds h WHERE h.id = $1::uuid"
HOLD_FIND = "WHERE h.id = $1::uuid AND h.partner_id"


def quote_row(**kw: Any) -> dict[str, Any]:
    row = {
        "id": QUOTE,
        "proposal_ref": "P-1",
        "proposal_version": 1,
        "currency": "EUR",
        "fx_rate_per_usd": D("0.9"),
        "fx_as_of": date(2027, 1, 31),
        "total_b2b_price": D("1150.00"),
        "price_hash": HASH,
        "valid_until": NOW + timedelta(days=1),
    }
    return {**row, **kw}


def quote_lines() -> list[dict[str, Any]]:
    return [
        {
            "line_no": 1,
            "line_ref": "L1",
            "component_id": f"tp:{PLAN}",
            "component_kind": "trip_plan",
            "service_date": START,
            "nights": None,
            "pax_adult": 2,
            "pax_child": 1,
            "departure_id": DEP,
            "net_cost_usd": D("800.005"),
            "supplier_rate_ids": ["r-1", "r-1"],
        },
        {
            "line_no": 2,
            "line_ref": "L2",
            "component_id": f"rt:{ROOM}",
            "component_kind": "hotel_room",
            "service_date": START + timedelta(days=3),
            "nights": 2,
            "pax_adult": 2,
            "pax_child": 0,
            "departure_id": None,
            "net_cost_usd": D("200"),
            "supplier_rate_ids": ["r-2", "r-3"],
        },
        {
            "line_no": 3,
            "line_ref": "L3",
            "component_id": f"sv:{SERVICE}",
            "component_kind": "transfer",
            "service_date": START + timedelta(days=4),
            "nights": None,
            "pax_adult": 1,
            "pax_child": 0,
            "departure_id": None,
            "net_cost_usd": D("10"),
            "supplier_rate_ids": None,
        },
    ]


def dep_row(**kw: Any) -> dict[str, Any]:
    row = {
        "id": DEP,
        "trip_plan_id": PLAN,
        "start_date": START,
        "capacity": 10,
        "status": "scheduled",
        "auto_confirm": True,
        "created_at": NOW,
        "updated_at": NOW,
    }
    return {**row, **kw}


def hold_row(status: str = "active", stored: str | None = None, **kw: Any) -> dict[str, Any]:
    row = {
        "id": HOLD,
        "partner_id": PARTNER_ID,
        "quote_id": QUOTE,
        "proposal_ref": "P-1",
        "proposal_version": 1,
        "idempotency_key": "key-0001",
        "stored_status": stored or status,
        "status": status,
        "expires_at": NOW + timedelta(hours=48),
        "extended_count": 0,
        "trip_case_id": None,
        "deposit_ref": None,
    }
    return {**row, **kw}


HELD_LINES = [
    {"line_no": 1, "line_ref": "L1", "departure_id": DEP, "seats": 3, "held_as": "available", "status": "available"},
    {"line_no": 2, "line_ref": "L2", "departure_id": None, "seats": 0, "held_as": "on_request", "status": "on_request"},
]


def place_conn(**over: Any) -> ScriptedConn:
    """A connection for create_hold: no replay, no current hold, the departure with ``used`` seats taken."""
    conn = ScriptedConn()
    conn.on(HOLD_BY_ID, hold_row())
    conn.on(HOLD_BY_KEY, over.get("replay"))
    conn.on(HOLD_BY_VERSION, over.get("current"))
    conn.on("FROM booking.b2b_quotes WHERE id", over.get("quote", quote_row()))
    conn.on("FROM booking.b2b_quote_lines", over.get("lines", quote_lines()))
    conn.on("FROM booking.departures d WHERE d.id = ANY", [over.get("dep", dep_row())])
    conn.on("FROM booking.b2b_hold_lines l", [{"departure_id": DEP, "held": over.get("held", 0), "sold": 0}])
    conn.on("FROM booking.b2b_hold_lines WHERE hold_id", HELD_LINES)
    conn.on("FROM booking.settings", over.get("hours", "48"))
    conn.on("INSERT INTO booking.b2b_holds", HOLD)
    return conn


async def place(conn: ScriptedConn, **kw: Any) -> dict[str, Any]:
    args = {
        "quote_id": QUOTE,
        "price_hash": HASH,
        "proposal_ref": "P-1",
        "proposal_version": 1,
        "idempotency_key": "key-0001",
        "now": NOW,
    }
    return await holds.create_hold(conn, CALLER, **(args | kw))


# ── Place ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_place_counts_seats_per_line_and_audits() -> None:
    conn = place_conn()
    out = await place(conn)
    assert out == {
        "hold_id": HOLD,
        "quote_id": QUOTE,
        "status": "active",
        "expires_at": (NOW + timedelta(hours=48)).isoformat(),
        "extended_count": 0,
        "lines": [{"line_ref": "L1", "line_status": "available"}, {"line_ref": "L2", "line_status": "on_request"}],
    }
    [insert] = conn.sql("INSERT INTO booking.b2b_holds")
    assert insert[:6] == (PARTNER_ID, "crm-client", QUOTE, "P-1", 1, "key-0001")
    assert insert[6] == NOW + timedelta(hours=48) and insert[7] == NOW
    # Seats count only on a departure line (adults + children); the others hold 0 seats and are on request.
    lines = conn.sql("INSERT INTO booking.b2b_hold_lines")
    assert [(r[2], r[3], r[4], r[5]) for r in lines] == [
        ("L1", DEP, 3, "available"),
        ("L2", None, 0, "on_request"),
        ("L3", None, 0, "on_request"),
    ]
    [audit] = conn.sql("INSERT INTO booking.audit_logs")
    assert audit[2] == "m2m:crm-client" and audit[3] == "b2b_hold.placed" and audit[5] == HOLD
    # Holds of this version already past expiry are written down first, so they free the version.
    assert conn.sql("UPDATE booking.b2b_holds SET status = 'expired'") == [(PARTNER_ID, "P-1", 1)]


@pytest.mark.asyncio
async def test_place_expiry_is_capped_by_the_departure_start() -> None:
    soon = NOW.date() + timedelta(days=1)
    lines = quote_lines()[:1]
    lines[0]["service_date"] = soon
    conn = place_conn(lines=lines, dep=dep_row(start_date=soon), hours="720")
    await place(conn)
    [insert] = conn.sql("INSERT INTO booking.b2b_holds")
    assert insert[6] == datetime(2027, 2, 2, tzinfo=UTC)


@pytest.mark.asyncio
async def test_place_sold_out_holds_nothing() -> None:
    conn = place_conn(held=9)
    with pytest.raises(B2BError) as exc:
        await place(conn)
    assert exc.value.error == "sold_out" and exc.value.message == "line L1: 1 seats left, 3 asked"
    assert not conn.sql("INSERT INTO booking.b2b_holds")
    assert not conn.sql("INSERT INTO booking.b2b_hold_lines")


@pytest.mark.asyncio
async def test_place_replay_of_the_same_request_returns_the_hold() -> None:
    conn = place_conn(replay=hold_row())
    out = await place(conn)
    assert out["hold_id"] == HOLD
    assert not conn.sql("FROM booking.b2b_quotes WHERE id")
    assert not conn.sql("INSERT INTO booking.b2b_holds")


@pytest.mark.parametrize(
    "replay",
    [
        hold_row(quote_id=OTHER_QUOTE),
        hold_row(proposal_ref="P-2"),
        hold_row(proposal_version=2),
    ],
)
@pytest.mark.asyncio
async def test_place_key_reused_for_another_request_is_invalid(replay: dict[str, Any]) -> None:
    with pytest.raises(B2BError) as exc:
        await place(place_conn(replay=replay))
    assert exc.value.error == "invalid" and "Idempotency-Key" in exc.value.message


@pytest.mark.asyncio
async def test_place_rejects_unknown_or_foreign_quote() -> None:
    with pytest.raises(B2BError) as exc:
        await place(place_conn(), quote_id="not-a-uuid")
    assert exc.value.error == "not_found"
    with pytest.raises(B2BError) as exc:
        await place(place_conn(quote=None))
    assert exc.value.error == "not_found"


@pytest.mark.asyncio
async def test_place_proposal_must_be_the_quotes() -> None:
    with pytest.raises(B2BError) as exc:
        await place(place_conn(), proposal_version=2)
    assert exc.value.error == "invalid" and "proposal_version" in exc.value.message


@pytest.mark.asyncio
async def test_place_wrong_hash_or_expired_quote_is_price_changed() -> None:
    with pytest.raises(B2BError) as exc:
        await place(place_conn(), price_hash="b" * 64)
    assert exc.value.error == "price_changed" and exc.value.extra == {"price_hash": HASH}
    with pytest.raises(B2BError) as exc:
        await place(place_conn(quote=quote_row(valid_until=NOW)))
    assert exc.value.error == "price_changed" and "expired" in exc.value.message
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_place_same_version_returns_current_hold_or_refuses_another_quote() -> None:
    conn = place_conn(current=hold_row())
    assert (await place(conn))["hold_id"] == HOLD
    assert not conn.sql("INSERT INTO booking.b2b_holds")
    with pytest.raises(B2BError) as exc:
        await place(place_conn(current=hold_row(status="confirmed", quote_id=OTHER_QUOTE)))
    assert exc.value.error == "invalid"
    assert f"hold {HOLD} already holds this proposal version (confirmed)" in exc.value.message


@pytest.mark.asyncio
async def test_place_retries_once_after_a_concurrent_insert() -> None:
    conn = place_conn()
    replays = iter([None, hold_row()])
    conn.on(HOLD_BY_KEY, lambda *_: next(replays))
    inserts = iter([asyncpg.UniqueViolationError("dup")])
    conn.on("INSERT INTO booking.b2b_holds", lambda *_: next(inserts))
    out = await place(conn)
    assert out["hold_id"] == HOLD and len(conn.sql(HOLD_BY_KEY)) == 2


@pytest.mark.asyncio
async def test_place_second_unique_violation_is_raised() -> None:
    conn = place_conn()
    conn.on("INSERT INTO booking.b2b_holds", asyncpg.UniqueViolationError("dup"))
    with pytest.raises(asyncpg.UniqueViolationError):
        await place(conn)
    assert len(conn.sql("INSERT INTO booking.b2b_holds")) == 2


@pytest.mark.parametrize(
    ("stored", "hours"),
    [("72", 72), (12, 12), ("abc", 48), (None, 48), ("0", 48), ("721", 48), ('"24"', 24)],
)
@pytest.mark.asyncio
async def test_hold_hours_setting(stored: Any, hours: int) -> None:
    assert await holds._hold_hours(ScriptedConn().on("FROM booking.settings", stored)) == hours


# ── Read / release ───────────────────────────────────────────────────────────


def find_conn(hold: dict[str, Any] | None) -> ScriptedConn:
    conn = ScriptedConn()
    conn.on(HOLD_FIND, hold)
    conn.on("FROM booking.b2b_hold_lines WHERE hold_id", HELD_LINES)
    return conn


@pytest.mark.asyncio
async def test_get_hold_reads_effective_status() -> None:
    conn = find_conn(hold_row(status="expired", stored="active"))
    out = await holds.get_hold(conn, CALLER, HOLD.upper())
    assert out["status"] == "expired" and [line["line_ref"] for line in out["lines"]] == ["L1", "L2"]
    # The id is normalised and the read is scoped to the caller's partner; a read takes no lock.
    assert conn.sql(HOLD_FIND) == [(HOLD, PARTNER_ID)]
    assert "FOR UPDATE" not in next(flat for flat, _ in conn.calls if HOLD_FIND in flat)


@pytest.mark.asyncio
async def test_get_hold_carries_the_quote_it_holds() -> None:
    """AA-952 (contract 1.2): the held quote's hash, currency, FX lock (the selling rate, as POST /quotes gave it) and
    each line's b2b price; never the net cost or the supplier rates."""
    conn = find_conn(hold_row())
    conn.on("FROM booking.b2b_quotes q WHERE q.id", {
        "currency": "VND", "fx_rate_per_usd": D("25000"), "fx_buffer_pct": D("0.02"), "fx_as_of": date(2027, 2, 1),
        "fx_source": "test", "price_hash": "b" * 64,
    })  # fmt: skip
    conn.on("FROM booking.b2b_quote_lines ql", [{"line_ref": "L1", "b2b_price": D("12345000")}])
    out = await holds.get_hold(conn, CALLER, HOLD)
    assert out["price_hash"] == "b" * 64 and out["currency"] == "VND"
    assert out["fx"] == {"base_currency": "USD", "quote_currency": "VND", "rate_per_usd": "25500.00000000",
                         "rate_date": "2027-02-01", "source": "test"}  # fmt: skip
    assert out["lines"][0]["b2b_price"] == {"amount": "12345000", "currency": "VND"}
    assert "b2b_price" not in out["lines"][1]  # a line the quote has no price for is left without one
    assert conn.sql("FROM booking.b2b_quote_lines ql") == [(QUOTE,)]
    assert "net_cost" not in str(out) and "supplier" not in str(out)
    # The response model accepts it (extra="forbid"): the 1.2 fields are part of the contract.
    from app.routers.b2b import Hold

    assert Hold.model_validate(out).fx is not None


@pytest.mark.asyncio
async def test_get_hold_without_its_quote_row_keeps_the_1_1_shape() -> None:
    out = await holds.get_hold(find_conn(hold_row()), CALLER, HOLD)
    assert {"price_hash", "currency", "fx"}.isdisjoint(out) and all("b2b_price" not in x for x in out["lines"])


@pytest.mark.asyncio
async def test_get_hold_of_another_partner_or_bad_id_is_not_found() -> None:
    for hold_id in (HOLD, "nope"):
        conn = find_conn(None)
        with pytest.raises(B2BError) as exc:
            await holds.get_hold(conn, CALLER, hold_id)
        assert exc.value.error == "not_found" and exc.value.status_code == 404
    assert not find_conn(None).sql(HOLD_FIND)


@pytest.mark.asyncio
async def test_release_active_hold_is_audited() -> None:
    conn = find_conn(hold_row())
    await holds.release_hold(conn, CALLER, HOLD)
    assert conn.sql("SET status = 'released'") == [(HOLD,)]
    [audit] = conn.sql("INSERT INTO booking.audit_logs")
    assert audit[3] == "b2b_hold.released"
    assert "FOR UPDATE" in next(flat for flat, _ in conn.calls if HOLD_FIND in flat)


@pytest.mark.asyncio
async def test_release_confirmed_hold_is_refused() -> None:
    with pytest.raises(B2BError) as exc:
        await holds.release_hold(find_conn(hold_row(status="confirmed")), CALLER, HOLD)
    assert exc.value.error == "invalid" and "cancel the booking" in exc.value.message


@pytest.mark.asyncio
async def test_release_past_expiry_writes_expired_without_audit() -> None:
    conn = find_conn(hold_row(status="expired", stored="active"))
    await holds.release_hold(conn, CALLER, HOLD)
    assert conn.sql("SET status = 'expired'") == [(HOLD,)]
    assert not conn.sql("SET status = 'released'") and not conn.sql("INSERT INTO booking.audit_logs")


@pytest.mark.parametrize("status", ["released", "expired"])
@pytest.mark.asyncio
async def test_release_of_a_finished_hold_is_a_no_op(status: str) -> None:
    conn = find_conn(hold_row(status=status))
    await holds.release_hold(conn, CALLER, HOLD)
    assert not conn.sql("UPDATE booking.b2b_holds")


# ── Confirm ──────────────────────────────────────────────────────────────────


def confirm_conn(hold: dict[str, Any] | None = None, **over: Any) -> ScriptedConn:
    conn = find_conn(hold or hold_row())
    conn.on(HOLD_BY_ID, hold_row(status="confirmed", trip_case_id=CASE))
    conn.on(HOLD_FIND, hold or hold_row())
    conn.on("FROM booking.b2b_quotes WHERE id", quote_row())
    conn.on("FROM booking.b2b_quote_lines", over.get("lines", quote_lines()))
    conn.on("FROM booking.departures d WHERE d.id = ANY", [over.get("dep", dep_row())])
    conn.on("SELECT clock_timestamp()", over.get("clock", NOW))
    conn.on("FROM booking.b2b_hold_lines l", [{"departure_id": DEP, "held": over.get("others", 0), "sold": 0}])
    conn.on("FROM booking.trip_plans p", [{"id": PLAN, "source_id": "BT-7D", "days": 3}])
    conn.on("INSERT INTO booking.trip_cases", {"id": CASE, "code": "TC-000042"})
    conn.on("INSERT INTO booking.trip_case_versions", "9d000000-0000-4000-8000-000000000001")
    items = iter(f"9e000000-0000-4000-8000-00000000000{n}" for n in range(1, 10))
    conn.on("INSERT INTO booking.trip_case_version_itinerary", lambda *_: next(items))
    conn.on("SELECT code FROM booking.trip_cases", "TC-000042")
    return conn


async def confirm(conn: ScriptedConn, **kw: Any) -> dict[str, Any]:
    args = {"price_hash": HASH, "deposit_ref": "DEP-1", "idempotency_key": "confirm-1"}
    return await holds.confirm_hold(conn, CALLER, HOLD, **(args | kw))


@pytest.mark.asyncio
async def test_confirm_opens_a_b2b_case_with_an_approved_version() -> None:
    conn = confirm_conn()
    out = await confirm(conn)
    assert out == {
        "booking_id": CASE,
        "hold_id": HOLD,
        "trip_case_code": "TC-000042",
        "lines": [{"line_ref": "L1", "status": "available"}, {"line_ref": "L2", "status": "on_request"}],
    }
    [case] = conn.sql("INSERT INTO booking.trip_cases")
    # source_id = proposal ref, the agency tenant, the plan's 3 days from 1 March; the room line ends after 2 nights
    # on 5 March, the service on 5 March; the biggest party is the plan line (2 adults + 1 child).
    assert case == ("P-1", TENANT, START, date(2027, 3, 5), 3, 2, 1, "EUR", f"b2b_hold:{HOLD}")
    [version] = conn.sql("INSERT INTO booking.trip_case_versions")
    assert version[1:] == ("B2B P-1 v1", D("1150.00"), "EUR", date(2027, 2, 2), D("0.9"), date(2027, 1, 31))
    items = conn.sql("INSERT INTO booking.trip_case_version_itinerary")
    assert [(i[1], i[3], i[4], i[5], i[6], i[7]) for i in items] == [
        (1, 1, 3, "BT-7D", None, None),
        (2, 4, 5, None, ROOM, None),
        (3, 5, 5, None, None, SERVICE),
    ]
    costs = conn.sql("INSERT INTO booking.version_item_costs")
    assert [(c[1], c[3]) for c in costs] == [(D("800.01"), "r-1"), (D("200.00"), None), (D("10.00"), None)]
    assert "2 supplier rates" in costs[0][2] and "0 supplier rates" in costs[2][2]
    assert conn.sql("SET is_approved = true")[0][1] == f"Approved by the B2B confirm of hold {HOLD}"
    assert conn.sql("UPDATE booking.b2b_hold_lines SET status = 'confirmed'") == [(HOLD,)]
    # No approved cancellation policy in this scripted DB: none is locked (a cancel then resolves it, AA-974).
    assert conn.sql("SET status = 'confirmed', confirmed_at") == [(HOLD, CASE, "DEP-1", None)]
    actions = [a[3] for a in conn.sql("INSERT INTO booking.audit_logs")]
    assert actions == ["trip_case.created", "b2b_hold.confirmed"]
    # The status history names no staff member (system change) and notes the M2M client.
    assert conn.sql("set_config('booking.actor'")
    assert conn.sql("set_config('booking.status_note'") == [(f"B2B confirm by m2m:crm-client of hold {HOLD}",)]


@pytest.mark.asyncio
async def test_confirm_of_a_confirmed_hold_returns_its_booking() -> None:
    conn = confirm_conn(hold_row(status="confirmed", trip_case_id=CASE))
    out = await confirm(conn, idempotency_key="another-key")
    assert out["booking_id"] == CASE and out["trip_case_code"] == "TC-000042"
    assert not conn.sql("INSERT INTO booking.trip_cases")


@pytest.mark.asyncio
async def test_confirm_needs_the_quote_hash() -> None:
    conn = confirm_conn()
    with pytest.raises(B2BError) as exc:
        await confirm(conn, price_hash="c" * 64)
    assert exc.value.error == "price_changed"
    assert not conn.sql("INSERT INTO booking.trip_cases")


@pytest.mark.parametrize("hold", [hold_row(status="expired", stored="active"), hold_row(status="released")])
@pytest.mark.asyncio
async def test_confirm_of_an_expired_or_released_hold_is_gone(hold: dict[str, Any]) -> None:
    with pytest.raises(B2BError) as exc:
        await confirm(confirm_conn(hold))
    assert exc.value.error == "hold_expired" and exc.value.status_code == 410


@pytest.mark.asyncio
async def test_confirm_judges_expiry_on_the_clock_after_the_locks() -> None:
    conn = confirm_conn(clock=NOW + timedelta(hours=48))
    with pytest.raises(B2BError) as exc:
        await confirm(conn)
    assert exc.value.error == "hold_expired" and "while it was being confirmed" in exc.value.message
    assert not conn.sql("INSERT INTO booking.trip_cases")


@pytest.mark.asyncio
async def test_confirm_on_a_cancelled_departure_is_sold_out() -> None:
    with pytest.raises(B2BError) as exc:
        await confirm(confirm_conn(dep=dep_row(status="cancelled")))
    assert exc.value.error == "sold_out" and DEP in exc.value.message


@pytest.mark.asyncio
async def test_confirm_recounts_seats_without_this_hold() -> None:
    conn = confirm_conn(others=8)
    with pytest.raises(B2BError) as exc:
        await confirm(conn)
    assert exc.value.error == "hold_expired" and "no longer has this hold's seats" in exc.value.message
    [usage] = conn.sql("FROM booking.b2b_hold_lines l")
    assert usage == ([DEP], NOW, HOLD)
    # 7 others + 3 of this hold fit a capacity of 10.
    assert (await confirm(confirm_conn(others=7)))["booking_id"] == CASE


@pytest.mark.asyncio
async def test_confirm_of_an_on_request_only_hold_takes_no_lock() -> None:
    held = [HELD_LINES[1]]
    conn = confirm_conn(lines=quote_lines()[1:])
    conn.on("FROM booking.b2b_hold_lines WHERE hold_id", held)
    out = await confirm(conn)
    assert out["lines"] == [{"line_ref": "L2", "status": "on_request"}]
    assert not conn.sql("FROM booking.departures d") and not conn.sql("FROM booking.b2b_hold_lines l")
    assert not conn.sql("FROM booking.trip_plans p")
    [case] = conn.sql("INSERT INTO booking.trip_cases")
    assert case[2:5] == (START + timedelta(days=3), date(2027, 3, 5), 2)


# ── Departures: availability inputs, B2B listing, staff rules ───────────────


@pytest.mark.asyncio
async def test_availability_without_departures_reads_nothing() -> None:
    conn = ScriptedConn()
    ask = departures.SeatAsk("L1", f"rt:{ROOM}", START, None, 2)
    assert await departures.availability(conn, [ask], today=NOW.date()) == [(departures.ON_REQUEST, None, None)]
    assert conn.calls == []
    assert await departures.usage(conn, []) == {}


@pytest.mark.asyncio
async def test_availability_rejects_a_malformed_departure_or_component() -> None:
    conn = ScriptedConn().on("FROM booking.departures d", [dep_row()])
    with pytest.raises(B2BError) as exc:
        await departures.availability(conn, [departures.SeatAsk("L1", f"tp:{PLAN}", START, "dep-1", 2)], today=START)
    assert exc.value.message == "line L1: unknown departure_id"
    with pytest.raises(B2BError) as exc:
        await departures.availability(conn, [departures.SeatAsk("L2", "zz:1", START, DEP, 2)], today=NOW.date())
    assert exc.value.message == "line L2: unknown component_id"


def listing_conn(rows: Any) -> ScriptedConn:
    conn = ScriptedConn()
    conn.on("JOIN booking.trip_plans p ON p.id = d.trip_plan_id", rows)
    conn.on("FROM booking.b2b_hold_lines l", [{"departure_id": DEP, "held": 3, "sold": 2}])
    return conn


@pytest.mark.asyncio
async def test_list_for_tour_counts_held_and_sold() -> None:
    other = "9f000000-0000-4000-8000-000000000b02"
    rows = [
        dep_row(source_system="cis", source_id="BT-7D"),
        dep_row(id=other, status="cancelled", source_system="cis", source_id="BT-7D"),
    ]
    conn = listing_conn(rows)
    out = await departures.list_for_tour(
        conn, source_system="cis", source_id="BT-7D", start=None, end=None, today=NOW.date()
    )
    assert out["items"][0] == {
        "departure_id": DEP,
        "tour": {"source_system": "cis", "source_id": "BT-7D"},
        "start_date": "2027-03-01",
        "capacity": 10,
        "held": 3,
        "sold": 2,
        "available": 5,
        "status": "scheduled",
        "auto_confirm": True,
    }
    assert out["items"][1]["available"] == 0 and out["items"][1]["held"] == 0
    [args] = conn.sql("JOIN booking.trip_plans p")
    assert args == ("cis", "BT-7D", NOW.date(), NOW.date() + timedelta(days=departures.LIST_DAYS))


@pytest.mark.parametrize(
    ("start", "end", "message"),
    [
        (START, START - timedelta(days=1), "to must not be before from"),
        (START, START + timedelta(days=2 * departures.LIST_DAYS + 1), "at most 732 days per request"),
    ],
)
@pytest.mark.asyncio
async def test_list_for_tour_window(start: date, end: date, message: str) -> None:
    with pytest.raises(B2BError) as exc:
        await departures.list_for_tour(
            listing_conn([]), source_system="cis", source_id="BT-7D", start=start, end=end, today=NOW.date()
        )
    assert exc.value.error == "invalid" and exc.value.message == message


@pytest.mark.asyncio
async def test_list_for_tour_unreadable_catalog_is_unavailable() -> None:
    conn = listing_conn(asyncpg.UndefinedTableError("catalog.v_tour"))
    with pytest.raises(B2BError) as exc:
        await departures.list_for_tour(
            conn, source_system="cis", source_id="BT-7D", start=START, end=START, today=NOW.date()
        )
    assert exc.value.error == "unavailable" and exc.value.extra == {"retry_after_s": 60}


def staff_conn(plan: str | None = PLAN) -> ScriptedConn:
    conn = ScriptedConn()
    conn.on("FROM booking.trip_plans WHERE source_system = 'cis'", plan)
    return conn


@pytest.mark.asyncio
async def test_staff_list_needs_a_saved_plan() -> None:
    with pytest.raises(NotFound) as exc:
        await departures.list_departures(staff_conn(None), "BT-7D")
    assert exc.value.code == "trip_plan_not_found"


@pytest.mark.asyncio
async def test_staff_list_shows_seats() -> None:
    conn = staff_conn()
    conn.on("FROM booking.departures d WHERE d.trip_plan_id", [dep_row()])
    conn.on("FROM booking.b2b_hold_lines l", [{"departure_id": DEP, "held": 4, "sold": 1}])
    out = await departures.list_departures(conn, "BT-7D", start=START, end=None)
    assert out["trip_plan_id"] == PLAN and out["source_id"] == "BT-7D"
    assert {k: out["items"][0][k] for k in ("held", "sold", "available")} == {"held": 4, "sold": 1, "available": 5}
    assert conn.sql("FROM booking.departures d WHERE d.trip_plan_id") == [(PLAN, START, None)]


def new_departure(**kw: Any) -> dict[str, Any]:
    data = {"start_date": datetime.now(UTC).date() + timedelta(days=30), "capacity": 12, "auto_confirm": False}
    return data | kw


@pytest.mark.asyncio
async def test_create_departure_writes_and_audits() -> None:
    conn = staff_conn()
    data = new_departure(status="guaranteed")
    conn.on("INSERT INTO booking.departures", dep_row(start_date=data["start_date"], capacity=12, status="guaranteed"))
    out = await departures.create_departure(conn, ADMIN, "BT-7D", data)
    assert (out["capacity"], out["held"], out["sold"], out["available"]) == (12, 0, 0, 12)
    [insert] = conn.sql("INSERT INTO booking.departures")
    assert insert == (PLAN, data["start_date"], 12, "guaranteed", False, "a-1")
    assert conn.sql("UPDATE booking.trip_plans SET updated_at") == [(PLAN,)]
    [audit] = conn.sql("INSERT INTO booking.audit_logs")
    assert audit[3] == "departure.created" and "12 seats (guaranteed)" in audit[9]


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (new_departure(capacity=-1), "invalid_capacity"),
        (new_departure(capacity=departures.MAX_CAPACITY + 1), "invalid_capacity"),
        (new_departure(start_date=datetime.now(UTC).date()), "invalid_start_date"),
        (new_departure(status="cancelled"), "invalid_status"),
    ],
)
@pytest.mark.asyncio
async def test_create_departure_rules(data: dict[str, Any], code: str) -> None:
    conn = staff_conn()
    with pytest.raises(Invalid) as exc:
        await departures.create_departure(conn, ADMIN, "BT-7D", data)
    assert exc.value.code == code
    assert not conn.sql("INSERT INTO booking.departures")


@pytest.mark.asyncio
async def test_create_departure_twice_on_a_date_is_a_conflict() -> None:
    conn = staff_conn().on("INSERT INTO booking.departures", asyncpg.UniqueViolationError("dup"))
    with pytest.raises(Conflict) as exc:
        await departures.create_departure(conn, ADMIN, "BT-7D", new_departure())
    assert exc.value.code == "departure_exists"


def update_conn(before: dict[str, Any] | None = None, held: int = 0, sold: int = 0) -> ScriptedConn:
    conn = ScriptedConn()
    conn.on("ORDER BY d.id FOR UPDATE", [before or dep_row()])
    conn.on("FROM booking.b2b_hold_lines l", [{"departure_id": DEP, "held": held, "sold": sold}])
    conn.on("UPDATE booking.departures", lambda _id, cap, status, auto, by: dep_row(capacity=cap or 10, status=status))
    return conn


@pytest.mark.asyncio
async def test_update_departure_changes_and_audits_only_what_changed() -> None:
    conn = update_conn(held=2, sold=3)
    out = await departures.update_departure(conn, ADMIN, DEP, {"capacity": 5, "auto_confirm": True, "status": None})
    assert (out["capacity"], out["available"]) == (5, 0)
    assert conn.sql("UPDATE booking.departures") == [(DEP, 5, None, None, "a-1")]
    [audit] = conn.sql("INSERT INTO booking.audit_logs")
    assert audit[3] == "departure.updated" and audit[9] == "Departure capacity 10 -> 5"


@pytest.mark.asyncio
async def test_update_departure_without_change_writes_nothing() -> None:
    conn = update_conn(held=1)
    out = await departures.update_departure(conn, ADMIN, DEP, {"capacity": 10, "status": "scheduled"})
    assert out["held"] == 1 and out["available"] == 9
    assert not conn.sql("UPDATE booking.departures") and not conn.sql("INSERT INTO booking.audit_logs")


@pytest.mark.asyncio
async def test_update_departure_unknown_is_not_found() -> None:
    conn = ScriptedConn()
    for dep_id in ("nope", DEP):
        with pytest.raises(NotFound) as exc:
            await departures.update_departure(conn, ADMIN, dep_id, {"capacity": 1})
        assert exc.value.code == "departure_not_found"
    assert len(conn.calls) == 1  # a malformed id is not looked up


@pytest.mark.parametrize(
    ("before", "changes", "error", "code"),
    [
        (dep_row(status="cancelled"), {"status": "scheduled"}, Conflict, "departure_cancelled"),
        (dep_row(), {"capacity": 4}, Conflict, "capacity_below_taken"),
        (dep_row(), {"capacity": departures.MAX_CAPACITY + 1}, Invalid, "invalid_capacity"),
        (dep_row(), {"status": "sold"}, Invalid, "invalid_status"),
    ],
)
@pytest.mark.asyncio
async def test_update_departure_rules(
    before: dict[str, Any], changes: dict[str, Any], error: type[Exception], code: str
) -> None:
    conn = update_conn(before, held=3, sold=2)
    with pytest.raises(error) as exc:
        await departures.update_departure(conn, ADMIN, DEP, changes)
    assert exc.value.code == code  # type: ignore[attr-defined]
    assert not conn.sql("UPDATE booking.departures")


# ── Expiry job ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_expire_due_runs_batches_until_a_short_one() -> None:
    batches = iter([[{"id": "a"}, {"id": "b"}], [{"id": "c"}], [{"id": "never"}]])
    conn = ScriptedConn().on("UPDATE booking.b2b_holds h SET status = 'expired'", lambda *_: next(batches))
    assert await expiry.expire_due(conn, batch=2) == 3
    assert conn.sql("UPDATE booking.b2b_holds h") == [(2,), (2,)]


@pytest.mark.asyncio
async def test_expire_due_stops_at_max_batches() -> None:
    conn = ScriptedConn().on("UPDATE booking.b2b_holds h SET status = 'expired'", [{"id": "a"}])
    assert await expiry.expire_due(conn, batch=1, max_batches=3) == 3


@pytest.mark.asyncio
async def test_expiry_handler_reports_the_count() -> None:
    conn = ScriptedConn().on("UPDATE booking.b2b_holds h", [{"id": "a"}])
    assert await expiry.HANDLERS[expiry.KIND](conn, None) == {"expired": 1}  # type: ignore[arg-type]
    assert await expiry.handle(ScriptedConn(), None) == {"expired": 0}  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_expiry_schedule_queues_only_when_a_hold_is_due() -> None:
    at = datetime(2027, 2, 1, 9, 30, 15, tzinfo=UTC)
    schedule = expiry.ExpirySchedule(now=lambda: at)
    idle = ScriptedConn().on("SELECT EXISTS", False)
    assert await schedule(idle) is None
    assert not idle.sql("INSERT INTO booking.job")
    due = ScriptedConn().on("SELECT EXISTS", True).on("INSERT INTO booking.job", "job-1")
    assert await schedule(due) == "job-1"
    [args] = due.sql("INSERT INTO booking.job")
    assert expiry.KIND in args and "bk_b2b_hold_expire:20270201T0930" in args and expiry.SCHEDULER in args


# ── Routes: caller mapping and status codes ─────────────────────────────────


class Recorder:
    def __init__(self, result: Any) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.result = result

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args[1:], kwargs))
        return self.result


HOLD_OUT = {
    "hold_id": HOLD,
    "quote_id": QUOTE,
    "status": "active",
    "expires_at": NOW.isoformat(),
    "extended_count": 0,
    "lines": [{"line_ref": "L1", "line_status": "available"}],
}
BOOKING_OUT = {
    "booking_id": CASE,
    "hold_id": HOLD,
    "trip_case_code": "TC-000042",
    "lines": [{"line_ref": "L1", "status": "confirmed"}],
}


def test_b2b_hold_routes_pass_the_partner_as_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    m2m = LocalM2M()
    client = _client(_conn(), m2m)
    place_rec, get_rec, release_rec, confirm_rec = (
        Recorder(HOLD_OUT),
        Recorder(HOLD_OUT),
        Recorder(None),
        Recorder(BOOKING_OUT),
    )
    monkeypatch.setattr(holds, "create_hold", place_rec)
    monkeypatch.setattr(holds, "get_hold", get_rec)
    monkeypatch.setattr(holds, "release_hold", release_rec)
    monkeypatch.setattr(holds, "confirm_hold", confirm_rec)
    key = {"Idempotency-Key": "key-0001"}
    body = {"quote_id": QUOTE, "price_hash": HASH, "proposal_ref": "P-1", "proposal_version": 1}

    resp = client.post("/booking/b2b/v1/holds", json=body, headers={**_headers(m2m.token()), **key})
    assert resp.status_code == 201 and resp.json()["hold_id"] == HOLD, resp.text
    (caller,), kwargs = place_rec.calls[0]
    assert caller == holds.Caller(partner_id=PARTNER_ID, tenant_id=TENANT, client_id=CRM_CLIENT)
    assert kwargs == {**{k: body[k] for k in body}, "idempotency_key": "key-0001"}

    assert client.get(f"/booking/b2b/v1/holds/{HOLD}", headers=_headers(m2m.token())).status_code == 200
    assert get_rec.calls[0][0] == (caller, HOLD)
    resp = client.delete(f"/booking/b2b/v1/holds/{HOLD}", headers=_headers(m2m.token()))
    assert resp.status_code == 204 and release_rec.calls[0][0] == (caller, HOLD)
    resp = client.post(
        f"/booking/b2b/v1/holds/{HOLD}/confirm",
        json={"price_hash": HASH, "deposit_ref": "DEP-1"},
        headers={**_headers(m2m.token()), **key},
    )
    assert resp.status_code == 201 and resp.json()["trip_case_code"] == "TC-000042", resp.text
    assert confirm_rec.calls[0] == (
        (caller, HOLD),
        {"price_hash": HASH, "deposit_ref": "DEP-1", "idempotency_key": "key-0001"},
    )


def test_b2b_hold_of_another_partner_is_a_404_problem() -> None:
    m2m = LocalM2M()
    conn = _conn()
    conn.on(HOLD_FIND, None)
    resp = _client(conn, m2m).get(f"/booking/b2b/v1/holds/{HOLD}", headers=_headers(m2m.token()))
    assert resp.status_code == 404 and resp.json() == {"error": "not_found", "message": "hold not found"}


def test_b2b_departures_route(monkeypatch: pytest.MonkeyPatch) -> None:
    m2m = LocalM2M()
    listing = Recorder({"items": []})
    monkeypatch.setattr(departures, "list_for_tour", listing)
    client = _client(_conn(), m2m)
    params = {"source_system": "cis", "source_id": "BT-7D", "from": "2027-03-01", "to": "2027-04-01"}
    resp = client.get("/booking/b2b/v1/departures", params=params, headers=_headers(m2m.token()))
    assert resp.status_code == 200 and resp.json() == {"items": []}, resp.text
    kwargs = listing.calls[0][1]
    assert (kwargs["source_id"], kwargs["start"], kwargs["end"]) == ("BT-7D", date(2027, 3, 1), date(2027, 4, 1))
    bad = client.get(
        "/booking/b2b/v1/departures", params={**params, "source_system": "x"}, headers=_headers(m2m.token())
    )
    assert bad.status_code == 422 and bad.json()["error"] == "invalid"


def staff_client(staff: Staff) -> TestClient:
    app = create_app(Settings(database_url=None, environment="test"))
    app.dependency_overrides[current_staff] = lambda: staff
    app.dependency_overrides[get_pool] = lambda: ScriptedConn()
    return TestClient(app)


def test_staff_departure_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    source = "aa679000-0000-4000-8000-00000000000a"
    listing, create, update = Recorder({"items": []}), Recorder({"id": DEP}), Recorder({"id": DEP})
    monkeypatch.setattr(departures, "list_departures", listing)
    monkeypatch.setattr(departures, "create_departure", create)
    monkeypatch.setattr(departures, "update_departure", update)
    url = f"/booking/catalog/trip-plans/{source}/departures"

    assert staff_client(SALES).get(url, params={"from": "2027-03-01"}).status_code == 200
    assert listing.calls[0] == ((source,), {"start": date(2027, 3, 1), "end": None})

    body = {"start_date": "2027-03-01", "capacity": 12}
    assert staff_client(SALES).post(url, json=body).status_code == 403
    assert staff_client(ADMIN).post(url, json={**body, "capacity": -1}).status_code == 422
    assert staff_client(ADMIN).post(url, json={**body, "status": "cancelled"}).status_code == 422
    assert staff_client(ADMIN).post(url, json=body).status_code == 201
    (_, src, data), _ = create.calls[0]
    assert src == source and data == {
        "start_date": date(2027, 3, 1),
        "capacity": 12,
        "auto_confirm": True,
        "status": "scheduled",
    }

    patch = f"/booking/catalog/departures/{DEP}"
    assert staff_client(ADMIN).patch(patch, json={"status": "cancelled"}).status_code == 200
    (_, dep_id, changes), _ = update.calls[0]
    assert dep_id == DEP and changes == {"status": "cancelled"}
    assert staff_client(ADMIN).patch("/booking/catalog/departures/nope", json={}).status_code == 422
    assert staff_client(SALES).patch(patch, json={"capacity": 3}).status_code == 403
