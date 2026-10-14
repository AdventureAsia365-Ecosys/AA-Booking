"""Trip case reads and staff writes (AA-681 slice 3): list, detail, edit, status, assignment.

Plain SQL on asyncpg. Every write runs in the caller's transaction, locks the trip case row first
(``FOR UPDATE``) and writes one ``booking.audit_logs`` row. Status rules live in the database
(bk-003 ``trip_case_status_transitions`` + trigger); this module reads the same table so it can
answer with the allowed next statuses instead of a bare trigger error.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import asyncpg

from app import audit
from app.auth.staff import Staff
from app.errors import Conflict, Invalid, NotFound

STATUSES = (
    "new_inquiry",
    "discovery",
    "building",
    "proposal_sent",
    "confirmed",
    "supplier_confirming",
    "invoice_sent",
    "payment_pending",
    "paid",
    "pre_departure",
    "on_trip",
    "completed",
    "cancelled",
)
TERMINAL_STATUSES = frozenset({"cancelled", "completed"})
REASON_REQUIRED = frozenset({"cancelled"})
ASSIGNMENT_ROLES = ("sales", "contracting", "ops", "guide")
EDITABLE_FIELDS = ("pax", "start_date", "currency", "notes")

CASE_COLUMNS = """
    tc.id::text AS id, tc.code, tc.status, tc.customer_id::text AS customer_id, tc.trip_style,
    tc.start_date, tc.end_date, tc.pax, tc.currency, tc.notes, tc.source_system, tc.source_id,
    tc.created_at, tc.updated_at
"""
CUSTOMER_COLUMNS = """
    c.name AS customer_name, c.email AS customer_email, c.phone AS customer_phone,
    c.verified AS customer_verified, c.needs_merge_review AS customer_needs_merge_review
"""
OPEN_ASSIGNEES = """
    (SELECT coalesce(json_agg(json_build_object(
                'cis_user_id', a.staff_user_id, 'role', a.role, 'name', s.name, 'assigned_at', a.assigned_at)
            ORDER BY a.role), '[]'::json)::text
     FROM booking.staff_assignments a JOIN booking.staff_users s ON s.cis_user_id = a.staff_user_id
     WHERE a.trip_case_id = tc.id AND a.unassigned_at IS NULL) AS assignees
"""


def _case_filter(ref: str, placeholder: str = "$1") -> tuple[str, str]:
    """A trip case is addressed by its UUID or its code (``TC-000123``, case-insensitive)."""
    try:
        return f"tc.id = {placeholder}::uuid", str(uuid.UUID(ref))
    except ValueError:
        return f"tc.code = {placeholder}", ref.strip().upper()


def _case_dict(row: Any) -> dict[str, Any]:
    out = dict(row)
    if "assignees" in out:
        out["assignees"] = json.loads(out["assignees"] or "[]")
    return out


async def resolve_case(conn: asyncpg.Connection, ref: str, *, lock: bool = False) -> Any:
    where, arg = _case_filter(ref)
    row = await conn.fetchrow(
        f"SELECT {CASE_COLUMNS}, tc.inquiry_snapshot::text AS inquiry_snapshot FROM booking.trip_cases tc"  # noqa: S608
        f" WHERE {where} AND tc.deleted_at IS NULL{' FOR UPDATE' if lock else ''}",
        arg,
    )
    if row is None:
        raise NotFound("trip_case_not_found", f"trip case {ref} not found")
    return row


async def list_cases(
    conn: asyncpg.Connection,
    *,
    statuses: list[str] | None = None,
    assignee: str | None = None,
    limit: int = 25,
    offset: int = 0,
) -> dict[str, Any]:
    """``assignee``: a CIS user id (open assignment in any role), or ``none`` for unassigned cases."""
    where = ["tc.deleted_at IS NULL"]
    args: list[Any] = []
    if statuses:
        unknown = sorted(set(statuses) - set(STATUSES))
        if unknown:
            raise Invalid("unknown_status", f"unknown status {', '.join(unknown)}")
        args.append(statuses)
        where.append(f"tc.status = ANY(${len(args)}::text[])")
    if assignee == "none":
        where.append(
            "NOT EXISTS (SELECT 1 FROM booking.staff_assignments a"
            " WHERE a.trip_case_id = tc.id AND a.unassigned_at IS NULL)"
        )
    elif assignee:
        args.append(assignee)
        where.append(
            "EXISTS (SELECT 1 FROM booking.staff_assignments a WHERE a.trip_case_id = tc.id"  # noqa: S608 — placeholder
            f" AND a.unassigned_at IS NULL AND a.staff_user_id = ${len(args)})"
        )
    clause = " AND ".join(where)
    total = await conn.fetchval(f"SELECT count(*) FROM booking.trip_cases tc WHERE {clause}", *args)  # noqa: S608
    rows = await conn.fetch(
        f"""
        SELECT {CASE_COLUMNS}, {CUSTOMER_COLUMNS}, {OPEN_ASSIGNEES},
               (SELECT count(*) FROM booking.trip_case_legs l WHERE l.trip_case_id = tc.id) AS leg_count,
               (SELECT max(v.version_number) FROM booking.trip_case_versions v WHERE v.trip_case_id = tc.id)
                   AS latest_version
        FROM booking.trip_cases tc JOIN booking.customers c ON c.id = tc.customer_id
        WHERE {clause}
        ORDER BY tc.created_at DESC, tc.id
        LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}
        """,  # noqa: S608 — fixed fragments, values are bound
        *args,
        limit,
        offset,
    )
    return {"items": [_case_dict(r) for r in rows], "total": total, "limit": limit, "offset": offset}


async def allowed_transitions(conn: asyncpg.Connection, status: str) -> list[str]:
    rows = await conn.fetch(
        "SELECT to_status FROM booking.trip_case_status_transitions WHERE from_status = $1 ORDER BY to_status", status
    )
    return [r["to_status"] for r in rows]


async def get_case(conn: asyncpg.Connection, ref: str) -> dict[str, Any]:
    where, arg = _case_filter(ref)
    row = await conn.fetchrow(
        f"""
        SELECT {CASE_COLUMNS}, {CUSTOMER_COLUMNS}, {OPEN_ASSIGNEES},
               tc.inquiry_snapshot::text AS inquiry_snapshot, tc.inbox_event_id::text AS inbox_event_id
        FROM booking.trip_cases tc JOIN booking.customers c ON c.id = tc.customer_id
        WHERE {where} AND tc.deleted_at IS NULL
        """,  # noqa: S608 — fixed fragments
        arg,
    )
    if row is None:
        raise NotFound("trip_case_not_found", f"trip case {ref} not found")
    case = _case_dict(row)
    case["inquiry_snapshot"] = json.loads(row["inquiry_snapshot"]) if row["inquiry_snapshot"] else None
    case_id = case["id"]
    legs = await conn.fetch(
        "SELECT position, source_tour_id, day_from, day_to FROM booking.trip_case_legs"
        " WHERE trip_case_id = $1::uuid ORDER BY day_from, position",
        case_id,
    )
    versions = await conn.fetch(
        """
        SELECT v.id::text AS id, v.version_number, v.notes, v.total_price::text AS total_price, v.currency,
               v.is_sent_to_customer, v.sent_at, v.is_approved, v.approved_at, v.created_by, v.created_at,
               v.updated_at,
               (SELECT count(*) FROM booking.trip_case_version_itinerary i WHERE i.trip_case_version_id = v.id)
                   AS item_count
        FROM booking.trip_case_versions v WHERE v.trip_case_id = $1::uuid ORDER BY v.version_number
        """,
        case_id,
    )
    history = await conn.fetch(
        "SELECT from_status, to_status, changed_by, note, changed_at FROM booking.trip_case_status_history"
        " WHERE trip_case_id = $1::uuid ORDER BY changed_at, id",
        case_id,
    )
    assignments = await conn.fetch(
        "SELECT a.id::text AS id, a.staff_user_id, s.name AS staff_name, a.role, a.assigned_by, a.assigned_at,"
        " a.unassigned_at FROM booking.staff_assignments a JOIN booking.staff_users s"
        " ON s.cis_user_id = a.staff_user_id WHERE a.trip_case_id = $1::uuid ORDER BY a.assigned_at",
        case_id,
    )
    case["legs"] = [dict(r) for r in legs]
    case["versions"] = [dict(r) for r in versions]
    case["status_history"] = [dict(r) for r in history]
    case["assignments"] = [dict(r) for r in assignments]
    case["allowed_transitions"] = await allowed_transitions(conn, case["status"])
    return case


def _plain(row: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    return {k: row[k] for k in keys}


async def update_case(conn: asyncpg.Connection, staff: Staff, ref: str, changes: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(changes) - set(EDITABLE_FIELDS))
    if unknown:
        raise Invalid("not_editable", f"not editable: {', '.join(unknown)}")
    row = await resolve_case(conn, ref, lock=True)
    before = _plain(row, EDITABLE_FIELDS)
    changed = {k: v for k, v in changes.items() if before[k] != v}
    if not changed:
        return {**before, "id": row["id"], "code": row["code"], "changed": []}
    sets = ", ".join(f"{column} = ${i + 2}" for i, column in enumerate(changed))
    try:
        updated = await conn.fetchrow(
            f"UPDATE booking.trip_cases SET {sets} WHERE id = $1::uuid"  # noqa: S608 — whitelisted column names
            f" RETURNING id::text AS id, code, {', '.join(EDITABLE_FIELDS)}",
            row["id"],
            *changed.values(),
        )
    except asyncpg.CheckViolationError as exc:
        raise Invalid("invalid_value", exc.constraint_name or "check violation") from exc
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="trip_case.updated",
        entity_type="trip_case",
        entity_id=row["id"],
        before={k: before[k] for k in changed},
        after={k: updated[k] for k in changed},
    )
    return {**dict(updated), "changed": sorted(changed)}


async def change_status(
    conn: asyncpg.Connection, staff: Staff, ref: str, to_status: str, reason: str | None
) -> dict[str, Any]:
    if to_status not in STATUSES:
        raise Invalid("unknown_status", f"unknown status {to_status}")
    reason = (reason or "").strip() or None
    row = await resolve_case(conn, ref, lock=True)
    from_status = row["status"]
    if from_status == to_status:
        raise Conflict("status_unchanged", f"trip case is already {to_status}")
    allowed = await allowed_transitions(conn, from_status)
    if to_status not in allowed:
        raise Conflict(
            "illegal_transition", f"{from_status} -> {to_status} is not allowed", allowed_transitions=allowed
        )
    if to_status in REASON_REQUIRED and not reason:
        raise Invalid("reason_required", f"a reason is required to move a trip case to {to_status}")
    await conn.execute(
        "SELECT set_config('booking.actor', $1, true), set_config('booking.status_note', $2, true)",
        staff.cis_user_id,
        reason or "",
    )
    try:
        await conn.execute("UPDATE booking.trip_cases SET status = $2 WHERE id = $1::uuid", row["id"], to_status)
    except asyncpg.CheckViolationError as exc:  # the trigger is the last word if the rules changed meanwhile
        raise Conflict("illegal_transition", str(exc)) from exc
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="trip_case.status_changed",
        entity_type="trip_case",
        entity_id=row["id"],
        before={"status": from_status},
        after={"status": to_status, "reason": reason},
    )
    return {
        "id": row["id"],
        "code": row["code"],
        "from_status": from_status,
        "status": to_status,
        "reason": reason,
        "allowed_transitions": await allowed_transitions(conn, to_status),
    }


async def assign(
    conn: asyncpg.Connection, staff: Staff, ref: str, cis_user_id: str | None, role: str
) -> dict[str, Any]:
    """Give ``role`` on the case to ``cis_user_id`` (closing the previous holder), or close it when None."""
    if role not in ASSIGNMENT_ROLES:
        raise Invalid("unknown_assignment_role", f"role must be one of {', '.join(ASSIGNMENT_ROLES)}")
    case = await resolve_case(conn, ref, lock=True)
    current = await conn.fetchrow(
        "SELECT id::text AS id, staff_user_id FROM booking.staff_assignments"
        " WHERE trip_case_id = $1::uuid AND role = $2 AND unassigned_at IS NULL FOR UPDATE",
        case["id"],
        role,
    )
    previous = current["staff_user_id"] if current else None
    if cis_user_id is not None:
        target = await conn.fetchrow(
            "SELECT cis_user_id, active FROM booking.staff_users WHERE cis_user_id = $1", cis_user_id
        )
        if target is None or not target["active"]:
            raise Invalid("staff_not_assignable", "the staff user does not exist or is not active")
    if previous == cis_user_id:
        return {"id": case["id"], "code": case["code"], "role": role, "staff_user_id": previous, "changed": False}
    if current is not None:
        await conn.execute(
            "UPDATE booking.staff_assignments SET unassigned_at = now() WHERE id = $1::uuid", current["id"]
        )
    if cis_user_id is not None:
        await conn.execute(
            "INSERT INTO booking.staff_assignments (trip_case_id, staff_user_id, role, assigned_by)"
            " VALUES ($1::uuid, $2, $3, $4)",
            case["id"],
            cis_user_id,
            role,
            staff.cis_user_id,
        )
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="trip_case.assigned" if cis_user_id else "trip_case.unassigned",
        entity_type="trip_case",
        entity_id=case["id"],
        before={"role": role, "staff_user_id": previous},
        after={"role": role, "staff_user_id": cis_user_id},
    )
    return {"id": case["id"], "code": case["code"], "role": role, "staff_user_id": cis_user_id, "changed": True}


def is_terminal(status: str) -> bool:
    return status in TERMINAL_STATUSES
