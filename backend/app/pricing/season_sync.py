"""The season calendar step of the catalog sync (AA-1091 bk-055; AA-1128 B1, bk-059).

``bk_trip_plan_sync`` (:mod:`app.catalog.trip_plan_sync`) gives every trip plan without a season calendar the calendar
of the destination of its first day, creating that empty calendar when it is missing (:func:`assign_default_calendars`).
On Dev the first version of this step never assigned a calendar: its batch query joined the CIS catalog view for every
plan before its LIMIT and ran past the statement timeout on every run, while the job said "succeeded". Now the batch is
picked from ``booking.trip_plans`` alone, the catalog is read one plan at a time (each read cut at the pricing policy's
``season_sync_read_timeout_seconds``, in a savepoint), and every plan picked is counted under one outcome.
:func:`last_run` gives the Seasons page the last finished run's counts and the plans still without a calendar.
"""

from __future__ import annotations

import json
from typing import Any

import asyncpg

NAME_MAX = 120  # technical-constant: the season_calendars name length (a calendar made by the sync)
# Every plan picked is counted under one outcome (picked = assigned + no_tour + no_destination + timed_out + not_tried).
COUNTS = ("picked", "assigned", "calendars_created", "no_tour", "no_destination", "timed_out", "not_tried")

# AA-1128 B1: the batch comes from booking.trip_plans alone; the CIS catalog is read one plan at a time below (the old
# batch query joined catalog.v_tour for every plan without a calendar before its LIMIT and ran past the statement
# timeout on every run, so no calendar was ever assigned on Dev).
UNASSIGNED_SQL = """
SELECT p.id::text AS id, p.source_system, p.source_id
FROM booking.trip_plans p
WHERE p.season_calendar_id IS NULL
ORDER BY md5(p.id::text || $2)
LIMIT $1
"""
# One tour key at a time, by equality (a filter the view's plan reaches).
TOUR_ID_SQL = """
SELECT t.tour_id FROM catalog.v_tour t WHERE t.source_system = $1 AND t.source_id = $2 LIMIT 1
"""
# One tour at a time (a tour_id = $1 filter reaches the view's parse; an ANY() list does not, lessons "Dữ liệu").
DAY_ONE_SQL = """
SELECT pl.destination_id::text AS destination_id, pl.destination_name
FROM catalog.v_tour_place pl
WHERE pl.tour_id = $1 AND pl.day_number = 1 AND pl.destination_id IS NOT NULL
ORDER BY pl.position
LIMIT 1
"""
CALENDAR_FOR_SQL = """
WITH made AS (
    INSERT INTO booking.season_calendars (owner_kind, destination_id, name, created_by)
    VALUES ('destination', $1::uuid, $2, $3)
    ON CONFLICT (destination_id) WHERE destination_id IS NOT NULL DO NOTHING
    RETURNING id
)
SELECT id::text FROM made
UNION ALL
SELECT id::text FROM booking.season_calendars WHERE destination_id = $1::uuid
LIMIT 1
"""
SYSTEM = "system:bk_trip_plan_sync"
# A catalog read cut by the read timeout (client side: TimeoutError; server side: QueryCanceledError).
READ_CUT: tuple[type[BaseException], ...] = (TimeoutError, asyncpg.QueryCanceledError)


async def assign_default_calendars(
    conn: asyncpg.Connection, limit: int, salt: str, read_timeout: float
) -> dict[str, int]:
    """Give up to ``limit`` plans without a calendar the calendar of the destination of their first day (made empty
    when missing). ``salt`` rotates which plans are tried, so the ones that cannot get a calendar never block the
    others. Every plan picked is counted under exactly one outcome (``picked`` = ``assigned`` + ``no_tour`` +
    ``no_destination`` + ``timed_out`` + ``not_tried``):

    - ``assigned`` (``calendars_created`` of them made a new empty calendar);
    - ``no_tour``: the plan's tour is not in ``catalog.v_tour`` (paused or retired in CIS);
    - ``no_destination``: its first day names no destination (the plan stays in the default low season);
    - ``timed_out``: a catalog read took longer than ``read_timeout`` seconds (pricing policy
      ``season_sync_read_timeout_seconds``): the read is rolled back to its savepoint and the batch ends there, since
      a slow catalog would cut every next read too; the plans after it are ``not_tried``.

    A caller treats ``timed_out`` or ``not_tried`` above zero as a degraded run (:mod:`app.catalog.trip_plan_sync`).
    Catalog errors other than a cut read (view missing, no grant) propagate: the whole step is skipped."""
    out = dict.fromkeys(COUNTS, 0)
    plans = await conn.fetch(UNASSIGNED_SQL, limit, salt)
    out["picked"] = len(plans)
    for done, plan in enumerate(plans):
        try:
            async with conn.transaction():  # a savepoint: a cut read rolls back alone
                tour_id = await conn.fetchval(
                    TOUR_ID_SQL, plan["source_system"], plan["source_id"], timeout=read_timeout
                )
                dest = None
                if tour_id is not None:
                    dest = await conn.fetchrow(DAY_ONE_SQL, tour_id, timeout=read_timeout)
        except READ_CUT:
            out["timed_out"] += 1
            out["not_tried"] = len(plans) - done - 1
            break
        if tour_id is None:
            out["no_tour"] += 1
            continue
        if dest is None:
            out["no_destination"] += 1
            continue
        before = await conn.fetchval(
            "SELECT count(*) FROM booking.season_calendars WHERE destination_id = $1::uuid", dest["destination_id"]
        )
        name = (dest["destination_name"] or "Destination")[:NAME_MAX]
        cal = await conn.fetchval(CALENDAR_FOR_SQL, dest["destination_id"], name, SYSTEM)
        out["calendars_created"] += int(not before)
        await conn.execute(
            "UPDATE booking.trip_plans SET season_calendar_id = $2::uuid WHERE id = $1::uuid AND"
            " season_calendar_id IS NULL",
            plan["id"],
            cal,
        )
        out["assigned"] += 1
    return out


# AA-1128 B1: the last finished run of the catalog sync and its season calendar step (the job_kind_created_idx
# index), and how many plans still wait for a calendar.
LAST_SYNC_SQL = """
SELECT j.finished_at, j.result::text AS result
FROM booking.job j
WHERE j.kind = 'bk_trip_plan_sync' AND j.status = 'succeeded'
ORDER BY j.created_at DESC
LIMIT 1
"""
PLAN_COVERAGE_SQL = """
SELECT count(*)::int AS plans, count(*) FILTER (WHERE season_calendar_id IS NULL)::int AS without_calendar
FROM booking.trip_plans
"""


def sync_out(finished_at: Any, result: dict[str, Any] | None) -> dict[str, Any] | None:
    """The season calendar step of one catalog sync result, as the Seasons page shows it (``None`` = no run yet).
    A run before AA-1128 has no ``picked``/``timed_out`` counts: they read 0; ``skipped`` names why a step did not
    run, and ``degraded`` is true when it was skipped or cut (the same rule as the job result)."""
    if result is None:
        return None
    step = result.get("calendars") if isinstance(result.get("calendars"), dict) else {}
    counts = {k: int(step.get(k) or 0) for k in COUNTS}
    skipped = step.get("skipped")
    return {
        "finished_at": finished_at,
        **counts,
        "skipped": str(skipped) if skipped else None,
        "degraded": bool(skipped or counts["timed_out"] or counts["not_tried"]),
    }


async def last_run(conn: asyncpg.Connection) -> dict[str, Any]:
    """``sync`` (:func:`sync_out` of the last finished catalog sync), ``plans`` and ``plans_without_calendar``."""
    last = await conn.fetchrow(LAST_SYNC_SQL)
    coverage = await conn.fetchrow(PLAN_COVERAGE_SQL)
    return {
        "sync": sync_out(last["finished_at"], json.loads(last["result"] or "null")) if last else None,
        "plans": coverage["plans"] if coverage else 0,
        "plans_without_calendar": coverage["without_calendar"] if coverage else 0,
    }
