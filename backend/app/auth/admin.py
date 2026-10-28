"""Staff profile operations: admin list / activate / role, the picker directory, detail and preferences.

Rows linked to a Cognito identity (``cognito_sub`` set, bk-016) refuse a role change with 409 ``managed_in_directory``:
their role follows the booking-* group on every sign-in. Their ``active`` flag is still a Booking admin switch.
"""

from __future__ import annotations

import json
from typing import Any

import asyncpg

from app import audit
from app.auth.staff import STAFF_COLUMNS, STAFF_ROLES, Staff
from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.notifications.service import STAFF_TYPES as STAFF_NOTIFICATION_TYPES


async def list_staff(conn: asyncpg.Connection, *, active: bool | None = None) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        f"SELECT {STAFF_COLUMNS} FROM booking.staff_users"  # noqa: S608 — constant
        " WHERE ($1::boolean IS NULL OR active = $1) ORDER BY active DESC, created_at",
        active,
    )
    return [Staff.from_row(r).public() for r in rows]


async def update_staff(
    conn: asyncpg.Connection,
    admin: Staff,
    cis_user_id: str,
    *,
    active: bool | None,
    role: str | None,
    reason: str | None = None,
) -> dict[str, Any]:
    """``reason`` (AA-996) is required when an active profile is deactivated (422 ``reason_required``) and optional
    otherwise (activation, role change). It is stored in the audit row (``after.reason`` and the summary); it is never
    kept on the profile."""
    reason = (reason or "").strip() or None
    if role is not None and role not in STAFF_ROLES:
        raise Invalid("unknown_staff_role", f"role must be one of {', '.join(STAFF_ROLES)}")
    row = await conn.fetchrow(
        f"SELECT {STAFF_COLUMNS} FROM booking.staff_users WHERE cis_user_id = $1 FOR UPDATE",  # noqa: S608
        cis_user_id,
    )
    if row is None:
        raise NotFound("staff_not_found", f"staff user {cis_user_id} not found")
    current = Staff.from_row(row)
    if current.managed_in_directory and role is not None and role != current.role:
        # AA-680 Phase 2: a row linked to a Cognito identity takes its role from the booking-* group on every sign-in,
        # so a Booking-side role change would be overwritten. `active` stays a Booking switch (suspend / re-enable).
        raise Conflict(
            "managed_in_directory",
            "this staff member's Booking role comes from their company sign-in groups:"
            " change it in CIS → Platform → Staff",
        )
    if cis_user_id == admin.cis_user_id and (active is False or (role is not None and role != "admin")):
        raise Conflict("cannot_demote_self", "an admin cannot deactivate or demote themself")
    new_active = current.active if active is None else active
    new_role = current.role if role is None else role
    if (new_active, new_role) == (current.active, current.role):
        return current.public()
    if current.active and not new_active and reason is None:
        # AA-996 (spec §4.13): a deactivation always carries an audited reason, whichever client sends it.
        raise Invalid("reason_required", "say why this profile is deactivated (reason)")
    updated = Staff.from_row(
        await conn.fetchrow(
            f"UPDATE booking.staff_users SET active = $2, role = $3 WHERE cis_user_id = $1"  # noqa: S608
            f" RETURNING {STAFF_COLUMNS}",
            cis_user_id,
            new_active,
            new_role,
        )
    )
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=admin.cis_user_id,
        action="staff.updated",
        entity_type="staff_user",
        entity_id=cis_user_id,
        before={"cis_user_id": cis_user_id, "active": current.active, "role": current.role},
        after={
            "cis_user_id": cis_user_id,
            "active": updated.active,
            "role": updated.role,
            **({"reason": reason} if reason else {}),
        },
        entity_label=updated.name or cis_user_id,
        # Lead with the verb the audit-log badge shows ("Deactivated" / "Activated") so a search for it finds the row.
        summary=_access_verb(current.active, updated.active)
        + f"active {current.active} -> {updated.active}, role {current.role} -> {updated.role}"
        + (f". Reason: {reason}" if reason else ""),
    )
    return updated.public()


def _access_verb(before: bool, after: bool) -> str:
    if before and not after:
        return "Deactivated. "
    if after and not before:
        return "Activated. "
    return ""


async def directory(conn: asyncpg.Connection, *, role: str | None = None) -> list[dict[str, Any]]:
    """Active staff for pickers (D4): id, name and Booking role only — no e-mail, no CIS role."""
    if role is not None and role not in STAFF_ROLES:
        raise Invalid("unknown_staff_role", f"role must be one of {', '.join(STAFF_ROLES)}")
    rows = await conn.fetch(
        "SELECT cis_user_id, name, role FROM booking.staff_users"
        " WHERE active AND ($1::text IS NULL OR role = $1) ORDER BY lower(coalesce(name, cis_user_id))",
        role,
    )
    return [dict(r) for r in rows]


async def staff_detail(conn: asyncpg.Connection, viewer: Staff, cis_user_id: str) -> dict[str, Any]:
    """Profile + open assignments. Admins see anyone; other staff only themselves."""
    if cis_user_id != viewer.cis_user_id and not viewer.can("staff.admin"):
        raise Forbidden("forbidden", "only an admin can view another staff profile")
    row = await conn.fetchrow(
        f"SELECT {STAFF_COLUMNS} FROM booking.staff_users WHERE cis_user_id = $1",  # noqa: S608 — constant
        cis_user_id,
    )
    if row is None:
        raise NotFound("staff_not_found", f"staff user {cis_user_id} not found")
    assignments = await conn.fetch(
        """
        SELECT a.role, a.assigned_at, tc.id::text AS trip_case_id, tc.code, tc.status, tc.start_date,
               c.name AS customer_name
        FROM booking.staff_assignments a
        JOIN booking.trip_cases tc ON tc.id = a.trip_case_id AND tc.deleted_at IS NULL
        LEFT JOIN booking.customers c ON c.id = tc.customer_id
        WHERE a.staff_user_id = $1 AND a.unassigned_at IS NULL
        ORDER BY a.assigned_at DESC
        """,
        cis_user_id,
    )
    items = [dict(a) for a in assignments]
    open_items = [a for a in items if a["status"] not in ("completed", "cancelled")]
    return {
        **Staff.from_row(row).public(),
        "updated_at": row["updated_at"],
        "open_assignments": open_items,
        "counts": {"open_assignments": len(open_items), "assignments": len(items)},
    }


async def get_preferences(conn: asyncpg.Connection, staff: Staff) -> dict[str, Any]:
    raw = await conn.fetchval(
        "SELECT preferences::text FROM booking.staff_users WHERE cis_user_id = $1", staff.cis_user_id
    )
    prefs = json.loads(raw) if raw else {}
    notify = prefs.get("notify") if isinstance(prefs.get("notify"), dict) else {}
    return {"notify": {t: bool(notify.get(t, True)) for t in STAFF_NOTIFICATION_TYPES}}


async def set_preferences(conn: asyncpg.Connection, staff: Staff, notify: dict[str, bool]) -> dict[str, Any]:
    unknown = sorted(set(notify) - set(STAFF_NOTIFICATION_TYPES))
    if unknown:
        raise Invalid("unknown_notification_type", f"unknown type {', '.join(unknown)}")
    before = await get_preferences(conn, staff)
    after = {"notify": {**before["notify"], **{k: bool(v) for k, v in notify.items()}}}
    if after == before:
        return after
    await conn.execute(
        "UPDATE booking.staff_users SET preferences = preferences || $2::jsonb WHERE cis_user_id = $1",
        staff.cis_user_id,
        json.dumps(after),
    )
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="staff.preferences_updated",
        entity_type="staff_user",
        entity_id=staff.cis_user_id,
        before=before,
        after=after,
        entity_label=staff.name or staff.cis_user_id,
        summary="Updated notification preferences",
    )
    return after
