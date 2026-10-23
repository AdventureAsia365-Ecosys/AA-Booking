"""AA-975 (bk-022): the B2B event feed, the on-request desk and the departure incident flag end to end as role
``booking`` on a real PostgreSQL (schema checks: ``test_bk022_schema.py``). Reuses the AA-973 fixtures (a sellable
plan with a room and a transfer, an active partner bound to the CRM client).

- every producer writes its event in the transaction of the change: confirm (``line.confirmed`` naming the booking),
  extend (``hold.extended``), the expiry job (``hold.expired``), the desk (``line.declined`` with its code,
  ``line.confirmed``), staff departure edits (``departure.guaranteed``), the incident flag (``departure.incident`` /
  ``departure.incident_cleared``, fanned out to the agencies with seats) and cancel (``booking.cancelled``);
- Linear R: a non-CRM client → 403 (and a token without ``booking.b2b/read``);
- Linear E: replaying from an old cursor yields the same events again, each once, in the same order; an incident
  event carries nothing but ids and the code (never traveller data);
- the feed order is the commit order: a row committed after a later-id row is still read after it, never skipped;
- each agency reads only its own events; the desk answers are idempotent and audited; one open incident per departure.

Dates are relative to today (lessons.md S245): departures start in 30 / 37 days (bk-020 module constants).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio

from app.b2b import expiry
from app.db import Database
from tests.integration.test_bk020_b2b import (  # noqa: F401 — fixtures admin, client, plan are used by name
    DAY1,
    DAY2,
    MAKER,
    SALES,
    TENANT,
    _departure,
    _hold_body,
    _insert,
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
from tests.staff_tokens import CRM_CLIENT, OTHER_M2M_CLIENT, LocalCis, LocalM2M

FIXTURES = (admin, client, plan)  # the AA-973 fixtures, used by name below

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

Client = tuple[httpx.AsyncClient, LocalCis, LocalM2M, Database]
MIGRATION = Path(__file__).resolve().parents[3] / "migrations" / "022_b2b_event_feed_desk.sql"
OTHER_TENANT = "e2000000-0000-4000-8000-000000000975"
CREATOR = "it-975-creator"
EVENT_KEYS = {
    "event_id",
    "cursor",
    "type",
    "occurred_at",
    "hold_id",
    "booking_id",
    "line_ref",
    "departure_id",
    "incident_code",
    "decline_code",
    "new_start_time",
}


@pytest_asyncio.fixture
async def desk(admin: asyncpg.Connection, plan: dict[str, str]) -> AsyncIterator[dict[str, str]]:
    """bk-022 again (re-runnable), a content creator, and our rows cleaned before the bk-020 fixture deletes
    departures, holds and partners (incidents reference departures; the outbox has no FKs)."""
    await admin.execute(MIGRATION.read_text(encoding="utf-8"))
    await admin.execute(
        "INSERT INTO booking.staff_users (cis_user_id, role, active, name) VALUES ($1, 'content_creator', true, $1)"
        " ON CONFLICT (cis_user_id) DO UPDATE SET role = EXCLUDED.role, active = true",
        CREATOR,
    )
    try:
        yield plan
    finally:
        async with admin.transaction():
            owner = await admin.fetchval("SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = 'booking'")
            await admin.execute(f'SET LOCAL ROLE "{owner}"')
            await admin.execute("DELETE FROM booking.departure_incidents")
            await admin.execute("DELETE FROM booking.outbox")
            await admin.execute("DELETE FROM booking.notifications WHERE type = 'b2b.on_request'")


async def _feed(c: httpx.AsyncClient, m2m: LocalM2M, after: str | None = None, **h: Any) -> dict[str, Any]:
    url = "/b2b/v1/events" + (f"?after={after}" if after else "")
    return await _ok(await c.get(url, headers={**_m2m(m2m), **h}))


async def _book(c: httpx.AsyncClient, m2m: LocalM2M, ref: str, lines: list[dict[str, Any]]) -> dict[str, Any]:
    quote = await _ok(await _quote(c, m2m, ref, lines), 201)
    placed = await c.post("/b2b/v1/holds", json=_hold_body(quote, ref), headers=_m2m(m2m, f"{ref}-hold-key"))
    hold = await _ok(placed, 201)
    url = f"/b2b/v1/holds/{hold['hold_id']}/confirm"
    body = {"price_hash": quote["price_hash"]}
    return await _ok(await c.post(url, json=body, headers=_m2m(m2m, f"{ref}-confirm-key")), 201)


def _room(room: str, day: str = DAY1) -> dict[str, Any]:
    return {"line_ref": "room", "component_id": f"rt:{room}", "service_date": day, "pax": {"adult": 2}}


async def test_feed_desk_and_incident_end_to_end(
    client: Client, admin: asyncpg.Connection, desk: dict[str, str]
) -> None:
    c, cis, m2m, db = client
    sales, creator = _staff(cis, SALES), _staff(cis, CREATOR)
    dep1 = await _departure(c, cis, DAY1, 6)
    dep2 = await _departure(c, cis, DAY2, 6)

    # Linear R: a non-CRM client and a token without the read scope are refused before anything is read.
    _problem(await c.get("/b2b/v1/events", headers=_m2m(m2m, client_id=OTHER_M2M_CLIENT)), 403, "forbidden")
    _problem(await c.get("/b2b/v1/events", headers=_m2m(m2m, scope="booking.b2b/hold")), 403, "forbidden")
    start = await _feed(c, m2m)
    assert start == {"items": [], "next_cursor": "bk1.0"}

    # Confirm: the tour line on an auto-confirm departure is confirmed at once, the room (no departure) goes on request.
    booking = await _book(c, m2m, "B3-1", [_plan_line("tour", desk["plan"], dep1["id"]), _room(desk["room"])])
    assert {line["line_ref"]: line["status"] for line in booking["lines"]} == {
        "tour": "confirmed",
        "room": "on_request",
    }

    # Extend + expire another hold (the expiry job writes the status, the trigger the event).
    quote = await _ok(await _quote(c, m2m, "B3-2", [_plan_line("tour", desk["plan"], dep2["id"], DAY2)]), 201)
    placed = await c.post("/b2b/v1/holds", json=_hold_body(quote, "B3-2"), headers=_m2m(m2m, "b3-2-hold-key"))
    hold2 = await _ok(placed, 201)
    url = f"/b2b/v1/holds/{hold2['hold_id']}/extend"
    await _ok(await c.post(url, json={"reason": "payment_claimed"}, headers=_m2m(m2m, "b3-2-extend-key")))
    await admin.execute(
        "UPDATE booking.b2b_holds SET created_at = now() - interval '2 hours', expires_at = now() - interval '1 hour'"
        " WHERE id = $1::uuid",
        hold2["hold_id"],
    )
    assert await expiry.expire_due(await db.pool()) == 1

    # The desk: the room line is open with its deadline; a content creator may not read it.
    assert (await c.get("/b2b-desk/on-request", headers=creator)).status_code == 403
    page = await _ok(await c.get("/b2b-desk/on-request", headers=sales))
    [line] = [i for i in page["items"] if i["hold_id"] == booking["hold_id"]]
    assert line["line_ref"] == "room" and line["status"] == "on_request" and line["sla"] in ("on_time", "due_soon")
    assert line["trip_case"] == {"id": booking["booking_id"], "code": booking["trip_case_code"]}
    assert line["partner"]["agency_name"] == "Agency 973" and line["component"]["kind"] == "hotel_room"
    assert line["component"]["label"] == "Hanoi Emerald · Deluxe"
    assert line["component"]["supplier"]["name"] == "Red River DMC"
    due = datetime.fromisoformat(line["answer_due_at"])
    assert timedelta(hours=23) < due - datetime.now(UTC) < timedelta(days=4)  # 1 business day (a weekend adds 2)
    summary = await _ok(await c.get("/b2b-desk/summary", headers=sales))
    assert summary["on_request"]["open"] == 1 and summary["open_incidents"] == 0
    notified = await admin.fetchval(
        "SELECT count(*) FROM booking.notifications WHERE type = 'b2b.on_request' AND entity_id = $1::uuid",
        booking["booking_id"],
    )
    assert notified == 1

    line_url = f"/b2b-desk/on-request/{booking['hold_id']}/lines/room"
    assert (await c.post(f"{line_url}/accept", headers=creator)).status_code == 403
    bad = await c.post(f"{line_url}/decline", json={"decline_code": "because"}, headers=sales)
    assert bad.status_code == 422
    declined = await _ok(
        await c.post(f"{line_url}/decline", json={"decline_code": "supplier_unavailable"}, headers=sales)
    )
    assert declined["status"] == "declined" and declined["decline_code"] == "supplier_unavailable"
    assert declined["answered_by"]["id"] == SALES and declined["sla"] == "answered"
    again = await _ok(await c.post(f"{line_url}/decline", json={"decline_code": "supplier_unavailable"}, headers=sales))
    assert again["answered_at"] == declined["answered_at"]  # the same answer again: unchanged
    assert (await c.post(f"{line_url}/accept", headers=sales)).status_code == 409
    missing = f"/b2b-desk/on-request/{booking['hold_id']}/lines/tour/accept"  # not an on-request line
    assert (await c.post(missing, headers=sales)).status_code == 404
    answered = await _ok(await c.get("/b2b-desk/on-request?state=answered", headers=sales))
    assert [i["line_ref"] for i in answered["items"]] == ["room"]
    status = await c.get(f"/b2b/v1/holds/{booking['hold_id']}", headers=_m2m(m2m))
    assert status.status_code == 200

    # Staff guarantee departure 1 (the agency has seats on it); departure 2 has none (its hold expired): no event.
    await _ok(
        await c.patch(f"/catalog/departures/{dep1['id']}", json={"status": "guaranteed"}, headers=_staff(cis, MAKER))
    )
    await _ok(
        await c.patch(f"/catalog/departures/{dep2['id']}", json={"status": "guaranteed"}, headers=_staff(cis, MAKER))
    )

    # Incident flag: one open per departure, clear, partners told; content creators cannot flag.
    flag_url = f"/b2b-desk/departures/{dep1['id']}/incidents"
    assert (await c.post(flag_url, json={"incident_code": "road"}, headers=creator)).status_code == 403
    assert (await c.post(flag_url, json={"incident_code": "road", "note": "x"}, headers=sales)).status_code == 422
    incident = await _ok(await c.post(flag_url, json={"incident_code": "road"}, headers=sales), 201)
    assert incident["partners_notified"] == 1 and incident["cleared_at"] is None
    dup = await c.post(flag_url, json={"incident_code": "weather"}, headers=sales)
    assert dup.status_code == 409 and dup.json()["detail"] == "incident_open"
    deps = await _ok(await c.get("/b2b-desk/departures", headers=sales))
    [row1] = [d for d in deps["items"] if d["id"] == dep1["id"]]
    assert row1["open_incident"]["id"] == incident["id"] and row1["partners"] == 1
    assert row1["tour_title"] == "Hanoi 1D" and row1["status"] == "guaranteed"
    cleared = await _ok(await c.post(f"/b2b-desk/incidents/{incident['id']}/clear", headers=sales))
    assert cleared["cleared_by"]["id"] == SALES
    assert (await _ok(await c.post(f"/b2b-desk/incidents/{incident['id']}/clear", headers=sales))) == cleared
    history = await _ok(await c.get(f"/b2b-desk/departures/{dep1['id']}/incidents", headers=sales))
    assert [h["id"] for h in history["items"]] == [incident["id"]]

    # Cancel the booking (AA-974): booking.cancelled.
    cancel = f"/b2b/v1/bookings/{booking['booking_id']}/cancel"
    await _ok(await c.post(cancel, json={"reason_code": "client_cancelled"}, headers=_m2m(m2m, "b3-1-cancel-key")))

    # The feed, in commit order.
    feed = await _feed(c, m2m)
    got = [(e["type"], e["hold_id"], e["line_ref"], e["departure_id"]) for e in feed["items"]]
    h1, h2 = booking["hold_id"], hold2["hold_id"]
    assert got == [
        ("line.confirmed", h1, "tour", dep1["id"]),
        ("hold.extended", h2, None, None),
        ("hold.expired", h2, None, None),
        ("line.declined", h1, "room", None),
        ("departure.guaranteed", None, None, dep1["id"]),
        ("departure.incident", None, None, dep1["id"]),
        ("departure.incident_cleared", None, None, dep1["id"]),
        ("booking.cancelled", h1, None, None),
    ]
    by_type = {e["type"]: e for e in feed["items"]}
    assert by_type["line.confirmed"]["booking_id"] == booking["booking_id"]
    assert by_type["line.declined"]["decline_code"] == "supplier_unavailable"
    assert by_type["booking.cancelled"]["booking_id"] == booking["booking_id"]
    # Linear E: an incident event is ids and the code only.
    incident_event = by_type["departure.incident"]
    assert set(incident_event) == EVENT_KEYS and incident_event["incident_code"] == "road"
    assert {k for k, v in incident_event.items() if v is not None} == {
        "event_id", "cursor", "type", "occurred_at", "departure_id", "incident_code",
    }  # fmt: skip
    assert all(set(e) == EVENT_KEYS for e in feed["items"])
    assert feed["next_cursor"] == feed["items"][-1]["cursor"]

    # Linear E: replay from an old cursor → the same events again, each once, same ids and order.
    third = feed["items"][2]["cursor"]
    replay = await _feed(c, m2m, third)
    assert [e["event_id"] for e in replay["items"]] == [e["event_id"] for e in feed["items"][3:]]
    assert len({e["event_id"] for e in replay["items"]}) == len(replay["items"])
    paged, cursor = [], None
    for _ in range(20):
        page = await _ok(
            await c.get("/b2b/v1/events?limit=3" + (f"&after={cursor}" if cursor else ""), headers=_m2m(m2m))
        )
        paged += page["items"]
        if not page["items"]:
            assert page["next_cursor"] == cursor
            break
        cursor = page["next_cursor"]
    assert [e["event_id"] for e in paged] == [e["event_id"] for e in feed["items"]]
    tail = await _feed(c, m2m, feed["next_cursor"])
    assert tail == {"items": [], "next_cursor": feed["next_cursor"]}

    # Bad cursors are 422; another agency reads none of these events.
    _problem(await c.get("/b2b/v1/events?after=12", headers=_m2m(m2m)), 422, "invalid")
    _problem(await c.get("/b2b/v1/events?after=bk1.999999999", headers=_m2m(m2m)), 422, "invalid")
    _problem(await c.get("/b2b/v1/events?limit=501", headers=_m2m(m2m)), 422, "invalid")
    await _insert(admin, "b2b_partners", tenant_id=OTHER_TENANT, agency_name="Agency 975", m2m_client_id=CRM_CLIENT)
    other = await _ok(await c.get("/b2b/v1/events", headers={**_m2m(m2m), "X-AA-Tenant-Id": OTHER_TENANT}))
    assert other == {"items": [], "next_cursor": "bk1.0"}
    # "Ahead" is checked against the caller's own head: a cursor of the first agency is ahead for this one, so the 422
    # reveals nothing about the global sequence.
    foreign = {**_m2m(m2m), "X-AA-Tenant-Id": OTHER_TENANT}
    _problem(await c.get(f"/b2b/v1/events?after={feed['next_cursor']}", headers=foreign), 422, "invalid")
    assert TENANT != OTHER_TENANT

    # Audit rows of the desk.
    actions = await admin.fetch(
        "SELECT action FROM booking.audit_logs WHERE action LIKE 'b2b_line.%' OR action LIKE 'departure.incident%'"
        " ORDER BY created_at"
    )
    assert [a["action"] for a in actions] == [
        "b2b_line.declined",
        "departure.incident_flagged",
        "departure.incident_cleared",
    ]


async def test_accept_confirms_the_line(client: Client, admin: asyncpg.Connection, desk: dict[str, str]) -> None:
    c, cis, m2m, _ = client
    booking = await _book(c, m2m, "B3-3", [_room(desk["room"])])
    url = f"/b2b-desk/on-request/{booking['hold_id']}/lines/room/accept"
    accepted = await _ok(await c.post(url, headers=_staff(cis, SALES)))
    assert accepted["status"] == "confirmed" and accepted["decline_code"] is None
    assert (await _ok(await c.post(url, headers=_staff(cis, SALES))))["answered_at"] == accepted["answered_at"]
    hold = await _ok(await c.get(f"/b2b/v1/holds/{booking['hold_id']}", headers=_m2m(m2m)))
    assert hold["status"] == "confirmed"
    feed = await _feed(c, m2m)
    assert [(e["type"], e["line_ref"], e["booking_id"]) for e in feed["items"]] == [
        ("line.confirmed", "room", booking["booking_id"])
    ]
    open_page = await _ok(await c.get("/b2b-desk/on-request", headers=_staff(cis, SALES)))
    assert all(i["hold_id"] != booking["hold_id"] for i in open_page["items"])


async def test_feed_order_is_commit_order(
    client: Client, admin: asyncpg.Connection, desk: dict[str, str], migrated_booking_dsn: str
) -> None:
    """A transaction that inserted first (smaller id) but commits last is still delivered, after the earlier commit."""
    c, _, m2m, _ = client
    insert = (
        "INSERT INTO booking.outbox (stream, event_type, partner_id, departure_id)"
        " VALUES ('b2b', 'departure.guaranteed', $1::uuid, gen_random_uuid()) RETURNING id"
    )
    slow, fast = await asyncpg.connect(migrated_booking_dsn), await asyncpg.connect(migrated_booking_dsn)
    try:
        tx = slow.transaction()
        await tx.start()
        slow_id = await slow.fetchval(insert, desk["partner"])
        fast_id = await fast.fetchval(insert, desk["partner"])
        assert slow_id < fast_id
        first = await _feed(c, m2m)
        assert len(first["items"]) == 1  # only the committed row; the open one is not stamped
        await tx.commit()
        second = await _feed(c, m2m, first["next_cursor"])
        assert len(second["items"]) == 1
        ids = await admin.fetch(
            "SELECT id FROM booking.outbox WHERE partner_id = $1::uuid ORDER BY feed_seq", desk["partner"]
        )
        assert [r["id"] for r in ids] == [fast_id, slow_id]  # feed order = commit order, not id order
        replay = await _feed(c, m2m)
        assert [e["event_id"] for e in replay["items"]] == [
            first["items"][0]["event_id"],
            second["items"][0]["event_id"],
        ]
    finally:
        await slow.close()
        await fast.close()


async def test_booking_role_cannot_rewrite_or_delete_events(
    admin: asyncpg.Connection, desk: dict[str, str], migrated_booking_dsn: str
) -> None:
    app = await asyncpg.connect(migrated_booking_dsn)
    try:
        row_id = await app.fetchval(
            "INSERT INTO booking.outbox (stream, event_type, partner_id, hold_id) VALUES ('b2b', 'hold.expired',"
            " $1::uuid, gen_random_uuid()) RETURNING id",
            desk["partner"],
        )
        for sql in (
            "UPDATE booking.outbox SET event_type = 'hold.extended' WHERE id = $1",
            "DELETE FROM booking.outbox WHERE id = $1",
        ):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await app.execute(sql, row_id)
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await app.execute(
                "INSERT INTO booking.outbox (stream, event_type, partner_id, hold_id, feed_seq) VALUES ('b2b',"
                " 'hold.expired', $1::uuid, gen_random_uuid(), 1)",
                desk["partner"],
            )
        with pytest.raises(asyncpg.CheckViolationError):  # a b2b event needs its partner
            await app.execute(
                "INSERT INTO booking.outbox (stream, event_type, hold_id)"
                " VALUES ('b2b', 'hold.expired', gen_random_uuid())"
            )
    finally:
        await app.close()
