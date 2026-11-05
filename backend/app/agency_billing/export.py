"""Period export of agency invoices and payments for the accountant (AA-1037 R4 extras G5), instead of an ERP link.

``GET /booking/agency-billing/export.csv?what=invoices|payments&from=YYYY-MM-DD&to=YYYY-MM-DD``
(``agency_billing.read``: admin and finance; not in preview sessions). One CSV (UTF-8 with a BOM so spreadsheet apps
read accents, comma separated, a header row), at most 366 days, ``to`` included:

- **invoices**: every agency invoice **issued** in the period (``issued_on``; paid and void ones included, drafts never:
  they hold no number), with its number, kind, status, agency, the bill-to legal fields of the issue snapshot,
  period, issue and due dates, currency, subtotal, tax, total, approved payments, balance, the VAT e-invoice number and
  the void date;
- **payments**: every agency payment **received** in the period (``received_on``), with its invoice, amount, method,
  bank reference and evidence, status and the maker / checker.

Free-text notes (staff notes, void and reject reasons) are left out. A cell that a spreadsheet would read as a formula
(starting with ``=``, ``+``, ``-``, ``@``, tab or CR) is prefixed with ``'`` (text cells only; amounts stay numbers).
Every export writes one ``booking.audit_logs`` row (who, what, which period, how many rows).
"""

from __future__ import annotations

import csv
import io
import json
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import asyncpg

from app import audit
from app.errors import Invalid

MAX_DAYS = 366
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")

INVOICES_SQL = """
SELECT i.number, i.invoice_kind, i.status, p.agency_name, i.party_tenant_id::text AS tenant_id,
       i.bill_to::text AS bill_to, i.period_start, i.period_end, i.issued_on, i.due_date, i.currency::text AS currency,
       i.subtotal, i.tax_rate, i.tax_amount, i.total, coalesce(a.paid, 0) AS paid,
       CASE WHEN i.status = 'issued' THEN i.total - coalesce(a.paid, 0) ELSE 0 END AS balance, i.e_invoice_no,
       i.voided_at
  FROM booking.agency_invoice i
  JOIN booking.b2b_partners p ON p.tenant_id = i.party_tenant_id
  LEFT JOIN (SELECT invoice_id, sum(amount) AS paid FROM booking.agency_payment WHERE status = 'approved'
              GROUP BY invoice_id) a ON a.invoice_id = i.id
 WHERE i.number IS NOT NULL AND i.issued_on >= $1 AND i.issued_on <= $2
 ORDER BY i.issued_on, i.number
"""
PAYMENTS_SQL = """
SELECT i.number AS invoice_number, i.invoice_kind, p2.agency_name, pm.received_on, pm.amount,
       pm.currency::text AS currency, pm.method, pm.reference, pm.document_ref, pm.status, pm.recorded_by,
       pm.recorded_at, pm.approved_by, pm.approved_at, pm.rejected_by, pm.rejected_at
  FROM booking.agency_payment pm
  JOIN booking.agency_invoice i ON i.id = pm.invoice_id
  JOIN booking.b2b_partners p2 ON p2.tenant_id = i.party_tenant_id
 WHERE pm.received_on >= $1 AND pm.received_on <= $2
 ORDER BY pm.received_on, i.number, pm.recorded_at
"""
INVOICE_COLUMNS = (
    "number",
    "kind",
    "status",
    "agency",
    "tenant_id",
    "bill_to_legal_entity",
    "bill_to_tax_id",
    "bill_to_country",
    "period_start",
    "period_last_day",
    "issued_on",
    "due_date",
    "currency",
    "subtotal",
    "tax_rate",
    "tax_amount",
    "total",
    "paid",
    "balance",
    "e_invoice_no",
    "voided_on",
)
PAYMENT_COLUMNS = (
    "invoice_number",
    "invoice_kind",
    "agency",
    "received_on",
    "amount",
    "currency",
    "method",
    "reference",
    "document_ref",
    "status",
    "recorded_by",
    "recorded_at",
    "approved_by",
    "approved_at",
    "rejected_by",
    "rejected_at",
)


def check_period(start: date, end: date) -> None:
    if end < start:
        raise Invalid("invalid_period", "to must be on or after from")
    if (end - start).days + 1 > MAX_DAYS:
        raise Invalid("invalid_period", f"export at most {MAX_DAYS} days at a time")


def cell(value: Any) -> str:
    """A CSV cell: dates ISO, decimals plain, None empty, formula-looking text neutralised."""
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, date):
        return value.isoformat()
    text = str(value)
    if text.startswith(FORMULA_START):
        return "'" + text
    return text


def _bill_to(raw: Any) -> dict[str, Any]:
    data = json.loads(raw) if isinstance(raw, str) else (raw or {})
    return data if isinstance(data, dict) else {}


def invoice_row(r: Any) -> list[Any]:
    bill_to = _bill_to(r["bill_to"])
    return [
        r["number"],
        r["invoice_kind"],
        r["status"],
        r["agency_name"],
        r["tenant_id"],
        bill_to.get("legal_entity_name"),
        bill_to.get("tax_id"),
        bill_to.get("billing_country"),
        r["period_start"],
        r["period_end"] - timedelta(days=1),  # the last day billed (period_end is exclusive)
        r["issued_on"],
        r["due_date"],
        r["currency"].strip(),
        Decimal(r["subtotal"]),
        Decimal(r["tax_rate"]),
        Decimal(r["tax_amount"]),
        Decimal(r["total"]),
        Decimal(r["paid"]),
        Decimal(r["balance"]),
        r["e_invoice_no"],
        r["voided_at"].date() if r["voided_at"] is not None else None,
    ]


def payment_row(r: Any) -> list[Any]:
    return [
        r["invoice_number"],
        r["invoice_kind"],
        r["agency_name"],
        r["received_on"],
        Decimal(r["amount"]),
        r["currency"].strip(),
        r["method"],
        r["reference"],
        r["document_ref"],
        r["status"],
        r["recorded_by"],
        r["recorded_at"].isoformat() if r["recorded_at"] else None,
        r["approved_by"],
        r["approved_at"].isoformat() if r["approved_at"] else None,
        r["rejected_by"],
        r["rejected_at"].isoformat() if r["rejected_at"] else None,
    ]


def to_csv(header: tuple[str, ...], rows: list[list[Any]]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow([cell(v) for v in row])
    return "﻿" + buf.getvalue()


def file_name(what: str, start: date, end: date) -> str:
    return f"agency-{what}-{start.isoformat()}_{end.isoformat()}.csv"


async def export(conn: asyncpg.Connection, what: str, start: date, end: date, actor: str) -> tuple[str, int]:
    """(CSV text, row count) of ``what`` = invoices | payments in [start, end]; audited."""
    check_period(start, end)
    if what == "invoices":
        rows = [invoice_row(r) for r in await conn.fetch(INVOICES_SQL, start, end)]
        text = to_csv(INVOICE_COLUMNS, rows)
    elif what == "payments":
        rows = [payment_row(r) for r in await conn.fetch(PAYMENTS_SQL, start, end)]
        text = to_csv(PAYMENT_COLUMNS, rows)
    else:
        raise Invalid("invalid_export", "what is invoices or payments")
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=actor,
        action="agency_billing.exported",
        entity_type="agency_billing_export",
        entity_id=None,
        after={"what": what, "from": start.isoformat(), "to": end.isoformat(), "rows": len(rows)},
        entity_label=f"{what} {start.isoformat()} to {end.isoformat()}",
        summary=f"Exported {len(rows)} agency {what} ({start.isoformat()} to {end.isoformat()}) as CSV",
    )
    return text, len(rows)
