"""Supplier contracts about to end (AA-1151, bk-068; S285 advisory B3: "contract expiring" 60 days ahead, one expiry
job for every supplier contract, hotels included: the hotel slice only adds its own reconfirmation reminder).

- **The window** is the pricing policy parameter ``contract_expiry_alert_days`` (Pricing > Policy, section
  operations; seeded 60 by bk-068): a published contract whose ``valid_to`` falls from today to today + that many days
  is *expiring*. An open-ended contract (no ``valid_to``) never is. No value in force = the job fails (no fallback
  number in code) and the staff list answers 503 ``policy_unavailable``.
- **The job** ``bk_contract_expiry_watch`` runs once per UTC day (:class:`ExpiryDailySchedule`, first worker pass at
  or after :data:`SCHEDULE_UTC_HOUR`; key ``<kind>:<UTC date>``) and writes one staff notification
  ``contract.expiring`` per contract and end date (shared queue, ``reminder_key`` ``expiring:<valid_to>``: a contract
  whose end moves is announced again; the same end never twice). ``dry_run`` reports and writes nothing.
- **The admin list** :func:`expiring` (``GET /booking/contracts/expiring``) is computed live from the contracts, so
  the Contracts page shows the warning even before the first run; it says whether each contract was announced.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import asyncpg

from app.jobs import queue
from app.jobs.queue import Job
from app.notifications import service as notifications
from app.pricing import policy_values

log = logging.getLogger(__name__)

KIND = "bk_contract_expiry_watch"
POLICY_KEY = "contract_expiry_alert_days"
NOTIFICATION_TYPE = "contract.expiring"
MAX_ATTEMPTS = 3  # technical-constant: job retries, like the other daily jobs (app.b2b.departure_watch)
SCHEDULE_UTC_HOUR = 1  # technical-constant: the daily jobs' slot (app.b2b.departure_watch uses the same hour)
SCHEDULER = "scheduler:aa1151-contract-expiry"
MAX_LISTED = 50  # technical-constant: how many contracts a job result names (the counts cover every one)

EXPIRING_SQL = """
SELECT k.id::text AS id, k.title, k.reference, k.valid_from, k.valid_to, k.currency,
       sp.id::text AS supplier_id, sp.code AS supplier_code, sp.company_name AS supplier_name,
       (k.valid_to - $1::date) AS days_left,
       EXISTS (SELECT 1 FROM booking.notifications n
                WHERE n.recipient_type = 'staff' AND n.type = $3 AND n.entity_id = k.id
                  AND n.data ->> 'reminder_key' = 'expiring:' || k.valid_to::text) AS announced
FROM booking.supplier_contracts k
JOIN booking.suppliers sp ON sp.id = k.supplier_id
WHERE k.status = 'published' AND k.valid_to IS NOT NULL AND k.valid_to BETWEEN $1::date AND $2::date
ORDER BY k.valid_to, sp.company_name, k.id
"""


async def window_days(conn: asyncpg.Connection, today: date, *, staff: bool) -> int:
    reader = policy_values.require_staff if staff else policy_values.require
    return (await reader(conn, today, POLICY_KEY)).integer(POLICY_KEY)


async def _rows(conn: asyncpg.Connection, today: date, days: int) -> list[dict[str, Any]]:
    rows = await conn.fetch(EXPIRING_SQL, today, today + timedelta(days=days), NOTIFICATION_TYPE)
    return [dict(r) for r in rows]


async def expiring(conn: asyncpg.Connection, *, today: date | None = None) -> dict[str, Any]:
    """The published contracts ending within the window, soonest first (the Contracts page warning)."""
    today = today or await policy_values.today_utc(conn)
    days = await window_days(conn, today, staff=True)
    return {"window_days": days, "today": today, "items": await _rows(conn, today, days)}


async def run_watch(conn: asyncpg.Connection, *, today: date, dry_run: bool = False) -> dict[str, Any]:
    """One pass (call inside a transaction): announce every expiring contract not announced for its end date yet."""
    days = await window_days(conn, today, staff=False)
    rows = await _rows(conn, today, days)
    announced: list[dict[str, Any]] = []
    for r in rows:
        if r["announced"]:
            continue
        brief = {"contract_id": r["id"], "valid_to": r["valid_to"].isoformat(), "days_left": r["days_left"]}
        announced.append(brief)
        if dry_run:
            continue
        await notifications.notify_staff_once(
            conn,
            type=NOTIFICATION_TYPE,
            title=f"Contract {r['title']} of {r['supplier_name']} ends on {r['valid_to'].isoformat()}"[:200],
            message=(
                f"{r['days_left']} days left. Agree the new rates with the supplier and enter the next contract"
                " before then, or what it prices stops selling."
            ),
            entity_type="supplier_contract",
            entity_id=r["id"],
            reminder_key=f"expiring:{r['valid_to'].isoformat()}",
            data={
                "contract_id": r["id"],
                "supplier_id": r["supplier_id"],
                "supplier_code": r["supplier_code"],
                "valid_to": r["valid_to"].isoformat(),
                "days_left": r["days_left"],
            },
        )
    return {
        "dry_run": dry_run,
        "day": today.isoformat(),
        "window_days": days,
        "expiring_count": len(rows),
        "announced_count": len(announced),
        "announced": announced[:MAX_LISTED],
    }


async def handle_watch(pool: asyncpg.Pool, job: Job) -> dict[str, Any]:
    payload = job.payload or {}
    today = date.fromisoformat(payload["day"]) if payload.get("day") else datetime.now(UTC).date()
    async with pool.acquire() as conn, conn.transaction():
        result = await run_watch(conn, today=today, dry_run=bool(payload.get("dry_run")))
    log.info(
        "contract_expiry_watch day=%s expiring=%s announced=%s dry_run=%s",
        today,
        result["expiring_count"],
        result["announced_count"],
        result["dry_run"],
    )
    return {"outcome": "ok", "trigger": payload.get("trigger"), **result}


HANDLERS = {KIND: handle_watch}


def schedule_key(day: date) -> str:
    return f"{KIND}:{day.isoformat()}"


class ExpiryDailySchedule:
    """Worker maintenance hook: queue one ``trigger=schedule`` run per UTC day, on the first pass at or after
    :data:`SCHEDULE_UTC_HOUR`. Idempotent across workers and restarts (key per day)."""

    def __init__(self, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._now = now
        self._done_day: date | None = None

    async def __call__(self, pool: asyncpg.Pool) -> list[str]:
        now = self._now().astimezone(UTC)
        today = now.date()
        if now.hour < SCHEDULE_UTC_HOUR or self._done_day == today:
            return []
        job_id = await queue.enqueue(
            pool,
            KIND,
            {"trigger": "schedule"},
            idempotency_key=schedule_key(today),
            max_attempts=MAX_ATTEMPTS,
            created_by=SCHEDULER,
        )
        self._done_day = today
        return [job_id] if job_id else []
