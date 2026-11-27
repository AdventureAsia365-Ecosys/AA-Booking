"""AA-1190 My work: the season and holiday changes waiting for a second person that the caller may approve.

The approve paths of :mod:`app.pricing.seasons_admin` (``decide_range``, ``decide_default``, ``decide_event`` and the
removal decisions) apply a maker-checker that this module restates as SQL, so the My work tiles "Season changes to
approve" and "Holiday events to approve" count only what the viewer can act on:

- never a proposal or a removal request of the viewer's own, except the business owner's own proposal while she may
  self-approve (AA-1158 Q5, :func:`app.auth.self_approval.may_self_approve`: a season range, a default season, a
  holiday event; never a stop-sale range, never a removal);
- a stop-sale range (and its removal) only for a ``stop_sale.approve`` holder (AA-1150).

Parameters: ``$1`` the caller, ``$2`` may self-approve, ``$3`` holds ``stop_sale.approve`` (:func:`approver`); the
event filter takes only ``$1, $2`` (:func:`event_args`). The Seasons page (each calendar's ``approvable``, its
``?approvable=me`` view, :func:`by_calendar`) and the Holiday events page (``GET /booking/pricing/holiday-events
?approvable=me``) read the same filters, so a tile equals its list. Read-only.
"""

from __future__ import annotations

from typing import Any

import asyncpg

from app.auth import self_approval
from app.auth.staff import Staff

RANGE_APPROVABLE = (
    "((r.kind <> 'stop_sale' OR $3::boolean) AND ("
    "(r.status = 'pending' AND (r.created_by IS DISTINCT FROM $1::text OR ($2::boolean AND r.kind = 'season')))"
    " OR (r.status = 'approved' AND r.end_requested_at IS NOT NULL AND r.ended_at IS NULL"
    " AND r.end_requested_by IS DISTINCT FROM $1::text)))"
)
DEFAULT_APPROVABLE = (
    "(c.pending_default_season IS NOT NULL AND (c.default_requested_by IS DISTINCT FROM $1::text OR $2::boolean))"
)
EVENT_APPROVABLE = (
    "(e.ended_at IS NULL AND ((e.status = 'pending' AND (e.created_by IS DISTINCT FROM $1::text OR $2::boolean))"
    " OR (e.status = 'approved' AND e.end_requested_at IS NOT NULL AND e.end_requested_by IS DISTINCT FROM $1::text)))"
)
SEASONS_APPROVABLE_SQL = f"""
SELECT (SELECT count(*) FROM booking.season_ranges r WHERE {RANGE_APPROVABLE})::int
     + (SELECT count(*) FROM booking.season_calendars c WHERE {DEFAULT_APPROVABLE})::int AS count,
       LEAST(
         (SELECT min(coalesce(r.end_requested_at, r.created_at)) FROM booking.season_ranges r WHERE {RANGE_APPROVABLE}),
         (SELECT min(c.default_requested_at) FROM booking.season_calendars c WHERE {DEFAULT_APPROVABLE})
       ) AS oldest_at
"""  # noqa: S608 — constants only
# Per calendar: its approvable ranges plus its approvable default season change (the Seasons page's column).
CALENDARS_APPROVABLE_SQL = f"""
SELECT id::text AS id, sum(n)::int AS n FROM (
  SELECT r.calendar_id AS id, count(*) AS n FROM booking.season_ranges r WHERE {RANGE_APPROVABLE} GROUP BY 1
  UNION ALL
  SELECT c.id, 1 FROM booking.season_calendars c WHERE {DEFAULT_APPROVABLE}
) t GROUP BY id
"""  # noqa: S608 — constants only
EVENTS_APPROVABLE_SQL = f"""
SELECT count(*)::int AS count, min(coalesce(e.end_requested_at, e.created_at)) AS oldest_at
FROM booking.holiday_events e WHERE {EVENT_APPROVABLE}
"""  # noqa: S608 — constants only


async def approver(conn: asyncpg.Connection, staff: Staff) -> tuple[str, bool, bool]:
    """The ``$1, $2, $3`` of the range and default filters: the caller, whether they may approve their own proposal now
    and whether they review stop-sales."""
    own = await self_approval.may_self_approve(conn, staff)
    return staff.cis_user_id, own, staff.can("stop_sale.approve")


async def event_args(conn: asyncpg.Connection, staff: Staff) -> tuple[str, bool]:
    """The ``$1, $2`` of :data:`EVENT_APPROVABLE` (an event has no stop-sale)."""
    return (await approver(conn, staff))[:2]


async def seasons_waiting_queue(conn: asyncpg.Connection, staff: Staff) -> dict[str, Any]:
    """The season changes (ranges, their removals, default seasons) ``staff`` may approve, and the oldest ask."""
    row = await conn.fetchrow(SEASONS_APPROVABLE_SQL, *(await approver(conn, staff)))
    return {"count": row["count"], "oldest_at": row["oldest_at"]}


async def events_waiting_queue(conn: asyncpg.Connection, staff: Staff) -> dict[str, Any]:
    """The holiday event changes (proposals, removals) ``staff`` may approve, and the oldest ask."""
    row = await conn.fetchrow(EVENTS_APPROVABLE_SQL, *(await event_args(conn, staff)))
    return {"count": row["count"], "oldest_at": row["oldest_at"]}


async def by_calendar(conn: asyncpg.Connection, staff: Staff) -> dict[str, int]:
    """Calendar id -> the waiting changes of it ``staff`` may approve ({} without ``season.approve``)."""
    if not staff.can("season.approve"):
        return {}
    return {r["id"]: r["n"] for r in await conn.fetch(CALENDARS_APPROVABLE_SQL, *(await approver(conn, staff)))}
