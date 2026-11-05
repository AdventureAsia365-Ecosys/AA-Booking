"""Agency payments (bk-041 / bk-042; root ADR 0019 b14: no payment provider, money is marked by hand).

**Maker-checker.** Finance (or an admin) records a payment against an issued invoice with a document reference (the
evidence: a bank statement line, a slip or receipt number, an ops file id); another finance or admin user approves or
rejects it (403 ``maker_checker`` for the recorder; a CHECK in the database too). Only approved payments count; an
approval that covers the total marks the invoice paid (database trigger). The decision is final.

Recording needs an ``Idempotency-Key`` (8-200 characters): the same key with the same facts returns the stored payment
(``replayed: true``), other facts are 409 ``idempotency_conflict``. A payment never exceeds what is left to pay after
the approved and the waiting payments (409 ``amount_exceeds_balance``). A card number in any text is 422.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import asyncpg

from app import audit
from app.agency_billing import invoices
from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.ops.money import idempotency_key, looks_like_card_number

COLUMNS = invoices.PAYMENT_COLUMNS
FACTS = ("amount", "method", "received_on", "reference", "document_ref", "note")
LIST_SQL = """
SELECT p.id::text AS id, p.invoice_id::text AS invoice_id, p.amount, p.currency, p.method, p.received_on, p.reference,
       p.document_ref, p.note, p.status, p.recorded_by, p.recorded_at, p.approved_by, p.approved_at, p.rejected_by,
       p.rejected_at, p.reject_reason, i.number AS invoice_number, i.invoice_kind, b.agency_name
  FROM booking.agency_payment p
  JOIN booking.agency_invoice i ON i.id = p.invoice_id
  JOIN booking.b2b_partners b ON b.tenant_id = i.party_tenant_id
"""


def _out(row: Any) -> dict[str, Any]:
    return dict(row)


async def _audit(conn: asyncpg.Connection, actor: str, action: str, payment: dict[str, Any], label: str,
                 after: dict[str, Any]) -> None:  # fmt: skip
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=actor,
        action=action,
        entity_type="agency_payment",
        entity_id=payment["id"],
        after=json.loads(json.dumps(after, default=str)),
        entity_label=label[:80],
        summary=action.replace("agency_payment.", "Agency payment ").replace("_", " "),
    )


async def list_payments(conn: asyncpg.Connection, *, state: str | None = None, invoice: str | None = None,
                        limit: int = 50, offset: int = 0) -> dict[str, Any]:  # fmt: skip
    where, args = ["true"], []
    if state and state != "all":
        args.append(state)
        where.append(f"p.status = ${len(args)}")
    if invoice:
        args.append(invoice)
        where.append(f"(i.number = ${len(args)} OR i.id::text = ${len(args)})")
    cond = " AND ".join(where)
    total = await conn.fetchval(
        "SELECT count(*) FROM booking.agency_payment p JOIN booking.agency_invoice i ON i.id = p.invoice_id"  # noqa: S608
        f" WHERE {cond}",
        *args,
    )
    args += [limit, offset]
    rows = await conn.fetch(
        f"{LIST_SQL} WHERE {cond} ORDER BY p.recorded_at DESC, p.id LIMIT ${len(args) - 1} OFFSET ${len(args)}",
        *args,
    )
    return {"items": [_out(r) for r in rows], "total": total, "limit": limit, "offset": offset}


async def record(
    conn: asyncpg.Connection, ref: str, body: dict[str, Any], actor: str, key: str | None
) -> dict[str, Any]:
    key = idempotency_key(key)
    if any(looks_like_card_number(body.get(k)) for k in ("reference", "document_ref", "note")):
        raise Invalid("card_number_not_allowed", "a payment card number never goes into Booking")
    async with conn.transaction():
        inv = await invoices._row(conn, ref, lock=True)
        stored = await conn.fetchrow(
            f"SELECT {COLUMNS} FROM booking.agency_payment WHERE invoice_id = $1::uuid AND idempotency_key = $2",  # noqa: S608
            inv["id"],
            key,
        )
        if stored is not None:
            same = all(
                (str(stored[k]) if stored[k] is not None else None)
                == (str(body.get(k)) if body.get(k) is not None else None)
                for k in FACTS
            )
            if not same:
                raise Conflict("idempotency_conflict", "this Idempotency-Key was already used for another payment")
            return {**_out(stored), "replayed": True}
        if inv["status"] != "issued":
            raise Conflict("invoice_not_issued", f"the invoice is {inv['status']}: payments go on an issued invoice")
        left = Decimal(inv["total"]) - Decimal(inv["paid"]) - Decimal(inv["waiting"])
        if Decimal(body["amount"]) > left:
            raise Conflict("amount_exceeds_balance", f"at most {left} {inv['currency']} is left to record",
                           left=str(left))  # fmt: skip
        row = await conn.fetchrow(
            "INSERT INTO booking.agency_payment (invoice_id, amount, currency, method, received_on, reference,"  # noqa: S608
            " document_ref, note, recorded_by, idempotency_key)"
            f" VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10) RETURNING {COLUMNS}",
            inv["id"],
            body["amount"],
            inv["currency"],
            body["method"],
            body["received_on"],
            body.get("reference"),
            body["document_ref"].strip(),
            body.get("note"),
            actor,
            key,
        )
        out = _out(row)
        await _audit(conn, actor, "agency_payment.recorded", out, f"{inv['number']} · {inv['agency_name']}",
                     {"amount": out["amount"], "currency": out["currency"], "method": out["method"],
                      "received_on": out["received_on"]})  # fmt: skip
    return {**out, "replayed": False}


async def _decide(conn: asyncpg.Connection, payment_id: str, actor: str, *, approve: bool,
                  reason: str | None = None) -> dict[str, Any]:  # fmt: skip
    async with conn.transaction():
        row = await conn.fetchrow(
            f"SELECT {COLUMNS} FROM booking.agency_payment WHERE id = $1::uuid FOR UPDATE",  # noqa: S608
            payment_id,
        )
        if row is None:
            raise NotFound("payment_not_found", "no agency payment with this id")
        if row["status"] != "recorded":
            raise Conflict("payment_decided", f"the payment is already {row['status']}")
        if row["recorded_by"] == actor:
            raise Forbidden("maker_checker", "the person who recorded a payment cannot approve or reject it")
        inv = await invoices._row(conn, row["invoice_id"], lock=True)
        if approve:
            if inv["status"] != "issued":
                raise Conflict("invoice_not_issued", f"the invoice is {inv['status']}")
            new = await conn.fetchrow(
                "UPDATE booking.agency_payment SET status = 'approved', approved_by = $2, approved_at = now()"  # noqa: S608
                f" WHERE id = $1::uuid RETURNING {COLUMNS}",
                payment_id,
                actor,
            )
        else:
            new = await conn.fetchrow(
                "UPDATE booking.agency_payment SET status = 'rejected', rejected_by = $2, rejected_at = now(),"  # noqa: S608
                f" reject_reason = $3 WHERE id = $1::uuid RETURNING {COLUMNS}",
                payment_id,
                actor,
                (reason or "").strip(),
            )
        out = _out(new)
        status = await conn.fetchval("SELECT status FROM booking.agency_invoice WHERE id = $1::uuid", inv["id"])
        await _audit(conn, actor, "agency_payment.approved" if approve else "agency_payment.rejected", out,
                     f"{inv['number']} · {inv['agency_name']}",
                     {"amount": out["amount"], "invoice_status": status, "reason": out["reject_reason"]})  # fmt: skip
    return {**out, "invoice_status": status, "invoice_number": inv["number"]}


async def approve(conn: asyncpg.Connection, payment_id: str, actor: str) -> dict[str, Any]:
    return await _decide(conn, payment_id, actor, approve=True)


async def reject(conn: asyncpg.Connection, payment_id: str, reason: str, actor: str) -> dict[str, Any]:
    if looks_like_card_number(reason):
        raise Invalid("card_number_not_allowed", "a payment card number never goes into Booking")
    return await _decide(conn, payment_id, actor, approve=False, reason=reason)
