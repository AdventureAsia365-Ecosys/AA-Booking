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
- **The admin list** :func:`expiring` (``GET /booking/contracts/expiring``, optionally for one supplier) is computed
  live from the contracts, so the pages show the warning even before the first run; it says whether each contract was
  announced and e-mailed. The staff pages Suppliers, a supplier's Contracts tab and Add-ons list it; the dashboard My
  work tile ``contracts_ending_soon`` counts it (:func:`queue`).
- **The e-mail** (AA-1177, Jira PR-31): after the announcement, the same job e-mails the active Booking staff of the
  roles in the policy value ``contract_expiry_mail_roles`` once per contract and end date
  (:mod:`app.supply.contract_expiry_mail`, recorded on the announcement row, so the same ``reminder_key``).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import asyncpg

from app import mailer
from app.jobs import queue
from app.jobs.queue import Job
from app.jobs.worker import Handler
from app.notifications import service as notifications
from app.pricing import policy_values
from app.settings import Settings
from app.supply import contract_expiry_mail

log = logging.getLogger(__name__)

KIND = "bk_contract_expiry_watch"
POLICY_KEY = "contract_expiry_alert_days"
NOTIFICATION_TYPE = contract_expiry_mail.NOTIFICATION_TYPE
MAX_ATTEMPTS = 3  # technical-constant: job retries, like the other daily jobs (app.b2b.departure_watch)
SCHEDULE_UTC_HOUR = 1  # technical-constant: the daily jobs' slot (app.b2b.departure_watch uses the same hour)
SCHEDULER = "scheduler:aa1151-contract-expiry"
MAX_LISTED = 50  # technical-constant: how many contracts a job result names (the counts cover every one)

# The contract "kind" is its supplier's categories (hotel, transport, guide, activity, ...): every kind is listed.
# $4: one supplier (UUID or code) or NULL for every supplier.
EXPIRING_SQL = """
SELECT k.id::text AS id, k.title, k.reference, k.valid_from, k.valid_to, k.currency,
       sp.id::text AS supplier_id, sp.code AS supplier_code, sp.company_name AS supplier_name,
       sp.categories AS supplier_categories,
       (k.valid_to - $1::date) AS days_left,
       n.id IS NOT NULL AS announced,
       coalesce(n.data -> 'mail' ->> 'done' = 'true', false) AS mailed
FROM booking.supplier_contracts k
JOIN booking.suppliers sp ON sp.id = k.supplier_id
LEFT JOIN booking.notifications n
       ON n.recipient_type = 'staff' AND n.type = $3 AND n.entity_id = k.id
      AND n.data ->> 'reminder_key' = 'expiring:' || k.valid_to::text
WHERE k.status = 'published' AND k.valid_to IS NOT NULL AND k.valid_to BETWEEN $1::date AND $2::date
  AND ($4::text IS NULL OR sp.id::text = $4::text OR sp.code = $4::text)
ORDER BY k.valid_to, sp.company_name, k.id
"""
QUEUE_SQL = """
SELECT count(*) AS count, min(k.valid_to) AS earliest_due
FROM booking.supplier_contracts k
WHERE k.status = 'published' AND k.valid_to IS NOT NULL AND k.valid_to BETWEEN $1::date AND $2::date
"""


async def window_days(conn: asyncpg.Connection, today: date, *, staff: bool) -> int:
    reader = policy_values.require_staff if staff else policy_values.require
    return (await reader(conn, today, POLICY_KEY)).integer(POLICY_KEY)


async def _rows(conn: asyncpg.Connection, today: date, days: int, supplier: str | None = None) -> list[dict[str, Any]]:
    rows = await conn.fetch(EXPIRING_SQL, today, today + timedelta(days=days), NOTIFICATION_TYPE, supplier)
    return [{**dict(r), "supplier_categories": list(r["supplier_categories"] or [])} for r in rows]


async def expiring(
    conn: asyncpg.Connection, *, today: date | None = None, supplier: str | None = None
) -> dict[str, Any]:
    """The published contracts ending within the window, soonest first (the pages' warning); ``supplier`` (UUID or
    code) narrows it to one supplier's contracts."""
    today = today or await policy_values.today_utc(conn)
    days = await window_days(conn, today, staff=True)
    return {"window_days": days, "today": today, "items": await _rows(conn, today, days, supplier)}


async def queue_count(conn: asyncpg.Connection) -> dict[str, Any]:
    """The My work tile ``contracts_ending_soon`` (AA-1177): how many contracts :func:`expiring` lists, the first end
    date and the window (503 ``policy_unavailable`` without a window in force: the tile shows "not available")."""
    today = await policy_values.today_utc(conn)
    days = await window_days(conn, today, staff=True)
    row = await conn.fetchrow(QUEUE_SQL, today, today + timedelta(days=days))
    return {"count": row["count"], "earliest_due": row["earliest_due"], "days": days}


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


async def handle_watch(pool: asyncpg.Pool, job: Job, mail: contract_expiry_mail.Mailer | None = None) -> dict[str, Any]:
    """Announce (one transaction), then e-mail what was announced and not mailed yet (outside it: a send is not
    undone by a rollback; each contract's mail record is written right after its sends)."""
    payload = job.payload or {}
    today = date.fromisoformat(payload["day"]) if payload.get("day") else datetime.now(UTC).date()
    dry_run = bool(payload.get("dry_run"))
    async with pool.acquire() as conn, conn.transaction():
        result = await run_watch(conn, today=today, dry_run=dry_run)
    mailed = (
        await contract_expiry_mail.run(pool, mail, today=today, window_days=result["window_days"], dry_run=dry_run)
        if mail is not None
        else {"outcome": "skipped", "reason": "mail not configured"}
    )
    log.info(
        "contract_expiry_watch day=%s expiring=%s announced=%s mail=%s sent=%s dry_run=%s",
        today,
        result["expiring_count"],
        result["announced_count"],
        mailed["outcome"],
        mailed.get("sent"),
        dry_run,
    )
    out = {"outcome": "ok", "trigger": payload.get("trigger"), **result, "mail": mailed}
    contract_expiry_mail.raise_if_failed(mailed)
    return out


def build_handler(settings: Settings, client_factory: Callable[[str], mailer.SesClient] = mailer.ses_client) -> Handler:
    """The ``bk_contract_expiry_watch`` handler bound to the mail settings (the SES client is made on the first
    send)."""
    mail = contract_expiry_mail.Mailer.from_settings(settings, client_factory)

    async def handle(pool: asyncpg.Pool, job: Job) -> dict[str, Any]:
        return await handle_watch(pool, job, mail)

    return handle


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
