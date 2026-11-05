"""Agency invoices (bk-041 / bk-042; root ADR 0019 d9, b14 / b17).

Life: ``draft`` (written by the draft jobs or edited by finance: adjustment lines, a note) -> ``issued`` (a person
issues it: the next gap-free number of its series, the issuer and bill-to snapshots, the tax rule in force, the due
date) -> ``paid`` (the database marks it once approved payments cover the total, bk-042 ``agency_payment_settle``) or
``void`` (an issued invoice with no approved payment; its lines are marked voided so the billed events can be drafted
again). A draft may be deleted (it holds no number). The legal VAT e-invoice is issued by the accountant in the
e-invoice software; its number is recorded once on the issued invoice (``e_invoice_no``, phase 1, Decree 70/2025).

Numbers: ``AAS-<year>-<6 digits>`` (software fee) and ``AAT-<year>-<6 digits>`` (tour settlement), from
``booking.invoice_sequence`` in the issue transaction (row lock until commit, so a rollback leaves no gap).
"""

from __future__ import annotations

import json
import uuid
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import asyncpg

from app import audit
from app.agency_billing import cis, config
from app.errors import Conflict, Invalid, NotFound
from app.ops.money import line_amount, looks_like_card_number

SERIES = {"saas": "AAS", "settlement": "AAT"}
KIND_LABEL = {"saas": "Software fee", "settlement": "Tour settlement"}
MAX_LINES = 500
CENT = Decimal("0.01")

INVOICE_COLUMNS = """
    i.id::text AS id, i.number, i.party_tenant_id, i.invoice_kind, i.status, i.period_start, i.period_end, i.currency,
    i.subtotal, i.tax_rule_id::text AS tax_rule_id, i.tax_rate, i.tax_amount, i.total,
    i.billing_entity_id::text AS billing_entity_id, i.issuer, i.bill_to, i.footer_wording, i.issued_on, i.due_date,
    i.issued_at, i.issued_by, i.e_invoice_no, i.e_invoice_recorded_by, i.e_invoice_recorded_at, i.staff_note,
    i.voided_at, i.voided_by, i.void_reason, i.source, i.created_by, i.created_at, i.updated_at,
    p.agency_name,
    coalesce(m.approved, 0) AS paid, coalesce(m.waiting, 0) AS waiting,
    CASE WHEN i.status = 'issued' THEN i.total - coalesce(m.approved, 0) ELSE 0 END AS balance,
    CASE WHEN i.status = 'issued' AND i.due_date < current_date AND i.total - coalesce(m.approved, 0) > 0
         THEN current_date - i.due_date ELSE 0 END AS overdue_days
"""
INVOICE_FROM = """
  FROM booking.agency_invoice i
  JOIN booking.b2b_partners p ON p.tenant_id = i.party_tenant_id
  LEFT JOIN LATERAL (
      SELECT sum(amount) FILTER (WHERE status = 'approved') AS approved,
             sum(amount) FILTER (WHERE status = 'recorded') AS waiting
        FROM booking.agency_payment WHERE invoice_id = i.id
  ) m ON true
"""
LINE_SQL = """
SELECT l.line_no, l.line_role, l.product, l.plan_key, l.source_hold_id::text AS source_hold_id, l.description,
       l.quantity, l.unit_price, l.amount, l.voided, l.created_by, l.created_at, tc.code AS trip_case_code
  FROM booking.agency_invoice_line l
  LEFT JOIN booking.b2b_holds h ON h.id = l.source_hold_id
  LEFT JOIN booking.trip_cases tc ON tc.id = h.trip_case_id
 WHERE l.invoice_id = $1::uuid ORDER BY l.line_no
"""
PAYMENT_COLUMNS = """
    id::text AS id, invoice_id::text AS invoice_id, amount, currency, method, received_on, reference, document_ref,
    note, status, recorded_by, recorded_at, approved_by, approved_at, rejected_by, rejected_at, reject_reason
"""


def _uuid(value: str) -> str | None:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        return None


def _out(row: Any) -> dict[str, Any]:
    d = dict(row)
    for k in ("issuer", "bill_to"):
        if isinstance(d.get(k), str):
            d[k] = json.loads(d[k])
    if isinstance(d.get("party_tenant_id"), uuid.UUID):
        d["party_tenant_id"] = str(d["party_tenant_id"])
    return d


async def list_invoices(
    conn: asyncpg.Connection,
    *,
    kind: str | None = None,
    status: str | None = None,
    tenant_id: str | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    where, args = ["true"], []

    def arg(v: Any) -> str:
        args.append(v)
        return f"${len(args)}"

    if kind:
        where.append(f"i.invoice_kind = {arg(kind)}")
    if status == "overdue":
        where.append("i.status = 'issued' AND i.due_date < current_date AND i.total - coalesce(m.approved, 0) > 0")
    elif status == "waiting":
        where.append("coalesce(m.waiting, 0) > 0")
    elif status and status != "all":
        where.append(f"i.status = {arg(status)}")
    if tenant_id:
        where.append(f"i.party_tenant_id = {arg(tenant_id)}::uuid")
    if q:
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where.append(
            f"(i.number ILIKE {arg(like)} OR p.agency_name ILIKE ${len(args)} OR i.e_invoice_no ILIKE ${len(args)})"
        )
    cond = " AND ".join(where)
    total = await conn.fetchval(f"SELECT count(*) {INVOICE_FROM} WHERE {cond}", *args)  # noqa: S608
    rows = await conn.fetch(
        f"SELECT {INVOICE_COLUMNS} {INVOICE_FROM} WHERE {cond}"  # noqa: S608
        f" ORDER BY i.created_at DESC, i.id LIMIT {arg(limit)} OFFSET {arg(offset)}",
        *args,
    )
    return {"items": [_out(r) for r in rows], "total": total, "limit": limit, "offset": offset}


async def _row(conn: asyncpg.Connection, ref: str, *, lock: bool = False) -> dict[str, Any]:
    key = _uuid(ref)
    cond = "i.id = $1::uuid" if key else "i.number = $1"
    row = await conn.fetchrow(
        f"SELECT {INVOICE_COLUMNS} {INVOICE_FROM} WHERE {cond}",  # noqa: S608
        key or ref,
    )
    if row is None:
        raise NotFound("invoice_not_found", "no agency invoice with this id or number")
    if lock:
        await conn.execute("SELECT 1 FROM booking.agency_invoice WHERE id = $1::uuid FOR UPDATE", row["id"])
        row = await conn.fetchrow(f"SELECT {INVOICE_COLUMNS} {INVOICE_FROM} WHERE i.id = $1::uuid", row["id"])  # noqa: S608
    return _out(row)


async def get(conn: asyncpg.Connection, ref: str) -> dict[str, Any]:
    inv = await _row(conn, ref)
    lines = [dict(r) for r in await conn.fetch(LINE_SQL, inv["id"])]
    payments = [
        dict(r)
        for r in await conn.fetch(
            f"SELECT {PAYMENT_COLUMNS} FROM booking.agency_payment WHERE invoice_id = $1::uuid ORDER BY recorded_at",  # noqa: S608
            inv["id"],
        )
    ]
    return {**inv, "lines": lines, "payments": payments}


async def recompute(conn: asyncpg.Connection, invoice_id: str) -> None:
    """subtotal = the lines, tax = round(subtotal x tax_rate), total = both (a draft only; the guard keeps the rest)."""
    rate = await conn.fetchval("SELECT tax_rate FROM booking.agency_invoice WHERE id = $1::uuid", invoice_id)
    subtotal = await conn.fetchval(
        "SELECT coalesce(sum(amount), 0) FROM booking.agency_invoice_line WHERE invoice_id = $1::uuid", invoice_id
    )
    tax = (Decimal(subtotal) * Decimal(rate or 0)).quantize(CENT, rounding=ROUND_HALF_UP)
    await conn.execute(
        "UPDATE booking.agency_invoice SET subtotal = $2::numeric, tax_amount = $3::numeric,"
        " total = $2::numeric + $3::numeric"
        " WHERE id = $1::uuid",
        invoice_id,
        subtotal,
        tax,
    )


async def _audit(conn: asyncpg.Connection, actor: str, action: str, inv: dict[str, Any], after: Any = None,
                 before: Any = None) -> None:  # fmt: skip
    label = inv.get("number") or f"Draft {KIND_LABEL[inv['invoice_kind']].lower()} {inv['period_start']}"
    await audit.record(
        conn,
        actor_type="staff" if not actor.startswith("system:") else "system",
        actor_ref=actor,
        action=action,
        entity_type="agency_invoice",
        entity_id=inv["id"],
        before=before,
        after=json.loads(json.dumps(after, default=str)) if after is not None else None,
        entity_label=f"{label} · {inv.get('agency_name') or ''}"[:80],
        summary=action.replace("agency_invoice.", "Agency invoice ").replace("_", " "),
    )


def _draft_only(inv: dict[str, Any]) -> None:
    if inv["status"] != "draft":
        raise Conflict("invoice_not_draft", f"the invoice is {inv['status']}: only a draft changes")


def _no_card(*texts: str | None) -> None:
    if any(looks_like_card_number(t) for t in texts):
        raise Invalid("card_number_not_allowed", "a payment card number never goes into Booking")


async def add_line(conn: asyncpg.Connection, ref: str, body: dict[str, Any], actor: str) -> dict[str, Any]:
    """An ``adjustment`` line on a draft (a contract price, a credit): description, quantity, unit price."""
    _no_card(body["description"])
    async with conn.transaction():
        inv = await _row(conn, ref, lock=True)
        _draft_only(inv)
        n = await conn.fetchval(
            "SELECT coalesce(max(line_no), 0) + 1 FROM booking.agency_invoice_line WHERE invoice_id = $1::uuid",
            inv["id"],
        )
        if n > MAX_LINES:
            raise Conflict("too_many_lines", f"an invoice holds at most {MAX_LINES} lines")
        quantity = Decimal(body.get("quantity") or 1)
        amount = line_amount(quantity, body["unit_price"])
        await conn.execute(
            "INSERT INTO booking.agency_invoice_line (invoice_id, invoice_kind, line_no, line_role, description,"
            " quantity, unit_price, amount, created_by) VALUES ($1::uuid, $2, $3, 'adjustment', $4, $5, $6, $7, $8)",
            inv["id"],
            inv["invoice_kind"],
            n,
            body["description"].strip(),
            quantity,
            body["unit_price"],
            amount,
            actor,
        )
        await recompute(conn, inv["id"])
        await _audit(conn, actor, "agency_invoice.line_added", inv,
                     {"line_no": n, "description": body["description"], "amount": amount})  # fmt: skip
    return await get(conn, inv["id"])


async def remove_line(conn: asyncpg.Connection, ref: str, line_no: int, actor: str) -> dict[str, Any]:
    """Removes a line of a draft (a job line comes back on the next draft run unless it was billed elsewhere)."""
    async with conn.transaction():
        inv = await _row(conn, ref, lock=True)
        _draft_only(inv)
        gone = await conn.fetchrow(
            "DELETE FROM booking.agency_invoice_line WHERE invoice_id = $1::uuid AND line_no = $2"
            " RETURNING line_role, description, amount",
            inv["id"],
            line_no,
        )
        if gone is None:
            raise NotFound("line_not_found", "no line with this number on the invoice")
        await recompute(conn, inv["id"])
        await _audit(conn, actor, "agency_invoice.line_removed", inv, {"line_no": line_no, **dict(gone)})
    return await get(conn, inv["id"])


async def patch(conn: asyncpg.Connection, ref: str, changes: dict[str, Any], actor: str) -> dict[str, Any]:
    """``staff_note`` (any status) and ``e_invoice_no`` (once, on an issued or paid invoice)."""
    async with conn.transaction():
        inv = await _row(conn, ref, lock=True)
        after: dict[str, Any] = {}
        if "staff_note" in changes:
            note = (changes["staff_note"] or "").strip() or None
            _no_card(note)
            await conn.execute("UPDATE booking.agency_invoice SET staff_note = $2 WHERE id = $1::uuid", inv["id"], note)
            after["staff_note"] = note
        if changes.get("e_invoice_no") is not None:
            if inv["status"] not in ("issued", "paid"):
                raise Conflict("invoice_not_issued", "the e-invoice number goes on an issued invoice")
            if inv["e_invoice_no"] is not None:
                raise Conflict("e_invoice_no_set", "the e-invoice number is already recorded")
            no = changes["e_invoice_no"].strip()
            await conn.execute(
                "UPDATE booking.agency_invoice SET e_invoice_no = $2, e_invoice_recorded_by = $3,"
                " e_invoice_recorded_at = now() WHERE id = $1::uuid",
                inv["id"],
                no,
                actor,
            )
            after["e_invoice_no"] = no
        if after:
            await _audit(conn, actor, "agency_invoice.updated", inv, after)
    return await get(conn, inv["id"])


async def delete_draft(conn: asyncpg.Connection, ref: str, actor: str) -> None:
    async with conn.transaction():
        inv = await _row(conn, ref, lock=True)
        _draft_only(inv)
        await conn.execute("DELETE FROM booking.agency_invoice WHERE id = $1::uuid", inv["id"])
        await _audit(conn, actor, "agency_invoice.draft_deleted", inv,
                     {"total": inv["total"], "period_start": inv["period_start"]})  # fmt: skip


TAKE_NUMBER_SQL = """
INSERT INTO booking.invoice_sequence (series, year, last_no) VALUES ($1, $2, 1)
ON CONFLICT (series, year) DO UPDATE SET last_no = booking.invoice_sequence.last_no + 1, updated_at = now()
RETURNING last_no
"""


async def take_number(conn: asyncpg.Connection, kind: str, year: int) -> str:
    """The next number of the kind's series for ``year``, inside the caller's transaction (gap-free)."""
    n = await conn.fetchval(TAKE_NUMBER_SQL, SERIES[kind], year)
    return f"{SERIES[kind]}-{year:04d}-{n:06d}"


def issuer_snapshot(entity: dict[str, Any]) -> dict[str, Any]:
    keys = ("code", "legal_entity", "tax_code", "registered_office", "country_iso2", "invoice_inbox",
            "payment_instructions")  # fmt: skip
    return {k: entity.get(k) for k in keys}


async def issue(conn: asyncpg.Connection, ref: str, actor: str, *, due_date: date | None = None) -> dict[str, Any]:
    """Issues a draft: the default billing entity, the agency's terms and the tax rule in force today, the bill-to
    snapshot, the next number. 409 ``invoice_not_draft`` / ``no_lines`` / ``no_billing_entity`` / ``no_terms`` /
    ``negative_total``; 422 ``invalid_due_date`` (before today). A zero total is paid at once."""
    async with conn.transaction():
        inv = await _row(conn, ref, lock=True)
        _draft_only(inv)
        if not await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM booking.agency_invoice_line WHERE invoice_id = $1::uuid)", inv["id"]
        ):
            raise Conflict("no_lines", "an invoice without lines is not issued")
        entity = await config.default_entity(conn)
        if entity is None:
            raise Conflict("no_billing_entity", "set a default billing entity first (Agency billing > Settings)")
        terms = await config.terms_for(conn, inv["party_tenant_id"])
        if terms is None:
            raise Conflict("no_terms", "set the default billing terms first")
        today: date = await conn.fetchval("SELECT current_date")
        if due_date is None:
            days = terms["saas_due_days"] if inv["invoice_kind"] == "saas" else terms["settlement_due_days"]
            due_date = today + timedelta(days=int(days))
        elif due_date < today:
            raise Invalid("invalid_due_date", "the due date cannot be before the issue day")
        bill_to = await cis.bill_to(conn, inv["party_tenant_id"], inv["agency_name"])
        rule = await config.pick_tax_rule(conn, inv["invoice_kind"], bill_to.get("billing_country"), today)
        await conn.execute(
            "UPDATE booking.agency_invoice SET tax_rule_id = $2::uuid, tax_rate = $3, footer_wording = $4"
            " WHERE id = $1::uuid",
            inv["id"],
            rule["id"] if rule else None,
            Decimal(rule["rate"]) if rule else Decimal(0),
            rule["footer_wording"] if rule else None,
        )
        await recompute(conn, inv["id"])
        total = await conn.fetchval("SELECT total FROM booking.agency_invoice WHERE id = $1::uuid", inv["id"])
        if total < 0:
            raise Conflict("negative_total", "the lines add up below zero: adjust the draft first")
        number = await take_number(conn, inv["invoice_kind"], today.year)
        issuer = issuer_snapshot(entity)
        if rule:
            issuer["tax_line"] = rule["line_wording"]
        await conn.execute(
            "UPDATE booking.agency_invoice SET status = 'issued', number = $2, issued_at = now(), issued_by = $3,"
            " issued_on = $4, due_date = $5, billing_entity_id = $6::uuid, issuer = $7::jsonb, bill_to = $8::jsonb"
            " WHERE id = $1::uuid",
            inv["id"],
            number,
            actor,
            today,
            due_date,
            entity["id"],
            json.dumps(issuer),
            json.dumps(bill_to),
        )
        if total == 0:
            await conn.execute("UPDATE booking.agency_invoice SET status = 'paid' WHERE id = $1::uuid", inv["id"])
        await _audit(conn, actor, "agency_invoice.issued", {**inv, "number": number},
                     {"number": number, "total": total, "due_date": due_date,
                      "bill_to_source": bill_to["source"]})  # fmt: skip
    return await get(conn, inv["id"])


async def void(conn: asyncpg.Connection, ref: str, reason: str, actor: str) -> dict[str, Any]:
    """Voids an issued invoice that has no approved payment and none waiting; its lines are marked voided."""
    _no_card(reason)
    async with conn.transaction():
        inv = await _row(conn, ref, lock=True)
        if inv["status"] != "issued":
            raise Conflict("invoice_not_issued", f"the invoice is {inv['status']}: only an issued invoice is voided")
        if Decimal(inv["paid"]) > 0:
            raise Conflict("invoice_has_payments", "an invoice with an approved payment is not voided")
        if Decimal(inv["waiting"]) > 0:
            raise Conflict("payments_waiting", "approve or reject the waiting payments first")
        await conn.execute(
            "UPDATE booking.agency_invoice SET status = 'void', voided_at = now(), voided_by = $2, void_reason = $3"
            " WHERE id = $1::uuid",
            inv["id"],
            actor,
            reason.strip(),
        )
        await conn.execute(
            "UPDATE booking.agency_invoice_line SET voided = true WHERE invoice_id = $1::uuid", inv["id"]
        )
        await _audit(conn, actor, "agency_invoice.voided", inv, {"reason": reason.strip()})
    return await get(conn, inv["id"])


async def summary(conn: asyncpg.Connection) -> dict[str, Any]:
    """The KPI strip of the Agency invoices page: open, overdue and waiting amounts per currency, drafts to issue."""
    rows = await conn.fetch(
        f"""SELECT i.currency,
                   count(*) FILTER (WHERE i.status = 'draft') AS drafts,
                   coalesce(sum(i.total - coalesce(m.approved, 0)) FILTER (WHERE i.status = 'issued'), 0) AS open,
                   coalesce(sum(i.total - coalesce(m.approved, 0)) FILTER (
                       WHERE i.status = 'issued' AND i.due_date < current_date), 0) AS overdue,
                   coalesce(sum(m.waiting), 0) AS waiting
              {INVOICE_FROM}
             GROUP BY i.currency ORDER BY i.currency"""  # noqa: S608
    )
    return {"by_currency": [dict(r) for r in rows]}
