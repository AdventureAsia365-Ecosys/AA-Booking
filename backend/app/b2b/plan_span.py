"""How many days a trip plan line of a B2B booking covers (AA-1088, M13 of the S278 system audit).

A ``tp:`` line starts on its ``service_date`` and covers the tour's days: the confirm stores the trip case's end date
from it, and the agency's confirmation and vouchers show it. Before AA-1088 the days were the plan's highest
component ``day_no`` only, so a 6-day tour whose plan prices only day 1 (or nothing yet) read as a 1-day trip
(end date = start date). The days are now the larger of:

- the tour's ``duration_days`` in the CIS catalog (``catalog.v_tour``, by the plan's ``source_id``);
- the plan's highest component ``day_no`` (a plan priced past the catalog length keeps its own days);
- 1.

Bounded to :data:`MAX_DAYS`. The catalog read runs in a savepoint and a failure (CIS down, a slow read) falls back to
the components: a document or a confirm never fails on it. Other lines (rooms, services) span their nights.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

import asyncpg

from app.b2b.components import PREFIX_PLAN, parse_component

log = logging.getLogger(__name__)

MAX_DAYS = 366  # same bound as a trip plan day (app.catalog.trip_plans.MAX_DAY)
PLAN_DAYS_SQL = (
    "SELECT p.id::text AS id, p.source_id, coalesce(max(c.day_no), 1) AS days FROM booking.trip_plans p"
    " LEFT JOIN booking.trip_plan_components c ON c.trip_plan_id = p.id WHERE p.id = ANY($1::uuid[])"
    " GROUP BY p.id, p.source_id"
)
TOUR_DAYS_SQL = "SELECT source_id, duration_days FROM catalog.v_tour WHERE source_id = ANY($1::text[])"


def _bounded(days: Any) -> int:
    try:
        n = int(days or 1)
    except (TypeError, ValueError):
        n = 1
    return min(max(n, 1), MAX_DAYS)


async def plan_days(conn: asyncpg.Connection, plan_ids: list[str]) -> tuple[dict[str, int], dict[str, str]]:
    """({plan id: days}, {plan id: catalog source_id}) of the given trip plans (see the module note)."""
    days: dict[str, int] = {}
    source: dict[str, str] = {}
    if not plan_ids:
        return days, source
    for row in await conn.fetch(PLAN_DAYS_SQL, sorted(set(plan_ids))):
        days[row["id"]], source[row["id"]] = _bounded(row["days"]), row["source_id"]
    keys = sorted({s for s in source.values() if s})
    if keys:
        try:
            async with conn.transaction():  # a savepoint: a catalog error must not abort the caller's work
                rows = await conn.fetch(TOUR_DAYS_SQL, keys)
        except (asyncpg.PostgresError, TimeoutError) as exc:
            log.warning("b2b_plan_days_catalog_unread error=%s", type(exc).__name__)
            rows = []
        tour_days = {r["source_id"]: _bounded(r.get("duration_days")) for r in rows if r.get("duration_days")}
        for plan_id, key in source.items():
            days[plan_id] = max(days[plan_id], tour_days.get(key, 1))
    return days, source


def plan_id_of(component_id: Any) -> str | None:
    """The trip plan id of a ``tp:`` component id, else None (another kind, or an id that does not parse)."""
    try:
        prefix, ident = parse_component(str(component_id))
    except ValueError:
        return None
    return ident if prefix == PREFIX_PLAN else None


def line_span(line: Any, days_of_plan: dict[str, int]) -> int:
    """Days a quote line covers: a trip plan line its plan's days, any other line its nights (at least 1)."""
    plan_id = plan_id_of(line.get("component_id"))
    if plan_id is not None:
        return max(days_of_plan.get(plan_id, 1), 1)
    return _bounded(line["nights"])


def trip_end(lines: list[Any], days_of_plan: dict[str, int]) -> date | None:
    """The last day of the trip: the latest ``service_date + span - 1`` over the lines (None without a dated line)."""
    ends = [r["service_date"] + timedelta(days=line_span(r, days_of_plan) - 1) for r in lines if r["service_date"]]
    return max(ends) if ends else None
