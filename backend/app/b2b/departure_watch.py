"""The daily departure checks (AA-1073, bk-046; S275 study items bk-01 and bk-03).

``bk_departure_watch`` looks at every scheduled departure with a minimum (``min_to_operate >= 1``) that has not started:

- **sold >= minimum** → ``guaranteed`` (the bk-022 trigger tells every agency on it: ``departure.guaranteed``);
- otherwise, **on or after its decision date** (``start_date - decision_days_before``) and not stamped yet → stamped at
  risk (``at_risk_at``; the bk-046 trigger emits ``departure.at_risk`` to the agencies on it), one staff notification
  ``departure.at_risk`` (shared queue, once per departure) and an audit row. Staff decide on the B2B desk: run anyway
  (guaranteed) or cancel. A departure with no minimum (0) is never touched.

Sold = confirmed holds whose line is not declined / cancelled (:func:`app.b2b.departures.usage`); held seats never
guarantee a departure. ``bk_allotment_release`` (:mod:`app.b2b.allotments`) releases the allotments due.

Both kinds are queued once per UTC day by :class:`DepartureDailySchedule` (first worker pass at or after 01:00 UTC; key
``<kind>:<UTC date>``) and by staff (``POST /booking/b2b-desk/departure-checks/runs``, key per kind and UTC minute;
``dry_run`` reports and writes nothing). Departures are locked ``FOR UPDATE SKIP LOCKED`` (a hold in flight keeps its
departure for the next run), at most :data:`BATCH` per run.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import asyncpg

from app import audit
from app.auth.staff import Staff
from app.b2b import allotments, departures
from app.jobs import queue
from app.jobs.queue import Job
from app.notifications import service as notifications

log = logging.getLogger(__name__)

KIND = "bk_departure_watch"
KINDS = (KIND, allotments.KIND)
MAX_ATTEMPTS = 3
SCHEDULE_UTC_HOUR = 1
SCHEDULER = "scheduler:aa1073-departures"
ACTOR = "system:bk_departure_watch"
AT_RISK_TYPE = "departure.at_risk"
BATCH = 500
MAX_LISTED = 50

CANDIDATES_SQL = f"""
SELECT {departures.DEPARTURE_COLUMNS}, p.source_id
FROM booking.departures d
JOIN booking.trip_plans p ON p.id = d.trip_plan_id
WHERE d.status = 'scheduled' AND d.min_to_operate >= 1 AND d.start_date > $1::date
ORDER BY d.start_date, d.id
LIMIT $2
"""  # noqa: S608 — constant fragment


async def run_watch(conn: asyncpg.Connection, *, today: date, dry_run: bool = False) -> dict[str, Any]:
    """One pass (call inside a transaction). Returns what moved (or would move, ``dry_run``)."""
    rows = await conn.fetch(CANDIDATES_SQL + ("" if dry_run else " FOR UPDATE OF d SKIP LOCKED"), today, BATCH)
    used = await departures.usage(conn, [r["id"] for r in rows])
    guaranteed: list[dict[str, Any]] = []
    at_risk: list[dict[str, Any]] = []
    for r in rows:
        sold = used.get(r["id"], (0, 0))[1]
        minimum = int(r["min_to_operate"])
        brief = {"departure_id": r["id"], "start_date": r["start_date"].isoformat(), "sold": sold, "min": minimum}
        if sold >= minimum:
            guaranteed.append(brief)
            if dry_run:
                continue
            await conn.execute("UPDATE booking.departures SET status = 'guaranteed' WHERE id = $1::uuid", r["id"])
            await audit.record(
                conn,
                actor_type="system",
                actor_ref=ACTOR,
                action="departure.guaranteed",
                entity_type="departure",
                entity_id=r["id"],
                before={"status": "scheduled"},
                after={"status": "guaranteed", "sold": sold, "min_to_operate": minimum},
                entity_label=f"{r['source_id'][:60]} {r['start_date']}",
                summary=f"Departure {r['start_date']} guaranteed: {sold} sold, minimum {minimum}",
            )
        elif r["at_risk_at"] is None and departures.decision_date(r) <= today:
            at_risk.append(brief)
            if dry_run:
                continue
            await conn.execute("UPDATE booking.departures SET at_risk_at = now() WHERE id = $1::uuid", r["id"])
            await notifications.notify_staff_once(
                conn,
                type=AT_RISK_TYPE,
                title=f"Departure {r['start_date']} is short of its minimum",
                message=(
                    f"{sold} of {minimum} seats sold on the decision date. Decide on the B2B desk: run it anyway or"
                    " cancel it."
                ),
                entity_type="departure",
                entity_id=r["id"],
                reminder_key="at_risk",
                data={"departure_id": r["id"], "source_id": r["source_id"], "sold": sold, "min_to_operate": minimum},
            )
            await audit.record(
                conn,
                actor_type="system",
                actor_ref=ACTOR,
                action="departure.at_risk",
                entity_type="departure",
                entity_id=r["id"],
                after={"sold": sold, "min_to_operate": minimum, "decision_date": departures.decision_date(r)},
                entity_label=f"{r['source_id'][:60]} {r['start_date']}",
                summary=f"Departure {r['start_date']} at risk: {sold} of {minimum} sold on the decision date",
            )
    return {
        "dry_run": dry_run,
        "day": today.isoformat(),
        "checked": len(rows),
        "guaranteed_count": len(guaranteed),
        "at_risk_count": len(at_risk),
        "guaranteed": guaranteed[:MAX_LISTED],
        "at_risk": at_risk[:MAX_LISTED],
        "more": len(rows) == BATCH,
    }


async def handle_watch(pool: asyncpg.Pool, job: Job) -> dict[str, Any]:
    payload = job.payload or {}
    today = date.fromisoformat(payload["day"]) if payload.get("day") else datetime.now(UTC).date()
    async with pool.acquire() as conn, conn.transaction():
        result = await run_watch(conn, today=today, dry_run=bool(payload.get("dry_run")))
    log.info(
        "departure_watch day=%s guaranteed=%s at_risk=%s dry_run=%s",
        today,
        result["guaranteed_count"],
        result["at_risk_count"],
        result["dry_run"],
    )
    return {"outcome": "ok", "trigger": payload.get("trigger"), **result}


HANDLERS = {KIND: handle_watch, allotments.KIND: allotments.handle_release}


def schedule_key(kind: str, day: date) -> str:
    return f"{kind}:{day.isoformat()}"


class DepartureDailySchedule:
    """Worker maintenance hook: queue one ``trigger=schedule`` run of each kind per UTC day, on the first pass at or
    after :data:`SCHEDULE_UTC_HOUR`. Idempotent across workers and restarts (key per kind and day)."""

    def __init__(self, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._now = now
        self._done_day: date | None = None

    async def __call__(self, pool: asyncpg.Pool) -> list[str]:
        now = self._now().astimezone(UTC)
        today = now.date()
        if now.hour < SCHEDULE_UTC_HOUR or self._done_day == today:
            return []
        queued = []
        for kind in KINDS:
            job_id = await queue.enqueue(
                pool,
                kind,
                {"trigger": "schedule"},
                idempotency_key=schedule_key(kind, today),
                max_attempts=MAX_ATTEMPTS,
                created_by=SCHEDULER,
            )
            if job_id:
                queued.append(job_id)
        self._done_day = today
        if queued:
            log.info("departures: queued %d scheduled runs for %s", len(queued), today)
        return queued


async def enqueue_now(
    conn: asyncpg.Connection, staff: Staff, *, dry_run: bool, now: datetime | None = None
) -> dict[str, Any]:
    """Staff "Run checks now": one run of each kind (key per kind and UTC minute, so a double click queues once)."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    minute = now.strftime("%Y%m%dT%H%M")
    mode = "dry" if dry_run else "live"
    jobs = {}
    for kind in KINDS:
        jobs[kind] = await queue.enqueue(
            conn,
            kind,
            {"trigger": "manual", "dry_run": dry_run},
            idempotency_key=f"{kind}:manual:{mode}:{minute}",
            max_attempts=MAX_ATTEMPTS,
            created_by=f"staff:{staff.cis_user_id}"[:128],
        )
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="departure_checks.queued",
        entity_type="job",
        entity_id=None,
        after={"dry_run": dry_run, "jobs": jobs},
        summary=f"Departure checks queued ({'dry run' if dry_run else 'live'})",
    )
    return {"dry_run": dry_run, "jobs": jobs}


RUNS_SQL = """
SELECT id::text AS id, kind, status, created_by, created_at, started_at, finished_at, attempt, result::text AS result,
       error
  FROM booking.job WHERE kind = ANY($1::text[])
 ORDER BY created_at DESC
 LIMIT $2
"""


async def recent_runs(conn: asyncpg.Connection, limit: int = 20) -> dict[str, Any]:
    """The latest runs of both kinds (status, counts)."""
    items = []
    for r in await conn.fetch(RUNS_SQL, list(KINDS), limit):
        item = dict(r)
        try:
            item["result"] = json.loads(r["result"]) if r["result"] else None
        except ValueError:
            item["result"] = None
        item["error"] = (r["error"] or "")[:300] or None
        items.append(item)
    return {"items": items, "schedule_utc_hour": SCHEDULE_UTC_HOUR}
