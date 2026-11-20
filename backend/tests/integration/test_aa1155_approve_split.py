"""AA-1155 T4.1 (root spec R4.3) over HTTP on a real PostgreSQL, as role ``booking``.

The approvals a write capability carried until now are their own capabilities: ``pricing.approve`` (approve / reject a
pricing rule, FX rate or cancellation policy; approve an agency tier discount or rate card; approve or end a departure
price adjustment), ``b2b_desk.approve`` (approve or end an agency allotment) and ``supplier.activate`` (activate a hotel
or a service). No built-in role lacks them yet (admin and contracting hold all three), so the first test takes
``contracting`` out of the three for its duration: a contracting user is then exactly the later custom maker role, who
makes every row (201) and is refused every decision (403 ``forbidden``), while an admin decides (200). The second test
keeps the built-in matrix: contracting approves an admin's rows (200), sales still cannot approve or end allotments.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio

from app.auth import staff as staff_auth
from tests.integration.conftest import as_booking_owner
from tests.integration.test_aa1073_rest import bk048  # noqa: F401 — fixture used by name
from tests.integration.test_bk020_b2b import (  # noqa: F401 — fixtures admin, client, plan are used by name
    ADMIN,
    DAY1,
    MAKER,
    SALES,
    SOURCE_ID,
    TODAY,
    _ok,
    _staff,
    admin,
    client,
    plan,
)
from tests.staff_tokens import LocalCis, LocalM2M

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

Client = tuple[httpx.AsyncClient, LocalCis, LocalM2M, Any]
OTHER_MAKER = "it-1155-maker-2"  # a second contracting user: never the row's maker, so only the capability refuses it
SPLIT = ("pricing.approve", "b2b_desk.approve", "supplier.activate")
NOTE = "it-1155"
DEPS = f"/catalog/trip-plans/{SOURCE_ID}/departures"
REASON = {"reason": "Invented reason"}


async def _purge(conn: asyncpg.Connection) -> None:
    """Rows this module writes that test_bk020_b2b's clean-up does not delete (it clears departures, partners, FX)."""
    async with as_booking_owner(conn):
        rules = "SELECT id FROM booking.pricing_rules WHERE note = $1"
        await conn.execute(f"DELETE FROM booking.policy_row_closures WHERE pricing_rule_id IN ({rules})", NOTE)  # noqa: S608
        await conn.execute(f"DELETE FROM booking.policy_approvals WHERE pricing_rule_id IN ({rules})", NOTE)  # noqa: S608
        await conn.execute("DELETE FROM booking.pricing_rules WHERE note = $1", NOTE)
        cards = "SELECT id FROM booking.partner_rate_cards WHERE note = $1"
        discounts = "SELECT id FROM booking.partner_tier_discounts WHERE note = $1"
        await conn.execute(
            f"DELETE FROM booking.partner_price_approvals WHERE partner_rate_card_id IN ({cards})"  # noqa: S608
            f" OR partner_tier_discount_id IN ({discounts})",
            NOTE,
        )
        await conn.execute("DELETE FROM booking.partner_rate_cards WHERE note = $1", NOTE)
        await conn.execute("DELETE FROM booking.partner_tier_discounts WHERE note = $1", NOTE)


@pytest_asyncio.fixture
async def split(admin: asyncpg.Connection) -> AsyncIterator[None]:  # noqa: F811
    await admin.execute(
        "INSERT INTO booking.staff_users (cis_user_id, role, active, name) VALUES ($1, 'contracting', true, $1)"
        " ON CONFLICT (cis_user_id) DO UPDATE SET role = EXCLUDED.role, active = true",
        OTHER_MAKER,
    )
    await _purge(admin)
    try:
        yield
    finally:
        await _purge(admin)


@pytest.fixture
def maker_only_contracting(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """``contracting`` keeps every right but the three split approvals (the later custom maker role)."""
    for perm in SPLIT:
        monkeypatch.setitem(staff_auth.PERMISSIONS, perm, staff_auth.PERMISSIONS[perm] - {"contracting"})
    yield


def _refused(response: httpx.Response) -> None:
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "forbidden", response.text


def _day(days: int) -> str:
    return (TODAY + timedelta(days=days)).isoformat()


async def _make_rows(c: httpx.AsyncClient, maker: dict[str, str], plan: dict[str, str], offset: int) -> dict[str, Any]:  # noqa: F811
    """One pending row of every family, made by ``maker`` (each needs only the write capability)."""
    rows: dict[str, Any] = {}
    rule = {"channel": "b2b", "target_margin": "0.16", "floor_margin": "0.10", "note": NOTE}
    for key, day in (("rule", offset + 40), ("rule_reject", offset + 41)):
        rows[key] = await _ok(
            await c.post("/pricing/rules", json={**rule, "valid_from": _day(day)}, headers=maker), 201
        )
    for key, currency in (("fx", "THB"), ("fx_reject", "LAK")):
        fx = {"currency": currency, "rate_per_usd": "35.5", "as_of": _day(offset + 1), "source": NOTE}
        rows[key] = await _ok(await c.post("/pricing/fx-rates", json=fx, headers=maker), 201)
    for key, day in (("cancellation", offset + 5), ("cancellation_reject", offset + 6)):
        policy = {"title": "Invented fee scale", "source_id": SOURCE_ID, "valid_from": _day(day),
                  "tiers": [{"min_days": 0, "fee_pct": "50"}]}  # fmt: skip
        rows[key] = await _ok(await c.post("/pricing/cancellation-policies", json=policy, headers=maker), 201)
    discount = {"tier_code": "silver", "discount_pct": "0.05", "valid_from": _day(offset + 30), "note": NOTE}
    rows["discount"] = await _ok(await c.post("/pricing/partner-tier-discounts", json=discount, headers=maker), 201)
    card = {"partner_id": plan["partner"], "source_id": SOURCE_ID, "discount_pct": "0.1",
            "valid_from": _day(offset + 30), "contract_ref": "Invented contract 1155", "note": NOTE}  # fmt: skip
    rows["card"] = await _ok(await c.post("/pricing/partner-rate-cards", json=card, headers=maker), 201)
    dep = await _ok(await c.post(DEPS, json={"start_date": DAY1, "capacity": 10}, headers=maker), 201)
    allot = {"partner_id": plan["partner"], "seats": 2, "release_days_before": 10}
    rows["allotment"] = await _ok(
        await c.post(f"/catalog/departures/{dep['id']}/allotments", json=allot, headers=maker), 201
    )
    adjustment = {"adjust_pct": "-0.1", "reason": "Invented low season"}
    rows["adjustment"] = await _ok(
        await c.post(f"/b2b-desk/departures/{dep['id']}/price-adjustments", json=adjustment, headers=maker), 201
    )
    return rows


def _decisions(rows: dict[str, Any]) -> list[tuple[str, Any]]:
    """Every approve-class call of this split on ``rows`` (path, body), in an order that keeps each one valid."""
    return [
        (f"/pricing/rules/{rows['rule']['id']}/approve", None),
        (f"/pricing/rules/{rows['rule_reject']['id']}/reject", REASON),
        (f"/pricing/fx-rates/{rows['fx']['id']}/approve", None),
        (f"/pricing/fx-rates/{rows['fx_reject']['id']}/reject", REASON),
        (f"/pricing/cancellation-policies/{rows['cancellation']['id']}/approve", None),
        (f"/pricing/cancellation-policies/{rows['cancellation_reject']['id']}/reject", REASON),
        (f"/pricing/partner-tier-discounts/{rows['discount']['id']}/approve", None),
        (f"/pricing/partner-rate-cards/{rows['card']['id']}/approve", None),
        (f"/catalog/allotments/{rows['allotment']['id']}/approve", None),
        (f"/catalog/allotments/{rows['allotment']['id']}/end", None),
        (f"/b2b-desk/price-adjustments/{rows['adjustment']['id']}/approve", None),
        (f"/b2b-desk/price-adjustments/{rows['adjustment']['id']}/end", None),
    ]


async def _hotel_of(admin: asyncpg.Connection, room: str) -> str:  # noqa: F811
    return await admin.fetchval("SELECT hotel_id::text FROM booking.hotel_room_types WHERE id = $1::uuid", room)


async def test_a_maker_without_the_approvals_is_refused_and_an_admin_decides(
    client: Client,  # noqa: F811
    admin: asyncpg.Connection,  # noqa: F811
    plan: dict[str, str],  # noqa: F811
    bk048: None,  # noqa: F811
    split: None,
    maker_only_contracting: None,
) -> None:
    c, cis, _, _ = client
    maker, other, boss = _staff(cis, MAKER), _staff(cis, OTHER_MAKER), _staff(cis, ADMIN)
    rows = await _make_rows(c, maker, plan, 0)
    # The maker's lists show no Approve / Reject button for the other maker either (the row flags follow the API).
    listed = (await _ok(await c.get("/pricing/rules?channel=b2b", headers=other)))["items"]
    mine = next(r for r in listed if r["id"] == rows["rule"]["id"])
    assert (mine["can_approve"], mine["can_reject"]) == (False, False)

    decisions = _decisions(rows)
    for path, body in decisions:
        _refused(await c.post(path, json=body, headers=other))
    for path, body in decisions:
        await _ok(await c.post(path, json=body, headers=boss))

    # The maker keeps its own writes: withdrawing its own pending row is not an approval.
    spare = {"channel": "b2b", "target_margin": "0.16", "floor_margin": "0.10", "note": NOTE, "valid_from": _day(50)}
    spare_row = await _ok(await c.post("/pricing/rules", json=spare, headers=maker), 201)
    assert (await _ok(await c.post(f"/pricing/rules/{spare_row['id']}/withdraw", headers=maker)))["status"] == (
        "withdrawn"
    )

    # Hotels and services: the maker deactivates (supply.write) but never activates (supplier.activate).
    for kind, ref in (("hotels", await _hotel_of(admin, plan["room"])), ("services", plan["service"])):
        assert (await _ok(await c.post(f"/{kind}/{ref}/deactivate", headers=maker)))["status"] == "inactive"
        _refused(await c.post(f"/{kind}/{ref}/activate", headers=other))
        assert (await _ok(await c.post(f"/{kind}/{ref}/activate", headers=boss)))["status"] == "active"


async def test_built_in_contracting_still_approves_and_sales_does_not_decide_allotments(
    client: Client,  # noqa: F811
    admin: asyncpg.Connection,  # noqa: F811
    plan: dict[str, str],  # noqa: F811
    bk048: None,  # noqa: F811
    split: None,
) -> None:
    c, cis, _, _ = client
    contracting, boss, sales = _staff(cis, MAKER), _staff(cis, ADMIN), _staff(cis, SALES)
    rows = await _make_rows(c, boss, plan, 100)
    allot = rows["allotment"]["id"]
    # Sales proposed allotments before and still may; deciding them is b2b_desk.approve now.
    _refused(await c.post(f"/catalog/allotments/{allot}/approve", headers=sales))
    _refused(await c.post(f"/catalog/allotments/{allot}/end", headers=sales))
    for path, body in _decisions(rows):
        await _ok(await c.post(path, json=body, headers=contracting))
    ended = await _ok(await c.get(f"/catalog/departures/{rows['allotment']['departure_id']}/allotments", headers=sales))
    assert [a["state"] for a in ended["items"]] == ["ended"]

    for kind, ref in (("hotels", await _hotel_of(admin, plan["room"])), ("services", plan["service"])):
        await _ok(await c.post(f"/{kind}/{ref}/deactivate", headers=boss))
        assert (await _ok(await c.post(f"/{kind}/{ref}/activate", headers=contracting)))["status"] == "active"
