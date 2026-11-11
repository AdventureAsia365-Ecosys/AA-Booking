"""Profit per trip case, departure and agency (AA-1074, bk-045; S275 study item bk-08).

Reads of the bk-045 views (owner ``bk_owner``, ``conf:commercial``, staff only, never in ``booking.b2b.v1``):

- ``v_case_profit``: per B2B trip case with a confirmed hold. Revenue = the confirmed quote total (the agency tier
  discount is already inside the B2B price), or the cancellation fee once the agency cancelled; in the quote currency
  and in USD at the quote's locked plain rate. Costs in USD: quoted (the quote's net lines), planned (live supplier
  bookings at their snapshot rates + the case's share of departure costs), actual (approved / paid bill lines,
  supplier cancellation fees included, + share), and the best estimate ``cost_usd`` (billed, else planned while live,
  else the quote while no booking exists yet). Profit, planned profit and margin (fraction of revenue).
- ``v_departure_profit``: per departure: sold seats, revenue and own costs of its cases, the full shared cost, profit,
  profit per sold seat, margin. Each row also carries ``tour_title`` (CIS catalog, by ``trip_plan_source_id``).
- ``v_agency_profit_month``: per agency (B2B partner) and travel month (trip start).

Every USD figure is a sum of per-line cents (:mod:`app.pricing.fx`). ``fx_missing`` flags a row whose currency had no
approved FX rate (its USD part is then left out of the totals). Permission ``profit.read`` (admin, finance).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import asyncpg

from app.errors import Invalid
from app.finance.departure_costs import with_tour_titles
from app.ops.supplier_bills import jsonable

MAX_PAGE = 200

CASES_SQL = """
SELECT p.trip_case_id::text AS trip_case_id, p.code, p.status, p.start_date, p.end_date, p.pax,
       p.partner_id::text AS partner_id, bp.agency_name, p.departure_id::text AS departure_id,
       tp.source_id AS trip_plan_source_id, p.currency, p.fx_rate_per_usd, p.cancelled, p.revenue, p.revenue_usd,
       p.quoted_cost_usd, p.bookings, p.planned_cost_usd, p.actual_cost_usd, p.supplier_cancel_fee_usd,
       p.shared_cost_usd, p.unbilled_bookings, p.fx_missing, p.cost_basis, p.cost_usd, p.profit_usd,
       p.planned_profit_usd, p.profit, p.margin
  FROM booking.v_case_profit p
  LEFT JOIN booking.b2b_partners bp ON bp.id = p.partner_id
  LEFT JOIN booking.trip_plans tp ON tp.id = p.trip_plan_id
 WHERE ($1::date IS NULL OR p.start_date >= $1) AND ($2::date IS NULL OR p.start_date <= $2)
   AND ($3::uuid IS NULL OR p.partner_id = $3::uuid)
"""

DEPARTURES_SQL = """
SELECT p.departure_id::text AS departure_id, tp.source_id AS trip_plan_source_id, p.start_date, p.status, p.capacity,
       p.sold_seats, p.cases, p.revenue_usd, p.case_cost_usd, p.shared_cost_lines, p.shared_planned_usd,
       p.shared_cost_usd, p.cost_usd, p.profit_usd, p.profit_per_seat_usd, p.margin, p.fx_missing
  FROM booking.v_departure_profit p
  JOIN booking.trip_plans tp ON tp.id = p.trip_plan_id
 WHERE ($1::date IS NULL OR p.start_date >= $1) AND ($2::date IS NULL OR p.start_date <= $2)
"""

AGENCIES_SQL = """
SELECT m.partner_id::text AS partner_id, bp.agency_name, m.travel_month, m.cases, m.cancelled_cases, m.revenue_usd,
       m.cost_usd, m.profit_usd, m.margin, m.fx_missing
  FROM booking.v_agency_profit_month m
  LEFT JOIN booking.b2b_partners bp ON bp.id = m.partner_id
 WHERE ($1::date IS NULL OR m.travel_month >= date_trunc('month', $1::date)::date)
   AND ($2::date IS NULL OR m.travel_month <= $2)
"""


def _window(start: date | None, end: date | None) -> None:
    if start and end and end < start:
        raise Invalid("invalid_window", "the end is before the start")


def _page(limit: int, offset: int) -> None:
    if not 1 <= limit <= MAX_PAGE or offset < 0:
        raise Invalid("invalid_page", f"limit is 1..{MAX_PAGE} and offset >= 0")


def totals(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> dict[str, Any]:
    """Sums of the USD columns over rows (``None`` counts as 0) and the margin of the sums."""
    out: dict[str, Any] = {k: sum((Decimal(str(r[k])) for r in rows if r.get(k) is not None), Decimal(0)) for k in keys}
    revenue = out.get("revenue_usd")
    profit = out.get("profit_usd")
    out["margin"] = (profit / revenue).quantize(Decimal("0.0001")) if revenue and profit is not None else None
    out["fx_missing"] = any(bool(r.get("fx_missing")) for r in rows)
    return out


async def cases(
    conn: asyncpg.Connection,
    *,
    start: date | None = None,
    end: date | None = None,
    partner: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    _window(start, end)
    _page(limit, offset)
    rows = [dict(r) for r in await conn.fetch(CASES_SQL + " ORDER BY p.start_date, p.code", start, end, partner)]
    return jsonable(
        {
            "items": rows[offset : offset + limit],
            "total": len(rows),
            "limit": limit,
            "offset": offset,
            "totals": totals(rows, ("revenue_usd", "planned_cost_usd", "actual_cost_usd", "cost_usd", "profit_usd")),
        }
    )


async def departures(
    conn: asyncpg.Connection, *, start: date | None = None, end: date | None = None, limit: int = 50, offset: int = 0
) -> dict[str, Any]:
    _window(start, end)
    _page(limit, offset)
    rows = [dict(r) for r in await conn.fetch(DEPARTURES_SQL + " ORDER BY p.start_date, p.departure_id", start, end)]
    return jsonable(
        {
            "items": await with_tour_titles(conn, rows[offset : offset + limit]),
            "total": len(rows),
            "limit": limit,
            "offset": offset,
            "totals": totals(rows, ("revenue_usd", "case_cost_usd", "shared_cost_usd", "cost_usd", "profit_usd")),
        }
    )


async def agencies(conn: asyncpg.Connection, *, start: date | None = None, end: date | None = None) -> dict[str, Any]:
    _window(start, end)
    rows = [
        dict(r) for r in await conn.fetch(AGENCIES_SQL + " ORDER BY m.travel_month DESC, bp.agency_name", start, end)
    ]
    return jsonable(
        {"items": rows[:500], "total": len(rows), "totals": totals(rows, ("revenue_usd", "cost_usd", "profit_usd"))}
    )
