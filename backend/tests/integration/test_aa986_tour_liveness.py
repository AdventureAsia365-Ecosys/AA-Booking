"""AA-986 L5 (root ADR 0015 decision 5, Q4) on a real PostgreSQL, as role ``booking``.

``catalog.v_tour_retired`` and ``catalog.v_tour_lifecycle_outbox`` (CIS 274) are stood in by plain tables with the
columns Booking selects, created by the ``lifecycle_views`` fixture around each test that needs them and dropped
after (lessons.md: stand-ins only around the code that needs them); ``catalog.v_tour`` is the CI stub table.

- without CIS 274 (views missing): a plan whose tour left ``catalog.v_tour`` is 409 ``tour_not_served`` /
  ``not_in_catalog``, the delta feed and the staff reads keep working (degraded, no error);
- with CIS 274: retired → new quote, new hold and confirm refused (409, reason), a confirmed booking kept and its
  replay answered, the delta lists the product as withdrawn, the lifecycle pull notifies staff once per open trip case
  and per plan (idempotent), the case shows the alert, the plan is in the orphaned filter; paused → refused but not
  withdrawn; restored → sold again, listed live again, ``tour.reinstated`` sent.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio

from app.catalog import lifecycle, liveness
from app.db import Database
from app.jobs.queue import Job
from tests.integration.test_bk020_b2b import (  # noqa: F401 — fixtures admin, client, plan
    DAY1,
    SOURCE_ID,
    _departure,
    _hold_body,
    _m2m,
    _ok,
    _plan_line,
    _problem,
    _quote,
    _staff,
    admin,
    client,
    plan,
)
from tests.staff_tokens import LocalCis, LocalM2M

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

Client = tuple[httpx.AsyncClient, LocalCis, LocalM2M, Database]
STUBS = """
CREATE TABLE catalog.v_tour_retired (
    source_system text NOT NULL DEFAULT 'cis', source_id text NOT NULL, tour_id uuid,
    state text NOT NULL, reason text, changed_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE catalog.v_tour_lifecycle_outbox (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, idempotency_key text NOT NULL UNIQUE,
    source_system text NOT NULL DEFAULT 'cis', source_id text NOT NULL, tour_id uuid, event text NOT NULL,
    reason text NOT NULL, replaced_by_tour_id uuid, occurred_at timestamptz NOT NULL DEFAULT now());
GRANT SELECT ON catalog.v_tour_retired, catalog.v_tour_lifecycle_outbox TO booking;
"""
DROP = "DROP TABLE IF EXISTS catalog.v_tour_retired; DROP TABLE IF EXISTS catalog.v_tour_lifecycle_outbox;"
ADMIN_ID = "it-973-admin"


async def _forget(conn: asyncpg.Connection) -> None:
    await conn.execute("DELETE FROM booking.notifications WHERE type LIKE 'tour.%'")
    await conn.execute("DELETE FROM booking.inbox_events WHERE source = 'cis'")
    await conn.execute("DELETE FROM booking.inbox_cursor WHERE source = 'cis'")
    await conn.execute("DELETE FROM booking.job WHERE kind = $1", lifecycle.KIND)


@pytest_asyncio.fixture
async def lifecycle_views(admin: asyncpg.Connection) -> AsyncIterator[asyncpg.Connection]:  # noqa: F811
    await admin.execute(DROP)
    await admin.execute(STUBS)
    liveness.reset_cache()
    await _forget(admin)
    try:
        yield admin
    finally:
        await _forget(admin)
        await admin.execute(DROP)
        liveness.reset_cache()


async def _pull(db: Database) -> dict[str, Any]:
    return await lifecycle.pull(await db.pool(), Job("it", lifecycle.KIND, {}, 1, 5))


async def _event(conn: asyncpg.Connection, key: str, event: str, reason: str) -> None:
    await conn.execute(
        "INSERT INTO catalog.v_tour_lifecycle_outbox (idempotency_key, source_id, event, reason)"
        " VALUES ($1, $2, $3, $4)",
        key,
        SOURCE_ID,
        event,
        reason,
    )


async def test_without_cis_274_a_tour_out_of_the_catalog_is_refused_and_reads_degrade(
    client: Client,  # noqa: F811
    admin: asyncpg.Connection,  # noqa: F811
    plan: dict[str, str],  # noqa: F811
) -> None:
    c, cis, m2m, _ = client
    await admin.execute(DROP)
    liveness.reset_cache()
    await _ok(await _quote(c, m2m, "L5-0", [_plan_line("tour", plan["plan"], None)]), 201)
    await admin.execute("DELETE FROM catalog.v_tour WHERE source_id = $1", SOURCE_ID)
    body = _problem(await _quote(c, m2m, "L5-0", [_plan_line("tour", plan["plan"], None)]), 409, "tour_not_served")
    assert body["tour_state"] == "not_in_catalog" and body["tour"]["source_id"] == SOURCE_ID
    since = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    products = await c.get(
        "/b2b/v1/products", params={"updated_since": since}, headers=_m2m(m2m, scope="booking.b2b/read")
    )
    assert products.status_code == 200 and products.json()["items"] == []
    orphaned = await _ok(await c.get("/catalog/trip-plans?orphaned=true", headers=_staff(cis, ADMIN_ID)))
    [item] = orphaned["items"]
    assert item["source_id"] == SOURCE_ID and item["catalog"]["state"] == "not_in_catalog"
    pulled = await _pull(client[3])
    assert pulled["outcome"] == "skipped"


async def test_retire_pause_restore_end_to_end(
    client: Client,  # noqa: F811
    lifecycle_views: asyncpg.Connection,
    plan: dict[str, str],  # noqa: F811
) -> None:
    c, cis, m2m, db = client
    admin_conn = lifecycle_views
    staff = _staff(cis, ADMIN_ID)
    dep = await _departure(c, cis, DAY1, 10)

    # Served: sell one booking, leave one hold active, keep one quote un-held.
    sold_quote = await _ok(await _quote(c, m2m, "L5-1", [_plan_line("tour", plan["plan"], dep["id"])]), 201)
    sold_hold = await _ok(
        await c.post("/b2b/v1/holds", json=_hold_body(sold_quote, "L5-1"), headers=_m2m(m2m, "l5-key-h1")), 201
    )
    confirm = {"price_hash": sold_quote["price_hash"]}
    confirm_url = f"/b2b/v1/holds/{sold_hold['hold_id']}/confirm"
    booking = await _ok(await c.post(confirm_url, json=confirm, headers=_m2m(m2m, "l5-key-c1")), 201)
    open_quote = await _ok(await _quote(c, m2m, "L5-2", [_plan_line("tour", plan["plan"], dep["id"])]), 201)
    open_hold = await _ok(
        await c.post("/b2b/v1/holds", json=_hold_body(open_quote, "L5-2"), headers=_m2m(m2m, "l5-key-h2")), 201
    )
    spare_quote = await _ok(await _quote(c, m2m, "L5-3", [_plan_line("tour", plan["plan"], dep["id"])]), 201)
    case_id = booking["booking_id"]
    await admin_conn.execute(
        "INSERT INTO booking.trip_case_legs (trip_case_id, position, source_tour_id, day_from, day_to, source_system,"
        " source_id) VALUES ($1::uuid, 1, $2, 1, 1, 'cis', $2)",
        case_id,
        SOURCE_ID,
    )
    assert (await _ok(await c.get(f"/trip-cases/{case_id}", headers=staff)))["tour_alerts"] == []

    # CIS retires the tour (source deleted): it leaves v_tour, gets a tombstone and an outbox event.
    await admin_conn.execute("DELETE FROM catalog.v_tour WHERE source_id = $1", SOURCE_ID)
    await admin_conn.execute(
        "INSERT INTO catalog.v_tour_retired (source_id, state, reason) VALUES ($1, 'retired', 'source_deleted')",
        SOURCE_ID,
    )
    await _event(admin_conn, "tle:1", "retired", "source_deleted")

    refused = _problem(
        await _quote(c, m2m, "L5-4", [_plan_line("tour", plan["plan"], dep["id"])]), 409, "tour_not_served"
    )
    assert (refused["tour_state"], refused["reason"]) == ("retired", "source_deleted")
    _problem(
        await c.post("/b2b/v1/holds", json=_hold_body(spare_quote, "L5-3"), headers=_m2m(m2m, "l5-key-h3")),
        409,
        "tour_not_served",
    )
    open_confirm = f"/b2b/v1/holds/{open_hold['hold_id']}/confirm"
    _problem(
        await c.post(open_confirm, json={"price_hash": open_quote["price_hash"]}, headers=_m2m(m2m, "l5-key-c2")),
        409,
        "tour_not_served",
    )
    # Kept: the confirmed booking answers its replay; the open hold is still active (it expires on its own).
    again = await _ok(await c.post(confirm_url, json=confirm, headers=_m2m(m2m, "l5-key-c1b")), 201)
    assert again["booking_id"] == case_id
    held = await _ok(await c.get(f"/b2b/v1/holds/{open_hold['hold_id']}", headers=_m2m(m2m)))
    assert held["status"] == "active"
    # A replay of the hold with its own key is not a new hold: answered.
    replay = await c.post("/b2b/v1/holds", json=_hold_body(open_quote, "L5-2"), headers=_m2m(m2m, "l5-key-h2"))
    assert replay.status_code == 201, replay.text

    # Delta feed: withdrawn; full list: absent.
    since = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    delta = await _ok(
        await c.get("/b2b/v1/products", params={"updated_since": since}, headers=_m2m(m2m, scope="booking.b2b/read"))
    )
    [withdrawn] = delta["items"]
    assert withdrawn["withdrawn"] is True and withdrawn["withdrawn_reason"] == "source_deleted"
    assert withdrawn["sellable"] is False and withdrawn["components"] == []
    full = await _ok(await c.get("/b2b/v1/products", headers=_m2m(m2m, scope="booking.b2b/read")))
    assert full["items"] == []

    # Lifecycle pull: scheduled because the outbox has news; notifies once per open case and per plan.
    pool = await db.pool()
    assert await lifecycle.PullSchedule()(pool) is not None
    first = await _pull(db)
    assert first["applied"] == {"notified": 1}
    second = await _pull(db)
    assert second["applied"] == {} and second["scanned"] == 0
    assert await lifecycle.PullSchedule(now=lambda: datetime.now(UTC) + timedelta(minutes=2))(pool) is None
    feed = await _ok(await c.get("/notifications", params={"type": "tour.retired"}, headers=staff))
    by_entity = {n["entity_type"]: n for n in feed["items"]}
    assert set(by_entity) == {"trip_case", "trip_plan"} and len(feed["items"]) == 2
    assert by_entity["trip_case"]["entity_id"] == case_id and sorted(by_entity["trip_case"]["data"]["via"]) == [
        "item",
        "leg",
    ]
    assert (
        by_entity["trip_plan"]["data"]["confirmed_holds"] == 1 and by_entity["trip_plan"]["data"]["active_holds"] == 1
    )
    status = await admin_conn.fetchval("SELECT status FROM booking.trip_cases WHERE id = $1::uuid", case_id)
    assert status == "confirmed"  # nothing cancelled (Q4)

    # Staff reads: the case flags the leg and lists the alert; the plan is orphaned with its reason.
    case = await _ok(await c.get(f"/trip-cases/{case_id}", headers=staff))
    assert case["legs"][0]["catalog"]["state"] == "retired"
    [alert] = case["tour_alerts"]
    assert alert["source_id"] == SOURCE_ID and alert["used_by"] == ["item", "leg"]
    orphaned = await _ok(await c.get("/catalog/trip-plans?orphaned=true", headers=staff))
    [item] = orphaned["items"]
    assert item["catalog"]["reason"] == "source_deleted" and item["confirmed_holds"] == 1 and item["open_cases"] == 1
    facets = await _ok(await c.get("/catalog/trip-plans/facets", headers=staff))
    assert facets["orphaned"] == 1

    # Paused (Master inactive): refused, but never listed as withdrawn.
    await admin_conn.execute(
        "UPDATE catalog.v_tour_retired SET state = 'paused', reason = 'master_inactive' WHERE source_id = $1", SOURCE_ID
    )
    paused = _problem(
        await _quote(c, m2m, "L5-5", [_plan_line("tour", plan["plan"], dep["id"])]), 409, "tour_not_served"
    )
    assert paused["tour_state"] == "paused" and "temporarily" in paused["message"]
    delta = await _ok(
        await c.get("/b2b/v1/products", params={"updated_since": since}, headers=_m2m(m2m, scope="booking.b2b/read"))
    )
    assert delta["items"] == []
    await _event(admin_conn, "tle:2", "paused", "master_inactive")
    assert (await _pull(db))["applied"] == {"ignored": 1}

    # Restored: back in v_tour (its updated_at moves), no tombstone; sold again, listed live, staff told.
    await admin_conn.execute("DELETE FROM catalog.v_tour_retired WHERE source_id = $1", SOURCE_ID)
    await admin_conn.execute(
        "INSERT INTO catalog.v_tour (source_id, title, country, duration_days, updated_at)"
        " VALUES ($1, 'Hanoi 1D', 'Vietnam', 1, now())",
        SOURCE_ID,
    )
    await _event(admin_conn, "tle:3", "restored", "restore")
    assert (await _pull(db))["applied"] == {"notified": 1}
    told = await _ok(await c.get("/notifications", params={"type": "tour.reinstated"}, headers=staff))
    assert len(told["items"]) == 2
    await _ok(await _quote(c, m2m, "L5-6", [_plan_line("tour", plan["plan"], dep["id"])]), 201)
    delta = await _ok(
        await c.get("/b2b/v1/products", params={"updated_since": since}, headers=_m2m(m2m, scope="booking.b2b/read"))
    )
    [live] = delta["items"]
    assert live["withdrawn"] is False and live["sellable"] is True
    assert (await _ok(await c.get(f"/trip-cases/{case_id}", headers=staff)))["tour_alerts"] == []
