"""Overdue agency invoice reminders (AA-1037 R4 extras G4): ``bk_agency_invoice_reminders``, once a day.

**Steps** (:func:`due_step`): an overdue issued invoice (balance left, due date past) gets one e-mail per step it
reaches: the ``reminder`` steps of the setting ``agency_reminder_steps_days`` (``booking.settings``, a JSON list of
days overdue; default ``[1, 7, 14]``) and the ``credit_stop`` step at the agency's ``suspend_after_days`` (its
billing terms, else the default row), the day its billing status turns ``past_due`` and new B2B holds are refused
(409 ``credit_hold_overdue``, ``app.b2b.holds.check_credit``). Only the highest step reached is sent: an invoice found
20 days overdue gets the 14-day reminder, not three e-mails at once.

**Once per invoice per step**: every handled step is a row of ``booking.agency_invoice_reminder`` (bk-044, primary key
``(invoice_id, step_days)``), written after the send (``sent``: the address and the SES message id, ``delivery_ref``)
or instead of it (``skipped``: the invoice's bill-to snapshot has no billing e-mail). A step with a row is never sent
again. Delivery is at least once, like ``bk_notify_email``: a worker that dies between the SES answer and the row
re-sends on retry.

**Recipient**: the billing e-mail of the invoice's bill-to snapshot (taken at issue). Outside ``prod`` every reminder
goes to the staff inbox (``STAFF_NOTIFY_EMAIL``) instead, subject prefixed with the environment and the agency's
address named in the body (``redirected`` true): Dev agencies are synthetic or the pilot, never mailed by a test run.
No ``SES_FROM_ADDRESS`` (or no staff inbox outside prod): the run is ``skipped`` and writes nothing, so the steps
are sent once mail is configured.

Each invoice is handled on its own; a failed send is counted, the others are kept, and the run raises at the end so
the runner retries it (a step already recorded is not sent again). Not deploy-gate exempt (seconds).
"""

from __future__ import annotations

import html
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import asyncpg

from app import mailer
from app.jobs import queue
from app.jobs.queue import Job
from app.jobs.worker import Handler, PermanentError
from app.settings import Settings

log = logging.getLogger(__name__)

KIND = "bk_agency_invoice_reminders"
SCHEDULER = "system:agency_billing"
MAX_ATTEMPTS = 3
STEPS_SETTING = "agency_reminder_steps_days"
DEFAULT_STEPS: tuple[int, ...] = (1, 7, 14)
MAX_STEP_DAYS = 365
MAX_INVOICES = 500
REMINDER = "reminder"
CREDIT_STOP = "credit_stop"
KIND_LABEL = {"saas": "software fee", "settlement": "tour settlement"}

OVERDUE_SQL = """
SELECT i.id::text AS id, i.number, i.invoice_kind, i.currency::text AS currency, i.total,
       i.total - coalesce(a.paid, 0) AS balance, i.issued_on, i.due_date, $1::date - i.due_date AS days_overdue,
       i.bill_to ->> 'billing_email' AS billing_email, i.issuer::text AS issuer, p.agency_name,
       coalesce(t.suspend_after_days, d.suspend_after_days)::integer AS suspend_after_days,
       coalesce((SELECT array_agg(r.step_days::integer ORDER BY r.step_days) FROM booking.agency_invoice_reminder r
                  WHERE r.invoice_id = i.id), '{}'::integer[]) AS done_steps
  FROM booking.agency_invoice i
  JOIN booking.b2b_partners p ON p.tenant_id = i.party_tenant_id
  LEFT JOIN (SELECT invoice_id, sum(amount) AS paid FROM booking.agency_payment WHERE status = 'approved'
              GROUP BY invoice_id) a ON a.invoice_id = i.id
  LEFT JOIN booking.billing_terms t ON t.party_tenant_id = i.party_tenant_id
  LEFT JOIN booking.billing_terms d ON d.party_tenant_id IS NULL
 WHERE i.status = 'issued' AND i.due_date < $1::date AND i.total - coalesce(a.paid, 0) > 0
 ORDER BY i.due_date, i.number
 LIMIT $2
"""
RECORD_SQL = """
INSERT INTO booking.agency_invoice_reminder
    (invoice_id, step_days, step_kind, days_overdue, outcome, skip_reason, sent_to, redirected, delivery_ref,
     created_by)
VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10)
ON CONFLICT (invoice_id, step_days) DO NOTHING
"""
LIST_SQL = """
SELECT r.step_days, r.step_kind, r.days_overdue, r.outcome, r.skip_reason, r.sent_to, r.redirected, r.created_at
  FROM booking.agency_invoice_reminder r WHERE r.invoice_id = $1::uuid ORDER BY r.step_days
"""


class ReminderFailed(RuntimeError):
    """Some sends failed: the others are recorded, the job is retried."""


# ── Pure rules ───────────────────────────────────────────────────────────────


def parse_steps(raw: Any) -> tuple[int, ...]:
    """The reminder steps from the setting (a JSON list of whole days 1..365); the default when unset or invalid."""
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return DEFAULT_STEPS
    if not isinstance(value, list) or not value:
        return DEFAULT_STEPS
    steps = set()
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int) or not 1 <= item <= MAX_STEP_DAYS:
            return DEFAULT_STEPS
        steps.add(item)
    return tuple(sorted(steps))


def due_step(
    days_overdue: int, steps: tuple[int, ...], suspend_after_days: int | None, done: list[int] | tuple[int, ...]
) -> tuple[int, str] | None:
    """The step to send now: the highest step reached (reminder steps + the credit stop at ``suspend_after_days``),
    unless it was already handled. None when no step is reached or the highest one is done."""
    kinds = dict.fromkeys(steps, REMINDER)
    if suspend_after_days is not None and 1 <= suspend_after_days <= MAX_STEP_DAYS:
        kinds[suspend_after_days] = CREDIT_STOP
    reached = [s for s in kinds if s <= days_overdue]
    if not reached:
        return None
    top = max(reached)
    return None if top in set(done) else (top, kinds[top])


@dataclass(frozen=True)
class Overdue:
    id: str
    number: str
    invoice_kind: str
    currency: str
    total: Decimal
    balance: Decimal
    issued_on: date | None
    due_date: date
    days_overdue: int
    billing_email: str | None
    agency_name: str
    issuer: dict[str, Any]


def _overdue(row: Any) -> Overdue:
    issuer = row["issuer"]
    return Overdue(
        id=row["id"],
        number=row["number"],
        invoice_kind=row["invoice_kind"],
        currency=row["currency"].strip(),
        total=Decimal(row["total"]),
        balance=Decimal(row["balance"]),
        issued_on=row["issued_on"],
        due_date=row["due_date"],
        days_overdue=int(row["days_overdue"]),
        billing_email=(row["billing_email"] or "").strip() or None,
        agency_name=row["agency_name"],
        issuer=json.loads(issuer) if isinstance(issuer, str) else (issuer or {}),
    )


def render(
    inv: Overdue, *, step_kind: str, to: str, environment: str, redirected_from: str | None = None
) -> mailer.Message:
    """The reminder e-mail (plain text + an HTML twin). No line detail, no payment reference: the invoice itself is
    the document; the issuer's payment instructions and invoice inbox come from the invoice's issuer snapshot."""
    prefix = "" if environment == "prod" else f"[{environment.capitalize()}] "
    days = f"{inv.days_overdue} day{'s' if inv.days_overdue != 1 else ''}"
    if step_kind == CREDIT_STOP:
        subject = f"{prefix}New bookings paused: invoice {inv.number} is {days} overdue"
        lead = (
            f"Invoice {inv.number} is {days} overdue. Under your payment terms, new booking holds with AA are paused "
            "until the overdue invoices are paid. Bookings already confirmed are not affected."
        )
    else:
        subject = f"{prefix}Payment reminder: invoice {inv.number} is {days} overdue"
        lead = f"This is a reminder that invoice {inv.number} is {days} overdue."
    facts = [
        ("Agency", inv.agency_name),
        ("Invoice", f"{inv.number} ({KIND_LABEL.get(inv.invoice_kind, inv.invoice_kind)})"),
        ("Issued on", inv.issued_on.isoformat() if inv.issued_on else "-"),
        ("Due date", inv.due_date.isoformat()),
        ("Invoice total", f"{inv.total:,.2f} {inv.currency}"),
        ("Balance due", f"{inv.balance:,.2f} {inv.currency}"),
    ]
    instructions = (inv.issuer.get("payment_instructions") or "").strip()
    inbox = (inv.issuer.get("invoice_inbox") or "").strip()
    paid_note = (
        f"If you have already paid, please send the transfer reference to {inbox}."
        if inbox
        else "If you have already paid, please reply with the transfer reference to your AA contact."
    )
    issuer = (inv.issuer.get("legal_entity") or "AA").strip()
    redirect_note = (
        f"Test copy: on {environment} this reminder goes to the staff inbox, not to {redirected_from}."
        if redirected_from
        else None
    )
    text = [*([redirect_note, ""] if redirect_note else []), lead, ""]
    text += [f"{k}: {v}" for k, v in facts]
    if instructions:
        text += ["", "How to pay:", instructions]
    text += ["", paid_note, "", "-- ", f"{issuer}, automatic message."]

    e = html.escape
    rows = "".join(
        f'<tr><td style="padding:2px 12px 2px 0;color:#555">{e(k)}</td><td>{e(v)}</td></tr>' for k, v in facts
    )
    body = (
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#222">'
        + (f'<p style="color:#b45309">{e(redirect_note)}</p>' if redirect_note else "")
        + f"<p>{e(lead)}</p><table>{rows}</table>"
        + (
            f'<p><strong>How to pay</strong></p><p style="white-space:pre-line">{e(instructions)}</p>'
            if instructions
            else ""
        )
        + f"<p>{e(paid_note)}</p>"
        f'<p style="color:#888;font-size:12px">{e(issuer)}, automatic message.</p></div>'
    )
    return mailer.Message(to=to, subject=subject[:200], text="\n".join(text), html=body)


# ── Job ──────────────────────────────────────────────────────────────────────


async def _steps(conn: asyncpg.Connection) -> tuple[int, ...]:
    return parse_steps(await conn.fetchval("SELECT value FROM booking.settings WHERE key = $1", STEPS_SETTING))


async def run(
    pool: asyncpg.Pool,
    settings: Settings,
    send: Callable[[mailer.Message], Any],
    *,
    today: date,
    actor: str,
) -> dict[str, Any]:
    """One pass over the overdue invoices; ``send(message) -> message id`` (awaitable)."""
    prod = settings.environment == "prod"
    if not settings.ses_from_address:
        return {"outcome": "skipped", "reason": "SES_FROM_ADDRESS not set"}
    if not prod and not settings.staff_notify_email:
        return {"outcome": "skipped", "reason": "STAFF_NOTIFY_EMAIL not set (reminders go to staff outside prod)"}
    async with pool.acquire() as conn:
        steps = await _steps(conn)
        rows = await conn.fetch(OVERDUE_SQL, today, MAX_INVOICES)
    counts = {"overdue": len(rows), "sent": 0, "skipped": 0, "failed": 0, "not_due": 0}
    for row in rows:
        step = due_step(int(row["days_overdue"]), steps, row["suspend_after_days"], list(row["done_steps"] or []))
        if step is None:
            counts["not_due"] += 1
            continue
        step_days, step_kind = step
        inv = _overdue(row)
        record: tuple[Any, ...]
        if inv.billing_email is None:
            record = ("skipped", "no_billing_email", None, False, None)
        else:
            to = inv.billing_email if prod else settings.staff_notify_email
            message = render(
                inv,
                step_kind=step_kind,
                to=to,
                environment=settings.environment,
                redirected_from=None if prod else inv.billing_email,
            )
            try:
                message_id = await send(message)
            except mailer.MailError as exc:
                counts["failed"] += 1
                log.warning("agency reminder %s step %s not sent: %s", inv.number, step_days, exc.code)
                continue
            record = ("sent", None, to, not prod, str(message_id)[:200])
        async with pool.acquire() as conn:
            await conn.execute(RECORD_SQL, inv.id, step_days, step_kind, inv.days_overdue, *record, actor)
        counts[record[0]] += 1
        log.info("agency reminder %s step %s (%s): %s", inv.number, step_days, step_kind, record[0])
    if counts["failed"]:
        raise ReminderFailed(f"{counts['failed']} reminder(s) not sent: {counts}")
    return {"outcome": "done", **counts}


def build_handler(settings: Settings, client_factory: Callable[[str], mailer.SesClient] = mailer.ses_client) -> Handler:
    """The ``bk_agency_invoice_reminders`` handler bound to settings (the SES client is made on the first send)."""
    clients: list[mailer.SesClient] = []

    async def send(message: mailer.Message) -> str:
        if not clients:
            clients.append(client_factory(settings.ses_region))
        try:
            return await mailer.send(
                clients[0],
                message,
                sender=settings.ses_from_address,
                configuration_set=settings.ses_configuration_set or None,
            )
        except mailer.MailError as exc:
            if exc.code in ("AccountSuspendedException", "SendingPausedException"):
                raise PermanentError(str(exc)) from exc  # nothing can be sent: stop the run
            raise

    async def handle(pool: asyncpg.Pool, job: Job) -> dict[str, Any]:
        today = job.payload.get("day")
        try:
            day = date.fromisoformat(today) if isinstance(today, str) else datetime.now(UTC).date()
        except ValueError:
            raise PermanentError("payload.day is not YYYY-MM-DD") from None
        return await run(pool, settings, send, today=day, actor=f"job:{KIND}")

    return handle


class ReminderSchedule:
    """Worker maintenance hook: one reminder run per UTC day (key per day)."""

    def __init__(self, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._now = now
        self._done_day: date | None = None

    async def __call__(self, pool: asyncpg.Pool) -> str | None:
        today = self._now().date()
        if self._done_day == today:
            return None
        job_id = await queue.enqueue(pool, KIND, {"trigger": "schedule", "day": today.isoformat()},
                                     idempotency_key=f"{KIND}:{today.isoformat()}", max_attempts=MAX_ATTEMPTS,
                                     created_by=SCHEDULER)  # fmt: skip
        self._done_day = today
        return job_id


async def for_invoice(conn: asyncpg.Connection, invoice_id: str) -> list[dict[str, Any]]:
    """The reminder steps handled for one invoice (staff invoice page)."""
    return [dict(r) for r in await conn.fetch(LIST_SQL, invoice_id)]
