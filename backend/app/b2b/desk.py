"""The B2B desk of the Booking staff admin (AA-975, bk-022): on-request lines and departure incidents.

**On-request desk** (design §2.5 "accept / decline within 1 business day", memo seam 10). A line of a confirmed B2B
booking that was held ``on_request`` (no departure, or a departure without ``auto_confirm``) waits here for AA ops,
who check with the supplier and then:

- **accept** → the line is ``confirmed`` (event ``line.confirmed``);
- **decline** with a ``decline_code`` → ``declined`` (event ``line.declined``; its seats are free again, since sold
  seats leave out declined lines).

The deadline ``answer_due_at`` is set when the booking is confirmed: ``b2b_on_request_answer_business_days`` (1)
business days, Mon-Fri, in ``b2b_ops_timezone`` (Asia/Ho_Chi_Minh), by the SQL function ``booking.b2b_answer_due``
(the database has the time-zone rules; the image does not ship tzdata). The **SLA badge** of an open line is
``overdue`` (past the deadline), ``due_soon`` (at most :data:`DUE_SOON_HOURS` left) or ``on_time``. Answering is
idempotent (the same answer again returns the line); another answer on an answered line is 409. A cancelled booking's
lines are ``cancelled`` (AA-974) and leave the desk. Pre-confirm holds are not on the desk: the agency has not paid
its deposit, so ops do no supplier work yet (the hold length stays ``b2b_hold_hours``).

**Incident flag** (design Fix B3: the button lives here, CRM S-02 links to it). Staff flag an incident on a departure
with a code from the contract list (weather, road, health, political, supplier, other) and clear it later; at most one
open incident per departure. The bk-022 trigger sends ``departure.incident`` / ``departure.incident_cleared`` to every
agency with seats on it. No free text anywhere, so an incident never carries traveller data (Linear AA-975 E).

Permissions: ``b2b_desk.read`` / ``b2b_desk.write`` (admin, sales, contracting; ``app.auth.staff``). Every answer,
flag and clear writes an audit row.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import asyncpg

from app import audit
from app.auth.staff import Staff
from app.b2b import departures
from app.b2b.components import PREFIX_PLAN, PREFIX_ROOM, PREFIX_SERVICE, parse_component
from app.catalog.destinations import CATALOG_ERRORS
from app.errors import Conflict, Invalid, NotFound
from app.notifications import service as notifications

log = logging.getLogger(__name__)

INCIDENT_CODES = ("weather", "road", "health", "political", "supplier", "other")
DECLINE_CODES = ("sold_out", "supplier_unavailable", "minimum_not_met", "other")
STATES = ("open", "answered", "all")
DUE_SOON_HOURS = 4
DAYS_SETTING = "b2b_on_request_answer_business_days"
TZ_SETTING = "b2b_ops_timezone"
DEFAULT_DAYS = 1
MAX_DAYS = 10
DEFAULT_TZ = "Asia/Ho_Chi_Minh"
NOTIFICATION_TYPE = "b2b.on_request"
MAX_PAGE = 100
DEFAULT_WINDOW_BEFORE = 14
DEFAULT_WINDOW_AFTER = 90
MAX_WINDOW = 400


def _uuid(value: str) -> str | None:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        return None


async def _setting(conn: asyncpg.Connection, key: str) -> Any:
    raw = await conn.fetchval("SELECT value FROM booking.settings WHERE key = $1", key)
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return None


async def answer_policy(conn: asyncpg.Connection) -> tuple[int, str]:
    """(business days, time zone) of the on-request SLA, falling back to the defaults on a bad settings value."""
    days, tz = await _setting(conn, DAYS_SETTING), await _setting(conn, TZ_SETTING)
    days = days if isinstance(days, int) and not isinstance(days, bool) and 1 <= days <= MAX_DAYS else DEFAULT_DAYS
    if not isinstance(tz, str) or not tz or len(tz) > 64:
        return days, DEFAULT_TZ
    try:
        async with conn.transaction():  # a savepoint: an unknown zone must not abort the confirm
            await conn.fetchval("SELECT now() AT TIME ZONE $1", tz)
    except asyncpg.PostgresError:
        log.warning("b2b_ops_timezone_invalid value=%r", tz[:64])
        return days, DEFAULT_TZ
    return days, tz


def sla_state(status: str, due: datetime | None, now: datetime) -> str:
    """``answered`` / ``closed`` once the line left ``on_request``; else ``overdue`` / ``due_soon`` / ``on_time``."""
    if status in ("confirmed", "declined"):
        return "answered"
    if status != "on_request":
        return "closed"
    if due is None:
        return "on_time"
    if now >= due:
        return "overdue"
    return "due_soon" if due - now <= timedelta(hours=DUE_SOON_HOURS) else "on_time"


# ── Opening requests (called by confirm, app.b2b.holds) ──────────────────────

OPEN_SQL = """
UPDATE booking.b2b_hold_lines SET answer_due_at = booking.b2b_answer_due(now(), $2, $3)
WHERE hold_id = $1::uuid AND status = 'on_request' AND answer_due_at IS NULL
RETURNING line_ref, answer_due_at
"""


async def open_requests(conn: asyncpg.Connection, *, hold_id: str, case_id: str, case_code: str) -> int:
    """Set the answer deadline of the confirmed hold's on-request lines and tell the staff queue. Returns the count."""
    days, tz = await answer_policy(conn)
    rows = await conn.fetch(OPEN_SQL, hold_id, days, tz)
    if not rows:
        return 0
    due = min(r["answer_due_at"] for r in rows)
    refs = sorted(r["line_ref"] for r in rows)
    await notifications.notify_staff(
        conn,
        type=NOTIFICATION_TYPE,
        title=f"B2B on-request lines on {case_code}",
        message=f"{len(rows)} line(s) need an accept or decline by {due.astimezone(UTC):%Y-%m-%d %H:%M} UTC",
        entity_type="trip_case",
        entity_id=case_id,
        data={"code": case_code, "hold_id": hold_id, "line_refs": refs, "answer_due_at": due},
    )
    return len(rows)


# ── On-request list ──────────────────────────────────────────────────────────

LINE_COLUMNS = """
l.hold_id::text AS hold_id, l.line_no, l.line_ref, l.status, l.seats, l.departure_id::text AS departure_id,
l.answer_due_at, l.answered_at, l.decline_code, l.answered_by, s.name AS answered_by_name,
h.confirmed_at, h.trip_case_id::text AS trip_case_id, c.code AS trip_case_code, h.proposal_ref,
p.id::text AS partner_id, p.agency_name, p.tenant_id::text AS tenant_id,
q.component_id, q.component_kind, q.service_date, q.nights, q.pax_adult, q.pax_child, q.pax_infant,
d.start_date AS departure_start,
(SELECT sb.code FROM booking.supplier_bookings sb WHERE sb.b2b_hold_id = l.hold_id AND sb.b2b_line_ref = l.line_ref
   AND sb.status IN ('pending', 'requested', 'confirmed')) AS supplier_booking
"""
LINE_FROM = """
FROM booking.b2b_hold_lines l
JOIN booking.b2b_holds h ON h.id = l.hold_id AND h.status = 'confirmed'
JOIN booking.trip_cases c ON c.id = h.trip_case_id
JOIN booking.b2b_partners p ON p.id = h.partner_id
JOIN booking.b2b_quote_lines q ON q.quote_id = h.quote_id AND q.line_no = l.line_no
LEFT JOIN booking.departures d ON d.id = l.departure_id
LEFT JOIN booking.staff_users s ON s.cis_user_id = l.answered_by
"""
STATE_WHERE = {
    "open": "l.held_as = 'on_request' AND l.status = 'on_request'",
    "answered": "l.held_as = 'on_request' AND l.answered_at IS NOT NULL",
    "all": "l.held_as = 'on_request'",
}
STATE_ORDER = {
    "open": "l.answer_due_at NULLS LAST, h.confirmed_at, l.hold_id, l.line_no",
    "answered": "l.answered_at DESC, l.hold_id, l.line_no",
    "all": "h.confirmed_at DESC, l.hold_id, l.line_no",
}


async def _labels(conn: asyncpg.Connection, component_ids: list[str]) -> dict[str, dict[str, Any]]:
    """What a component is, for ops: hotel + room type, service title, or tour title; and the supplier."""
    rooms, services, plans = [], [], []
    for cid in set(component_ids):
        try:
            prefix, ident = parse_component(cid)
        except ValueError:
            continue
        {PREFIX_ROOM: rooms, PREFIX_SERVICE: services, PREFIX_PLAN: plans}[prefix].append(ident)
    out: dict[str, dict[str, Any]] = {}
    if rooms:
        for r in await conn.fetch(
            "SELECT rt.id::text AS id, h.name || ' · ' || rt.name AS label, s.id::text AS supplier_id,"
            " s.company_name AS supplier FROM booking.hotel_room_types rt JOIN booking.hotels h ON h.id = rt.hotel_id"
            " JOIN booking.suppliers s ON s.id = h.supplier_id WHERE rt.id = ANY($1::uuid[])",
            rooms,
        ):
            out[f"{PREFIX_ROOM}:{r['id']}"] = dict(r)
    if services:
        for r in await conn.fetch(
            "SELECT v.id::text AS id, v.title AS label, s.id::text AS supplier_id, s.company_name AS supplier"
            " FROM booking.services v JOIN booking.suppliers s ON s.id = v.supplier_id WHERE v.id = ANY($1::uuid[])",
            services,
        ):
            out[f"{PREFIX_SERVICE}:{r['id']}"] = dict(r)
    if plans:
        keys = {
            r["id"]: r["source_id"]
            for r in await conn.fetch(
                "SELECT id::text AS id, source_id FROM booking.trip_plans WHERE id = ANY($1::uuid[])", plans
            )
        }
        titles = await tour_titles(conn, list(keys.values()))
        for plan_id, source_id in keys.items():
            out[f"{PREFIX_PLAN}:{plan_id}"] = {
                "id": plan_id,
                "label": titles.get(source_id) or f"Tour {source_id[:8]}",
                "supplier_id": None,
                "supplier": None,
                "source_id": source_id,
            }
    return out


async def tour_titles(conn: asyncpg.Connection, source_ids: list[str]) -> dict[str, str]:
    """CIS catalog titles by source_id; empty when the catalog cannot be read (the desk still works)."""
    if not source_ids:
        return {}
    try:
        async with conn.transaction():  # a savepoint: a catalog error must not abort the caller's transaction
            rows = await conn.fetch(
                "SELECT source_id, title FROM catalog.v_tour WHERE source_system = 'cis' AND source_id = ANY($1)",
                sorted(set(source_ids)),
            )
    except CATALOG_ERRORS:
        return {}
    return {r["source_id"]: r["title"] for r in rows}


def line_out(row: Any, labels: dict[str, dict[str, Any]], now: datetime) -> dict[str, Any]:
    label = labels.get(row["component_id"]) or {}
    return {
        "hold_id": row["hold_id"],
        "line_ref": row["line_ref"],
        "status": row["status"],
        "sla": sla_state(row["status"], row["answer_due_at"], now),
        "answer_due_at": row["answer_due_at"],
        "requested_at": row["confirmed_at"],
        "answered_at": row["answered_at"],
        "answered_by": (
            {"id": row["answered_by"], "name": row["answered_by_name"]} if row["answered_by"] is not None else None
        ),
        "decline_code": row["decline_code"],
        "trip_case": {"id": row["trip_case_id"], "code": row["trip_case_code"]},
        "proposal_ref": row["proposal_ref"],
        "partner": {"id": row["partner_id"], "agency_name": row["agency_name"], "tenant_id": row["tenant_id"]},
        "component": {
            "id": row["component_id"],
            "kind": row["component_kind"],
            "label": label.get("label"),
            "supplier": ({"id": label["supplier_id"], "name": label["supplier"]} if label.get("supplier_id") else None),
        },
        "service_date": row["service_date"],
        "nights": row["nights"],
        "pax": {"adult": row["pax_adult"], "child": row["pax_child"], "infant": row["pax_infant"]},
        "seats": row["seats"],
        "departure": (
            {"id": row["departure_id"], "start_date": row["departure_start"]} if row["departure_id"] else None
        ),
        # AA-682 D13: a live supplier booking answers this line from the case's Suppliers tab.
        "supplier_booking": row.get("supplier_booking"),
    }


async def list_on_request(
    conn: asyncpg.Connection, *, state: str = "open", limit: int = 50, offset: int = 0, now: datetime | None = None
) -> dict[str, Any]:
    if state not in STATES:
        raise Invalid("invalid_state", f"state must be one of {', '.join(STATES)}")
    if not 1 <= limit <= MAX_PAGE or offset < 0:
        raise Invalid("invalid_page", f"limit is 1..{MAX_PAGE} and offset >= 0")
    now = now or datetime.now(UTC)
    where = STATE_WHERE[state]
    total = await conn.fetchval(f"SELECT count(*) {LINE_FROM} WHERE {where}")  # noqa: S608 — constant fragments
    rows = await conn.fetch(
        f"SELECT {LINE_COLUMNS} {LINE_FROM} WHERE {where} ORDER BY {STATE_ORDER[state]} LIMIT $1 OFFSET $2",  # noqa: S608
        limit,
        offset,
    )
    labels = await _labels(conn, [r["component_id"] for r in rows])
    return {
        "items": [line_out(r, labels, now) for r in rows],
        "total": int(total or 0),
        "limit": limit,
        "offset": offset,
        "state": state,
    }


async def _line(conn: asyncpg.Connection, hold_id: str, line_ref: str, now: datetime) -> dict[str, Any]:
    row = await conn.fetchrow(
        f"SELECT {LINE_COLUMNS} {LINE_FROM} WHERE l.hold_id = $1::uuid AND l.line_ref = $2",  # noqa: S608
        hold_id,
        line_ref,
    )
    if row is None:
        raise NotFound("line_not_found", "no on-request line of a confirmed B2B booking with this hold and line_ref")
    return line_out(row, await _labels(conn, [row["component_id"]]), now)


async def answer_line(
    conn: asyncpg.Connection,
    staff: Staff,
    hold_id: str,
    line_ref: str,
    *,
    accept: bool,
    decline_code: str | None = None,
    via_supplier_booking: bool = False,
) -> dict[str, Any]:
    """Accept or decline an on-request line (call inside a transaction). The hold row is locked first, as a cancel
    locks it (AA-974), so an answer and a cancel of the same booking run one after the other.

    AA-682 D13 (bk-026): the desk and the supplier booking are one action. A line with a live supplier booking is
    answered from the case's Suppliers tab (its confirm / decline call this with ``via_supplier_booking``); the desk
    route answers it 409 ``answer_from_suppliers_tab`` so the two never disagree."""
    hold = _uuid(hold_id)
    if hold is None:
        raise NotFound("line_not_found", "no such hold")
    if accept:
        decline_code = None
    elif decline_code not in DECLINE_CODES:
        raise Invalid("invalid_decline_code", f"decline_code must be one of {', '.join(DECLINE_CODES)}")
    locked = await conn.fetchrow("SELECT status FROM booking.b2b_holds WHERE id = $1::uuid FOR UPDATE", hold)
    line = await conn.fetchrow(
        "SELECT held_as, status, decline_code FROM booking.b2b_hold_lines WHERE hold_id = $1::uuid AND line_ref = $2"
        " FOR UPDATE",
        hold,
        line_ref,
    )
    if locked is None or line is None or line["held_as"] != "on_request" or locked["status"] != "confirmed":
        raise NotFound("line_not_found", "no on-request line of a confirmed B2B booking with this hold and line_ref")
    target = "confirmed" if accept else "declined"
    if line["status"] == target and (accept or line["decline_code"] == decline_code):
        return await _line(conn, hold, line_ref, datetime.now(UTC))  # the same answer again
    if line["status"] != "on_request":
        raise Conflict("line_answered", f"the line is already {line['status']}; it cannot be answered again")
    if not via_supplier_booking:
        booking = await conn.fetchval(
            "SELECT code FROM booking.supplier_bookings WHERE b2b_hold_id = $1::uuid AND b2b_line_ref = $2"
            " AND status IN ('pending', 'requested', 'confirmed')",
            hold,
            line_ref,
        )
        if booking is not None:
            raise Conflict(
                "answer_from_suppliers_tab",
                f"supplier booking {booking} covers this line: confirm or decline it on the case's Suppliers tab",
                supplier_booking=booking,
            )
    await conn.execute(
        "UPDATE booking.b2b_hold_lines SET status = $3, decline_code = $4, answered_at = now(), answered_by = $5"
        " WHERE hold_id = $1::uuid AND line_ref = $2",
        hold,
        line_ref,
        target,
        decline_code,
        staff.cis_user_id,
    )
    out = await _line(conn, hold, line_ref, datetime.now(UTC))
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="b2b_line.accepted" if accept else "b2b_line.declined",
        entity_type="trip_case",
        entity_id=out["trip_case"]["id"],
        before={"line_ref": line_ref, "status": "on_request"},
        after={"line_ref": line_ref, "status": target, "decline_code": decline_code},
        entity_label=out["trip_case"]["code"],
        summary=(
            f"On-request line {line_ref} accepted"
            if accept
            else f"On-request line {line_ref} declined ({decline_code})"
        ),
    )
    log.info("b2b_line_answered hold=%s line=%s status=%s", hold, line_ref, target)
    return out


# ── Summary (dashboard tile, page header) ────────────────────────────────────

SUMMARY_SQL = """
SELECT count(*) FILTER (WHERE l.status = 'on_request')::int AS open,
       count(*) FILTER (WHERE l.status = 'on_request' AND l.answer_due_at <= $1)::int AS overdue,
       count(*) FILTER (WHERE l.status = 'on_request' AND l.answer_due_at > $1 AND l.answer_due_at <= $2)::int
           AS due_soon
FROM booking.b2b_hold_lines l
JOIN booking.b2b_holds h ON h.id = l.hold_id AND h.status = 'confirmed'
WHERE l.held_as = 'on_request'
"""


async def summary(conn: asyncpg.Connection, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    row = await conn.fetchrow(SUMMARY_SQL, now, now + timedelta(hours=DUE_SOON_HOURS))
    incidents = await conn.fetchval("SELECT count(*)::int FROM booking.departure_incidents WHERE cleared_at IS NULL")
    return {
        "on_request": {"open": row["open"], "overdue": row["overdue"], "due_soon": row["due_soon"]},
        "open_incidents": int(incidents or 0),
        "due_soon_hours": DUE_SOON_HOURS,
    }


# ── Departures and incidents ─────────────────────────────────────────────────

INCIDENT_COLUMNS = """
i.id::text AS id, i.departure_id::text AS departure_id, i.incident_code, i.flagged_at, i.flagged_by,
f.name AS flagged_by_name, i.cleared_at, i.cleared_by, k.name AS cleared_by_name
"""
INCIDENT_FROM = """
FROM booking.departure_incidents i
LEFT JOIN booking.staff_users f ON f.cis_user_id = i.flagged_by
LEFT JOIN booking.staff_users k ON k.cis_user_id = i.cleared_by
"""
# Partners with seats on a departure: the same rule as the bk-022 fan-out (booking.outbox_departure_event).
PARTNERS_SQL = """
SELECT l.departure_id::text AS departure_id, count(DISTINCT h.partner_id)::int AS partners
FROM booking.b2b_hold_lines l
JOIN booking.b2b_holds h ON h.id = l.hold_id
WHERE l.departure_id = ANY($1::uuid[])
  AND ((h.status = 'active' AND h.expires_at > now())
       OR (h.status = 'confirmed' AND l.status NOT IN ('declined', 'cancelled')))
GROUP BY l.departure_id
"""


def incident_out(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "departure_id": row["departure_id"],
        "incident_code": row["incident_code"],
        "flagged_at": row["flagged_at"],
        "flagged_by": {"id": row["flagged_by"], "name": row["flagged_by_name"]},
        "cleared_at": row["cleared_at"],
        "cleared_by": (
            {"id": row["cleared_by"], "name": row["cleared_by_name"]} if row["cleared_by"] is not None else None
        ),
    }


async def _partners(conn: asyncpg.Connection, departure_ids: list[str]) -> dict[str, int]:
    if not departure_ids:
        return {}
    return {r["departure_id"]: r["partners"] for r in await conn.fetch(PARTNERS_SQL, departure_ids)}


async def list_departures(
    conn: asyncpg.Connection, *, start: date | None = None, end: date | None = None, today: date | None = None
) -> dict[str, Any]:
    """Departures of every plan between ``start`` and ``end`` (default: 14 days ago to 90 days ahead, so the ones
    travelling now show) with seats, the agencies on them and the open incident."""
    today = today or datetime.now(UTC).date()
    start = start or today - timedelta(days=DEFAULT_WINDOW_BEFORE)
    end = end or today + timedelta(days=DEFAULT_WINDOW_AFTER)
    if end < start or (end - start).days > MAX_WINDOW:
        raise Invalid("invalid_window", f"to is on or after from, at most {MAX_WINDOW} days apart")
    rows = await conn.fetch(
        f"SELECT {departures.DEPARTURE_COLUMNS}, p.source_id FROM booking.departures d"  # noqa: S608
        " JOIN booking.trip_plans p ON p.id = d.trip_plan_id"
        " WHERE d.start_date BETWEEN $1 AND $2 ORDER BY d.start_date, d.id",
        start,
        end,
    )
    ids = [r["id"] for r in rows]
    used = await departures.usage(conn, ids) if ids else {}
    partners = await _partners(conn, ids)
    open_incidents = (
        {
            r["departure_id"]: incident_out(r)
            for r in await conn.fetch(
                f"SELECT {INCIDENT_COLUMNS} {INCIDENT_FROM}"  # noqa: S608
                " WHERE i.departure_id = ANY($1::uuid[]) AND i.cleared_at IS NULL",
                ids,
            )
        }
        if ids
        else {}
    )
    titles = await tour_titles(conn, [r["source_id"] for r in rows])
    items = []
    for r in rows:
        held, sold = used.get(r["id"], (0, 0))
        items.append(
            departures.staff_out(r, held, sold)
            | {
                "source_id": r["source_id"],
                "tour_title": titles.get(r["source_id"]),
                "partners": partners.get(r["id"], 0),
                "open_incident": open_incidents.get(r["id"]),
            }
        )
    return {"items": items, "from": start, "to": end}


async def list_incidents(conn: asyncpg.Connection, departure_id: str) -> dict[str, Any]:
    dep = _uuid(departure_id)
    if dep is None or await conn.fetchval("SELECT 1 FROM booking.departures WHERE id = $1::uuid", dep) is None:
        raise NotFound("departure_not_found", f"departure {departure_id} not found")
    rows = await conn.fetch(
        f"SELECT {INCIDENT_COLUMNS} {INCIDENT_FROM} WHERE i.departure_id = $1::uuid ORDER BY i.flagged_at DESC",  # noqa: S608
        dep,
    )
    return {"items": [incident_out(r) for r in rows]}


async def flag_incident(
    conn: asyncpg.Connection, staff: Staff, departure_id: str, incident_code: str
) -> dict[str, Any]:
    """Flag an incident on a departure (call inside a transaction). 409 when it is cancelled or already has an open
    incident (clear that one first). The trigger tells every agency with seats on it (``departure.incident``)."""
    if incident_code not in INCIDENT_CODES:
        raise Invalid("invalid_incident_code", f"incident_code must be one of {', '.join(INCIDENT_CODES)}")
    dep = _uuid(departure_id)
    rows = await conn.fetch(departures.LOCK_SQL, [dep]) if dep else []
    if not rows:
        raise NotFound("departure_not_found", f"departure {departure_id} not found")
    departure = rows[0]
    if departure["status"] == "cancelled":
        raise Conflict("departure_cancelled", "a cancelled departure takes no incident")
    open_id = await conn.fetchval(
        "SELECT id::text FROM booking.departure_incidents WHERE departure_id = $1::uuid AND cleared_at IS NULL", dep
    )
    if open_id is not None:
        raise Conflict(
            "incident_open", "this departure already has an open incident; clear it first", incident_id=open_id
        )
    partners = (await _partners(conn, [dep])).get(dep, 0)
    new_id = await conn.fetchval(
        "INSERT INTO booking.departure_incidents (departure_id, incident_code, flagged_by)"
        " VALUES ($1::uuid, $2, $3) RETURNING id::text",
        dep,
        incident_code,
        staff.cis_user_id,
    )
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="departure.incident_flagged",
        entity_type="departure",
        entity_id=dep,
        after={"incident_id": new_id, "incident_code": incident_code, "partners_notified": partners},
        entity_label=str(departure["start_date"]),
        summary=f"Incident ({incident_code}) flagged on departure {departure['start_date']}; {partners} agencies told",
    )
    row = await conn.fetchrow(f"SELECT {INCIDENT_COLUMNS} {INCIDENT_FROM} WHERE i.id = $1::uuid", new_id)  # noqa: S608
    log.info("departure_incident_flagged departure=%s code=%s partners=%s", dep, incident_code, partners)
    return incident_out(row) | {"partners_notified": partners}


async def clear_incident(conn: asyncpg.Connection, staff: Staff, incident_id: str) -> dict[str, Any]:
    """Clear an open incident (call inside a transaction); clearing a cleared one returns it unchanged."""
    ident = _uuid(incident_id)
    row = (
        await conn.fetchrow(
            "SELECT id::text AS id, departure_id::text AS departure_id, incident_code, cleared_at"
            " FROM booking.departure_incidents WHERE id = $1::uuid FOR UPDATE",
            ident,
        )
        if ident
        else None
    )
    if row is None:
        raise NotFound("incident_not_found", f"incident {incident_id} not found")
    if row["cleared_at"] is None:
        partners = (await _partners(conn, [row["departure_id"]])).get(row["departure_id"], 0)
        await conn.execute(
            "UPDATE booking.departure_incidents SET cleared_at = greatest(now(), flagged_at), cleared_by = $2"
            " WHERE id = $1::uuid",
            ident,
            staff.cis_user_id,
        )
        await audit.record(
            conn,
            actor_type="staff",
            actor_ref=staff.cis_user_id,
            action="departure.incident_cleared",
            entity_type="departure",
            entity_id=row["departure_id"],
            before={"incident_id": ident, "incident_code": row["incident_code"], "cleared_at": None},
            after={"incident_id": ident, "cleared": True, "partners_notified": partners},
            entity_label=f"incident {ident[:8]}",
            summary=f"Incident ({row['incident_code']}) cleared; {partners} agencies told",
        )
    out = await conn.fetchrow(f"SELECT {INCIDENT_COLUMNS} {INCIDENT_FROM} WHERE i.id = $1::uuid", ident)  # noqa: S608
    return incident_out(out)
