"""The Booking role editor: custom staff roles as data (AA-1155 T6; root spec R2.2, R2.3, R4.4; root ADR 0006a A14).

An ``is_owner`` staff member (the CIS directory marker, read uncached through ``shared.v_staff_booking_grant``,
:func:`app.auth.custom_roles.is_owner`) creates a custom role by cloning a built-in one, then ticks its capabilities,
relabels or archives it. Built-in roles stay in code and are shown read-only. Every change writes one audit row with
the role before and after. Rules, all checked here before any write (400, :class:`app.errors.BadRequest`):

- the code matches ``^custom_[a-z][a-z0-9_]{1,46}$`` (never a built-in role name; the bk-062 CHECK too);
- ``based_on`` is a built-in role; the label is 1-80 characters without control characters;
- every capability is known to the code (:data:`app.auth.staff.PERMISSIONS`, ``unknown_capability``) and none is in
  the approve class (``approve_class_capability``; root spec D4, and the bk-062 CHECK if the code is ever bypassed).

Readers: ``staff.admin`` or ``is_owner``. Writers: ``is_owner`` only (403 ``owner_only``). The resolver's process cache
is dropped after each save (:func:`app.auth.custom_roles.forget`); other tasks follow within its bound.
"""

from __future__ import annotations

from typing import Any

import asyncpg

from app import audit
from app.auth import custom_roles
from app.auth.staff import STAFF_ROLES, Staff, role_permissions
from app.errors import BadRequest, Conflict, Forbidden, NotFound

LABEL_MAX = 80  # technical-constant: the bk-062 staff_role_label_chk bound, a column size and not a policy value
OWNER_ONLY = "owner_only"

ROLES_SQL = (
    "SELECT r.code, r.label, r.based_on, r.archived, r.created_at, r.created_by, r.updated_at, r.updated_by,"
    " coalesce(array_agg(c.capability ORDER BY c.capability) FILTER (WHERE c.capability IS NOT NULL), '{}')"
    " AS capabilities,"
    " (SELECT count(*) FROM booking.staff_users u WHERE u.role = r.code AND u.active) AS holders"
    " FROM booking.staff_role r LEFT JOIN booking.staff_role_capability c ON c.role_code = r.code"
)


def _view(row: Any) -> dict[str, Any]:
    return {
        "code": row["code"],
        "label": row["label"],
        "based_on": row["based_on"],
        "archived": row["archived"],
        "capabilities": sorted(row["capabilities"] or ()),
        "holders": int(row["holders"] or 0),
        "created_at": row["created_at"],
        "created_by": row["created_by"],
        "updated_at": row["updated_at"],
        "updated_by": row["updated_by"],
    }


def _snapshot(role: dict[str, Any]) -> dict[str, Any]:
    """What the audit row keeps of a role (before / after)."""
    return {k: role[k] for k in ("code", "label", "based_on", "archived", "capabilities")}


def check_code(code: Any) -> str:
    if not isinstance(code, str) or not custom_roles.CUSTOM_CODE.fullmatch(code):
        raise BadRequest(
            "invalid_role_code", "a custom role code is custom_ then a lower-case letter and 2-47 letters, digits or _"
        )
    return code


def check_label(label: Any) -> str:
    text = label.strip() if isinstance(label, str) else ""
    if not 1 <= len(text) <= LABEL_MAX or any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise BadRequest("invalid_label", f"a label is 1-{LABEL_MAX} characters, no control characters")
    return text


def check_base(based_on: Any) -> str:
    if based_on not in STAFF_ROLES:
        raise BadRequest("unknown_base_role", f"based_on must be one of {', '.join(STAFF_ROLES)}")
    return str(based_on)


def check_capabilities(capabilities: Any) -> frozenset[str]:
    """Root spec R2.2: 400 on a capability the code does not know or one in the approve class."""
    names = [c for c in capabilities if isinstance(c, str)] if isinstance(capabilities, list | tuple | set) else None
    if names is None or len(names) != len(capabilities):
        raise BadRequest("invalid_capabilities", "capabilities is a list of capability names")
    unknown = sorted(set(names) - custom_roles.known_capabilities())
    if unknown:
        raise BadRequest("unknown_capability", f"unknown capability: {', '.join(unknown)}", capabilities=unknown)
    approve = sorted(c for c in set(names) if custom_roles.is_approve_class(c))
    if approve:
        raise BadRequest(
            "approve_class_capability",
            f"a custom role never holds an approval: {', '.join(approve)}",
            capabilities=approve,
        )
    return frozenset(names)


def clone_of(based_on: str) -> frozenset[str]:
    """The capabilities a new role starts with: the built-in role's, without the approve class."""
    return role_permissions(based_on) & custom_roles.assignable()


async def require_owner(conn: asyncpg.Connection | asyncpg.Pool, staff: Staff) -> None:
    # AA-1157: custom_roles.markers reads False inside a view-as session (never an owner there).
    if not (await custom_roles.markers(conn, staff))[0]:
        raise Forbidden(OWNER_ONLY, "only an owner (CIS Staff page) changes Booking roles")


async def list_roles(conn: asyncpg.Connection, staff: Staff) -> dict[str, Any]:
    owner = (await custom_roles.markers(conn, staff))[0]
    if not owner and not staff.can("staff.admin"):
        raise Forbidden("forbidden", "the role editor is for administrators and owners")
    rows = await conn.fetch(ROLES_SQL + " GROUP BY r.code ORDER BY r.archived, lower(r.label), r.code")  # noqa: S608
    known = sorted(custom_roles.known_capabilities())
    return {
        "builtin": [{"code": r, "capabilities": sorted(role_permissions(r))} for r in STAFF_ROLES],
        "custom": [_view(r) for r in rows],
        "capabilities": [{"name": c, "assignable": not custom_roles.is_approve_class(c)} for c in known],
        "can_edit": owner,
        # AA-1157: an owner (not inside a view) may start a read-only "view as" on any of these roles; the start
        # re-checks owner and downward.
        "can_view_as": owner,
    }


async def _one(conn: asyncpg.Connection, code: str) -> dict[str, Any] | None:
    row = await conn.fetchrow(ROLES_SQL + " WHERE r.code = $1 GROUP BY r.code", code)  # noqa: S608 — constant
    return None if row is None else _view(row)


async def create_role(
    conn: asyncpg.Connection,
    staff: Staff,
    *,
    code: Any,
    label: Any,
    based_on: Any,
    capabilities: Any = None,
) -> dict[str, Any]:
    """Clone ``based_on`` into a new custom role (its capabilities without the approve class, or ``capabilities``)."""
    await require_owner(conn, staff)
    code = check_code(code)
    label = check_label(label)
    based_on = check_base(based_on)
    caps = clone_of(based_on) if capabilities is None else check_capabilities(capabilities)
    inserted = await conn.fetchval(
        "INSERT INTO booking.staff_role (code, label, based_on, created_by, updated_by) VALUES ($1, $2, $3, $4, $4)"
        " ON CONFLICT (code) DO NOTHING RETURNING code",
        code,
        label,
        based_on,
        staff.cis_user_id,
    )
    if inserted is None:
        raise Conflict("role_exists", f"a custom role {code} already exists")
    await _add(conn, code, caps, staff)
    after = await _one(conn, code)
    assert after is not None  # noqa: S101 — inserted above in this transaction
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="staff_role.created",
        entity_type="staff_role",
        entity_id=None,
        after=_snapshot(after),
        entity_label=f"{label} ({code})",
        summary=f"Custom role created from {based_on} with {len(caps)} capabilities",
    )
    custom_roles.forget()
    return after


async def _add(conn: asyncpg.Connection, code: str, caps: frozenset[str], staff: Staff) -> None:
    if caps:
        await conn.execute(
            "INSERT INTO booking.staff_role_capability (role_code, capability, created_by)"
            " SELECT $1, c, $3 FROM unnest($2::text[]) AS c ON CONFLICT DO NOTHING",
            code,
            sorted(caps),
            staff.cis_user_id,
        )


async def update_role(
    conn: asyncpg.Connection,
    staff: Staff,
    code: str,
    *,
    label: Any = None,
    capabilities: Any = None,
    archived: bool | None = None,
) -> dict[str, Any]:
    """Relabel, set the capabilities (the full set) or archive / restore a custom role; audited before and after."""
    await require_owner(conn, staff)
    code = check_code(code)
    new_label = None if label is None else check_label(label)
    new_caps = None if capabilities is None else check_capabilities(capabilities)
    locked = await conn.fetchval("SELECT code FROM booking.staff_role WHERE code = $1 FOR UPDATE", code)
    if locked is None:
        raise NotFound("role_not_found", f"no custom role {code}")
    before = await _one(conn, code)
    assert before is not None  # noqa: S101 — locked above
    held = frozenset(before["capabilities"])
    changes: list[str] = []
    if new_caps is not None and new_caps != held:
        removed, added = sorted(held - new_caps), sorted(new_caps - held)
        if removed:
            await conn.execute(
                "DELETE FROM booking.staff_role_capability WHERE role_code = $1 AND capability = ANY($2::text[])",
                code,
                removed,
            )
        await _add(conn, code, frozenset(added), staff)
        changes.append("capabilities " + " ".join([*(f"+{c}" for c in added), *(f"-{c}" for c in removed)]))
    label_changed = new_label is not None and new_label != before["label"]
    archive_changed = archived is not None and archived != before["archived"]
    if label_changed:
        changes.append(f"label {before['label']} -> {new_label}")
    if archive_changed:
        changes.append("archived" if archived else "restored")
    if not changes:
        return before
    await conn.execute(
        "UPDATE booking.staff_role SET label = coalesce($2, label), archived = coalesce($3, archived),"
        " updated_at = now(), updated_by = $4 WHERE code = $1",
        code,
        new_label if label_changed else None,
        archived if archive_changed else None,
        staff.cis_user_id,
    )
    after = await _one(conn, code)
    assert after is not None  # noqa: S101 — locked above
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="staff_role.updated",
        entity_type="staff_role",
        entity_id=None,
        before=_snapshot(before),
        after=_snapshot(after),
        entity_label=f"{after['label']} ({code})",
        summary="; ".join(changes),
    )
    custom_roles.forget()
    return after
