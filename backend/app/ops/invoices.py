"""Customer invoices of a sold B2C trip case and their payment schedule (bk-028; AA-682 slice S3, S252 design §3.3,
§4, §5, critic B5, B6, B9 and notes 3, 5, 12, 18).

An **invoice** (``INV-000123``) is a proforma payment request (design D7: tax-inclusive prices, no tax line; the legal
VAT invoice stays outside Booking) for the case's customer, in the currency of the approved version. One live invoice
per case. Lines: the approved version's total (locked) + adjustments with a reason (design D8 / Q24). Schedule: by
default ``deposit_pct`` (30 %) due ``deposit_due_days`` after the issue day and the rest
``balance_due_days_before_start`` before the trip, or one payment when the trip is too close (§11 Q4; policy data in
``booking.settings``).

Life: ``draft`` (edit, soft-delete) → ``sent`` (bill-to snapshot, a customer notification row, the case moves to
``invoice_sent``) → ``partially_paid`` ⇄ ``paid`` (derived from verified payments by the database, ``app.ops.payments``)
→ ``cancelled`` (only with net verified paid 0 and no payment waiting; the case steps back). **Amendment** (critic B5):
a sent invoice takes appended adjustment lines and a new schedule for its uncovered part; rows the verified payments
already cover stay; the customer feed says "updated". **Reminders**: ``POST …/remind`` writes one customer notification
row a day (no e-mail, design D15); the daily ``bk_ops_reminders`` job writes the due-soon / overdue ones.

Creating takes an ``Idempotency-Key`` (unique per case) and the hash of the request body: a replay returns the stored
invoice, another body under the same key is 409 ``idempotency_conflict`` (critic B6). Every write locks the case, then
the invoice, writes one audit row and runs :func:`app.ops.track.advance_case`.

Permissions: ``billing.read`` / ``billing.write`` (admin, sales; not in preview sessions, critic note 8).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import asyncpg

from app import audit, paging
from app.auth.staff import Staff
from app.errors import Conflict, Invalid, NotFound
from app.notifications import service as notifications
from app.ops import lifecycle, money, track

log = logging.getLogger(__name__)

STATUSES = ("draft", "sent", "partially_paid", "paid", "cancelled")
MONEY_STATUSES = ("sent", "partially_paid", "paid")
# A case takes a new invoice once sold and before any money status (an invoice after a cancelled one too).
INVOICEABLE_CASE_STATUSES = frozenset({"confirmed", "supplier_confirming"})
TRAVEL_STATUSES = frozenset({"pre_departure", "on_trip", "completed"})
MAX_ADJUSTMENTS = 20
MAX_PAGE = 100
VERSION_LINE = 1

INVOICE_COLUMNS = """
    i.id::text AS id, i.invoice_number, i.status, i.document_type, i.bill_to_kind, i.currency,
    i.total_amount::text AS total_amount, i.trip_case_id::text AS trip_case_id, tc.code AS trip_case_code,
    tc.status AS trip_case_status, tc.channel AS trip_case_channel, tc.start_date AS trip_start_date,
    i.customer_id::text AS customer_id, cu.code AS customer_code, coalesce(cu.name, cu.email) AS customer_name,
    i.bill_to_name, i.bill_to_email, i.bill_to_address, i.notes, i.terms, i.issued_at, i.sent_by,
    sent.name AS sent_by_name, i.cancelled_at, i.cancelled_by, i.cancel_reason, i.deleted_at, i.created_by,
    i.created_at, i.updated_at, i.version_id::text AS version_id, v.version_number, v.title AS version_title,
    v.fx_rate_locked::text AS fx_rate_locked, b.verified_received::text AS verified_received,
    b.verified_refunded::text AS verified_refunded, b.net_paid::text AS net_paid,
    b.unverified_amount::text AS unverified_amount, b.unverified_count, b.balance::text AS balance, b.next_due_date,
    b.overdue_amount::text AS overdue_amount, b.oldest_overdue_date
"""
INVOICE_FROM = """
FROM booking.invoices i
JOIN booking.trip_cases tc ON tc.id = i.trip_case_id
JOIN booking.trip_case_versions v ON v.id = i.version_id
JOIN booking.v_invoice_balance b ON b.invoice_id = i.id
LEFT JOIN booking.customers cu ON cu.id = i.customer_id
LEFT JOIN booking.staff_users sent ON sent.cis_user_id = i.sent_by
"""
ITEMS_SQL = """
SELECT id::text AS id, position, kind, description, quantity::text AS quantity, unit_price::text AS unit_price,
       amount::text AS amount, reason, created_by, created_at
  FROM booking.invoice_items WHERE invoice_id = $1::uuid ORDER BY position
"""
SCHEDULE_SQL = """
SELECT s.schedule_id::text AS id, s.seq, s.kind, s.amount::text AS amount, s.due_date,
       s.covered_amount::text AS covered_amount, s.remaining_amount::text AS remaining_amount, s.state, s.days_overdue
  FROM booking.v_payment_schedule_status s WHERE s.invoice_id = $1::uuid ORDER BY s.due_date, s.seq
"""
CASE_SQL = """
SELECT tc.id::text AS id, tc.code, tc.status, tc.channel, tc.start_date, tc.customer_id::text AS customer_id
  FROM booking.trip_cases tc WHERE {where} AND tc.deleted_at IS NULL
"""
APPROVED_SQL = """
SELECT v.id::text AS id, v.version_number, v.title, v.total_price, v.currency, v.fx_rate_locked::text AS fx_rate_locked
  FROM booking.trip_case_versions v
 WHERE v.trip_case_id = $1::uuid AND v.is_approved AND v.deleted_at IS NULL
"""
LIVE_SQL = """
SELECT id::text AS id, invoice_number, status FROM booking.invoices
 WHERE trip_case_id = $1::uuid AND status <> 'cancelled' AND deleted_at IS NULL
"""


# ── Helpers ──────────────────────────────────────────────────────────────────


def _uuid(value: Any) -> str | None:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        return None


def _case_filter(ref: str) -> tuple[str, str]:
    ident = _uuid(ref)
    if ident:
        return "tc.id = $1::uuid", ident
    return "tc.code = $1", ref.strip().upper()


def jsonable(value: Any) -> Any:
    """Decimals as strings (money never travels as a float), recursively."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [jsonable(v) for v in value]
    return value


async def lock_case(conn: asyncpg.Connection, ref: str) -> Any:
    where, arg = _case_filter(ref)
    row = await conn.fetchrow(CASE_SQL.format(where=where) + " FOR UPDATE", arg)
    if row is None:
        raise NotFound("trip_case_not_found", f"trip case {ref} not found")
    return row


async def _read_case(conn: asyncpg.Connection, ref: str) -> Any:
    where, arg = _case_filter(ref)
    row = await conn.fetchrow(CASE_SQL.format(where=where), arg)
    if row is None:
        raise NotFound("trip_case_not_found", f"trip case {ref} not found")
    return row


def _number(number: str) -> str:
    return (number or "").strip().upper()


async def find_invoice(conn: asyncpg.Connection, number: str) -> Any:
    """``id`` / ``trip_case_id`` / ``status`` of an invoice by number or UUID (404 when missing or deleted)."""
    ident = _uuid(number)
    where = "id = $1::uuid" if ident else "invoice_number = $1"
    row = await conn.fetchrow(
        "SELECT id::text AS id, invoice_number, trip_case_id::text AS trip_case_id, status, deleted_at"  # noqa: S608
        f" FROM booking.invoices WHERE {where}",
        ident or _number(number),
    )
    if row is None or row["deleted_at"] is not None:
        raise NotFound("invoice_not_found", f"invoice {number} not found")
    return row


async def lock_invoice(conn: asyncpg.Connection, number: str) -> tuple[Any, Any]:
    """Lock order: the trip case, then the invoice (critic B4). Returns (case, invoice row)."""
    first = await find_invoice(conn, number)
    case = await lock_case(conn, first["trip_case_id"])
    row = await conn.fetchrow(
        "SELECT id::text AS id, invoice_number, trip_case_id::text AS trip_case_id, status, currency, total_amount,"
        " version_id::text AS version_id, customer_id::text AS customer_id, deleted_at"
        " FROM booking.invoices WHERE id = $1::uuid FOR UPDATE",
        first["id"],
    )
    if row is None or row["deleted_at"] is not None:
        raise NotFound("invoice_not_found", f"invoice {number} not found")
    return case, row


async def _int_setting(conn: asyncpg.Connection, key: str, default: int, low: int, high: int) -> int:
    raw = await conn.fetchval("SELECT value FROM booking.settings WHERE key = $1", key)
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        value = None
    if isinstance(value, int) and not isinstance(value, bool) and low <= value <= high:
        return value
    return default


async def schedule_policy(conn: asyncpg.Connection) -> money.SchedulePolicy:
    pct = await _int_setting(conn, "deposit_pct", 30, 0, 100)
    return money.SchedulePolicy(
        deposit_pct=Decimal(pct),
        deposit_due_days=await _int_setting(conn, "deposit_due_days", 3, 0, 90),
        balance_due_days_before_start=await _int_setting(conn, "balance_due_days_before_start", 30, 0, 365),
    )


async def _setting_json(conn: asyncpg.Connection, key: str) -> Any:
    raw = await conn.fetchval("SELECT value FROM booking.settings WHERE key = $1", key)
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return None


async def ops_today(conn: asyncpg.Connection) -> date:
    return await lifecycle.ops_today(conn, await lifecycle.ops_timezone(conn))


async def _audit(
    conn: asyncpg.Connection,
    staff: Staff,
    action: str,
    invoice: Any,
    case_code: str,
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
        entity_type="invoice",
        entity_id=invoice["id"],
        before=jsonable(before),
        after=jsonable(after),
        entity_label=f"{case_code} {invoice['invoice_number']}",
        summary=summary[:500],
    )


def _person(ref: str | None, name: str | None) -> dict[str, Any] | None:
    return {"id": ref, "name": name} if ref else None


def summary_out(row: Any) -> dict[str, Any]:
    out = dict(row)
    out["trip_case"] = {
        "id": out.pop("trip_case_id"),
        "code": out.pop("trip_case_code"),
        "status": out.pop("trip_case_status"),
        "channel": out.pop("trip_case_channel"),
        "start_date": out.pop("trip_start_date"),
    }
    out["customer"] = (
        {"id": out["customer_id"], "code": out.pop("customer_code"), "name": out.pop("customer_name")}
        if out["customer_id"]
        else None
    )
    out.pop("customer_code", None)
    out.pop("customer_name", None)
    out["sent_by"] = _person(out["sent_by"], out.pop("sent_by_name"))
    out["version"] = {
        "id": out.pop("version_id"),
        "number": out.pop("version_number"),
        "title": out.pop("version_title"),
        "fx_rate_locked": out.pop("fx_rate_locked"),
    }
    out["overdue"] = out["overdue_amount"] not in (None, "0.00")
    return out


def actions(row: dict[str, Any], staff: Staff) -> dict[str, bool]:
    write = staff.can("billing.write")
    status = row["status"]
    owed = row.get("balance") not in (None, "0.00") and not str(row.get("balance") or "0").startswith("-")
    return {
        "edit": write and status == "draft",
        "send": write and status == "draft",
        "delete": write and status == "draft",
        "amend": write and status in MONEY_STATUSES,
        # Only a sent invoice cancels: partially_paid always has net verified paid > 0 (refund it back to sent first).
        "cancel": write and status == "sent",
        "remind": write and status in ("sent", "partially_paid") and owed,
        "record_payment": write and status in MONEY_STATUSES,
    }


async def detail(conn: asyncpg.Connection, staff: Staff, invoice_id: str) -> dict[str, Any]:
    from app.ops import payments  # noqa: PLC0415 — payments imports this module

    row = await conn.fetchrow(
        f"SELECT {INVOICE_COLUMNS} {INVOICE_FROM} WHERE i.id = $1::uuid",  # noqa: S608 — constants only
        invoice_id,
    )
    if row is None:
        raise NotFound("invoice_not_found", f"invoice {invoice_id} not found")
    out = summary_out(row)
    out["items"] = [dict(r) for r in await conn.fetch(ITEMS_SQL, invoice_id)]
    out["schedule"] = [dict(r) for r in await conn.fetch(SCHEDULE_SQL, invoice_id)]
    out["payments"] = await payments.for_invoice(conn, staff, invoice_id)
    out["issuer"] = await _setting_json(conn, "invoice_issuer")
    out["payment_instructions"] = await _setting_json(conn, "payment_instructions")
    out["actions"] = actions(out, staff)
    return jsonable(out)


# ── Reads ────────────────────────────────────────────────────────────────────


async def get_invoice(conn: asyncpg.Connection, staff: Staff, number: str) -> dict[str, Any]:
    row = await find_invoice(conn, number)
    return await detail(conn, staff, row["id"])


async def list_invoices(
    conn: asyncpg.Connection,
    staff: Staff,
    *,
    status: str | None = None,
    overdue: bool | None = None,
    case: str | None = None,
    q: str | None = None,
    start: date | None = None,
    end: date | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Invoices (not deleted), newest first; ``status`` one value, ``open`` (sent + partially paid) or ``all``."""
    if status not in (None, "all", "open", *STATUSES):
        raise Invalid("unknown_status", f"status must be one of {', '.join((*STATUSES, 'open', 'all'))}")
    if not 1 <= limit <= MAX_PAGE or offset < 0:
        raise Invalid("invalid_page", f"limit is 1..{MAX_PAGE} and offset >= 0")
    args: list[Any] = []
    where = ["i.deleted_at IS NULL"]

    def add(sql: str, value: Any) -> None:
        args.append(value)
        where.append(sql.format(p=f"${len(args)}"))

    if status == "open":
        where.append("i.status IN ('sent', 'partially_paid')")
    elif status and status != "all":
        add("i.status = {p}", status)
    if overdue is True:
        where.append("b.overdue_amount > 0")
    elif overdue is False:
        where.append("b.overdue_amount = 0")
    if case:
        ident = _uuid(case)
        add("tc.id = {p}::uuid" if ident else "tc.code = {p}", ident or case.strip().upper())
    if q:
        add(
            "(i.invoice_number ILIKE {p} OR tc.code ILIKE {p} OR i.bill_to_name ILIKE {p} OR cu.name ILIKE {p})",
            paging.contains(q),
        )
    if start:
        add("coalesce(i.issued_at, i.created_at)::date >= {p}", start)
    if end:
        add("coalesce(i.issued_at, i.created_at)::date <= {p}", end)
    clause = " AND ".join(where)
    total = await conn.fetchval(f"SELECT count(*) {INVOICE_FROM} WHERE {clause}", *args)  # noqa: S608
    args.extend([limit, offset])
    rows = await conn.fetch(
        f"SELECT {INVOICE_COLUMNS} {INVOICE_FROM} WHERE {clause}"  # noqa: S608 — fixed fragments, bound values
        f" ORDER BY coalesce(i.issued_at, i.created_at) DESC, i.id DESC LIMIT ${len(args) - 1} OFFSET ${len(args)}",
        *args,
    )
    return jsonable(
        {"items": [summary_out(r) for r in rows], "total": int(total or 0), "limit": limit, "offset": offset}
    )


async def for_case(conn: asyncpg.Connection, staff: Staff, ref: str) -> dict[str, Any]:
    """The case Billing tab: whether an invoice can be made (and why not), the approved version, the default
    schedule, the live invoice (detail) and the cancelled ones."""
    case = await _read_case(conn, ref)
    version = await conn.fetchrow(APPROVED_SQL, case["id"])
    live = await conn.fetchrow(LIVE_SQL, case["id"])
    reason = None
    if case["channel"] == "b2b":
        reason = "b2b_no_invoice"
    elif live is not None:
        reason = "invoice_exists"
    elif case["status"] not in INVOICEABLE_CASE_STATUSES:
        reason = "case_not_invoiceable"
    elif version is None:
        reason = "no_approved_version"
    elif version["total_price"] is None or version["currency"] is None:
        reason = "version_not_priced"
    defaults = None
    if version is not None and version["total_price"] is not None and version["currency"]:
        total = Decimal(version["total_price"])
        defaults = {
            "currency": version["currency"],
            "version_total": total,
            "schedule": money.default_schedule(
                total,
                version["currency"],
                issue_day=await ops_today(conn),
                start=case["start_date"],
                policy=await schedule_policy(conn),
            ),
        }
    history = await conn.fetch(
        f"SELECT {INVOICE_COLUMNS} {INVOICE_FROM}"  # noqa: S608 — constants only
        " WHERE i.trip_case_id = $1::uuid AND i.status = 'cancelled' ORDER BY i.created_at DESC",
        case["id"],
    )
    return jsonable(
        {
            "trip_case": {
                "id": case["id"],
                "code": case["code"],
                "status": case["status"],
                "channel": case["channel"],
                "start_date": case["start_date"],
            },
            "invoiceable": reason is None and staff.can("billing.write"),
            "reason": reason,
            "version": (
                {
                    "id": version["id"],
                    "number": version["version_number"],
                    "title": version["title"],
                    "total_price": version["total_price"],
                    "currency": version["currency"],
                }
                if version
                else None
            ),
            "defaults": defaults,
            "invoice": await detail(conn, staff, live["id"]) if live else None,
            "history": [summary_out(r) for r in history],
        }
    )


async def summary(conn: asyncpg.Connection) -> dict[str, Any]:
    """The /billing KPIs: outstanding and overdue balances, money received this month (verified, net of refunds) and
    payments waiting for verification, per currency plus a USD total at each version's locked FX rate (critic note 5:
    the rate the quote was sold at, ADR 0005 d5), and invoice counts by status."""
    today = await ops_today(conn)
    month_start = today.replace(day=1)
    rows = await conn.fetch(
        """
        SELECT i.currency, v.fx_rate_locked, i.status, b.balance, b.overdue_amount,
               (SELECT coalesce(sum(CASE WHEN p.kind = 'receipt' THEN p.amount ELSE -p.amount END), 0)
                  FROM booking.payments p
                 WHERE p.invoice_id = i.id AND p.verified_at IS NOT NULL AND p.voided_at IS NULL
                   AND p.received_on >= $1::date AND p.received_on <= $2::date) AS received,
               b.unverified_amount, b.unverified_count
          FROM booking.invoices i
          JOIN booking.trip_case_versions v ON v.id = i.version_id
          JOIN booking.v_invoice_balance b ON b.invoice_id = i.id
         WHERE i.deleted_at IS NULL
        """,
        month_start,
        today,
    )
    kpis: dict[str, dict[str, Any]] = {
        k: {"count": 0, "by_currency": {}, "usd": Decimal(0), "unconverted": 0}
        for k in ("outstanding", "overdue", "received_this_month", "awaiting_verification")
    }
    counts = dict.fromkeys(STATUSES, 0)

    def add(kpi: str, row: Any, amount: Decimal | None, n: int = 1) -> None:
        if amount is None or amount == 0:
            return
        k = kpis[kpi]
        k["count"] += n
        k["by_currency"][row["currency"]] = k["by_currency"].get(row["currency"], Decimal(0)) + amount
        rate = Decimal(1) if row["currency"] == "USD" else row["fx_rate_locked"]
        if rate:
            k["usd"] += (amount / Decimal(rate)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        else:
            k["unconverted"] += 1

    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
        if r["status"] in ("sent", "partially_paid") and r["balance"] is not None and r["balance"] > 0:
            add("outstanding", r, r["balance"])
            add("overdue", r, r["overdue_amount"])
        if r["status"] != "draft":
            add("received_this_month", r, r["received"])
            add("awaiting_verification", r, r["unverified_amount"], int(r["unverified_count"] or 0))
    for k in kpis.values():
        k["by_currency"] = [{"currency": c, "amount": a} for c, a in sorted(k["by_currency"].items())]
    return jsonable({**kpis, "invoices": counts, "month_start": month_start, "today": today})


# ── Writes ───────────────────────────────────────────────────────────────────


def _adjustments(raw: list[dict[str, Any]], currency: str) -> list[dict[str, Any]]:
    if len(raw) > MAX_ADJUSTMENTS:
        raise Invalid("too_many_adjustments", f"at most {MAX_ADJUSTMENTS} adjustment lines")
    out = []
    for n, adj in enumerate(raw, start=1):
        description = " ".join(str(adj.get("description") or "").split())[:500]
        reason = " ".join(str(adj.get("reason") or "").split())[:500]
        if not description:
            raise Invalid("description_required", f"adjustment {n}: say what it is for")
        if not reason:
            raise Invalid("reason_required", f"adjustment {n}: say why it is made")
        quantity = money.parse_amount(adj.get("quantity", 1), "USD", field=f"adjustment {n} quantity")
        if not Decimal(0) < quantity <= Decimal(9999):
            raise Invalid("invalid_quantity", f"adjustment {n}: quantity is more than 0 and at most 9999")
        unit = money.parse_amount(adj.get("unit_price"), currency, field=f"adjustment {n} price", allow_negative=True)
        amount = money.line_amount(quantity, unit)
        if amount == 0:
            raise Invalid("invalid_amount", f"adjustment {n}: the amount is 0")
        if amount != money.round_money(amount, currency):
            # e.g. 1.5 x 1001 VND: the line would carry a fraction the schedule (minor units) can never add up to.
            raise Invalid(
                "invalid_amount",
                f"adjustment {n}: quantity x price gives {amount}, more decimals than {currency} allows",
            )
        out.append(
            {"description": description, "reason": reason, "quantity": quantity, "unit_price": unit, "amount": amount}
        )
    return out


def _schedule_in(raw: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    if raw is None:
        return None
    return [{**r, "due_date": r.get("due_date")} for r in raw]


async def _insert_lines(
    conn: asyncpg.Connection, staff: Staff, invoice_id: str, lines: list[dict[str, Any]], first: int
) -> None:
    for position, line in enumerate(lines, start=first):
        await conn.execute(
            "INSERT INTO booking.invoice_items (invoice_id, position, kind, description, quantity, unit_price, amount,"
            " reason, created_by, updated_by) VALUES ($1::uuid, $2, 'adjustment', $3, $4, $5, $6, $7, $8, $8)",
            invoice_id,
            position,
            line["description"],
            line["quantity"],
            line["unit_price"],
            line["amount"],
            line["reason"],
            staff.cis_user_id,
        )


async def _insert_schedule(
    conn: asyncpg.Connection, staff: Staff, invoice_id: str, rows: list[dict[str, Any]], first: int = 1
) -> None:
    for seq, row in enumerate(rows, start=first):
        await conn.execute(
            "INSERT INTO booking.payment_schedule (invoice_id, seq, kind, amount, due_date, created_by, updated_by)"
            " VALUES ($1::uuid, $2, $3, $4, $5, $6, $6)",
            invoice_id,
            seq,
            row["kind"],
            row["amount"],
            row["due_date"],
            staff.cis_user_id,
        )


def _body_for_hash(body: dict[str, Any]) -> dict[str, Any]:
    return {
        "adjustments": body.get("adjustments") or [],
        "schedule": body.get("schedule"),
        "notes": body.get("notes"),
        "terms": body.get("terms"),
        "send_now": bool(body.get("send_now")),
    }


async def create(
    conn: asyncpg.Connection, staff: Staff, ref: str, body: dict[str, Any], key: str | None
) -> tuple[dict[str, Any], bool]:
    """A draft invoice from the approved version (+ adjustments, + the given or default schedule), sent at once with
    ``send_now``. Returns (invoice, replayed)."""
    idem = money.idempotency_key(key)
    sha = money.request_hash(_body_for_hash(body))
    case = await lock_case(conn, ref)
    stored = await conn.fetchrow(
        "SELECT id::text AS id, request_sha256 FROM booking.invoices WHERE trip_case_id = $1::uuid"
        " AND idempotency_key = $2",
        case["id"],
        idem,
    )
    if stored is not None:
        if stored["request_sha256"] != sha:
            raise Conflict("idempotency_conflict", "this Idempotency-Key was already used for another request")
        return await detail(conn, staff, stored["id"]), True
    if case["channel"] == "b2b":
        raise Conflict("b2b_no_invoice", "a B2B trip has no customer invoice (agency settlement comes later)")
    live = await conn.fetchrow(LIVE_SQL, case["id"])
    if live is not None:
        raise Conflict(
            "invoice_exists",
            f"{case['code']} already has invoice {live['invoice_number']}",
            invoice=live["invoice_number"],
        )
    if case["status"] not in INVOICEABLE_CASE_STATUSES:
        raise Conflict(
            "case_not_invoiceable", f"trip case {case['code']} is {case['status']}: invoice a confirmed trip"
        )
    version = await conn.fetchrow(APPROVED_SQL, case["id"])
    if version is None:
        raise Conflict("no_approved_version", f"trip case {case['code']} has no approved version")
    if version["total_price"] is None or version["currency"] is None:
        raise Conflict("version_not_priced", f"version {version['version_number']} has no price: price it first")
    currency = version["currency"].strip()
    version_total = Decimal(version["total_price"])
    adjustments = _adjustments(body.get("adjustments") or [], currency)
    total = version_total + sum((a["amount"] for a in adjustments), Decimal(0))
    if total <= 0:
        raise Invalid("invalid_total", "the invoice total must be more than 0")
    schedule_raw = _schedule_in(body.get("schedule"))
    schedule = (
        money.normalize_schedule(schedule_raw, total, currency)
        if schedule_raw is not None
        else money.default_schedule(
            total,
            currency,
            issue_day=await ops_today(conn),
            start=case["start_date"],
            policy=await schedule_policy(conn),
        )
    )
    notes = (body.get("notes") or "").strip() or None
    terms = (body.get("terms") or "").strip() or None
    try:
        async with conn.transaction():
            invoice_id = await conn.fetchval(
                "INSERT INTO booking.invoices (trip_case_id, version_id, customer_id, currency, notes, terms,"
                " idempotency_key, request_sha256, created_by, updated_by)"
                " VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5, $6, $7, $8, $9, $9) RETURNING id::text",
                case["id"],
                version["id"],
                case["customer_id"],
                currency,
                notes,
                terms,
                idem,
                sha,
                staff.cis_user_id,
            )
    except asyncpg.UniqueViolationError as exc:  # a concurrent create of the same case won
        raise Conflict("invoice_exists", f"{case['code']} already has a live invoice") from exc
    await conn.execute(
        "INSERT INTO booking.invoice_items (invoice_id, position, kind, description, quantity, unit_price, amount,"
        " created_by, updated_by) VALUES ($1::uuid, $2, 'version_total', $3, 1, $4, $4, $5, $5)",
        invoice_id,
        VERSION_LINE,
        f"Trip price ({version['title'] or 'proposal V' + str(version['version_number'])})"[:500],
        version_total,
        staff.cis_user_id,
    )
    await _insert_lines(conn, staff, invoice_id, adjustments, VERSION_LINE + 1)
    await _insert_schedule(conn, staff, invoice_id, schedule)
    row = await find_invoice(conn, invoice_id)
    await _audit(
        conn,
        staff,
        "invoice.created",
        row,
        case["code"],
        f"Drafted {row['invoice_number']}: {total} {currency}, {len(schedule)} payment(s)",
        after={"total": total, "currency": currency, "adjustments": len(adjustments), "schedule": schedule},
    )
    if body.get("send_now"):
        await _send(conn, staff, case, row["id"])
    return await detail(conn, staff, invoice_id), False


async def update_draft(conn: asyncpg.Connection, staff: Staff, number: str, changes: dict[str, Any]) -> dict[str, Any]:
    """Edit a draft: replace its adjustments and / or schedule (no schedule with new adjustments = the default one),
    notes, terms. A sent invoice changes notes only; its lines and schedule change by an amendment."""
    case, inv = await lock_invoice(conn, number)
    status = inv["status"]
    touched = sorted(k for k in ("adjustments", "schedule", "notes", "terms") if k in changes)
    if status != "draft":
        if set(touched) - {"notes"}:
            raise Conflict(
                "invoice_sent", f"{inv['invoice_number']} is {status}: amend it (POST …/amend) to change lines"
            )
        if status == "cancelled":
            raise Conflict("invoice_closed", f"{inv['invoice_number']} is cancelled")
    currency = inv["currency"].strip()
    before = {"total": inv["total_amount"]}
    if "adjustments" in changes:
        lines = _adjustments(changes.get("adjustments") or [], currency)
        await conn.execute(
            "DELETE FROM booking.invoice_items WHERE invoice_id = $1::uuid AND kind = 'adjustment'", inv["id"]
        )
        await _insert_lines(conn, staff, inv["id"], lines, VERSION_LINE + 1)
    total = Decimal(await conn.fetchval("SELECT total_amount FROM booking.invoices WHERE id = $1::uuid", inv["id"]))
    if status == "draft" and total <= 0:
        raise Invalid("invalid_total", "the invoice total must be more than 0")
    if status == "draft" and ("schedule" in changes or "adjustments" in changes):
        raw = _schedule_in(changes.get("schedule"))
        rows = (
            money.normalize_schedule(raw, total, currency)
            if raw is not None
            else money.default_schedule(
                total,
                currency,
                issue_day=await ops_today(conn),
                start=case["start_date"],
                policy=await schedule_policy(conn),
            )
        )
        await conn.execute("DELETE FROM booking.payment_schedule WHERE invoice_id = $1::uuid", inv["id"])
        await _insert_schedule(conn, staff, inv["id"], rows)
    sets: list[str] = []
    args: list[Any] = [inv["id"], staff.cis_user_id]
    for col in ("notes", "terms"):
        if col in changes:
            args.append((changes.get(col) or "").strip() or None)
            sets.append(f"{col} = ${len(args)}")
    if sets:
        await conn.execute(
            f"UPDATE booking.invoices SET {', '.join(sets)}, updated_by = $2 WHERE id = $1::uuid",  # noqa: S608
            *args,
        )
    if touched:
        await _audit(
            conn,
            staff,
            "invoice.updated",
            inv,
            case["code"],
            f"Edited {inv['invoice_number']}: {', '.join(touched)}",
            before=before,
            after={"total": total, "changed": touched},
        )
    return {**await detail(conn, staff, inv["id"]), "changed": touched}


async def delete_draft(conn: asyncpg.Connection, staff: Staff, number: str) -> dict[str, Any]:
    case, inv = await lock_invoice(conn, number)
    if inv["status"] != "draft":
        raise Conflict("invoice_sent", f"{inv['invoice_number']} is {inv['status']}: cancel it instead")
    await conn.execute(
        "UPDATE booking.invoices SET deleted_at = now(), updated_by = $2 WHERE id = $1::uuid",
        inv["id"],
        staff.cis_user_id,
    )
    await _audit(conn, staff, "invoice.deleted", inv, case["code"], f"Deleted draft {inv['invoice_number']}")
    return {"invoice_number": inv["invoice_number"], "deleted": True}


async def _send(conn: asyncpg.Connection, staff: Staff, case: Any, invoice_id: str) -> None:
    inv = await conn.fetchrow(
        "SELECT id::text AS id, invoice_number, status, currency, total_amount, customer_id::text AS customer_id"
        " FROM booking.invoices WHERE id = $1::uuid FOR UPDATE",
        invoice_id,
    )
    if case["status"] not in INVOICEABLE_CASE_STATUSES:
        raise Conflict("case_not_invoiceable", f"trip case {case['code']} is {case['status']}")
    sched = await conn.fetchrow(
        "SELECT count(*) AS n, coalesce(sum(amount), 0) AS total FROM booking.payment_schedule"
        " WHERE invoice_id = $1::uuid",
        invoice_id,
    )
    if not sched["n"] or sched["total"] != inv["total_amount"]:
        raise Invalid(
            "schedule_mismatch",
            f"the schedule adds up to {sched['total']} but the invoice total is {inv['total_amount']}",
            schedule_total=str(sched["total"]),
            total=str(inv["total_amount"]),
        )
    customer = await conn.fetchrow(
        "SELECT name, email, phone, address, code FROM booking.customers WHERE id = $1::uuid", inv["customer_id"]
    )
    name = (customer["name"] or customer["email"] or customer["phone"] or customer["code"])[:200]
    try:
        await conn.execute(
            "UPDATE booking.invoices SET status = 'sent', issued_at = now(), sent_by = $2, bill_to_name = $3,"
            " bill_to_email = $4, bill_to_address = $5, updated_by = $2 WHERE id = $1::uuid",
            invoice_id,
            staff.cis_user_id,
            name,
            customer["email"],
            (customer["address"] or None) and customer["address"][:500],
        )
    except asyncpg.CheckViolationError as exc:
        raise Conflict("invoice_not_sendable", str(exc).split("\n")[0]) from exc
    total = f"{inv['total_amount']} {inv['currency'].strip()}"
    await notifications.notify_customer(
        conn,
        customer_id=inv["customer_id"],
        type="invoice.sent",
        title=f"Payment request {inv['invoice_number']}",
        message=f"Your payment request {inv['invoice_number']} for {total} is ready, with its due dates.",
        entity_type="invoice",
        entity_id=invoice_id,
        data={"invoice_number": inv["invoice_number"], "case_code": case["code"], "total": total},
    )
    await _audit(
        conn,
        staff,
        "invoice.sent",
        inv,
        case["code"],
        f"Sent {inv['invoice_number']} ({total}); customer notified in My Trips (no e-mail)",
        before={"status": "draft"},
        after={"status": "sent"},
    )
    await track.advance_case(conn, case, cause=f"invoice {inv['invoice_number']} sent")


async def send(conn: asyncpg.Connection, staff: Staff, number: str) -> dict[str, Any]:
    case, inv = await lock_invoice(conn, number)
    if inv["status"] != "draft":
        if inv["status"] == "cancelled":
            raise Conflict("invoice_closed", f"{inv['invoice_number']} is cancelled")
        return await detail(conn, staff, inv["id"])  # already sent: the same answer
    await _send(conn, staff, case, inv["id"])
    return await detail(conn, staff, inv["id"])


async def cancel(conn: asyncpg.Connection, staff: Staff, number: str, reason: str) -> dict[str, Any]:
    reason = (reason or "").strip()
    if not reason:
        raise Invalid("reason_required", "say why the invoice is cancelled")
    case, inv = await lock_invoice(conn, number)
    if inv["status"] == "cancelled":
        return await detail(conn, staff, inv["id"])
    if inv["status"] == "draft":
        raise Conflict("invoice_draft", f"{inv['invoice_number']} is a draft: delete it instead")
    money_row = await conn.fetchrow(
        "SELECT net_paid, unverified_count FROM booking.v_invoice_balance WHERE invoice_id = $1::uuid", inv["id"]
    )
    if money_row["net_paid"] != 0 or money_row["unverified_count"]:
        raise Conflict(
            "invoice_has_payments",
            "refund the verified payments and void or verify the waiting ones before cancelling",
            net_paid=str(money_row["net_paid"]),
            unverified=int(money_row["unverified_count"]),
        )
    try:
        await conn.execute(
            "UPDATE booking.invoices SET status = 'cancelled', cancelled_at = now(), cancelled_by = $2,"
            " cancel_reason = $3, updated_by = $2 WHERE id = $1::uuid",
            inv["id"],
            staff.cis_user_id,
            reason[:500],
        )
    except asyncpg.CheckViolationError as exc:
        raise Conflict("invoice_has_payments", str(exc).split("\n")[0]) from exc
    await notifications.notify_customer(
        conn,
        customer_id=inv["customer_id"],
        type="invoice.cancelled",
        title=f"Payment request {inv['invoice_number']} cancelled",
        message=f"Payment request {inv['invoice_number']} was cancelled. Your trip consultant will be in touch.",
        entity_type="invoice",
        entity_id=inv["id"],
        data={"invoice_number": inv["invoice_number"], "case_code": case["code"]},
    )
    await _audit(
        conn,
        staff,
        "invoice.cancelled",
        inv,
        case["code"],
        f"Cancelled {inv['invoice_number']}: {reason[:200]}",
        before={"status": inv["status"]},
        after={"status": "cancelled", "reason": reason[:500]},
    )
    if case["status"] != "cancelled":
        await track.advance_case(conn, case, cause=f"invoice {inv['invoice_number']} cancelled")
    return await detail(conn, staff, inv["id"])


AMENDING_SQL = "UPDATE booking.invoices SET amending = $2, updated_by = $3 WHERE id = $1::uuid"


async def amend(conn: asyncpg.Connection, staff: Staff, number: str, body: dict[str, Any]) -> dict[str, Any]:
    """Critic B5: append adjustment lines to a sent invoice and re-plan the part of the schedule no verified payment
    covers yet. ``schedule`` = the new uncovered rows (they follow the covered ones); without it the difference goes on
    the last uncovered row (a new row when there is none). The state follows (paid can drop to partially paid)."""
    case, inv = await lock_invoice(conn, number)
    if inv["status"] not in MONEY_STATUSES:
        raise Conflict("invoice_not_sent", f"{inv['invoice_number']} is {inv['status']}: edit a draft instead")
    currency = inv["currency"].strip()
    lines = _adjustments(body.get("adjustments") or [], currency)
    if not lines:
        raise Invalid("adjustments_required", "an amendment adds at least one adjustment line")
    old_total = Decimal(inv["total_amount"])
    new_total = old_total + sum((x["amount"] for x in lines), Decimal(0))
    if new_total <= 0:
        raise Invalid("invalid_total", "the invoice total must stay more than 0; cancel the invoice instead")
    net = Decimal(await conn.fetchval("SELECT booking.invoice_net_paid($1::uuid)", inv["id"]))
    current = [
        {**dict(r), "amount": Decimal(r["amount"])}
        for r in await conn.fetch(
            "SELECT id::text AS id, seq, kind, amount, due_date FROM booking.payment_schedule"
            " WHERE invoice_id = $1::uuid ORDER BY due_date, seq",
            inv["id"],
        )
    ]
    covered = money.waterfall(current, net)
    frozen = [r for r, c in zip(current, covered, strict=True) if c > 0]
    open_rows = [r for r, c in zip(current, covered, strict=True) if c == 0]
    frozen_total = sum((r["amount"] for r in frozen), Decimal(0))
    last_due = max((r["due_date"] for r in frozen), default=None)
    raw = _schedule_in(body.get("schedule"))
    if raw is not None:
        planned = money.normalize_schedule(raw, new_total - frozen_total, currency) if raw else []
        if not raw and new_total != frozen_total:
            raise Invalid("schedule_required", "the schedule must cover the rest of the new total")
    else:
        planned = [{"kind": r["kind"], "amount": r["amount"], "due_date": r["due_date"]} for r in open_rows]
        delta = new_total - old_total
        if planned:
            planned[-1]["amount"] += delta
            if planned[-1]["amount"] <= 0:
                raise Invalid("schedule_required", "give the new schedule: the change does not fit the last payment")
        elif delta > 0:
            due = max(await ops_today(conn), last_due or date.min)
            planned = [{"kind": "other", "amount": delta, "due_date": due}]
        else:
            raise Invalid("schedule_required", "give the new schedule: every payment is already covered")
    if last_due is not None and any(r["due_date"] < last_due for r in planned):
        raise Invalid("invalid_schedule", f"new payments are due on or after {last_due} (the last paid one)")
    max_line = await conn.fetchval(
        "SELECT max(position) FROM booking.invoice_items WHERE invoice_id = $1::uuid", inv["id"]
    )
    if len(frozen) + len(planned) > money.MAX_SCHEDULE_ROWS:
        raise Invalid("invalid_schedule", f"a payment schedule has at most {money.MAX_SCHEDULE_ROWS} rows")
    try:
        # The amendment window (bk-028 invoices.amending): the guards let lines and schedule rows of a sent invoice
        # change only while it is open; closing it checks that the schedule adds up to the new total (a 409 here).
        await conn.execute(AMENDING_SQL, inv["id"], True, staff.cis_user_id)
        await _insert_lines(conn, staff, inv["id"], lines, int(max_line or 0) + 1)
        if open_rows:
            await conn.execute(
                "DELETE FROM booking.payment_schedule WHERE id = ANY($1::uuid[])", [r["id"] for r in open_rows]
            )
        await _insert_schedule(conn, staff, inv["id"], planned, max((r["seq"] for r in frozen), default=0) + 1)
        await conn.execute(AMENDING_SQL, inv["id"], False, staff.cis_user_id)
    except asyncpg.CheckViolationError as exc:
        raise Conflict("invalid_amendment", str(exc).split("\n")[0]) from exc
    except asyncpg.UniqueViolationError as exc:
        raise Conflict("invalid_amendment", "a schedule row number is taken; reload and try again") from exc
    status = await conn.fetchval("SELECT status FROM booking.invoices WHERE id = $1::uuid", inv["id"])
    await notifications.notify_customer(
        conn,
        customer_id=inv["customer_id"],
        type="invoice.updated",
        title=f"Payment request {inv['invoice_number']} updated",
        message=f"Your payment request {inv['invoice_number']} was updated: the total is now {new_total} {currency}.",
        entity_type="invoice",
        entity_id=inv["id"],
        data={"invoice_number": inv["invoice_number"], "case_code": case["code"], "total": f"{new_total} {currency}"},
    )
    await _audit(
        conn,
        staff,
        "invoice.amended",
        inv,
        case["code"],
        f"Amended {inv['invoice_number']}: {old_total} -> {new_total} {currency} ({len(lines)} line(s))",
        before={"total": old_total, "status": inv["status"]},
        after={"total": new_total, "status": status, "lines": [x["description"] for x in lines], "schedule": planned},
    )
    await track.advance_case(conn, case, cause=f"invoice {inv['invoice_number']} amended")
    return await detail(conn, staff, inv["id"])


async def remind(conn: asyncpg.Connection, staff: Staff, number: str) -> dict[str, Any]:
    """One customer notification row per invoice and day (no e-mail, design D15)."""
    case, inv = await lock_invoice(conn, number)
    if inv["status"] not in ("sent", "partially_paid"):
        raise Conflict("nothing_due", f"{inv['invoice_number']} is {inv['status']}: nothing to remind")
    bal = await conn.fetchrow(
        "SELECT balance, next_due_date FROM booking.v_invoice_balance WHERE invoice_id = $1::uuid", inv["id"]
    )
    today = await ops_today(conn)
    currency = inv["currency"].strip()
    written = await notifications.notify_customer_once(
        conn,
        customer_id=inv["customer_id"],
        type="payment.reminder",
        title=f"Reminder: payment request {inv['invoice_number']}",
        message=f"{bal['balance']} {currency} is still to pay"
        + (f", next payment due {bal['next_due_date'].isoformat()}." if bal["next_due_date"] else "."),
        entity_type="invoice",
        entity_id=inv["id"],
        reminder_key=f"payment.reminder:{inv['id']}:{today.isoformat()}",
        data={"invoice_number": inv["invoice_number"], "case_code": case["code"]},
    )
    if written:
        await _audit(
            conn, staff, "invoice.reminded", inv, case["code"], f"Reminded the customer of {inv['invoice_number']}"
        )
    return {**await detail(conn, staff, inv["id"]), "reminded": written}


# ── Case cancel / reopen (critic B9) ─────────────────────────────────────────


async def on_case_cancelled(conn: asyncpg.Connection, *, case_id: str, case_code: str) -> dict[str, int]:
    """In the transaction that cancels a case: a draft invoice is deleted; a sent one stays (refunds first, then staff
    cancel it) and raises one staff notification."""
    drafts = await conn.fetch(
        "UPDATE booking.invoices SET deleted_at = now() WHERE trip_case_id = $1::uuid AND status = 'draft'"
        " AND deleted_at IS NULL RETURNING id::text AS id, invoice_number",
        case_id,
    )
    sent = await conn.fetchrow(
        "SELECT id::text AS id, invoice_number, status FROM booking.invoices WHERE trip_case_id = $1::uuid"
        " AND deleted_at IS NULL AND status IN ('sent', 'partially_paid', 'paid')",
        case_id,
    )
    if sent is not None:
        await notifications.notify_staff(
            conn,
            type="invoice.case_cancelled",
            title=f"{case_code} cancelled: settle invoice {sent['invoice_number']}",
            message="Refund what was paid (record and verify a refund), then cancel the invoice.",
            entity_type="invoice",
            entity_id=sent["id"],
            data={"invoice_number": sent["invoice_number"], "case_code": case_code},
        )
    if drafts or sent:
        await audit.record(
            conn,
            actor_type="system",
            actor_ref="aa682:case_cancel_cascade",
            action="invoice.case_cancelled",
            entity_type="trip_case",
            entity_id=case_id,
            after={
                "deleted_drafts": [r["invoice_number"] for r in drafts],
                "to_settle": sent and sent["invoice_number"],
            },
            entity_label=case_code,
            summary=f"Case cancelled: {len(drafts)} draft invoice(s) deleted"
            + (f", {sent['invoice_number']} to refund and cancel" if sent else ""),
        )
    return {"deleted_drafts": len(drafts), "to_settle": 1 if sent else 0}


async def blocks_reopen(conn: asyncpg.Connection, case_id: str) -> str | None:
    """The invoice that keeps a cancelled case from reopening (not cancelled, not deleted), or a payment waiting."""
    row = await conn.fetchrow(LIVE_SQL, case_id)
    if row is not None:
        return row["invoice_number"]
    return await conn.fetchval(
        "SELECT p.ref FROM booking.payments p JOIN booking.invoices i ON i.id = p.invoice_id"
        " WHERE i.trip_case_id = $1::uuid AND p.verified_at IS NULL AND p.voided_at IS NULL LIMIT 1",
        case_id,
    )


async def money_badge(conn: asyncpg.Connection, case_id: str) -> dict[str, Any] | None:
    """The case header money badge: the live invoice's number, state and balance."""
    row = await conn.fetchrow(
        "SELECT i.invoice_number, i.status, i.currency, i.total_amount::text AS total_amount,"
        " b.net_paid::text AS net_paid, b.balance::text AS balance, b.overdue_amount::text AS overdue_amount,"
        " b.unverified_count FROM booking.invoices i JOIN booking.v_invoice_balance b ON b.invoice_id = i.id"
        " WHERE i.trip_case_id = $1::uuid AND i.status <> 'cancelled' AND i.deleted_at IS NULL",
        case_id,
    )
    return dict(row) if row else None
