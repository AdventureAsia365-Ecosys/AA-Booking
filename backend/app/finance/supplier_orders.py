"""Supplier purchase orders (AA-1074, bk-045; S275 study item bk-06).

A **purchase order** (``booking.supplier_orders``, ``PO-000123``) asks one supplier for a set of pending supplier
bookings of one currency and service month (its **lines**: ``supplier_bookings.supplier_order_id``). *Create orders*
(:func:`create_orders`) takes the pending bookings of the trip cases and departures staff pick that are on no live
order, groups them by supplier + currency + service month and puts each group on the supplier's draft order of that
group (a new one when none exists). Bookings without a net amount or a service day are left out (counted).

Life: ``draft`` (lines can be removed; Create orders adds more) → **Send** (:func:`send`: the snapshot of the lines,
the total and the supplier's contact e-mail are fixed, ``sending``, one ``bk_supplier_order_send`` job) → ``sent``
(the job e-mailed the order; its pending lines become ``requested`` as if staff had requested each one, cases
advance). A refused e-mail puts the order back to ``draft`` with ``send_error``. ``draft`` / ``sending`` / ``sent`` →
``cancelled`` (reason, final; pending lines leave the order). The supplier's answers are recorded on each line as
before (confirm with the supplier's ref / decline): **confirmed** / **partly confirmed** is derived from the lines
(:func:`order_state`), never stored.

Outside ``prod`` the e-mail goes to the staff inbox (``STAFF_NOTIFY_EMAIL``), the supplier's address named in the body
(``redirected``): Dev suppliers are synthetic. The order shows services, dates, nights, pax, nets and AA references
only: never an agency or traveller name (DM-6.8). The printable order (:func:`document`) is the same content as HTML.

Permissions: ``ops.read`` + ``pricing.read`` to read (nets), ``ops.write`` + ``pricing.read`` to write.
"""

from __future__ import annotations

import html
import json
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import asyncpg

from app import audit, mailer
from app.auth.staff import Staff
from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.jobs import queue
from app.jobs.queue import Job
from app.jobs.worker import Handler, PermanentError
from app.ops import track
from app.ops.supplier_bills import jsonable
from app.settings import Settings

log = logging.getLogger(__name__)

KIND = "bk_supplier_order_send"
ACTOR = "system:bk_supplier_order_send"
MAX_ATTEMPTS = 5
MAX_PAGE = 100
MAX_SCOPE = 50
LIVE_LINE = ("pending", "requested", "confirmed")

ORDER_COLUMNS = """
    o.id::text AS id, o.code, o.supplier_id::text AS supplier_id, s.code AS supplier_code,
    s.company_name AS supplier_name, s.contact_email AS supplier_email, o.currency::text AS currency, o.service_month,
    o.status, o.total_net, o.snapshot, o.sent_to, o.redirected, o.send_requested_at, o.sent_by, o.sent_at,
    o.delivery_ref, o.send_error, o.note, o.cancelled_at, o.cancelled_by, o.cancel_reason, o.created_by, o.created_at,
    o.updated_at,
    (SELECT count(*) FROM booking.supplier_bookings b WHERE b.supplier_order_id = o.id)::int AS lines,
    (SELECT coalesce(array_agg(b.status ORDER BY b.code), '{}') FROM booking.supplier_bookings b
      WHERE b.supplier_order_id = o.id) AS line_statuses,
    (SELECT sum(b.net_amount) FROM booking.supplier_bookings b
      WHERE b.supplier_order_id = o.id AND b.status IN ('pending', 'requested', 'confirmed')) AS live_net
"""
ORDER_FROM = "FROM booking.supplier_orders o JOIN booking.suppliers s ON s.id = o.supplier_id"

LINES_SQL = """
SELECT b.id::text AS id, b.code, b.status, b.item_description, b.service_date, b.nights, b.pax, b.net_amount,
       b.net_currency::text AS net_currency, b.confirmation_ref, b.trip_case_id::text AS trip_case_id,
       tc.code AS trip_case_code, b.departure_id::text AS departure_id, d.start_date AS departure_start_date
  FROM booking.supplier_bookings b
  LEFT JOIN booking.trip_cases tc ON tc.id = b.trip_case_id
  LEFT JOIN booking.departures d ON d.id = b.departure_id
 WHERE b.supplier_order_id = $1::uuid
 ORDER BY b.service_date NULLS LAST, b.code
"""


# ── Pure helpers ─────────────────────────────────────────────────────────────


def order_state(status: str, line_statuses: list[str]) -> str:
    """What the order page shows: the stored status, or for a sent order ``confirmed`` (every live line confirmed) /
    ``partly_confirmed`` (some confirmed) from the suppliers' answers on the lines."""
    if status != "sent":
        return status
    live = [s for s in line_statuses if s in LIVE_LINE]
    confirmed = sum(1 for s in live if s == "confirmed")
    if live and confirmed == len(live):
        return "confirmed"
    if confirmed:
        return "partly_confirmed"
    return "sent"


def service_month(day: date) -> date:
    return day.replace(day=1)


def reference(line: dict[str, Any]) -> str:
    """The AA reference printed for a line: the trip case code, or the departure day for a shared cost."""
    if line.get("trip_case_code"):
        return str(line["trip_case_code"])
    start = line.get("departure_start_date")
    return f"Group departure {start.isoformat() if isinstance(start, date) else start or ''}".strip()


def snapshot_lines(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The order's lines as the supplier sees them (DM-6.8: no agency or traveller name)."""
    out = []
    for line in lines:
        day = line.get("service_date")
        out.append(
            {
                "code": line["code"],
                "reference": reference(line),
                "service": line["item_description"],
                "service_date": day.isoformat() if isinstance(day, date) else day,
                "nights": line.get("nights"),
                "pax": line.get("pax"),
                "net_amount": str(line["net_amount"]) if line.get("net_amount") is not None else None,
            }
        )
    return out


def _uuid(value: Any) -> str | None:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        return None


def _need_pricing(staff: Staff) -> None:
    if not staff.can("pricing.read"):
        raise Forbidden("forbidden", "purchase orders show net amounts: they need pricing.read")


def order_out(row: Any) -> dict[str, Any]:
    out = dict(row)
    out["supplier"] = {
        "id": out.pop("supplier_id"),
        "code": out.pop("supplier_code"),
        "name": out.pop("supplier_name"),
        "email": out.pop("supplier_email"),
    }
    statuses = list(out.pop("line_statuses") or [])
    out["state"] = order_state(out["status"], statuses)
    out["line_counts"] = {s: statuses.count(s) for s in sorted(set(statuses))}
    if isinstance(out.get("snapshot"), str):
        out["snapshot"] = json.loads(out["snapshot"])
    status = out["status"]
    out["actions"] = {
        "send": status == "draft" and bool(out["supplier"]["email"]) and out["lines"] > 0,
        "cancel": status in ("draft", "sending", "sent"),
        "remove_lines": status == "draft",
    }
    return out


async def _find(conn: asyncpg.Connection, ref: str, *, lock: bool = False) -> Any:
    ident = _uuid(ref)
    where = "o.id = $1::uuid" if ident else "o.code = $1"
    row = await conn.fetchrow(
        f"SELECT {ORDER_COLUMNS} {ORDER_FROM} WHERE {where}",  # noqa: S608 — constants only
        ident or (ref or "").strip().upper(),
    )
    if row is None:
        raise NotFound("supplier_order_not_found", f"purchase order {ref} not found")
    if lock:
        await conn.execute("SELECT 1 FROM booking.supplier_orders WHERE id = $1::uuid FOR UPDATE", row["id"])
        row = await conn.fetchrow(f"SELECT {ORDER_COLUMNS} {ORDER_FROM} WHERE o.id = $1::uuid", row["id"])  # noqa: S608
    return row


async def _audit(conn: asyncpg.Connection, actor: str, action: str, order: Any, summary: str, **kw: Any) -> None:
    await audit.record(
        conn,
        actor_type="system" if actor.startswith("system:") else "staff",
        actor_ref=actor,
        action=action,
        entity_type="supplier_order",
        entity_id=order["id"],
        entity_label=f"{order['code']} {order['supplier_name']}"[:200],
        summary=summary[:500],
        before=jsonable(kw.get("before")),
        after=jsonable(kw.get("after")),
    )


# ── Reads ────────────────────────────────────────────────────────────────────


async def detail(conn: asyncpg.Connection, staff: Staff, ref: str) -> dict[str, Any]:
    _need_pricing(staff)
    row = await _find(conn, ref)
    out = order_out(row)
    out["lines"] = [dict(r) for r in await conn.fetch(LINES_SQL, row["id"])]
    return jsonable(out)


async def list_orders(
    conn: asyncpg.Connection,
    staff: Staff,
    *,
    status: str | None = None,
    supplier: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    _need_pricing(staff)
    if status not in (None, "draft", "sending", "sent", "cancelled", "open"):
        raise Invalid("unknown_status", "status is draft, sending, sent, cancelled or open")
    if not 1 <= limit <= MAX_PAGE or offset < 0:
        raise Invalid("invalid_page", f"limit is 1..{MAX_PAGE} and offset >= 0")
    supplier_id = _uuid(supplier) if supplier else None
    if supplier and supplier_id is None:
        raise Invalid("invalid_filter", "supplier is a supplier id")
    where = (
        "($1::text IS NULL OR o.status = $1 OR ($1 = 'open' AND o.status <> 'cancelled'))"
        " AND ($2::uuid IS NULL OR o.supplier_id = $2::uuid)"
    )
    total = await conn.fetchval(f"SELECT count(*) {ORDER_FROM} WHERE {where}", status, supplier_id)  # noqa: S608
    rows = await conn.fetch(
        f"SELECT {ORDER_COLUMNS} {ORDER_FROM} WHERE {where}"  # noqa: S608 — constants only
        " ORDER BY o.created_at DESC, o.id LIMIT $3 OFFSET $4",
        status,
        supplier_id,
        limit,
        offset,
    )
    items = []
    for r in rows:
        item = order_out(r)
        item.pop("snapshot", None)
        items.append(item)
    return jsonable({"items": items, "total": int(total or 0), "limit": limit, "offset": offset})


# ── Writes ───────────────────────────────────────────────────────────────────


async def create_orders(
    conn: asyncpg.Connection,
    staff: Staff,
    *,
    trip_cases: list[str] | None = None,
    departures: list[str] | None = None,
) -> dict[str, Any]:
    """Group the pending bookings of these trip cases (codes or ids) and departures (ids) that are on no live order
    into draft orders, one per supplier + currency + service month (joining the supplier's draft of that group)."""
    _need_pricing(staff)
    cases = [c.strip().upper() for c in (trip_cases or []) if c and c.strip()]
    deps = [_uuid(d) for d in (departures or []) if d]
    if None in deps:
        raise Invalid("invalid_departure", "departures are departure ids")
    if not cases and not deps:
        raise Invalid("scope_required", "pick at least one trip case or departure")
    if len(cases) + len(deps) > MAX_SCOPE:
        raise Invalid("scope_too_large", f"at most {MAX_SCOPE} trip cases and departures at once")
    rows = await conn.fetch(
        "SELECT b.id::text AS id, b.code, b.supplier_id::text AS supplier_id, b.net_amount,"
        " b.net_currency::text AS net_currency, b.service_date"
        " FROM booking.supplier_bookings b"
        " LEFT JOIN booking.trip_cases tc ON tc.id = b.trip_case_id"
        " LEFT JOIN booking.supplier_orders o ON o.id = b.supplier_order_id"
        " WHERE b.status = 'pending' AND (b.supplier_order_id IS NULL OR o.status = 'cancelled')"
        " AND ((tc.deleted_at IS NULL AND (tc.code = ANY($1::text[]) OR tc.id::text = ANY($1::text[])))"
        "      OR b.departure_id = ANY($2::uuid[]))"
        " ORDER BY b.supplier_id, b.service_date, b.code FOR UPDATE OF b",
        cases,
        deps,
    )
    skipped = {"no_net": 0, "no_date": 0}
    groups: dict[tuple[str, str, date], list[Any]] = {}
    for r in rows:
        if r["net_amount"] is None or not r["net_currency"]:
            skipped["no_net"] += 1
            continue
        if r["service_date"] is None:
            skipped["no_date"] += 1
            continue
        key = (r["supplier_id"], r["net_currency"].strip(), service_month(r["service_date"]))
        groups.setdefault(key, []).append(r)
    orders = []
    for (supplier_id, currency, month), lines in sorted(groups.items()):
        order_id = await conn.fetchval(
            "SELECT id::text FROM booking.supplier_orders WHERE supplier_id = $1::uuid AND currency = $2"
            " AND service_month = $3 AND status = 'draft' ORDER BY created_at LIMIT 1 FOR UPDATE",
            supplier_id,
            currency,
            month,
        )
        created = order_id is None
        if created:
            order_id = await conn.fetchval(
                "INSERT INTO booking.supplier_orders (supplier_id, currency, service_month, created_by, updated_by)"
                " VALUES ($1::uuid, $2, $3, $4, $4) RETURNING id::text",
                supplier_id,
                currency,
                month,
                staff.cis_user_id,
            )
        try:
            await conn.execute(
                "UPDATE booking.supplier_bookings SET supplier_order_id = $1::uuid, updated_by = $3"
                " WHERE id = ANY($2::uuid[])",
                order_id,
                [x["id"] for x in lines],
                staff.cis_user_id,
            )
        except asyncpg.CheckViolationError as exc:
            raise Conflict("invalid_order_line", str(exc).split("\n")[0]) from exc
        order = await _find(conn, order_id)
        await _audit(
            conn,
            staff.cis_user_id,
            "supplier_order.created" if created else "supplier_order.lines_added",
            order,
            f"{'Created' if created else 'Added to'} {order['code']}: {len(lines)} line(s)"
            f" ({', '.join(x['code'] for x in lines[:10])})",
        )
        orders.append({**order_out(order), "added": len(lines), "created": created})
    for o in orders:
        o.pop("snapshot", None)
    return jsonable({"orders": orders, "lines": sum(len(v) for v in groups.values()), "skipped": skipped})


async def remove_line(conn: asyncpg.Connection, staff: Staff, ref: str, booking_id: str) -> dict[str, Any]:
    """Take a line off a draft order (the booking stays pending, free to join another order)."""
    _need_pricing(staff)
    order = await _find(conn, ref, lock=True)
    if order["status"] != "draft":
        raise Conflict("supplier_order_locked", f"{order['code']} is {order['status']}: its lines are fixed")
    ident = _uuid(booking_id)
    code = await conn.fetchval(
        "UPDATE booking.supplier_bookings SET supplier_order_id = NULL, updated_by = $3"
        " WHERE id = $1::uuid AND supplier_order_id = $2::uuid RETURNING code",
        ident,
        order["id"],
        staff.cis_user_id,
    )
    if code is None:
        raise NotFound("order_line_not_found", f"that booking is not a line of {order['code']}")
    await _audit(conn, staff.cis_user_id, "supplier_order.line_removed", order, f"Removed {code} from {order['code']}")
    return await detail(conn, staff, order["id"])


async def send(conn: asyncpg.Connection, staff: Staff, ref: str, *, note: str | None = None) -> dict[str, Any]:
    """Send a draft order: fix what the supplier receives (lines, total, address, note) and queue the e-mail."""
    _need_pricing(staff)
    order = await _find(conn, ref, lock=True)
    if order["status"] in ("sending", "sent"):
        return await detail(conn, staff, order["id"])  # pressed twice
    if order["status"] != "draft":
        raise Conflict("supplier_order_closed", f"{order['code']} is {order['status']}")
    email = (order["supplier_email"] or "").strip()
    if not email:
        raise Conflict("supplier_no_email", f"{order['supplier_name']} has no contact e-mail: add one first")
    lines = [dict(r) for r in await conn.fetch(LINES_SQL, order["id"])]
    live = [x for x in lines if x["status"] in ("pending", "requested")]
    if not live:
        raise Conflict("supplier_order_empty", f"{order['code']} has no pending or requested line to send")
    total = sum((Decimal(str(x["net_amount"])) for x in live if x["net_amount"] is not None), Decimal(0))
    text_note = (note or "").strip()[:2000] or order["note"]
    snap = {
        "code": order["code"],
        "supplier": {"code": order["supplier_code"], "name": order["supplier_name"]},
        "currency": order["currency"],
        "service_month": order["service_month"].isoformat(),
        "lines": snapshot_lines(live),
        "total_net": str(total),
        "note": text_note,
    }
    await conn.execute(
        "UPDATE booking.supplier_orders SET status = 'sending', snapshot = $2::jsonb, total_net = $3, sent_to = $4,"
        " send_requested_at = now(), sent_by = $5, send_error = NULL, note = $6, updated_by = $5 WHERE id = $1::uuid",
        order["id"],
        json.dumps(snap),
        total,
        email,
        staff.cis_user_id,
        text_note,
    )
    await queue.enqueue(
        conn,
        KIND,
        {"order_id": order["id"]},
        idempotency_key=f"{KIND}:{order['id']}:{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%f')}",
        max_attempts=MAX_ATTEMPTS,
        created_by=f"staff:{staff.cis_user_id}"[:128],
    )
    await _audit(
        conn,
        staff.cis_user_id,
        "supplier_order.send_requested",
        order,
        f"Send {order['code']} to {order['supplier_name']}: {len(live)} line(s), {total} {order['currency']}",
        after={"lines": len(live), "total_net": total},
    )
    return await detail(conn, staff, order["id"])


async def cancel(conn: asyncpg.Connection, staff: Staff, ref: str, reason: str) -> dict[str, Any]:
    """Cancel an order (final, reason). Its pending lines leave it; requested / confirmed lines stay with the supplier
    (cancel each with the supplier, then on its booking)."""
    _need_pricing(staff)
    reason = (reason or "").strip()
    if not reason:
        raise Invalid("reason_required", "say why the order is cancelled")
    order = await _find(conn, ref, lock=True)
    if order["status"] == "cancelled":
        return await detail(conn, staff, order["id"])
    await conn.execute(
        "UPDATE booking.supplier_orders SET status = 'cancelled', cancelled_at = now(), cancelled_by = $2,"
        " cancel_reason = $3, updated_by = $2 WHERE id = $1::uuid",
        order["id"],
        staff.cis_user_id,
        reason[:500],
    )
    freed = await conn.fetch(
        "UPDATE booking.supplier_bookings SET supplier_order_id = NULL, updated_by = $2"
        " WHERE supplier_order_id = $1::uuid AND status = 'pending' RETURNING code",
        order["id"],
        staff.cis_user_id,
    )
    await _audit(
        conn,
        staff.cis_user_id,
        "supplier_order.cancelled",
        order,
        f"Cancelled {order['code']}: {reason[:200]} ({len(freed)} pending line(s) freed)",
        before={"status": order["status"]},
        after={"status": "cancelled"},
    )
    return await detail(conn, staff, order["id"])


# ── The order as the supplier sees it ────────────────────────────────────────


def _view(order: dict[str, Any], lines: list[dict[str, Any]]) -> dict[str, Any]:
    """The content of the e-mail and the printable order: the snapshot once sent, else the live draft lines."""
    snap = order.get("snapshot")
    if isinstance(snap, str):
        snap = json.loads(snap)
    if snap:
        return snap
    live = [x for x in lines if x["status"] in ("pending", "requested")]
    total = sum((Decimal(str(x["net_amount"])) for x in live if x["net_amount"] is not None), Decimal(0))
    return {
        "code": order["code"],
        "supplier": {"code": order["supplier_code"], "name": order["supplier_name"]},
        "currency": order["currency"],
        "service_month": order["service_month"].isoformat(),
        "lines": snapshot_lines(live),
        "total_net": str(total),
        "note": order.get("note"),
    }


def render_html(view: dict[str, Any], *, draft: bool, redirect_note: str | None = None) -> str:
    e = html.escape
    rows = "".join(
        "<tr>"
        + "".join(
            f'<td style="padding:4px 8px;border-bottom:1px solid #ddd">{e(str(v)) if v is not None else ""}</td>'
            for v in (
                ln["code"],
                ln["reference"],
                ln["service"],
                ln["service_date"],
                ln.get("nights"),
                ln.get("pax"),
                ln["net_amount"],
            )
        )
        + "</tr>"
        for ln in view["lines"]
    )
    head = "".join(
        f'<th style="text-align:left;padding:4px 8px;border-bottom:2px solid #333">{e(h)}</th>'
        for h in ("Line", "AA reference", "Service", "Date", "Nights", "Pax", f"Net ({view['currency']})")
    )
    note = view.get("note")
    return (
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#222">'
        + (f'<p style="color:#b45309">{e(redirect_note)}</p>' if redirect_note else "")
        + ('<p style="color:#b45309">Draft, not sent yet.</p>' if draft else "")
        + f"<h2>Purchase order {e(view['code'])}</h2>"
        + f"<p>To: {e(view['supplier']['name'])} ({e(view['supplier']['code'])})<br>"
        + f"Service month: {e(view['service_month'][:7])}</p>"
        + f'<table style="border-collapse:collapse"><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>'
        + f"<p><strong>Total net: {e(view['total_net'])} {e(view['currency'])}</strong></p>"
        + (f'<p style="white-space:pre-line">{e(note)}</p>' if note else "")
        + "<p>Please confirm each line with your booking reference, quoting the line code.</p>"
        + '<p style="color:#888;font-size:12px">Adventure Asia, automatic message.</p></div>'
    )


def render_text(view: dict[str, Any], *, redirect_note: str | None = None) -> str:
    out = [*([redirect_note, ""] if redirect_note else [])]
    out += [f"Purchase order {view['code']}", f"Service month: {view['service_month'][:7]}", ""]
    for ln in view["lines"]:
        parts = (f"{ln['nights']} night(s)" if ln.get("nights") else "", f"{ln['pax']} pax" if ln.get("pax") else "")
        extra = " ".join(p for p in parts if p)
        out.append(
            f"{ln['code']} | {ln['reference']} | {ln['service']} | {ln['service_date']} | {extra} | {ln['net_amount']}"
            f" {view['currency']}"
        )
    out += ["", f"Total net: {view['total_net']} {view['currency']}"]
    if view.get("note"):
        out += ["", view["note"]]
    out += ["", "Please confirm each line with your booking reference, quoting the line code.", "", "-- ",
            "Adventure Asia, automatic message."]  # fmt: skip
    return "\n".join(out)


async def document(conn: asyncpg.Connection, staff: Staff, ref: str) -> str:
    """The printable order (HTML): what was sent, or the draft as it stands."""
    _need_pricing(staff)
    row = await _find(conn, ref)
    order = dict(row)
    lines = [dict(r) for r in await conn.fetch(LINES_SQL, row["id"])]
    body = render_html(_view(order, lines), draft=order["status"] == "draft")
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f"<title>{html.escape(order['code'])}</title></head><body>{body}</body></html>"
    )


# ── The send job ─────────────────────────────────────────────────────────────


async def _mark_requested(conn: asyncpg.Connection, order: Any) -> int:
    """The order left: its pending lines become requested (requester = who pressed Send); cases advance."""
    lines = await conn.fetch(
        "SELECT b.id::text AS id, b.code, b.trip_case_id::text AS trip_case_id FROM booking.supplier_bookings b"
        " WHERE b.supplier_order_id = $1::uuid AND b.status = 'pending' ORDER BY b.code",
        order["id"],
    )
    case_ids = sorted({x["trip_case_id"] for x in lines if x["trip_case_id"]})
    cases = {}
    for case_id in case_ids:  # lock order: case, then booking (as app.ops.supplier_bookings)
        cases[case_id] = await conn.fetchrow(
            "SELECT id::text AS id, code, status, channel FROM booking.trip_cases WHERE id = $1::uuid FOR UPDATE",
            case_id,
        )
    for line in lines:
        await conn.execute(
            "UPDATE booking.supplier_bookings SET status = 'requested', requested_at = now(), requested_by = $2,"
            " updated_by = $2 WHERE id = $1::uuid AND status = 'pending'",
            line["id"],
            order["sent_by"],
        )
        await audit.record(
            conn,
            actor_type="system",
            actor_ref=ACTOR,
            action="supplier_booking.requested",
            entity_type="supplier_booking",
            entity_id=line["id"],
            entity_label=line["code"],
            summary=f"Requested {line['code']} with purchase order {order['code']}",
            before={"status": "pending"},
            after={"status": "requested", "supplier_order": order["code"]},
        )
    for case in cases.values():
        await track.advance_case(conn, case, cause=f"purchase order {order['code']} sent")
    return len(lines)


async def run_send(
    pool: asyncpg.Pool, settings: Settings, send_mail: Callable[[mailer.Message], Any], order_id: str
) -> dict[str, Any]:
    """Send one order (status ``sending``). A refused e-mail puts it back to ``draft`` with the reason."""
    prod = settings.environment == "prod"
    async with pool.acquire() as conn:
        row = await _find(conn, order_id)
    if row["status"] != "sending":
        return {"outcome": "skipped", "reason": f"order is {row['status']}"}
    order = dict(row)
    reason = None
    if not settings.ses_from_address:
        reason = "SES_FROM_ADDRESS not set"
    elif not prod and not settings.staff_notify_email:
        reason = "STAFF_NOTIFY_EMAIL not set (orders go to staff outside prod)"
    if reason is None:
        to = order["sent_to"] if prod else settings.staff_notify_email
        note = (
            None
            if prod
            else f"Test copy: on {settings.environment} this order goes to the staff inbox, not to {order['sent_to']}."
        )
        view = _view(order, [])
        message = mailer.Message(
            to=to,
            subject=(f"{'' if prod else f'[{settings.environment}] '}Purchase order {order['code']}")[:200],
            text=render_text(view, redirect_note=note),
            html=render_html(view, draft=False, redirect_note=note),
        )
        try:
            message_id = str(await send_mail(message))[:200]
        except mailer.MailError as exc:
            if not exc.permanent:
                raise  # throttling / network: the runner retries
            reason = f"e-mail refused: {exc.code}"
    async with pool.acquire() as conn, conn.transaction():
        locked = await _find(conn, order["id"], lock=True)
        if locked["status"] != "sending":
            return {"outcome": "skipped", "reason": f"order is {locked['status']}"}
        if reason is not None:
            await conn.execute(
                "UPDATE booking.supplier_orders SET status = 'draft', send_requested_at = NULL, sent_by = NULL,"
                " snapshot = NULL, total_net = NULL, sent_to = NULL, send_error = $2, updated_by = $3"
                " WHERE id = $1::uuid",
                order["id"],
                reason[:500],
                ACTOR,
            )
            await _audit(conn, ACTOR, "supplier_order.send_failed", locked, f"Not sent: {reason}")
            log.warning("%s %s not sent: %s", KIND, order["code"], reason)
            return {"outcome": "not_sent", "reason": reason, "order": order["code"]}
        await conn.execute(
            "UPDATE booking.supplier_orders SET status = 'sent', sent_at = now(), delivery_ref = $2, redirected = $3,"
            " updated_by = $4 WHERE id = $1::uuid",
            order["id"],
            message_id,
            not prod,
            ACTOR,
        )
        requested = await _mark_requested(conn, locked)
        await _audit(
            conn,
            ACTOR,
            "supplier_order.sent",
            locked,
            f"Sent {order['code']} to {order['supplier_name']}{' (staff inbox)' if not prod else ''};"
            f" {requested} line(s) requested",
            after={"delivery_ref": message_id, "redirected": not prod, "requested": requested},
        )
    return {"outcome": "sent", "order": order["code"], "requested": requested, "redirected": not prod}


def build_handler(settings: Settings, client_factory: Callable[[str], mailer.SesClient] = mailer.ses_client) -> Handler:
    """The ``bk_supplier_order_send`` handler bound to settings (the SES client is made on the first send)."""
    clients: list[mailer.SesClient] = []

    async def send_mail(message: mailer.Message) -> str:
        if not clients:
            clients.append(client_factory(settings.ses_region))
        return await mailer.send(
            clients[0],
            message,
            sender=settings.ses_from_address,
            configuration_set=settings.ses_configuration_set or None,
        )

    async def handle(pool: asyncpg.Pool, job: Job) -> dict[str, Any]:
        order_id = _uuid((job.payload or {}).get("order_id"))
        if order_id is None:
            raise PermanentError("payload.order_id is not an order id")
        return await run_send(pool, settings, send_mail, order_id)

    return handle
