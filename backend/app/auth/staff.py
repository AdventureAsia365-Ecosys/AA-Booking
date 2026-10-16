"""Booking staff: the thin profile over the CIS login (ADR 0004 O1), permissions and FastAPI dependencies.

A request is staff-authenticated when its bearer token is a valid CIS staff JWT (``app.auth.tokens``)
**and** the matching ``booking.staff_users`` row is active. The row is provisioned on the first valid
token: CIS ``admin`` → Booking ``admin`` (active); ``reviewer`` → ``sales`` and ``content`` →
``content_creator``, both inactive until a Booking admin activates them. Later tokens refresh the
profile fields (email, name, cis_role, last_seen_at) but never the Booking role or the active flag.

Permissions are a role matrix in code (same pattern as CIS ``frontend/lib/rbac.ts``, ERD v2 README
question 5). The matrix is provisional until the PRD role matrix is confirmed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any

import asyncpg
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app import audit
from app.auth.tokens import AuthUnavailable, InvalidToken, StaffClaims

STAFF_ROLES = ("admin", "sales", "contracting", "content_creator")
# CIS role (staff JWT cis_role) → Booking role on first provisioning (ERD v2 identity note).
ROLE_FROM_CIS = {"admin": "admin", "reviewer": "sales", "content": "content_creator"}
AUTO_ACTIVE_CIS_ROLES = frozenset({"admin"})

PERMISSIONS: dict[str, frozenset[str]] = {
    "trip_case.read": frozenset({"admin", "sales", "contracting"}),
    "trip_case.write": frozenset({"admin", "sales"}),
    "trip_case.assign": frozenset({"admin", "sales"}),
    "version.write": frozenset({"admin", "sales"}),
    "customer.read": frozenset({"admin", "sales", "contracting"}),
    "customer.write": frozenset({"admin", "sales"}),
    "customer.admin": frozenset({"admin"}),  # block / unblock, archive
    "staff.directory": frozenset({"admin", "sales", "contracting"}),  # StaffPicker (D4)
    # Margins, net costs and FX are commercial data: sales quote, contracting owns rates (ADR 0005 d8).
    "pricing.read": frozenset({"admin", "sales", "contracting"}),
    # New pricing rule / FX rows and their approval (maker-checker, bk-007): contracting owns rates, admin backs up.
    "pricing.write": frozenset({"admin", "contracting"}),
    # Supply (bk-008, ADR 0005 d8): contracting owns suppliers and contracts; sales reads them to quote.
    "supply.read": frozenset({"admin", "sales", "contracting"}),
    "supply.write": frozenset({"admin", "contracting"}),
    # CIS catalog reads (content is read-only in Booking, O7): every staff role, content creators included.
    "catalog.read": frozenset({"admin", "sales", "contracting", "content_creator"}),
    # Commercial row of a catalog tour (sellable, trip style, price hint, notes; bk-009): contracting + admin.
    "trip_plan.write": frozenset({"admin", "contracting"}),
    "staff.admin": frozenset({"admin"}),
    "audit.read": frozenset({"admin"}),
    # Retention runs and their status (AA-825): DRY RUN / live delete and redaction of Booking rows.
    "retention.admin": frozenset({"admin"}),
}

# last_seen_at is refreshed at most this often (one write per staff user per 5 minutes, not per request).
LAST_SEEN_EVERY = "5 minutes"

STAFF_COLUMNS = "cis_user_id, email, name, role, active, cis_role, created_at, updated_at, last_seen_at"

bearer = HTTPBearer(auto_error=False, description="CIS staff JWT (aud=booking)")


@dataclass(frozen=True)
class Staff:
    cis_user_id: str
    email: str | None
    name: str | None
    role: str
    active: bool
    cis_role: str | None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_seen_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Any) -> Staff:
        return cls(**{k: row[k] for k in STAFF_COLUMNS.split(", ")})

    def can(self, permission: str) -> bool:
        return self.active and self.role in PERMISSIONS.get(permission, frozenset())

    @property
    def permissions(self) -> list[str]:
        return sorted(p for p in PERMISSIONS if self.can(p))

    def public(self) -> dict[str, Any]:
        return {
            "cis_user_id": self.cis_user_id,
            "email": self.email,
            "name": self.name,
            "role": self.role,
            "active": self.active,
            "cis_role": self.cis_role,
            "created_at": self.created_at,
            "last_seen_at": self.last_seen_at,
        }


async def provision(conn: asyncpg.Connection, claims: StaffClaims, role: str) -> Staff:
    """Create the profile on the first token, refresh profile fields afterwards. Returns the row."""
    row = await conn.fetchrow(
        f"""
        INSERT INTO booking.staff_users AS s (cis_user_id, email, name, role, active, cis_role, last_seen_at)
        VALUES ($1, $2, $3, $4, $5, $6, now())
        ON CONFLICT (cis_user_id) DO UPDATE
            SET email = coalesce(EXCLUDED.email, s.email),
                name = coalesce(EXCLUDED.name, s.name),
                cis_role = EXCLUDED.cis_role,
                last_seen_at = now()
            WHERE s.last_seen_at IS NULL
               OR s.last_seen_at < now() - interval '{LAST_SEEN_EVERY}'
               OR s.cis_role IS DISTINCT FROM EXCLUDED.cis_role
               OR (EXCLUDED.email IS NOT NULL AND s.email IS DISTINCT FROM EXCLUDED.email)
               OR (EXCLUDED.name IS NOT NULL AND s.name IS DISTINCT FROM EXCLUDED.name)
        RETURNING {STAFF_COLUMNS}, (xmax = 0) AS inserted
        """,  # noqa: S608 — constants only
        claims.sub,
        claims.email,
        claims.name,
        role,
        claims.cis_role in AUTO_ACTIVE_CIS_ROLES,
        claims.cis_role,
    )
    inserted = False
    if row is None:  # unchanged within LAST_SEEN_EVERY: nothing written
        row = await conn.fetchrow(
            f"SELECT {STAFF_COLUMNS} FROM booking.staff_users WHERE cis_user_id = $1",  # noqa: S608
            claims.sub,
        )
    else:
        inserted = bool(row["inserted"])
    staff = Staff.from_row(row)
    if inserted:
        await audit.record(
            conn,
            actor_type="system",
            actor_ref=staff.cis_user_id,
            action="staff.provisioned",
            entity_type="staff_user",
            entity_id=staff.cis_user_id,
            after={**staff.public(), "from_cis_role": claims.cis_role},
        )
    return staff


async def get_pool(request: Request) -> asyncpg.Pool:
    db = request.app.state.db
    if not db.configured:
        raise HTTPException(status_code=503, detail="database_not_configured")
    return await db.pool()


async def current_staff(
    request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]
) -> Staff:
    if credentials is None or credentials.scheme.lower() != "bearer" or not credentials.credentials:
        raise HTTPException(status_code=401, detail="missing_bearer_token", headers={"WWW-Authenticate": "Bearer"})
    try:
        claims = await request.app.state.staff_verifier.verify(credentials.credentials)
    except InvalidToken as exc:
        raise HTTPException(status_code=401, detail="invalid_token", headers={"WWW-Authenticate": "Bearer"}) from exc
    except AuthUnavailable as exc:
        raise HTTPException(status_code=503, detail="auth_unavailable") from exc
    role = ROLE_FROM_CIS.get(claims.cis_role)
    if role is None:
        raise HTTPException(status_code=403, detail="unknown_cis_role")
    pool = await get_pool(request)
    async with pool.acquire() as conn, conn.transaction():
        staff = await provision(conn, claims, role)
    if not staff.active:
        raise HTTPException(status_code=403, detail="staff_inactive")
    return staff


def require(permission: str) -> Callable[..., Awaitable[Staff]]:
    if permission not in PERMISSIONS:
        raise ValueError(f"unknown permission {permission!r}")

    async def dependency(staff: Annotated[Staff, Depends(current_staff)]) -> Staff:
        if not staff.can(permission):
            raise HTTPException(status_code=403, detail="forbidden")
        return staff

    return dependency
