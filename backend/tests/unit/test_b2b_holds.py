"""AA-973 pure rules: line availability against departures (seats, auto-confirm, cancelled, started, lines adding up),
the departure-to-line binding, the expiry schedule key, and the hold routes' input checks. Real SQL (locks, counts,
confirm) is in tests/integration/test_bk020_b2b.py."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from app.b2b import departures, expiry
from app.b2b.departures import AVAILABLE, ON_REQUEST, SOLD_OUT, SeatAsk, evaluate
from app.b2b.errors import B2BError
from tests.staff_tokens import LocalM2M
from tests.unit.test_b2b_api import _client, _conn, _headers

PLAN = "8e000000-0000-4000-8000-000000000a01"
OTHER_PLAN = "8e000000-0000-4000-8000-000000000a02"
DEP = "9f000000-0000-4000-8000-000000000b01"
TODAY = date(2027, 2, 1)
START = date(2027, 3, 1)


def _row(**kw: object) -> dict[str, object]:
    row = {
        "id": DEP,
        "trip_plan_id": PLAN,
        "start_date": START,
        "capacity": 4,
        "status": "scheduled",
        "auto_confirm": True,
    }
    return {**row, **kw}


def _ask(ref: str = "L1", seats: int = 2, dep: str | None = DEP, comp: str = f"tp:{PLAN}", on: date = START) -> SeatAsk:
    return SeatAsk(line_ref=ref, component_id=comp, service_date=on, departure_id=dep, seats=seats)


def test_no_departure_is_on_request() -> None:
    assert evaluate([_ask(dep=None, comp="rt:x")], {}, {}, today=TODAY) == [(ON_REQUEST, None, None)]


def test_seats_left_and_auto_confirm() -> None:
    assert evaluate([_ask()], {DEP: _row()}, {DEP: (1, 1)}, today=TODAY) == [(AVAILABLE, DEP, None)]
    assert evaluate([_ask()], {DEP: _row(auto_confirm=False)}, {}, today=TODAY)[0][0] == ON_REQUEST
    status, _, why = evaluate([_ask(seats=3)], {DEP: _row()}, {DEP: (1, 1)}, today=TODAY)[0]
    assert status == SOLD_OUT and why == "2 seats left, 3 asked"


def test_capacity_zero_cancelled_and_started_are_sold_out() -> None:
    assert evaluate([_ask(seats=1)], {DEP: _row(capacity=0)}, {}, today=TODAY)[0][0] == SOLD_OUT
    assert evaluate([_ask()], {DEP: _row(status="cancelled")}, {}, today=TODAY)[0][2] == "the departure is cancelled"
    assert evaluate([_ask()], {DEP: _row()}, {}, today=START)[0][2] == "the departure has started"
    assert evaluate([_ask()], {DEP: _row(status="guaranteed")}, {}, today=TODAY)[0][0] == AVAILABLE


def test_lines_on_one_departure_add_up() -> None:
    out = evaluate([_ask("A", 2), _ask("B", 2), _ask("C", 1)], {DEP: _row()}, {}, today=TODAY)
    assert [o[0] for o in out] == [AVAILABLE, AVAILABLE, SOLD_OUT]


@pytest.mark.parametrize(
    "ask",
    [
        _ask(comp=f"tp:{OTHER_PLAN}"),  # another plan's departure
        _ask(comp="rt:9f000000-0000-4000-8000-000000000c01"),  # not a plan line
        _ask(on=START + timedelta(days=1)),  # not the start date
        _ask(dep="9f000000-0000-4000-8000-0000000000ff"),  # unknown
    ],
)
def test_departure_must_belong_to_the_line(ask: SeatAsk) -> None:
    with pytest.raises(B2BError) as exc:
        evaluate([ask], {DEP: _row()}, {}, today=TODAY)
    assert exc.value.error == "invalid" and exc.value.message.startswith("line L1:")


def test_seats_and_start_of() -> None:
    assert departures.seats(2, 1) == 3
    assert departures.start_of(START) == datetime(2027, 3, 1, tzinfo=UTC)


def test_schedule_key_is_one_per_utc_minute() -> None:
    at = datetime(2026, 10, 23, 19, 5, 59, tzinfo=timezone(timedelta(hours=7)))
    assert expiry.schedule_key(at) == "bk_b2b_hold_expire:20261023T1205"
    assert expiry.schedule_key(at + timedelta(seconds=1)) == "bk_b2b_hold_expire:20261023T1206"


def test_hold_routes_check_input_before_any_write() -> None:
    m2m, conn = LocalM2M(), _conn()
    client = _client(conn, m2m)
    body = {"quote_id": "q", "price_hash": "a" * 64, "proposal_ref": "P", "proposal_version": 1}
    for headers in (_headers(m2m.token()), {**_headers(m2m.token()), "Idempotency-Key": "short"}):
        resp = client.post("/booking/b2b/v1/holds", json=body, headers=headers)
        assert resp.status_code == 422 and resp.json()["error"] == "invalid", resp.text
    bad_hash = client.post(
        "/booking/b2b/v1/holds/h1/confirm",
        json={"price_hash": "nothex"},
        headers={**_headers(m2m.token()), "Idempotency-Key": "k" * 8},
    )
    assert bad_hash.status_code == 422 and bad_hash.json()["error"] == "invalid"
    # Scopes: a hold needs booking.b2b/hold, a confirm booking.b2b/book (checked before the body is used).
    key = {"Idempotency-Key": "k" * 8}
    read_only = {**_headers(m2m.token("booking.b2b/read")), **key}
    assert client.post("/booking/b2b/v1/holds", json=body, headers=read_only).status_code == 403
    confirm = {"price_hash": "a" * 64}
    hold_only = {**_headers(m2m.token("booking.b2b/hold")), **key}
    assert client.post("/booking/b2b/v1/holds/h1/confirm", json=confirm, headers=hold_only).status_code == 403
    assert not conn.sql("INSERT INTO booking.b2b_holds")
