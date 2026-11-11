"""Departure shared costs (AA-1074, bk-045; S275 study item bk-05) and the departure cost view (bk-07).

A **departure cost** (``booking.departure_costs``) is a cost the whole departure shares: a coach, a guide, a group
entrance. It names a supplier (and usually a service), a service day and how it is priced:

- ``rate``: from the supplier rate in force on the service day for the departure's **sold seats** (the pax band;
  ``app.supply.rates.rates_in_force_many``). The rate's unit is the basis (``group`` / ``vehicle`` / ``day`` /
  ``person``); ``net = unit x quantity`` (x the sold seats for ``person``). Re-priced by the job
  ``bk_departure_cost_recompute`` whenever the sold seats move (a hold confirmed or cancelled: the watermark of
  :class:`RecomputeSchedule`), until the cost is frozen.
- ``manual``: an amount staff enter (no rate, e.g. a one-off quote from the supplier); never re-priced.

**Ordering and freezing.** *Order* makes one departure-level supplier booking (pending; no trip case) from the cost,
so it can join a supplier purchase order (:mod:`app.finance.supplier_orders`) or be requested by hand. While the
booking is pending a re-price also updates its net and pax. When the booking is **requested** the cost freezes
(bk-045 trigger): the price the supplier was asked for is fixed from then. Remove a cost with a reason (final); an
ordered one only while its booking is pending (the booking is cancelled with it).

**Split to trip cases**: ``booking.v_departure_cost_share`` (net x seats / sold seats, half up to the cent per case;
the remainder stays with the departure). The departure cost view (:func:`departure_costs`) shows the shared costs,
each sold case's own costs and share, the bills of the departure, planned against actual in USD.

Permissions: reads ``bill.read`` + ``pricing.read`` (net amounts); writes ``ops.write`` + ``pricing.read``.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import asyncpg

from app import audit
from app.auth.staff import Staff
from app.b2b import departures as b2b_departures
from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.jobs import queue
from app.jobs.queue import Job
from app.ops.supplier_bills import jsonable
from app.supply.rates import RateAsk, rates_in_force_many

log = logging.getLogger(__name__)

KIND = "bk_departure_cost_recompute"
ACTOR = "system:bk_departure_cost_recompute"
SCHEDULER = "system:departure_cost_recompute"
MAX_ATTEMPTS = 3
BASES = ("group", "vehicle", "day", "person")
PRICING = ("rate", "manual")
CENT = Decimal("0.01")
MAX_QUANTITY = 999

COST_SQL = """
SELECT dc.id::text AS id, dc.departure_id::text AS departure_id, dc.supplier_id::text AS supplier_id,
       s.code AS supplier_code, s.company_name AS supplier_name, dc.service_id::text AS service_id,
       sv.code AS service_code, sv.title AS service_title, dc.supplier_rate_id::text AS supplier_rate_id,
       dc.item_description, dc.service_date, dc.basis, dc.quantity, dc.pricing, dc.unit_amount, dc.net_amount,
       dc.currency::text AS currency, dc.priced_for_pax, dc.priced_at, dc.frozen_at,
       dc.supplier_booking_id::text AS supplier_booking_id, sb.code AS supplier_booking_code,
       sb.status AS supplier_booking_status, so.code AS supplier_order_code, dc.status, dc.cancelled_at,
       dc.cancel_reason, dc.notes, dc.created_by, dc.created_at, dc.updated_at,
       cl.planned_usd, cl.billed, cl.billed_usd, cl.cost_usd
  FROM booking.departure_costs dc
  JOIN booking.suppliers s ON s.id = dc.supplier_id
  LEFT JOIN booking.services sv ON sv.id = dc.service_id
  LEFT JOIN booking.supplier_bookings sb ON sb.id = dc.supplier_booking_id
  LEFT JOIN booking.supplier_orders so ON so.id = sb.supplier_order_id
  LEFT JOIN booking.v_departure_cost_lines cl ON cl.departure_cost_id = dc.id
"""

DEPARTURE_SQL = """
SELECT d.id::text AS id, d.trip_plan_id::text AS trip_plan_id, tp.source_id AS trip_plan_source_id, d.start_date,
       d.capacity, d.status
  FROM booking.departures d JOIN booking.trip_plans tp ON tp.id = d.trip_plan_id
 WHERE d.id = $1::uuid
"""

# The watermark of the re-pricing inputs: the sold seats of every departure that has an active, unfrozen rate cost,
# and the newest rate change (a new rate band re-prices too).
WATERMARK_SQL = """
SELECT concat_ws('|',
    (SELECT string_agg(x.departure_id::text || ':' || x.sold, ',' ORDER BY x.departure_id)
       FROM (SELECT dc.departure_id,
                    coalesce((SELECT sum(l.seats) FROM booking.b2b_hold_lines l
                                JOIN booking.b2b_holds h ON h.id = l.hold_id
                               WHERE l.departure_id = dc.departure_id AND h.status = 'confirmed'
                                 AND l.status NOT IN ('declined', 'cancelled')), 0) AS sold
               FROM booking.departure_costs dc
              WHERE dc.status = 'active' AND dc.frozen_at IS NULL AND dc.pricing = 'rate'
              GROUP BY dc.departure_id) x),
    (SELECT count(*) || ':' || coalesce(max(updated_at)::text, '') FROM booking.supplier_rates))
"""


# ── Pure helpers ─────────────────────────────────────────────────────────────


def price(unit_amount: Decimal | str, *, basis: str, quantity: int, pax: int) -> Decimal:
    """Net of a shared cost: unit x quantity, x the seats for a per-person basis; half up to the cent."""
    if basis not in BASES:
        raise ValueError(f"basis must be one of {BASES}")
    unit = Decimal(str(unit_amount))
    per = max(pax, 0) if basis == "person" else 1
    return (unit * quantity * per).quantize(CENT, rounding=ROUND_HALF_UP)


def _uuid(value: Any) -> str | None:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        return None


def _need_pricing(staff: Staff) -> None:
    if not staff.can("pricing.read"):
        raise Forbidden("forbidden", "departure costs are net amounts: they need pricing.read")


def _text(value: Any, limit: int) -> str | None:
    text = str(value or "").strip()
    return text[:limit] if text else None


async def _departure(conn: asyncpg.Connection, ref: str, *, lock: bool = False) -> Any:
    ident = _uuid(ref)
    row = await conn.fetchrow(DEPARTURE_SQL, ident) if ident else None
    if row is None:
        raise NotFound("departure_not_found", f"departure {ref} not found")
    if lock:
        await conn.execute("SELECT 1 FROM booking.departures WHERE id = $1::uuid FOR UPDATE", row["id"])
    return row


async def sold_seats(conn: asyncpg.Connection, departure_id: str) -> int:
    """Seats of confirmed holds on the departure (line not declined / cancelled), the rule of app.b2b.departures."""
    return (await b2b_departures.usage(conn, [departure_id])).get(departure_id, (0, 0))[1]


async def _cost(conn: asyncpg.Connection, cost_id: str, *, lock: bool = False) -> Any:
    ident = _uuid(cost_id)
    if ident is None:
        raise NotFound("departure_cost_not_found", f"departure cost {cost_id} not found")
    if lock:
        dep = await conn.fetchval("SELECT departure_id::text FROM booking.departure_costs WHERE id = $1::uuid", ident)
        if dep is None:
            raise NotFound("departure_cost_not_found", f"departure cost {cost_id} not found")
        await conn.execute("SELECT 1 FROM booking.departures WHERE id = $1::uuid FOR UPDATE", dep)
        await conn.execute("SELECT 1 FROM booking.departure_costs WHERE id = $1::uuid FOR UPDATE", ident)
    row = await conn.fetchrow(COST_SQL + " WHERE dc.id = $1::uuid", ident)  # noqa: S608 — constants only
    if row is None:
        raise NotFound("departure_cost_not_found", f"departure cost {cost_id} not found")
    return row


def cost_out(row: Any) -> dict[str, Any]:
    out = dict(row)
    out["supplier"] = {"id": out.pop("supplier_id"), "code": out.pop("supplier_code"), "name": out.pop("supplier_name")}
    service_id, code, title = out.pop("service_id"), out.pop("service_code"), out.pop("service_title")
    out["service"] = {"id": service_id, "code": code, "title": title} if service_id else None
    booking_id, b_code, b_status, o_code = (
        out.pop("supplier_booking_id"),
        out.pop("supplier_booking_code"),
        out.pop("supplier_booking_status"),
        out.pop("supplier_order_code"),
    )
    out["supplier_booking"] = (
        {"id": booking_id, "code": b_code, "status": b_status, "order_code": o_code} if booking_id else None
    )
    live = out["status"] == "active"
    out["actions"] = {
        "edit": live and out["frozen_at"] is None,
        "order": live and booking_id is None,
        "remove": live and (booking_id is None or b_status == "pending"),
    }
    return out


async def _audit(conn: asyncpg.Connection, actor: str, action: str, row: Any, summary: str, **kw: Any) -> None:
    await audit.record(
        conn,
        actor_type="system" if actor.startswith("system:") else "staff",
        actor_ref=actor,
        action=action,
        entity_type="departure_cost",
        entity_id=row["id"],
        entity_label=f"Departure {row['departure_id'][:8]} · {row['item_description'][:60]}",
        summary=summary[:500],
        before=jsonable(kw.get("before")),
        after=jsonable(kw.get("after")),
    )


async def _rate(conn: asyncpg.Connection, service_id: str, on: date, pax: int) -> dict[str, Any] | None:
    """The rate in force for the service on the day and the seats (the pax band; 0 sold = the smallest band, 1)."""
    found = (await rates_in_force_many(conn, [RateAsk(None, service_id, on, max(pax, 1))]))[0]
    return found


async def _service(conn: asyncpg.Connection, service_id: str) -> Any:
    row = await conn.fetchrow(
        "SELECT id::text AS id, supplier_id::text AS supplier_id, title FROM booking.services WHERE id = $1::uuid",
        service_id,
    )
    if row is None:
        raise NotFound("service_not_found", f"service {service_id} not found")
    return row


# ── Writes ───────────────────────────────────────────────────────────────────


async def create(conn: asyncpg.Connection, staff: Staff, departure_ref: str, data: dict[str, Any]) -> dict[str, Any]:
    """Add a shared cost to a departure: ``pricing`` rate (``service_id``; the rate band for the sold seats) or manual
    (``supplier_id``, ``basis``, ``unit_amount``, ``currency``)."""
    _need_pricing(staff)
    dep = await _departure(conn, departure_ref, lock=True)
    if dep["status"] == "cancelled":
        raise Conflict("departure_cancelled", "this departure is cancelled: it takes no new cost")
    pricing = data.get("pricing") or ("rate" if data.get("service_id") else "manual")
    if pricing not in PRICING:
        raise Invalid("invalid_pricing", f"pricing must be one of {', '.join(PRICING)}")
    service_date = data.get("service_date") or dep["start_date"]
    if not isinstance(service_date, date):
        raise Invalid("invalid_date", "service_date is a date")
    quantity = int(data.get("quantity") or 1)
    if not 1 <= quantity <= MAX_QUANTITY:
        raise Invalid("invalid_quantity", f"quantity is 1..{MAX_QUANTITY}")
    sold = await sold_seats(conn, dep["id"])
    service = await _service(conn, str(data["service_id"])) if data.get("service_id") else None
    supplier_id = service["supplier_id"] if service else _uuid(data.get("supplier_id"))
    if supplier_id is None:
        raise Invalid("supplier_required", "name the supplier (or a service of it)")
    if service and data.get("supplier_id") and _uuid(data.get("supplier_id")) != service["supplier_id"]:
        raise Invalid("service_supplier_mismatch", "the service is of another supplier")
    if pricing == "rate":
        if service is None:
            raise Invalid("service_required", "a rate-priced cost names a service")
        rate = await _rate(conn, service["id"], service_date, sold)
        if rate is None:
            raise Conflict("no_rate_in_force", "the service has no rate in force on that day for the sold seats")
        if rate["unit"] not in BASES:
            raise Conflict("rate_not_shared", f"the rate is priced per {rate['unit']}: not a shared cost")
        basis, unit_amount, currency, rate_id = rate["unit"], Decimal(rate["net_amount"]), rate["currency"], rate["id"]
    else:
        basis = data.get("basis") or "group"
        if basis not in BASES:
            raise Invalid("invalid_basis", f"basis must be one of {', '.join(BASES)}")
        currency = str(data.get("currency") or "").strip().upper()
        if len(currency) != 3 or not currency.isalpha():
            raise Invalid("invalid_currency", "currency is a 3-letter ISO code")
        try:
            unit_amount = Decimal(str(data.get("unit_amount")))
        except ArithmeticError as exc:
            raise Invalid("invalid_amount", "unit_amount is an amount") from exc
        if not unit_amount.is_finite() or unit_amount < 0 or unit_amount != unit_amount.quantize(CENT):
            raise Invalid("invalid_amount", "unit_amount is >= 0 with at most 2 decimals")
        rate_id = None
    description = _text(data.get("item_description"), 500) or (service["title"] if service else None)
    if not description:
        raise Invalid("description_required", "say what the cost is")
    net = price(unit_amount, basis=basis, quantity=quantity, pax=sold)
    try:
        cost_id = await conn.fetchval(
            "INSERT INTO booking.departure_costs (departure_id, supplier_id, service_id, supplier_rate_id,"
            " item_description, service_date, basis, quantity, pricing, unit_amount, net_amount, currency,"
            " priced_for_pax, notes, created_by, updated_by)"
            " VALUES ($1::uuid, $2::uuid, $3::uuid, $4::uuid, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $15)"
            " RETURNING id::text",
            dep["id"],
            supplier_id,
            service["id"] if service else None,
            rate_id,
            description,
            service_date,
            basis,
            quantity,
            pricing,
            unit_amount,
            net,
            currency,
            sold,
            _text(data.get("notes"), 2000),
            staff.cis_user_id,
        )
    except asyncpg.ForeignKeyViolationError as exc:
        raise NotFound("supplier_not_found", "supplier not found") from exc
    except asyncpg.CheckViolationError as exc:
        raise Conflict("invalid_departure_cost", str(exc).split("\n")[0]) from exc
    row = await _cost(conn, cost_id)
    await _audit(
        conn,
        staff.cis_user_id,
        "departure_cost.created",
        row,
        f"Added {description[:80]}: {net} {currency} for {sold} sold seat(s) ({pricing})",
        after={"net_amount": net, "currency": currency, "basis": basis, "quantity": quantity, "pricing": pricing},
    )
    return jsonable(cost_out(row))


async def update(conn: asyncpg.Connection, staff: Staff, cost_id: str, changes: dict[str, Any]) -> dict[str, Any]:
    """Edit an unfrozen cost: quantity, service day, description, notes; a manual one also its unit amount, basis and
    currency. Re-prices at once (a rate cost re-reads the rate band); a pending booking behind it follows."""
    _need_pricing(staff)
    row = await _cost(conn, cost_id, lock=True)
    if row["status"] != "active":
        raise Conflict("departure_cost_closed", "this cost was removed")
    facts = {k: v for k, v in changes.items() if k in ("quantity", "service_date", "unit_amount", "basis", "currency")}
    if row["frozen_at"] is not None and facts:
        raise Conflict("departure_cost_frozen", "ordered from the supplier: its money facts are fixed")
    if row["pricing"] == "rate" and {"unit_amount", "basis", "currency"} & set(facts):
        raise Invalid("rate_priced", "a rate-priced cost takes its amount from the rate")
    new = dict(row)
    if "item_description" in changes:
        new["item_description"] = _text(changes["item_description"], 500)
        if not new["item_description"]:
            raise Invalid("description_required", "say what the cost is")
    if "notes" in changes:
        new["notes"] = _text(changes["notes"], 2000)
    if "quantity" in facts:
        new["quantity"] = int(facts["quantity"])
        if not 1 <= new["quantity"] <= MAX_QUANTITY:
            raise Invalid("invalid_quantity", f"quantity is 1..{MAX_QUANTITY}")
    if "service_date" in facts:
        new["service_date"] = facts["service_date"]
    if "basis" in facts:
        if facts["basis"] not in BASES:
            raise Invalid("invalid_basis", f"basis must be one of {', '.join(BASES)}")
        new["basis"] = facts["basis"]
    if "currency" in facts:
        new["currency"] = str(facts["currency"] or "").strip().upper()
    if "unit_amount" in facts:
        new["unit_amount"] = Decimal(str(facts["unit_amount"]))
        if new["unit_amount"] < 0 or new["unit_amount"] != new["unit_amount"].quantize(CENT):
            raise Invalid("invalid_amount", "unit_amount is >= 0 with at most 2 decimals")
    changed = await _reprice(conn, row, new, actor=staff.cis_user_id)
    fresh = await _cost(conn, row["id"])
    if changed:
        await _audit(
            conn,
            staff.cis_user_id,
            "departure_cost.updated",
            fresh,
            f"Edited {fresh['item_description'][:80]}: {', '.join(changed)}",
            before={k: row[k] for k in changed},
            after={k: fresh[k] for k in changed},
        )
    return jsonable({**cost_out(fresh), "changed": changed})


EDITABLE = (
    "item_description",
    "notes",
    "service_date",
    "quantity",
    "basis",
    "currency",
    "unit_amount",
    "supplier_rate_id",
    "net_amount",
    "priced_for_pax",
)


async def _reprice(conn: asyncpg.Connection, row: Any, new: dict[str, Any], *, actor: str) -> list[str]:
    """Write ``new`` (re-priced: a rate cost re-reads its band for the sold seats) when it differs from ``row``;
    a pending booking behind the cost follows (net, pax, day, description). Returns the changed columns."""
    sold = await sold_seats(conn, row["departure_id"])
    if row["frozen_at"] is None:
        if row["pricing"] == "rate":
            rate = await _rate(conn, row["service_id"], new["service_date"], sold) if row["service_id"] else None
            if rate is not None and rate["unit"] in BASES:
                new["supplier_rate_id"] = rate["id"]
                new["unit_amount"] = Decimal(rate["net_amount"])
                new["currency"] = rate["currency"]
                new["basis"] = rate["unit"]
        new["priced_for_pax"] = sold
        new["net_amount"] = price(new["unit_amount"], basis=new["basis"], quantity=new["quantity"], pax=sold)
    changed = [k for k in EDITABLE if str(new.get(k)) != str(row[k])]
    if not changed:
        return []
    sets = ", ".join(f"{k} = ${i + 3}{'::uuid' if k == 'supplier_rate_id' else ''}" for i, k in enumerate(changed))
    try:
        await conn.execute(
            f"UPDATE booking.departure_costs SET {sets}, updated_by = $2, priced_at = now() WHERE id = $1::uuid",  # noqa: S608
            row["id"],
            actor,
            *[new[k] for k in changed],
        )
    except asyncpg.CheckViolationError as exc:
        raise Conflict("invalid_departure_cost", str(exc).split("\n")[0]) from exc
    if row["supplier_booking_id"] and row["supplier_booking_status"] == "pending":
        await conn.execute(
            "UPDATE booking.supplier_bookings SET net_amount = $2, net_currency = $3, pax = $4, service_date = $5,"
            " item_description = $6, updated_by = $7 WHERE id = $1::uuid AND status = 'pending'",
            row["supplier_booking_id"],
            new["net_amount"],
            new["currency"],
            _booking_pax(new["priced_for_pax"]),
            new["service_date"],
            new["item_description"],
            actor,
        )
    return changed


def _booking_pax(seats: int) -> int | None:
    return seats if 1 <= seats <= 120 else None


async def remove(conn: asyncpg.Connection, staff: Staff, cost_id: str, reason: str) -> dict[str, Any]:
    """Remove a cost (final, with a reason). An ordered one only while its booking is pending (cancelled with it)."""
    _need_pricing(staff)
    reason = (reason or "").strip()
    if not reason:
        raise Invalid("reason_required", "say why the cost is removed")
    row = await _cost(conn, cost_id, lock=True)
    if row["status"] == "cancelled":
        return jsonable(cost_out(row))
    if row["supplier_booking_id"] and row["supplier_booking_status"] not in ("pending", "cancelled", "declined"):
        raise Conflict(
            "departure_cost_ordered",
            f"{row['supplier_booking_code']} is {row['supplier_booking_status']}: cancel it with the supplier first",
        )
    if row["supplier_booking_id"] and row["supplier_booking_status"] == "pending":
        await conn.execute(
            "UPDATE booking.supplier_bookings SET status = 'cancelled', cancel_reason = $2, cancelled_at = now(),"
            " updated_by = $3 WHERE id = $1::uuid AND status = 'pending'",
            row["supplier_booking_id"],
            f"Departure cost removed: {reason}"[:500],
            staff.cis_user_id,
        )
    await conn.execute(
        "UPDATE booking.departure_costs SET status = 'cancelled', cancelled_at = now(), cancelled_by = $2,"
        " cancel_reason = $3, updated_by = $2 WHERE id = $1::uuid",
        row["id"],
        staff.cis_user_id,
        reason[:500],
    )
    fresh = await _cost(conn, row["id"])
    await _audit(conn, staff.cis_user_id, "departure_cost.removed", fresh, f"Removed: {reason[:200]}")
    return jsonable(cost_out(fresh))


async def order(conn: asyncpg.Connection, staff: Staff, cost_id: str) -> dict[str, Any]:
    """Make the departure-level supplier booking (pending) of a cost, once. It can then join a purchase order."""
    _need_pricing(staff)
    row = await _cost(conn, cost_id, lock=True)
    if row["status"] != "active":
        raise Conflict("departure_cost_closed", "this cost was removed")
    if row["supplier_booking_id"]:
        return jsonable(cost_out(row))
    try:
        booking_id = await conn.fetchval(
            "INSERT INTO booking.supplier_bookings (departure_id, supplier_id, service_id, item_description,"
            " service_date, pax, net_amount, net_currency, created_by, updated_by)"
            " VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5, $6, $7, $8, $9, $9) RETURNING id::text",
            row["departure_id"],
            row["supplier_id"],
            row["service_id"],
            row["item_description"],
            row["service_date"],
            _booking_pax(row["priced_for_pax"]),
            row["net_amount"],
            row["currency"],
            staff.cis_user_id,
        )
    except asyncpg.CheckViolationError as exc:
        raise Conflict("invalid_supplier_booking", str(exc).split("\n")[0]) from exc
    await conn.execute(
        "UPDATE booking.departure_costs SET supplier_booking_id = $2::uuid, updated_by = $3 WHERE id = $1::uuid",
        row["id"],
        booking_id,
        staff.cis_user_id,
    )
    fresh = await _cost(conn, row["id"])
    await _audit(
        conn,
        staff.cis_user_id,
        "departure_cost.ordered",
        fresh,
        f"Supplier booking {fresh['supplier_booking_code']} made for {fresh['item_description'][:80]}",
    )
    return jsonable(cost_out(fresh))


# ── Reads ────────────────────────────────────────────────────────────────────


def _sum(values: list[Any]) -> Decimal:
    return sum((Decimal(str(v)) for v in values if v is not None), Decimal(0))


async def departure_costs(conn: asyncpg.Connection, staff: Staff, departure_ref: str) -> dict[str, Any]:
    """The departure's costs, planned against actual in USD: its shared costs (each with its booking's billed amount),
    every sold trip case (revenue, own costs, share), the bills of the departure, and the profit."""
    _need_pricing(staff)
    dep = await _departure(conn, departure_ref)
    costs = await conn.fetch(
        COST_SQL + " WHERE dc.departure_id = $1::uuid ORDER BY dc.status, dc.service_date, dc.created_at",  # noqa: S608
        dep["id"],
    )
    cases = await conn.fetch(
        "SELECT p.trip_case_id::text AS trip_case_id, p.code, p.status, p.partner_id::text AS partner_id,"
        " bp.agency_name, p.currency, p.revenue, p.revenue_usd, p.planned_cost_usd, p.actual_cost_usd,"
        " p.shared_cost_usd, p.cost_usd, p.profit_usd, p.margin, p.unbilled_bookings, p.fx_missing, p.cancelled,"
        " coalesce((SELECT sum(s.seats) FROM booking.v_departure_cost_share s WHERE s.trip_case_id = p.trip_case_id"
        "           AND s.departure_id = $1::uuid), 0)::int AS seats"
        " FROM booking.v_case_profit p LEFT JOIN booking.b2b_partners bp ON bp.id = p.partner_id"
        " WHERE p.departure_id = $1::uuid ORDER BY p.code",
        dep["id"],
    )
    bills = await conn.fetch(
        "SELECT b.id::text AS id, b.code, b.status, b.currency::text AS currency, b.amount, b.bill_number,"
        " s.company_name AS supplier_name, b.issue_date, b.due_date"
        " FROM booking.supplier_bills b JOIN booking.suppliers s ON s.id = b.supplier_id"
        " WHERE b.departure_id = $1::uuid AND b.status <> 'cancelled' ORDER BY b.issue_date, b.code",
        dep["id"],
    )
    profit = await conn.fetchrow("SELECT * FROM booking.v_departure_profit WHERE departure_id = $1::uuid", dep["id"])
    sold = await sold_seats(conn, dep["id"])
    active = [c for c in costs if c["status"] == "active"]
    return jsonable(
        {
            "departure": {**dict(dep), "sold_seats": sold},
            "shared_costs": [cost_out(c) for c in costs],
            "cases": [dict(c) for c in cases],
            "bills": [dict(b) for b in bills],
            "totals_usd": {
                "shared_planned": _sum([c["planned_usd"] for c in active]),
                "shared_billed": _sum([c["billed_usd"] for c in active if c["billed"]]),
                "shared_cost": _sum([c["cost_usd"] for c in active]),
                "revenue": profit["revenue_usd"] if profit else Decimal(0),
                "case_cost": profit["case_cost_usd"] if profit else Decimal(0),
                "cost": profit["cost_usd"] if profit else Decimal(0),
                "profit": profit["profit_usd"] if profit else Decimal(0),
                "profit_per_seat": profit["profit_per_seat_usd"] if profit else None,
                "margin": profit["margin"] if profit else None,
                "fx_missing": bool(profit["fx_missing"]) if profit else False,
            },
        }
    )


async def list_departures(
    conn: asyncpg.Connection,
    staff: Staff,
    *,
    start: date | None = None,
    end: date | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Departures in a date window with their sold seats, shared costs and profit (USD), soonest first."""
    _need_pricing(staff)
    if not 1 <= limit <= 100 or offset < 0:
        raise Invalid("invalid_page", "limit is 1..100 and offset >= 0")
    where = "($1::date IS NULL OR d.start_date >= $1) AND ($2::date IS NULL OR d.start_date <= $2)"
    total = await conn.fetchval(f"SELECT count(*) FROM booking.departures d WHERE {where}", start, end)  # noqa: S608
    rows = await conn.fetch(
        "SELECT d.id::text AS id, d.start_date, d.status, d.capacity, tp.source_id AS trip_plan_source_id,"  # noqa: S608
        " coalesce(p.sold_seats, 0) AS sold_seats, coalesce(p.cases, 0) AS cases,"
        " coalesce(p.shared_cost_lines, 0) AS shared_cost_lines, p.revenue_usd, p.cost_usd, p.profit_usd,"
        " p.profit_per_seat_usd, p.margin, coalesce(p.fx_missing, false) AS fx_missing"
        " FROM booking.departures d JOIN booking.trip_plans tp ON tp.id = d.trip_plan_id"
        " LEFT JOIN booking.v_departure_profit p ON p.departure_id = d.id"
        f" WHERE {where} ORDER BY d.start_date, d.id LIMIT $3 OFFSET $4",
        start,
        end,
        limit,
        offset,
    )
    return jsonable({"items": [dict(r) for r in rows], "total": int(total or 0), "limit": limit, "offset": offset})


# ── Re-pricing job ───────────────────────────────────────────────────────────


async def recompute_departure(conn: asyncpg.Connection, departure_id: str) -> dict[str, int]:
    """Re-price every active, unfrozen rate cost of a departure for its sold seats (departure row locked)."""
    await conn.execute("SELECT 1 FROM booking.departures WHERE id = $1::uuid FOR UPDATE", departure_id)
    rows = await conn.fetch(
        COST_SQL + " WHERE dc.departure_id = $1::uuid AND dc.status = 'active' AND dc.frozen_at IS NULL"  # noqa: S608
        " AND dc.pricing = 'rate' ORDER BY dc.id FOR UPDATE OF dc",
        departure_id,
    )
    counts = {"costs": len(rows), "repriced": 0, "unchanged": 0}
    for row in rows:
        changed = await _reprice(conn, row, dict(row), actor=ACTOR)
        if changed:
            counts["repriced"] += 1
            fresh = await _cost(conn, row["id"])
            await _audit(
                conn,
                ACTOR,
                "departure_cost.repriced",
                fresh,
                f"Re-priced for {fresh['priced_for_pax']} sold seat(s): {row['net_amount']} -> {fresh['net_amount']}"
                f" {fresh['currency']}",
                before={k: row[k] for k in changed},
                after={k: fresh[k] for k in changed},
            )
        else:
            counts["unchanged"] += 1
    return counts


async def run(pool: asyncpg.Pool, departure_id: str | None = None) -> dict[str, Any]:
    """Re-price the unfrozen rate costs of every departure that has one (or of one departure), one transaction each;
    a departure that fails is counted and the run raises at the end (retried; the others are stored)."""
    async with pool.acquire() as conn:
        ids = [
            r["id"]
            for r in await conn.fetch(
                "SELECT DISTINCT departure_id::text AS id FROM booking.departure_costs"
                " WHERE status = 'active' AND frozen_at IS NULL AND pricing = 'rate'"
                " AND ($1::uuid IS NULL OR departure_id = $1::uuid) ORDER BY 1",
                departure_id,
            )
        ]
    totals = {"departures": len(ids), "costs": 0, "repriced": 0, "unchanged": 0, "failed": 0}
    errors: list[str] = []
    for dep in ids:
        try:
            async with pool.acquire() as conn, conn.transaction():
                out = await recompute_departure(conn, dep)
        except Exception as exc:  # noqa: BLE001 — one bad departure must not stop the others; raised below
            totals["failed"] += 1
            errors.append(f"{dep}: {type(exc).__name__}")
            log.warning("%s departure %s failed: %s", KIND, dep, type(exc).__name__)
            continue
        for k in ("costs", "repriced", "unchanged"):
            totals[k] += out[k]
    if errors:
        raise RuntimeError(f"{len(errors)} departure(s) failed: {'; '.join(errors[:10])}")
    return {"outcome": "ok", **totals}


async def handle(pool: asyncpg.Pool, job: Job) -> dict[str, Any]:
    dep = (job.payload or {}).get("departure_id")
    return await run(pool, _uuid(dep) if dep else None)


HANDLERS = {KIND: handle}


class RecomputeSchedule:
    """Worker maintenance hook: one ``bk_departure_cost_recompute`` run per new watermark (the sold seats of every
    departure with an unfrozen rate cost, and the newest rate change): a hold confirmed or cancelled re-prices within
    one worker pass. The idempotency key is the watermark's hash, so a watermark is run once."""

    def __init__(self, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._now = now
        self._done: str | None = None

    async def __call__(self, pool: asyncpg.Pool) -> str | None:
        async with pool.acquire() as conn:
            mark = await conn.fetchval(WATERMARK_SQL)
        key = f"{KIND}:wm:{hashlib.sha256(str(mark).encode()).hexdigest()[:32]}"
        if key == self._done:
            return None
        job_id = await queue.enqueue(
            pool, KIND, {"trigger": "watermark"}, idempotency_key=key, max_attempts=MAX_ATTEMPTS, created_by=SCHEDULER
        )
        self._done = key
        return job_id
