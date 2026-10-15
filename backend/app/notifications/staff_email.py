"""``bk_notify_email``: the "new inquiry" e-mail to the staff inbox (AA-811).

``bk_inquiry_process`` writes the staff ``trip_case.new_inquiry`` notification row and, in the same
transaction, queues one ``bk_notify_email`` job with idempotency key ``bk_notify_email:<notification_id>``,
so a notification gets at most one job (the ``booking.job`` unique index), and the intake never waits on SES.

The handler reads the notification, its trip case, the lead customer, the legs and (live, soft) the CIS
catalog titles and countries of the requested tours, renders a plain-text + HTML message and sends it through
SES (:mod:`app.mailer`). The SES ``MessageId`` is kept in the job result (no migration).

- ``STAFF_NOTIFY_EMAIL`` or ``SES_FROM_ADDRESS`` empty → the job succeeds as ``skipped`` with the reason.
- An SES refusal that a retry cannot fix (rejected address, sender not allowed, account paused) fails the job at
  once; throttling, network and 5xx errors are retried by the runner (backoff, ``max_attempts``).
- Delivery is at least once: a worker that dies after SES accepted the message but before the job is marked
  succeeded re-sends it when the reaper re-queues the job.
"""

from __future__ import annotations

import html
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

import asyncpg

from app import mailer
from app.catalog import CATALOG_ERRORS
from app.jobs.queue import Job
from app.jobs.worker import Handler, PermanentError
from app.settings import Settings
from app.trip_cases.sql import COUNTRIES_ARRAY, LEGS_JSON, leg_title, loads

log = logging.getLogger(__name__)

KIND = "bk_notify_email"
NEW_INQUIRY_TYPE = "trip_case.new_inquiry"


def idempotency_key(notification_id: str) -> str:
    return f"{KIND}:{notification_id}"


@dataclass(frozen=True)
class Tour:
    title: str
    country: str | None
    day_from: int
    day_to: int


@dataclass(frozen=True)
class InquiryMail:
    code: str
    customer_name: str | None
    customer_email: str | None
    customer_phone: str | None
    needs_merge_review: bool
    tours: tuple[Tour, ...]
    countries: tuple[str, ...]
    start_date: date | None
    end_date: date | None
    pax: int | None
    pax_adults: int | None
    pax_children: int | None


CASE_SQL = f"""
    SELECT n.id::text AS notification_id, n.type, tc.code, tc.start_date, tc.end_date, tc.pax, tc.pax_adults,
           tc.pax_children, tc.inquiry_snapshot::text AS inquiry_snapshot, c.name AS customer_name,
           c.email AS customer_email, c.phone AS customer_phone,
           coalesce(c.needs_merge_review, false) AS needs_merge_review,
           {LEGS_JSON}, {COUNTRIES_ARRAY}
    FROM booking.notifications n
    JOIN booking.trip_cases tc ON tc.id = n.entity_id
    LEFT JOIN booking.customers c ON c.id = tc.customer_id
    WHERE n.id = $1::uuid AND n.entity_type = 'trip_case'
"""  # noqa: S608 — module constants only, the id is bound


async def _catalog_tours(conn: asyncpg.Connection, tour_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Title and country per CIS tour id (contract: ``source_tour_id`` = CIS ``tour_id``). Soft: an unreadable
    catalog (grant missing, view renamed) only drops the titles, the e-mail still goes out."""
    if not tour_ids:
        return {}
    try:
        rows = await conn.fetch(
            "SELECT DISTINCT ON (tour_id) tour_id::text AS tour_id, title, country FROM catalog.v_tour"
            " WHERE tour_id::text = ANY($1::text[]) ORDER BY tour_id, source_version DESC",
            tour_ids,
        )
    except CATALOG_ERRORS as exc:
        log.warning("catalog.v_tour unreadable, inquiry e-mail uses snapshot titles: %s", type(exc).__name__)
        return {}
    return {r["tour_id"]: dict(r) for r in rows}


async def load_inquiry(conn: asyncpg.Connection, notification_id: str) -> InquiryMail | None:
    row = await conn.fetchrow(CASE_SQL, notification_id)
    if row is None or row["type"] != NEW_INQUIRY_TYPE:
        return None
    snapshot = loads(row["inquiry_snapshot"], {})
    legs = sorted(loads(row["legs"], []), key=lambda leg: (leg["day_from"], leg["position"]))
    catalog = await _catalog_tours(conn, sorted({leg["source_tour_id"] for leg in legs}))
    tours = []
    for leg in legs:
        hit = catalog.get(leg["source_tour_id"]) or {}
        title = (hit.get("title") or "").strip() or leg_title(snapshot, leg["source_tour_id"])
        tours.append(Tour(title, hit.get("country"), int(leg["day_from"]), int(leg["day_to"])))
    countries: list[str] = []
    for name in [t.country for t in tours] + list(row["countries"] or []):
        if name and name not in countries:
            countries.append(name)
    return InquiryMail(
        code=row["code"],
        customer_name=row["customer_name"],
        customer_email=row["customer_email"],
        customer_phone=row["customer_phone"],
        needs_merge_review=bool(row["needs_merge_review"]),
        tours=tuple(tours),
        countries=tuple(countries),
        start_date=row["start_date"],
        end_date=row["end_date"],
        pax=row["pax"],
        pax_adults=row["pax_adults"],
        pax_children=row["pax_children"],
    )


# ── Template ─────────────────────────────────────────────────────────────────


def _dates(m: InquiryMail) -> str:
    if m.start_date and m.end_date:
        return f"{m.start_date.isoformat()} to {m.end_date.isoformat()}"
    if m.start_date:
        return f"from {m.start_date.isoformat()}"
    return "not given yet"


def _travellers(m: InquiryMail) -> str:
    if m.pax_adults:
        children = m.pax_children or 0
        text = f"{m.pax_adults} adult{'s' if m.pax_adults != 1 else ''}"
        return text + (f", {children} child{'ren' if children != 1 else ''}" if children else "")
    if m.pax:
        return f"{m.pax} traveller{'s' if m.pax != 1 else ''}"
    return "not given yet"


def _tour_line(t: Tour) -> str:
    days = f"day {t.day_from}" if t.day_from == t.day_to else f"days {t.day_from}-{t.day_to}"
    return f"{t.title}{f' ({t.country})' if t.country else ''}, {days}"


def case_url(admin_url: str, code: str) -> str:
    return f"{admin_url.rstrip('/')}/cases/{code}"


def render(m: InquiryMail, *, to: str, admin_url: str, environment: str) -> mailer.Message:
    """Plain text + a simple HTML twin. Staff-only content: the customer's contact details are included."""
    prefix = "" if environment == "prod" else f"[{environment.capitalize()}] "
    name = m.customer_name or "A traveller"
    url = case_url(admin_url, m.code)
    duration = max((t.day_to for t in m.tours), default=None)
    facts = [
        ("Customer", name),
        ("Email", m.customer_email or "-"),
        ("Phone", m.customer_phone or "-"),
        ("Countries", ", ".join(m.countries) or "-"),
        ("Trip length", f"{duration} day{'s' if duration != 1 else ''}" if duration else "-"),
        ("Travel dates", _dates(m)),
        ("Travellers", _travellers(m)),
    ]
    tours = [_tour_line(t) for t in m.tours]
    review = "The contact matches an existing customer: review before merging." if m.needs_merge_review else None

    text_lines = [f"New inquiry {m.code} from TripPlanner", ""]
    text_lines += [f"{label}: {value}" for label, value in facts]
    text_lines += ["", "Tours requested:"]
    text_lines += [f"  {i}. {line}" for i, line in enumerate(tours, 1)] or ["  (custom trip, no AA tour)"]
    if review:
        text_lines += ["", review]
    text_lines += ["", f"Open the trip case: {url}", "", "-- ", "AA-Booking, automatic message. Do not reply."]

    e = html.escape
    rows = "".join(
        f'<tr><td style="padding:2px 12px 2px 0;color:#555">{e(label)}</td><td>{e(value)}</td></tr>'
        for label, value in facts
    )
    items = "".join(f"<li>{e(line)}</li>" for line in tours) or "<li>Custom trip, no AA tour</li>"
    html_body = (
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#222">'
        f'<h2 style="font-size:18px">New inquiry {e(m.code)} from TripPlanner</h2>'
        f"<table>{rows}</table>"
        f"<p><strong>Tours requested</strong></p><ol>{items}</ol>"
        + (f'<p style="color:#b45309">{e(review)}</p>' if review else "")
        + f'<p><a href="{e(url, quote=True)}">Open the trip case {e(m.code)}</a></p>'
        '<p style="color:#888;font-size:12px">AA-Booking, automatic message. Do not reply.</p></div>'
    )
    return mailer.Message(
        to=to, subject=f"{prefix}New inquiry {m.code}: {name}", text="\n".join(text_lines), html=html_body
    )


# ── Handler ──────────────────────────────────────────────────────────────────


def build_handler(settings: Settings, client_factory: Callable[[str], mailer.SesClient] = mailer.ses_client) -> Handler:
    """The ``bk_notify_email`` handler bound to settings. The SES client is created on the first send."""
    clients: list[mailer.SesClient] = []

    def client() -> mailer.SesClient:
        if not clients:
            clients.append(client_factory(settings.ses_region))
        return clients[0]

    async def handle(pool: asyncpg.Pool, job: Job) -> dict[str, Any]:
        notification_id = job.payload.get("notification_id")
        if not isinstance(notification_id, str) or not notification_id:
            raise PermanentError("payload.notification_id missing")
        for name, value in (
            ("STAFF_NOTIFY_EMAIL", settings.staff_notify_email),
            ("SES_FROM_ADDRESS", settings.ses_from_address),
        ):
            if not value:
                log.info("inquiry e-mail for notification %s skipped: %s is not set", notification_id, name)
                return {"outcome": "skipped", "reason": f"{name} not set", "notification_id": notification_id}
        async with pool.acquire() as conn:
            inquiry = await load_inquiry(conn, notification_id)
        if inquiry is None:
            raise PermanentError(f"notification {notification_id} is not a {NEW_INQUIRY_TYPE} of a trip case")
        message = render(
            inquiry,
            to=settings.staff_notify_email,
            admin_url=settings.staff_admin_url,
            environment=settings.environment,
        )
        try:
            message_id = await mailer.send(
                client(),
                message,
                sender=settings.ses_from_address,
                configuration_set=settings.ses_configuration_set or None,
            )
        except mailer.MailError as exc:
            if exc.permanent:
                raise PermanentError(str(exc)) from exc
            raise
        log.info("inquiry e-mail %s sent to staff (SES message %s)", inquiry.code, message_id)
        return {
            "outcome": "sent",
            "message_id": message_id,
            "to": settings.staff_notify_email,
            "trip_case": inquiry.code,
            "notification_id": notification_id,
        }

    return handle


def payload(notification_id: str) -> dict[str, Any]:
    return {"notification_id": notification_id}
