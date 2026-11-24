"""The supplier contract expiry e-mail (AA-1177, Jira PR-31: "remind the admins when a supplier contract is about to
end"), sent by the daily job ``bk_contract_expiry_watch`` right after its in-app announcement (:mod:`app.supply.
contract_expiry`).

- **What is mailed**: every ``contract.expiring`` staff notification whose contract is still published and still ends
  within the window (``contract_expiry_alert_days``) and whose mail record is not ``done``. One e-mail per contract and
  end date: the record lives on that notification row (``data.mail``), whose ``reminder_key`` ``expiring:<valid_to>``
  is already unique per contract and end date (bk-027 ``notifications_reminder_uq``), so a second run sends nothing
  and a contract whose end moves is mailed again with its new announcement.
- **Who gets it**: resolved at send time, never stored as a list: the active Booking staff (``booking.staff_users``)
  whose role is one of the pricing policy value ``contract_expiry_mail_roles`` (Pricing > Policy, seeded
  ``["admin", "contracting"]`` by bk-074; an empty list switches the e-mail off, the in-app notification and the
  lists stay). The list names built-in roles; a custom role (AA-1155, ``booking.staff_role``, cloned from a built-in
  one) counts as its ``based_on`` role while it is not archived and still holds ``supply.read`` (the e-mail's link
  opens the supplier's Contracts tab), so moving someone to a custom role cloned from contracting keeps their
  e-mail. Each person gets their own message (no address sees another). A person without an e-mail address is
  skipped. Someone given the role after a contract was mailed does not get that contract's e-mail.
- **The record** ``data.mail``: ``sent`` / ``refused`` (the staff keys handled, never an address), ``done`` and
  ``at``. ``refused`` = SES refused that address for good (``MessageRejected`` and the like: a retry cannot help).
  A throttled or failed send leaves ``done`` false: the others are kept, the job raises at the end and the runner
  retries, sending only to the people not handled yet. Delivery is at least once, like the agency reminders.
- **Not configured** (no ``SES_FROM_ADDRESS``): skipped with the reason, nothing written, so the contracts still in
  the window are mailed once mail is configured.
- **Body**: English, then Vietnamese. Business facts only (supplier, contract, end date, the link to the supplier's
  Contracts tab); no personal data but the recipient's own address in To. Outside prod the subject starts with the
  environment.
"""

from __future__ import annotations

import html
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import asyncpg

from app import mailer
from app.jobs.worker import PermanentError
from app.pricing import policy_values
from app.settings import Settings

log = logging.getLogger(__name__)

NOTIFICATION_TYPE = "contract.expiring"
ROLES_KEY = "contract_expiry_mail_roles"
# SES answers after which nothing at all can be sent: stop the run instead of refusing every address.
STOP_CODES = frozenset({"AccountSuspendedException", "SendingPausedException"})
CATEGORY_LABEL = {
    "hotel": ("Hotel", "Khách sạn"),
    "transport": ("Transport", "Vận chuyển"),
    "guide": ("Guide", "Hướng dẫn viên"),
    "entrance": ("Entrance", "Vé tham quan"),
    "activity": ("Activity", "Hoạt động"),
    "meal": ("Meal", "Bữa ăn"),
    "dmc": ("DMC", "DMC"),
}

PENDING_SQL = """
SELECT n.id::text AS notification_id, n.data -> 'mail' AS mail,
       k.id::text AS contract_id, k.title, k.reference, k.valid_to, (k.valid_to - $1::date) AS days_left,
       sp.code AS supplier_code, sp.company_name AS supplier_name, sp.categories AS supplier_categories
FROM booking.notifications n
JOIN booking.supplier_contracts k ON k.id = n.entity_id
JOIN booking.suppliers sp ON sp.id = k.supplier_id
WHERE n.recipient_type = 'staff' AND n.type = $3 AND n.entity_type = 'supplier_contract'
  AND n.data ->> 'reminder_key' = 'expiring:' || k.valid_to::text
  AND k.status = 'published' AND k.valid_to BETWEEN $1::date AND $2::date
  AND coalesce(n.data -> 'mail' ->> 'done', 'false') <> 'true'
ORDER BY k.valid_to, sp.company_name, k.id
"""
# A custom role (AA-1155, booking.staff_role) counts as the built-in role it was cloned from (based_on) while it is not
# archived and still holds READ_CAPABILITY, the capability the e-mail's link needs (the supplier's Contracts tab).
READ_CAPABILITY = "supply.read"
RECIPIENTS_SQL = """
SELECT s.cis_user_id, btrim(s.email) AS email
FROM booking.staff_users s
WHERE s.active AND s.email IS NOT NULL AND position('@' IN s.email) > 1
  AND (s.role = ANY($1::text[])
       OR EXISTS (SELECT 1 FROM booking.staff_role r
                  JOIN booking.staff_role_capability c ON c.role_code = r.code AND c.capability = $2
                  WHERE r.code = s.role AND NOT r.archived AND r.based_on = ANY($1::text[])))
ORDER BY s.cis_user_id
"""
RECORD_SQL = "UPDATE booking.notifications SET data = jsonb_set(data, '{mail}', $2::jsonb, true) WHERE id = $1::uuid"


class MailFailed(RuntimeError):
    """Some sends failed for a reason a retry can fix: the others are recorded, the job is retried."""


@dataclass(frozen=True)
class Recipient:
    ref: str  # booking.staff_users.cis_user_id (recorded; the address never is)
    email: str


@dataclass
class Mailer:
    """How the job sends: ``send(message) -> message id``; ``skip_reason`` set = mail is not configured."""

    send: Callable[[mailer.Message], Awaitable[str]] | None
    environment: str
    admin_url: str
    skip_reason: str | None = None

    @classmethod
    def from_settings(cls, settings: Settings, client_factory: Callable[[str], mailer.SesClient]) -> Mailer:
        if not settings.ses_from_address:
            return cls(None, settings.environment, settings.staff_admin_url, "SES_FROM_ADDRESS not set")
        clients: list[mailer.SesClient] = []

        async def send(message: mailer.Message) -> str:
            if not clients:
                clients.append(client_factory(settings.ses_region))
            return await mailer.send(
                clients[0],
                message,
                sender=settings.ses_from_address,
                configuration_set=settings.ses_configuration_set or None,
            )

        return cls(send, settings.environment, settings.staff_admin_url)


@dataclass
class Record:
    """``data.mail`` of one announcement."""

    sent: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    done: bool = False

    @classmethod
    def load(cls, raw: Any) -> Record:
        data = json.loads(raw) if isinstance(raw, str) else (raw or {})
        return cls(list(data.get("sent") or []), list(data.get("refused") or []), bool(data.get("done")))

    def handled(self) -> set[str]:
        return {*self.sent, *self.refused}

    def dump(self, at: datetime) -> str:
        return json.dumps({"sent": self.sent, "refused": self.refused, "done": self.done, "at": at.isoformat()})


# ── The message ──────────────────────────────────────────────────────────────


def _days(n: int, vi: bool = False) -> str:
    return f"{n} ngày" if vi else f"{n} day{'s' if n != 1 else ''}"


def _facts(row: Any, vi: bool) -> list[tuple[str, str]]:
    kinds = ", ".join(CATEGORY_LABEL.get(c, (c, c))[1 if vi else 0] for c in (row["supplier_categories"] or []))
    labels = (
        ("Nhà cung cấp", "Loại", "Hợp đồng", "Mã tham chiếu", "Ngày kết thúc", "Còn lại")
        if vi
        else ("Supplier", "Kind", "Contract", "Reference", "Ends on", "Days left")
    )
    values = (
        f"{row['supplier_name']} ({row['supplier_code']})",
        kinds or "-",
        row["title"],
        row["reference"] or "-",
        row["valid_to"].isoformat(),
        _days(int(row["days_left"]), vi),
    )
    return list(zip(labels, values, strict=True))


def _texts(days: str, vi: bool) -> tuple[str, str, str]:
    if vi:
        return (
            f"Một hợp đồng nhà cung cấp đã xuất bản sẽ hết hạn sau {days}.",
            "Hãy thoả thuận giá mới với nhà cung cấp và nhập hợp đồng tiếp theo vào Booking trước ngày đó, nếu không "
            "những gì hợp đồng này định giá sẽ ngừng bán.",
            "Bạn nhận e-mail này vì vai trò Booking của bạn nằm trong danh sách ở Pricing > Policy (vai trò nhận "
            "e-mail hợp đồng sắp hết hạn).",
        )
    return (
        f"A published supplier contract ends in {days}.",
        "Agree the new rates with the supplier and enter the next contract in Booking before then, or what this "
        "contract prices stops selling.",
        "You get this e-mail because your Booking role is one of those set in Pricing > Policy (contract expiry "
        "e-mail roles).",
    )


def render(row: Any, *, to: str, environment: str, admin_url: str) -> mailer.Message:
    """One contract's e-mail to one person: English, then Vietnamese, plain text with an HTML twin."""
    prefix = "" if environment == "prod" else f"[{environment.capitalize()}] "
    end = row["valid_to"].isoformat()
    subject = f"{prefix}Supplier contract ends {end} / Hợp đồng nhà cung cấp hết hạn {end}: {row['supplier_name']}"
    link = f"{admin_url.rstrip('/')}/suppliers/{row['supplier_code']}?tab=contracts"
    text: list[str] = []
    body = ['<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#222">']
    e = html.escape
    for vi in (False, True):
        lead, ask, why = _texts(_days(int(row["days_left"]), vi), vi)
        facts = _facts(row, vi)
        open_label = "Mở nhà cung cấp" if vi else "Open the supplier"
        text += [lead, "", *(f"{k}: {v}" for k, v in facts), "", ask, f"{open_label}: {link}", "", why, ""]
        rows = "".join(
            f'<tr><td style="padding:2px 12px 2px 0;color:#555">{e(k)}</td><td>{e(v)}</td></tr>' for k, v in facts
        )
        body.append(
            f"<p>{e(lead)}</p><table>{rows}</table><p>{e(ask)}</p>"
            f'<p><a href="{e(link)}">{e(open_label)}</a></p>'
            f'<p style="color:#888;font-size:12px">{e(why)}</p>'
            + ("" if vi else '<hr style="border:none;border-top:1px solid #ddd">')
        )
    text += ["-- ", "Adventure Asia Booking, automatic message / tin nhắn tự động."]
    body.append('<p style="color:#888;font-size:12px">Adventure Asia Booking, automatic message.</p></div>')
    return mailer.Message(to=to, subject=subject[:200], text="\n".join(text), html="".join(body))


# ── The run ──────────────────────────────────────────────────────────────────


async def recipients(conn: asyncpg.Connection, roles: list[str]) -> list[Recipient]:
    """The active staff of ``roles`` (built-in, or a custom role cloned from one; see the module docstring) with an
    address, one per address (first key wins)."""
    seen: set[str] = set()
    out: list[Recipient] = []
    for r in await conn.fetch(RECIPIENTS_SQL, list(roles), READ_CAPABILITY):
        key = r["email"].lower()
        if key not in seen:
            seen.add(key)
            out.append(Recipient(r["cis_user_id"], r["email"]))
    return out


async def _send_one(mail: Mailer, row: Any, person: Recipient, record: Record, counts: dict[str, Any]) -> bool:
    """Send to one person and note the outcome on ``record``; False = a retry may still deliver it."""
    assert mail.send is not None  # noqa: S101 — checked by the caller (skip_reason)
    message = render(row, to=person.email, environment=mail.environment, admin_url=mail.admin_url)
    try:
        await mail.send(message)
    except mailer.MailError as exc:
        if exc.code in STOP_CODES:
            raise PermanentError(str(exc)) from exc
        if exc.permanent:
            record.refused.append(person.ref)
            counts["refused"] += 1
            log.warning("contract expiry mail refused contract=%s code=%s", row["contract_id"], exc.code)
            return True
        counts["failed"] += 1
        log.warning("contract expiry mail failed contract=%s code=%s", row["contract_id"], exc.code)
        return False
    record.sent.append(person.ref)
    counts["sent"] += 1
    return True


async def _mail_contract(
    pool: asyncpg.Pool, mail: Mailer, row: Any, people: list[Recipient], counts: dict[str, Any]
) -> None:
    record = Record.load(row["mail"])
    todo = [p for p in people if p.ref not in record.handled()]
    delivered = [await _send_one(mail, row, p, record, counts) for p in todo]
    record.done = all(delivered)
    async with pool.acquire() as conn:
        await conn.execute(RECORD_SQL, row["notification_id"], record.dump(datetime.now(UTC)))
    counts["contracts_done"] += int(record.done)


async def run(
    pool: asyncpg.Pool, mail: Mailer, *, today: date, window_days: int, dry_run: bool = False
) -> dict[str, Any]:
    """Mail every announced contract not mailed yet (see the module docstring). Raises :class:`PermanentError` when
    SES cannot send at all; failed sends are counted (:func:`raise_if_failed` turns them into a retry)."""
    if mail.skip_reason:
        return {"outcome": "skipped", "reason": mail.skip_reason}
    async with pool.acquire() as conn:
        roles = list((await policy_values.require(conn, today, ROLES_KEY)).get(ROLES_KEY))
        pending = await conn.fetch(PENDING_SQL, today, today + timedelta(days=window_days), NOTIFICATION_TYPE)
        people = await recipients(conn, roles) if roles else []
    counts: dict[str, Any] = {
        "outcome": "done",
        "roles": roles,
        "contracts": len(pending),
        "recipients": len(people),
        "contracts_done": 0,
        "sent": 0,
        "refused": 0,
        "failed": 0,
    }
    if not roles:
        return {**counts, "outcome": "off", "reason": f"{ROLES_KEY} is empty"}
    if not people:
        # Nobody to mail yet: nothing is recorded, so the contracts are mailed once someone holds the role.
        return {**counts, "outcome": "no_recipients"}
    if dry_run:
        todo = sum(len([p for p in people if p.ref not in Record.load(r["mail"]).handled()]) for r in pending)
        return {**counts, "outcome": "dry_run", "would_send": todo}
    for row in pending:
        await _mail_contract(pool, mail, row, people, counts)
    return counts


def raise_if_failed(result: dict[str, Any]) -> None:
    if result.get("failed"):
        raise MailFailed(f"{result['failed']} contract expiry e-mail(s) not sent: {result}")
