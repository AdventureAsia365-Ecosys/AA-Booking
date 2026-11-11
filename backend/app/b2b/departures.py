"""Fixed departures and their seats (AA-973, bk-020): capacity, held and sold seats, line availability, staff edits.

- A **departure** is a start date of a trip plan with a seat ``capacity``, a ``status`` (``scheduled`` →
  ``guaranteed``; ``cancelled`` is final) and ``auto_confirm``.
- **Held / sold are counted, never stored** (:data:`USAGE_SQL`): held = seats of lines of holds that are ``active``
  and not past ``expires_at``; sold = seats of lines of ``confirmed`` holds whose line is not ``declined`` /
  ``cancelled``. An expired hold frees its seats the moment it expires, before the expiry job writes its status.
- **Availability of a line** (:func:`evaluate`, design Q1 6.2): no departure = ``on_request`` (AA ops confirm with the
  supplier); a departure with enough free seats = ``available`` when it auto-confirms, else ``on_request``; not enough
  seats, ``cancelled``, or sales closed = ``sold_out``. Lines of one request on the same departure add up.
- **Sales cutoff** (AA-1073, bk-046): sales close on ``start_date - sales_cutoff_days`` (that day included); cutoff 0
  is the old rule (a departure starting today or earlier is sold out).
- **Allotments** (AA-1073, bk-046, :mod:`app.b2b.allotments`): an agency's allotment in force and not released holds
  ``unused = max(seats - its own held + sold, 0)`` seats back from every other agency. General free seats =
  ``capacity - held - sold - sum(unused)``; an agency can take the general free seats plus its own unused allotment
  (:func:`reserved_for_others`). Counted, never stored, under the same departure lock.
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
d.min_to_operate, d.sales_cutoff_days, d.decision_days_before, d.at_risk_at, d.created_at, d.updated_at
"""
# The columns of a departure row written by the staff endpoints (RETURNING; no table alias there).
RETURNING = """
RETURNING id::text AS id, trip_plan_id::text AS trip_plan_id, start_date, capacity, status, auto_confirm,
          min_to_operate, sales_cutoff_days, decision_days_before, at_risk_at, created_at, updated_at
"""
MAX_CUTOFF_DAYS = 90
MAX_DECISION_DAYS = 365
DEFAULT_DECISION_DAYS = 21

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

INSERT_SQL = (
    """
INSERT INTO booking.departures (trip_plan_id, start_date, capacity, status, auto_confirm, min_to_operate,
                                sales_cutoff_days, decision_days_before, created_by, updated_by)
VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $9)
"""
    + RETURNING
)
UPDATE_SQL = (
    """
UPDATE booking.departures SET capacity = coalesce($2, capacity), status = coalesce($3, status),
       auto_confirm = coalesce($4, auto_confirm), min_to_operate = coalesce($6, min_to_operate),
       at_risk_at = CASE WHEN coalesce($6, min_to_operate) = 0 THEN NULL ELSE at_risk_at END,
       sales_cutoff_days = coalesce($7, sales_cutoff_days), decision_days_before = coalesce($8, decision_days_before),
       updated_by = $5
WHERE id = $1::uuid
"""
    + RETURNING
)
# Allotments in force and not released, with the agency's own seats on the departure (held live + sold): what each
# agency's allotment still holds back from the others (AA-1073, bk-046). Same "live" rule as USAGE_SQL.
ALLOTMENTS_SQL = """
SELECT a.departure_id::text AS departure_id, a.partner_id::text AS partner_id, a.seats::int AS seats,
       a.release_days_before::int AS release_days_before,
       coalesce((
           SELECT sum(hl.seats) FROM booking.b2b_hold_lines hl JOIN booking.b2b_holds hh ON hh.id = hl.hold_id
            WHERE hl.departure_id = a.departure_id AND hh.partner_id = a.partner_id
              AND ((hh.status = 'active' AND hh.expires_at > coalesce($2::timestamptz, now()))
                   OR (hh.status = 'confirmed' AND hl.status NOT IN ('declined', 'cancelled')))), 0)::int AS used
FROM booking.departure_allotments a
WHERE a.departure_id = ANY($1::uuid[]) AND a.approved_at IS NOT NULL AND a.ended_at IS NULL AND a.released_at IS NULL
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


def free_seats(row: Any, held: int, sold: int, reserved: int = 0) -> int:
    """Seats still free: capacity - held - sold - ``reserved`` (the unused allotments the caller may not take)."""
    return max(int(row["capacity"]) - held - sold - reserved, 0)


@dataclass(frozen=True)
class Allotment:
    """One agency's allotment in force and not released, with its own seats on the departure."""

    partner_id: str
    seats: int
    used: int
    release_days_before: int = 0

    @property
    def unused(self) -> int:
        return max(self.seats - self.used, 0)


async def live_allotments(
    conn: asyncpg.Connection, departure_ids: list[str], *, at: datetime | None = None
) -> dict[str, list[Allotment]]:
    """``{departure id: [allotment in force, not released]}`` (AA-1073); a departure without one is absent."""
    if not departure_ids:
        return {}
    out: dict[str, list[Allotment]] = {}
    for r in await conn.fetch(ALLOTMENTS_SQL, departure_ids, at):
        out.setdefault(r["departure_id"], []).append(
            Allotment(r["partner_id"], int(r["seats"]), int(r["used"]), int(r["release_days_before"]))
        )
    return out


def reserved_for_others(allotments: dict[str, list[Allotment]], partner_id: str | None) -> dict[str, int]:
    """``{departure id: unused allotment seats of the agencies other than partner_id}`` (all of them for None)."""
    return {dep: sum(a.unused for a in rows if a.partner_id != partner_id) for dep, rows in allotments.items()}


def sales_close_date(row: Any) -> date:
    """The first day the departure no longer sells: ``start_date - sales_cutoff_days``."""
    return row["start_date"] - timedelta(days=int(row["sales_cutoff_days"]))


def decision_date(row: Any) -> date:
    return row["start_date"] - timedelta(days=int(row["decision_days_before"]))


def evaluate(
    asks: list[SeatAsk],
    rows: dict[str, Any],
    used: dict[str, tuple[int, int]],
    *,
    today: date,
    reserved: dict[str, int] | None = None,
) -> list[tuple[str, str | None, str | None]]:
    """``[(availability, departure id, why sold out)]`` per line (same order), or B2BError 422 ``invalid`` for a
    departure that does not belong to the line. ``reserved``: per departure, the unused allotment seats of the other
    agencies (never offered to this caller)."""
    reserved = reserved or {}
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
        free = free_seats(row, held, sold, reserved.get(dep_id, 0)) - taken.get(dep_id, 0)
        if row["status"] == "cancelled":
            out.append((SOLD_OUT, dep_id, "the departure is cancelled"))
        elif row["start_date"] <= today:
            out.append((SOLD_OUT, dep_id, "the departure has started"))
        elif sales_close_date(row) <= today:
            out.append((SOLD_OUT, dep_id, f"sales closed on {sales_close_date(row)}"))
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
    conn: asyncpg.Connection,
    asks: list[SeatAsk],
    *,
    today: date,
    exclude_hold: str | None = None,
    partner_id: str | None = None,
) -> list[tuple[str, str | None, str | None]]:
    """Availability for a quote (no lock: a quote reserves nothing; the hold re-checks under the lock).
    ``exclude_hold``: leave that hold's seats out (the quote's own proposal version's live hold, which the caller
    releases before it holds the new quote). ``partner_id``: the asking agency (its own allotment is offered to it)."""
    ids = _departure_ids(asks)
    rows = {r["id"]: r for r in await conn.fetch(BY_IDS_SQL, ids)} if ids else {}
    reserved = reserved_for_others(await live_allotments(conn, ids), partner_id)
    return evaluate(asks, rows, await usage(conn, ids, exclude_hold=exclude_hold), today=today, reserved=reserved)


async def lock_departures(
    conn: asyncpg.Connection, asks: list[SeatAsk], *, today: date, partner_id: str | None = None
) -> tuple[list[tuple[str, str | None, str | None]], dict[str, Any]]:
    """Inside a transaction: lock the lines' departures (id order), count, evaluate. Returns the per-line result and
    the locked rows (for the hold expiry cap). ``partner_id``: the holding agency (its own allotment is offered)."""
    ids = _departure_ids(asks)
    rows = {r["id"]: r for r in await conn.fetch(LOCK_SQL, ids)} if ids else {}
    reserved = reserved_for_others(await live_allotments(conn, ids), partner_id)
    return evaluate(asks, rows, await usage(conn, ids), today=today, reserved=reserved), rows


def start_of(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=UTC)


def _allotment_out(row: Any, own: Allotment | None) -> dict[str, Any] | None:
    if own is None:
        return None
    return {
        "seats": own.seats,
        "used": min(own.used, own.seats),
        "available": own.unused,
        "release_date": (row["start_date"] - timedelta(days=own.release_days_before)).isoformat(),
    }


def seats_to_guarantee(row: Any, sold: int) -> int | None:
    """Seats still to sell before the minimum is met: None without a minimum, 0 once met or no longer scheduled."""
    minimum = int(row["min_to_operate"])
    if minimum <= 0:
        return None
    if row["status"] != "scheduled":
        return 0
    return max(minimum - sold, 0)


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


def departure_out(row: Any, held: int, sold: int, *, reserved: int = 0, own: Allotment | None = None) -> dict[str, Any]:
    """The contract ``Departure``. ``available`` is what the calling agency can hold: the general free seats plus its
    own unused allotment (``reserved`` = the other agencies' unused allotments). 1.6 (AA-1073): ``min_to_operate``,
    ``seats_to_guarantee``, ``last_sale_date`` and the caller's ``allotment``."""
    available = 0 if row["status"] == "cancelled" else free_seats(row, held, sold, reserved)
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
        "min_to_operate": int(row["min_to_operate"]),
        "seats_to_guarantee": seats_to_guarantee(row, sold),
        "last_sale_date": (sales_close_date(row) - timedelta(days=1)).isoformat(),
        "allotment": _allotment_out(row, own),
    }


async def list_for_tour(
    pool: asyncpg.Pool,
    *,
    source_system: str,
    source_id: str,
    start: date | None,
    end: date | None,
    today: date,
    partner_id: str | None = None,
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
        ids = [r["id"] for r in rows]
        used = await usage(conn, ids)
        allots = await live_allotments(conn, ids)
    reserved = reserved_for_others(allots, partner_id)
    items = []
    for r in rows:
        own = next((a for a in allots.get(r["id"], []) if a.partner_id == partner_id), None)
        items.append(departure_out(r, *used.get(r["id"], (0, 0)), reserved=reserved.get(r["id"], 0), own=own))
    return {"items": items}


# ── Staff (GET|POST /booking/catalog/trip-plans/{source_id}/departures, PATCH /booking/catalog/departures/{id}) ──


def is_at_risk(row: Any, sold: int) -> bool:
    """At risk on the desk: scheduled, stamped by the watch on its decision date, still short of its minimum."""
    return (
        row["status"] == "scheduled"
        and row["at_risk_at"] is not None
        and int(row["min_to_operate"]) > 0
        and sold < int(row["min_to_operate"])
    )


def staff_out(row: Any, held: int, sold: int, allots: list[Allotment] | None = None) -> dict[str, Any]:
    allots = allots or []
    unused = sum(a.unused for a in allots)
    return {
        "id": row["id"],
        "trip_plan_id": row["trip_plan_id"],
        "start_date": row["start_date"],
        "capacity": int(row["capacity"]),
        "held": held,
        "sold": sold,
        "available": 0 if row["status"] == "cancelled" else free_seats(row, held, sold, unused),
        "allotted": sum(a.seats for a in allots),
        "allotment_unused": unused,
        "status": row["status"],
        "auto_confirm": bool(row["auto_confirm"]),
        "min_to_operate": int(row["min_to_operate"]),
        "seats_to_guarantee": seats_to_guarantee(row, sold),
        "sales_cutoff_days": int(row["sales_cutoff_days"]),
        "decision_days_before": int(row["decision_days_before"]),
        "sales_close_date": sales_close_date(row),
        "decision_date": decision_date(row),
        "at_risk_at": row["at_risk_at"],
        "at_risk": is_at_risk(row, sold),
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
    ids = [r["id"] for r in rows]
    used = await usage(conn, ids)
    allots = await live_allotments(conn, ids)
    return {
        "trip_plan_id": plan_id,
        "source_id": source_id,
        "items": [staff_out(r, *used.get(r["id"], (0, 0)), allots.get(r["id"])) for r in rows],
    }


def _check_capacity(capacity: int) -> None:
    if not 0 <= capacity <= MAX_CAPACITY:
        raise Invalid("invalid_capacity", f"capacity must be between 0 and {MAX_CAPACITY}")


def _check_rules(capacity: int, minimum: int, cutoff: int, decision: int) -> None:
    """The bk-046 CHECKs, as 422 answers before the database refuses them."""
    _check_capacity(capacity)
    if not 0 <= minimum <= capacity:
        raise Invalid("invalid_min_to_operate", "min_to_operate is between 0 (no minimum) and the capacity")
    if not 0 <= cutoff <= MAX_CUTOFF_DAYS:
        raise Invalid("invalid_sales_cutoff", f"sales_cutoff_days is between 0 and {MAX_CUTOFF_DAYS}")
    if not 0 <= decision <= MAX_DECISION_DAYS:
        raise Invalid("invalid_decision_days", f"decision_days_before is between 0 and {MAX_DECISION_DAYS}")


def _rules_of(data: dict[str, Any]) -> tuple[int, int, int, int]:
    return (
        int(data["capacity"]),
        int(data.get("min_to_operate") or 0),
        int(data.get("sales_cutoff_days") or 0),
        int(data["decision_days_before"] if data.get("decision_days_before") is not None else DEFAULT_DECISION_DAYS),
    )


RULE_KEYS = ("capacity", "status", "auto_confirm", "min_to_operate", "sales_cutoff_days", "decision_days_before")


async def create_departure(
    conn: asyncpg.Connection, staff: Staff, source_id: str, data: dict[str, Any]
) -> dict[str, Any]:
    """``data``: ``start_date``, ``capacity``, ``auto_confirm`` (default true), ``status`` (scheduled | guaranteed),
    ``min_to_operate`` (0), ``sales_cutoff_days`` (0), ``decision_days_before`` (21)."""
    plan_id = await _plan_id(conn, source_id)
    capacity, minimum, cutoff, decision = _rules_of(data)
    _check_rules(capacity, minimum, cutoff, decision)
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
            capacity,
            status,
            bool(data.get("auto_confirm", True)),
            minimum,
            cutoff,
            decision,
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
        after={k: row[k] for k in ("start_date", *RULE_KEYS)} | {"source_id": source_id},
        entity_label=f"{source_id[:60]} {row['start_date']}",
        summary=f"Departure {row['start_date']}: {row['capacity']} seats ({status})",
    )
    return staff_out(row, 0, 0)


def _rowcount(status: str | None) -> int:
    """The row count of an asyncpg command status (``UPDATE 3``); 0 when there is none."""
    tail = (status or "").split()[-1:] or ["0"]
    return int(tail[0]) if tail[0].isdigit() else 0


END_ALLOTMENTS_SQL = """
UPDATE booking.departure_allotments SET ended_at = now(), ended_by = $2, end_reason = 'departure_cancelled'
WHERE departure_id = $1::uuid AND ended_at IS NULL
"""


async def update_departure(
    conn: asyncpg.Connection, staff: Staff, departure_id: str, changes: dict[str, Any]
) -> dict[str, Any]:
    """``changes``: any of ``capacity``, ``status``, ``auto_confirm``, ``min_to_operate``, ``sales_cutoff_days``,
    ``decision_days_before``. Capacity never below held + sold + the unused allotments, nor below the minimum;
    cancelled is final and ends the departure's allotments; a status move is scheduled ↔ guaranteed or → cancelled.
    A minimum set back to 0 clears the at-risk stamp (no minimum, nothing at risk)."""
    dep = _uuid(departure_id)
    rows = await conn.fetch(LOCK_SQL, [dep]) if dep else []
    if not rows:
        raise NotFound("departure_not_found", f"departure {departure_id} not found")
    before = rows[0]
    held, sold = (await usage(conn, [before["id"]])).get(before["id"], (0, 0))
    allots = (await live_allotments(conn, [before["id"]])).get(before["id"], [])
    if before["status"] == "cancelled":
        raise Conflict("departure_cancelled", "a cancelled departure cannot change")
    new = {k: changes[k] for k in RULE_KEYS if changes.get(k) is not None}
    if "status" in new and new["status"] not in STATUSES:
        raise Invalid("invalid_status", f"status must be one of {', '.join(STATUSES)}")
    merged = {k: new.get(k, before[k]) for k in RULE_KEYS}
    _check_rules(
        int(merged["capacity"]),
        int(merged["min_to_operate"]),
        int(merged["sales_cutoff_days"]),
        int(merged["decision_days_before"]),
    )
    if "capacity" in new:
        unused = sum(a.unused for a in allots)
        if int(new["capacity"]) < held + sold + unused:
            raise Conflict(
                "capacity_below_taken",
                f"{held} seats are held, {sold} sold and {unused} kept for agency allotments: capacity cannot go lower",
            )
    changed = {k: v for k, v in new.items() if before[k] != v}
    if not changed:
        return staff_out(before, held, sold, allots)
    row = await conn.fetchrow(
        UPDATE_SQL,
        before["id"],
        changed.get("capacity"),
        changed.get("status"),
        changed.get("auto_confirm"),
        staff.cis_user_id,
        changed.get("min_to_operate"),
        changed.get("sales_cutoff_days"),
        changed.get("decision_days_before"),
    )
    ended = 0
    if changed.get("status") == "cancelled":
        ended = _rowcount(await conn.execute(END_ALLOTMENTS_SQL, before["id"], staff.cis_user_id))
        allots = []
    await conn.execute("UPDATE booking.trip_plans SET updated_at = now() WHERE id = $1::uuid", before["trip_plan_id"])
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="departure.updated",
        entity_type="departure",
        entity_id=row["id"],
        before={k: before[k] for k in changed},
        after=changed | ({"allotments_ended": ended} if ended else {}),
        entity_label=str(row["start_date"]),
        summary="Departure " + ", ".join(f"{k} {before[k]} -> {v}" for k, v in changed.items()),
    )
    return staff_out(row, held, sold, allots)


# ── Recurring generator (POST …/trip-plans/{source_id}/departures:generate, AA-1073 bk-02) ──

MAX_GENERATE_DAYS = 400
MAX_GENERATE_DATES = 400

GENERATE_SQL = """
INSERT INTO booking.departures (trip_plan_id, start_date, capacity, status, auto_confirm, min_to_operate,
                                sales_cutoff_days, decision_days_before, created_by, updated_by)
SELECT $1::uuid, d, $3, 'scheduled', $4, $5, $6, $7, $8, $8 FROM unnest($2::date[]) AS d
ON CONFLICT (trip_plan_id, start_date) DO NOTHING
RETURNING start_date
"""


def build_dates(
    date_from: date, date_to: date, *, weekdays: list[int] | None = None, month_day: int | None = None
) -> list[date]:
    """The start dates of a pattern between ``date_from`` and ``date_to`` (both included): ``weekdays`` (ISO 1 = Monday
    .. 7 = Sunday) or ``month_day`` (1..31; a month without that day is skipped, never moved). Pure."""
    if (weekdays is None) == (month_day is None):
        raise Invalid("invalid_pattern", "a pattern is weekly (weekdays) or monthly (day), not both")
    if date_to < date_from:
        raise Invalid("invalid_window", "date_to is on or after date_from")
    if (date_to - date_from).days > MAX_GENERATE_DAYS:
        raise Invalid("invalid_window", f"at most {MAX_GENERATE_DAYS} days from date_from to date_to")
    out: list[date] = []
    if weekdays is not None:
        wanted = set(weekdays)
        if not wanted or not wanted <= set(range(1, 8)):
            raise Invalid("invalid_pattern", "weekdays are ISO days 1 (Monday) to 7 (Sunday), at least one")
        day = date_from
        while day <= date_to:
            if day.isoweekday() in wanted:
                out.append(day)
            day += timedelta(days=1)
    elif month_day is not None:
        if not 1 <= month_day <= 31:
            raise Invalid("invalid_pattern", "day is between 1 and 31")
        year, month = date_from.year, date_from.month
        while (year, month) <= (date_to.year, date_to.month):
            try:
                day = date(year, month, month_day)
            except ValueError:
                day = None  # this month has no such day: skipped
            if day is not None and date_from <= day <= date_to:
                out.append(day)
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    if len(out) > MAX_GENERATE_DATES:
        raise Invalid("too_many_dates", f"at most {MAX_GENERATE_DATES} departures per run")
    return out


async def generate_departures(
    conn: asyncpg.Connection, staff: Staff, source_id: str, data: dict[str, Any], *, today: date | None = None
) -> dict[str, Any]:
    """Create the departures of a weekly or monthly pattern (call inside a transaction). A date the plan already has
    is skipped (``ON CONFLICT DO NOTHING``); ``dry_run`` lists what would be created and writes nothing. One audit row
    per run that created something."""
    today = today or datetime.now(UTC).date()
    plan_id = await _plan_id(conn, source_id)
    pattern = data["pattern"]
    capacity, minimum, cutoff, decision = _rules_of(data)
    _check_rules(capacity, minimum, cutoff, decision)
    if data["date_from"] <= today:
        raise Invalid("invalid_window", "date_from is after today (UTC): a started departure is never sold")
    dates = build_dates(
        data["date_from"],
        data["date_to"],
        weekdays=pattern.get("weekdays") if pattern["kind"] == "weekly" else None,
        month_day=pattern.get("day") if pattern["kind"] == "monthly" else None,
    )
    if not dates:
        raise Invalid("no_dates", "the pattern gives no date in this window")
    auto_confirm = bool(data.get("auto_confirm", True))
    if data.get("dry_run"):
        existing = {
            r["start_date"]
            for r in await conn.fetch(
                "SELECT start_date FROM booking.departures"
                " WHERE trip_plan_id = $1::uuid AND start_date = ANY($2::date[])",
                plan_id,
                dates,
            )
        }
        created = [d for d in dates if d not in existing]
    else:
        rows = await conn.fetch(
            GENERATE_SQL, plan_id, dates, capacity, auto_confirm, minimum, cutoff, decision, staff.cis_user_id
        )
        made = {r["start_date"] for r in rows}
        created = [d for d in dates if d in made]
    skipped = [d for d in dates if d not in set(created)]
    if created and not data.get("dry_run"):
        await conn.execute("UPDATE booking.trip_plans SET updated_at = now() WHERE id = $1::uuid", plan_id)
        await audit.record(
            conn,
            actor_type="staff",
            actor_ref=staff.cis_user_id,
            action="departure.generated",
            entity_type="trip_plan",
            entity_id=plan_id,
            after={
                "source_id": source_id,
                "pattern": pattern,
                "from": data["date_from"],
                "to": data["date_to"],
                "capacity": capacity,
                "min_to_operate": minimum,
                "sales_cutoff_days": cutoff,
                "decision_days_before": decision,
                "auto_confirm": auto_confirm,
                "created": len(created),
                "skipped": len(skipped),
            },
            entity_label=source_id[:60],
            summary=f"{len(created)} departures generated ({len(skipped)} dates already had one)",
        )
    return {
        "dry_run": bool(data.get("dry_run")),
        "created": created,
        "skipped": skipped,
        "created_count": len(created),
        "skipped_count": len(skipped),
    }
