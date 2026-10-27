"""My Payments: what a signed-in traveller may see of their own payment requests (AA-682 S5; S252 design §5, §7, §8,
critic note 10; contract ``booking.customer.v1``).

Scope (every rule in SQL, never filtered in Python after a wider read):

- the traveller's verified identity ``(iss, sub)`` → ``booking.customer_identities`` (bk-024) → one **live** customer
  (``deleted_at IS NULL``). No identity row (a lead from a guest hand-off is never linked to a sign-in, design §11) →
  no invoice: the list is empty, every number is 404;
- invoices of that customer's **B2C trip cases** (``trip_cases.customer_id``, critic note 10: the case's customer, so a
  later merge that re-points the case carries its invoices along), case not deleted;
- the invoice is not deleted and not a ``draft`` (a draft was never sent to the customer); ``sent``,
  ``partially_paid``, ``paid`` and ``cancelled`` are shown.

Anything else is ``404 invoice_not_found`` — another customer's number, a draft, a B2B case, an unknown or malformed
number: the answer never says which.

Field policy (allow-list, ``booking.customer.v1``): invoice number, status, document type, currency, total, paid
(verified receipts - verified refunds), balance, next due date, overdue amount, issue time; the trip's code, dates and
TripPlanner trip id; schedule rows (sequence, kind, amount, due date, remaining, state); **verified, non-voided**
payments only (ref, kind, amount, currency, method, received date); the payment instructions text while money is owed.
Never a staff member (sent / recorded / verified / voided by), an evidence file, a bank reference or note, an internal
note, a cancel / void reason, a bill-to snapshot, a net cost or anything about a supplier. The router's response
models drop any other key; :data:`INVOICE_KEYS` and the unit tests pin the list.

Schedule states map the bk-028 view (``v_payment_schedule_status``) to the customer chips: ``scheduled`` → Scheduled,
``due`` → ``due_soon`` (Due soon), ``paid`` → Paid, ``overdue`` → Overdue; a cancelled invoice's rows are ``cancelled``.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from typing import Any

import asyncpg

from app.auth.customer import Traveller
from app.errors import NotFound

log = logging.getLogger(__name__)

MAX_INVOICES = 50
MAX_INSTRUCTIONS = 2000
NUMBER_RE = re.compile(r"^INV-[0-9]{6,12}$")
OWED_STATUSES = frozenset({"sent", "partially_paid"})
SCHEDULE_STATE = {"scheduled": "scheduled", "due": "due_soon", "paid": "paid", "overdue": "overdue"}

INVOICE_KEYS = (
    "invoice_number",
    "status",
    "document_type",
    "currency",
    "total_amount",
    "paid_amount",
    "balance",
    "next_due_date",
    "overdue_amount",
    "issued_at",
    "trip",
    "schedule",
    "payments",
    "payment_instructions",
)

CUSTOMER_SQL = """
SELECT c.id::text
  FROM booking.customer_identities ci
  JOIN booking.customers c ON c.id = ci.customer_id
 WHERE ci.iss = $1 AND ci.sub = $2 AND c.deleted_at IS NULL
"""

# The one scope every read goes through ($1 = the customer id).
OWN_INVOICES = """
  FROM booking.invoices i
  JOIN booking.trip_cases tc ON tc.id = i.trip_case_id
  JOIN booking.v_invoice_balance b ON b.invoice_id = i.id
 WHERE tc.customer_id = $1::uuid
   AND tc.channel = 'b2c'
   AND tc.deleted_at IS NULL
   AND i.bill_to_kind = 'customer'
   AND i.deleted_at IS NULL
   AND i.status <> 'draft'
"""
INVOICE_COLUMNS = """
SELECT i.id::text AS id, i.invoice_number, i.status, i.document_type, i.currency,
       i.total_amount::text AS total_amount, b.net_paid::text AS paid_amount, b.balance::text AS balance,
       b.next_due_date, b.overdue_amount::text AS overdue_amount, i.issued_at,
       tc.code AS trip_code, tc.start_date AS trip_start_date, tc.end_date AS trip_end_date,
       CASE WHEN tc.source_system = 'tripplanner' THEN tc.source_id END AS planner_trip_id
"""
LIST_SQL = INVOICE_COLUMNS + OWN_INVOICES + " ORDER BY i.issued_at DESC, i.invoice_number DESC LIMIT $2"
ONE_SQL = INVOICE_COLUMNS + OWN_INVOICES + " AND i.invoice_number = $2"

SCHEDULE_SQL = """
SELECT s.invoice_id::text AS invoice_id, s.seq, s.kind, s.amount::text AS amount, s.due_date,
       s.remaining_amount::text AS remaining_amount, s.state
  FROM booking.v_payment_schedule_status s
 WHERE s.invoice_id = ANY($1::uuid[])
 ORDER BY s.due_date, s.seq
"""
PAYMENTS_SQL = """
SELECT p.invoice_id::text AS invoice_id, p.ref, p.kind, p.amount::text AS amount, p.currency, p.method, p.received_on
  FROM booking.payments p
 WHERE p.invoice_id = ANY($1::uuid[]) AND p.verified_at IS NOT NULL AND p.voided_at IS NULL
 ORDER BY p.received_on, p.ref
"""
INSTRUCTIONS_SQL = "SELECT value FROM booking.settings WHERE key = 'payment_instructions'"


def _text(value: Any) -> str | None:
    """The ``payment_instructions`` setting as plain text (a JSON string); anything else is not shown."""
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        return None
    if not isinstance(parsed, str) or not parsed.strip():
        return None
    return parsed.strip()[:MAX_INSTRUCTIONS]


def _iso(value: date | datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


async def customer_of(conn: asyncpg.Connection, traveller: Traveller) -> str | None:
    """The live customer linked to this sign-in identity, or None."""
    return await conn.fetchval(CUSTOMER_SQL, traveller.iss, traveller.sub)


def _schedule_row(row: Any, invoice_status: str) -> dict[str, Any]:
    state = "cancelled" if invoice_status == "cancelled" else SCHEDULE_STATE.get(row["state"], "scheduled")
    return {
        "seq": row["seq"],
        "kind": row["kind"],
        "amount": row["amount"],
        "due_date": _iso(row["due_date"]),
        "remaining_amount": "0.00" if state == "cancelled" else row["remaining_amount"],
        "state": state,
    }


def _payment_row(row: Any) -> dict[str, Any]:
    return {
        "ref": row["ref"],
        "kind": row["kind"],
        "amount": row["amount"],
        "currency": row["currency"],
        "method": row["method"],
        "received_on": _iso(row["received_on"]),
    }


async def _invoices(conn: asyncpg.Connection, rows: list[Any]) -> list[dict[str, Any]]:
    if not rows:
        return []
    ids = [r["id"] for r in rows]
    schedule: dict[str, list[Any]] = {i: [] for i in ids}
    paid: dict[str, list[Any]] = {i: [] for i in ids}
    for s in await conn.fetch(SCHEDULE_SQL, ids):
        schedule[s["invoice_id"]].append(s)
    for p in await conn.fetch(PAYMENTS_SQL, ids):
        paid[p["invoice_id"]].append(p)
    instructions = _text(await conn.fetchval(INSTRUCTIONS_SQL))
    out = []
    for r in rows:
        status = r["status"]
        out.append(
            {
                "invoice_number": r["invoice_number"],
                "status": status,
                "document_type": r["document_type"],
                "currency": r["currency"],
                "total_amount": r["total_amount"],
                "paid_amount": r["paid_amount"],
                "balance": r["balance"],
                "next_due_date": _iso(r["next_due_date"]) if status != "cancelled" else None,
                "overdue_amount": r["overdue_amount"],
                "issued_at": _iso(r["issued_at"]),
                "trip": {
                    "code": r["trip_code"],
                    "planner_trip_id": r["planner_trip_id"],
                    "start_date": _iso(r["trip_start_date"]),
                    "end_date": _iso(r["trip_end_date"]),
                },
                "schedule": [_schedule_row(s, status) for s in schedule[r["id"]]],
                "payments": [_payment_row(p) for p in paid[r["id"]]],
                # Bank details only while something is owed (never on a paid or a cancelled request).
                "payment_instructions": instructions if status in OWED_STATUSES else None,
            }
        )
    return out


async def list_payments(conn: asyncpg.Connection, traveller: Traveller) -> dict[str, Any]:
    """``GET /customer/v1/payments``: every visible invoice of the traveller, newest first (at most 50)."""
    customer = await customer_of(conn, traveller)
    if customer is None:
        log.info("customer_payments route=list outcome=no_customer")
        return {"invoices": []}
    rows = await conn.fetch(LIST_SQL, customer, MAX_INVOICES)
    log.info("customer_payments route=list outcome=ok invoices=%s", len(rows))
    return {"invoices": await _invoices(conn, rows)}


async def get_invoice(conn: asyncpg.Connection, traveller: Traveller, number: str) -> dict[str, Any]:
    """``GET /customer/v1/invoices/{number}``: one visible invoice, else 404 ``invoice_not_found``."""
    wanted = (number or "").strip().upper()
    customer = await customer_of(conn, traveller) if NUMBER_RE.fullmatch(wanted) else None
    row = await conn.fetchrow(ONE_SQL, customer, wanted) if customer is not None else None
    if row is None:
        log.info("customer_payments route=invoice outcome=not_found")
        raise NotFound("invoice_not_found", "invoice not found")
    log.info("customer_payments route=invoice outcome=ok")
    return (await _invoices(conn, [row]))[0]
