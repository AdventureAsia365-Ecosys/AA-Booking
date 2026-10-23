"""Tour lifecycle events from CIS → staff notifications (AA-986 slice L5; root ADR 0015 decisions 4–5, Q4).

``bk_tour_lifecycle_pull`` reads ``catalog.v_tour_lifecycle_outbox`` (CIS 274, contract catalog.tour.v1.3) as role
``booking``, the outbox → pull → inbox pattern of the inquiry intake (ADR 0001, 0008; contracts rules 3–4):

- every outbox row not consumed yet goes into ``booking.inbox_events`` (``source = 'cis'``, ``contract =
  'catalog.tour'``, the producer's ``idempotency_key``; ``ON CONFLICT DO NOTHING``) and is applied **in the same
  transaction**, then marked ``processed``. Idempotent: a second run, an overlapping worker or a retry finds the inbox
  row and does nothing. The producer id is never the only cursor: ``booking.inbox_cursor.safe_id`` is a scan bound
  that only advances over rows older than :data:`SAFE_AFTER_SECONDS` (a lower id can still commit until then).
- ``retired``: one staff notification (shared staff queue, type ``tour.retired``) per **open** trip case that uses
  the key (a leg, the case's own source key, or an item of one of its versions) and one per **trip plan** of the
  key (with its confirmed bookings and active B2B holds on future departures; an active hold can no longer be
  confirmed, so the text tells staff to contact the agency and ``data.active_hold_list`` names the holds and
  agencies). Nothing is cancelled or changed: confirmed bookings, holds and trip cases are kept (Q4); cancelling is
  a staff decision. The flag itself is derived at read
  time (``app.catalog.liveness``), so it needs no column and goes away by itself on restore.
- ``restored``: when Booking told staff about the retirement, the same cases and plan get ``tour.reinstated``.
- ``paused`` and ``superseded``: nothing (ADR 0015: a pause flags no staff and pushes nothing; a supersede is
  invisible, the key stays the same).

Scheduling (:class:`PullSchedule`, a worker maintenance hook, like ``bk_b2b_hold_expire``): on each maintenance pass,
only when the outbox view is there (feature detection, :func:`app.catalog.liveness.view_ready`) and has a row the
inbox does not hold, it queues one run with the idempotency key ``bk_tour_lifecycle_pull:<UTC minute>``. No job row is
written while nothing is new, and nothing at all before CIS 274 is applied (the job then answers ``skipped``).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import asyncpg

from app import audit
from app.catalog import liveness
from app.jobs import queue
from app.jobs.queue import Job
from app.notifications import service as notifications

log = logging.getLogger(__name__)

KIND = "bk_tour_lifecycle_pull"
SOURCE = "cis"
CONTRACT = "catalog.tour"
CONTRACT_VERSION = "1.3"
SCHEDULER = "system:tour_lifecycle_pull"
MAX_ATTEMPTS = 5
BATCH = 200
# The CIS lifecycle service writes the event in the transaction of the change: occurred_at is that transaction's
# start, the id is drawn later and visible at commit. Same reasoning and window as the inquiry pull.
MAX_PRODUCER_TX_SECONDS = 300
SAFE_AFTER_SECONDS = 2 * MAX_PRODUCER_TX_SECONDS
MAX_CASES_PER_EVENT = 500

TYPE_RETIRED = "tour.retired"
TYPE_REINSTATED = "tour.reinstated"
OPEN_STATUSES_SQL = "tc.status NOT IN ('completed', 'cancelled')"
REASON_WORDS = {
    "source_deleted": "the source tour was deleted",
    "master_trashed": "its Master content was trashed",
    "superseded_orphan": "it was replaced and no current version is left",
}

OUTBOX = liveness.OUTBOX_VIEW.qualified
PENDING_SQL = f"""
SELECT EXISTS (
    SELECT 1 FROM {OUTBOX} o
    WHERE o.event_id > coalesce((SELECT safe_id FROM booking.inbox_cursor WHERE source = $1 AND contract = $2), 0)
      AND NOT EXISTS (SELECT 1 FROM booking.inbox_events i
                      WHERE i.source = $1 AND i.contract = $2 AND i.idempotency_key = o.idempotency_key))
"""  # noqa: S608 — module constant
SCAN_SQL = f"""
SELECT o.event_id, o.idempotency_key, o.source_system, o.source_id, o.event, o.reason, o.occurred_at
FROM {OUTBOX} o
WHERE o.event_id > $1
  AND NOT EXISTS (SELECT 1 FROM booking.inbox_events i
                  WHERE i.source = $2 AND i.contract = $3 AND i.idempotency_key = o.idempotency_key)
ORDER BY o.event_id
LIMIT $4
"""  # noqa: S608 — module constant
ADVANCE_SQL = f"""
UPDATE booking.inbox_cursor SET safe_id = greatest(safe_id, coalesce((
    SELECT max(o.event_id) FROM {OUTBOX} o
    WHERE o.event_id > $3 AND o.occurred_at < now() - make_interval(secs => $4)), 0))
WHERE source = $1 AND contract = $2
RETURNING safe_id
"""  # noqa: S608 — module constant
# Open trip cases that use the key: a TripPlanner leg (bk-011 key), the case's own key (started from a trip plan),
# or an item of one of its versions (staff "use for trip case" and the B2B confirm carry the plan's source_id).
AFFECTED_CASES_SQL = f"""
SELECT tc.id::text AS id, tc.code, tc.status, tc.channel,
       EXISTS (SELECT 1 FROM booking.trip_case_legs l
               WHERE l.trip_case_id = tc.id AND l.source_system = 'cis' AND l.source_id = $1) AS via_leg,
       (tc.source_system = 'cis' AND tc.source_id = $1) AS via_case,
       EXISTS (SELECT 1 FROM booking.trip_case_versions v
               JOIN booking.trip_case_version_itinerary i ON i.trip_case_version_id = v.id
               WHERE v.trip_case_id = tc.id AND v.deleted_at IS NULL AND i.source_tour_id = $1) AS via_item
FROM booking.trip_cases tc
WHERE tc.deleted_at IS NULL AND {OPEN_STATUSES_SQL}
  AND (EXISTS (SELECT 1 FROM booking.trip_case_legs l
               WHERE l.trip_case_id = tc.id AND l.source_system = 'cis' AND l.source_id = $1)
       OR (tc.source_system = 'cis' AND tc.source_id = $1)
       OR EXISTS (SELECT 1 FROM booking.trip_case_versions v
                  JOIN booking.trip_case_version_itinerary i ON i.trip_case_version_id = v.id
                  WHERE v.trip_case_id = tc.id AND v.deleted_at IS NULL AND i.source_tour_id = $1))
ORDER BY tc.code
LIMIT $2
"""  # noqa: S608 — constant fragment
PLAN_SQL = """
SELECT p.id::text AS id, p.sellable,
       (SELECT count(DISTINCT h.id) FROM booking.b2b_holds h
        JOIN booking.b2b_hold_lines l ON l.hold_id = h.id
        JOIN booking.departures d ON d.id = l.departure_id
        WHERE d.trip_plan_id = p.id AND d.start_date >= $2 AND h.status = 'confirmed') AS confirmed_holds,
       (SELECT count(DISTINCT h.id) FROM booking.b2b_holds h
        JOIN booking.b2b_hold_lines l ON l.hold_id = h.id
        JOIN booking.departures d ON d.id = l.departure_id
        WHERE d.trip_plan_id = p.id AND d.start_date >= $2 AND h.status = 'active' AND h.expires_at > now())
           AS active_holds,
       -- the active holds staff must act on (they can no longer be confirmed): ids and agencies, capped for the bell
       (SELECT coalesce(json_agg(x ORDER BY x.expires_at), '[]'::json)::text FROM (
            SELECT DISTINCT h.id::text AS hold_id, h.expires_at, pa.agency_name
            FROM booking.b2b_holds h
            JOIN booking.b2b_partners pa ON pa.id = h.partner_id
            JOIN booking.b2b_hold_lines l ON l.hold_id = h.id
            JOIN booking.departures d ON d.id = l.departure_id
            WHERE d.trip_plan_id = p.id AND d.start_date >= $2 AND h.status = 'active' AND h.expires_at > now()
            ORDER BY h.expires_at LIMIT 20) x) AS active_hold_list
FROM booking.trip_plans p
WHERE p.source_system = 'cis' AND p.source_id = $1
"""
WAS_TOLD_SQL = (
    "SELECT EXISTS (SELECT 1 FROM booking.notifications WHERE recipient_type = 'staff' AND type = $1"
    " AND data->>'source_id' = $2)"
)


def _payload(row: Any) -> dict[str, Any]:
    """The inbox copy of one outbox row: ids, state names and a time; no personal data."""
    return {
        "contract": CONTRACT,
        "version": CONTRACT_VERSION,
        "idempotency_key": row["idempotency_key"],
        "source": SOURCE,
        "outbox_id": row["event_id"],
        "source_system": row["source_system"],
        "source_id": row["source_id"],
        "event": row["event"],
        "reason": row["reason"],
        "occurred_at": row["occurred_at"].isoformat() if row["occurred_at"] else None,
    }


def _why(reason: str | None) -> str:
    return REASON_WORDS.get(reason or "", reason or "it left the catalog")


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def plan_retired_message(source_id: str, reason: str | None, confirmed: int, active: int) -> str:
    """The staff text for a retired trip plan. Confirmed bookings are kept; active holds are NOT safe: confirm is
    refused (409 ``tour_not_served``) and they expire on their own, so staff must contact the agency (Q4)."""
    text = (
        f"AA no longer serves tour {source_id} ({_why(reason)}). Its trip plan is no longer offered to agencies. "
        f"{_plural(confirmed, 'confirmed booking')} on future departures "
        f"{'is' if confirmed == 1 else 'are'} kept."
    )
    if active:
        text += (
            f" {_plural(active, 'active B2B hold')} can no longer be confirmed and will expire: contact the "
            f"agenc{'y' if active == 1 else 'ies'} now."
        )
    return text + " See Trip plans, filter Orphaned."


def _hold_list(plan: Any) -> list[dict[str, Any]]:
    raw = plan.get("active_hold_list")  # json text from PLAN_SQL (asyncpg Record and dict both have .get)
    if not raw:
        return []
    return json.loads(raw) if isinstance(raw, str) else list(raw)


async def _notify(conn: asyncpg.Connection, kind: str, row: Any, cases: list[Any], plan: Any | None) -> dict[str, int]:
    source_id, reason = row["source_id"], row["reason"]
    base = {"source_system": SOURCE, "source_id": source_id, "reason": reason, "outbox_id": row["event_id"]}
    retired = kind == TYPE_RETIRED
    for case in cases:
        via = [w for w, on in (("leg", case["via_leg"]), ("case", case["via_case"]), ("item", case["via_item"])) if on]
        if retired:
            title = f"Tour withdrawn: {case['code']}"
            message = (
                f"AA no longer serves tour {source_id} ({_why(reason)}). Trip case {case['code']} "
                f"({case['status']}) uses it and is kept as it is; decide with the "
                f"{'agency' if case['channel'] == 'b2b' else 'customer'} what to do (cancelling is a staff decision)."
            )
        else:
            title = f"Tour back in the catalog: {case['code']}"
            message = f"Tour {source_id} is served again; trip case {case['code']} can use it as before."
        await notifications.notify_staff(
            conn,
            type=kind,
            title=title,
            message=message,
            entity_type="trip_case",
            entity_id=case["id"],
            data={**base, "code": case["code"], "status": case["status"], "via": via},
        )
    if plan is not None:
        if retired:
            title = f"Tour withdrawn: trip plan {source_id[:8]}"
            message = plan_retired_message(source_id, reason, plan["confirmed_holds"], plan["active_holds"])
        else:
            title = f"Tour back in the catalog: trip plan {source_id[:8]}"
            message = f"Tour {source_id} is served again; its trip plan is offered as before."
        await notifications.notify_staff(
            conn,
            type=kind,
            title=title,
            message=message,
            entity_type="trip_plan",
            entity_id=plan["id"],
            data={
                **base,
                "sellable": plan["sellable"],
                "confirmed_holds": plan["confirmed_holds"],
                "active_holds": plan["active_holds"],
                "active_hold_list": _hold_list(plan),
            },
        )
    return {"cases": len(cases), "plans": 1 if plan is not None else 0}


async def apply_event(conn: asyncpg.Connection, row: Any, *, today: date) -> dict[str, Any]:
    """Act on one outbox row inside the pull transaction. Returns what was done (job result, audit)."""
    event, source_id = row["event"], row["source_id"]
    if row["source_system"] != SOURCE or event not in ("retired", "restored"):
        return {"outcome": "ignored", "event": event}
    if event == "restored" and not await conn.fetchval(WAS_TOLD_SQL, TYPE_RETIRED, source_id):
        return {"outcome": "ignored", "event": event, "why": "no retirement was notified"}
    cases = await conn.fetch(AFFECTED_CASES_SQL, source_id, MAX_CASES_PER_EVENT)
    plan = await conn.fetchrow(PLAN_SQL, source_id, today)
    if not cases and plan is None:
        return {"outcome": "nothing_affected", "event": event}
    kind = TYPE_RETIRED if event == "retired" else TYPE_REINSTATED
    counts = await _notify(conn, kind, row, list(cases), plan)
    await audit.record(
        conn,
        actor_type="system",
        actor_ref=SCHEDULER,
        action=f"catalog.{kind}",
        entity_type="trip_plan" if plan is not None else "catalog_tour",
        entity_id=plan["id"] if plan is not None else None,
        after={"source_id": source_id, "reason": row["reason"], "outbox_id": row["event_id"], **counts},
        entity_label=f"tour {source_id[:8]}",
        summary=f"CIS tour {event}: notified staff about {counts['cases']} trip case(s), {counts['plans']} plan(s)",
    )
    return {"outcome": "notified", "event": event, **counts, "truncated": len(cases) >= MAX_CASES_PER_EVENT}


async def pull(pool: asyncpg.Pool, job: Job, *, today: date | None = None) -> dict[str, Any]:
    async with pool.acquire() as conn:
        if not await liveness.view_ready(conn, liveness.OUTBOX_VIEW):
            return {"outcome": "skipped", "reason": "catalog.v_tour_lifecycle_outbox not readable (CIS 274)"}
    today = today or datetime.now(UTC).date()
    applied: dict[str, int] = {}
    async with pool.acquire() as conn, conn.transaction(isolation="repeatable_read"):
        # One snapshot for the scan and the cursor advance (same rule as the inquiry pull).
        await conn.execute(
            "INSERT INTO booking.inbox_cursor (source, contract) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            SOURCE,
            CONTRACT,
        )
        safe_id = await conn.fetchval(
            "SELECT safe_id FROM booking.inbox_cursor WHERE source = $1 AND contract = $2 FOR UPDATE",
            SOURCE,
            CONTRACT,
        )
        rows = await conn.fetch(SCAN_SQL, safe_id, SOURCE, CONTRACT, BATCH)
        for row in rows:
            inbox_id = await conn.fetchval(
                """
                INSERT INTO booking.inbox_events
                    (source, contract, idempotency_key, payload, contract_version, source_position)
                VALUES ($1, $2, $3, $4::jsonb, $5, $6)
                ON CONFLICT (source, contract, idempotency_key) DO NOTHING
                RETURNING id::text
                """,
                SOURCE,
                CONTRACT,
                row["idempotency_key"],
                json.dumps(_payload(row)),
                CONTRACT_VERSION,
                row["event_id"],
            )
            if inbox_id is None:
                continue
            result = await apply_event(conn, row, today=today)
            applied[result["outcome"]] = applied.get(result["outcome"], 0) + 1
            await conn.execute(
                "UPDATE booking.inbox_events SET status = 'processed', processed_at = now() WHERE id = $1::uuid",
                inbox_id,
            )
        drained = len(rows) < BATCH
        if drained:
            safe_id = await conn.fetchval(ADVANCE_SQL, SOURCE, CONTRACT, safe_id, SAFE_AFTER_SECONDS)
    if applied:
        log.info("tour lifecycle pull: %s (safe_id=%s)", applied, safe_id)
    return {"scanned": len(rows), "applied": applied, "drained": drained, "safe_id": safe_id}


HANDLERS = {KIND: pull}


def schedule_key(at: datetime) -> str:
    return f"{KIND}:{at.astimezone(UTC):%Y%m%dT%H%M}"


class PullSchedule:
    """Worker maintenance hook: queue one pull when the outbox has a row the inbox lacks (at most one per minute)."""

    def __init__(self, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._now = now

    async def __call__(self, pool: asyncpg.Pool) -> str | None:
        async with pool.acquire() as conn:
            if not await liveness.view_ready(conn, liveness.OUTBOX_VIEW):
                return None
            if not await conn.fetchval(PENDING_SQL, SOURCE, CONTRACT):
                return None
        return await queue.enqueue(
            pool,
            KIND,
            {"trigger": "schedule"},
            idempotency_key=schedule_key(self._now()),
            max_attempts=MAX_ATTEMPTS,
            created_by=SCHEDULER,
        )
