"""Booking staff: the thin staff profile, sign-in rules, permissions and FastAPI dependencies.

A request is staff-authenticated when its bearer token passes the multi-issuer verifier (``app.auth.identity_config``,
a byte copy of the CIS verifier) **and** the matching active ``booking.staff_users`` row exists. Two issuers:

- **WORKFORCE** (Cognito staff pool, clients booking-staff / breakglass; AA-680 Phase 2, ADR 0006a). The row is found
  by its Cognito identity ``(cognito_iss, cognito_sub)`` (bk-016). The Booking role comes from the ``booking-*`` group
  on **every** request (the first in :data:`BOOKING_GROUP_ROLES` order wins); no ``booking-*`` group is 403
  ``no_booking_role`` and nothing is written. An identity with no row gets one, keyed ``'cog:' || sub`` and active,
  from the token claims; bridge-era rows were linked to their identity once by the AA-CIS-Infra backfill script. A
  linked row whose ``active`` is false is refused (403 ``staff_inactive``): ``active`` stays a Booking-side switch.
- **BRIDGE** (the CIS-minted Booking token, during the transition only, ADR 0006a A6): the row is keyed by the CIS
  ``admin_users.id`` (``sub``) and provisioned on the first token: CIS ``admin`` → Booking ``admin`` (active);
  ``reviewer`` → ``sales`` and ``content`` → ``content_creator``, both inactive until a Booking admin activates them.
  Later tokens refresh the profile fields (email, name, cis_role, last_seen_at) but never the role or the active flag.

**Preview cap (AA-911 decision D-1, root ADR 0006a A11).** A WORKFORCE token from the Preview-only
``booking-staff-preview`` client (the fixed Preview host ``booking-preview.lumiguides.it.com``, which runs unmerged PR
code) is a read-only session whatever ``booking-*`` group the user holds: any request other than GET / HEAD / OPTIONS
is 403 ``preview_read_only`` before anything is read or written, and :meth:`Staff.can` grants only
:data:`PREVIEW_PERMISSIONS` (reads; never ``staff.admin``, ``audit.read`` or any write). ``/booking/staff/me`` reports
``preview_viewer: true`` and the capped permission list, so the UI hides write actions.

**E2E test session cap (AA-910, root ADR 0006a A10a).** The temporary WORKFORCE E2E user
:data:`E2E_TEST_STAFF_EMAIL` holds ``booking-admin`` so a signed-in smoke can read every staff page (``/staff`` and
``/audit-log`` included), but its sign-in codes sit in a shared test mailbox. So its session is read-only on every
client: any request other than GET / HEAD / OPTIONS is 403 ``e2e_read_only`` before anything is read or written.
Reads keep the admin permissions. The match is on the access token's ``email`` claim, which the WORKFORCE pre token
trigger sets to the normalised address; lower-casing here can only cap more sessions, never fewer.

Permissions are a role matrix in code (same pattern as CIS ``frontend/lib/rbac.ts``, ERD v2 README
question 5). The matrix is provisional until the PRD role matrix is confirmed.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import asyncpg
import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app import audit
from app.auth import identity_config
from app.auth.identity_verifier import AuthUnavailable, InvalidToken, Principal

log = logging.getLogger(__name__)

STAFF_ROLES = ("admin", "sales", "contracting", "content_creator")
# BRIDGE: CIS role (bridge token cis_role) → Booking role on first provisioning (ERD v2 identity note).
ROLE_FROM_CIS = {"admin": "admin", "reviewer": "sales", "content": "content_creator"}
AUTO_ACTIVE_CIS_ROLES = frozenset({"admin"})

# WORKFORCE: Cognito group → Booking role, in precedence order (AA-CIS-Infra identity.tf local.id_staff_groups, ADR
# 0006 decision 3). A user in several booking-* groups gets the first one listed here.
BOOKING_GROUP_ROLES: tuple[tuple[str, str], ...] = (
    ("booking-admin", "admin"),
    ("booking-sales", "sales"),
    ("booking-contracting", "contracting"),
    ("booking-content_creator", "content_creator"),
)
# WORKFORCE: the informational cis_role column = the highest cis-* group, if any.
CIS_GROUP_ROLES: tuple[tuple[str, str], ...] = (
    ("cis-admin", "admin"),
    ("cis-reviewer", "reviewer"),
    ("cis-content", "content"),
)
# Prefix of the staff key of rows created on a WORKFORCE sign-in (bk-016 staff_users_cognito_key_chk).
COGNITO_KEY_PREFIX = "cog:"

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
    # B2B partners (bk-019, AA-972): which agencies may call booking.b2b.v1 and through which M2M client.
    "b2b.admin": frozenset({"admin"}),
    # B2B desk (bk-022, AA-975): on-request lines to accept / decline and incidents on departures. Every operational
    # role reads and answers it (no separate ops role exists); content creators do not.
    "b2b_desk.read": frozenset({"admin", "sales", "contracting"}),
    "b2b_desk.write": frozenset({"admin", "sales", "contracting"}),
    # Operations (bk-026, AA-682 S1): supplier bookings of a sold trip and their documents. Every operational role
    # books and chases suppliers (no separate ops role yet, design D16); content creators do not.
    "ops.read": frozenset({"admin", "sales", "contracting"}),
    "ops.write": frozenset({"admin", "sales", "contracting"}),
}

# AA-911 D-1: the most a Preview-client session may do, intersected with its role's permissions. Reads only; the admin
# reads (staff.admin, audit.read, retention.admin) and every write stay out.
PREVIEW_PERMISSIONS: frozenset[str] = frozenset(
    {
        "trip_case.read",
        "customer.read",
        "staff.directory",
        "pricing.read",
        "supply.read",
        "catalog.read",
        "b2b_desk.read",
        "ops.read",
    }
)
# The only methods a Preview-client session may call (RFC 9110 safe methods that routes here serve).
PREVIEW_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# AA-910 (root ADR 0006a A10a): the Booking E2E test user. Its session never writes (see the module docstring). A code
# constant that mirrors the WORKFORCE trigger allow-map (AA-CIS-Infra workforce.E2E_BOOKING_TEST_STAFF_EMAIL); removed
# with that exception.
E2E_TEST_STAFF_EMAIL = "e2e-booking@e2e-mail.lumiguides.it.com"

# last_seen_at is refreshed at most this often (one write per staff user per 5 minutes, not per request).
LAST_SEEN_EVERY = "5 minutes"
LAST_SEEN_EVERY_TD = timedelta(minutes=5)

STAFF_COLUMNS = "cis_user_id, email, name, role, active, cis_role, created_at, updated_at, last_seen_at, cognito_sub"
# Columns every staff row dict carries; cognito_sub (bk-016) is read with .get so older fakes stay valid.
_BASE_COLUMNS = (
    "cis_user_id",
    "email",
    "name",
    "role",
    "active",
    "cis_role",
    "created_at",
    "updated_at",
    "last_seen_at",
)

bearer = HTTPBearer(
    auto_error=False, description="Cognito WORKFORCE access token (booking-staff client) or the CIS bridge token"
)


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
    cognito_sub: str | None = None
    # Session flag, not a column: True when this request's token came from the Preview-only client (AA-911 D-1).
    preview: bool = False

    @classmethod
    def from_row(cls, row: Any) -> Staff:
        return cls(**{k: row[k] for k in _BASE_COLUMNS}, cognito_sub=row.get("cognito_sub"))

    @property
    def managed_in_directory(self) -> bool:
        """Linked to a Cognito identity: the role follows the booking-* group (CIS → Platform → Staff)."""
        return self.cognito_sub is not None

    def can(self, permission: str) -> bool:
        if self.preview and permission not in PREVIEW_PERMISSIONS:
            return False
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
            "managed_in_directory": self.managed_in_directory,
        }


@dataclass(frozen=True)
class BridgeClaims:
    """What the CIS bridge token says about the caller (``sub`` = CIS ``admin_users.id``)."""

    sub: str
    email: str | None
    name: str | None
    cis_role: str


async def provision(conn: asyncpg.Connection, claims: BridgeClaims, role: str) -> Staff:
    """BRIDGE: create the profile on the first token, refresh profile fields afterwards. Returns the row."""
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


def booking_role(groups: tuple[str, ...]) -> str | None:
    """The Booking role of a WORKFORCE principal: the first booking-* group in precedence order, or None."""
    matched = [role for group, role in BOOKING_GROUP_ROLES if group in groups]
    if len(matched) > 1:
        log.info("booking_role_multiple roles=%s chosen=%s", ",".join(matched), matched[0])
    return matched[0] if matched else None


def cis_role_from_groups(groups: tuple[str, ...]) -> str | None:
    return next((role for group, role in CIS_GROUP_ROLES if group in groups), None)


def _display_name(email: str | None) -> str | None:
    """Initial name of a row created on a WORKFORCE sign-in: Cognito access tokens carry no name, and Booking does not
    read the CIS directory, so the e-mail local part stands in (``thu@adventure.asia`` → ``thu``)."""
    if not email or "@" not in email:
        return None
    return email.split("@", 1)[0][:120] or None


_IDENTITY_WHERE = "cognito_iss = $1 AND cognito_sub = $2"


async def sync_cognito_staff(conn: asyncpg.Connection, principal: Principal, role: str) -> Staff:
    """WORKFORCE: the row of this Cognito identity, created on the first sign-in, its role synced from the groups.

    Never rebinds an identity: a row is found by ``(cognito_iss, cognito_sub)`` only. The caller has already refused a
    principal without a booking-* group. Runs inside the caller's transaction.
    """
    cis_role = cis_role_from_groups(principal.groups)
    row = await conn.fetchrow(
        f"SELECT {STAFF_COLUMNS} FROM booking.staff_users WHERE {_IDENTITY_WHERE}",  # noqa: S608 — constants only
        principal.iss,
        principal.sub,
    )
    if row is None:
        key = COGNITO_KEY_PREFIX + principal.sub
        row = await conn.fetchrow(
            f"""
            INSERT INTO booking.staff_users
                (cis_user_id, email, name, role, active, cis_role, cognito_iss, cognito_sub, last_seen_at)
            VALUES ($1, $2, $3, $4, true, $5, $6, $7, now())
            ON CONFLICT DO NOTHING
            RETURNING {STAFF_COLUMNS}
            """,  # noqa: S608 — constants only
            key,
            principal.email,
            _display_name(principal.email),
            role,
            cis_role,
            principal.iss,
            principal.sub,
        )
        if row is not None:
            staff = Staff.from_row(row)
            log.info("booking_staff_provisioned source=cognito cis_user_id=%s role=%s", key, role)
            await audit.record(
                conn,
                actor_type="system",
                actor_ref=staff.cis_user_id,
                action="staff.provisioned",
                entity_type="staff_user",
                entity_id=staff.cis_user_id,
                after={**staff.public(), "source": "cognito", "client_id": principal.client_id},
                entity_label=staff.name or staff.cis_user_id,
                summary=f"Created on the first company e-mail sign-in, role {role} (Cognito group)",
            )
            return staff
        # A concurrent first sign-in created it (ON CONFLICT waited for that commit), or the key is taken.
        row = await conn.fetchrow(
            f"SELECT {STAFF_COLUMNS} FROM booking.staff_users WHERE {_IDENTITY_WHERE}",  # noqa: S608
            principal.iss,
            principal.sub,
        )
        if row is None:
            log.warning("booking_staff_identity_conflict cis_user_id=%s", key)
            raise HTTPException(status_code=403, detail="identity_conflict")
    return await _sync_row(conn, Staff.from_row(row), principal, role, cis_role)


async def _sync_row(
    conn: asyncpg.Connection, staff: Staff, principal: Principal, role: str, cis_role: str | None
) -> Staff:
    email = principal.email or staff.email
    stale = staff.last_seen_at is None or datetime.now(UTC) - staff.last_seen_at >= LAST_SEEN_EVERY_TD
    if (staff.role, staff.email, staff.cis_role) == (role, email, cis_role) and not stale:
        return staff
    locked = await conn.fetchrow(
        f"SELECT {STAFF_COLUMNS} FROM booking.staff_users WHERE cis_user_id = $1 FOR UPDATE",  # noqa: S608
        staff.cis_user_id,
    )
    if locked is None:  # deleted since the first read: nothing to sync (the next request provisions again)
        return staff
    before = Staff.from_row(locked)
    updated = Staff.from_row(
        await conn.fetchrow(
            f"""
            UPDATE booking.staff_users
               SET role = $2, email = $3, cis_role = $4, last_seen_at = now()
             WHERE cis_user_id = $1
            RETURNING {STAFF_COLUMNS}
            """,  # noqa: S608 — constants only
            staff.cis_user_id,
            role,
            email,
            cis_role,
        )
    )
    if before.role != updated.role:
        log.info(
            "booking_staff_role_synced cis_user_id=%s from=%s to=%s", updated.cis_user_id, before.role, updated.role
        )
        await audit.record(
            conn,
            actor_type="system",
            actor_ref=updated.cis_user_id,
            action="staff.role_synced",
            entity_type="staff_user",
            entity_id=updated.cis_user_id,
            before={"cis_user_id": updated.cis_user_id, "role": before.role},
            after={"cis_user_id": updated.cis_user_id, "role": updated.role},
            entity_label=updated.name or updated.cis_user_id,
            summary=f"Role {before.role} -> {updated.role} from the booking-* Cognito group",
        )
    return updated


def _bridge_claims(principal: Principal, token: str) -> BridgeClaims:
    """BRIDGE: the verified principal plus the ``name`` claim, which the vendored Principal does not carry. The token
    was verified by the caller, so reading one more claim without the signature check is safe here."""
    if principal.sub.startswith(COGNITO_KEY_PREFIX):  # never a CIS admin_users.id; such a key belongs to a Cognito row
        raise HTTPException(status_code=401, detail="invalid_token", headers={"WWW-Authenticate": "Bearer"})
    raw = jwt.decode(token, options={"verify_signature": False}).get("name")
    name = (raw.strip()[:320] or None) if isinstance(raw, str) else None
    cis_role = principal.groups[0] if len(principal.groups) == 1 else ""
    return BridgeClaims(sub=principal.sub, email=principal.email, name=name, cis_role=cis_role)


def is_e2e_test_principal(principal: Principal) -> bool:
    """True for the AA-910 E2E test user (exact address after trim + lower-case; see :data:`E2E_TEST_STAFF_EMAIL`)."""
    return isinstance(principal.email, str) and principal.email.strip().lower() == E2E_TEST_STAFF_EMAIL


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
    verifier = request.app.state.staff_verifier
    token = credentials.credentials
    try:
        principal = await verifier.verify(token)
    except InvalidToken as exc:
        raise HTTPException(status_code=401, detail="invalid_token", headers={"WWW-Authenticate": "Bearer"}) from exc
    except AuthUnavailable as exc:
        raise HTTPException(status_code=503, detail="auth_unavailable") from exc
    kind = identity_config.issuer_kind(verifier, principal.iss)
    if kind == identity_config.WORKFORCE:
        preview = identity_config.is_preview_client(request.app.state.settings, principal.client_id)
        if preview and request.method.upper() not in PREVIEW_METHODS:
            # AA-911 D-1: a Preview-host session never writes, whatever its groups. Refused before any DB access.
            log.info("booking_preview_write_refused method=%s path=%s", request.method, request.url.path)
            raise HTTPException(status_code=403, detail="preview_read_only")
        if is_e2e_test_principal(principal) and request.method.upper() not in PREVIEW_METHODS:
            # AA-910: the E2E admin's codes are readable by CI roles; it may read every page but never write.
            log.info("booking_e2e_write_refused method=%s path=%s", request.method, request.url.path)
            raise HTTPException(status_code=403, detail="e2e_read_only")
        role = booking_role(principal.groups)
        if role is None:
            raise HTTPException(status_code=403, detail="no_booking_role")
        pool = await get_pool(request)
        async with pool.acquire() as conn, conn.transaction():
            staff = await sync_cognito_staff(conn, principal, role)
        if preview:
            staff = replace(staff, preview=True)
    elif kind == identity_config.BRIDGE:
        claims = _bridge_claims(principal, token)
        role = ROLE_FROM_CIS.get(claims.cis_role)
        if role is None:
            raise HTTPException(status_code=403, detail="unknown_cis_role")
        pool = await get_pool(request)
        async with pool.acquire() as conn, conn.transaction():
            staff = await provision(conn, claims, role)
    else:  # an issuer the verifier holds but this module has no rules for: refuse
        raise HTTPException(status_code=401, detail="invalid_token", headers={"WWW-Authenticate": "Bearer"})
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
