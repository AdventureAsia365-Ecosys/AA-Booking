"""The operations sheet of a departure (AA-1073 rest, bk-048; S275 study item bk-16).

- **Meeting point and time** (``departures.meeting_point``, ``meeting_time``): where and when the group meets on the
  start date (a public place, never a traveller's address). A change on a departure that is not cancelled emits
  ``departure.updated`` to every agency on it (bk-048 trigger; the event names the departure only, the agency reads the
  new values from ``GET /departures``, contract booking.b2b.v1 1.8), so the agency CRM shows them to its travellers.
- **Ops note** (``departures.ops_note``): staff only, never sent.
- **Staff** (``booking.departure_staff``): who works the departure, by role (guide, tour leader, driver): an AA staff
  member or the supplier that provides the person. No guide name, phone or e-mail is stored, and nothing of it is
  sent to an agency (no staff PII in any event).

Read ``b2b_desk.read``, write ``ops.write`` (``app.routers.b2b_desk``). Every change writes one audit row.
"""

from __future__ import annotations

import uuid
from datetime import time
from typing import Any

import asyncpg

from app import audit
from app.auth.staff import Staff
from app.errors import Conflict, Invalid, NotFound

ROLES = ("guide", "tour_leader", "driver")
ROLE_LABEL = {"guide": "Guide", "tour_leader": "Tour leader", "driver": "Driver"}
MAX_POINT = 300
MAX_NOTE = 2000
OPS_FIELDS = ("meeting_point", "meeting_time", "ops_note")

STAFF_SQL = """
SELECT s.id::text AS id, s.role, s.staff_user_id, su.name AS staff_name, s.supplier_id::text AS supplier_id,
       sp.code AS supplier_code, sp.company_name AS supplier_name, s.assigned_by, ab.name AS assigned_by_name,
       s.assigned_at
FROM booking.departure_staff s
LEFT JOIN booking.staff_users su ON su.cis_user_id = s.staff_user_id
LEFT JOIN booking.suppliers sp ON sp.id = s.supplier_id
LEFT JOIN booking.staff_users ab ON ab.cis_user_id = s.assigned_by
"""
OPS_SQL = (
    "SELECT id::text AS id, start_date, status, meeting_point, meeting_time, ops_note FROM booking.departures"
    " WHERE id = $1::uuid"
)
OPS_LOCK_SQL = OPS_SQL + " FOR UPDATE"
STAFF_OPTIONS_SQL = "SELECT cis_user_id AS id, name FROM booking.staff_users WHERE active ORDER BY name NULLS LAST, 1"
# Suppliers that provide guides or drivers (categories of bk-008: guide, transport).
SUPPLIER_OPTIONS_SQL = """
SELECT id::text AS id, code, company_name AS name FROM booking.suppliers
WHERE status = 'active' AND categories && ARRAY['guide', 'transport']::text[]
ORDER BY company_name, id
"""


def _uuid(value: str | None) -> str | None:
    try:
        return str(uuid.UUID(str(value))) if value is not None else None
    except ValueError:
        return None


def staff_row_out(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "role": row["role"],
        "staff": (
            {"id": row["staff_user_id"], "name": row["staff_name"]} if row["staff_user_id"] is not None else None
        ),
        "supplier": (
            {"id": row["supplier_id"], "code": row["supplier_code"], "name": row["supplier_name"]}
            if row["supplier_id"] is not None
            else None
        ),
        "assigned_by": {"id": row["assigned_by"], "name": row["assigned_by_name"]},
        "assigned_at": row["assigned_at"],
    }


async def _departure(conn: asyncpg.Connection, departure_id: str, *, lock: bool = False) -> Any:
    dep = _uuid(departure_id)
    row = await conn.fetchrow(OPS_LOCK_SQL if lock else OPS_SQL, dep) if dep else None
    if row is None:
        raise NotFound("departure_not_found", f"departure {departure_id} not found")
    return row


def ops_out(row: Any) -> dict[str, Any]:
    return {k: row[k] for k in OPS_FIELDS}


async def read_ops(conn: asyncpg.Connection, departure_id: str, *, options: bool = True) -> dict[str, Any]:
    """The ops sheet: meeting point and time, note, the staff on the departure and (``options``) who can be assigned."""
    dep = await _departure(conn, departure_id)
    staff = await conn.fetch(STAFF_SQL + " WHERE s.departure_id = $1::uuid ORDER BY s.role, s.assigned_at", dep["id"])
    out: dict[str, Any] = {**ops_out(dep), "staff": [staff_row_out(r) for r in staff]}
    if options:
        out["staff_options"] = [dict(r) for r in await conn.fetch(STAFF_OPTIONS_SQL)]
        out["supplier_options"] = [dict(r) for r in await conn.fetch(SUPPLIER_OPTIONS_SQL)]
    return out


def clean_ops(changes: dict[str, Any]) -> dict[str, Any]:
    """The ops fields a request sets (present keys only; an empty text clears it), or 422."""
    out: dict[str, Any] = {}
    if "meeting_point" in changes:
        point = (changes["meeting_point"] or "").strip() or None
        if point is not None and len(point) > MAX_POINT:
            raise Invalid("invalid_meeting_point", f"a meeting point of at most {MAX_POINT} characters")
        out["meeting_point"] = point
    if "meeting_time" in changes:
        value = changes["meeting_time"]
        if value is not None and not isinstance(value, time):
            raise Invalid("invalid_meeting_time", "meeting_time is a time of day (HH:MM)")
        out["meeting_time"] = value
    if "ops_note" in changes:
        note = (changes["ops_note"] or "").strip() or None
        if note is not None and len(note) > MAX_NOTE:
            raise Invalid("invalid_ops_note", f"a note of at most {MAX_NOTE} characters")
        out["ops_note"] = note
    return out


async def update_ops(
    conn: asyncpg.Connection, staff: Staff, departure_id: str, changes: dict[str, Any]
) -> dict[str, Any]:
    """Set the meeting point, time and note (call inside a transaction). A changed meeting point or time on a
    departure that is not cancelled tells the agencies on it (trigger). Nothing changed = no write."""
    dep = await _departure(conn, departure_id, lock=True)
    new = clean_ops(changes)
    changed = {k: v for k, v in new.items() if dep[k] != v}
    if changed and dep["status"] == "cancelled" and set(changed) & {"meeting_point", "meeting_time"}:
        raise Conflict("departure_cancelled", "a cancelled departure has no meeting point")
    if changed:
        await conn.execute(
            "UPDATE booking.departures SET meeting_point = $2, meeting_time = $3, ops_note = $4, updated_by = $5"
            " WHERE id = $1::uuid",
            dep["id"],
            changed.get("meeting_point", dep["meeting_point"]),
            changed.get("meeting_time", dep["meeting_time"]),
            changed.get("ops_note", dep["ops_note"]),
            staff.cis_user_id,
        )
        told = bool(set(changed) & {"meeting_point", "meeting_time"})
        await audit.record(
            conn,
            actor_type="staff",
            actor_ref=staff.cis_user_id,
            action="departure.ops_updated",
            entity_type="departure",
            entity_id=dep["id"],
            # The note is staff free text: the audit row says it changed, not what it says.
            before={k: (dep[k] if k != "ops_note" else bool(dep[k])) for k in changed},
            after={k: (v if k != "ops_note" else bool(v)) for k, v in changed.items()},
            entity_label=str(dep["start_date"]),
            summary="Ops sheet: "
            + ", ".join(k.replace("_", " ") for k in changed)
            + (" changed (agencies told)" if told else " changed"),
        )
    return await read_ops(conn, dep["id"], options=False)


async def assign(conn: asyncpg.Connection, staff: Staff, departure_id: str, data: dict[str, Any]) -> dict[str, Any]:
    """Put a staff member (``staff_user_id``) or a supplier (``supplier_id``), exactly one, on the departure in a
    ``role`` (call inside a transaction). The same person twice in one role is 409 ``already_assigned``."""
    dep = await _departure(conn, departure_id)
    if dep["status"] == "cancelled":
        raise Conflict("departure_cancelled", "a cancelled departure needs no staff")
    role = data.get("role")
    if role not in ROLES:
        raise Invalid("invalid_role", f"role is one of {', '.join(ROLES)}")
    staff_user, supplier = data.get("staff_user_id"), _uuid(data.get("supplier_id"))
    if (staff_user is None) == (supplier is None):
        raise Invalid("invalid_assignee", "give staff_user_id or supplier_id, one of them")
    if staff_user is not None and not await conn.fetchval(
        "SELECT active FROM booking.staff_users WHERE cis_user_id = $1", staff_user
    ):
        raise Invalid("staff_not_found", "staff_user_id is not an active staff member")
    if (
        supplier is not None
        and await conn.fetchval("SELECT status FROM booking.suppliers WHERE id = $1::uuid", supplier) != "active"
    ):
        raise Invalid("supplier_not_found", "supplier_id is not an active supplier")
    try:
        async with conn.transaction():
            new_id = await conn.fetchval(
                "INSERT INTO booking.departure_staff (departure_id, role, staff_user_id, supplier_id, assigned_by)"
                " VALUES ($1::uuid, $2, $3, $4::uuid, $5) RETURNING id::text",
                dep["id"],
                role,
                staff_user,
                supplier,
                staff.cis_user_id,
            )
    except asyncpg.UniqueViolationError:
        raise Conflict("already_assigned", f"already on this departure as {ROLE_LABEL[role].lower()}") from None
    row = await conn.fetchrow(STAFF_SQL + " WHERE s.id = $1::uuid", new_id)
    who = row["staff_name"] or row["supplier_name"] or "someone"
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="departure.staff_assigned",
        entity_type="departure",
        entity_id=dep["id"],
        after={"assignment_id": new_id, "role": role, "staff_user_id": staff_user, "supplier_id": supplier},
        entity_label=str(dep["start_date"]),
        summary=f"{ROLE_LABEL[role]}: {who} assigned",
    )
    return staff_row_out(row)


async def unassign(conn: asyncpg.Connection, staff: Staff, assignment_id: str) -> dict[str, Any]:
    """Take one assignment off its departure (call inside a transaction). The audit row keeps who it was."""
    ident = _uuid(assignment_id)
    row = await conn.fetchrow(STAFF_SQL + " WHERE s.id = $1::uuid", ident) if ident else None
    if row is None:
        raise NotFound("assignment_not_found", f"assignment {assignment_id} not found")
    departure_id = await conn.fetchval(
        "DELETE FROM booking.departure_staff WHERE id = $1::uuid RETURNING departure_id::text", row["id"]
    )
    who = row["staff_name"] or row["supplier_name"] or "someone"
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="departure.staff_unassigned",
        entity_type="departure",
        entity_id=departure_id,
        before={
            "assignment_id": row["id"],
            "role": row["role"],
            "staff_user_id": row["staff_user_id"],
            "supplier_id": row["supplier_id"],
        },  # fmt: skip
        summary=f"{ROLE_LABEL[row['role']]}: {who} unassigned",
    )
    return {"id": row["id"], "departure_id": departure_id, "removed": True}
