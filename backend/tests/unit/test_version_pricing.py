"""Version net cost lines and POST …/versions/{n}/price (AA-810, S236 review finding 3), scripted connection.

Real SQL (bk-004 costs, bk-006 sent lock, the price columns) is covered by tests/integration/test_bk004_pricing.py.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal as D
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.auth.staff import Staff, current_staff, get_pool
from app.errors import Conflict, Invalid, Unavailable
from app.main import create_app
from app.pricing.engine import Channel
from app.settings import Settings
from app.trip_cases import versions
from app.trip_cases.versions import Cost, Item
from tests.unit.fakes import ScriptedConn

STAFF = Staff("u-1", None, "Sales One", "sales", True, "reviewer")
CONTRACTING = Staff("u-5", None, "Contracts", "contracting", True, "reviewer")
CASE_ID = "11111111-1111-4111-8111-111111111111"
VERSION_ID = "22222222-2222-4222-8222-222222222222"
ITEM_A = "33333333-3333-4333-8333-333333333333"
ITEM_B = "44444444-4444-4444-8444-444444444444"
TODAY = date(2026, 10, 20)

RULE = {
    "channel": "b2c_tailor_made",
    "target_margin": D("0.3000"),
    "floor_margin": D("0.2000"),
    "peak_uplift_pts": D("0.0300"),
    "card_fee_pct": D("0.0300"),
    "valid_from": date(2026, 10, 14),
}
EUR = {
    "currency": "EUR",
    "rate_per_usd": D("0.92"),
    "buffer_pct": D("0.02"),
    "as_of": date(2026, 10, 19),
    "source": "ECB",
}


def case_row(status: str = "building") -> dict[str, Any]:
    return {"id": CASE_ID, "code": "TC-000001", "status": status, "currency": "USD", "inquiry_snapshot": None}


def version_row(*, sent: bool = False, total: str | None = None) -> dict[str, Any]:
    return {
        "id": VERSION_ID,
        "version_number": 1,
        "is_approved": False,
        "is_sent_to_customer": sent,
        "title": None,
        "notes": None,
        "total_price": total,
        "currency": "USD",
        "channel": None,
        "badge": "sent" if sent else "draft",
    }


def item_row(item_id: str, position: int, costs: list[tuple[str, str, str | None]] | None = None) -> dict[str, Any]:
    return {
        "id": item_id,
        "position": position,
        "title": f"Item {position}",
        "day_from": 1,
        "day_to": 1,
        "source_tour_id": None,
        "notes": None,
        "costs": json.dumps(
            [
                {"id": f"c-{i}", "net_amount": a, "net_currency": cur, "supplier_note": note}
                for i, (a, cur, note) in enumerate(costs or [])
            ]
        ),
    }


def conn_for(
    items: list[dict[str, Any]], *, case: str = "building", sent: bool = False, fx: Any = None
) -> ScriptedConn:
    return (
        ScriptedConn()
        .on("FROM booking.trip_cases tc", case_row(case))
        .on("FROM booking.trip_case_versions v", version_row(sent=sent))
        .on("SELECT id::text AS id, position, title", items)
        .on("FROM booking.pricing_rules", RULE)
        .on("FROM booking.fx_rates", lambda currencies, on: [r for r in (fx or [EUR]) if r["currency"] in currencies])
    )


async def price(conn: ScriptedConn, **kw: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "channel": Channel.B2C_TAILOR_MADE,
        "currency": "USD",
        "peak": False,
        "override_total": None,
        "override_reason": None,
        "today": TODAY,
        **kw,
    }
    return await versions.price_version(conn, STAFF, "TC-000001", 1, **args)


# ── Price ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_price_usd_saves_total_currency_channel_and_unit_fx() -> None:
    conn = conn_for([item_row(ITEM_A, 1, [("100.00", "USD", "hotel")]), item_row(ITEM_B, 2, [("0.00", "USD", None)])])
    out = await price(conn)
    # 100 / (1 - 0.30 - 0.03) = 149.25 -> rounded up to 150.
    assert out["quote"]["final_total"] == D("150.00") and out["quote"]["below_floor"] is False
    assert conn.sql("SET total_price = $2, currency = $3") == [
        (VERSION_ID, D("150.00"), "USD", "b2c_tailor_made", D(1), TODAY, "u-1")
    ]
    audit = conn.sql("INSERT INTO booking.audit_logs")[0]
    assert audit[3] == "trip_case_version.priced" and json.loads(audit[7])["cost_lines"] == 2


@pytest.mark.asyncio
async def test_price_eur_locks_the_selling_rate() -> None:
    conn = conn_for([item_row(ITEM_A, 1, [("100.00", "USD", None)])])
    out = await price(conn, currency="EUR")
    update = conn.sql("SET total_price = $2, currency = $3")[0]
    assert update[2] == "EUR" and update[4] == D("0.92") and update[5] == date(2026, 10, 19)
    assert [fx["currency"] for fx in out["quote"]["fx_rates"]] == ["EUR"]


@pytest.mark.asyncio
async def test_override_below_floor_needs_a_reason() -> None:
    conn = conn_for([item_row(ITEM_A, 1, [("100.00", "USD", None)])])
    with pytest.raises(Invalid) as no_reason:
        await price(conn, override_total=D("110"))
    assert no_reason.value.code == "override_reason_required"
    assert conn.sql("SET total_price = $2") == []
    await price(conn, override_total=D("110"), override_reason="  repeat client ")
    audit = conn.sql("INSERT INTO booking.audit_logs")[0]
    assert json.loads(audit[7])["override_reason"] == "repeat client"
    assert "override below floor: repeat client" in audit[-1]


@pytest.mark.asyncio
async def test_override_above_floor_keeps_an_optional_reason() -> None:
    conn = conn_for([item_row(ITEM_A, 1, [("100.00", "USD", None)])])
    await price(conn, override_total=D("160"))
    audit = conn.sql("INSERT INTO booking.audit_logs")[0]
    assert audit[-1].endswith("(override)") and json.loads(audit[7])["override_reason"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("items", "code"),
    [
        ([], "version_empty"),
        ([item_row(ITEM_A, 1, [("1", "USD", None)]), item_row(ITEM_B, 2)], "costs_missing"),
        ([item_row(ITEM_A, 1, [("1", "VND", None)])], "fx_rate_missing"),
        ([item_row(ITEM_A, 1, [("99999999999999", "USD", None)])], "price_too_large"),
    ],
)
async def test_price_refusals(items: list[dict[str, Any]], code: str) -> None:
    with pytest.raises(Invalid) as err:
        await price(conn_for(items))
    assert err.value.code == code
    if code == "costs_missing":
        assert err.value.extra["item_ids"] == [ITEM_B]
    if code == "fx_rate_missing":
        assert err.value.extra["currencies"] == ["VND"]


@pytest.mark.asyncio
async def test_price_without_rule_or_with_bad_data() -> None:
    conn = conn_for([item_row(ITEM_A, 1, [("1", "USD", None)])]).on("FROM booking.pricing_rules", None)
    with pytest.raises(Invalid) as no_rule:
        await price(conn)
    assert no_rule.value.code == "pricing_rule_missing"
    broken = conn_for([item_row(ITEM_A, 1, [("1", "USD", None)])]).on("FROM booking.pricing_rules", OSError("db"))
    with pytest.raises(Unavailable):
        await price(broken)
    with pytest.raises(Invalid) as not_selling:
        await price(conn_for([item_row(ITEM_A, 1, [("1", "USD", None)])]), currency="VND")
    assert not_selling.value.code == "pricing_error"


@pytest.mark.asyncio
async def test_price_needs_an_open_case_and_a_draft() -> None:
    items = [item_row(ITEM_A, 1, [("1", "USD", None)])]
    with pytest.raises(Conflict) as closed:
        await price(conn_for(items, case="cancelled"))
    assert closed.value.code == "trip_case_closed"
    with pytest.raises(Conflict) as sent:
        await price(conn_for(items, sent=True))
    assert sent.value.code == "version_sent"


# ── Costs on the item save ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_changed_costs_are_rewritten_and_clear_the_price() -> None:
    conn = (
        conn_for([item_row(ITEM_A, 1, [("100.00", "USD", "hotel")]), item_row(ITEM_B, 2, [("5.00", "USD", None)])])
        .on("FROM booking.trip_case_versions v", version_row(total="150.00"))
        .on("SELECT id::text AS id, position FROM", [{"id": ITEM_A, "position": 1}, {"id": ITEM_B, "position": 2}])
    )
    items = [
        Item("Item 1", 1, 1, id=ITEM_A, costs=(Cost(D("100"), "USD", "hotel"),)),  # same lines: untouched
        Item("Item 2", 1, 1, id=ITEM_B, costs=(Cost(D("7"), "VND", None), Cost(D("0"), "USD", "incl."))),
    ]
    await versions.replace_items(conn, STAFF, "TC-000001", 1, items)
    assert conn.sql("DELETE FROM booking.version_item_costs") == [(ITEM_B,)]
    assert conn.sql("INSERT INTO booking.version_item_costs") == [
        (ITEM_B, D("7"), "VND", None, "u-1", None),
        (ITEM_B, D("0"), "USD", "incl.", "u-1", None),
    ]
    assert conn.sql("total_price = CASE WHEN $3") == [(VERSION_ID, "u-1", True)]
    assert json.loads(conn.sql("INSERT INTO booking.audit_logs")[0][7])["costs_replaced"] == 1


@pytest.mark.asyncio
async def test_same_costs_or_omitted_costs_keep_the_price() -> None:
    conn = conn_for([item_row(ITEM_A, 1, [("100.00", "USD", None)])]).on(
        "FROM booking.trip_case_versions v", version_row(total="150.00")
    )
    await versions.replace_items(
        conn, STAFF, "TC-000001", 1, [Item("Item 1", 1, 1, id=ITEM_A, costs=(Cost(D("100"), "USD"),))]
    )
    await versions.replace_items(conn, STAFF, "TC-000001", 1, [Item("Item 1", 1, 1, id=ITEM_A)])
    assert conn.sql("DELETE FROM booking.version_item_costs") == []
    assert conn.sql("INSERT INTO booking.version_item_costs") == []
    assert conn.sql("total_price = CASE WHEN $3") == [(VERSION_ID, "u-1", False)] * 2


@pytest.mark.asyncio
async def test_new_item_costs_use_the_new_item_id() -> None:
    new_id = "66666666-6666-4666-8666-666666666666"
    conn = conn_for([]).on("SELECT id::text AS id, position FROM", [{"id": new_id, "position": 1}])
    await versions.replace_items(conn, STAFF, "TC-000001", 1, [Item("New", 1, 1, costs=(Cost(D("9"), "THB"),))])
    assert conn.sql("INSERT INTO booking.version_item_costs") == [(new_id, D("9"), "THB", None, "u-1", None)]


@pytest.mark.asyncio
async def test_vn_copies_cost_lines() -> None:
    latest = {"id": "v-2", "version_number": 2, "title": None, "notes": None, "currency": "USD", "channel": None}
    conn = (
        ScriptedConn()
        .on("FROM booking.trip_cases tc", case_row())
        .on("ORDER BY version_number DESC", latest)
        .on("coalesce(max(version_number), 0) + 1", 3)
        .on("INSERT INTO booking.trip_case_versions", VERSION_ID)
        .on("FROM booking.trip_case_versions v", version_row())
    )
    await versions.create_version(conn, STAFF, "TC-000001", None)
    assert conn.sql("INSERT INTO booking.version_item_costs") == [(VERSION_ID, "v-2")]


# ── HTTP wiring ──────────────────────────────────────────────────────────────


def client(monkeypatch: pytest.MonkeyPatch, staff: Staff = STAFF) -> tuple[TestClient, list[Any]]:
    calls: list[Any] = []

    async def fake_price(conn: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(("price", args, kwargs))
        return {"quote": {}, "version": {}}

    async def fake_replace(conn: Any, *args: Any) -> dict[str, Any]:
        calls.append(("items", args))
        return {"items": []}

    monkeypatch.setattr(versions, "price_version", fake_price)
    monkeypatch.setattr(versions, "replace_items", fake_replace)
    app = create_app(Settings(database_url=None, environment="test"))
    app.dependency_overrides[current_staff] = lambda: staff
    app.dependency_overrides[get_pool] = lambda: ScriptedConn()
    return TestClient(app), calls


def test_price_route_maps_body_and_requires_version_write(monkeypatch: pytest.MonkeyPatch) -> None:
    c, calls = client(monkeypatch)
    url = "/booking/trip-cases/TC-000001/versions/1/price"
    r = c.post(url, json={"channel": "b2b", "currency": "EUR", "override_total": "990", "override_reason": "why"})
    assert r.status_code == 200
    kwargs = calls[0][2]
    assert kwargs["channel"] is Channel.B2B and kwargs["override_total"] == D("990") and kwargs["peak"] is False
    for bad in ({}, {"channel": "retail"}, {"channel": "b2b", "override_total": "0"}, {"channel": "b2b", "x": 1}):
        assert c.post(url, json=bad).status_code == 422
    denied, _ = client(monkeypatch, CONTRACTING)
    assert denied.post(url, json={"channel": "b2b"}).status_code == 403


def test_items_route_passes_costs(monkeypatch: pytest.MonkeyPatch) -> None:
    c, calls = client(monkeypatch)
    url = "/booking/trip-cases/TC-000001/versions/1/items"
    body = {
        "items": [
            {"title": "A", "day_from": 1, "day_to": 1, "costs": [{"net_amount": "0", "net_currency": "USD"}]},
            {"title": "B", "day_from": 1, "day_to": 2},
            {"title": "C", "day_from": 2, "day_to": 2, "costs": [{"net_amount": "5", "net_currency": "THB",
                                                                    "supplier_note": "  "}]},
        ]
    }  # fmt: skip
    assert c.put(url, json=body).status_code == 200
    items = calls[0][1][3]
    assert items[0].costs == (Cost(D("0"), "USD", None),) and items[1].costs is None
    assert items[2].costs == (Cost(D("5"), "THB", None),)
    for cost in ({"net_amount": "-1", "net_currency": "USD"}, {"net_amount": "1", "net_currency": "usd"}):
        bad = {"items": [{"title": "A", "day_from": 1, "day_to": 1, "costs": [cost]}]}
        assert c.put(url, json=bad).status_code == 422
    too_many = [{"net_amount": "1", "net_currency": "USD"}] * (versions.MAX_COSTS_PER_ITEM + 1)
    assert (
        c.put(url, json={"items": [{"title": "A", "day_from": 1, "day_to": 1, "costs": too_many}]}).status_code == 422
    )
