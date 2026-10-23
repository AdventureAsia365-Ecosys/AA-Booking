"""Fixed departures and their seats (AA-973, bk-020): capacity, held and sold seats, line availability, staff edits.

- A **departure** is a start date of a trip plan with a seat ``capacity``, a ``status`` (``scheduled`` →
  ``guaranteed``; ``cancelled`` is final) and ``auto_confirm``.
- **Held / sold are counted, never stored** (:data:`USAGE_SQL`): held = seats of lines of holds that are ``active``
  and not past ``expires_at``; sold = seats of lines of ``confirmed`` holds whose line is not ``declined`` /
  ``cancelled``. An expired hold frees its seats the moment it expires, before the expiry job writes its status.
- **Availability of a line** (:func:`evaluate`, design Q1 6.2): no departure = ``on_request`` (AA ops confirm with the
  supplier); a departure with enough free seats = ``available`` when it auto-confirms, else ``on_request``; not enough
  seats, ``cancelled``, or starting today or earlier = ``sold_out``. Lines of one request on the same departure add up.
- **Seats** of a line = adults + children (infants take no seat, as in pricing).
- **Serialisation** (Linear AA-973 E: two holds on the last seat → one wins): :func:`lock_departures` takes ``FOR
  UPDATE`` on the departures of a hold in id order before counting, so concurrent holds on a departure run one after
  the other and the second counts the first's committed seats (READ COMMITTED: a new snapshot per statement).

A departure only belongs to a ``tp:<plan>`` line of its own plan, on its ``start_date``. Ids only, no content (O7).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import asyncpg

from app import audit
from app.auth.staff import Staff
from app.b2b.components import PREFIX_PLAN, parse_component
from app.b2b.errors import B2BError
from app.catalog.destinations import CATALOG_ERRORS
from app.errors import Conflict, Invalid, NotFound

AVAILABLE, ON_REQUEST, SOLD_OUT = "available", "on_request", "sold_out"
STATUSES = ("scheduled", "guaranteed", "cancelled")
MAX_CAPACITY = 1000
LIST_DAYS = 366

DEPARTURE_COLUMNS = """
d.id::text AS id, d.trip_plan_id::text AS trip_plan_id, d.start_date, d.capacity, d.status, d.auto_confirm,
d.created_at, d.updated_at
"""

# Seats per departure: held (live holds) and sold (confirmed holds, line not declined / cancelled). ``$2`` is the
# instant "live" is judged at (NULL = the transaction's now(), which only ever counts a hold MORE than the wall clock
# would: safe for places and quotes); ``$3`` leaves one hold out (confirm's own re-count).
USAGE_SQL = """
SELECT l.departure_id::text AS departure_id,
       coalesce(sum(l.seats) FILTER (
           WHERE h.status = 'active' AND h.expires_at > coalesce($2::timestamptz, now())), 0)::int AS held,
       coalesce(sum(l.seats) FILTER (
           WHERE h.status = 'confirmed' AND l.status NOT IN ('declined', 'cancelled')), 0)::int AS sold
FROM booking.b2b_hold_lines l
JOIN booking.b2b_holds h ON h.id = l.hold_id
WHERE l.departure_id = ANY($1::uuid[]) AND ($3::uuid IS NULL OR h.id <> $3::uuid)
GROUP BY l.departure_id
"""

INSERT_SQL = """
INSERT INTO booking.departures (trip_plan_id, start_date, capacity, status, auto_confirm, created_by, updated_by)
VALUES ($1::uuid, $2, $3, $4, $5, $6, $6)
RETURNING id::text AS id, trip_plan_id::text AS trip_plan_id, start_date, capacity, status, auto_confirm, created_at,
          updated_at
"""
UPDATE_SQL = """
UPDATE booking.departures SET capacity = coalesce($2, capacity), status = coalesce($3, status),
       auto_confirm = coalesce($4, auto_confirm), updated_by = $5
WHERE id = $1::uuid
RETURNING id::text AS id, trip_plan_id::text AS trip_plan_id, start_date, capacity, status, auto_confirm, created_at,
          updated_at
"""
BY_IDS_SQL = f"SELECT {DEPARTURE_COLUMNS} FROM booking.departures d WHERE d.id = ANY($1::uuid[])"  # noqa: S608
LOCK_SQL = (
    f"SELECT {DEPARTURE_COLUMNS} FROM booking.departures d WHERE d.id = ANY($1::uuid[])"  # noqa: S608
    " ORDER BY d.id FOR UPDATE"
)


def seats(adult: int, child: int) -> int:
    return adult + child


def _uuid(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        return None


@dataclass(frozen=True)
class SeatAsk:
    """What availability needs of one line (quote or hold)."""

    line_ref: str
    component_id: str
    service_date: date
    departure_id: str | None
    seats: int


def _invalid(line_ref: str, why: str) -> B2BError:
    return B2BError("invalid", f"line {line_ref}: {why}")


async def usage(
    conn: asyncpg.Connection,
    departure_ids: list[str],
    *,
    at: datetime | None = None,
    exclude_hold: str | None = None,
) -> dict[str, tuple[int, int]]:
    """``{departure id: (held, sold)}``; a departure with no hold line is absent (0, 0). ``at``: judge live holds at
    this instant instead of the transaction start; ``exclude_hold``: leave that hold's lines out."""
    if not departure_ids:
        return {}
    rows = await conn.fetch(USAGE_SQL, departure_ids, at, exclude_hold)
    return {r["departure_id"]: (r["held"], r["sold"]) for r in rows}


def free_seats(row: Any, held: int, sold: int) -> int:
    return max(int(row["capacity"]) - held - sold, 0)


def evaluate(
    asks: list[SeatAsk], rows: dict[str, Any], used: dict[str, tuple[int, int]], *, today: date
) -> list[tuple[str, str | None, str | None]]:
    """``[(availability, departure id, why sold out)]`` per line (same order), or B2BError 422 ``invalid`` for a
    departure that does not belong to the line."""
    taken: dict[str, int] = {}
    out: list[tuple[str, str | None, str | None]] = []
    for ask in asks:
        if ask.departure_id is None:
            out.append((ON_REQUEST, None, None))
            continue
        dep_id = _uuid(ask.departure_id)
        row = rows.get(dep_id) if dep_id else None
        if row is None:
            raise _invalid(ask.line_ref, "unknown departure_id")
        try:
            prefix, plan_id = parse_component(ask.component_id)
        except ValueError:
            raise _invalid(ask.line_ref, "unknown component_id") from None
        if prefix != PREFIX_PLAN or plan_id != row["trip_plan_id"]:
            raise _invalid(ask.line_ref, "a departure belongs to the tp: line of its own trip plan")
        if ask.service_date != row["start_date"]:
            raise _invalid(ask.line_ref, f"service_date must be the departure's start date {row['start_date']}")
        held, sold = used.get(dep_id, (0, 0))
        free = free_seats(row, held, sold) - taken.get(dep_id, 0)
        if row["status"] == "cancelled":
            out.append((SOLD_OUT, dep_id, "the departure is cancelled"))
        elif row["start_date"] <= today:
            out.append((SOLD_OUT, dep_id, "the departure has started"))
        elif ask.seats > free:
            out.append((SOLD_OUT, dep_id, f"{max(free, 0)} seats left, {ask.seats} asked"))
        else:
            taken[dep_id] = taken.get(dep_id, 0) + ask.seats
            out.append((AVAILABLE if row["auto_confirm"] else ON_REQUEST, dep_id, None))
    return out


def _departure_ids(asks: list[SeatAsk]) -> list[str]:
    ids: set[str] = set()
    for ask in asks:
        if ask.departure_id is not None:
            dep = _uuid(ask.departure_id)
            if dep is None:
                raise _invalid(ask.line_ref, "unknown departure_id")
            ids.add(dep)
    return sorted(ids)


async def availability(
    conn: asyncpg.Connection, asks: list[SeatAsk], *, today: date
) -> list[tuple[str, str | None, str | None]]:
    """Availability for a quote (no lock: a quote reserves nothing; the hold re-checks under the lock)."""
    ids = _departure_ids(asks)
    rows = {r["id"]: r for r in await conn.fetch(BY_IDS_SQL, ids)} if ids else {}
    return evaluate(asks, rows, await usage(conn, ids), today=today)


async def lock_departures(
    conn: asyncpg.Connection, asks: list[SeatAsk], *, today: date
) -> tuple[list[tuple[str, str | None, str | None]], dict[str, Any]]:
    """Inside a transaction: lock the lines' departures (id order), count, evaluate. Returns the per-line result and
    the locked rows (for the hold expiry cap)."""
    ids = _departure_ids(asks)
    rows = {r["id"]: r for r in await conn.fetch(LOCK_SQL, ids)} if ids else {}
    return evaluate(asks, rows, await usage(conn, ids), today=today), rows


def start_of(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=UTC)


# ── B2B read (GET /booking/b2b/v1/departures) ────────────────────────────────

B2B_LIST_SQL = f"""
SELECT {DEPARTURE_COLUMNS}, p.source_system, p.source_id
FROM booking.departures d
JOIN booking.trip_plans p ON p.id = d.trip_plan_id
WHERE p.source_system = $1 AND p.source_id = $2 AND p.sellable
  AND EXISTS (SELECT 1 FROM catalog.v_tour t WHERE t.source_system = p.source_system AND t.source_id = p.source_id)
  AND d.start_date BETWEEN $3 AND $4
ORDER BY d.start_date, d.id
"""  # noqa: S608 — constant fragment


def departure_out(row: Any, held: int, sold: int) -> dict[str, Any]:
    available = 0 if row["status"] == "cancelled" else free_seats(row, held, sold)
    return {
        "departure_id": row["id"],
        "tour": {"source_system": row["source_system"], "source_id": row["source_id"]},
        "start_date": row["start_date"].isoformat(),
        "capacity": int(row["capacity"]),
        "held": held,
        "sold": sold,
        "available": available,
        "status": row["status"],
        "auto_confirm": bool(row["auto_confirm"]),
    }


async def list_for_tour(
    pool: asyncpg.Pool, *, source_system: str, source_id: str, start: date | None, end: date | None, today: date
) -> dict[str, Any]:
    start = start or today
    end = end or start + timedelta(days=LIST_DAYS)
    if end < start:
        raise B2BError("invalid", "to must not be before from")
    if (end - start).days > 2 * LIST_DAYS:
        raise B2BError("invalid", f"at most {2 * LIST_DAYS} days per request")
    async with pool.acquire() as conn:
        try:
            rows = await conn.fetch(B2B_LIST_SQL, source_system, source_id, start, end)
        except CATALOG_ERRORS:
            raise B2BError("unavailable", "the catalog is not readable right now", retry_after_s=60) from None
        used = await usage(conn, [r["id"] for r in rows])
    return {"items": [departure_out(r, *used.get(r["id"], (0, 0))) for r in rows]}


# ── Staff (GET|POST /booking/catalog/trip-plans/{source_id}/departures, PATCH /booking/catalog/departures/{id}) ──


def staff_out(row: Any, held: int, sold: int) -> dict[str, Any]:
    return {
        "id": row["id"],
        "trip_plan_id": row["trip_plan_id"],
        "start_date": row["start_date"],
        "capacity": int(row["capacity"]),
        "held": held,
        "sold": sold,
        "available": 0 if row["status"] == "cancelled" else free_seats(row, held, sold),
        "status": row["status"],
        "auto_confirm": bool(row["auto_confirm"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


async def _plan_id(conn: asyncpg.Connection, source_id: str) -> str:
    plan = await conn.fetchval(
        "SELECT id::text FROM booking.trip_plans WHERE source_system = 'cis' AND source_id = $1", source_id
    )
    if plan is None:
        raise NotFound("trip_plan_not_found", "save the commercial row of this tour first")
    return plan


async def list_departures(
    conn: asyncpg.Connection, source_id: str, *, start: date | None = None, end: date | None = None
) -> dict[str, Any]:
    plan_id = await _plan_id(conn, source_id)
    rows = await conn.fetch(
        f"SELECT {DEPARTURE_COLUMNS} FROM booking.departures d WHERE d.trip_plan_id = $1::uuid"  # noqa: S608
        " AND ($2::date IS NULL OR d.start_date >= $2) AND ($3::date IS NULL OR d.start_date <= $3)"
        " ORDER BY d.start_date",
        plan_id,
        start,
        end,
    )
    used = await usage(conn, [r["id"] for r in rows])
    return {
        "trip_plan_id": plan_id,
        "source_id": source_id,
        "items": [staff_out(r, *used.get(r["id"], (0, 0))) for r in rows],
    }


def _check_capacity(capacity: int) -> None:
    if not 0 <= capacity <= MAX_CAPACITY:
        raise Invalid("invalid_capacity", f"capacity must be between 0 and {MAX_CAPACITY}")


async def create_departure(
    conn: asyncpg.Connection, staff: Staff, source_id: str, data: dict[str, Any]
) -> dict[str, Any]:
    """``data``: ``start_date``, ``capacity``, ``auto_confirm`` (default true), ``status`` (scheduled | guaranteed)."""
    plan_id = await _plan_id(conn, source_id)
    _check_capacity(int(data["capacity"]))
    if data["start_date"] <= datetime.now(UTC).date():
        raise Invalid("invalid_start_date", "a new departure starts after today (UTC); a started one is never sold")
    status = data.get("status") or "scheduled"
    if status not in ("scheduled", "guaranteed"):
        raise Invalid("invalid_status", "a new departure is scheduled or guaranteed")
    try:
        row = await conn.fetchrow(
            INSERT_SQL,
            plan_id,
            data["start_date"],
            int(data["capacity"]),
            status,
            bool(data.get("auto_confirm", True)),
            staff.cis_user_id,
        )
    except asyncpg.UniqueViolationError:
        raise Conflict("departure_exists", f"this tour already has a departure on {data['start_date']}") from None
    await conn.execute("UPDATE booking.trip_plans SET updated_at = now() WHERE id = $1::uuid", plan_id)
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="departure.created",
        entity_type="departure",
        entity_id=row["id"],
        after={k: row[k] for k in ("start_date", "capacity", "status", "auto_confirm")} | {"source_id": source_id},
        entity_label=f"{source_id[:60]} {row['start_date']}",
        summary=f"Departure {row['start_date']}: {row['capacity']} seats ({status})",
    )
    return staff_out(row, 0, 0)


async def update_departure(
    conn: asyncpg.Connection, staff: Staff, departure_id: str, changes: dict[str, Any]
) -> dict[str, Any]:
    """``changes``: any of ``capacity``, ``status``, ``auto_confirm``. Capacity never below held + sold; cancelled is
    final; a status move is scheduled ↔ guaranteed or → cancelled."""
    dep = _uuid(departure_id)
    rows = await conn.fetch(LOCK_SQL, [dep]) if dep else []
    if not rows:
        raise NotFound("departure_not_found", f"departure {departure_id} not found")
    before = rows[0]
    held, sold = (await usage(conn, [before["id"]])).get(before["id"], (0, 0))
    if before["status"] == "cancelled":
        raise Conflict("departure_cancelled", "a cancelled departure cannot change")
    new = {k: changes[k] for k in ("capacity", "status", "auto_confirm") if changes.get(k) is not None}
    if "capacity" in new:
        _check_capacity(int(new["capacity"]))
        if int(new["capacity"]) < held + sold:
            raise Conflict("capacity_below_taken", f"{held} seats are held and {sold} sold: capacity cannot go lower")
    if "status" in new and new["status"] not in STATUSES:
        raise Invalid("invalid_status", f"status must be one of {', '.join(STATUSES)}")
    changed = {k: v for k, v in new.items() if before[k] != v}
    if not changed:
        return staff_out(before, held, sold)
    row = await conn.fetchrow(
        UPDATE_SQL,
        before["id"],
        changed.get("capacity"),
        changed.get("status"),
        changed.get("auto_confirm"),
        staff.cis_user_id,
    )
    await conn.execute("UPDATE booking.trip_plans SET updated_at = now() WHERE id = $1::uuid", before["trip_plan_id"])
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="departure.updated",
        entity_type="departure",
        entity_id=row["id"],
        before={k: before[k] for k in changed},
        after=changed,
        entity_label=str(row["start_date"]),
        summary="Departure " + ", ".join(f"{k} {before[k]} -> {v}" for k, v in changed.items()),
    )
    return staff_out(row, held, sold)
