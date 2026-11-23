"""Read-only "view as a lower role" for an owner (AA-1157; root ADR 0019 d5 as amended, root ADR 0006a A14).

An owner (the CIS ``is_owner`` marker, read through ``shared.v_staff_booking_grant``) sees Booking as a lower staff role
sees it, without a second account and without touching the sign-in: the Cognito session stays as it is.

How it works
------------
1. ``POST /booking/view-as`` (app/routers/staff_view_as.py) with ``{role, minutes 5-60, reason?}``: the caller must
   be an owner (uncached read, 403 ``owner_only``), the target a built-in role or an active custom role (404
   ``view_as_unknown_role``) whose read permissions are a subset of the caller's (tech lead Q6, 403
   ``view_as_not_downward``: :func:`downward` over :func:`read_permissions`). Under a per-staff advisory lock the
   caller's other open sessions end (``superseded``), then one ``staff.view_as_started`` row goes to
   ``booking.audit_logs`` (``entity_type`` ``staff_view_as``, ``entity_id`` the new session id) whose ``after`` holds
   the viewer's role, the target role, its label, its FULL capability list, the start, the expiry and the reason.
2. The session id goes into the BFF's httpOnly cookie ``bk_view_as`` and comes back as header :data:`HEADER`. It is an
   opaque, unguessable id (uuid4), not a signed token: Booking has no signing secret, and the id is checked on every
   request against its start / end rows and bound to the viewer, so it is revocable at once and can only NARROW rights
   (tech-lead-visible decision, recorded in the ADR amendment).
3. :func:`app.auth.staff.current_staff` refuses every non-GET/HEAD/OPTIONS request carrying the header with 403
   ``view_as_read_only`` before any database access. For a read, :func:`apply` checks the session (start row of this
   viewer, no end row, not expired, ``after`` intact, the viewer still an owner, the role still usable and downward)
   and narrows the staff: permissions = target ∩ viewer, no E2E grant, ``view_as`` set. A dead session is ended (one
   ``staff.view_as_ended`` row, under the same lock, never twice) and answers 409 ``view_as_ended``; another viewer's
   session 403 ``view_as_not_own``.
4. During a view the owner markers read False (:data:`app.auth.custom_roles.VIEWING`), so no owner or business-owner
   power is reachable, whatever route asks.
5. Ending: ``POST /booking/view-as/{session_id}/end`` (the BFF's Back, sent WITHOUT the header), a new start, the
   staff sign-out (the BFF ends every open view, also an expired one), :func:`apply` on expiry, or the owner's next
   ``GET /booking/view-as`` outside a view (:func:`close_expired`: the banner's check at 0 minutes, the Roles page). A
   session already past its expiry always ends with reason ``expired``, whichever path closes it.
6. Writes: in the browser flow the staff BFF refuses every write of a view first (never forwarded) and writes one
   structured log line ``booking_view_as_write_refused`` (the staff admin's container log). A write that reaches the
   API with the header anyway is refused in :func:`app.auth.staff.current_staff` with the same log line from the API.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

import asyncpg
from fastapi import HTTPException
from fastapi.routing import APIRoute

from app import audit
from app.auth import custom_roles

log = logging.getLogger(__name__)

HEADER = "x-booking-view-as"
ENTITY = "staff_view_as"
STARTED = "staff.view_as_started"
ENDED = "staff.view_as_ended"

MAX_MINUTES = 60  # technical-constant: root ADR 0019 d5 view-as ceiling, a security bound and not a business value
MIN_MINUTES = 5  # technical-constant: the shortest view the start accepts (same bound as the CIS view)
DEFAULT_MINUTES = 30  # technical-constant: the start's default length, chosen per start under MAX_MINUTES

READ_ONLY = "view_as_read_only"
ENDED_DETAIL = "view_as_ended"
NOT_OWN = "view_as_not_own"
NOT_DOWNWARD = "view_as_not_downward"
UNKNOWN_ROLE = "view_as_unknown_role"
OWNER_ONLY = "owner_only"
UNAVAILABLE = "view_as_unavailable"

# Built-in role names as the staff admin shows them (frontend ROLE_LABEL; the CIS roles catalogue BOOKING_ROLE_LABELS).
# The built-in role names, as the staff admin shows them (frontend lib/rbac.ts ROLE_LABEL; a unit test pins the two).
BUILTIN_LABELS = {
    "admin": "Admin",
    "sales": "Sales",
    "contracting": "Product & operations",
    "content_creator": "Content creator",
    "finance": "Finance",
}

LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtext('staff_view_as:' || $1))"
# $1 session, $2 viewer (actor_ref)
START_SQL = (
    "SELECT actor_ref, after::text AS after FROM booking.audit_logs "
    "WHERE entity_type = 'staff_view_as' AND entity_id = $1::uuid AND action = 'staff.view_as_started' "
    "ORDER BY created_at LIMIT 1"
)
ENDED_SQL = (
    "SELECT 1 FROM booking.audit_logs WHERE entity_type = 'staff_view_as' AND entity_id = $1::uuid "
    "AND action = 'staff.view_as_ended' LIMIT 1"
)
# The viewer's sessions that have no end row yet (open or expired-unclosed), newest first, with their start row's after.
OPEN_SQL = (
    "SELECT s.entity_id::text AS session_id, s.after::text AS after FROM booking.audit_logs s "
    "WHERE s.entity_type = 'staff_view_as' AND s.action = 'staff.view_as_started' AND s.actor_ref = $1 "
    "AND NOT EXISTS (SELECT 1 FROM booking.audit_logs e WHERE e.entity_type = 'staff_view_as' "
    "AND e.entity_id = s.entity_id AND e.action = 'staff.view_as_ended') ORDER BY s.created_at DESC LIMIT 20"
)


@dataclass(frozen=True)
class ViewAs:
    """The open view of one request (on the narrowed staff)."""

    session_id: str
    target_role: str
    target_label: str
    viewer_role: str
    expires_at: str

    def public(self) -> dict[str, Any]:
        """What the page may see (GET /booking/view-as, /booking/staff/me): never the session id, which only the BFF
        holds (httpOnly cookie)."""
        return {
            "target_role": self.target_role,
            "target_label": self.target_label,
            "viewer_role": self.viewer_role,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True)
class Target:
    code: str
    label: str
    capabilities: frozenset[str]


# ── Downward (Q6) ────────────────────────────────────────────────────────────
_read_cache: dict[int, frozenset[str]] = {}


def _route_permissions(dependant: Any) -> set[str]:
    """Every permission a route's dependency tree checks (``require()`` sets ``.permission``; nested through
    ``Annotated[..., Depends(...)]``)."""
    out: set[str] = set()
    for dep in getattr(dependant, "dependencies", ()) or ():
        perm = getattr(dep.call, "permission", None)
        if isinstance(perm, str):
            out.add(perm)
        out |= _route_permissions(dep)
    return out


def api_routes(routes: Any) -> Any:
    """Every APIRoute under ``routes``, also inside included routers (FastAPI >= 0.143 keeps an included router as
    one node whose ``original_router`` holds its routes) and mounts."""
    for route in routes or ():
        if isinstance(route, APIRoute):
            yield route
            continue
        sub = getattr(route, "original_router", None)
        nested = getattr(sub, "routes", None) if sub is not None else getattr(route, "routes", None)
        if nested:
            yield from api_routes(nested)


def read_permissions(app: Any) -> frozenset[str]:
    """The read rights of Booking: every permission a GET route checks, plus every ``*.read`` permission (the ones a
    service checks itself). Walked from ``app.routes``, so a route added later counts. Cached per app."""
    from app.auth.staff import PERMISSIONS  # noqa: PLC0415 — staff imports this module

    key = id(app)
    if key not in _read_cache:
        perms: set[str] = {p for p in PERMISSIONS if p.endswith(".read")}
        for route in api_routes(getattr(app, "routes", ())):
            if "GET" in route.methods:
                perms |= _route_permissions(route.dependant)
        _read_cache[key] = frozenset(perms)
    return _read_cache[key]


def downward(viewer_permissions: frozenset[str], target: frozenset[str], reads: frozenset[str]) -> tuple[bool, list]:
    """(ok, missing): the target's read permissions are a subset of the viewer's. ``missing`` is logged only."""
    missing = sorted((target & reads) - viewer_permissions)
    return not missing, missing


async def target_role(conn: asyncpg.Connection | asyncpg.Pool, code: Any) -> Target | None:
    """A built-in role, or an active custom role (uncached; its capabilities narrowed like the resolver's)."""
    from app.auth.staff import STAFF_ROLES, role_permissions  # noqa: PLC0415

    if not isinstance(code, str):
        return None
    if code in STAFF_ROLES:
        return Target(code, BUILTIN_LABELS.get(code, code), role_permissions(code))
    if not custom_roles.CUSTOM_CODE.fullmatch(code):
        return None
    try:
        role = await custom_roles.read_role(conn, code)
    except custom_roles.Unavailable as exc:
        raise HTTPException(status_code=503, detail=UNAVAILABLE) from exc
    if role is None or role.archived:
        return None
    return Target(role.code, role.label, custom_roles.effective(role))


# ── Start / end ──────────────────────────────────────────────────────────────
async def _end(conn: asyncpg.Connection, actor: str, session_id: str, reason: str, actor_type: str = "staff") -> bool:
    """One end row for ``session_id`` unless it has one (the caller holds the lock). True when written."""
    if await conn.fetchval(ENDED_SQL, session_id):
        return False
    await audit.record(
        conn,
        actor_type=actor_type,
        actor_ref=actor,
        action=ENDED,
        entity_type=ENTITY,
        entity_id=session_id,
        after={"session_id": session_id, "end_reason": reason},
        entity_label="View as",
        summary=f"View-as session ended ({reason})",
    )
    return True


async def start(
    pool: asyncpg.Pool, app: Any, staff: Any, cognito_sub: str | None, code: Any, minutes: int, reason: str | None
) -> dict[str, Any]:
    """Open a view (see the module docstring). Raises HTTPException on a refusal; returns the session."""
    if staff.preview or staff.e2e_writer or staff.view_as is not None or not cognito_sub:
        raise HTTPException(status_code=403, detail=OWNER_ONLY)
    async with pool.acquire() as conn, conn.transaction():
        try:
            grant = await custom_roles.read_grant(conn, cognito_sub)
        except custom_roles.Unavailable as exc:
            raise HTTPException(status_code=503, detail=UNAVAILABLE) from exc
        if grant is None or not grant.is_owner:
            log.info("booking_view_as_refused reason=owner_only")
            raise HTTPException(status_code=403, detail=OWNER_ONLY)
        target = await target_role(conn, code)
        if target is None:
            raise HTTPException(status_code=404, detail=UNKNOWN_ROLE)
        ok, missing = downward(frozenset(staff.permissions), target.capabilities, read_permissions(app))
        if not ok:
            log.info("booking_view_as_refused reason=not_downward role=%s missing=%s", target.code, ",".join(missing))
            raise HTTPException(status_code=403, detail=NOT_DOWNWARD)
        actor = staff.cis_user_id
        await conn.execute(LOCK_SQL, actor)
        now: datetime = await conn.fetchval("SELECT now()")
        ended = await _end_open(conn, actor, "superseded", now)
        expires = now + timedelta(minutes=minutes)
        session_id = str(uuid.uuid4())
        await audit.record(
            conn,
            actor_type="staff",
            actor_ref=actor,
            action=STARTED,
            entity_type=ENTITY,
            entity_id=session_id,
            after={
                "session_id": session_id,
                "viewer_role": staff.role,
                "target_role": target.code,
                "target_label": target.label,
                "capabilities": sorted(target.capabilities),
                "started_at": now.isoformat(),
                "expires_at": expires.isoformat(),
                "minutes": minutes,
                "reason": reason,
                "ended_sessions": ended,
            },
            entity_label="View as",
            summary=f"View as {target.label} started for {minutes} minutes (read-only)",
        )
    log.info("booking_view_as_started session=%s role=%s minutes=%s", session_id, target.code, minutes)
    return {
        "session_id": session_id,
        "target_role": target.code,
        "target_label": target.label,
        "expires_at": expires.isoformat(),
        "read_only": True,
    }


async def end(pool: asyncpg.Pool, staff: Any, session_id: str, reason: str = "ended") -> dict[str, Any]:
    """End the viewer's own session (idempotent). 404 for an unknown or another member's session."""
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(START_SQL, session_id)
        if row is None or row["actor_ref"] != staff.cis_user_id:
            raise HTTPException(status_code=404, detail="view_as_not_found")
        await conn.execute(LOCK_SQL, staff.cis_user_id)
        changed = await _end(conn, staff.cis_user_id, session_id, reason)
    return {"session_id": session_id, "ended": True, "changed": changed}


async def end_all(pool: asyncpg.Pool, staff: Any, reason: str = "signed_out") -> list[str]:
    """End every open session of the viewer (the BFF's sign-out); one already past its expiry ends as ``expired``."""
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(LOCK_SQL, staff.cis_user_id)
        now: datetime = await conn.fetchval("SELECT now()")
        return await _end_open(conn, staff.cis_user_id, reason, now)


async def close_expired(pool: asyncpg.Pool, staff: Any) -> list[str]:
    """End the viewer's sessions that are past their expiry but have no end row yet (reason ``expired``, actor the
    system), so a view left to time out gets its end row on the owner's next view-as read. Cheap: one indexed read when
    there is nothing to close. Best effort: a database error is logged and nothing is closed."""
    actor = getattr(staff, "cis_user_id", None)
    if not actor:
        return []
    try:
        async with pool.acquire() as conn, conn.transaction():
            if not await conn.fetch(OPEN_SQL, actor):
                return []
            await conn.execute(LOCK_SQL, actor)
            now: datetime = await conn.fetchval("SELECT now()")
            return await _end_open(conn, actor, None, now, actor_type="system")
    except (asyncpg.PostgresError, OSError) as exc:
        log.warning("booking_view_as_close_expired_failed error_class=%s", type(exc).__name__)
        return []


def _past_expiry(after_text: Any, now: datetime) -> bool:
    """Is the start row's ``after.expires_at`` at or before ``now``? An unreadable one counts as past (fail closed)."""
    import json  # noqa: PLC0415

    try:
        after = json.loads(after_text) if isinstance(after_text, str) else None
    except ValueError:
        after = None
    expires = _parse(after.get("expires_at")) if isinstance(after, dict) else None
    return expires is None or expires <= now


async def _end_open(
    conn: asyncpg.Connection, actor: str, reason: str | None, now: datetime, actor_type: str = "staff"
) -> list[str]:
    """End the viewer's sessions without an end row (the caller holds the lock): a session past its expiry as
    ``expired``, the others as ``reason`` (``None``: leave them open). The ids ended."""
    ended = []
    for r in await conn.fetch(OPEN_SQL, actor):
        why = "expired" if _past_expiry(r["after"], now) else reason
        if why is not None and await _end(conn, actor, r["session_id"], why, actor_type=actor_type):
            ended.append(r["session_id"])
    return ended


# ── Per request ──────────────────────────────────────────────────────────────
def _uuid(value: Any) -> str | None:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        return None


async def _close(pool: asyncpg.Pool, actor: str, session_id: str, reason: str) -> HTTPException:
    """End the session as the system (best effort, under the lock) and the 409 to answer."""
    try:
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(LOCK_SQL, actor)
            await _end(conn, actor, session_id, reason, actor_type="system")
    except (asyncpg.PostgresError, OSError) as exc:
        log.warning("booking_view_as_close_failed session=%s error_class=%s", session_id, type(exc).__name__)
    log.info("booking_view_as_ended session=%s reason=%s", session_id, reason)
    return HTTPException(status_code=409, detail=ENDED_DETAIL)


async def _session_state(pool: asyncpg.Pool, session_id: str) -> tuple[Any, bool]:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(START_SQL, session_id)
        ended = bool(row is not None and await conn.fetchval(ENDED_SQL, session_id))
    return row, ended


async def apply(pool: asyncpg.Pool, app: Any, staff: Any, cognito_sub: str | None, ref: str) -> Any:
    """The narrowed staff of a read made inside a view, or an HTTPException (see the module docstring)."""
    import json  # noqa: PLC0415

    session_id = _uuid(ref)
    if session_id is None:
        raise HTTPException(status_code=409, detail=ENDED_DETAIL)
    row, ended = await _session_state(pool, session_id)
    if row is None:
        raise HTTPException(status_code=409, detail=ENDED_DETAIL)
    if row["actor_ref"] != staff.cis_user_id:
        raise HTTPException(status_code=403, detail=NOT_OWN)
    if ended:
        raise HTTPException(status_code=409, detail=ENDED_DETAIL)
    after = json.loads(row["after"]) if row["after"] else {}
    expires = _parse(after.get("expires_at")) if isinstance(after, dict) else None
    if expires is None or not isinstance(after.get("target_role"), str):
        raise await _close(pool, staff.cis_user_id, session_id, "invalid")  # erased or tampered: fail closed
    async with pool.acquire() as conn:
        now: datetime = await conn.fetchval("SELECT now()")
    if expires <= now:
        raise await _close(pool, staff.cis_user_id, session_id, "expired")
    target, reason = await _still_allowed(pool, app, staff, cognito_sub, after["target_role"])
    if target is None:
        raise await _close(pool, staff.cis_user_id, session_id, reason)
    view = ViewAs(session_id, target.code, target.label, staff.role, expires.isoformat())
    return replace(
        staff,
        role=target.code,
        custom_permissions=target.capabilities & frozenset(staff.permissions),
        e2e_grants=frozenset(),
        role_label=target.label,
        view_as=view,
    )


async def _still_allowed(
    pool: asyncpg.Pool, app: Any, staff: Any, cognito_sub: str | None, code: str
) -> tuple[Target | None, str]:
    """(target, "") while the viewer is still an owner and the role still usable and downward; else (None, cause)."""
    try:
        grant = await custom_roles.grant_for(pool, cognito_sub) if cognito_sub else None
    except custom_roles.Unavailable as exc:
        raise HTTPException(status_code=503, detail=UNAVAILABLE) from exc
    if grant is None or not grant.is_owner:
        return None, "owner_lost"
    async with pool.acquire() as conn:
        target = await target_role(conn, code)
    if target is None:
        return None, "role_unavailable"
    if not downward(frozenset(staff.permissions), target.capabilities, read_permissions(app))[0]:
        return None, "not_downward"
    return target, ""


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None
