"""AA-1030 (bk-043): agency price tiers and rate cards with a scripted connection — which discount a line gets, the
quote priced with it (stored and hashed; a 0 % partner hashes as before), the margin left under the b2b rule, and the
staff routes (permissions, maker-checker, tier assignment audit).

Real SQL (in force = newest approved row, card validity, grants) is covered by tests/integration/test_aa1030_tiers.py.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.auth.staff import Staff, current_staff
from app.b2b import agency_pricing
from app.b2b.agency_pricing import AgencyTerms, Card
from app.main import create_app
from app.pricing.engine import Channel, PricingRule
from app.settings import Settings
from tests.staff_tokens import LocalM2M
from tests.unit.fakes import ScriptedConn
from tests.unit.test_b2b_api import B2B_RULE, PARTNER_ID, _client, _conn, _headers, _quote_body
from tests.unit.test_pricing_api import ScriptedDatabase

PLAN = "7d9f8b00-0000-4000-8000-0000000000e1"
TERMS_FRAGMENT = "FROM booking.b2b_partners p WHERE p.id"
CARDS_FRAGMENT = "FROM booking.partner_rate_cards c JOIN booking.partner_price_approvals a ON a.partner_rate_card_id"
ADMIN = Staff("11", "admin@example.com", "Admin", "admin", True, "admin")
OTHER_ADMIN = Staff("12", "admin2@example.com", "Admin Two", "admin", True, "admin")
SALES = Staff("13", "sales@example.com", "Sales", "sales", True, "reviewer")


# ── Which discount a line gets ───────────────────────────────────────────────


def test_plan_card_beats_catalogue_card_beats_tier() -> None:
    terms = AgencyTerms(
        tier_code="gold",
        tier_discount=D("0.06"),
        cards=(Card("card-all", None, D("0.04")), Card("card-plan", PLAN, D("0.08"))),
    )
    assert terms.discount_for(PLAN) == (D("0.08"), "card-plan")
    assert terms.discount_for("another-plan") == (D("0.04"), "card-all")
    assert terms.discount_for(None) == (D("0.04"), "card-all")
    no_cards = AgencyTerms(tier_code="gold", tier_discount=D("0.06"))
    assert no_cards.discount_for(PLAN) == (D("0.06"), None)
    plan_only = AgencyTerms(tier_code="silver", tier_discount=D("0.03"), cards=(Card("card-plan", PLAN, D("0")),))
    assert plan_only.discount_for(PLAN) == (D("0"), "card-plan")  # a 0 % card still overrides the tier
    assert plan_only.discount_for(None) == (D("0.03"), None)


def test_end_card_ends_the_contract_rate_of_its_scope() -> None:
    # An open-ended catalogue card, then an approved end card of the same scope (the in-force read returns only the
    # end card for that scope): the partner is back on the tier.
    ended = AgencyTerms(tier_code="gold", tier_discount=D("0.06"), cards=(Card("end-all", None, None),))
    assert ended.discount_for(PLAN) == (D("0.06"), None)
    assert ended.discount_for(None) == (D("0.06"), None)
    assert ended.cards[0].ends
    # A plan card ended while a catalogue card stays: the plan line falls back to the catalogue card.
    plan_ended = AgencyTerms(
        tier_code="gold",
        tier_discount=D("0.06"),
        cards=(Card("card-all", None, D("0.04")), Card("end-plan", PLAN, None)),
    )
    assert plan_ended.discount_for(PLAN) == (D("0.04"), "card-all")
    # The catalogue card ended, a plan card stays: only that plan keeps its rate.
    all_ended = AgencyTerms(
        tier_code="silver",
        tier_discount=D("0.03"),
        cards=(Card("end-all", None, None), Card("card-plan", PLAN, D("0.08"))),
    )
    assert all_ended.discount_for(PLAN) == (D("0.08"), "card-plan")
    assert all_ended.discount_for("another-plan") == (D("0.03"), None)


@pytest.mark.asyncio
async def test_cards_in_force_put_an_end_card_first_on_one_day() -> None:
    conn = ScriptedConn()
    conn.on(TERMS_FRAGMENT, {"tier_code": "gold", "tier_discount": D("0.0600")})
    conn.on(CARDS_FRAGMENT, [{"id": "end-all", "trip_plan_id": None, "discount_pct": None}])
    terms = await agency_pricing.terms_for(conn, PARTNER_ID, date(2026, 11, 5))
    assert terms.discount_for(PLAN) == (D("0.0600"), None)
    assert "(c.discount_pct IS NULL) DESC" in agency_pricing.CARDS_IN_FORCE_SQL


def test_apply_discount_and_margin_left() -> None:
    assert agency_pricing.apply_discount(D("100"), D("0")) == D("100")
    assert agency_pricing.apply_discount(D("100"), D("0.06")) == D("94.00")
    rule = PricingRule(Channel.B2B, target_margin=D("0.15"), floor_margin=D("0.10"), card_fee_pct=D("0.03"))
    # 1 - 0.03 - 0.82 / 0.97 = 0.1246; with 6 %: 1 - 0.03 - 0.82 / 0.94 = 0.0977 (below the 10 % floor).
    assert agency_pricing.margin_after_discount(rule, D("0")) == D("0.1500")
    assert agency_pricing.margin_after_discount(rule, D("0.03")) == D("0.1246")
    assert agency_pricing.margin_after_discount(rule, D("0.06")) == D("0.0977")


@pytest.mark.asyncio
async def test_terms_for_reads_the_tier_and_cards() -> None:
    conn = ScriptedConn()
    conn.on(TERMS_FRAGMENT, {"tier_code": "silver", "tier_discount": D("0.0300")})
    conn.on(CARDS_FRAGMENT, [{"id": "c1", "trip_plan_id": PLAN, "discount_pct": D("0.0500")}])
    terms = await agency_pricing.terms_for(conn, PARTNER_ID, date(2026, 11, 5))
    assert terms == AgencyTerms("silver", D("0.0300"), (Card("c1", PLAN, D("0.0500")),))
    no_approved = ScriptedConn().on(TERMS_FRAGMENT, {"tier_code": "gold", "tier_discount": None})
    assert (await agency_pricing.terms_for(no_approved, PARTNER_ID, date(2026, 11, 5))).tier_discount == D("0")
    assert await agency_pricing.terms_for(ScriptedConn(), PARTNER_ID, date(2026, 11, 5)) == agency_pricing.NO_TERMS


# ── Quotes priced with the tier ──────────────────────────────────────────────


def _quote(conn: ScriptedConn) -> dict[str, Any]:
    m2m = LocalM2M()
    resp = _client(conn, m2m).post("/booking/b2b/v1/quotes", json=_quote_body(), headers=_headers(m2m.token()))
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_tier_discount_lowers_every_line_and_is_stored() -> None:
    standard = _quote(_conn().on(TERMS_FRAGMENT, {"tier_code": "standard", "tier_discount": D("0")}))
    plain = _quote(_conn())  # no tier row read at all (a partner from before bk-043 reads as standard)
    assert standard["price_hash"] == plain["price_hash"]  # a 0 % line hashes as before
    conn = _conn().on(TERMS_FRAGMENT, {"tier_code": "silver", "tier_discount": D("0.0300")})
    silver = _quote(conn)
    # 97.5609… x 0.97 = 94.634… → 94.64; 24.3902… x 0.97 = 23.658… → 23.66 (rounded up like every b2b line).
    assert [line["b2b_price"]["amount"] for line in standard["lines"]] == ["97.57", "24.40"]
    assert [line["b2b_price"]["amount"] for line in silver["lines"]] == ["94.64", "23.66"]
    assert silver["total_b2b_price"]["amount"] == "118.30"
    assert silver["price_hash"] != standard["price_hash"]
    [quote_args] = conn.sql("INSERT INTO booking.b2b_quotes")
    assert quote_args[18:20] == ("silver", D("0.0300"))
    lines = conn.sql("INSERT INTO booking.b2b_quote_lines")
    assert [(r[13], r[17], r[18]) for r in lines] == [(D("94.64"), D("0.0300"), None), (D("23.66"), D("0.0300"), None)]


def test_catalogue_rate_card_overrides_the_tier() -> None:
    conn = _conn().on(TERMS_FRAGMENT, {"tier_code": "gold", "tier_discount": D("0.0600")})
    conn.on(CARDS_FRAGMENT, [{"id": "c-all", "trip_plan_id": None, "discount_pct": D("0.0200")}])
    body = _quote(conn)
    # 97.5609… x 0.98 = 95.609… → 95.61; 24.3902… x 0.98 = 23.902… → 23.91.
    assert [line["b2b_price"]["amount"] for line in body["lines"]] == ["95.61", "23.91"]
    lines = conn.sql("INSERT INTO booking.b2b_quote_lines")
    assert {(r[17], r[18]) for r in lines} == {(D("0.0200"), "c-all")}
    assert "discount" not in str(body)  # the contract body is unchanged: no discount or card field


# ── Staff routes ─────────────────────────────────────────────────────────────


def _staff_client(conn: ScriptedConn, staff: Staff) -> TestClient:
    app = create_app(Settings(database_url=None, environment="test"), ScriptedDatabase(conn))
    app.dependency_overrides[current_staff] = lambda: staff
    return TestClient(app)


def _tier_rows() -> list[dict[str, Any]]:
    return [
        {"code": "standard", "label": "Standard", "sort_order": 10, "created_at": None, "partners": 2,
         "discount_in_force": D("0"), "pending": 0},
        {"code": "gold", "label": "Gold", "sort_order": 30, "created_at": None, "partners": 1,
         "discount_in_force": D("0.0600"), "pending": 1},
    ]  # fmt: skip


def test_list_tiers_shows_margin_left_and_floor_flag() -> None:
    conn = ScriptedConn().on("FROM booking.pricing_rules", B2B_RULE).on("FROM booking.partner_tiers t", _tier_rows())
    body = _staff_client(conn, SALES).get("/booking/pricing/partner-tiers").json()
    assert [(t["code"], t["margin_after_discount"], t["below_floor"]) for t in body["items"]] == [
        ("standard", "0.1500", False),
        ("gold", "0.0977", True),
    ]
    assert body["b2b_rule"]["floor_margin"] == "0.1000"


def test_writes_need_pricing_write() -> None:
    conn = ScriptedConn()
    client = _staff_client(conn, SALES)
    body = {"tier_code": "gold", "discount_pct": "0.05", "valid_from": "2027-01-01"}
    assert client.post("/booking/pricing/partner-tier-discounts", json=body).status_code == 403
    assert client.patch(f"/booking/pricing/partner-terms/{PARTNER_ID}", json={"tier_code": "gold"}).status_code == 403
    assert not conn.sql("INSERT INTO")


def test_discount_validation() -> None:
    client = _staff_client(ScriptedConn(), ADMIN)
    too_much = {"tier_code": "gold", "discount_pct": "0.5", "valid_from": "2027-01-01"}
    assert client.post("/booking/pricing/partner-tier-discounts", json=too_much).status_code == 422
    bad_code = {"tier_code": "Gold!", "discount_pct": "0.05", "valid_from": "2027-01-01"}
    assert client.post("/booking/pricing/partner-tier-discounts", json=bad_code).status_code == 422


def test_maker_cannot_approve_own_discount() -> None:
    conn = ScriptedConn().on(
        "FROM booking.partner_tier_discounts p LEFT JOIN booking.partner_price_approvals a",
        {"created_by": ADMIN.cis_user_id, "approval_id": None},
    )
    resp = _staff_client(conn, ADMIN).post(f"/booking/pricing/partner-tier-discounts/{PLAN}/approve")
    assert resp.status_code == 403 and resp.json()["detail"] == "own_row"
    assert not conn.sql("INSERT INTO booking.partner_price_approvals")
    approved = ScriptedConn().on(
        "FROM booking.partner_tier_discounts p LEFT JOIN booking.partner_price_approvals a",
        {"created_by": ADMIN.cis_user_id, "approval_id": "x"},
    )
    resp = _staff_client(approved, OTHER_ADMIN).post(f"/booking/pricing/partner-tier-discounts/{PLAN}/approve")
    assert resp.status_code == 409 and resp.json()["detail"] == "already_approved"


def test_rate_card_validity_checked_before_insert() -> None:
    conn = ScriptedConn().on("SELECT agency_name FROM booking.b2b_partners", {"agency_name": "Invented Agency"})
    body = {"partner_id": PARTNER_ID, "discount_pct": "0.04", "valid_from": "2027-02-01", "valid_to": "2027-01-01"}
    resp = _staff_client(conn, ADMIN).post("/booking/pricing/partner-rate-cards", json=body)
    assert resp.status_code == 422 and resp.json()["detail"] == "invalid_validity"
    assert not conn.sql("INSERT INTO booking.partner_rate_cards")


def test_set_partner_tier_is_audited() -> None:
    terms_row = {
        "id": PARTNER_ID, "agency_name": "Invented Agency", "status": "active", "billing_currency": "USD",
        "tier_code": "gold", "tier_label": "Gold", "projected": True, "tier_discount": D("0.0600"),
        "card_discount": None, "plan_cards": 0,
    }  # fmt: skip
    conn = ScriptedConn()
    conn.on(
        "FROM booking.b2b_partners WHERE id = $1::uuid FOR UPDATE", {"agency_name": "Invented", "tier_code": "standard"}
    )
    conn.on("FROM booking.partner_tiers WHERE code", {"code": "gold", "label": "Gold", "sort_order": 30})
    conn.on("FROM booking.b2b_partners p JOIN booking.partner_tiers t", terms_row)
    resp = _staff_client(conn, ADMIN).patch(f"/booking/pricing/partner-terms/{PARTNER_ID}", json={"tier_code": "gold"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["effective_discount"] == "0.0600"
    [update] = conn.sql("UPDATE booking.b2b_partners SET tier_code")
    assert update == (PARTNER_ID, "gold", ADMIN.cis_user_id)
    [audit_args] = conn.sql("INSERT INTO booking.audit_logs")
    assert "b2b_partner.tier_changed" in audit_args
    unknown = ScriptedConn().on("FOR UPDATE", {"agency_name": "Invented", "tier_code": "standard"})
    resp = _staff_client(unknown, ADMIN).patch(
        f"/booking/pricing/partner-terms/{PARTNER_ID}", json={"tier_code": "zinc"}
    )
    assert resp.status_code == 404 and resp.json()["detail"] == "tier_not_found"


def _discount_row(**over: Any) -> dict[str, Any]:
    row = {
        "id": PLAN, "tier_code": "gold", "tier_label": "Gold", "discount_pct": D("0.0500"),
        "valid_from": date(2027, 1, 1), "note": "Invented review", "created_at": None, "created_by": ADMIN.cis_user_id,
        "created_by_name": "Admin", "approved_by": None, "approved_by_name": None, "approved_at": None,
        "status": "pending",
    }  # fmt: skip
    return {**row, **over}


def _card_row(**over: Any) -> dict[str, Any]:
    row = {
        "id": PLAN, "partner_id": PARTNER_ID, "agency_name": "Invented Agency", "trip_plan_id": None,
        "source_id": None, "discount_pct": D("0.0400"), "valid_from": date(2027, 1, 1), "valid_to": None,
        "contract_ref": "Invented 1", "note": None, "created_at": None, "created_by": ADMIN.cis_user_id,
        "created_by_name": "Admin", "approved_by": None, "approved_by_name": None, "approved_at": None,
        "status": "pending",
    }  # fmt: skip
    return {**row, **over}


def test_create_and_approve_tier_discount() -> None:
    conn = ScriptedConn().on(
        "FROM booking.partner_tiers WHERE code", {"code": "gold", "label": "Gold", "sort_order": 3}
    )
    conn.on("INSERT INTO booking.partner_tier_discounts", PLAN)
    conn.on("FROM booking.partner_tier_discounts p JOIN booking.partner_tiers t", _discount_row())
    body = {"tier_code": "gold", "discount_pct": "0.05", "valid_from": "2027-01-01", "note": " Invented review "}
    resp = _staff_client(conn, ADMIN).post("/booking/pricing/partner-tier-discounts", json=body)
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "pending" and resp.json()["can_approve"] is False
    [insert] = conn.sql("INSERT INTO booking.partner_tier_discounts")
    assert insert == ("gold", D("0.05"), date(2027, 1, 1), "Invented review", ADMIN.cis_user_id)
    approving = ScriptedConn().on(
        "FROM booking.partner_tier_discounts p LEFT JOIN booking.partner_price_approvals a",
        {"created_by": ADMIN.cis_user_id, "approval_id": None},
    )
    approving.on(
        "FROM booking.partner_tier_discounts p JOIN booking.partner_tiers t", _discount_row(status="scheduled")
    )
    resp = _staff_client(approving, OTHER_ADMIN).post(f"/booking/pricing/partner-tier-discounts/{PLAN}/approve")
    assert resp.status_code == 200 and resp.json()["status"] == "scheduled"
    [approval] = approving.sql("INSERT INTO booking.partner_price_approvals")
    assert approval == (PLAN, OTHER_ADMIN.cis_user_id)
    listed = ScriptedConn().on("FROM booking.partner_tier_discounts p JOIN", [_discount_row()])
    body = _staff_client(listed, OTHER_ADMIN).get("/booking/pricing/partner-tier-discounts").json()
    assert body["items"][0]["can_approve"] is True


def test_create_rate_card_for_one_tour_and_approve() -> None:
    conn = ScriptedConn().on("SELECT agency_name FROM booking.b2b_partners", {"agency_name": "Invented Agency"})
    conn.on("FROM booking.trip_plans WHERE source_system = 'cis'", PLAN)
    conn.on("INSERT INTO booking.partner_rate_cards", PLAN)
    conn.on("FROM booking.partner_rate_cards p JOIN booking.b2b_partners", _card_row(source_id="tour-1"))
    body = {"partner_id": PARTNER_ID, "source_id": "tour-1", "discount_pct": "0.04", "valid_from": "2027-01-01"}
    resp = _staff_client(conn, ADMIN).post("/booking/pricing/partner-rate-cards", json=body)
    assert resp.status_code == 201, resp.text
    [insert] = conn.sql("INSERT INTO booking.partner_rate_cards")
    assert insert[:3] == (PARTNER_ID, PLAN, D("0.04"))
    unknown_tour = ScriptedConn().on("SELECT agency_name FROM booking.b2b_partners", {"agency_name": "X"})
    resp = _staff_client(unknown_tour, ADMIN).post("/booking/pricing/partner-rate-cards", json=body)
    assert resp.status_code == 404 and resp.json()["detail"] == "trip_plan_not_found"
    approving = ScriptedConn().on(
        "FROM booking.partner_rate_cards p LEFT JOIN booking.partner_price_approvals a",
        {"created_by": ADMIN.cis_user_id, "approval_id": None},
    )
    approving.on("FROM booking.partner_rate_cards p JOIN booking.b2b_partners", _card_row(status="in_force"))
    resp = _staff_client(approving, OTHER_ADMIN).post(f"/booking/pricing/partner-rate-cards/{PLAN}/approve")
    assert resp.status_code == 200 and resp.json()["status"] == "in_force"
    listed = ScriptedConn().on("FROM booking.partner_rate_cards p JOIN", [_card_row(status="expired")])
    assert _staff_client(listed, SALES).get("/booking/pricing/partner-rate-cards").json()["items"][0]["status"] == (
        "expired"
    )


def test_end_card_is_created_pending_with_no_discount() -> None:
    conn = ScriptedConn().on("SELECT agency_name FROM booking.b2b_partners", {"agency_name": "Invented Agency"})
    conn.on("INSERT INTO booking.partner_rate_cards", PLAN)
    conn.on("FROM booking.partner_rate_cards p JOIN booking.b2b_partners", _card_row(discount_pct=None))
    body = {"partner_id": PARTNER_ID, "discount_pct": None, "valid_from": "2027-01-01", "contract_ref": "Invented 1"}
    resp = _staff_client(conn, ADMIN).post("/booking/pricing/partner-rate-cards", json=body)
    assert resp.status_code == 201, resp.text
    assert resp.json()["ends_contract_rate"] is True and resp.json()["discount_pct"] is None
    [insert] = conn.sql("INSERT INTO booking.partner_rate_cards")
    assert insert[:3] == (PARTNER_ID, None, None)
    [audit_args] = conn.sql("INSERT INTO booking.audit_logs")
    assert any("ends the contract rate" in str(a) for a in audit_args)
    missing = {"partner_id": PARTNER_ID, "valid_from": "2027-01-01"}  # discount_pct must be sent (null = end)
    assert _staff_client(ScriptedConn(), ADMIN).post(
        "/booking/pricing/partner-rate-cards", json=missing
    ).status_code == (422)


def test_tier_create_and_relabel() -> None:
    conn = ScriptedConn().on("FROM booking.pricing_rules", B2B_RULE).on("FROM booking.partner_tiers t", _tier_rows())
    resp = _staff_client(conn, ADMIN).post(
        "/booking/pricing/partner-tiers", json={"code": "gold", "label": "Gold", "sort_order": 30}
    )
    assert resp.status_code == 201 and resp.json()["code"] == "gold"
    conn.on("FROM booking.partner_tiers WHERE code", {"code": "gold", "label": "Gold", "sort_order": 30})
    resp = _staff_client(conn, ADMIN).patch("/booking/pricing/partner-tiers/gold", json={"label": "Gold plus"})
    assert resp.status_code == 200
    [update] = conn.sql("UPDATE booking.partner_tiers SET label")
    assert update == ("gold", "Gold plus")
    assert _staff_client(conn, ADMIN).patch("/booking/pricing/partner-tiers/Gold", json={}).status_code == 422
    terms = ScriptedConn().on(
        "FROM booking.b2b_partners p JOIN booking.partner_tiers t",
        [{"id": PARTNER_ID, "agency_name": "Invented", "status": "active", "billing_currency": "USD",
          "tier_code": "silver", "tier_label": "Silver", "projected": False, "tier_discount": None,
          "card_discount": D("0.0450"), "plan_cards": 2}],
    )  # fmt: skip
    [item] = _staff_client(terms, SALES).get("/booking/pricing/partner-terms").json()["items"]
    assert (item["tier_discount"], item["effective_discount"], item["plan_cards"]) == ("0", "0.0450", 2)
