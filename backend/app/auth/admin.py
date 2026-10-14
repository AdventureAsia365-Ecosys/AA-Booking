"""Booking admin operations on staff profiles: list, activate/deactivate, set the Booking role."""

from __future__ import annotations

from typing import Any

import asyncpg

from app import audit
from app.auth.staff import STAFF_COLUMNS, STAFF_ROLES, Staff
from app.errors import Conflict, Invalid, NotFound


async def list_staff(conn: asyncpg.Connection, *, active: bool | None = None) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        f"SELECT {STAFF_COLUMNS} FROM booking.staff_users"  # noqa: S608 — constant
        " WHERE ($1::boolean IS NULL OR active = $1) ORDER BY active DESC, created_at",
        active,
    )
    return [Staff.from_row(r).public() for r in rows]


async def update_staff(
    conn: asyncpg.Connection, admin: Staff, cis_user_id: str, *, active: bool | None, role: str | None
) -> dict[str, Any]:
    if role is not None and role not in STAFF_ROLES:
        raise Invalid("unknown_staff_role", f"role must be one of {', '.join(STAFF_ROLES)}")
    row = await conn.fetchrow(
        f"SELECT {STAFF_COLUMNS} FROM booking.staff_users WHERE cis_user_id = $1 FOR UPDATE",  # noqa: S608
        cis_user_id,
    )
    if row is None:
        raise NotFound("staff_not_found", f"staff user {cis_user_id} not found")
    current = Staff.from_row(row)
    if cis_user_id == admin.cis_user_id and (active is False or (role is not None and role != "admin")):
        raise Conflict("cannot_demote_self", "an admin cannot deactivate or demote themself")
    new_active = current.active if active is None else active
    new_role = current.role if role is None else role
    if (new_active, new_role) == (current.active, current.role):
        return current.public()
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
        after={"cis_user_id": cis_user_id, "active": updated.active, "role": updated.role},
    )
    return updated.public()
