"""AA-972: booking.b2b.v1 routes with a scripted connection — auth chain, Problem errors, pricing, hash, naming.

Real SQL (bk-019, rates in force, append-only grants) is covered by tests/integration/test_bk019_b2b.py.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.b2b import components, quotes
from app.b2b.components import LineAsk, RoomAsk
from app.main import create_app
from app.pricing import repository
from app.pricing.engine import Channel, PricingRule
from tests.staff_tokens import CRM_CLIENT, OTHER_M2M_CLIENT, LocalCis, LocalM2M
from tests.unit.fakes import ScriptedConn, served_plan
from tests.unit.test_pricing_api import ScriptedDatabase

TENANT = "5b7d6f00-0000-4000-8000-0000000000aa"
PARTNER_ID = "6c8e7a00-0000-4000-8000-0000000000bb"
ROOM = "7d9f8b00-0000-4000-8000-0000000000c1"
SERVICE = "7d9f8b00-0000-4000-8000-0000000000c2"
DEP = "7d9f8b00-0000-4000-8000-0000000000d1"
TODAY = datetime.now(UTC).date()

B2B_RULE = {
    "channel": "b2b",
    "target_margin": D("0.1500"),
    "floor_margin": D("0.1000"),
    "peak_uplift_pts": D("0.0300"),
    "card_fee_pct": D("0.0300"),
    "valid_from": date(2026, 10, 14),
}
FX = [
    {"currency": "VND", "rate_per_usd": D("25000"), "buffer_pct": D("0"), "as_of": TODAY, "source": "synthetic"},
    {"currency": "EUR", "rate_per_usd": D("0.9"), "buffer_pct": D("0.02"), "as_of": TODAY, "source": "synthetic"},
]
PARTNER = {
    "id": PARTNER_ID,
    "tenant_id": TENANT,
    "agency_name": "Agency One",
    "status": "active",
    "billing_currency": "USD",
    "m2m_client_id": CRM_CLIENT,
}


def _rate(rid: str, unit: str, amount: str, currency: str) -> dict[str, Any]:
    return {"id": rid, "unit": unit, "net_amount": amount, "currency": currency}


def _rates(per_ask: Any) -> Any:
    """Script the batched rate query: ``per_ask(room, service, on, pax)`` → a rate dict or None, for each ask."""

    def answer(rooms: list, services: list, days: list, paxes: list) -> list[dict[str, Any]]:
        out = []
        for n, ask in enumerate(zip(rooms, services, days, paxes, strict=True), start=1):
            rate = per_ask(*ask)
            out.append({"n": n, **(rate or {"id": None, "unit": None, "net_amount": None, "currency": None})})
        return out

    return answer


def _rate_asks(conn: ScriptedConn) -> list[tuple[Any, ...]]:
    """The (room, service, day, pax) asks of the single batched rate query of a request."""
    [call] = conn.sql("FROM booking.supplier_rates x")
    return list(zip(*call, strict=True))


def _conn(partner: Any = PARTNER) -> ScriptedConn:
    conn = ScriptedConn()
    conn.on("FROM booking.b2b_partners WHERE tenant_id", partner)
    conn.on("FROM booking.pricing_rules", B2B_RULE)
    conn.on("FROM booking.fx_rates", lambda cur, on: [r for r in FX if r["currency"] in cur])
    conn.on("FROM booking.settings", "48")
    conn.on(
        "FROM booking.hotel_room_types rt JOIN booking.hotels",
        [{"id": ROOM, "status": "active", "max_occupancy": None, "hotel_status": "active"}],
    )
    conn.on("FROM booking.services WHERE id", [{"id": SERVICE, "status": "active", "service_type": "transfer"}])
    conn.on(
        "FROM booking.supplier_rates x",
        _rates(
            lambda room, service, on, pax: (
                _rate("r-room", "room_night", "1000000.00", "VND") if room else _rate("r-svc", "person", "10.00", "USD")
            )
        ),
    )
    conn.on("INSERT INTO booking.b2b_quotes", "9a000000-0000-4000-8000-000000000001")
    return conn


def _client(conn: ScriptedConn, m2m: LocalM2M, *, m2m_on: bool = True) -> TestClient:
    settings = m2m.settings() if m2m_on else LocalCis().settings()
    app = create_app(
        settings, ScriptedDatabase(conn), LocalCis().verifier(), m2m_verifier=m2m.verifier() if m2m_on else None
    )
    return TestClient(app, raise_server_exceptions=False)


def _headers(token: str, tenant: str | None = TENANT) -> dict[str, str]:
    h = {"Authorization": f"Bearer {token}"}
    if tenant is not None:
        h["X-AA-Tenant-Id"] = tenant
    return h


def _quote_body(**overrides: Any) -> dict[str, Any]:
    body = {
        "proposal_ref": "P-1",
        "proposal_version": 1,
        "currency": "USD",
        "lines": [
            {
                "line_ref": "L1",
                "component_id": f"rt:{ROOM}",
                "service_date": "2027-03-01",
                "nights": 2,
                "pax": {"adult": 2},
            },
            {"line_ref": "L2", "component_id": f"sv:{SERVICE}", "service_date": "2027-03-01", "pax": {"adult": 2}},
        ],
    }
    body.update(overrides)
    return body


def _problem(resp: Any, status: int, error: str) -> None:
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert body["error"] == error and isinstance(body["message"], str)
    assert set(body) <= {"error", "message", "price_hash", "retry_after_s"}


# ── Auth chain ───────────────────────────────────────────────────────────────


def test_quote_prices_lines_and_stores_append_only() -> None:
    m2m, conn = LocalM2M(), _conn()
    resp = _client(conn, m2m).post("/booking/b2b/v1/quotes", json=_quote_body(), headers=_headers(m2m.token()))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert set(body) == {"quote_id", "currency", "fx", "valid_until", "price_hash", "lines", "total_b2b_price"}
    # 2 nights x 1,000,000 VND / 25,000 = 80 USD net; / (1 - 0.15 - 0.03) = 97.5609… → ceil 97.57.
    # 10 USD x 2 pax = 20 net → 24.3902… → 24.40. Total = exact sum.
    assert [line["b2b_price"] for line in body["lines"]] == [
        {"amount": "97.57", "currency": "USD"},
        {"amount": "24.40", "currency": "USD"},
    ]
    assert body["total_b2b_price"] == {"amount": "121.97", "currency": "USD"}
    assert body["fx"] == {
        "base_currency": "USD",
        "quote_currency": "USD",
        "rate_per_usd": "1.00000000",
        "rate_date": TODAY.isoformat(),
        "source": "base",
    }
    assert re.fullmatch(r"[a-f0-9]{64}", body["price_hash"])
    # AA-973: a line without a fixed departure goes on request (AA ops confirm with the supplier).
    assert all(
        line["unit"] == "per_line" and line["availability"] == "on_request" and line["departure_id"] is None
        for line in body["lines"]
    )
    valid_until = datetime.fromisoformat(body["valid_until"])
    assert timedelta(hours=47) < valid_until - datetime.now(UTC) <= timedelta(hours=48)
    # Two room nights looked up separately (season change safe), one double room (default mix for 2), in ONE query.
    room_asks = [a for a in _rate_asks(conn) if a[0]]
    assert [a[2] for a in room_asks] == [date(2027, 3, 1), date(2027, 3, 2)] and {a[3] for a in room_asks} == {2}
    [quote_args] = conn.sql("INSERT INTO booking.b2b_quotes")
    assert quote_args[0] == PARTNER_ID and quote_args[1] == CRM_CLIENT and quote_args[15] == body["price_hash"]
    lines = conn.sql("INSERT INTO booking.b2b_quote_lines")
    assert [(r[2], r[4], r[13], r[14]) for r in lines] == [
        ("L1", "hotel_room", D("97.57"), D("80.0000")),
        ("L2", "transfer", D("24.40"), D("20.0000")),
    ]


def test_same_request_same_hash_and_price_change_new_hash() -> None:
    m2m = LocalM2M()
    first = _client(_conn(), m2m).post("/booking/b2b/v1/quotes", json=_quote_body(), headers=_headers(m2m.token()))
    again = _client(_conn(), m2m).post(
        "/booking/b2b/v1/quotes", json=_quote_body(proposal_ref="P-2"), headers=_headers(m2m.token())
    )
    assert first.json()["price_hash"] == again.json()["price_hash"]
    assert first.json()["quote_id"] == again.json()["quote_id"]  # scripted id; real ids differ
    conn = _conn().on(
        "FROM booking.supplier_rates x", _rates(lambda room, service, on, pax: _rate("r", "person", "11.00", "USD"))
    )
    body = _quote_body(lines=[_quote_body()["lines"][1]])
    changed = _client(conn, m2m).post("/booking/b2b/v1/quotes", json=body, headers=_headers(m2m.token()))
    assert changed.status_code == 201 and changed.json()["price_hash"] != first.json()["price_hash"]


def test_eur_quote_locks_selling_rate() -> None:
    m2m = LocalM2M()
    resp = _client(_conn(), m2m).post(
        "/booking/b2b/v1/quotes", json=_quote_body(currency="EUR"), headers=_headers(m2m.token())
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["fx"]["rate_per_usd"] == "0.91800000" and body["fx"]["source"] == "synthetic"
    # 97.5609756… x 0.918 = 89.5609… → 89.57 ; 24.3902… x 0.918 = 22.3902… → 22.40
    assert [line["b2b_price"]["amount"] for line in body["lines"]] == ["89.57", "22.40"]


@pytest.mark.parametrize(
    ("token_kwargs", "status", "error"),
    [
        ({"scope": "booking.b2b/read"}, 403, "forbidden"),  # missing booking.b2b/quote
        ({"client_id": OTHER_M2M_CLIENT}, 403, "forbidden"),  # wrong client
        ({"exp_in": -600}, 401, "unauthorized"),
        ({"token_use": "id"}, 401, "unauthorized"),
    ],
)
def test_wrong_scope_or_client_is_403_bad_token_401(token_kwargs: dict, status: int, error: str) -> None:
    m2m = LocalM2M()
    resp = _client(_conn(), m2m).post(
        "/booking/b2b/v1/quotes", json=_quote_body(), headers=_headers(m2m.token(**token_kwargs))
    )
    _problem(resp, status, error)


def test_no_token_and_staff_token_are_401() -> None:
    m2m = LocalM2M()
    client = _client(_conn(), m2m)
    _problem(client.get("/booking/b2b/v1/products", headers={"X-AA-Tenant-Id": TENANT}), 401, "unauthorized")
    staff = LocalCis().workforce.token(["booking-admin"])
    _problem(client.get("/booking/b2b/v1/products", headers=_headers(staff)), 401, "unauthorized")


def test_m2m_token_is_refused_on_staff_routes() -> None:
    m2m = LocalM2M()
    resp = _client(_conn(), m2m).get("/booking/b2b-partners", headers=_headers(m2m.token()))
    assert resp.status_code == 401


def test_partner_checks() -> None:
    m2m = LocalM2M()
    token = m2m.token()
    _problem(_client(_conn(None), m2m).get("/booking/b2b/v1/products", headers=_headers(token)), 403, "unknown_partner")
    suspended = {**PARTNER, "status": "suspended"}
    _problem(
        _client(_conn(suspended), m2m).get("/booking/b2b/v1/products", headers=_headers(token)), 403, "unknown_partner"
    )
    other_bound = {**PARTNER, "m2m_client_id": OTHER_M2M_CLIENT}
    _problem(
        _client(_conn(other_bound), m2m).get("/booking/b2b/v1/products", headers=_headers(token)), 403, "forbidden"
    )
    _problem(_client(_conn(), m2m).get("/booking/b2b/v1/products", headers=_headers(token, None)), 422, "invalid")
    _problem(
        _client(_conn(), m2m).get("/booking/b2b/v1/products", headers=_headers(token, "not-a-uuid")), 422, "invalid"
    )


def test_not_configured_is_503() -> None:
    m2m = LocalM2M()
    resp = _client(_conn(), m2m, m2m_on=False).get("/booking/b2b/v1/products", headers=_headers(m2m.token()))
    _problem(resp, 503, "unavailable")


def test_validation_errors_are_problems_without_values() -> None:
    m2m = LocalM2M()
    body = _quote_body(currency="usd", secret_field="x")
    resp = _client(_conn(), m2m).post("/booking/b2b/v1/quotes", json=body, headers=_headers(m2m.token()))
    _problem(resp, 422, "invalid")
    assert "usd" not in resp.json()["message"]


def test_staff_validation_errors_keep_fastapi_shape() -> None:
    m2m = LocalM2M()
    resp = _client(_conn(), m2m).get("/booking/catalog/trip-plans/not-a-uuid")
    assert resp.status_code in (401, 422) and "error" not in resp.json()


# ── Quote refusals ───────────────────────────────────────────────────────────


def test_quote_refusals() -> None:
    m2m = LocalM2M()
    token = m2m.token()
    client = _client(_conn(), m2m)
    lines = _quote_body()["lines"]
    _problem(
        client.post("/booking/b2b/v1/quotes", json=_quote_body(currency="JPY"), headers=_headers(token)), 422, "invalid"
    )
    dup = _quote_body(lines=[lines[0], {**lines[1], "line_ref": "L1"}])
    _problem(client.post("/booking/b2b/v1/quotes", json=dup, headers=_headers(token)), 422, "invalid")
    dep = _quote_body(lines=[{**lines[1], "departure_id": "D-1"}])
    _problem(client.post("/booking/b2b/v1/quotes", json=dep, headers=_headers(token)), 422, "invalid")
    unknown = _quote_body(lines=[{**lines[1], "component_id": "xx:123"}])
    _problem(client.post("/booking/b2b/v1/quotes", json=unknown, headers=_headers(token)), 422, "invalid")
    no_rate = _conn().on("FROM booking.supplier_rates x", None)
    resp = _client(no_rate, m2m).post("/booking/b2b/v1/quotes", json=_quote_body(), headers=_headers(token))
    _problem(resp, 422, "invalid")
    assert "L1" in resp.json()["message"]
    no_rule = _conn().on("FROM booking.pricing_rules", None)
    resp = _client(no_rule, m2m).post("/booking/b2b/v1/quotes", json=_quote_body(), headers=_headers(token))
    _problem(resp, 503, "unavailable")
    no_fx = _conn().on("FROM booking.fx_rates", [])
    resp = _client(no_fx, m2m).post("/booking/b2b/v1/quotes", json=_quote_body(), headers=_headers(token))
    _problem(resp, 503, "unavailable")
    assert "VND" in resp.json()["message"]


def test_trip_plan_line_sums_its_components() -> None:
    m2m = LocalM2M()
    plan = "8e000000-0000-4000-8000-0000000000d1"
    conn = _conn()
    conn.on("FROM booking.trip_plans WHERE id", [{"id": plan, "sellable": True}])
    served_plan(conn, plan, "src-1")  # AA-986 L5: the plan's tour is in catalog.v_tour
    conn.on(
        "FROM booking.trip_plan_components WHERE trip_plan_id",
        [
            {"trip_plan_id": plan, "day_no": 1, "room_type_id": ROOM, "service_id": None},
            {"trip_plan_id": plan, "day_no": 2, "room_type_id": None, "service_id": SERVICE},
        ],
    )
    line = {"line_ref": "T", "component_id": f"tp:{plan}", "service_date": "2027-03-01", "pax": {"adult": 2}}
    resp = _client(conn, m2m).post(
        "/booking/b2b/v1/quotes", json=_quote_body(lines=[line]), headers=_headers(m2m.token())
    )
    assert resp.status_code == 201, resp.text
    # 1 night (40 USD) + transfer 20 USD = 60 net → 73.1707… → 73.18
    assert resp.json()["total_b2b_price"] == {"amount": "73.18", "currency": "USD"}
    assert [a[2] for a in _rate_asks(conn)] == [date(2027, 3, 1), date(2027, 3, 2)]
    [row] = conn.sql("INSERT INTO booking.b2b_quote_lines")
    assert row[4] == "trip_plan"


# ── Reads ────────────────────────────────────────────────────────────────────


def test_products_page() -> None:
    m2m = LocalM2M()
    plan = "8e000000-0000-4000-8000-0000000000d1"
    now = datetime.now(UTC)
    conn = _conn()
    conn.on(
        "WHERE (p.sellable OR $2::timestamptz IS NOT NULL)",
        [
            {
                "trip_plan_id": plan,
                "source_system": "cis",
                "source_id": "src-1",
                "sellable": True,
                "has_fixed_departures": False,
                "updated_at": now,
            }
        ],
    )
    conn.on(
        "WHERE c.trip_plan_id = ANY",
        [
            {"trip_plan_id": plan, "day_no": 1, "room_type_id": ROOM, "service_id": None, "service_type": None,
             "destination_id": "d1", "created_at": now},
        ],
    )  # fmt: skip
    resp = _client(conn, m2m).get("/booking/b2b/v1/products", headers=_headers(m2m.token("booking.b2b/read")))
    assert resp.status_code == 200, resp.text
    [item] = resp.json()["items"]
    assert item["tour"] == {"source_system": "cis", "source_id": "src-1"} and item["has_fixed_departures"] is False
    assert [c["component_id"] for c in item["components"]] == [f"tp:{plan}", f"rt:{ROOM}"]
    assert item["components"][1]["kind"] == "hotel_room" and resp.json()["next_cursor"] is None


def test_products_delta_lists_plans_that_stopped_being_sellable() -> None:
    m2m = LocalM2M()
    gone = "8e000000-0000-4000-8000-0000000000d2"
    now = datetime.now(UTC)
    conn = _conn()
    conn.on(
        "WHERE (p.sellable OR $2::timestamptz IS NOT NULL)",
        [
            {
                "trip_plan_id": gone,
                "source_system": "cis",
                "source_id": "src-2",
                "sellable": False,
                "has_fixed_departures": True,
                "updated_at": now,
            }
        ],
    )
    since = (now - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    resp = _client(conn, m2m).get(
        "/booking/b2b/v1/products", params={"updated_since": since}, headers=_headers(m2m.token("booking.b2b/read"))
    )
    assert resp.status_code == 200, resp.text
    [item] = resp.json()["items"]
    assert item["sellable"] is False and item["components"] == []
    assert item["has_fixed_departures"] is False  # an unsellable plan offers no departure either
    assert conn.sql("WHERE c.trip_plan_id = ANY") == []  # no component read for an unsellable plan
    [args] = conn.sql("WHERE (p.sellable OR $2::timestamptz IS NOT NULL)")
    assert args[1] is not None  # the delta flag that lets unsellable plans through


def test_fx_rates_and_parity() -> None:
    m2m = LocalM2M()
    token = m2m.token("booking.b2b/read")
    conn = _conn()
    conn.on(
        "SELECT DISTINCT ON (f.currency) f.currency::text",
        [{"currency": "VND", "rate_per_usd": D("25000"), "as_of": TODAY, "source": "synthetic"}],
    )
    resp = _client(conn, m2m).get(f"/booking/b2b/v1/fx-rates?date={TODAY.isoformat()}", headers=_headers(token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["rates"] == [
        {"currency": "VND", "rate_per_usd": "25000.00000000", "as_of": TODAY.isoformat(), "source": "synthetic"}
    ]
    conn.on(
        "price_hint_amount, p.price_hint_currency",
        {"price_hint_amount": D("1000.00"), "price_hint_currency": "USD", "price_hint_basis": "per_person"},
    )
    url = "/booking/b2b/v1/price-parity?source_system=cis&source_id=src-1&currency="
    resp = _client(conn, m2m).get(url + "EUR", headers=_headers(token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["from_price"] == {"amount": "918.00", "currency": "EUR"}
    assert resp.json()["basis"] == "per_person_2pax_low_season"
    assert _client(conn, m2m).get(url + "USD", headers=_headers(token)).json()["from_price"]["amount"] == "1000.00"
    assert _client(conn, m2m).get(url + "GBP", headers=_headers(token)).json()["from_price"] is None  # no rate
    group = conn.on(
        "price_hint_amount, p.price_hint_currency",
        {"price_hint_amount": D("1000.00"), "price_hint_currency": "USD", "price_hint_basis": "per_group"},
    )
    assert _client(group, m2m).get(url + "USD", headers=_headers(token)).json()["from_price"] is None
    gone = _conn().on("price_hint_amount, p.price_hint_currency", None)
    _problem(_client(gone, m2m).get(url + "USD", headers=_headers(token)), 404, "not_found")


# ── Contract naming (Fix B19) ────────────────────────────────────────────────

FORBIDDEN_KEY = re.compile(r"(supplier_net|net|cost|margin|card_fee|rate_id)", re.IGNORECASE)


def _walk(node: Any, out: set[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "properties" and isinstance(value, dict):
                out.update(value)
            _walk(value, out)
    elif isinstance(node, list):
        for value in node:
            _walk(value, out)


def test_b2b_responses_never_carry_net_cost_or_margin() -> None:
    """booking.b2b.v1 exposes b2b_price only; no key named supplier_net, net, cost or margin (ADR 0005a d2)."""
    app = create_app(LocalM2M().settings(), ScriptedDatabase(ScriptedConn()), LocalCis().verifier())
    spec = app.openapi()
    b2b_paths = {p: v for p, v in spec["paths"].items() if p.startswith("/booking/b2b/v1/")}
    assert set(b2b_paths) == {
        "/booking/b2b/v1/products",
        "/booking/b2b/v1/quotes",
        "/booking/b2b/v1/fx-rates",
        "/booking/b2b/v1/price-parity",
        "/booking/b2b/v1/departures",
        "/booking/b2b/v1/holds",
        "/booking/b2b/v1/holds/{hold_id}",
        "/booking/b2b/v1/holds/{hold_id}/confirm",
        "/booking/b2b/v1/holds/{hold_id}/extend",
        "/booking/b2b/v1/bookings/{booking_id}/cancel",
        "/booking/b2b/v1/events",
    }
    refs: set[str] = set()
    for op in (op for path in b2b_paths.values() for op in path.values()):
        for code, resp in op["responses"].items():
            if not code.startswith("2"):
                continue
            schema = resp.get("content", {}).get("application/json", {}).get("schema", {})
            if "$ref" in schema:
                refs.add(schema["$ref"].rsplit("/", 1)[1])
    assert refs == {
        "ProductPage",
        "Quote",
        "FxRates",
        "PriceParity",
        "DeparturePage",
        "Hold",
        "Booking",
        "CancellationResult",
        "EventPage",
    }
    keys: set[str] = set()
    pending, seen = list(refs), set()
    schemas = spec["components"]["schemas"]
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        _walk(schemas[name], keys)
        pending += re.findall(r"#/components/schemas/(\w+)", str(schemas[name]))
    assert "b2b_price" in keys and "total_b2b_price" in keys
    assert {k for k in keys if FORBIDDEN_KEY.search(k) and "b2b_price" not in k} == set()


def test_price_hash_is_canonical() -> None:
    rule = repository.DatedRule(
        PricingRule(Channel.B2B, D("0.15"), D("0.10"), D("0.03"), D("0.03")), date(2026, 10, 14)
    )
    fx = quotes.FxLock("USD", D("1"), D("0"), date(2026, 10, 23), "base")
    line = {
        "line_ref": "L1",
        "component_id": f"rt:{ROOM}",
        "service_date": date(2027, 3, 1),
        "nights": 2,
        "pax_adult": 2,
        "pax_child": 0,
        "pax_infant": 0,
        "rooms": [],
        "unit": "per_line",
        "availability": "available",
        "departure_id": None,
        "b2b_price": D("97.57"),
    }
    a = quotes.price_hash(currency="USD", fx=fx, rule=rule, margin=D("0.1500"), lines=[line], total=D("97.57"))
    b = quotes.price_hash(currency="USD", fx=fx, rule=rule, margin=D("0.15"), lines=[dict(line)], total=D("97.57"))
    assert a == b  # 0.1500 and 0.15 hash alike
    c = quotes.price_hash(
        currency="USD", fx=fx, rule=rule, margin=D("0.15"), lines=[{**line, "b2b_price": D("97.58")}], total=D("97.58")
    )
    assert c != a
    d = quotes.price_hash(
        currency="USD", fx=fx, rule=rule, margin=D("0.15"), lines=[{**line, "departure_id": DEP}], total=D("97.57")
    )
    assert d != a  # the departure is part of what was quoted (AA-973)


def _ask(adult: int, child: int = 0, *rooms: RoomAsk) -> LineAsk:
    return LineAsk("L", "rt:x", date(2027, 1, 1), None, adult=adult, child=child, infant=1, rooms=rooms)


def test_room_mix() -> None:
    assert _ask(3).party == 3
    assert components.room_mix(_ask(3), None) == [(2, 1), (1, 1)]  # default: doubles, the odd one in a single
    assert components.room_mix(_ask(1), None) == [(1, 1)]
    assert components.room_mix(_ask(4, 0, RoomAsk("double", 2)), 2) == [(2, 2)]
    assert components.room_mix(_ask(2, 0, RoomAsk("single", 2)), None) == [(1, 2)]
    assert components.room_mix(_ask(2, 0, RoomAsk("twin"), RoomAsk("double")), None) == [(2, 2)]
    assert components.room_mix(_ask(2, 1, RoomAsk("family")), 4) == [(3, 1)]  # 3 travellers share the family room
    assert components.room_mix(_ask(4, 1, RoomAsk("double"), RoomAsk("family")), None) == [(2, 1), (3, 1)]
    with pytest.raises(components.B2BError, match="at most 2"):
        components.room_mix(_ask(3, 0, RoomAsk("triple")), 2)
    with pytest.raises(components.B2BError, match="fewer than 3 adults"):
        components.room_mix(_ask(3, 0, RoomAsk("single")), None)
    with pytest.raises(components.B2BError, match="fewer than 5 adults"):
        components.room_mix(_ask(5, 0, RoomAsk("family")), 4)  # capped at the room type's max occupancy


# ── Review round (AA-972): room mix, bounded work ────────────────────────────

BANDS = {1: "30.00", 2: "40.00"}  # single and double bands of one room type (USD); no band above 2


def _banded_conn() -> ScriptedConn:
    def per_ask(room: Any, service: Any, on: Any, pax: int) -> dict[str, Any] | None:
        if room:
            return _rate(f"r-{pax}", "room_night", BANDS[pax], "USD") if pax in BANDS else None
        return _rate("r-svc", "person", "10.00", "USD")

    return _conn().on("FROM booking.supplier_rates x", _rates(per_ask))


def _room_line(adult: int, rooms: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    line = {"line_ref": "R", "component_id": f"rt:{ROOM}", "service_date": "2027-03-01", "pax": {"adult": adult}}
    return {**line, "rooms": rooms, **extra}


@pytest.mark.parametrize(
    ("adult", "rooms", "net", "paxes"),
    [
        (4, [{"occupancy": "double", "count": 2}], 80, {2}),  # was a spurious 422 (band matched against the party)
        (2, [{"occupancy": "single", "count": 2}], 60, {1}),  # was priced at the double band x 2
        (4, [], 80, {2}),  # default mix: 2 doubles
        (3, [], 70, {1, 2}),  # default mix: 1 double + 1 single
    ],
)
def test_room_lines_price_each_room_by_its_occupancy(adult: int, rooms: list, net: int, paxes: set) -> None:
    m2m, conn = LocalM2M(), _banded_conn()
    body = _quote_body(lines=[_room_line(adult, rooms)])
    resp = _client(conn, m2m).post("/booking/b2b/v1/quotes", json=body, headers=_headers(m2m.token()))
    assert resp.status_code == 201, resp.text
    [line] = conn.sql("INSERT INTO booking.b2b_quote_lines")
    assert line[14] == D(net).quantize(D("0.0001"))
    assert {a[3] for a in _rate_asks(conn)} == paxes


def test_room_type_id_must_match_the_component() -> None:
    m2m = LocalM2M()
    token = m2m.token()
    other = "7d9f8b00-0000-4000-8000-0000000000ff"
    for ref in (f"rt:{ROOM}", ROOM):
        body = _quote_body(lines=[_room_line(2, [{"occupancy": "double", "room_type_id": ref}])])
        assert (
            _client(_banded_conn(), m2m).post("/booking/b2b/v1/quotes", json=body, headers=_headers(token)).status_code
            == 201
        )
    for ref in (f"rt:{other}", "junk"):
        body = _quote_body(lines=[_room_line(2, [{"occupancy": "double", "room_type_id": ref}])])
        resp = _client(_banded_conn(), m2m).post("/booking/b2b/v1/quotes", json=body, headers=_headers(token))
        _problem(resp, 422, "invalid")
        assert "room_type_id" in resp.json()["message"]
    svc = {"line_ref": "S", "component_id": f"sv:{SERVICE}", "service_date": "2027-03-01", "pax": {"adult": 2}}
    body = _quote_body(lines=[{**svc, "rooms": [{"occupancy": "double", "room_type_id": ROOM}]}])
    _problem(
        _client(_banded_conn(), m2m).post("/booking/b2b/v1/quotes", json=body, headers=_headers(token)), 422, "invalid"
    )


def test_room_mix_refusals_name_the_line() -> None:
    m2m = LocalM2M()
    conn = _banded_conn().on(
        "FROM booking.hotel_room_types rt JOIN booking.hotels",
        [{"id": ROOM, "status": "active", "max_occupancy": 2, "hotel_status": "active"}],
    )
    body = _quote_body(lines=[_room_line(3, [{"occupancy": "triple"}])])
    resp = _client(conn, m2m).post("/booking/b2b/v1/quotes", json=body, headers=_headers(m2m.token()))
    _problem(resp, 422, "invalid")
    assert "line R" in resp.json()["message"] and conn.sql("FROM booking.supplier_rates x") == []
    no_band = _quote_body(lines=[_room_line(3, [{"occupancy": "family"}])])  # family of 3, no band for 3
    resp = _client(_banded_conn(), m2m).post("/booking/b2b/v1/quotes", json=no_band, headers=_headers(m2m.token()))
    _problem(resp, 422, "invalid")
    assert "for 3 per room" in resp.json()["message"]


def test_quote_too_large_is_refused_before_any_rate_lookup() -> None:
    m2m, conn = LocalM2M(), _banded_conn()
    lines = [_room_line(2, [], line_ref=f"L{i}", nights=6) for i in range(170)]  # 170 x 6 = 1,020 lookups
    resp = _client(conn, m2m).post(
        "/booking/b2b/v1/quotes", json=_quote_body(lines=lines), headers=_headers(m2m.token())
    )
    _problem(resp, 422, "invalid")
    assert "too large" in resp.json()["message"]
    assert conn.sql("FROM booking.supplier_rates x") == []
    ok = _client(_banded_conn(), m2m).post(
        "/booking/b2b/v1/quotes", json=_quote_body(lines=lines[:166]), headers=_headers(m2m.token())
    )  # 996 lookups
    assert ok.status_code == 201, ok.text


def test_quote_runs_a_fixed_number_of_queries() -> None:
    m2m, conn = LocalM2M(), _banded_conn()
    lines = [_room_line(2, [], line_ref=f"L{i}", nights=5) for i in range(40)]
    resp = _client(conn, m2m).post(
        "/booking/b2b/v1/quotes", json=_quote_body(lines=lines), headers=_headers(m2m.token())
    )
    assert resp.status_code == 201, resp.text
    assert len(conn.sql("FROM booking.supplier_rates x")) == 1
    assert len(conn.sql("FROM booking.hotel_room_types rt JOIN booking.hotels")) == 1
    assert len(_rate_asks(conn)) == 5  # 200 planned lookups, deduplicated to the 5 distinct nights
