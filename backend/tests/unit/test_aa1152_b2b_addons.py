"""AA-1152: ``GET /booking/b2b/v1/add-ons`` (booking.b2b.v1 1.11) with a scripted connection: the rows of
``booking.v_place_addons`` per offer, their places, the agency's B2B price and AA's RRP per traveller, the M2M chain,
and no net, margin or supplier in the answer. Fictional places and services only."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal as D
from typing import Any

from app.b2b import addon_list
from tests.staff_tokens import LocalM2M
from tests.unit.test_b2b_api import B2B_RULE, SERVICE, TODAY, _client, _conn, _headers, _problem

OFFER_A = "a1520000-0000-4000-8000-0000000000a1"
OFFER_B = "a1520000-0000-4000-8000-0000000000b2"
PLACE = "d1520000-0000-4000-8000-0000000000d1"
OTHER_PLACE = "d1520000-0000-4000-8000-0000000000d2"
DAY = TODAY + timedelta(days=3)
URL = "/booking/b2b/v1/add-ons"


def _row(offer: str, title: str, **over: Any) -> dict[str, Any]:
    return {
        "component_id": f"sv:{SERVICE}",
        "offer_id": offer,
        "service_id": SERVICE,
        "service_type": "activity",
        "unit": "person",
        "duration_minutes": 180,
        "max_group_size": 12,
        "country_code": "VN",
        "min_pax": 1,
        "cutoff_hours": 24,
        "operating_weekdays": [1, 2, 3, 4, 5, 6, 7],
        "start_times": ["17:30"],
        "sort_order": 0,
        "featured": False,
        "inventory_kind": "on_request",
        "free_sale_max_pax": None,
        "service_date": DAY,
        "title": title,
        **over,
    }


def _addon_conn(rows: list[dict[str, Any]], rule: dict[str, Any] | None = None) -> Any:
    conn = _conn()
    if rule is not None:
        conn.on("FROM booking.pricing_rules", rule)
    conn.on("SELECT DISTINCT ON (v.offer_id)", rows)
    conn.on(
        "SELECT DISTINCT v.offer_id::text",
        [
            {"offer_id": OFFER_A, "destination_id": PLACE, "place_role": "operates"},
            {"offer_id": OFFER_A, "destination_id": OTHER_PLACE, "place_role": "pickup"},
            {"offer_id": OFFER_B, "destination_id": PLACE, "place_role": "operates"},
        ],
    )
    conn.on(
        "FROM catalog.v_destination d",
        [
            {"destination_id": PLACE, "name": "Lantern Bay", "country": "Vietnam", "country_code": "VN"},
            {"destination_id": OTHER_PLACE, "name": "Harbour Hill", "country": "Vietnam", "country_code": "VN"},
        ],
    )
    return conn


def test_lists_offers_with_places_and_per_person_prices() -> None:
    m2m = LocalM2M()
    rows = [_row(OFFER_A, "Evening lantern walk"), _row(OFFER_B, "Morning rice field ride", featured=True)]
    conn = _addon_conn(rows, rule={**B2B_RULE, "rrp_markup_pct": D("0.25")})
    resp = _client(conn, m2m).get(
        f"{URL}?pax=2&destination_id={PLACE}", headers=_headers(m2m.token("booking.b2b/read"))
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["currency"] == "USD" and body["priced_for_pax"] == 2 and body["truncated"] is False
    # Featured first, then the AA sort order and the title.
    assert [i["title"] for i in body["items"]] == ["Morning rice field ride", "Evening lantern walk"]
    walk = body["items"][1]
    assert walk["component_id"] == f"sv:{SERVICE}" and walk["first_date"] == DAY.isoformat()
    assert walk["places"] == [
        {"destination_id": PLACE, "role": "operates", "name": "Lantern Bay", "country_code": "VN"},
        {"destination_id": OTHER_PLACE, "role": "pickup", "name": "Harbour Hill", "country_code": "VN"},
    ]
    # 2 x 10 USD net at a 15 % margin and a 3 % card fee = 24.40 for the party, 12.20 a traveller.
    assert walk["b2b_price_per_person"] == {"amount": "12.20", "currency": "USD"}
    # RRP: 12.195... x 1.25 = 15.24, up to the policy's USD increment (5) = 20.
    assert walk["rrp_per_person"] == {"amount": "20.00", "currency": "USD"}
    [offers] = conn.sql("SELECT DISTINCT ON (v.offer_id)")
    assert offers == (PLACE, None)


def test_no_rrp_markup_means_no_rrp_and_the_date_filters_the_rows() -> None:
    m2m = LocalM2M()
    conn = _addon_conn([_row(OFFER_A, "Evening lantern walk")])
    resp = _client(conn, m2m).get(f"{URL}?date={DAY.isoformat()}", headers=_headers(m2m.token("booking.b2b/read")))
    assert resp.status_code == 200, resp.text
    [item] = resp.json()["items"]
    assert item["rrp_per_person"] is None and item["b2b_price_per_person"] is not None
    assert resp.json()["date"] == DAY.isoformat()
    [offers] = conn.sql("SELECT DISTINCT ON (v.offer_id)")
    assert offers == (None, DAY)


def test_an_offer_without_a_rate_is_listed_unpriced() -> None:
    m2m = LocalM2M()
    conn = _addon_conn([_row(OFFER_A, "Evening lantern walk")])
    conn.on("FROM booking.supplier_rates x", lambda *args: [])
    resp = _client(conn, m2m).get(URL, headers=_headers(m2m.token("booking.b2b/read")))
    assert resp.status_code == 200, resp.text
    [item] = resp.json()["items"]
    assert item["b2b_price_per_person"] is None and item["rrp_per_person"] is None


def test_refusals() -> None:
    m2m = LocalM2M()
    token = m2m.token("booking.b2b/read")
    conn = _addon_conn([])
    _problem(_client(conn, m2m).get(f"{URL}?currency=XYZ", headers=_headers(token)), 422, "invalid")
    past = (TODAY - timedelta(days=1)).isoformat()
    _problem(_client(conn, m2m).get(f"{URL}?date={past}", headers=_headers(token)), 422, "invalid")
    assert _client(conn, m2m).get(f"{URL}?pax=0", headers=_headers(token)).status_code == 422
    resp = _client(conn, m2m).get(URL, headers=_headers(m2m.token("booking.b2b/quote")))
    assert resp.status_code == 403
    assert _client(conn, m2m).get(URL).status_code == 401


def test_empty_place_answers_no_rows() -> None:
    m2m = LocalM2M()
    resp = _client(_addon_conn([]), m2m).get(
        f"{URL}?destination_id={PLACE}", headers=_headers(m2m.token("booking.b2b/read"))
    )
    assert resp.status_code == 200 and resp.json()["items"] == []


def test_rrp_of_a_currency_the_policy_does_not_round() -> None:
    assert addon_list.rrp_per_person(D("400000"), D("0.25"), "VND", {"USD": D("5")}) == D("500000.00")
    assert addon_list.rrp_per_person(D("400100"), D("0.25"), "VND", {}) == D("501000.00")
    assert addon_list.rrp_per_person(D("10"), None, "USD", {"USD": D("5")}) is None
    assert addon_list.per_person(D("100.01"), 3, "USD") == D("33.34")
