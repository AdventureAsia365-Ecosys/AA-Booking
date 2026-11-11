"""The calling agency's own AA invoices (AA-1037, contract booking.b2b.v1 1.5, root ADR 0019 d17: the data of the CRM
"Bookings with AA" page):

- ``GET /booking/b2b/v1/invoices`` (``kind`` saas | settlement, ``status`` issued | paid | void, ``limit``,
  ``offset``): a page of the agency's invoices, newest first, without lines;
- ``GET /booking/b2b/v1/invoices/{number}``: one of them with its lines, its approved payments and the issuer snapshot
  (how to pay); 404 ``invoice_not_found`` for a draft, another agency's number or anything that is not a number;
- ``GET /booking/b2b/v1/billing-status``: the agency's row of ``booking.v_tenant_billing_status`` (the credit stop
  before a 409 ``credit_hold_overdue``, and the paid-through day of the software fee).

**Scope.** The agency is the partner :mod:`app.b2b.access` resolved from the token and ``X-AA-Tenant-Id`` (its client
must be the partner's ``m2m_client_id``); every read is filtered by that partner's ``tenant_id`` in SQL
(``agency_invoice.party_tenant_id``), never by anything the caller sends. A draft (no number, still changing) is never
listed or answered.

**Field allow-list.** The staff row (:mod:`app.agency_billing.invoices`) carries the staff note, the void reason, who
issued / voided / recorded / approved, the payment bank reference and document, the bill-to snapshot (the agency's
billing contact, personal data the B2B contract never carries) and the amount waiting for approval. The functions below
copy only the contract fields into a new dict, key by key; the router's ``extra="forbid"`` response models are the
second guard (an unexpected key is a 500, never a leak). The issuer snapshot is copied key by key too
(:data:`ISSUER_KEYS`), so a key added to the snapshot later stays inside Booking until the contract names it.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

import asyncpg

from app.agency_billing import invoices
from app.b2b import terms
from app.b2b.errors import B2BError

DEFAULT_LIMIT = 50
MAX_LIMIT = 100
MAX_OFFSET = 10_000
CENT = Decimal("0.01")
_ONE_DAY = timedelta(days=1)
# The AA entity's snapshot taken at issue (app.agency_billing.invoices.issuer_snapshot + the tax line of the rule).
ISSUER_KEYS = (
    "code",
    "legal_entity",
    "tax_code",
    "registered_office",
    "country_iso2",
    "invoice_inbox",
    "payment_instructions",
    "tax_line",
)
SUMMARY_KEYS = ("number", "invoice_kind", "status", "period_start", "currency", "issued_on", "due_date", "e_invoice_no")
MONEY_KEYS = ("subtotal", "tax_amount", "total", "paid", "balance")

BILLING_STATUS_SQL = """
SELECT status, oldest_overdue_days, overdue_invoices, overdue_amount::text AS overdue_amount, paid_through,
       warn_after_days, suspend_after_days
FROM booking.v_tenant_billing_status WHERE tenant_id = $1::uuid
"""


def amount(value: Any) -> str:
    """A stored amount as the contract's decimal string with 2 places ("1500000.00", "-20.00")."""
    return format(Decimal(str(value if value is not None else 0)).quantize(CENT), "f")


def _rate(value: Any) -> str:
    return format(Decimal(str(value if value is not None else 0)).quantize(Decimal("0.0001")), "f")


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


def summary(row: dict[str, Any]) -> dict[str, Any]:
    """The list item of one invoice: contract fields only (see the module docstring)."""
    out: dict[str, Any] = {k: row.get(k) for k in SUMMARY_KEYS}
    # The stored period_end is exclusive (the first day after the period); the agency sees the last day it covers.
    out["period_end"] = row["period_end"] - _ONE_DAY if row.get("period_end") is not None else None
    for k in MONEY_KEYS:
        out[k] = amount(row.get(k))
    out["overdue_days"] = int(row.get("overdue_days") or 0)
    out["currency"] = str(row["currency"])
    return out


def issuer(snapshot: Any) -> dict[str, Any] | None:
    if not isinstance(snapshot, dict):
        return None
    return {k: _text(snapshot.get(k)) for k in ISSUER_KEYS}


# AA-1073 rest (bk-048): a departure fee line reaches the agency as an ``adjustment`` (its description says
# "Departure fee ..."): the contract 1.5 line_role enum has no departure_fee and the AA-Partner CRM refuses a role it
# does not know. The CRM learns the role first; a later minor then sends it as it is.
AGENCY_LINE_ROLE = {"departure_fee": "adjustment"}


def line(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "line_no": int(row["line_no"]),
        "line_role": AGENCY_LINE_ROLE.get(row["line_role"], row["line_role"]),
        "description": row["description"],
        "quantity": amount(row["quantity"]),
        "unit_price": amount(row["unit_price"]),
        "amount": amount(row["amount"]),
        "hold_id": row.get("source_hold_id"),
        "booking_id": row.get("booking_id"),
        "voided": bool(row["voided"]),
    }


def payment(row: dict[str, Any]) -> dict[str, Any]:
    return {"received_on": row["received_on"], "amount": amount(row["amount"]), "currency": str(row["currency"])}


def detail(row: dict[str, Any]) -> dict[str, Any]:
    """One invoice with its lines, approved payments and issuer: contract fields only."""
    return {
        **summary(row),
        "tax_rate": _rate(row.get("tax_rate")),
        "footer_wording": row.get("footer_wording"),
        "issuer": issuer(row.get("issuer")),
        "lines": [line(r) for r in row.get("lines") or []],
        "payments": [payment(r) for r in row.get("payments") or []],
    }


async def list_for_agency(
    conn: asyncpg.Connection,
    *,
    tenant_id: str,
    kind: str | None,
    status: str | None,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    page = await invoices.list_invoices(
        conn,
        kind=kind,
        status=status,
        tenant_id=tenant_id,
        limit=limit,
        offset=offset,
        statuses=invoices.AGENCY_STATUSES,
    )
    return {
        "items": [summary(r) for r in page["items"]],
        "total": int(page["total"] or 0),
        "limit": limit,
        "offset": offset,
    }


async def get_for_agency(conn: asyncpg.Connection, number: str, *, tenant_id: str) -> dict[str, Any]:
    row = await invoices.get_for_agency(conn, number, tenant_id=tenant_id)
    if row is None:
        raise B2BError("invoice_not_found", "no invoice of this agency with this number")
    return detail(row)


async def billing_status(conn: asyncpg.Connection, *, tenant_id: str) -> dict[str, Any]:
    """The agency's credit status (as ``GET /terms`` ``credit``) plus the paid-through day of its software fee."""
    row = await conn.fetchrow(BILLING_STATUS_SQL, tenant_id)
    return {**terms.credit_out(row), "paid_through": row["paid_through"] if row is not None else None}
