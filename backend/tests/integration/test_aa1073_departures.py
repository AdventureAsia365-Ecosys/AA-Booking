"""AA-1073 (bk-046) end to end as role ``booking`` on a real PostgreSQL (schema checks: ``test_bk046_schema.py``).

- the recurring generator: dry run preview, create, a second run skips the dates that exist;
- minimum to operate and sales cutoff on the staff list and the B2B ``GET /departures`` (contract 1.6 fields); a quote
  inside the cutoff is ``sold_out`` (sales closed);
- agency allotments: proposed by one person, refused to the same person, approved by another; the other agency sees
  fewer seats, the allotment agency its own; capacity cannot go below them; ending it gives the seats back;
- the daily watch on a role-``booking`` connection rolled back at the end: guarantee at the minimum (fan-out
  ``departure.guaranteed``), at risk on the decision date (staff notification, desk flag); the release job
  (``allotment.released`` to the one agency). Rolled back, so no new outbox type stays committed (tests of bk-022 /
  bk-023 re-run those files, which put back the older CHECK and trigger).

Dates are relative to today; agency names are invented.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta

import asyncpg
import httpx
import pytest
import pytest_asyncio

from app.b2b import allotments, departure_watch, desk
from app.db import Database
from tests.integration.conftest import apply_migrations
from tests.integration.test_bk020_b2b import (  # noqa: F401 — fixtures admin, client, plan are used by name
    ADMIN,
    DAY1,
    DAY1_DATE,
    MAKER,
    SALES,
    SOURCE_ID,
    TODAY,
    _hold_body,
    _insert,
    _m2m,
    _ok,
    _plan_line,
    _quote,
    _staff,
    admin,
    client,
    plan,
)
from tests.staff_tokens import CRM_CLIENT, LocalCis, LocalM2M

FIXTURES = (admin, client, plan)  # the AA-973 fixtures, used by name below

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

Client = tuple[httpx.AsyncClient, LocalCis, LocalM2M, Database]
OTHER_TENANT = "e2000000-0000-4000-8000-000000001073"
DEPS = f"/catalog/trip-plans/{SOURCE_ID}/departures"


@pytest_asyncio.fixture
async def bk046(migrated_admin_dsn: str) -> None:
    """Tests of bk-022 / bk-023 re-run those files (older outbox trigger and CHECK): put bk-046 back first."""
    conn = await asyncpg.connect(migrated_admin_dsn)
    try:
        await apply_migrations(conn, only=[46])
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def booking_tx(migrated_booking_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_booking_dsn)
    tx = conn.transaction()
    await tx.start()
    try:
        yield conn
    finally:
        await tx.rollback()
        await conn.close()


def _other(m2m: LocalM2M, key: str | None = None) -> dict[str, str]:
    return {**_m2m(m2m, key), "X-AA-Tenant-Id": OTHER_TENANT}


async def _b2b(c: httpx.AsyncClient, headers: dict[str, str]) -> dict[str, dict]:
    page = await _ok(
        await c.get(f"/b2b/v1/departures?source_system=cis&source_id={SOURCE_ID}&from={TODAY}", headers=headers)
    )
    return {d["start_date"]: d for d in page["items"]}


async def test_generator_minimum_cutoff_and_allotments(
    bk046: None, client: Client, admin: asyncpg.Connection, plan: dict[str, str]
) -> None:
    c, cis, m2m, _ = client
    maker, sales, boss = _staff(cis, MAKER), _staff(cis, SALES), _staff(cis, ADMIN)

    # Generator: weekly on the weekday of DAY1 for three weeks, preview first.
    weekday = DAY1_DATE.isoweekday()
    body = {
        "date_from": DAY1,
        "date_to": (DAY1_DATE + timedelta(days=20)).isoformat(),
        "pattern": {"kind": "weekly", "weekdays": [weekday]},
        "capacity": 6,
        "min_to_operate": 4,
        "sales_cutoff_days": 2,
        "decision_days_before": 21,
    }
    assert (await c.post(f"{DEPS}:generate", json=body, headers=sales)).status_code == 403
    preview = await _ok(await c.post(f"{DEPS}:generate", json={**body, "dry_run": True}, headers=maker))
    dates = [(DAY1_DATE + timedelta(days=7 * i)).isoformat() for i in range(3)]
    assert preview["created"] == dates and preview["skipped"] == [] and preview["dry_run"] is True
    assert (await _ok(await c.get(DEPS, headers=sales)))["items"] == []
    made = await _ok(await c.post(f"{DEPS}:generate", json=body, headers=maker))
    assert made["created_count"] == 3
    again = await _ok(await c.post(f"{DEPS}:generate", json=body, headers=maker))
    assert (again["created_count"], again["skipped"]) == (0, dates)
    listed = (await _ok(await c.get(DEPS, headers=sales)))["items"]
    assert [
        (d["start_date"], d["min_to_operate"], d["sales_cutoff_days"], d["seats_to_guarantee"]) for d in listed
    ] == [(d, 4, 2, 4) for d in dates]
    dep1 = listed[0]
    audit = await admin.fetchval(
        "SELECT count(*) FROM booking.audit_logs WHERE action = 'departure.generated' AND entity_id = $1::uuid",
        plan["plan"],
    )
    assert audit == 1

    # The contract 1.6 fields on the B2B read.
    seen = (await _b2b(c, _m2m(m2m)))[DAY1]
    assert (seen["min_to_operate"], seen["seats_to_guarantee"], seen["last_sale_date"], seen["allotment"]) == (
        4,
        4,
        (DAY1_DATE - timedelta(days=3)).isoformat(),
        None,
    )

    # Allotment: 3 seats of DAY1 for the other agency, released 10 days before.
    other = await _insert(
        admin, "b2b_partners", tenant_id=OTHER_TENANT, agency_name="Copperbay Trips", m2m_client_id=CRM_CLIENT
    )
    allot_url = f"/catalog/departures/{dep1['id']}/allotments"
    proposed = await _ok(
        await c.post(allot_url, json={"partner_id": other, "seats": 3, "release_days_before": 10}, headers=maker), 201
    )
    assert proposed["state"] == "pending" and proposed["partner"]["agency_name"] == "Copperbay Trips"
    dup = await c.post(allot_url, json={"partner_id": other, "seats": 2, "release_days_before": 10}, headers=sales)
    assert dup.status_code == 409 and "allotment_pending" in dup.text
    own = await c.post(f"/catalog/allotments/{proposed['id']}/approve", headers=maker)
    assert own.status_code == 403 and "maker_checker" in own.text
    approved = await _ok(await c.post(f"/catalog/allotments/{proposed['id']}/approve", headers=boss))
    assert approved["state"] == "in_force" and approved["used"] == 0
    listing = await _ok(await c.get(allot_url, headers=sales))
    assert listing["departure"]["allotted"] == 3 and listing["departure"]["available"] == 3
    assert {o["agency_name"] for o in listing["partner_options"]} >= {"Agency 973", "Copperbay Trips"}

    # Seats: the first agency no longer sees the other's 3; the other sees all 6 and its allotment.
    mine, theirs = (await _b2b(c, _m2m(m2m)))[DAY1], (await _b2b(c, _other(m2m)))[DAY1]
    assert (mine["available"], mine["allotment"]) == (3, None)
    assert theirs["available"] == 6
    assert theirs["allotment"] == {
        "seats": 3,
        "used": 0,
        "available": 3,
        "release_date": (DAY1_DATE - timedelta(days=10)).isoformat(),
    }
    over = await _ok(await _quote(c, m2m, "PRO-1073", [_plan_line("tour", plan["plan"], dep1["id"], adult=4)]), 201)
    assert over["lines"][0]["availability"] == "sold_out"
    fits = await _ok(await _quote(c, m2m, "PRO-1073", [_plan_line("tour", plan["plan"], dep1["id"], adult=3)]), 201)
    assert fits["lines"][0]["availability"] == "available"
    # A hold of the first agency takes the last free seats; the allotment agency still holds its own 3.
    await _ok(await c.post("/b2b/v1/holds", json=_hold_body(fits, "PRO-1073"), headers=_m2m(m2m, "key-1073-a1")), 201)
    assert (await _b2b(c, _other(m2m)))[DAY1]["available"] == 3

    # Capacity cannot go below held + sold + the unused allotment; min cannot exceed the capacity.
    patch = f"/catalog/departures/{dep1['id']}"
    low = await c.patch(patch, json={"capacity": 5}, headers=maker)
    assert low.status_code == 409 and "capacity_below_taken" in low.text
    too_high = await c.patch(patch, json={"min_to_operate": 7}, headers=maker)
    assert too_high.status_code == 422 and "invalid_min_to_operate" in too_high.text

    # Ending the allotment gives its seats back to everyone.
    ended = await _ok(await c.post(f"/catalog/allotments/{proposed['id']}/end", headers=sales))
    assert ended["state"] == "ended" and ended["end_reason"] == "withdrawn"
    assert (await _b2b(c, _m2m(m2m)))[DAY1]["available"] == 3  # 6 - its own hold of 3

    # Sales cutoff: a departure inside its cutoff is sold out with the reason.
    soon = (TODAY + timedelta(days=3)).isoformat()
    near = await _ok(
        await c.post(DEPS, json={"start_date": soon, "capacity": 6, "sales_cutoff_days": 5}, headers=maker), 201
    )
    closed = await _quote(c, m2m, "PRO-1073-B", [_plan_line("tour", plan["plan"], near["id"], start=soon)])
    assert (await _ok(closed, 201))["lines"][0]["availability"] == "sold_out"


async def test_watch_and_release(
    bk046: None, client: Client, admin: asyncpg.Connection, plan: dict[str, str], booking_tx: asyncpg.Connection
) -> None:
    c, cis, m2m, _ = client
    maker, boss = _staff(cis, MAKER), _staff(cis, ADMIN)
    # A: minimum 2, decision 40 days before (passed); 2 sold below → guaranteed.
    # B: minimum 5, decision 40 days before, nothing sold → at risk.  C: minimum 5, decision 10 days before → untouched.
    common = {"capacity": 8, "decision_days_before": 40}
    dep_a = await _ok(await c.post(DEPS, json={"start_date": DAY1, "min_to_operate": 2, **common}, headers=maker), 201)
    day_b = (DAY1_DATE + timedelta(days=1)).isoformat()
    dep_b = await _ok(await c.post(DEPS, json={"start_date": day_b, "min_to_operate": 5, **common}, headers=maker), 201)
    day_c = (DAY1_DATE + timedelta(days=2)).isoformat()
    dep_c = await _ok(
        await c.post(DEPS, json={"start_date": day_c, "min_to_operate": 5, "capacity": 8, "decision_days_before": 10},
                     headers=maker), 201
    )  # fmt: skip
    quote = await _ok(await _quote(c, m2m, "PRO-W", [_plan_line("tour", plan["plan"], dep_a["id"], adult=2)]), 201)
    hold = await _ok(
        await c.post("/b2b/v1/holds", json=_hold_body(quote, "PRO-W"), headers=_m2m(m2m, "key-1073-w1")), 201
    )
    confirm = {"price_hash": quote["price_hash"], "deposit_ref": "pay_1073"}
    await _ok(
        await c.post(f"/b2b/v1/holds/{hold['hold_id']}/confirm", json=confirm, headers=_m2m(m2m, "conf-1073-w1")), 201
    )

    # An allotment on A for another agency, released 5 days before.
    other = await _insert(
        admin, "b2b_partners", tenant_id=OTHER_TENANT, agency_name="Saltmarsh Holidays", m2m_client_id=CRM_CLIENT
    )
    proposed = await _ok(
        await c.post(f"/catalog/departures/{dep_a['id']}/allotments",
                     json={"partner_id": other, "seats": 2, "release_days_before": 5}, headers=maker), 201
    )  # fmt: skip
    await _ok(await c.post(f"/catalog/allotments/{proposed['id']}/approve", headers=boss))

    conn = booking_tx
    dry = await departure_watch.run_watch(conn, today=TODAY, dry_run=True)
    assert {g["departure_id"] for g in dry["guaranteed"]} == {dep_a["id"]}
    assert {a["departure_id"] for a in dry["at_risk"]} == {dep_b["id"]}
    out = await departure_watch.run_watch(conn, today=TODAY)
    assert (out["guaranteed_count"], out["at_risk_count"]) == (1, 1)
    status = dict(
        await conn.fetch(
            "SELECT id::text, status FROM booking.departures WHERE id = ANY($1::uuid[])",
            [dep_a["id"], dep_b["id"], dep_c["id"]],
        )
    )
    assert status == {dep_a["id"]: "guaranteed", dep_b["id"]: "scheduled", dep_c["id"]: "scheduled"}
    events = await conn.fetch(
        "SELECT event_type, partner_id::text AS partner FROM booking.outbox WHERE departure_id = $1::uuid"
        " AND event_type = 'departure.guaranteed'",
        dep_a["id"],
    )
    assert [e["event_type"] for e in events] == ["departure.guaranteed"] and events[0]["partner"] == plan["partner"]
    note = await conn.fetchval(
        "SELECT count(*) FROM booking.notifications WHERE type = 'departure.at_risk' AND entity_id = $1::uuid",
        dep_b["id"],
    )
    assert note == 1
    again = await departure_watch.run_watch(conn, today=TODAY)  # stamped once, guaranteed once
    assert (again["guaranteed_count"], again["at_risk_count"]) == (0, 0)
    board = {d["id"]: d for d in (await desk.list_departures(conn, today=TODAY))["items"]}
    assert board[dep_b["id"]]["at_risk"] is True and board[dep_a["id"]]["at_risk"] is False
    assert (await desk.summary(conn))["departures_at_risk"] >= 1

    # Release: due on start - 5 days; not before.
    assert (await allotments.release_due(conn, today=DAY1_DATE - timedelta(days=6)))["released"] == 0
    released = await allotments.release_due(conn, today=DAY1_DATE - timedelta(days=5))
    assert released["released"] == 1 and released["allotments"] == [proposed["id"]]
    events = await conn.fetch(
        "SELECT partner_id::text AS partner FROM booking.outbox WHERE event_type = 'allotment.released'"
        " AND departure_id = $1::uuid",
        dep_a["id"],
    )
    assert [e["partner"] for e in events] == [other]
    assert (await allotments.release_due(conn, today=DAY1_DATE - timedelta(days=5)))["released"] == 0
