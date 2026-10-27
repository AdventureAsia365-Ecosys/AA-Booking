"""Manual payments of a customer invoice (bk-028; AA-682 slice S3, S252 design D6, §3.3, §5, critic B4, B6, B8 and
notes 4, 6, 12).

No payment provider: staff **record** money that arrived (``receipt``) or was paid back (``refund``) with the method,
the day, the bank / receipt reference (never a card number: 422 ``card_number_not_allowed``) and a note; then a
**second person verifies** it (``payment.verify``, admin; the recorder gets 403 ``maker_checker``, and the database
refuses it too) once the evidence file (bank slip, receipt; ``POST /ops-files`` kind ``payment_evidence`` with the
payment as entity) is attached, unless the method is cash (422 ``evidence_required``). Only verified, non-voided
payments count: the database settles the invoice (``sent`` → ``partially_paid`` → ``paid``) and the case follows
(:func:`app.ops.track.advance_case`). **Void** (a reason, final) takes a payment out: a verified one needs
``payment.verify`` as well; the invoice and the case step back. A refund is a payment of kind ``refund`` with the same
steps; it never takes the net paid below 0 (409 ``refund_exceeds_paid``), and neither does voiding a verified receipt
(409 ``void_would_overdraw``: void the refund first). The payments of a cancelled invoice are final (409
``invoice_closed``).

Every record takes an ``Idempotency-Key`` (unique per invoice) and the hash of the body: a replay returns the stored
payment (200), another body under the same key is 409 ``idempotency_conflict``; the same method + reference already
recorded is 409 ``duplicate_reference`` unless ``allow_duplicate_reference`` (design §5). Lock order: the case, the
invoice, the payment (critic B4). Audit rows never carry the reference or the note (``"(set)"``).

Permissions: read ``billing.read``; record, edit, void ``billing.write`` (admin, sales); verify ``payment.verify``
(admin). No job and no agent session ever writes a payment (design §9, the AA-682 comment).
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import asyncpg

from app import audit, paging
from app.auth.staff import Staff
from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.notifications import service as notifications
from app.ops import files as ops_files
from app.ops import invoices, money, track

log = logging.getLogger(__name__)

KINDS = ("receipt", "refund")
METHODS = ("bank_transfer", "card_offline", "cash", "other")
STATES = ("recorded", "verified", "voided")
MAX_PAGE = 100
STATE_SQL = (
    "CASE WHEN p.voided_at IS NOT NULL THEN 'voided' WHEN p.verified_at IS NOT NULL THEN 'verified' ELSE 'recorded' END"
)
PAYMENT_COLUMNS = f"""
    p.id::text AS id, p.ref, p.kind, p.amount::text AS amount, p.currency, p.method, p.received_on, p.reference,
    p.note, p.received_amount::text AS received_amount, p.received_currency,
    p.evidence_file_id::text AS evidence_file_id, {STATE_SQL} AS state, p.recorded_by, rec.name AS recorded_by_name,
    p.recorded_at, p.verified_by, ver.name AS verified_by_name, p.verified_at, p.voided_by, vo.name AS voided_by_name,
    p.voided_at, p.void_reason, p.created_at, p.updated_at, i.id::text AS invoice_id, i.invoice_number,
    i.status AS invoice_status, tc.id::text AS trip_case_id, tc.code AS trip_case_code
"""
PAYMENT_FROM = """
FROM booking.payments p
JOIN booking.invoices i ON i.id = p.invoice_id
JOIN booking.trip_cases tc ON tc.id = i.trip_case_id
LEFT JOIN booking.customers cu ON cu.id = i.customer_id
LEFT JOIN booking.staff_users rec ON rec.cis_user_id = p.recorded_by
LEFT JOIN booking.staff_users ver ON ver.cis_user_id = p.verified_by
LEFT JOIN booking.staff_users vo ON vo.cis_user_id = p.voided_by
"""


def _uuid(value: Any) -> str | None:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        return None


def payment_out(row: Any, staff: Staff, files: list[dict] | None = None) -> dict[str, Any]:
    out = dict(row)
    for who in ("recorded_by", "verified_by", "voided_by"):
        name = out.pop(f"{who}_name")
        out[who] = {"id": out[who], "name": name} if out[who] else None
    out["invoice"] = {
        "id": out.pop("invoice_id"),
        "number": out.pop("invoice_number"),
        "status": out.pop("invoice_status"),
    }
    out["trip_case"] = {"id": out.pop("trip_case_id"), "code": out.pop("trip_case_code")}
    out["files"] = files or []
    state = out["state"]
    recorder = out["recorded_by"]["id"] if out["recorded_by"] else None
    live_invoice = out["invoice"]["status"] in invoices.MONEY_STATUSES
    out["actions"] = {
        "edit": state == "recorded" and staff.can("billing.write"),
        "attach": state == "recorded" and staff.can("billing.write"),
        "verify": state == "recorded"
        and live_invoice
        and staff.can("payment.verify")
        and staff.cis_user_id != recorder,
        "void": live_invoice
        and (
            (state == "recorded" and staff.can("billing.write"))
            or (state == "verified" and staff.can("billing.write") and staff.can("payment.verify"))
        ),
    }
    out["needs_evidence"] = out["method"] != "cash" and not out["evidence_file_id"]
    return out


async def _get(conn: asyncpg.Connection, ref: str, *, lock: bool = False) -> Any:
    ident = _uuid(ref)
    row = await conn.fetchrow(
        f"SELECT {PAYMENT_COLUMNS} {PAYMENT_FROM} WHERE "  # noqa: S608 — constants only
        + ("p.id = $1::uuid" if ident else "p.ref = $1")
        + (" FOR UPDATE OF p" if lock else ""),
        ident or (ref or "").strip().upper(),
    )
    if row is None:
        raise NotFound("payment_not_found", f"payment {ref} not found")
    return row


async def _one(conn: asyncpg.Connection, staff: Staff, payment_id: str) -> dict[str, Any]:
    row = await _get(conn, payment_id)
    attached = await ops_files.files_of(conn, "payment", [row["id"]])
    return invoices.jsonable(payment_out(row, staff, attached.get(row["id"])))


async def for_invoice(conn: asyncpg.Connection, staff: Staff, invoice_id: str) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        f"SELECT {PAYMENT_COLUMNS} {PAYMENT_FROM} WHERE p.invoice_id = $1::uuid"  # noqa: S608 — constants only
        " ORDER BY p.recorded_at, p.id",
        invoice_id,
    )
    attached = await ops_files.files_of(conn, "payment", [r["id"] for r in rows])
    return [payment_out(r, staff, attached.get(r["id"])) for r in rows]


async def get_payment(conn: asyncpg.Connection, staff: Staff, ref: str) -> dict[str, Any]:
    row = await _get(conn, ref)
    out = await _one(conn, staff, row["id"])
    balance = await conn.fetchrow(
        "SELECT i.currency, i.total_amount::text AS total_amount, b.net_paid::text AS net_paid,"
        " b.balance::text AS balance FROM booking.invoices i JOIN booking.v_invoice_balance b ON b.invoice_id = i.id"
        " WHERE i.id = $1::uuid",
        row["invoice_id"],
    )
    out["invoice"].update(dict(balance))
    return out


async def list_payments(
    conn: asyncpg.Connection,
    staff: Staff,
    *,
    state: str | None = None,
    kind: str | None = None,
    method: str | None = None,
    invoice: str | None = None,
    case: str | None = None,
    q: str | None = None,
    start: date | None = None,
    end: date | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Payments across invoices, newest first; ``state`` recorded (waiting for verification) / verified / voided."""
    if state not in (None, *STATES) or kind not in (None, *KINDS) or method not in (None, *METHODS):
        raise Invalid("invalid_filter", "unknown state, kind or method")
    if not 1 <= limit <= MAX_PAGE or offset < 0:
        raise Invalid("invalid_page", f"limit is 1..{MAX_PAGE} and offset >= 0")
    args: list[Any] = []
    where = ["i.deleted_at IS NULL"]

    def add(sql: str, value: Any) -> None:
        args.append(value)
        where.append(sql.format(p=f"${len(args)}"))

    if state:
        add(f"{STATE_SQL} = {{p}}", state)
    if kind:
        add("p.kind = {p}", kind)
    if method:
        add("p.method = {p}", method)
    if invoice:
        ident = _uuid(invoice)
        add("i.id = {p}::uuid" if ident else "i.invoice_number = {p}", ident or invoice.strip().upper())
    if case:
        ident = _uuid(case)
        add("tc.id = {p}::uuid" if ident else "tc.code = {p}", ident or case.strip().upper())
    if q:
        add(
            "(p.ref ILIKE {p} OR i.invoice_number ILIKE {p} OR tc.code ILIKE {p} OR p.reference ILIKE {p})",
            paging.contains(q),
        )
    if start:
        add("p.received_on >= {p}", start)
    if end:
        add("p.received_on <= {p}", end)
    clause = " AND ".join(where)
    total = await conn.fetchval(f"SELECT count(*) {PAYMENT_FROM} WHERE {clause}", *args)  # noqa: S608
    args.extend([limit, offset])
    rows = await conn.fetch(
        f"SELECT {PAYMENT_COLUMNS} {PAYMENT_FROM} WHERE {clause}"  # noqa: S608 — fixed fragments, bound values
        f" ORDER BY p.recorded_at DESC, p.id DESC LIMIT ${len(args) - 1} OFFSET ${len(args)}",
        *args,
    )
    return invoices.jsonable(
        {"items": [payment_out(r, staff) for r in rows], "total": int(total or 0), "limit": limit, "offset": offset}
    )


# ── Writes ───────────────────────────────────────────────────────────────────


def _no_card(field: str, text: str | None) -> None:
    if money.looks_like_card_number(text):
        raise Invalid(
            "card_number_not_allowed",
            f"{field} looks like a payment card number: enter the bank or receipt reference, never a card number",
        )


def _text(value: Any, limit: int) -> str | None:
    return (" ".join(str(value).split())[:limit] or None) if value is not None else None


async def _lock(conn: asyncpg.Connection, ref: str) -> tuple[Any, Any, Any]:
    """Lock order: case, invoice, payment. Returns (case, invoice, payment row)."""
    first = await _get(conn, ref)
    case, inv = await invoices.lock_invoice(conn, first["invoice_id"])
    row = await _get(conn, first["id"], lock=True)
    return case, inv, row


async def _audit(
    conn: asyncpg.Connection,
    staff: Staff,
    action: str,
    row: Any,
    summary: str,
    *,
    before: Any = None,
    after: Any = None,
) -> None:
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action=action,
        entity_type="payment",
        entity_id=row["id"],
        before=invoices.jsonable(before),
        after=invoices.jsonable(after),
        entity_label=f"{row['trip_case_code']} {row['invoice_number']} {row['ref']}",
        summary=summary[:500],
    )


def _body_for_hash(body: dict[str, Any]) -> dict[str, Any]:
    keys = ("kind", "amount", "method", "received_on", "reference", "note", "received_amount", "received_currency")
    return {k: body.get(k) for k in keys}


async def record(
    conn: asyncpg.Connection, staff: Staff, number: str, body: dict[str, Any], key: str | None
) -> tuple[dict[str, Any], bool]:
    """Record a receipt or a refund against a sent invoice. Returns (payment, replayed)."""
    idem = money.idempotency_key(key)
    sha = money.request_hash(_body_for_hash(body))
    case, inv = await invoices.lock_invoice(conn, number)
    stored = await conn.fetchrow(
        "SELECT id::text AS id, request_sha256 FROM booking.payments WHERE invoice_id = $1::uuid"
        " AND idempotency_key = $2",
        inv["id"],
        idem,
    )
    if stored is not None:
        if stored["request_sha256"] != sha:
            raise Conflict("idempotency_conflict", "this Idempotency-Key was already used for another request")
        return await _one(conn, staff, stored["id"]), True
    if inv["status"] not in invoices.MONEY_STATUSES:
        raise Conflict(
            "invoice_not_payable", f"{inv['invoice_number']} is {inv['status']}: payments go on a sent invoice"
        )
    kind, method = body.get("kind"), body.get("method")
    if kind not in KINDS or method not in METHODS:
        raise Invalid("invalid_payment", "unknown kind or method")
    currency = inv["currency"].strip()
    amount = money.parse_amount(body.get("amount"), currency)
    if amount <= 0:
        raise Invalid("invalid_amount", "the amount must be more than 0")
    received_on = body.get("received_on")
    today = await invoices.ops_today(conn)
    if not isinstance(received_on, date) or received_on > today + timedelta(days=1):
        raise Invalid("invalid_received_on", "the day the money moved is required and cannot be in the future")
    reference = _text(body.get("reference"), 100)
    note = (str(body.get("note")).strip()[:2000] or None) if body.get("note") is not None else None
    _no_card("the reference", reference)
    _no_card("the note", note)
    received_amount, received_currency = body.get("received_amount"), body.get("received_currency")
    if (received_amount is None) != (received_currency is None):
        raise Invalid("received_pair", "the amount received and its currency go together")
    if received_amount is not None:
        received_currency = str(received_currency).strip().upper()
        received_amount = money.parse_amount(received_amount, received_currency, field="received_amount")
        if received_amount <= 0:
            raise Invalid("invalid_amount", "the amount received must be more than 0")
    net = Decimal(await conn.fetchval("SELECT booking.invoice_net_paid($1::uuid)", inv["id"]))
    if kind == "refund" and amount > net:
        raise Conflict(
            "refund_exceeds_paid", f"a refund of {amount} {currency} is more than the {net} {currency} verified as paid"
        )
    if reference and not body.get("allow_duplicate_reference"):
        dup = await conn.fetchval(
            "SELECT ref FROM booking.payments WHERE method = $1 AND lower(reference) = lower($2) AND voided_at IS NULL"
            " LIMIT 1",
            method,
            reference,
        )
        if dup:
            raise Conflict(
                "duplicate_reference",
                f"{dup} already has this {method.replace('_', ' ')} reference; record it anyway only if it is"
                " another payment",
                payment=dup,
            )
    try:
        async with conn.transaction():
            payment_id = await conn.fetchval(
                "INSERT INTO booking.payments (invoice_id, kind, amount, currency, method, received_on, reference,"
                " note, received_amount, received_currency, idempotency_key, request_sha256, recorded_by, updated_by)"
                " VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $13) RETURNING id::text",
                inv["id"],
                kind,
                amount,
                currency,
                method,
                received_on,
                reference,
                note,
                received_amount,
                received_currency,
                idem,
                sha,
                staff.cis_user_id,
            )
    except asyncpg.CheckViolationError as exc:
        raise Conflict("invalid_payment", str(exc).split("\n")[0]) from exc
    row = await _get(conn, payment_id)
    await _audit(
        conn,
        staff,
        "payment.recorded",
        row,
        f"Recorded {row['ref']}: {kind} {amount} {currency} by {method.replace('_', ' ')}",
        after={
            "kind": kind,
            "amount": amount,
            "currency": currency,
            "method": method,
            "received_on": received_on,
            "reference": "(set)" if reference else None,
            "note": "(set)" if note else None,
        },
    )
    await notifications.notify_staff(
        conn,
        type="payment.recorded",
        title=f"{case['code']}: verify {row['ref']} ({kind} {amount} {currency})",
        message=f"Recorded on {inv['invoice_number']} by {staff.name or staff.cis_user_id}. Another admin verifies it"
        + (" (cash: no evidence needed)." if method == "cash" else " once the evidence file is attached."),
        entity_type="payment",
        entity_id=payment_id,
        data={"ref": row["ref"], "invoice_number": inv["invoice_number"], "case_code": case["code"]},
    )
    return await _one(conn, staff, payment_id), False


async def update(conn: asyncpg.Connection, staff: Staff, ref: str, changes: dict[str, Any]) -> dict[str, Any]:
    """The reference and the note of a payment not yet verified."""
    _case, _inv, row = await _lock(conn, ref)
    if row["state"] != "recorded":
        raise Conflict("payment_locked", f"{row['ref']} is {row['state']}: it no longer changes")
    new: dict[str, Any] = {}
    if "reference" in changes:
        new["reference"] = _text(changes.get("reference"), 100)
        _no_card("the reference", new["reference"])
    if "note" in changes:
        new["note"] = (str(changes.get("note") or "").strip()[:2000]) or None
        _no_card("the note", new["note"])
    changed = sorted(k for k, v in new.items() if v != row[k])
    if changed:
        sets = ", ".join(f"{k} = ${i + 3}" for i, k in enumerate(changed))
        await conn.execute(
            f"UPDATE booking.payments SET {sets}, updated_by = $2 WHERE id = $1::uuid",  # noqa: S608 — fixed names
            row["id"],
            staff.cis_user_id,
            *[new[k] for k in changed],
        )
        await _audit(
            conn,
            staff,
            "payment.updated",
            row,
            f"Edited {row['ref']}: {', '.join(changed)}",
            after=dict.fromkeys(changed, "(set)"),
        )
    return {**await _one(conn, staff, row["id"]), "changed": changed}


async def verify(conn: asyncpg.Connection, staff: Staff, ref: str) -> dict[str, Any]:
    """Maker-checker: another admin verifies with the evidence (unless cash); the invoice settles, the case follows."""
    case, inv, row = await _lock(conn, ref)
    if row["state"] == "verified":
        return await _one(conn, staff, row["id"])  # the same answer again
    if row["state"] == "voided":
        raise Conflict("payment_voided", f"{row['ref']} is voided")
    if row["recorded_by"] == staff.cis_user_id:
        raise Forbidden("maker_checker", f"you recorded {row['ref']}: another admin verifies it")
    if inv["status"] not in invoices.MONEY_STATUSES:
        raise Conflict("invoice_not_payable", f"{inv['invoice_number']} is {inv['status']}")
    if row["method"] != "cash" and not row["evidence_file_id"]:
        raise Invalid("evidence_required", "attach the bank slip or receipt before verifying (cash needs none)")
    if row["kind"] == "refund":
        net = Decimal(await conn.fetchval("SELECT booking.invoice_net_paid($1::uuid)", inv["id"]))
        if Decimal(row["amount"]) > net:
            raise Conflict("refund_exceeds_paid", f"the refund is more than the {net} verified as paid")
    before = await conn.fetchval("SELECT status FROM booking.invoices WHERE id = $1::uuid", inv["id"])
    try:
        await conn.execute(
            "UPDATE booking.payments SET verified_by = $2, verified_at = now(), updated_by = $2 WHERE id = $1::uuid",
            row["id"],
            staff.cis_user_id,
        )
    except asyncpg.CheckViolationError as exc:
        raise Conflict("payment_not_verifiable", str(exc).split("\n")[0]) from exc
    bal = await conn.fetchrow(
        "SELECT i.status, b.balance FROM booking.invoices i JOIN booking.v_invoice_balance b ON b.invoice_id = i.id"
        " WHERE i.id = $1::uuid",
        inv["id"],
    )
    await _audit(
        conn,
        staff,
        "payment.verified",
        row,
        f"Verified {row['ref']} ({row['kind']} {row['amount']} {row['currency']}); invoice {before} -> {bal['status']}",
        before={"state": "recorded", "invoice_status": before},
        after={"state": "verified", "invoice_status": bal["status"]},
    )
    if bal["balance"] is not None and bal["balance"] < 0:
        await notifications.notify_staff(
            conn,
            type="invoice.overpaid",
            title=f"{case['code']}: {inv['invoice_number']} is overpaid by {-bal['balance']} {row['currency']}",
            message="Refund the difference (record and verify a refund) or amend the invoice.",
            entity_type="invoice",
            entity_id=inv["id"],
            data={"invoice_number": inv["invoice_number"], "case_code": case["code"]},
        )
    await _after_money_change(conn, case, inv, f"payment {row['ref']} verified", bal["status"])
    return await _one(conn, staff, row["id"])


async def void(conn: asyncpg.Connection, staff: Staff, ref: str, reason: str) -> dict[str, Any]:
    """Take a payment out (final, with a reason); a verified one needs ``payment.verify`` too."""
    reason = (reason or "").strip()
    if not reason:
        raise Invalid("reason_required", "say why the payment is voided")
    case, inv, row = await _lock(conn, ref)
    if row["state"] == "voided":
        return await _one(conn, staff, row["id"])
    if row["state"] == "verified" and not staff.can("payment.verify"):
        raise Forbidden("forbidden", "voiding a verified payment needs payment.verify (an admin)")
    if inv["status"] not in invoices.MONEY_STATUSES:
        # A cancelled invoice holds net verified paid 0; voiding one of its payments would break that.
        raise Conflict("invoice_closed", f"{inv['invoice_number']} is {inv['status']}: its payments are final")
    if row["state"] == "verified" and row["kind"] == "receipt":
        net = Decimal(await conn.fetchval("SELECT booking.invoice_net_paid($1::uuid)", inv["id"]))
        if Decimal(row["amount"]) > net:
            raise Conflict(
                "void_would_overdraw",
                f"voiding {row['ref']} takes the net paid ({net}) below 0: void the verified refund first",
            )
    before = await conn.fetchval("SELECT status FROM booking.invoices WHERE id = $1::uuid", inv["id"])
    try:
        await conn.execute(
            "UPDATE booking.payments SET voided_at = now(), voided_by = $2, void_reason = $3, updated_by = $2"
            " WHERE id = $1::uuid",
            row["id"],
            staff.cis_user_id,
            reason[:500],
        )
    except asyncpg.CheckViolationError as exc:
        raise Conflict("payment_not_voidable", str(exc).split("\n")[0]) from exc
    after = await conn.fetchval("SELECT status FROM booking.invoices WHERE id = $1::uuid", inv["id"])
    await _audit(
        conn,
        staff,
        "payment.voided",
        row,
        f"Voided {row['ref']}: {reason[:200]}; invoice {before} -> {after}",
        before={"state": row["state"], "invoice_status": before},
        after={"state": "voided", "reason": reason[:500], "invoice_status": after},
    )
    await _after_money_change(conn, case, inv, f"payment {row['ref']} voided", after)
    return await _one(conn, staff, row["id"])


async def _after_money_change(conn: asyncpg.Connection, case: Any, inv: Any, cause: str, invoice_status: str) -> None:
    """The case follows its invoice; a trip already travelling never steps back but staff hear of a reopened balance
    (critic note 12)."""
    status = await conn.fetchval("SELECT status FROM booking.trip_cases WHERE id = $1::uuid", case["id"])
    if status in invoices.TRAVEL_STATUSES and invoice_status != "paid":
        await notifications.notify_staff(
            conn,
            type="invoice.balance_reopened",
            title=f"{case['code']} is {status.replace('_', ' ')} but {inv['invoice_number']} is no longer paid",
            message="The trip keeps its status; collect the balance or refund and amend the invoice.",
            entity_type="invoice",
            entity_id=inv["id"],
            data={"invoice_number": inv["invoice_number"], "case_code": case["code"]},
        )
        return
    if status != "cancelled":
        await track.advance_case(conn, case, cause=cause)
