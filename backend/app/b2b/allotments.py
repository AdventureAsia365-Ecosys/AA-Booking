"""Per-agency seat allotments on a departure (AA-1073, bk-046; S275 study item bk-03).

An **allotment** reserves ``seats`` on one departure for one agency (``booking.b2b_partners``) until its release date
``start_date - release_days_before`` (UTC day). Rows are append-only facts (``booking.departure_allotments``):

- **Pending → in force** by a second staff member (maker-checker: the maker never approves; DB CHECK too). Approving
  locks the departure row (the lock holds take, :data:`app.b2b.departures.LOCK_SQL`), counts held, sold and the other
  agencies' unused allotments, and refuses (409 ``allotment_exceeds_free``) when the general free seats would go below
  zero.
- **A change** is a new pending row naming the one in force it ``replaces``; approving it ends the old one
  (``replaced``) in the same transaction, so at most one is in force per departure and agency.
- **End**: a pending row is ``rejected``, one in force or released is ``withdrawn``; cancelling the departure ends
  them all (``departure_cancelled``, :func:`app.b2b.departures.update_departure`).
- **Release**: the job ``bk_allotment_release`` stamps ``released_at`` on the release date (queued daily with the
  departure watch, :mod:`app.b2b.departure_watch`); the outbox trigger tells the agency (``allotment.released``). A
  released allotment reserves nothing; its row stays.

Seats are counted in :mod:`app.b2b.departures` (``live_allotments`` / ``reserved_for_others``): an agency's unused
allotment (seats minus its own held + sold on the departure) is kept from every other agency; its own holds draw on
it first. Staff endpoints: ``app.routers.catalog`` (``b2b_desk.read`` / ``b2b_desk.write``). Ids, seats and dates
only: no free text, no traveller data.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import asyncpg

from app import audit
from app.auth.staff import Staff
from app.b2b import departures
from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.jobs.queue import Job

log = logging.getLogger(__name__)

KIND = "bk_allotment_release"
ACTOR = "system:bk_allotment_release"
MAX_SEATS = 1000
MAX_RELEASE_DAYS = 365
RELEASE_BATCH = 500

ALLOTMENT_SQL = """
SELECT a.id::text AS id, a.departure_id::text AS departure_id, a.partner_id::text AS partner_id,
       p.agency_name, p.status AS partner_status, a.seats::int AS seats,
       a.release_days_before::int AS release_days_before, a.replaces_id::text AS replaces_id,
       a.created_by, cu.name AS created_by_name, a.created_at, a.approved_by, au.name AS approved_by_name,
       a.approved_at, a.released_at, a.ended_at, a.ended_by, eu.name AS ended_by_name, a.end_reason,
       d.start_date
FROM booking.departure_allotments a
JOIN booking.b2b_partners p ON p.id = a.partner_id
JOIN booking.departures d ON d.id = a.departure_id
LEFT JOIN booking.staff_users cu ON cu.cis_user_id = a.created_by
LEFT JOIN booking.staff_users au ON au.cis_user_id = a.approved_by
LEFT JOIN booking.staff_users eu ON eu.cis_user_id = a.ended_by
"""
# An agency's own seats on a departure (live holds + sold), the same rule as departures.USAGE_SQL.
PARTNER_USED_SQL = """
SELECT coalesce(sum(pl.seats), 0)::int
FROM booking.b2b_hold_lines pl JOIN booking.b2b_holds ph ON ph.id = pl.hold_id
WHERE pl.departure_id = $1::uuid AND ph.partner_id = $2::uuid
  AND ((ph.status = 'active' AND ph.expires_at > now())
       OR (ph.status = 'confirmed' AND pl.status NOT IN ('declined', 'cancelled')))
"""
PARTNER_OPTIONS_SQL = (
    "SELECT id::text AS id, agency_name FROM booking.b2b_partners WHERE status = 'active' ORDER BY agency_name, id"
)


def _uuid(value: str | None) -> str | None:
    try:
        return str(uuid.UUID(str(value))) if value is not None else None
    except ValueError:
        return None


def state(row: Any) -> str:
    """``pending`` | ``in_force`` | ``released`` | ``ended``."""
    if row["ended_at"] is not None:
        return "ended"
    if row["released_at"] is not None:
        return "released"
    if row["approved_at"] is not None:
        return "in_force"
    return "pending"


def release_date(row: Any) -> date:
    return row["start_date"] - timedelta(days=int(row["release_days_before"]))


def _who(ref: str | None, name: str | None) -> dict[str, Any] | None:
    return None if ref is None else {"id": ref, "name": name}


def allotment_out(row: Any, used: int | None = None) -> dict[str, Any]:
    st = state(row)
    out = {
        "id": row["id"],
        "departure_id": row["departure_id"],
        "partner": {"id": row["partner_id"], "agency_name": row["agency_name"]},
        "seats": int(row["seats"]),
        "release_days_before": int(row["release_days_before"]),
        "release_date": release_date(row),
        "state": st,
        "replaces_id": row["replaces_id"],
        "created_by": _who(row["created_by"], row["created_by_name"]),
        "created_at": row["created_at"],
        "approved_by": _who(row["approved_by"], row["approved_by_name"]),
        "approved_at": row["approved_at"],
        "released_at": row["released_at"],
        "ended_by": _who(row["ended_by"], row["ended_by_name"]),
        "ended_at": row["ended_at"],
        "end_reason": row["end_reason"],
        # The agency's own seats on the departure, and what the allotment still keeps from the others (in force).
        "used": used,
        "unused": max(int(row["seats"]) - used, 0) if used is not None and st == "in_force" else None,
    }
    return out


async def _departure(conn: asyncpg.Connection, departure_id: str, *, lock: bool) -> Any:
    dep = _uuid(departure_id)
    sql = departures.LOCK_SQL if lock else departures.BY_IDS_SQL
    rows = await conn.fetch(sql, [dep]) if dep else []
    if not rows:
        raise NotFound("departure_not_found", f"departure {departure_id} not found")
    return rows[0]


async def _row(conn: asyncpg.Connection, allotment_id: str, *, lock: bool = False) -> Any:
    ident = _uuid(allotment_id)
    row = (
        await conn.fetchrow(ALLOTMENT_SQL + " WHERE a.id = $1::uuid" + (" FOR UPDATE OF a" if lock else ""), ident)
        if ident
        else None
    )
    if row is None:
        raise NotFound("allotment_not_found", f"allotment {allotment_id} not found")
    return row


async def list_for_departure(conn: asyncpg.Connection, departure_id: str) -> dict[str, Any]:
    """The departure (seats as staff see them), its allotments (newest first, every state) with each agency's own
    seats, and the active agencies a new allotment can name."""
    dep = await _departure(conn, departure_id, lock=False)
    held, sold = (await departures.usage(conn, [dep["id"]])).get(dep["id"], (0, 0))
    live = (await departures.live_allotments(conn, [dep["id"]])).get(dep["id"], [])
    rows = await conn.fetch(ALLOTMENT_SQL + " WHERE a.departure_id = $1::uuid ORDER BY a.created_at DESC", dep["id"])
    used_by = {a.partner_id: a.used for a in live}
    items = []
    for r in rows:
        in_force = state(r) == "in_force"
        used = used_by.get(r["partner_id"]) if in_force else None
        if used is None and in_force:
            used = int(await conn.fetchval(PARTNER_USED_SQL, dep["id"], r["partner_id"]) or 0)
        items.append(allotment_out(r, used))
    options = [{"id": r["id"], "agency_name": r["agency_name"]} for r in await conn.fetch(PARTNER_OPTIONS_SQL)]
    return {"departure": departures.staff_out(dep, held, sold, live), "items": items, "partner_options": options}


def _check_numbers(seats: int, release_days: int, dep: Any) -> None:
    if not 1 <= seats <= MAX_SEATS:
        raise Invalid("invalid_seats", f"seats is between 1 and {MAX_SEATS}")
    if seats > int(dep["capacity"]):
        raise Invalid("invalid_seats", f"the departure has {dep['capacity']} seats in all")
    if not 0 <= release_days <= MAX_RELEASE_DAYS:
        raise Invalid("invalid_release_days", f"release_days_before is between 0 and {MAX_RELEASE_DAYS}")


def _check_open(dep: Any, release_days: int, today: date) -> None:
    if dep["status"] == "cancelled":
        raise Conflict("departure_cancelled", "a cancelled departure takes no allotment")
    released_on = dep["start_date"] - timedelta(days=release_days)
    if released_on <= today:
        raise Conflict(
            "release_date_passed",
            f"the release date {released_on} is not after today: the seats would go back to sale at once",
        )


async def create(
    conn: asyncpg.Connection, staff: Staff, departure_id: str, data: dict[str, Any], *, today: date | None = None
) -> dict[str, Any]:
    """The maker step (call inside a transaction): a pending allotment of ``seats`` for ``partner_id`` released
    ``release_days_before`` days before the start. It replaces the agency's allotment in force, once approved. One
    pending per departure and agency (409 ``allotment_pending``)."""
    today = today or datetime.now(UTC).date()
    dep = await _departure(conn, departure_id, lock=True)
    seats, release_days = int(data["seats"]), int(data["release_days_before"])
    _check_numbers(seats, release_days, dep)
    _check_open(dep, release_days, today)
    partner = _uuid(data.get("partner_id"))
    status = (
        await conn.fetchval("SELECT status FROM booking.b2b_partners WHERE id = $1::uuid", partner) if partner else None
    )
    if status is None:
        raise Invalid("partner_not_found", "partner_id is not a B2B partner")
    if status != "active":
        raise Conflict("partner_suspended", "the agency is suspended: it takes no allotment")
    current = await conn.fetch(
        "SELECT id::text AS id, approved_at FROM booking.departure_allotments"
        " WHERE departure_id = $1::uuid AND partner_id = $2::uuid AND ended_at IS NULL AND released_at IS NULL",
        dep["id"],
        partner,
    )
    pending = next((r["id"] for r in current if r["approved_at"] is None), None)
    if pending is not None:
        raise Conflict(
            "allotment_pending", "this agency already has an allotment waiting for approval", allotment_id=pending
        )
    replaces = next((r["id"] for r in current if r["approved_at"] is not None), None)
    new_id = await conn.fetchval(
        "INSERT INTO booking.departure_allotments (departure_id, partner_id, seats, release_days_before, replaces_id,"
        " created_by) VALUES ($1::uuid, $2::uuid, $3, $4, $5::uuid, $6) RETURNING id::text",
        dep["id"],
        partner,
        seats,
        release_days,
        replaces,
        staff.cis_user_id,
    )
    row = await _row(conn, new_id)
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="allotment.created",
        entity_type="departure",
        entity_id=dep["id"],
        after={
            "allotment_id": new_id,
            "partner_id": partner,
            "seats": seats,
            "release_days_before": release_days,
            "replaces_id": replaces,
        },
        entity_label=str(dep["start_date"]),
        summary=f"Allotment of {seats} seats for {row['agency_name']} proposed (waiting for approval)",
    )
    return allotment_out(row)


async def approve(
    conn: asyncpg.Connection, staff: Staff, allotment_id: str, *, today: date | None = None
) -> dict[str, Any]:
    """The checker step (call inside a transaction): another person than the maker puts it in force, ending the one it
    replaces. Approving an allotment in force again returns it unchanged."""
    today = today or datetime.now(UTC).date()
    row = await _row(conn, allotment_id, lock=True)
    st = state(row)
    if st == "in_force" or st == "released":
        return allotment_out(row)
    if st == "ended":
        raise Conflict("allotment_ended", f"this allotment was {row['end_reason']}; propose a new one")
    if row["created_by"] == staff.cis_user_id:
        raise Forbidden("maker_checker", "you proposed this allotment: another person approves it")
    dep = await _departure(conn, row["departure_id"], lock=True)
    _check_open(dep, int(row["release_days_before"]), today)
    held, sold = (await departures.usage(conn, [dep["id"]])).get(dep["id"], (0, 0))
    others = [
        a
        for a in (await departures.live_allotments(conn, [dep["id"]])).get(dep["id"], [])
        if a.partner_id != row["partner_id"]
    ]
    own_used = int(await conn.fetchval(PARTNER_USED_SQL, dep["id"], row["partner_id"]) or 0)
    unused = sum(a.unused for a in others) + max(int(row["seats"]) - own_used, 0)
    free_after = int(dep["capacity"]) - held - sold - unused
    if free_after < 0:
        raise Conflict(
            "allotment_exceeds_free",
            f"{held} seats are held, {sold} sold and {unused} would be kept for allotments on {dep['capacity']} seats:"
            f" {-free_after} too many",
        )
    replaced = None
    if row["replaces_id"] is not None:
        replaced = await conn.fetchval(
            "UPDATE booking.departure_allotments SET ended_at = now(), ended_by = $2, end_reason = 'replaced'"
            " WHERE id = $1::uuid AND ended_at IS NULL RETURNING id::text",
            row["replaces_id"],
            staff.cis_user_id,
        )
    # The live one of this agency may also be a newer row than replaces_id named (approved after this was proposed).
    stale = await conn.fetch(
        "UPDATE booking.departure_allotments SET ended_at = now(), ended_by = $3, end_reason = 'replaced'"
        " WHERE departure_id = $1::uuid AND partner_id = $2::uuid AND approved_at IS NOT NULL AND ended_at IS NULL"
        " RETURNING id::text",
        dep["id"],
        row["partner_id"],
        staff.cis_user_id,
    )
    await conn.execute(
        "UPDATE booking.departure_allotments SET approved_by = $2, approved_at = now() WHERE id = $1::uuid",
        row["id"],
        staff.cis_user_id,
    )
    ended = [x for x in [replaced, *(r["id"] for r in stale)] if x]
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="allotment.approved",
        entity_type="departure",
        entity_id=dep["id"],
        after={"allotment_id": row["id"], "seats": int(row["seats"]), "replaced": ended, "free_after": free_after},
        entity_label=str(dep["start_date"]),
        summary=f"Allotment of {row['seats']} seats for {row['agency_name']} approved",
    )
    out = await _row(conn, row["id"])
    return allotment_out(out, own_used)


async def end(conn: asyncpg.Connection, staff: Staff, allotment_id: str) -> dict[str, Any]:
    """End an allotment (call inside a transaction): a pending one is rejected, one in force or released is withdrawn
    (its seats go back to sale at once). Ending an ended one returns it unchanged."""
    row = await _row(conn, allotment_id, lock=True)
    st = state(row)
    if st == "ended":
        return allotment_out(row)
    reason = "rejected" if st == "pending" else "withdrawn"
    await conn.execute(
        "UPDATE booking.departure_allotments SET ended_at = now(), ended_by = $2, end_reason = $3 WHERE id = $1::uuid",
        row["id"],
        staff.cis_user_id,
        reason,
    )
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action=f"allotment.{reason}",
        entity_type="departure",
        entity_id=row["departure_id"],
        before={"allotment_id": row["id"], "state": st},
        after={"allotment_id": row["id"], "end_reason": reason},
        entity_label=str(row["start_date"]),
        summary=f"Allotment of {row['seats']} seats for {row['agency_name']} {reason}",
    )
    return allotment_out(await _row(conn, row["id"]))


# ── Release job (bk_allotment_release) ───────────────────────────────────────

DUE_SQL = """
SELECT a.id::text AS id, a.departure_id::text AS departure_id, a.partner_id::text AS partner_id, a.seats::int AS seats,
       d.start_date
FROM booking.departure_allotments a
JOIN booking.departures d ON d.id = a.departure_id
WHERE a.approved_at IS NOT NULL AND a.ended_at IS NULL AND a.released_at IS NULL
  AND d.start_date - a.release_days_before::int <= $1::date
ORDER BY d.start_date, a.id
LIMIT $2
"""


async def release_due(conn: asyncpg.Connection, *, today: date, dry_run: bool = False) -> dict[str, Any]:
    """Release every allotment whose release date is today or earlier (call inside a transaction; at most
    :data:`RELEASE_BATCH` per call). The trigger tells each agency (``allotment.released``)."""
    rows = await conn.fetch(DUE_SQL + ("" if dry_run else " FOR UPDATE OF a SKIP LOCKED"), today, RELEASE_BATCH)
    if not dry_run and rows:
        await conn.execute(
            "UPDATE booking.departure_allotments SET released_at = greatest(now(), approved_at)"
            " WHERE id = ANY($1::uuid[]) AND released_at IS NULL",
            [r["id"] for r in rows],
        )
        for r in rows:
            await audit.record(
                conn,
                actor_type="system",
                actor_ref=ACTOR,
                action="allotment.released",
                entity_type="departure",
                entity_id=r["departure_id"],
                after={"allotment_id": r["id"], "partner_id": r["partner_id"], "seats": r["seats"]},
                entity_label=str(r["start_date"]),
                summary=f"Allotment of {r['seats']} seats released back to sale",
            )
    return {
        "dry_run": dry_run,
        "day": today.isoformat(),
        "released": len(rows),
        "allotments": [r["id"] for r in rows[:50]],
        "more": len(rows) == RELEASE_BATCH,
    }


async def handle_release(pool: asyncpg.Pool, job: Job) -> dict[str, Any]:
    payload = job.payload or {}
    today = date.fromisoformat(payload["day"]) if payload.get("day") else datetime.now(UTC).date()
    async with pool.acquire() as conn, conn.transaction():
        result = await release_due(conn, today=today, dry_run=bool(payload.get("dry_run")))
    log.info("allotment_release day=%s released=%s dry_run=%s", today, result["released"], result["dry_run"])
    return {"outcome": "ok", "trigger": payload.get("trigger"), **result}
