"""AA-1030 (bk-045) end to end on a real PostgreSQL, as role ``booking``: two invented partners on different tiers get
different B2B prices for the same tour (root ADR 0019 b15 starting discounts, approved by the bk-045 seed), a contract
rate card overrides the tier only once a second person approved it, a new tier discount is pending until approved,
and the staff reads show the tier, the discount and the margin left. Schema checks: ``test_bk045_schema.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal as D
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio

from tests.integration.test_bk019_b2b import (  # noqa: F401 — fixtures used by name
    ADMIN,
    CHECKER,
    MAKER,
    SALES,
    SOURCE_ID,
    _ok,
    _staff,
    admin,
    client,
    supply,
)
from tests.staff_tokens import CRM_CLIENT, LocalCis, LocalM2M

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

TENANT_SILVER = "e2000000-0000-4000-8000-000000103001"
TENANT_GOLD = "e2000000-0000-4000-8000-000000103002"
TODAY = datetime.now(UTC).date()


async def _purge(conn: asyncpg.Connection) -> None:
    """Rows this module writes that test_bk019_b2b's clean-up does not know (it deletes partners and quotes)."""
    await conn.execute("DELETE FROM booking.b2b_quote_lines")
    await conn.execute("DELETE FROM booking.b2b_quotes")
    await conn.execute(
        "DELETE FROM booking.partner_price_approvals WHERE partner_rate_card_id IS NOT NULL"
        " OR partner_tier_discount_id IN (SELECT id FROM booking.partner_tier_discounts WHERE created_by IS NOT NULL)"
    )
    await conn.execute("DELETE FROM booking.partner_rate_cards")
    await conn.execute("DELETE FROM booking.partner_tier_discounts WHERE created_by IS NOT NULL")
    await conn.execute("DELETE FROM booking.audit_logs WHERE entity_type LIKE 'partner_%'")


@pytest_asyncio.fixture
async def tiers_clean(admin: asyncpg.Connection) -> AsyncIterator[None]:  # noqa: F811
    await _purge(admin)
    try:
        yield
    finally:
        await _purge(admin)


def _m2m(m2m: LocalM2M, tenant: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {m2m.token()}", "X-AA-Tenant-Id": tenant}


async def _quote(c: httpx.AsyncClient, m2m: LocalM2M, tenant: str, lines: list[dict[str, Any]]) -> dict[str, Any]:
    request = {"proposal_ref": f"PRO-{tenant[-4:]}", "proposal_version": 1, "currency": "USD", "lines": lines}
    return await _ok(await c.post("/b2b/v1/quotes", json=request, headers=_m2m(m2m, tenant)), 201)


def _prices(quote: dict[str, Any]) -> list[str]:
    return [line["b2b_price"]["amount"] for line in quote["lines"]]


async def test_two_partners_on_different_tiers_get_different_prices(
    client: tuple[httpx.AsyncClient, LocalCis, LocalM2M],  # noqa: F811
    admin: asyncpg.Connection,  # noqa: F811
    supply: dict[str, str],  # noqa: F811
    tiers_clean: None,
) -> None:
    c, cis, m2m = client
    boss, sales, maker, checker = _staff(cis, ADMIN), _staff(cis, SALES), _staff(cis, MAKER), _staff(cis, CHECKER)

    # Two invented agencies, same CRM client, both on standard first.
    ids = {}
    for tenant, name in ((TENANT_SILVER, "Invented Lotus Travel"), (TENANT_GOLD, "Invented Heron Journeys")):
        body = {"tenant_id": tenant, "agency_name": name, "m2m_client_id": CRM_CLIENT}
        ids[tenant] = (await _ok(await c.post("/b2b-partners", json=body, headers=boss), 201))["id"]
    terms = await _ok(await c.get("/pricing/partner-terms", headers=sales))
    assert {t["tier_code"] for t in terms["items"]} == {"standard"}

    commercial = {"sellable": True, "price_hint": {"amount": "150.00", "currency": "USD", "basis": "per_person"}}
    await _ok(await c.put(f"/catalog/trip-plans/{SOURCE_ID}/commercial", json=commercial, headers=maker))
    items = {
        "items": [{"day_no": 1, "hotel_room_type_id": supply["room"]}, {"day_no": 1, "service_id": supply["service"]}]
    }
    plan_id = (await _ok(await c.put(f"/catalog/trip-plans/{SOURCE_ID}/components", json=items, headers=maker)))[
        "trip_plan_id"
    ]
    lines = [
        {"line_ref": "plan", "component_id": f"tp:{plan_id}", "service_date": "2027-03-01", "pax": {"adult": 2}},
        {"line_ref": "car", "component_id": f"sv:{supply['service']}", "service_date": "2027-03-01",
         "pax": {"adult": 2}},
    ]  # fmt: skip
    # Standard: plan 60 USD net / 0.82 = 73.1707… → 73.18; car 20 USD → 24.40 (test_bk019_b2b.py numbers).
    standard = await _quote(c, m2m, TENANT_SILVER, lines)
    assert _prices(standard) == ["73.18", "24.40"]

    # Assign the tiers: pricing.write only, audited.
    url = "/pricing/partner-terms/{}"
    assert (await c.patch(url.format(ids[TENANT_GOLD]), json={"tier_code": "gold"}, headers=sales)).status_code == 403
    gold_terms = await _ok(await c.patch(url.format(ids[TENANT_GOLD]), json={"tier_code": "gold"}, headers=boss))
    assert gold_terms["tier_code"] == "gold" and D(gold_terms["effective_discount"]) == D("0.06")
    await _ok(await c.patch(url.format(ids[TENANT_SILVER]), json={"tier_code": "silver"}, headers=boss))
    missing = await c.patch(url.format(ids[TENANT_SILVER]), json={"tier_code": "platinum"}, headers=boss)
    assert missing.status_code == 404

    # The same tour, the same day: silver 3 % off, gold 6 % off (rounded up to the cent).
    silver = await _quote(c, m2m, TENANT_SILVER, lines)
    gold = await _quote(c, m2m, TENANT_GOLD, lines)
    assert _prices(silver) == ["70.98", "23.66"]  # 73.1707 x 0.97 = 70.976; 24.3902 x 0.97 = 23.658
    assert _prices(gold) == ["68.79", "22.93"]  # x 0.94 = 68.780; 22.926
    assert len({standard["price_hash"], silver["price_hash"], gold["price_hash"]}) == 3
    stored = await admin.fetchrow(
        "SELECT partner_tier_code, tier_discount_pct FROM booking.b2b_quotes WHERE id = $1::uuid", gold["quote_id"]
    )
    assert (stored["partner_tier_code"], stored["tier_discount_pct"]) == ("gold", D("0.0600"))

    # A contract rate card for the gold agency on this tour: pending until a second person approves it.
    card_body = {"partner_id": ids[TENANT_GOLD], "source_id": SOURCE_ID, "discount_pct": "0.1", "valid_from":
                 TODAY.isoformat(), "valid_to": "2027-12-31", "contract_ref": "Invented contract 12"}  # fmt: skip
    assert (await c.post("/pricing/partner-rate-cards", json=card_body, headers=sales)).status_code == 403
    card = await _ok(await c.post("/pricing/partner-rate-cards", json=card_body, headers=maker), 201)
    assert card["status"] == "pending" and card["can_approve"] is False
    assert _prices(await _quote(c, m2m, TENANT_GOLD, lines)) == ["68.79", "22.93"]  # pending changes nothing
    own = await c.post(f"/pricing/partner-rate-cards/{card['id']}/approve", headers=maker)
    assert own.status_code == 403 and own.json()["detail"] == "own_row"
    approved = await _ok(await c.post(f"/pricing/partner-rate-cards/{card['id']}/approve", headers=checker))
    assert approved["status"] == "in_force"
    carded = await _quote(c, m2m, TENANT_GOLD, lines)
    assert _prices(carded) == ["65.86", "22.93"]  # the plan line takes the card (x 0.9), the car keeps the tier
    line_rows = await admin.fetch(
        "SELECT line_ref, agency_discount_pct, rate_card_id::text AS card FROM booking.b2b_quote_lines"
        " WHERE quote_id = $1::uuid ORDER BY line_no",
        carded["quote_id"],
    )
    assert [(r["line_ref"], r["agency_discount_pct"], r["card"]) for r in line_rows] == [
        ("plan", D("0.1000"), card["id"]),
        ("car", D("0.0600"), None),
    ]
    assert _prices(await _quote(c, m2m, TENANT_SILVER, lines)) == ["70.98", "23.66"]  # another agency is untouched

    # The card is open-ended in effect until an end card is approved: from the same day, the agency is back on its
    # tier (an end card beats a discount card of its scope on one day). Pending first, maker-checker as for cards.
    end_body = {"partner_id": ids[TENANT_GOLD], "source_id": SOURCE_ID, "discount_pct": None,
                "valid_from": TODAY.isoformat(), "contract_ref": "Invented contract 12"}  # fmt: skip
    end = await _ok(await c.post("/pricing/partner-rate-cards", json=end_body, headers=maker), 201)
    assert end["ends_contract_rate"] is True and end["status"] == "pending"
    assert _prices(await _quote(c, m2m, TENANT_GOLD, lines)) == ["65.86", "22.93"]  # pending changes nothing
    ended = await _ok(await c.post(f"/pricing/partner-rate-cards/{end['id']}/approve", headers=checker))
    assert ended["status"] == "in_force"
    back = await _quote(c, m2m, TENANT_GOLD, lines)
    assert _prices(back) == ["68.79", "22.93"]  # the gold tier again
    assert (
        await admin.fetchval(
            "SELECT count(*) FROM booking.b2b_quote_lines WHERE quote_id = $1::uuid AND rate_card_id IS NOT NULL",
            back["quote_id"],
        )
        == 0
    )
    cards = await _ok(await c.get(f"/pricing/partner-rate-cards?partner_id={ids[TENANT_GOLD]}", headers=sales))
    assert {r["id"]: r["status"] for r in cards["items"]} == {end["id"]: "in_force", card["id"]: "superseded"}

    # A new silver discount: pending, then approved by a second person, then in force.
    new = {"tier_code": "silver", "discount_pct": "0.05", "valid_from": TODAY.isoformat(), "note": "Invented review"}
    row = await _ok(await c.post("/pricing/partner-tier-discounts", json=new, headers=maker), 201)
    assert row["status"] == "pending"
    assert _prices(await _quote(c, m2m, TENANT_SILVER, lines)) == ["70.98", "23.66"]
    await _ok(await c.post(f"/pricing/partner-tier-discounts/{row['id']}/approve", headers=checker))
    assert _prices(await _quote(c, m2m, TENANT_SILVER, lines)) == ["69.52", "23.18"]  # x 0.95

    tiers = await _ok(await c.get("/pricing/partner-tiers", headers=sales))
    by_code = {t["code"]: t for t in tiers["items"]}
    assert D(by_code["silver"]["discount_in_force"]) == D("0.05") and by_code["gold"]["partners"] == 1
    assert all(isinstance(t["below_floor"], bool) for t in tiers["items"])
    history = await _ok(await c.get("/pricing/partner-tier-discounts?tier_code=silver", headers=sales))
    assert [r["status"] for r in history["items"]] == ["in_force", "superseded"]
    actions = {
        r["action"]
        for r in await admin.fetch(
            "SELECT action FROM booking.audit_logs WHERE entity_type IN ('b2b_partner', 'partner_rate_card',"
            " 'partner_tier_discount')"
        )
    }
    assert {"b2b_partner.tier_changed", "partner_rate_card.created", "partner_rate_card.approved",
            "partner_tier_discount.created", "partner_tier_discount.approved"} <= actions  # fmt: skip
