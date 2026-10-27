"""Supplier bills and payouts (bk-030; AA-682 slice S4, S252 design §3.4, §4, §5, §6, critic B4, B6 and notes 2, 8, 9).

A **supplier bill** (``BIL-000123``) is what a supplier asks AA to pay: the supplier's own bill number, a currency, an
amount, an issue and a due date, optionally one trip case, a document (the supplier's PDF; ``POST /ops-files`` kind
``supplier_bill``), and **lines** matching it to CONFIRMED supplier bookings of that supplier (one line per booking,
amount in the bill currency).

Life: ``received`` (the working draft: facts, lines and document change freely and need not add up yet) →
``approved`` by ANOTHER person (``bill.approve``; the creator, and whoever last changed the bill's money facts or
lines (``facts_edited_by``), get 403 ``maker_checker``, the database refuses it too), only with at least one line and
the lines adding up to the amount → ``paid``, DERIVED by the database from the live payouts (paid when they reach
the amount; a voided payout steps the bill back to ``approved``).
``received`` / ``approved`` → ``cancelled`` with a reason (never "Delete", design Q23), an approved bill only while no
payout is live (void them first). Cancelled is final.

A **payout** (``SPO-000123``) records money AA sent the supplier against an approved bill (``payout.write``, admin;
never the person who entered the bill or last changed its money facts: 403 ``maker_checker``): amount in the bill
currency (never above the balance: 409 ``payout_exceeds_balance``), method, the day it was paid, the bank reference
(never a card number: 422 ``card_number_not_allowed``), a note; the evidence (bank slip) is uploaded right after
(``POST /ops-files`` kind ``payout_evidence``, the payout as entity). A payout counts at once (no verify step); a
mistake is **voided** (final, with a reason) and recorded again. Creating a bill and recording a payout take an
``Idempotency-Key`` (bills: unique globally; payouts: unique per bill) and the hash of the business body: a replay
returns the stored row (200, ``replayed``), another body is 409 ``idempotency_conflict``. Lock order: the bill, then
the payout (critic B4).

**Cost vs billed** (:func:`case_costs`): per supplier booking of a case, the expected net (``supplier_bookings``,
``pricing.read``) next to what suppliers billed (lines of bills not cancelled) and the variance.

Permissions: ``bill.read`` (admin, sales, contracting), ``bill.write`` / ``bill.approve`` (admin, contracting),
``payout.write`` (admin). None is in the preview cap (critic note 8: payout references are bank references). No job and
no agent session ever writes a bill or a payout (design §9, the AA-682 comment); ``bk_ops_reminders`` only announces
bills that are due (``supplier_bill.due``, :mod:`app.ops.lifecycle`).
"""

from __future__ import annotations

import json
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
from app.ops import lifecycle, money

log = logging.getLogger(__name__)

STATUSES = ("received", "approved", "paid", "cancelled")
OPEN_STATUSES = ("received", "approved")
METHODS = ("bank_transfer", "card_offline", "cash", "other")
MAX_PAGE = 100
MAX_LINES = 50
MAX_FUTURE_DAYS = 1

BILL_COLUMNS = """
    b.id::text AS id, b.code, b.supplier_id::text AS supplier_id, s.company_name AS supplier_name,
    b.trip_case_id::text AS trip_case_id, tc.code AS trip_case_code, tc.channel AS trip_case_channel,
    b.bill_number, b.currency, b.amount::text AS amount, b.issue_date, b.due_date, b.status, b.notes,
    b.document_file_id::text AS document_file_id, b.created_by, cr.name AS created_by_name, b.facts_edited_by,
    fe.name AS facts_edited_by_name, b.approved_by,
    ap.name AS approved_by_name, b.approved_at, b.paid_at, b.cancelled_by, ca.name AS cancelled_by_name,
    b.cancelled_at, b.cancel_reason, b.created_at, b.updated_at, bal.paid_amount::text AS paid_amount,
    bal.balance::text AS balance, bal.live_payouts, bal.line_count, bal.lines_amount::text AS lines_amount,
    bal.overdue
"""
BILL_FROM = """
FROM booking.supplier_bills b
JOIN booking.suppliers s ON s.id = b.supplier_id
JOIN booking.v_supplier_bill_balance bal ON bal.bill_id = b.id
LEFT JOIN booking.trip_cases tc ON tc.id = b.trip_case_id
LEFT JOIN booking.staff_users cr ON cr.cis_user_id = b.created_by
LEFT JOIN booking.staff_users fe ON fe.cis_user_id = b.facts_edited_by
LEFT JOIN booking.staff_users ap ON ap.cis_user_id = b.approved_by
LEFT JOIN booking.staff_users ca ON ca.cis_user_id = b.cancelled_by
"""
LINES_SQL = """
SELECT it.supplier_booking_id::text AS supplier_booking_id, it.amount::text AS amount, it.created_at,
       sb.code, sb.status, sb.item_description, sb.service_date, sb.nights, sb.pax,
       sb.net_amount::text AS net_amount, sb.net_currency, tc.id::text AS trip_case_id, tc.code AS trip_case_code,
       tc.channel AS trip_case_channel
  FROM booking.supplier_bill_items it
  JOIN booking.supplier_bookings sb ON sb.id = it.supplier_booking_id
  JOIN booking.trip_cases tc ON tc.id = sb.trip_case_id
 WHERE it.bill_id = $1::uuid
 ORDER BY sb.service_date NULLS LAST, sb.code
"""
PAYOUT_COLUMNS = """
    o.id::text AS id, o.code, o.bill_id::text AS bill_id, o.amount::text AS amount, o.currency, o.method, o.paid_on,
    o.reference, o.note, o.evidence_file_id::text AS evidence_file_id,
    CASE WHEN o.voided_at IS NOT NULL THEN 'voided' ELSE 'live' END AS state, o.recorded_by,
    rec.name AS recorded_by_name, o.recorded_at, o.voided_by, vo.name AS voided_by_name, o.voided_at, o.void_reason
"""
PAYOUT_FROM = """
FROM booking.supplier_payouts o
LEFT JOIN booking.staff_users rec ON rec.cis_user_id = o.recorded_by
LEFT JOIN booking.staff_users vo ON vo.cis_user_id = o.voided_by
"""


# ── Helpers ──────────────────────────────────────────────────────────────────


def _uuid(value: Any) -> str | None:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        return None


def jsonable(value: Any) -> Any:
    """Decimals as strings (money never travels as a float), recursively."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [jsonable(v) for v in value]
    return value


def _person(ref: str | None, name: str | None) -> dict[str, Any] | None:
    return {"id": ref, "name": name} if ref else None


def _text(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    return " ".join(str(value).split())[:limit] or None


def _note(value: Any) -> str | None:
    return (str(value).strip()[:2000] or None) if value is not None else None


def _no_card(field: str, text: str | None) -> None:
    if money.looks_like_card_number(text):
        raise Invalid(
            "card_number_not_allowed",
            f"{field} looks like a payment card number: enter the bank or receipt reference, never a card number",
        )


def approve_blockers(row: dict[str, Any], staff: Staff) -> list[str]:
    """Why ``staff`` cannot approve the bill now (empty = can): the UI explains, the API and the DB refuse."""
    out = []
    if row["status"] != "received":
        out.append("not_received")
    if not staff.can("bill.approve"):
        out.append("permission")
    if staff.cis_user_id in (row["created_by"], row["facts_edited_by"]):
        out.append("maker_checker")
    if not row["line_count"]:
        out.append("no_lines")
    elif Decimal(row["lines_amount"]) != Decimal(row["amount"]):
        out.append("lines_mismatch")
    return out


def bill_out(row: Any, staff: Staff) -> dict[str, Any]:
    out = dict(row)
    creator, editor = out["created_by"], out["facts_edited_by"]
    for who in ("created_by", "facts_edited_by", "approved_by", "cancelled_by"):
        out[who] = _person(out[who], out.pop(f"{who}_name"))
    out["supplier"] = {"id": out.pop("supplier_id"), "name": out.pop("supplier_name")}
    case_id, case_code, channel = out.pop("trip_case_id"), out.pop("trip_case_code"), out.pop("trip_case_channel")
    out["trip_case"] = {"id": case_id, "code": case_code, "channel": channel} if case_id else None
    status = out["status"]
    blockers = approve_blockers({**dict(row), "created_by": creator, "facts_edited_by": editor}, staff)
    out["approve_blockers"] = blockers
    out["actions"] = {
        # Received: everything; approved / paid: the notes only (the form's locked mode).
        "edit": status != "cancelled" and staff.can("bill.write"),
        "attach": status == "received" and staff.can("bill.write"),
        "approve": not blockers,
        "cancel": staff.can("bill.write")
        and (status == "received" or (status == "approved" and not out["live_payouts"])),
        "pay": status == "approved"
        and staff.can("payout.write")
        and staff.cis_user_id not in (creator, editor)
        and Decimal(out["balance"]) > 0,
    }
    return out


def payout_out(row: Any, staff: Staff, files: list[dict] | None = None) -> dict[str, Any]:
    out = dict(row)
    for who in ("recorded_by", "voided_by"):
        out[who] = _person(out[who], out.pop(f"{who}_name"))
    out["files"] = files or []
    out["needs_evidence"] = out["state"] == "live" and not out["evidence_file_id"]
    out["actions"] = {
        "void": out["state"] == "live" and staff.can("payout.write"),
        "attach": out["state"] == "live" and staff.can("payout.write"),
    }
    return out


async def _find(conn: asyncpg.Connection, ident: str, *, lock: bool = False) -> Any:
    key = _uuid(ident)
    row = await conn.fetchrow(
        f"SELECT {BILL_COLUMNS} {BILL_FROM} WHERE "  # noqa: S608 — constants only
        + ("b.id = $1::uuid" if key else "b.code = $1"),
        key or (ident or "").strip().upper(),
    )
    if row is None:
        raise NotFound("supplier_bill_not_found", f"supplier bill {ident} not found")
    if lock:
        await conn.execute("SELECT 1 FROM booking.supplier_bills WHERE id = $1::uuid FOR UPDATE", row["id"])
        row = await conn.fetchrow(f"SELECT {BILL_COLUMNS} {BILL_FROM} WHERE b.id = $1::uuid", row["id"])  # noqa: S608
    return row


async def _audit(
    conn: asyncpg.Connection,
    staff: Staff,
    action: str,
    entity_type: str,
    entity_id: str,
    label: str,
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
        entity_type=entity_type,
        entity_id=entity_id,
        before=jsonable(before),
        after=jsonable(after),
        entity_label=label,
        summary=summary[:500],
    )


# ── Reads ────────────────────────────────────────────────────────────────────


async def detail(conn: asyncpg.Connection, staff: Staff, bill_id: str) -> dict[str, Any]:
    row = await _find(conn, bill_id)
    out = bill_out(row, staff)
    see_net = staff.can("pricing.read")
    lines = []
    for r in await conn.fetch(LINES_SQL, row["id"]):
        line = dict(r)
        if not see_net:
            line["net_amount"] = line["net_currency"] = None
        lines.append(line)
    out["lines"] = lines
    payouts = await conn.fetch(
        f"SELECT {PAYOUT_COLUMNS} {PAYOUT_FROM} WHERE o.bill_id = $1::uuid ORDER BY o.recorded_at, o.id",  # noqa: S608
        row["id"],
    )
    attached = await ops_files.files_of(conn, "supplier_payout", [p["id"] for p in payouts])
    out["payouts"] = [payout_out(p, staff, attached.get(p["id"])) for p in payouts]
    out["files"] = (await ops_files.files_of(conn, "supplier_bill", [row["id"]])).get(row["id"], [])
    return jsonable(out)


async def list_bills(
    conn: asyncpg.Connection,
    staff: Staff,
    *,
    status: str | None = None,
    supplier: str | None = None,
    case: str | None = None,
    overdue: bool | None = None,
    due_before: date | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Supplier bills, the most urgent first (open ones by due date, then the rest newest first)."""
    if status not in (None, "open", "all", *STATUSES):
        raise Invalid("invalid_filter", "unknown status")
    if not 1 <= limit <= MAX_PAGE or offset < 0:
        raise Invalid("invalid_page", f"limit is 1..{MAX_PAGE} and offset >= 0")
    args: list[Any] = []
    where = ["true"]

    def add(sql: str, value: Any) -> None:
        args.append(value)
        where.append(sql.format(p=f"${len(args)}"))

    if status == "open":
        where.append("b.status IN ('received', 'approved')")
    elif status and status != "all":
        add("b.status = {p}", status)
    if supplier:
        ident = _uuid(supplier)
        if ident is None:
            raise Invalid("invalid_filter", "supplier is a supplier id")
        add("b.supplier_id = {p}::uuid", ident)
    if case:
        ident = _uuid(case)
        add(
            "(b.trip_case_id = {p}::uuid OR EXISTS (SELECT 1 FROM booking.supplier_bill_items it JOIN"
            " booking.supplier_bookings sb ON sb.id = it.supplier_booking_id WHERE it.bill_id = b.id"
            " AND sb.trip_case_id = {p}::uuid))"
            if ident
            else "(tc.code = {p} OR EXISTS (SELECT 1 FROM booking.supplier_bill_items it JOIN"
            " booking.supplier_bookings sb ON sb.id = it.supplier_booking_id JOIN booking.trip_cases c2"
            " ON c2.id = sb.trip_case_id WHERE it.bill_id = b.id AND c2.code = {p}))",
            ident or case.strip().upper(),
        )
    if overdue is not None:
        where.append("bal.overdue" if overdue else "NOT bal.overdue")
    if due_before:
        add("b.due_date <= {p}", due_before)
    if q:
        add(
            "(b.code ILIKE {p} OR b.bill_number ILIKE {p} OR s.company_name ILIKE {p} OR tc.code ILIKE {p})",
            paging.contains(q),
        )
    clause = " AND ".join(where)
    total = await conn.fetchval(f"SELECT count(*) {BILL_FROM} WHERE {clause}", *args)  # noqa: S608
    args.extend([limit, offset])
    rows = await conn.fetch(
        f"SELECT {BILL_COLUMNS} {BILL_FROM} WHERE {clause}"  # noqa: S608 — fixed fragments, bound values
        " ORDER BY (b.status IN ('received', 'approved')) DESC,"
        " CASE WHEN b.status IN ('received', 'approved') THEN b.due_date END ASC,"
        f" b.created_at DESC, b.id DESC LIMIT ${len(args) - 1} OFFSET ${len(args)}",
        *args,
    )
    return jsonable(
        {"items": [bill_out(r, staff) for r in rows], "total": int(total or 0), "limit": limit, "offset": offset}
    )


async def summary(conn: asyncpg.Connection) -> dict[str, Any]:
    """The supplier-bills KPIs of /billing: to approve (received), to pay (approved balances), overdue, paid out this
    month (live payouts by ``paid_on`` in the ops month), per currency, and bill counts by status."""
    today = await lifecycle.ops_today(conn, await lifecycle.ops_timezone(conn))
    month_start = today.replace(day=1)
    rows = await conn.fetch(
        "SELECT b.status, b.currency, b.amount, bal.balance, bal.overdue FROM booking.supplier_bills b"
        " JOIN booking.v_supplier_bill_balance bal ON bal.bill_id = b.id"
    )
    paid = await conn.fetch(
        "SELECT currency, sum(amount) AS amount, count(*) AS n FROM booking.supplier_payouts"
        " WHERE voided_at IS NULL AND paid_on >= $1::date AND paid_on <= $2::date GROUP BY currency",
        month_start,
        today,
    )
    kpis: dict[str, dict[str, Any]] = {k: {"count": 0, "by_currency": {}} for k in ("to_approve", "to_pay", "overdue")}

    def add(kpi: str, currency: str, amount: Decimal) -> None:
        k = kpis[kpi]
        k["count"] += 1
        k["by_currency"][currency] = k["by_currency"].get(currency, Decimal(0)) + amount

    counts = dict.fromkeys(STATUSES, 0)
    for r in rows:
        counts[r["status"]] += 1
        if r["status"] == "received":
            add("to_approve", r["currency"], r["amount"])
        if r["status"] == "approved":
            add("to_pay", r["currency"], r["balance"])
        if r["overdue"]:
            add("overdue", r["currency"], r["balance"])
    out: dict[str, Any] = {}
    for name, k in kpis.items():
        out[name] = {
            "count": k["count"],
            "by_currency": [{"currency": c, "amount": a} for c, a in sorted(k["by_currency"].items())],
        }
    out["paid_this_month"] = {
        "count": sum(int(p["n"]) for p in paid),
        "by_currency": [
            {"currency": p["currency"], "amount": p["amount"]} for p in sorted(paid, key=lambda p: p["currency"])
        ],
    }
    return jsonable({**out, "bills": counts, "month_start": month_start, "today": today})


async def billable(
    conn: asyncpg.Connection, staff: Staff, *, supplier: str, case: str | None = None, q: str | None = None
) -> dict[str, Any]:
    """Confirmed bookings of a supplier (optionally of one case) a bill can name, with what bills not cancelled
    already claim for each (a booking may be billed in parts)."""
    ident = _uuid(supplier)
    if ident is None:
        raise Invalid("invalid_filter", "supplier is a supplier id")
    args: list[Any] = [ident]
    where = ["sb.supplier_id = $1::uuid", "sb.status = 'confirmed'", "tc.deleted_at IS NULL"]
    if case:
        cid = _uuid(case)
        args.append(cid or case.strip().upper())
        where.append(f"tc.id = ${len(args)}::uuid" if cid else f"tc.code = ${len(args)}")
    if q:
        args.append(paging.contains(q))
        where.append(
            f"(sb.code ILIKE ${len(args)} OR tc.code ILIKE ${len(args)} OR sb.item_description ILIKE ${len(args)})"
        )
    rows = await conn.fetch(
        "SELECT sb.id::text AS id, sb.code, sb.item_description, sb.service_date, sb.nights, sb.pax,"  # noqa: S608 — fixed fragments, bound values
        " sb.net_amount::text AS net_amount, sb.net_currency, sb.confirmation_ref, tc.id::text AS trip_case_id,"
        " tc.code AS trip_case_code, tc.channel AS trip_case_channel,"
        " (SELECT coalesce(json_agg(json_build_object('currency', x.currency, 'amount', x.amount::text)"
        "                           ORDER BY x.currency), '[]'::json)"
        "    FROM (SELECT b.currency, sum(it.amount) AS amount FROM booking.supplier_bill_items it"
        "            JOIN booking.supplier_bills b ON b.id = it.bill_id"
        "           WHERE it.supplier_booking_id = sb.id AND b.status <> 'cancelled' GROUP BY b.currency) x"
        " ) AS billed"
        " FROM booking.supplier_bookings sb JOIN booking.trip_cases tc ON tc.id = sb.trip_case_id"
        f" WHERE {' AND '.join(where)} ORDER BY sb.service_date NULLS LAST, sb.code LIMIT 200",  # noqa: S608
        *args,
    )
    see_net = staff.can("pricing.read")
    items = []
    for r in rows:
        item = dict(r)
        item["billed"] = _json(item["billed"])
        if not see_net:
            item["net_amount"] = item["net_currency"] = None
        items.append(item)
    return jsonable({"items": items})


def _total() -> dict[str, Any]:
    return {"net": Decimal(0), "billed": Decimal(0), "variance": None}


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


async def case_costs(conn: asyncpg.Connection, staff: Staff, ref: str) -> dict[str, Any]:
    """The case's cost vs billed panel: per supplier booking (live, or with a bill line), the expected net next to the
    sum of bill lines of bills not cancelled, per currency, and the variance (billed - net) once the booking is billed
    and the currencies agree. Totals per currency: net, billed, and the sum of the line variances (null when none).
    Needs ``pricing.read`` (net amounts; design §2 S4) on top of ``bill.read``."""
    if not staff.can("pricing.read"):
        raise Forbidden("forbidden", "the cost panel shows net amounts: it needs pricing.read")
    ident = _uuid(ref)
    case = await conn.fetchrow(
        "SELECT id::text AS id, code, channel FROM booking.trip_cases WHERE "  # noqa: S608 — fixed fragments, bound values
        + ("id = $1::uuid" if ident else "code = $1")
        + " AND deleted_at IS NULL",
        ident or (ref or "").strip().upper(),
    )
    if case is None:
        raise NotFound("trip_case_not_found", f"trip case {ref} not found")
    rows = await conn.fetch(
        """
        SELECT sb.id::text AS id, sb.code, sb.status, sb.item_description, sb.service_date,
               sb.net_amount, sb.net_currency, s.id::text AS supplier_id, s.company_name AS supplier_name,
               (SELECT coalesce(json_agg(json_build_object('currency', x.currency, 'amount', x.amount::text,
                                                           'bills', x.bills) ORDER BY x.currency), '[]'::json)
                  FROM (SELECT b.currency, sum(it.amount) AS amount, array_agg(b.code ORDER BY b.code) AS bills
                          FROM booking.supplier_bill_items it JOIN booking.supplier_bills b ON b.id = it.bill_id
                         WHERE it.supplier_booking_id = sb.id AND b.status <> 'cancelled'
                         GROUP BY b.currency) x) AS billed
          FROM booking.supplier_bookings sb
          JOIN booking.suppliers s ON s.id = sb.supplier_id
         WHERE sb.trip_case_id = $1::uuid
           AND (sb.status IN ('pending', 'requested', 'confirmed')
                OR EXISTS (SELECT 1 FROM booking.supplier_bill_items it WHERE it.supplier_booking_id = sb.id))
         ORDER BY sb.service_date NULLS LAST, sb.code
        """,
        case["id"],
    )
    totals: dict[str, dict[str, Decimal]] = {}
    lines = []
    for r in rows:
        billed = [
            {"currency": b["currency"], "amount": Decimal(b["amount"]), "bills": b["bills"]} for b in _json(r["billed"])
        ]
        net, net_cur = r["net_amount"], (r["net_currency"] or "").strip() or None
        variance = None
        if net is not None and net_cur:
            same = [b for b in billed if b["currency"] == net_cur]
            # A variance only once the booking is billed, in its net currency only (nothing billed yet is not "billed
            # less than costed").
            if billed and len(billed) == len(same):
                variance = same[0]["amount"] - net
            t = totals.setdefault(net_cur, _total())
            t["net"] += net
            if variance is not None:
                t["variance"] = (t["variance"] or Decimal(0)) + variance
        for b in billed:
            t = totals.setdefault(b["currency"], _total())
            t["billed"] += b["amount"]
        lines.append(
            {
                "supplier_booking": {"id": r["id"], "code": r["code"], "status": r["status"]},
                "supplier": {"id": r["supplier_id"], "name": r["supplier_name"]},
                "item_description": r["item_description"],
                "service_date": r["service_date"],
                "net_amount": net,
                "net_currency": net_cur,
                "billed": billed,
                "variance": variance,
                "currency_mismatch": bool(billed) and net_cur is not None and variance is None,
                "unbilled": not billed and r["status"] == "confirmed",
            }
        )
    # Variance per currency: the sum of the line variances (bookings billed in their net currency); unbilled nets stay
    # in ``net`` but out of the variance, and a currency with no comparable line has none (null).
    by_currency = [
        {"currency": c, "net": t["net"], "billed": t["billed"], "variance": t["variance"]}
        for c, t in sorted(totals.items())
    ]
    return jsonable({"trip_case": dict(case), "lines": lines, "totals": by_currency})


# ── Writes: bills ────────────────────────────────────────────────────────────


def _lines(raw: list[dict[str, Any]] | None, currency: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for n, line in enumerate(raw or [], start=1):
        booking = _uuid(line.get("supplier_booking_id"))
        if booking is None:
            raise Invalid("invalid_line", f"line {n}: supplier_booking_id is a supplier booking id")
        if booking in seen:
            raise Invalid("invalid_line", f"line {n}: a booking appears once on a bill")
        seen.add(booking)
        amount = money.parse_amount(line.get("amount"), currency, field=f"line {n} amount")
        if amount <= 0:
            raise Invalid("invalid_line", f"line {n}: the amount must be more than 0")
        out.append({"supplier_booking_id": booking, "amount": amount})
    if len(out) > MAX_LINES:
        raise Invalid("too_many_lines", f"a bill has at most {MAX_LINES} lines")
    return out


async def _insert_lines(conn: asyncpg.Connection, staff: Staff, bill_id: str, lines: list[dict[str, Any]]) -> None:
    for line in lines:
        try:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO booking.supplier_bill_items (bill_id, supplier_booking_id, amount, created_by)"
                    " VALUES ($1::uuid, $2::uuid, $3, $4)",
                    bill_id,
                    line["supplier_booking_id"],
                    line["amount"],
                    staff.cis_user_id,
                )
        except asyncpg.CheckViolationError as exc:
            raise Conflict("invalid_bill_line", str(exc).split("\n")[0]) from exc
        except asyncpg.ForeignKeyViolationError as exc:
            raise NotFound("supplier_booking_not_found", "a line names a supplier booking that does not exist") from exc


def _body_for_hash(body: dict[str, Any]) -> dict[str, Any]:
    keys = ("supplier_id", "trip_case", "bill_number", "currency", "amount", "issue_date", "due_date", "notes")
    out = {k: body.get(k) for k in keys}
    out["lines"] = sorted(
        ({"b": str(x.get("supplier_booking_id")), "a": x.get("amount")} for x in body.get("lines") or []),
        key=lambda x: x["b"],
    )
    return out


def _dates(issue: Any, due: Any, today: date) -> tuple[date, date]:
    if not isinstance(issue, date) or not isinstance(due, date):
        raise Invalid("invalid_dates", "the issue date and the due date are required")
    if issue > today + timedelta(days=MAX_FUTURE_DAYS):
        raise Invalid("invalid_dates", "the issue date cannot be in the future")
    if due < issue:
        raise Invalid("invalid_dates", "the due date cannot be before the issue date")
    return issue, due


async def _case_id(conn: asyncpg.Connection, ref: str | None) -> str | None:
    if not ref:
        return None
    ident = _uuid(ref)
    case_id = await conn.fetchval(
        "SELECT id::text FROM booking.trip_cases WHERE "  # noqa: S608 — fixed fragments, bound values
        + ("id = $1::uuid" if ident else "code = $1")
        + " AND deleted_at IS NULL",
        ident or ref.strip().upper(),
    )
    if case_id is None:
        raise NotFound("trip_case_not_found", f"trip case {ref} not found")
    return case_id


async def create(
    conn: asyncpg.Connection, staff: Staff, body: dict[str, Any], key: str | None
) -> tuple[dict[str, Any], bool]:
    """Enter a supplier bill (received) with its lines. Returns (bill, replayed)."""
    idem = money.idempotency_key(key)
    sha = money.request_hash(_body_for_hash(body))
    stored = await conn.fetchrow(
        "SELECT id::text AS id, request_sha256 FROM booking.supplier_bills WHERE idempotency_key = $1", idem
    )
    if stored is not None:
        if stored["request_sha256"] != sha:
            raise Conflict("idempotency_conflict", "this Idempotency-Key was already used for another request")
        return await detail(conn, staff, stored["id"]), True
    supplier_id = _uuid(body.get("supplier_id"))
    supplier = (
        await conn.fetchrow(
            "SELECT id::text AS id, company_name FROM booking.suppliers WHERE id = $1::uuid", supplier_id
        )
        if supplier_id
        else None
    )
    if supplier is None:
        raise NotFound("supplier_not_found", "supplier not found")
    currency = str(body.get("currency") or "").strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        raise Invalid("invalid_currency", "currency is a 3-letter ISO code")
    amount = money.parse_amount(body.get("amount"), currency)
    if amount <= 0:
        raise Invalid("invalid_amount", "the amount must be more than 0")
    number = _text(body.get("bill_number"), 100)
    if not number:
        raise Invalid("bill_number_required", "enter the supplier's bill number")
    issue, due = _dates(
        body.get("issue_date"),
        body.get("due_date"),
        await lifecycle.ops_today(conn, await lifecycle.ops_timezone(conn)),
    )
    case_id = await _case_id(conn, body.get("trip_case"))
    notes = _note(body.get("notes"))
    lines = _lines(body.get("lines"), currency)
    try:
        async with conn.transaction():
            row = await conn.fetchrow(
                "INSERT INTO booking.supplier_bills (supplier_id, trip_case_id, bill_number, currency, amount,"
                " issue_date, due_date, notes, idempotency_key, request_sha256, created_by, facts_edited_by,"
                " updated_by) VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8, $9, $10, $11, $11, $11)"
                " RETURNING id::text, code",
                supplier["id"],
                case_id,
                number,
                currency,
                amount,
                issue,
                due,
                notes,
                idem,
                sha,
                staff.cis_user_id,
            )
    except asyncpg.UniqueViolationError as exc:
        if "idempotency" in (exc.constraint_name or ""):
            raise Conflict("idempotency_conflict", "this Idempotency-Key was just used by another request") from exc
        raise Conflict(
            "duplicate_bill_number", f"{supplier['company_name']} already has a bill {number} that is not cancelled"
        ) from exc
    await _insert_lines(conn, staff, row["id"], lines)
    await _audit(
        conn,
        staff,
        "supplier_bill.created",
        "supplier_bill",
        row["id"],
        f"{row['code']} {supplier['company_name']}",
        f"Entered {row['code']}: {supplier['company_name']} bill {number}, {amount} {currency}, due {due.isoformat()}",
        after={
            "bill_number": number,
            "currency": currency,
            "amount": amount,
            "issue_date": issue,
            "due_date": due,
            "trip_case_id": case_id,
            "lines": len(lines),
            "notes": "(set)" if notes else None,
        },
    )
    return await detail(conn, staff, row["id"]), False


async def update(conn: asyncpg.Connection, staff: Staff, ident: str, changes: dict[str, Any]) -> dict[str, Any]:
    """Edit a received bill (number, currency, amount, dates, notes, lines = the whole new set); notes only on an
    approved or paid one. Whoever changes a money fact or the lines becomes the bill's ``facts_edited_by`` and can then
    neither approve it nor record its payout (maker-checker: another person checks the numbers as they stand)."""
    row = await _find(conn, ident, lock=True)
    status = row["status"]
    if status == "cancelled":
        raise Conflict("supplier_bill_closed", f"{row['code']} is cancelled")
    if status != "received" and set(changes) - {"notes"}:
        raise Conflict("supplier_bill_locked", f"{row['code']} is {status}: only its notes change")
    currency = str(changes.get("currency") or row["currency"]).strip().upper()
    new: dict[str, Any] = {}
    if "bill_number" in changes:
        new["bill_number"] = _text(changes["bill_number"], 100)
        if not new["bill_number"]:
            raise Invalid("bill_number_required", "enter the supplier's bill number")
    if "currency" in changes:
        if len(currency) != 3 or not currency.isalpha():
            raise Invalid("invalid_currency", "currency is a 3-letter ISO code")
        new["currency"] = currency
    if "amount" in changes:
        new["amount"] = money.parse_amount(changes["amount"], currency)
        if new["amount"] <= 0:
            raise Invalid("invalid_amount", "the amount must be more than 0")
    if "issue_date" in changes or "due_date" in changes:
        today = await lifecycle.ops_today(conn, await lifecycle.ops_timezone(conn))
        new["issue_date"], new["due_date"] = _dates(
            changes.get("issue_date", row["issue_date"]), changes.get("due_date", row["due_date"]), today
        )
    if "notes" in changes:
        new["notes"] = _note(changes["notes"])
    changed = sorted(k for k, v in new.items() if v != (Decimal(row[k]) if k == "amount" else row[k]))
    lines: list[dict[str, Any]] | None = None
    if "lines" in changes and changes["lines"] is not None:
        lines = _lines(changes["lines"], currency)
        old = {r["supplier_booking_id"]: Decimal(r["amount"]) for r in await conn.fetch(LINES_SQL, row["id"])}
        if old == {x["supplier_booking_id"]: x["amount"] for x in lines}:
            lines = None
    lines_changed = lines is not None
    facts_changed = lines_changed or any(k != "notes" for k in changed)
    if changed or lines_changed:
        # The DB guard sets facts_edited_by itself on a fact change; a lines-only change marks it here, before the
        # lines are replaced (supplier_bill_items_guard checks it).
        sets = ", ".join(
            [f"{k} = ${i + 3}" for i, k in enumerate(changed)] + (["facts_edited_by = $2"] if facts_changed else [])
        )
        try:
            await conn.execute(
                f"UPDATE booking.supplier_bills SET {sets}, updated_by = $2 WHERE id = $1::uuid",  # noqa: S608
                row["id"],
                staff.cis_user_id,
                *[new[k] for k in changed],
            )
        except asyncpg.UniqueViolationError as exc:
            raise Conflict("duplicate_bill_number", "this supplier already has a bill with that number") from exc
        except asyncpg.CheckViolationError as exc:
            raise Conflict("invalid_supplier_bill", str(exc).split("\n")[0]) from exc
    if lines is not None:
        await conn.execute("DELETE FROM booking.supplier_bill_items WHERE bill_id = $1::uuid", row["id"])
        await _insert_lines(conn, staff, row["id"], lines)
    if changed or lines_changed:
        after = {k: ("(set)" if k == "notes" and new[k] else new[k]) for k in changed}
        if lines_changed:
            after["lines"] = len(changes["lines"])
        await _audit(
            conn,
            staff,
            "supplier_bill.updated",
            "supplier_bill",
            row["id"],
            f"{row['code']} {row['supplier_name']}",
            f"Edited {row['code']}: {', '.join([*changed, *(['lines'] if lines_changed else [])])}",
            after=after,
        )
    out = await detail(conn, staff, row["id"])
    return {**out, "changed": [*changed, *(["lines"] if lines_changed else [])]}


async def approve(conn: asyncpg.Connection, staff: Staff, ident: str) -> dict[str, Any]:
    """Maker-checker: another person approves a received bill whose lines add up to the amount."""
    row = await _find(conn, ident, lock=True)
    if row["status"] in ("approved", "paid"):
        return await detail(conn, staff, row["id"])  # the same answer again
    if row["status"] == "cancelled":
        raise Conflict("supplier_bill_closed", f"{row['code']} is cancelled")
    if row["created_by"] == staff.cis_user_id:
        raise Forbidden("maker_checker", f"you entered {row['code']}: another person approves it")
    if row["facts_edited_by"] == staff.cis_user_id:
        raise Forbidden("maker_checker", f"you last changed the amounts of {row['code']}: another person approves it")
    if not row["line_count"]:
        raise Conflict("bill_not_ready", "match the bill to at least one confirmed supplier booking first")
    if Decimal(row["lines_amount"]) != Decimal(row["amount"]):
        raise Conflict(
            "bill_not_ready",
            f"the lines add up to {row['lines_amount']} {row['currency']} but the bill is {row['amount']}"
            f" {row['currency']}",
        )
    try:
        await conn.execute(
            "UPDATE booking.supplier_bills SET status = 'approved', approved_by = $2, approved_at = now(),"
            " updated_by = $2 WHERE id = $1::uuid",
            row["id"],
            staff.cis_user_id,
        )
    except asyncpg.CheckViolationError as exc:
        raise Conflict("bill_not_ready", str(exc).split("\n")[0]) from exc
    await _audit(
        conn,
        staff,
        "supplier_bill.approved",
        "supplier_bill",
        row["id"],
        f"{row['code']} {row['supplier_name']}",
        f"Approved {row['code']}: {row['amount']} {row['currency']} to {row['supplier_name']}",
        before={"status": "received"},
        after={"status": "approved"},
    )
    await notifications.notify_staff(
        conn,
        type="supplier_bill.approved",
        title=f"Pay {row['code']}: {row['amount']} {row['currency']} to {row['supplier_name']}"[:200],
        message=f"Approved by {staff.name or staff.cis_user_id}; due {row['due_date'].isoformat()}. An admin records"
        " the payout with the bank slip.",
        entity_type="supplier_bill",
        entity_id=row["id"],
        data={"code": row["code"], "due_date": row["due_date"].isoformat()},
    )
    return await detail(conn, staff, row["id"])


async def cancel(conn: asyncpg.Connection, staff: Staff, ident: str, reason: str) -> dict[str, Any]:
    """Cancel a received bill, or an approved one with no live payout (final, with a reason)."""
    reason = (reason or "").strip()
    if not reason:
        raise Invalid("reason_required", "say why the bill is cancelled")
    row = await _find(conn, ident, lock=True)
    if row["status"] == "cancelled":
        return await detail(conn, staff, row["id"])
    if row["status"] == "paid" or row["live_payouts"]:
        raise Conflict("bill_has_payouts", f"{row['code']} has payouts: void them before cancelling the bill")
    try:
        await conn.execute(
            "UPDATE booking.supplier_bills SET status = 'cancelled', cancelled_at = now(), cancelled_by = $2,"
            " cancel_reason = $3, updated_by = $2 WHERE id = $1::uuid",
            row["id"],
            staff.cis_user_id,
            reason[:500],
        )
    except asyncpg.CheckViolationError as exc:
        raise Conflict("supplier_bill_not_cancellable", str(exc).split("\n")[0]) from exc
    await _audit(
        conn,
        staff,
        "supplier_bill.cancelled",
        "supplier_bill",
        row["id"],
        f"{row['code']} {row['supplier_name']}",
        f"Cancelled {row['code']}: {reason[:200]}",
        before={"status": row["status"]},
        after={"status": "cancelled", "reason": reason[:500]},
    )
    return await detail(conn, staff, row["id"])


# ── Writes: payouts ──────────────────────────────────────────────────────────


def _payout_hash(body: dict[str, Any]) -> dict[str, Any]:
    return {k: body.get(k) for k in ("amount", "method", "paid_on", "reference", "note")}


async def _payout(conn: asyncpg.Connection, staff: Staff, payout_id: str) -> dict[str, Any]:
    row = await conn.fetchrow(
        f"SELECT {PAYOUT_COLUMNS} {PAYOUT_FROM} WHERE o.id = $1::uuid",
        payout_id,  # noqa: S608 — constants only
    )
    attached = await ops_files.files_of(conn, "supplier_payout", [row["id"]])
    return jsonable(payout_out(row, staff, attached.get(row["id"])))


async def record_payout(
    conn: asyncpg.Connection, staff: Staff, ident: str, body: dict[str, Any], key: str | None
) -> tuple[dict[str, Any], bool]:
    """Record money paid to the supplier against an approved bill. Returns (payout + bill, replayed)."""
    idem = money.idempotency_key(key)
    sha = money.request_hash(_payout_hash(body))
    bill = await _find(conn, ident, lock=True)
    stored = await conn.fetchrow(
        "SELECT id::text AS id, request_sha256 FROM booking.supplier_payouts WHERE bill_id = $1::uuid"
        " AND idempotency_key = $2",
        bill["id"],
        idem,
    )
    if stored is not None:
        if stored["request_sha256"] != sha:
            raise Conflict("idempotency_conflict", "this Idempotency-Key was already used for another request")
        return {**await _payout(conn, staff, stored["id"]), "bill": await detail(conn, staff, bill["id"])}, True
    if bill["status"] != "approved":
        raise Conflict("bill_not_payable", f"{bill['code']} is {bill['status']}: payouts go on an approved bill")
    if bill["created_by"] == staff.cis_user_id:
        raise Forbidden("maker_checker", f"you entered {bill['code']}: another admin records its payout")
    if bill["facts_edited_by"] == staff.cis_user_id:
        raise Forbidden(
            "maker_checker", f"you last changed the amounts of {bill['code']}: another admin records its payout"
        )
    method = body.get("method")
    if method not in METHODS:
        raise Invalid("invalid_payout", "unknown method")
    currency = bill["currency"].strip()
    amount = money.parse_amount(body.get("amount"), currency)
    if amount <= 0:
        raise Invalid("invalid_amount", "the amount must be more than 0")
    balance = Decimal(bill["balance"])
    if amount > balance:
        raise Conflict(
            "payout_exceeds_balance", f"{amount} {currency} is more than the {balance} {currency} left to pay"
        )
    paid_on = body.get("paid_on")
    today = await lifecycle.ops_today(conn, await lifecycle.ops_timezone(conn))
    if not isinstance(paid_on, date) or paid_on > today + timedelta(days=MAX_FUTURE_DAYS):
        raise Invalid("invalid_paid_on", "the day the money left is required and cannot be in the future")
    reference = _text(body.get("reference"), 100)
    note = _note(body.get("note"))
    _no_card("the reference", reference)
    _no_card("the note", note)
    if reference and not body.get("allow_duplicate_reference"):
        dup = await conn.fetchval(
            "SELECT code FROM booking.supplier_payouts WHERE method = $1 AND lower(reference) = lower($2)"
            " AND voided_at IS NULL LIMIT 1",
            method,
            reference,
        )
        if dup:
            raise Conflict(
                "duplicate_reference",
                f"{dup} already has this {method.replace('_', ' ')} reference; record it anyway only if it is"
                " another transfer",
                payout=dup,
            )
    try:
        async with conn.transaction():
            payout_id = await conn.fetchval(
                "INSERT INTO booking.supplier_payouts (bill_id, amount, currency, method, paid_on, reference, note,"
                " idempotency_key, request_sha256, recorded_by, updated_by)"
                " VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10, $10) RETURNING id::text",
                bill["id"],
                amount,
                currency,
                method,
                paid_on,
                reference,
                note,
                idem,
                sha,
                staff.cis_user_id,
            )
    except asyncpg.CheckViolationError as exc:
        raise Conflict("invalid_payout", str(exc).split("\n")[0]) from exc
    out = await _payout(conn, staff, payout_id)
    after = await conn.fetchval("SELECT status FROM booking.supplier_bills WHERE id = $1::uuid", bill["id"])
    await _audit(
        conn,
        staff,
        "supplier_payout.recorded",
        "supplier_payout",
        payout_id,
        f"{bill['code']} {out['code']}",
        f"Paid {amount} {currency} to {bill['supplier_name']} on {bill['code']} ({method.replace('_', ' ')});"
        f" bill {bill['status']} -> {after}",
        after={
            "bill": bill["code"],
            "amount": amount,
            "currency": currency,
            "method": method,
            "paid_on": paid_on,
            "reference": "(set)" if reference else None,
            "note": "(set)" if note else None,
            "bill_status": after,
        },
    )
    return {**out, "bill": await detail(conn, staff, bill["id"])}, False


async def void_payout(conn: asyncpg.Connection, staff: Staff, payout_ident: str, reason: str) -> dict[str, Any]:
    """Take a payout out (final, with a reason); the bill steps back to approved. Lock order: bill, payout."""
    reason = (reason or "").strip()
    if not reason:
        raise Invalid("reason_required", "say why the payout is voided")
    key = _uuid(payout_ident)
    first = await conn.fetchrow(
        "SELECT id::text AS id, bill_id::text AS bill_id FROM booking.supplier_payouts WHERE "  # noqa: S608 — fixed fragments, bound values
        + ("id = $1::uuid" if key else "code = $1"),
        key or (payout_ident or "").strip().upper(),
    )
    if first is None:
        raise NotFound("supplier_payout_not_found", f"payout {payout_ident} not found")
    bill = await _find(conn, first["bill_id"], lock=True)
    row = await conn.fetchrow(
        "SELECT id::text AS id, code, amount, currency, voided_at FROM booking.supplier_payouts"
        " WHERE id = $1::uuid FOR UPDATE",
        first["id"],
    )
    if row["voided_at"] is not None:
        return {**await _payout(conn, staff, row["id"]), "bill": await detail(conn, staff, bill["id"])}
    try:
        await conn.execute(
            "UPDATE booking.supplier_payouts SET voided_at = now(), voided_by = $2, void_reason = $3, updated_by = $2"
            " WHERE id = $1::uuid",
            row["id"],
            staff.cis_user_id,
            reason[:500],
        )
    except asyncpg.CheckViolationError as exc:
        raise Conflict("payout_not_voidable", str(exc).split("\n")[0]) from exc
    after = await conn.fetchval("SELECT status FROM booking.supplier_bills WHERE id = $1::uuid", bill["id"])
    await _audit(
        conn,
        staff,
        "supplier_payout.voided",
        "supplier_payout",
        row["id"],
        f"{bill['code']} {row['code']}",
        f"Voided {row['code']} ({row['amount']} {row['currency']}): {reason[:200]}; bill {bill['status']} -> {after}",
        before={"state": "live", "bill_status": bill["status"]},
        after={"state": "voided", "reason": reason[:500], "bill_status": after},
    )
    return {**await _payout(conn, staff, row["id"]), "bill": await detail(conn, staff, bill["id"])}
