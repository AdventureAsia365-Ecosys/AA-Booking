"""The departure board (AA-1073 rest, bk-048; S275 study item bk-04): every fixed departure across trip plans, for the
staff page ``/departures`` (month calendar + table) and its drill-down drawer.

- **Rows** come from the view ``booking.v_departure_occupancy`` (bk-048): seats held, sold, allotted, unused
  allotment, free and occupancy %, the minimum and the at-risk rule, the open incident and how many agencies are on it,
  counted by the same rules as :mod:`app.b2b.departures` (never stored). The tour title and country come from the CIS
  catalog (``catalog.v_tour``); when the catalog cannot be read the board still answers, without them (and a country
  filter then matches nothing).
- **Filters**: a date window (default: this month; at most :data:`MAX_WINDOW` days), country (the CIS tour country),
  trip plan (CIS ``source_id``), status, at risk only. Paged (``limit`` / ``offset``), ordered by start date.
- **Price column** (bk-17): the plan's price hint (the commercial row's "from" price), the departure's price
  adjustment in force and whether a quote today still takes the early-booking discount of the b2b rule (and until
  when).
- **Detail** (:func:`detail`): the row, then seats by agency (held, sold, its allotment, the trip cases), the ops sheet
  (:mod:`app.b2b.departure_ops`), the fees (:mod:`app.b2b.departure_fees`) with what "Charge fee" prefills, and the
  price adjustments (:mod:`app.b2b.price_adjustments`).

Read ``b2b_desk.read`` (``app.routers.b2b_desk``). Agency names and case codes only, no traveller data.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import asyncpg

from app.auth.staff import Staff
from app.b2b import departure_fees, departure_ops, price_adjustments
from app.b2b.allotments import ALLOTMENT_SQL, allotment_out
from app.b2b.allotments import state as allotment_state
from app.catalog.destinations import CATALOG_ERRORS
from app.errors import Invalid, NotFound
from app.pricing import engine, repository

STATUSES = ("scheduled", "guaranteed", "cancelled")
MAX_WINDOW = 400
MAX_PAGE = 500

VIEW_COLUMNS = """
o.departure_id::text AS id, o.trip_plan_id::text AS trip_plan_id, o.source_id, o.start_date, o.status, o.capacity,
o.held, o.sold, o.allotted, o.allotment_unused, o.free, o.occupancy_pct, o.min_to_operate, o.at_risk,
o.open_incident_id::text AS open_incident_id, o.open_incident_code, o.agencies_count, o.auto_confirm,
o.sales_cutoff_days, o.decision_days_before, o.meeting_point, o.meeting_time,
p.price_hint_amount, p.price_hint_currency, p.price_hint_basis
"""
VIEW_FROM = """
FROM booking.v_departure_occupancy o
JOIN booking.trip_plans p ON p.id = o.trip_plan_id
"""
WHERE = """
o.start_date BETWEEN $1 AND $2
AND ($3::text IS NULL OR o.source_id = $3)
AND ($4::text IS NULL OR o.status = $4)
AND (NOT $5::boolean OR o.at_risk)
AND ($6::text[] IS NULL OR o.source_id = ANY($6::text[]))
"""
# Every trip plan with a departure in the window (the board's filter options).
WINDOW_PLANS_SQL = """
SELECT DISTINCT p.source_id FROM booking.departures d JOIN booking.trip_plans p ON p.id = d.trip_plan_id
WHERE d.start_date BETWEEN $1 AND $2
"""
# The agencies on a departure: live holds and sold lines (the bk-022 fan-out rule) with their trip cases.
AGENCIES_SQL = """
SELECT h.partner_id::text AS partner_id, p.agency_name,
       coalesce(sum(l.seats) FILTER (WHERE h.status = 'active' AND h.expires_at > now()), 0)::int AS held,
       coalesce(sum(l.seats) FILTER (
           WHERE h.status = 'confirmed' AND l.status NOT IN ('declined', 'cancelled')), 0)::int AS sold,
       coalesce(jsonb_agg(DISTINCT jsonb_build_object('id', tc.id::text, 'code', tc.code))
                FILTER (WHERE tc.id IS NOT NULL), '[]'::jsonb) AS cases
FROM booking.b2b_hold_lines l
JOIN booking.b2b_holds h ON h.id = l.hold_id
JOIN booking.b2b_partners p ON p.id = h.partner_id
LEFT JOIN booking.trip_cases tc ON tc.id = h.trip_case_id
WHERE l.departure_id = $1::uuid
  AND ((h.status = 'active' AND h.expires_at > now())
       OR (h.status = 'confirmed' AND l.status NOT IN ('declined', 'cancelled')))
GROUP BY h.partner_id, p.agency_name
ORDER BY p.agency_name, h.partner_id
"""


def _uuid(value: str | None) -> str | None:
    try:
        return str(uuid.UUID(str(value))) if value is not None else None
    except ValueError:
        return None


def month_window(today: date) -> tuple[date, date]:
    start = today.replace(day=1)
    nxt = (start + timedelta(days=32)).replace(day=1)
    return start, nxt - timedelta(days=1)


async def _catalog(conn: asyncpg.Connection, source_ids: list[str]) -> dict[str, dict[str, Any]]:
    """CIS title and country by source_id; empty when the catalog cannot be read (the board still answers)."""
    if not source_ids:
        return {}
    try:
        async with conn.transaction():  # a savepoint: a catalog error must not abort the caller's transaction
            rows = await conn.fetch(
                "SELECT source_id, title, country FROM catalog.v_tour"
                " WHERE source_system = 'cis' AND source_id = ANY($1)",
                sorted(set(source_ids)),
            )
    except CATALOG_ERRORS:
        return {}
    return {r["source_id"]: {"title": r["title"], "country": r["country"]} for r in rows}


async def _country_sources(conn: asyncpg.Connection, country: str) -> list[str]:
    """The source ids of the CIS tours of a country (case-insensitive); [] when the catalog cannot be read."""
    try:
        async with conn.transaction():
            rows = await conn.fetch(
                "SELECT source_id FROM catalog.v_tour WHERE source_system = 'cis' AND lower(country) = lower($1)",
                country,
            )
    except CATALOG_ERRORS:
        return []
    return [r["source_id"] for r in rows]


def early_booking(rule: repository.DatedRule | None, start: date, today: date) -> dict[str, Any] | None:
    """The early-booking discount a quote today takes on a departure starting ``start`` (None when the rule has none or
    it is too late): its margin points and the last quote day."""
    if rule is None or rule.early_days is None or rule.early_discount_pts is None:
        return None
    last = start - timedelta(days=int(rule.early_days))
    if today > last:
        return None
    return {"days": int(rule.early_days), "pts": rule.early_discount_pts, "until": last}


def row_out(
    row: Any,
    catalog: dict[str, dict[str, Any]],
    adjustment: price_adjustments.Adjustment | None,
    early: dict[str, Any] | None,
) -> dict[str, Any]:
    tour = catalog.get(row["source_id"]) or {}
    hint = (
        {"amount": row["price_hint_amount"], "currency": row["price_hint_currency"], "basis": row["price_hint_basis"]}
        if row["price_hint_amount"] is not None
        else None
    )
    return {
        "id": row["id"],
        "trip_plan_id": row["trip_plan_id"],
        "source_id": row["source_id"],
        "tour_title": tour.get("title"),
        "country": tour.get("country"),
        "start_date": row["start_date"],
        "status": row["status"],
        "capacity": int(row["capacity"]),
        "held": int(row["held"]),
        "sold": int(row["sold"]),
        "allotted": int(row["allotted"]),
        "allotment_unused": int(row["allotment_unused"]),
        "free": int(row["free"]),
        "occupancy_pct": row["occupancy_pct"],
        "min_to_operate": int(row["min_to_operate"]),
        "at_risk": bool(row["at_risk"]),
        "open_incident": (
            {"id": row["open_incident_id"], "incident_code": row["open_incident_code"]}
            if row["open_incident_id"] is not None
            else None
        ),
        "agencies_count": int(row["agencies_count"]),
        "auto_confirm": bool(row["auto_confirm"]),
        "meeting_point": row["meeting_point"],
        "meeting_time": row["meeting_time"],
        "price": {
            "hint": hint,
            "adjustment": (
                {
                    "id": adjustment.id,
                    "adjust_pct": adjustment.pct,
                    "amount": adjustment.amount,
                    "currency": adjustment.currency,
                }  # fmt: skip
                if adjustment is not None
                else None
            ),
            "early_booking": early,
        },
    }


async def _b2b_rule(conn: asyncpg.Connection, today: date) -> repository.DatedRule | None:
    try:
        return await repository.rule_in_force(conn, engine.Channel.B2B, today)
    except Exception:  # noqa: BLE001 — a bad stored rule never breaks the board; the price column shows no discount
        return None


async def board(
    conn: asyncpg.Connection,
    *,
    start: date | None = None,
    end: date | None = None,
    source_id: str | None = None,
    status: str | None = None,
    at_risk: bool = False,
    country: str | None = None,
    limit: int = 200,
    offset: int = 0,
    today: date | None = None,
) -> dict[str, Any]:
    """The board rows of a window (default: the current month), filtered and paged, with the window's totals."""
    today = today or datetime.now(UTC).date()
    if start is None and end is None:
        start, end = month_window(today)
    start = start or (end - timedelta(days=31) if end else today)
    end = end or start + timedelta(days=31)
    if end < start or (end - start).days > MAX_WINDOW:
        raise Invalid("invalid_window", f"to is on or after from, at most {MAX_WINDOW} days apart")
    if status is not None and status not in STATUSES:
        raise Invalid("invalid_status", f"status is one of {', '.join(STATUSES)}")
    country = (country or "").strip() or None
    sources = await _country_sources(conn, country) if country else None
    args = (start, end, source_id or None, status, at_risk, sources)
    rows = await conn.fetch(
        f"SELECT {VIEW_COLUMNS} {VIEW_FROM} WHERE {WHERE}"  # noqa: S608
        " ORDER BY o.start_date, o.departure_id LIMIT $7 OFFSET $8",
        *args,
        limit,
        offset,
    )
    totals = await conn.fetchrow(
        "SELECT count(*)::int AS departures, coalesce(sum(o.capacity), 0)::int AS capacity,"
        " coalesce(sum(o.held), 0)::int AS held, coalesce(sum(o.sold), 0)::int AS sold,"
        " coalesce(sum(o.free), 0)::int AS free, count(*) FILTER (WHERE o.at_risk)::int AS at_risk,"
        " count(*) FILTER (WHERE o.open_incident_id IS NOT NULL)::int AS incidents"
        f" {VIEW_FROM} WHERE {WHERE}",  # noqa: S608
        *args,
    )
    ids = [r["id"] for r in rows]
    # The filter options come from every plan of the window, not from the filtered page (lessons: server options).
    window_sources = [r["source_id"] for r in await conn.fetch(WINDOW_PLANS_SQL, start, end)]
    catalog = await _catalog(conn, [r["source_id"] for r in rows] + window_sources)
    adjustments = await price_adjustments.in_force(conn, ids)
    rule = await _b2b_rule(conn, today)
    items = [row_out(r, catalog, adjustments.get(r["id"]), early_booking(rule, r["start_date"], today)) for r in rows]
    return {
        "items": items,
        "total": int(totals["departures"]),
        "totals": dict(totals),
        "limit": limit,
        "offset": offset,
        "from": start,
        "to": end,
        "countries": sorted({c["country"] for s in window_sources if (c := catalog.get(s)) and c.get("country")}),
        "plans": sorted(
            ({"source_id": s, "title": (catalog.get(s) or {}).get("title")} for s in set(window_sources)),
            key=lambda p: ((p["title"] or "~").lower(), p["source_id"]),
        ),
    }


async def detail(
    conn: asyncpg.Connection, staff: Staff, departure_id: str, *, today: date | None = None
) -> dict[str, Any]:
    """One departure for the drawer: the board row, seats by agency, the ops sheet, fees and price adjustments."""
    today = today or datetime.now(UTC).date()
    dep = _uuid(departure_id)
    row = (
        await conn.fetchrow(f"SELECT {VIEW_COLUMNS} {VIEW_FROM} WHERE o.departure_id = $1::uuid", dep)  # noqa: S608
        if dep
        else None
    )
    if row is None:
        raise NotFound("departure_not_found", f"departure {departure_id} not found")
    catalog = await _catalog(conn, [row["source_id"]])
    adjustment = (await price_adjustments.in_force(conn, [row["id"]])).get(row["id"])
    out = row_out(row, catalog, adjustment, early_booking(await _b2b_rule(conn, today), row["start_date"], today))

    agencies: dict[str, dict[str, Any]] = {}
    for a in await conn.fetch(AGENCIES_SQL, row["id"]):
        cases = a["cases"]
        if isinstance(cases, str):  # a connection without the jsonb codec
            cases = json.loads(cases)
        agencies[a["partner_id"]] = {
            "partner_id": a["partner_id"],
            "agency_name": a["agency_name"],
            "held": int(a["held"]),
            "sold": int(a["sold"]),
            "cases": sorted(cases, key=lambda c: c.get("code") or ""),
            "allotment": None,
        }
    allotment_rows = await conn.fetch(
        ALLOTMENT_SQL + " WHERE a.departure_id = $1::uuid AND a.ended_at IS NULL AND a.approved_at IS NOT NULL"
        " ORDER BY a.created_at",
        row["id"],
    )
    for a in allotment_rows:
        entry = agencies.setdefault(
            a["partner_id"],
            {
                "partner_id": a["partner_id"],
                "agency_name": a["agency_name"],
                "held": 0,
                "sold": 0,
                "cases": [],
                "allotment": None,
            },  # fmt: skip
        )
        used = entry["held"] + entry["sold"]
        out_a = allotment_out(a, used)
        st = allotment_state(a)
        entry["allotment"] = {
            "id": out_a["id"],
            "seats": out_a["seats"],
            "state": st,
            "release_date": out_a["release_date"],
            "used": used,
            "unused": max(int(a["seats"]) - used, 0),
        }
    agency_list = sorted(agencies.values(), key=lambda x: (x["agency_name"] or "", x["partner_id"]))
    fees = await departure_fees.list_fees(conn, staff, departure_id=row["id"], limit=departure_fees.MAX_PAGE)
    suggestions = departure_fees.suggestions(
        min_to_operate=out["min_to_operate"],
        sold=out["sold"],
        status=out["status"],
        allotments=[
            {"partner_id": x["partner_id"], **x["allotment"]} for x in agency_list if x["allotment"] is not None
        ],
    )
    return {
        "departure": out,
        "agencies": agency_list,
        "ops": await departure_ops.read_ops(conn, row["id"]),
        "fees": fees["items"],
        "fee_suggestions": suggestions,
        "price_adjustments": await price_adjustments.list_for_departure(conn, staff, row["id"]),
        "partner_options": [
            {"id": r["id"], "agency_name": r["agency_name"]}
            for r in await conn.fetch(
                "SELECT id::text AS id, agency_name FROM booking.b2b_partners ORDER BY agency_name, id"
            )
        ],
    }
