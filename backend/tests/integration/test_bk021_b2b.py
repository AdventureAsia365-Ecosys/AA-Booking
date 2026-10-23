"""AA-974 (bk-021): the B2B commercial rules end to end as role ``booking`` on a real PostgreSQL (schema checks:
``test_bk021_schema.py``). Reuses the AA-973 fixtures (a sellable plan priced in VND, an active partner).

- Linear E: a VND quote uses the approved rate in force on the pricing day (not an older, a pending or a future row),
  lines rounded up to 1,000 VND;
- Linear R: a second extend → 409 ``extend_limit`` (a retry with the same key → the hold);
- Linear R: a 4th re-hold of a proposal, or of a departure under fresh proposal refs, in a day → 429 ``rehold_limit``;
- Linear E: cancel inside the fee window returns the scheduled fee — dry run writes nothing, the cancel frees the seats
  and cancels the trip case; the policy is the one locked at confirm (a booking without one never takes a policy
  approved after its confirm); staff fee policies with maker-checker; the same end to end in VND.

Dates are relative to today (lessons.md S245): departures start in 30 / 37 days (bk-020 module constants).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio

from app.b2b import cancellation
from app.db import Database
from tests.integration.test_bk020_b2b import (  # noqa: F401 — fixtures admin, client, plan are used by name
    CHECKER,
    DAY1,
    DAY1_DATE,
    DAY2,
    MAKER,
    SOURCE_ID,
    TENANT,
    TODAY,
    _departure,
    _hold_body,
    _insert,
    _m2m,
    _ok,
    _plan_line,
    _problem,
    _quote,
    _seats,
    _staff,
    admin,
    client,
    plan,
)
from tests.staff_tokens import CRM_CLIENT, LocalCis, LocalM2M

FIXTURES = (admin, client, plan)  # the AA-973 fixtures, used by name below

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[3] / "migrations" / "021_b2b_commercial_rules.sql"
SEED = "00000000-0000-4000-8000-0000000bc021"
Client = tuple[httpx.AsyncClient, LocalCis, LocalM2M, Database]
UTC_TODAY = datetime.now(UTC).date()
# Days before departure are counted from the UTC day; DAY1 / DAY2 are 30 / 37 local days ahead.
DAYS1, DAYS2 = (DAY1_DATE - UTC_TODAY).days, (DAY1_DATE + timedelta(days=7) - UTC_TODAY).days
SEED_PCT = cancellation.fee_pct_for(
    cancellation.parse_tiers(
        [
            {"min_days": 60, "fee_pct": 0},
            {"min_days": 30, "fee_pct": 25},
            {"min_days": 15, "fee_pct": 50},
            {"min_days": 7, "fee_pct": 75},
            {"min_days": 0, "fee_pct": 100},
        ]
    ),
    DAYS1,
)


@pytest_asyncio.fixture
async def rules(admin: asyncpg.Connection, plan: dict[str, str]) -> AsyncIterator[dict[str, str]]:
    """bk-021 again (other modules may have emptied policy_approvals or re-run bk-007), then clean up our rows before
    the bk-020 fixture deletes holds, cases and the plan."""
    await admin.execute(MIGRATION.read_text(encoding="utf-8"))
    try:
        yield plan
    finally:
        async with admin.transaction():
            owner = await admin.fetchval("SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = 'booking'")
            await admin.execute(f'SET LOCAL ROLE "{owner}"')
            await admin.execute("DELETE FROM booking.b2b_cancellations")
            await admin.execute("UPDATE booking.b2b_holds SET cancellation_policy_id = NULL")
            await admin.execute("DELETE FROM booking.cancellation_policies WHERE id <> $1::uuid", SEED)


async def _approved_fx(admin: asyncpg.Connection, rate: int, as_of: Any, *, approve: bool = True) -> None:
    fx = await _insert(
        admin, "fx_rates", currency="VND", rate_per_usd=rate, buffer_pct=0, as_of=as_of, source="synthetic",
        created_by=MAKER,
    )  # fmt: skip
    if approve:
        await admin.execute(
            "INSERT INTO booking.policy_approvals (entity_type, fx_rate_id, approved_by)"
            " VALUES ('fx_rate', $1::uuid, $2)",
            fx,
            CHECKER,
        )


async def test_vnd_quote_uses_the_rate_in_force_on_the_pricing_day(
    client: Client, admin: asyncpg.Connection, rules: dict[str, str]
) -> None:
    c, _, m2m, _ = client
    # In force today: the approved 25,000 row dated yesterday (plan fixture). Not used: an older approved row, a
    # pending row dated today and an approved row dated tomorrow.
    await _approved_fx(admin, 24_000, TODAY - timedelta(days=10))
    await _approved_fx(admin, 26_000, TODAY, approve=False)
    await _approved_fx(admin, 30_000, TODAY + timedelta(days=1))
    room = {"line_ref": "room", "component_id": f"rt:{rules['room']}", "service_date": DAY1, "nights": 2}
    body = {"proposal_ref": "VND-1", "proposal_version": 1, "currency": "VND", "lines": [{**room, "pax": {"adult": 2}}]}
    quote = await _ok(await c.post("/b2b/v1/quotes", json=body, headers=_m2m(m2m)), 201)
    assert quote["fx"] == {
        "base_currency": "USD",
        "quote_currency": "VND",
        "rate_per_usd": "25000.00000000",
        "rate_date": (TODAY - timedelta(days=1)).isoformat(),
        "source": "synthetic",
    }
    # 2 nights x 1,000,000 VND = 80 USD net; seed b2b rule 15 % + 3 % card fee → 97.5609… USD → x 25,000 =
    # 2,439,024.39 VND → rounded up to 1,000 VND.
    assert quote["lines"][0]["b2b_price"] == {"amount": "2440000", "currency": "VND"}
    assert quote["total_b2b_price"] == {"amount": "2440000", "currency": "VND"}
    stored = await admin.fetchrow(
        "SELECT rounding, fx_rate_per_usd, fx_as_of, total_b2b_price FROM booking.b2b_quotes WHERE id = $1::uuid",
        quote["quote_id"],
    )
    assert dict(stored) == {
        "rounding": "line_ceil_1000",
        "fx_rate_per_usd": D("25000"),
        "fx_as_of": TODAY - timedelta(days=1),
        "total_b2b_price": D("2440000.00"),
    }
    # The VND rate list the CRM reads (R17 source) shows the same row in force.
    rates = await _ok(await c.get(f"/b2b/v1/fx-rates?date={TODAY.isoformat()}", headers=_m2m(m2m)))
    assert [(r["currency"], r["rate_per_usd"]) for r in rates["rates"]] == [("VND", "25000.00000000")]


async def test_extend_once(client: Client, admin: asyncpg.Connection, rules: dict[str, str]) -> None:
    c, _, m2m, _ = client
    dep = await _departure(c, client[1], DAY1, 4)
    quote = await _ok(await _quote(c, m2m, "EXT-1", [_plan_line("tour", rules["plan"], dep["id"])]), 201)
    hold = await _ok(await c.post("/b2b/v1/holds", json=_hold_body(quote, "EXT-1"), headers=_m2m(m2m, "ext-hold")), 201)
    url = f"/b2b/v1/holds/{hold['hold_id']}/extend"
    _problem(
        await c.post(url, json={"reason": "payment_claimed"}, headers=_m2m(m2m, "ext-key-1", scope="booking.b2b/read")),
        403,
        "forbidden",
    )
    extended = await _ok(await c.post(url, json={"reason": "payment_claimed"}, headers=_m2m(m2m, "ext-key-1")))
    before = datetime.fromisoformat(hold["expires_at"])
    assert extended["extended_count"] == 1
    assert datetime.fromisoformat(extended["expires_at"]) == before + timedelta(hours=72)
    retry = await _ok(await c.post(url, json={"reason": "payment_claimed"}, headers=_m2m(m2m, "ext-key-1")))
    assert retry == extended
    second = await c.post(url, json={"reason": "client_recheck"}, headers=_m2m(m2m, "ext-key-2"))
    _problem(second, 409, "extend_limit")
    row = await admin.fetchrow(
        "SELECT extended_count, extend_reason, extend_idempotency_key, extended_at IS NOT NULL AS stamped"
        " FROM booking.b2b_holds WHERE id = $1::uuid",
        hold["hold_id"],
    )
    assert dict(row) == {
        "extended_count": 1,
        "extend_reason": "payment_claimed",
        "extend_idempotency_key": "ext-key-1",
        "stamped": True,
    }
    assert (await _seats(c, m2m))[DAY1] == (2, 0, 2)  # the seats stay held
    # A released hold cannot be extended (410: place a new hold); an unknown one is 404.
    other = await _ok(await _quote(c, m2m, "EXT-2", [_plan_line("tour", rules["plan"], dep["id"])]), 201)
    held = await _ok(
        await c.post("/b2b/v1/holds", json=_hold_body(other, "EXT-2"), headers=_m2m(m2m, "ext-hold-2")), 201
    )
    await c.delete(f"/b2b/v1/holds/{held['hold_id']}", headers=_m2m(m2m))
    gone = await c.post(
        f"/b2b/v1/holds/{held['hold_id']}/extend", json={"reason": "client_recheck"}, headers=_m2m(m2m, "ext-key-3")
    )
    _problem(gone, 410, "hold_expired")
    unknown = f"/b2b/v1/holds/{SEED}/extend"
    _problem(await c.post(unknown, json={"reason": "client_recheck"}, headers=_m2m(m2m, "ext-key-4")), 404, "not_found")
    actions = await admin.fetch(
        "SELECT action FROM booking.audit_logs WHERE entity_id = $1 ORDER BY created_at", hold["hold_id"]
    )
    assert [a["action"] for a in actions] == ["b2b_hold.placed", "b2b_hold.extended"]


async def test_fourth_rehold_in_a_day_is_429(client: Client, admin: asyncpg.Connection, rules: dict[str, str]) -> None:
    c, _, m2m, _ = client
    room = {"line_ref": "room", "component_id": f"rt:{rules['room']}", "service_date": DAY1, "pax": {"adult": 2}}

    async def hold(ref: str, version: int, key: str) -> httpx.Response:
        quote = await _ok(await _quote(c, m2m, ref, [room], version=version), 201)
        return await c.post("/b2b/v1/holds", json=_hold_body(quote, ref, version), headers=_m2m(m2m, key))

    first = await _ok(await hold("RH-1", 1, "rh-key-1"), 201)  # the first hold is not a re-hold
    for version in (2, 3, 4):  # re-holds 1..3 (the client's "check availability again")
        await _ok(await hold("RH-1", version, f"rh-key-{version}"), 201)
    refused = _problem(await hold("RH-1", 5, "rh-key-5"), 429, "rehold_limit")
    midnight = datetime.combine(datetime.now(UTC).date() + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
    assert 0 < refused["retry_after_s"] <= (midnight - datetime.now(UTC)).total_seconds() + 5
    assert await admin.fetchval("SELECT count(*) FROM booking.b2b_holds WHERE proposal_ref = 'RH-1'") == 4
    # Idempotent answers still work after the limit, and another proposal is not limited.
    replay = await c.post(
        "/b2b/v1/holds",
        json={**_hold_body({"quote_id": first["quote_id"], "price_hash": "0" * 64}, "RH-1")},
        headers=_m2m(m2m, "rh-key-1"),
    )
    assert replay.status_code == 201 and replay.json()["hold_id"] == first["hold_id"]
    await _ok(await hold("RH-2", 1, "rh-other-1"), 201)
    # Re-holds of earlier days do not count: move them to yesterday, then three more re-holds fit today.
    async with admin.transaction():
        owner = await admin.fetchval("SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = 'booking'")
        await admin.execute(f'SET LOCAL ROLE "{owner}"')
        await admin.execute(
            "UPDATE booking.b2b_holds SET created_at = created_at - interval '1 day' WHERE proposal_ref = 'RH-1'"
        )
    for version in (5, 6, 7):
        await _ok(await hold("RH-1", version, f"rh-key-{version}b"), 201)
    _problem(await hold("RH-1", 8, "rh-key-8"), 429, "rehold_limit")


async def test_cancel_with_fee_and_dry_run(client: Client, admin: asyncpg.Connection, rules: dict[str, str]) -> None:
    c, cis, m2m, _ = client
    maker, checker, boss = _staff(cis, MAKER), _staff(cis, CHECKER), _staff(cis, "it-973-admin")
    dep1 = await _departure(c, cis, DAY1, 4)
    dep2 = await _departure(c, cis, DAY2, 4)

    async def book(ref: str, dep: dict[str, Any], start: str) -> tuple[dict[str, Any], dict[str, Any]]:
        quote = await _ok(await _quote(c, m2m, ref, [_plan_line("tour", rules["plan"], dep["id"], start)]), 201)
        hold = await _ok(
            await c.post("/b2b/v1/holds", json=_hold_body(quote, ref), headers=_m2m(m2m, f"{ref}-hold-key")), 201
        )
        url = f"/b2b/v1/holds/{hold['hold_id']}/confirm"
        booking = await _ok(
            await c.post(url, json={"price_hash": quote["price_hash"]}, headers=_m2m(m2m, f"{ref}-confirm-key")), 201
        )
        return quote, booking

    # Booking 1 under the seeded default (30 days before departure → the 30-day tier, 25 %; 29 between local and UTC
    # midnight → 50 %, SEED_PCT).
    quote, booking = await book("CX-1", dep1, DAY1)
    locked = await admin.fetchval(
        "SELECT cancellation_policy_id::text FROM booking.b2b_holds WHERE id = $1::uuid", booking["hold_id"]
    )
    assert locked == SEED
    assert (await _seats(c, m2m))[DAY1] == (0, 2, 2)

    # Staff add a plan policy for this tour (maker-checker) AFTER booking 1: it applies to new bookings only.
    body = {
        "title": "Hanoi 1D agency terms",
        "source_id": SOURCE_ID,
        "valid_from": UTC_TODAY.isoformat(),  # the confirm judges "in force" on the UTC day
        "tiers": [{"min_days": 45, "fee_pct": "0"}, {"min_days": 0, "fee_pct": "40"}],
    }
    url = "/cancellation-policies"
    assert (await c.post(f"/pricing{url}", json=body, headers=_staff(cis, "it-973-sales"))).status_code == 403
    created = await _ok(await c.post(f"/pricing{url}", json=body, headers=maker), 201)
    assert created["status"] == "pending" and created["source_id"] == SOURCE_ID
    assert (await c.post(f"/pricing{url}/{created['id']}/approve", headers=maker)).status_code == 403  # own row
    approved = await _ok(await c.post(f"/pricing{url}/{created['id']}/approve", headers=checker))
    assert approved["status"] == "in_force"
    listed = await _ok(await c.get(f"/pricing{url}", headers=boss))
    statuses = {(i["source_id"], i["id"]): i["status"] for i in listed["items"]}
    assert statuses[(None, SEED)] == "in_force" and statuses[(SOURCE_ID, created["id"])] == "in_force"

    cancel_url = f"/b2b/v1/bookings/{booking['booking_id']}/cancel"
    total = D(quote["total_b2b_price"]["amount"])
    fee = (total * SEED_PCT / 100).quantize(D("0.01"), rounding=ROUND_HALF_UP)
    expected = {
        "booking_id": booking["booking_id"],
        "fee": {"amount": f"{fee:.2f}", "currency": "USD"},
        "refundable_b2b_amount": {"amount": f"{total - fee:.2f}", "currency": "USD"},
        "fee_basis": {"policy_id": SEED, "days_before_departure": DAYS1, "fee_pct": f"{SEED_PCT:.2f}"},
    }

    # Dry run: the scheduled fee, nothing written.
    reason = {"reason_code": "client_cancelled"}
    preview = await _ok(await c.post(f"{cancel_url}?dry_run=true", json=reason, headers=_m2m(m2m, "cx-dry-1")))
    assert preview == {**expected, "dry_run": True, "cancelled_at": None}
    assert await admin.fetchval("SELECT count(*) FROM booking.b2b_cancellations") == 0
    assert (await _seats(c, m2m))[DAY1] == (0, 2, 2)
    _problem(
        await c.post(cancel_url, json=reason, headers=_m2m(m2m, "cx-key-1", scope="booking.b2b/hold")),
        403,
        "forbidden",
    )

    # The cancel: same fee, the trip case cancelled, the seats back on sale.
    done = await _ok(await c.post(cancel_url, json=reason, headers=_m2m(m2m, "cx-key-1")))
    assert done | {"cancelled_at": None} == {**expected, "dry_run": False, "cancelled_at": None}
    assert done["cancelled_at"] is not None
    assert (await _seats(c, m2m))[DAY1] == (0, 0, 4)
    case = await admin.fetchrow("SELECT status FROM booking.trip_cases WHERE id = $1::uuid", booking["booking_id"])
    assert case["status"] == "cancelled"
    history = await admin.fetch(
        "SELECT from_status, to_status, changed_by, note FROM booking.trip_case_status_history"
        " WHERE trip_case_id = $1::uuid ORDER BY seq",
        booking["booking_id"],
    )
    assert [(h["from_status"], h["to_status"], h["changed_by"]) for h in history][-1] == (
        "confirmed",
        "cancelled",
        None,
    )
    assert history[-1]["note"].startswith(f"B2B cancel by m2m:{CRM_CLIENT} (client_cancelled); fee {SEED_PCT:.2f}%")
    lines = await admin.fetch("SELECT status FROM booking.b2b_hold_lines WHERE hold_id = $1::uuid", booking["hold_id"])
    assert {r["status"] for r in lines} == {"cancelled"}
    stored = await admin.fetchrow(
        "SELECT * FROM booking.b2b_cancellations WHERE hold_id = $1::uuid", booking["hold_id"]
    )
    assert (stored["fee_amount"], stored["refundable_amount"], stored["departure_date"]) == (
        fee,
        total - fee,
        DAY1_DATE,
    )
    assert stored["idempotency_key"] == "cx-key-1" and stored["reason_code"] == "client_cancelled"
    # Replays (another key, or a dry run) return the stored cancellation; nothing more is written.
    again = await _ok(await c.post(cancel_url, json=reason, headers=_m2m(m2m, "cx-key-2")))
    preview_after = await _ok(await c.post(f"{cancel_url}?dry_run=true", json=reason, headers=_m2m(m2m, "cx-dry-2")))
    assert again == preview_after == done
    assert await admin.fetchval("SELECT count(*) FROM booking.b2b_cancellations") == 1

    # Booking 2 is confirmed after the plan policy: it carries it (37 days before → the 0-day tier, 40 %).
    quote2, booking2 = await book("CX-2", dep2, DAY2)
    preview2 = await _ok(
        await c.post(
            f"/b2b/v1/bookings/{booking2['booking_id']}/cancel?dry_run=true", json=reason, headers=_m2m(m2m, "cx-dry-3")
        )
    )
    assert preview2["fee_basis"] == {"policy_id": created["id"], "days_before_departure": DAYS2, "fee_pct": "40.00"}

    # AA cancels departure 2 (staff): the agency's cancel of booking 2 then costs nothing, whatever the schedule says.
    await _ok(await c.patch(f"/catalog/departures/{dep2['id']}", json={"status": "cancelled"}, headers=maker))
    free = await _ok(
        await c.post(
            f"/b2b/v1/bookings/{booking2['booking_id']}/cancel?dry_run=true",
            json={"reason_code": "departure_cancelled"},
            headers=_m2m(m2m, "cx-dry-4"),
        )
    )
    assert free["fee"]["amount"] == "0.00" and free["fee_basis"]["fee_pct"] == "0.00"
    assert free["refundable_b2b_amount"] == quote2["total_b2b_price"]

    # Another agency (same client, another tenant) cannot see or cancel these bookings; an unknown id is 404 too.
    other_tenant = "e2000000-0000-4000-8000-000000000974"
    await _insert(admin, "b2b_partners", tenant_id=other_tenant, agency_name="Agency 974", m2m_client_id=CRM_CLIENT)
    foreign = {**_m2m(m2m, "cx-key-9"), "X-AA-Tenant-Id": other_tenant}
    url2 = f"/b2b/v1/bookings/{booking2['booking_id']}/cancel"
    _problem(await c.post(url2, json=reason, headers=foreign), 404, "not_found")
    _problem(
        await c.post(f"/b2b/v1/bookings/{SEED}/cancel", json=reason, headers=_m2m(m2m, "cx-key-9")), 404, "not_found"
    )
    assert TENANT != other_tenant


async def test_two_concurrent_reholds_at_the_limit(
    client: Client, admin: asyncpg.Connection, rules: dict[str, str]
) -> None:
    """Both re-holds wait on the proposal's advisory lock (held here by another session), then run one after the
    other: with 2 re-holds done today and a limit of 3, exactly one passes."""
    c, _, m2m, _ = client
    room = {"line_ref": "room", "component_id": f"rt:{rules['room']}", "service_date": DAY1, "pax": {"adult": 2}}
    quotes = {v: await _ok(await _quote(c, m2m, "RH-3", [room], version=v), 201) for v in (1, 2, 3, 4, 5)}
    for version in (1, 2, 3):
        body = _hold_body(quotes[version], "RH-3", version)
        await _ok(await c.post("/b2b/v1/holds", json=body, headers=_m2m(m2m, f"rh3-key-{version}")), 201)
    tx = admin.transaction()
    await tx.start()
    await admin.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended($1 || ':' || $2, 974))", rules["partner"], "RH-3"
    )
    tasks = [
        asyncio.create_task(
            c.post("/b2b/v1/holds", json=_hold_body(quotes[v], "RH-3", v), headers=_m2m(m2m, f"rh3-key-{v}"))
        )
        for v in (4, 5)
    ]
    await asyncio.sleep(0.5)
    waiting = await admin.fetchval("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted")
    await tx.rollback()
    results = await asyncio.gather(*tasks)
    assert waiting == 2
    assert sorted(r.status_code for r in results) == [201, 429]
    assert await admin.fetchval("SELECT count(*) FROM booking.b2b_holds WHERE proposal_ref = 'RH-3'") == 4


async def _book(
    c: httpx.AsyncClient, m2m: LocalM2M, ref: str, line: dict[str, Any], currency: str = "USD"
) -> tuple[dict[str, Any], dict[str, Any]]:
    body = {"proposal_ref": ref, "proposal_version": 1, "currency": currency, "lines": [line]}
    quote = await _ok(await c.post("/b2b/v1/quotes", json=body, headers=_m2m(m2m)), 201)
    hold = await _ok(
        await c.post("/b2b/v1/holds", json=_hold_body(quote, ref), headers=_m2m(m2m, f"{ref}-hold-key")), 201
    )
    url = f"/b2b/v1/holds/{hold['hold_id']}/confirm"
    booking = await _ok(
        await c.post(url, json={"price_hash": quote["price_hash"]}, headers=_m2m(m2m, f"{ref}-confirm-key")), 201
    )
    return quote, booking


async def test_vnd_booking_cancel_end_to_end(client: Client, admin: asyncpg.Connection, rules: dict[str, str]) -> None:
    """Linear E on the VND path: quote (25,000 VND/USD, lines up to 1,000 VND) → hold → confirm into a VND B2B trip
    case → cancel dry run and for real: a whole-VND fee, refundable = total - fee, the stored row passes the CHECK."""
    c, cis, m2m, _ = client
    dep = await _departure(c, cis, DAY1, 4)
    quote, booking = await _book(c, m2m, "VX-1", _plan_line("tour", rules["plan"], dep["id"]), "VND")
    total = D(quote["total_b2b_price"]["amount"])
    assert quote["total_b2b_price"]["currency"] == "VND" and quote["fx"]["rate_per_usd"] == "25000.00000000"
    assert total > 0 and total % 1000 == 0 and "." not in quote["total_b2b_price"]["amount"]
    case = await admin.fetchrow(
        "SELECT c.currency AS case_currency, v.currency AS version_currency, v.total_price, v.channel"
        " FROM booking.trip_cases c JOIN booking.trip_case_versions v ON v.trip_case_id = c.id"
        " WHERE c.id = $1::uuid",
        booking["booking_id"],
    )
    assert (case["case_currency"].strip(), case["version_currency"].strip(), case["channel"]) == ("VND", "VND", "b2b")
    assert case["total_price"] == total

    fee = (total * SEED_PCT / 100).quantize(D("1"), rounding=ROUND_HALF_UP)
    expected = {
        "booking_id": booking["booking_id"],
        "fee": {"amount": str(fee), "currency": "VND"},
        "refundable_b2b_amount": {"amount": str(total - fee), "currency": "VND"},
        "fee_basis": {"policy_id": SEED, "days_before_departure": DAYS1, "fee_pct": f"{SEED_PCT:.2f}"},
    }
    cancel_url = f"/b2b/v1/bookings/{booking['booking_id']}/cancel"
    reason = {"reason_code": "client_cancelled"}
    preview = await _ok(await c.post(f"{cancel_url}?dry_run=true", json=reason, headers=_m2m(m2m, "vx-dry-1")))
    assert preview == {**expected, "dry_run": True, "cancelled_at": None}
    assert await admin.fetchval("SELECT count(*) FROM booking.b2b_cancellations") == 0
    done = await _ok(await c.post(cancel_url, json=reason, headers=_m2m(m2m, "vx-key-1")))
    assert done | {"cancelled_at": None} == {**expected, "dry_run": False, "cancelled_at": None}
    stored = await admin.fetchrow(
        "SELECT currency, total_b2b_price, fee_amount, refundable_amount FROM booking.b2b_cancellations"
        " WHERE hold_id = $1::uuid",
        booking["hold_id"],
    )
    assert stored["currency"] == "VND" and stored["total_b2b_price"] == total
    assert stored["fee_amount"] == fee and stored["fee_amount"] == stored["fee_amount"].to_integral_value()
    assert stored["fee_amount"] + stored["refundable_amount"] == total
    status = await admin.fetchval("SELECT status FROM booking.trip_cases WHERE id = $1::uuid", booking["booking_id"])
    assert status == "cancelled"
    assert (await _seats(c, m2m))[DAY1] == (0, 0, 4)


async def test_a_later_backdated_policy_never_reprices_an_unlocked_booking(
    client: Client, admin: asyncpg.Connection, rules: dict[str, str]
) -> None:
    """A hold confirmed without a locked policy (the AA-973 code, after bk-021 was applied) resolves the policy among
    the rows approved by its confirm: a plan policy approved later, even dated before the confirm, does not apply."""
    c, cis, m2m, _ = client
    dep = await _departure(c, cis, DAY1, 4)
    _, booking = await _book(c, m2m, "LG-1", _plan_line("tour", rules["plan"], dep["id"]))
    async with admin.transaction():
        owner = await admin.fetchval("SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = 'booking'")
        await admin.execute(f'SET LOCAL ROLE "{owner}"')
        await admin.execute(
            "UPDATE booking.b2b_holds SET cancellation_policy_id = NULL WHERE id = $1::uuid", booking["hold_id"]
        )
    body = {
        "title": "Backdated agency terms",
        "source_id": SOURCE_ID,
        "valid_from": (UTC_TODAY - timedelta(days=30)).isoformat(),
        "tiers": [{"min_days": 0, "fee_pct": "40"}],
    }
    created = await _ok(await c.post("/pricing/cancellation-policies", json=body, headers=_staff(cis, MAKER)), 201)
    await _ok(await c.post(f"/pricing/cancellation-policies/{created['id']}/approve", headers=_staff(cis, CHECKER)))
    url = f"/b2b/v1/bookings/{booking['booking_id']}/cancel?dry_run=true"
    reason = {"reason_code": "client_cancelled"}
    preview = await _ok(await c.post(url, json=reason, headers=_m2m(m2m, "lg-dry-1")))
    assert preview["fee_basis"] == {"policy_id": SEED, "days_before_departure": DAYS1, "fee_pct": f"{SEED_PCT:.2f}"}
    # Control: had the booking been confirmed after that approval, the plan policy would apply.
    async with admin.transaction():
        await admin.execute(f'SET LOCAL ROLE "{owner}"')
        await admin.execute(
            "UPDATE booking.b2b_holds SET confirmed_at = now() + interval '1 minute' WHERE id = $1::uuid",
            booking["hold_id"],
        )
    later = await _ok(await c.post(url, json=reason, headers=_m2m(m2m, "lg-dry-2")))
    assert later["fee_basis"]["policy_id"] == created["id"] and later["fee_basis"]["fee_pct"] == "40.00"


async def test_departure_rehold_limit_with_fresh_proposal_refs(
    client: Client, admin: asyncpg.Connection, rules: dict[str, str]
) -> None:
    """Per departure: an agency that holds and lets go one departure under a new proposal ref each time is refused
    on the 4th re-hold of that departure in a day (429); another departure, and holds still active, are not limited."""
    c, cis, m2m, _ = client
    dep1 = await _departure(c, cis, DAY1, 10)
    dep2 = await _departure(c, cis, DAY2, 10)

    async def hold(ref: str, dep: dict[str, Any], start: str = DAY1) -> httpx.Response:
        quote = await _ok(await _quote(c, m2m, ref, [_plan_line("tour", rules["plan"], dep["id"], start)]), 201)
        return await c.post("/b2b/v1/holds", json=_hold_body(quote, ref), headers=_m2m(m2m, f"{ref}-key"))

    kept = await _ok(await hold("RD-0", dep1), 201)  # active all along: not a let-go hold
    for n in (1, 2, 3, 4):  # the first hold of the cycle, then re-holds 1..3, each released
        held = await _ok(await hold(f"RD-{n}", dep1), 201)
        assert (await c.delete(f"/b2b/v1/holds/{held['hold_id']}", headers=_m2m(m2m))).status_code == 204
    refused = _problem(await hold("RD-5", dep1), 429, "rehold_limit")
    assert dep1["id"] in refused["message"] and refused["retry_after_s"] > 0
    await _ok(await hold("RD-6", dep2, DAY2), 201)  # another departure
    statuses = await admin.fetch(
        "SELECT proposal_ref, status FROM booking.b2b_holds WHERE proposal_ref LIKE 'RD-%' ORDER BY proposal_ref"
    )
    assert [(r["proposal_ref"], r["status"]) for r in statuses] == [
        ("RD-0", "active"),
        ("RD-1", "released"),
        ("RD-2", "released"),
        ("RD-3", "released"),
        ("RD-4", "released"),
        ("RD-6", "active"),
    ]
    assert kept["status"] == "active"
