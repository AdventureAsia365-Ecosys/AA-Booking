"""Booking custom staff roles: who holds one and what it may do (AA-1155 T5; root spec R4.1-R4.3, design D2-D4; root
ADR 0006a A14).

Built-in roles stay in code (:data:`app.auth.staff.PERMISSIONS`); ``booking.staff_role`` /
``booking.staff_role_capability`` (bk-062) hold custom roles only. A person holds one when their WORKFORCE token carries
the group :data:`CUSTOM_GROUP` and no built-in ``booking-*`` group (a built-in group always wins: an administrator never
depends on these tables). WHICH custom role is the CIS Staff page grant, read through the CIS contract view
``shared.v_staff_booking_grant`` (CIS 462, contract ``staff.roles.v1``: ``cognito_sub``, ``booking_role_code``,
``is_owner``, ``is_business_owner``; SELECT to role ``booking`` only).

Resolution (:func:`resolve`, called by :func:`app.auth.staff.current_staff` for a ``booking-custom`` holder):

1. the view row of the token's ``sub`` (none, or a code that is not a custom code: 403 ``no_booking_role``);
2. the role row and its capabilities (unknown or archived: 403 ``no_booking_role``);
3. the capabilities kept are those the code knows and that are not in the approve class (:func:`assignable`): a row
   that slipped past the editor and the bk-062 CHECK is dropped and logged, never granted.

Fail closed for custom holders only (R4.2): a database error on the view or the tables is 403
``custom_role_unavailable`` and is not cached, so the next request tries again. Built-in holders never reach this
module. A view row and a role are each reused for at most :data:`CACHE_S` per process (R4.1); the role editor clears
the process cache on every save (:func:`forget`), the other tasks pick a change up within that bound.

The owner marker (``is_owner``) gates the role editor (R4.4, :mod:`app.auth.role_editor`). It is read from the same
view, uncached (:func:`is_owner`): an owner change in CIS applies to the next editor write.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import asyncpg
from fastapi import HTTPException

log = logging.getLogger(__name__)

# The WORKFORCE group of a custom-role holder (AA-CIS-Infra identity.tf, A14). It grants nothing by itself.
CUSTOM_GROUP = "booking-custom"
# Root spec D3 (CIS 462 and bk-062 CHECKs): never equal to a built-in role name.
CUSTOM_CODE = re.compile(r"^custom_[a-z][a-z0-9_]{1,46}$")
# Root spec D4 (bk-062 staff_role_capability_not_approve_chk): the approve class, reserved for built-in roles.
APPROVE_SUFFIX = re.compile(r"\.approve(_[a-z0-9_]+)?$")
APPROVE_NAMED = frozenset(
    {
        "supplier.activate",
        "contract.publish",
        "stop_sale.approve",
        "payout.write",
        "payment.verify",
        "billing_config.write",
        "staff.admin",
    }
)

# Root spec R4.1 sets the bound ("cached at most 60 seconds"): how stale a revoked or archived custom role may be on a
# running task. It is a security ceiling of the spec, not a business value: raising it would let a revoked holder act
# longer, so it is not offered as an editable setting.
CACHE_S = 60.0  # technical-constant: root spec R4.1 upper bound of the custom-role cache, a revocation guarantee
CACHE_SIZE = 1024  # technical-constant: entries per process cache (staff count is in the tens), cleared when full

# 403 details (the staff admin shows them on the sign-in error page).
NO_BOOKING_ROLE = "no_booking_role"
UNAVAILABLE = "custom_role_unavailable"

# The seeded role (bk-062 section 5; root spec R2.3, tech lead S285 audit section 3.3 and Q3). The unit test
# tests/unit/test_aa1155_role_editor.py keeps this set, the migration's seed array and PERMISSIONS in step.
SEED_CODE = "custom_product_ops_accounting"
SEED_LABEL = "Product, ops & accounting"
SEED_BASED_ON = "contracting"
SEED_CAPABILITIES = frozenset(
    {
        "supply.read",
        "supply.write",
        "pricing.read",
        "pricing.write",
        "trip_plan.write",
        "stop_sale.write",
        "catalog.read",
        "ops.read",
        "ops.write",
        "b2b_desk.read",
        "b2b_desk.write",
        "staff.directory",
        "bill.read",
        "bill.write",
        "billing.read",
        "billing.write",
        "agency_billing.read",
        "agency_billing.write",
        "profit.read",
        "b2b_desk.book",
        "trip_case.read",
        "trip_case.write",
        "customer.read",
        "customer.write",
    }
)

GRANT_SQL = (
    "SELECT booking_role_code, is_owner, is_business_owner FROM shared.v_staff_booking_grant WHERE cognito_sub = $1"
)
ROLE_SQL = (
    "SELECT r.code, r.label, r.based_on, r.archived,"
    " coalesce(array_agg(c.capability ORDER BY c.capability) FILTER (WHERE c.capability IS NOT NULL), '{}')"
    " AS capabilities"
    " FROM booking.staff_role r LEFT JOIN booking.staff_role_capability c ON c.role_code = r.code"
    " WHERE r.code = $1 GROUP BY r.code"
)


def is_approve_class(capability: str) -> bool:
    """Root spec D4: ``*.approve``, ``*.approve_<x>`` and the named approvals; never held by a custom role."""
    return bool(APPROVE_SUFFIX.search(capability)) or capability in APPROVE_NAMED


def known_capabilities() -> frozenset[str]:
    from app.auth.staff import PERMISSIONS  # noqa: PLC0415 — staff imports this module

    return frozenset(PERMISSIONS)


def assignable() -> frozenset[str]:
    """Every capability a custom role may hold: known to the code and not in the approve class."""
    return frozenset(p for p in known_capabilities() if not is_approve_class(p))


@dataclass(frozen=True)
class Grant:
    """One row of ``shared.v_staff_booking_grant``."""

    role_code: str | None
    is_owner: bool
    is_business_owner: bool


@dataclass(frozen=True)
class CustomRole:
    code: str
    label: str
    based_on: str
    archived: bool
    capabilities: frozenset[str]


class Unavailable(Exception):
    """The view or the role tables could not be read."""


_grants: dict[str, tuple[float, Grant | None]] = {}
_roles: dict[str, tuple[float, CustomRole | None]] = {}


def forget() -> None:
    """Drop both process caches (tests; the role editor after a save)."""
    _grants.clear()
    _roles.clear()


def _cached(cache: dict[str, tuple[float, Any]], key: str) -> tuple[bool, Any]:
    hit = cache.get(key)
    if hit is not None and time.monotonic() - hit[0] < CACHE_S:
        return True, hit[1]
    return False, None


def _store(cache: dict[str, tuple[float, Any]], key: str, value: Any) -> None:
    if len(cache) >= CACHE_SIZE:
        cache.clear()
    cache[key] = (time.monotonic(), value)


def _grant(row: Any) -> Grant | None:
    if row is None:
        return None
    return Grant(row["booking_role_code"], row["is_owner"] is True, row["is_business_owner"] is True)


async def read_grant(conn: asyncpg.Connection | asyncpg.Pool, sub: str) -> Grant | None:
    """The view row of ``sub``, uncached. Raises :class:`Unavailable` on any database error."""
    try:
        return _grant(await conn.fetchrow(GRANT_SQL, sub))
    except (asyncpg.PostgresError, OSError) as exc:
        log.warning("booking_custom_role_grant_unavailable error_class=%s", type(exc).__name__)
        raise Unavailable from exc


async def grant_for(pool: asyncpg.Pool, sub: str) -> Grant | None:
    """The view row of ``sub`` (cached :data:`CACHE_S`, an absent row too). Raises :class:`Unavailable`."""
    hit, value = _cached(_grants, sub)
    if hit:
        return value
    async with pool.acquire() as conn:
        grant = await read_grant(conn, sub)
    _store(_grants, sub, grant)
    return grant


def role_from_row(row: Any) -> CustomRole:
    return CustomRole(
        code=row["code"],
        label=row["label"],
        based_on=row["based_on"],
        archived=row["archived"] is True,
        capabilities=frozenset(row["capabilities"] or ()),
    )


async def read_role(conn: asyncpg.Connection | asyncpg.Pool, code: str) -> CustomRole | None:
    """The role ``code`` with its stored capabilities, uncached. Raises :class:`Unavailable` on a database error."""
    try:
        row = await conn.fetchrow(ROLE_SQL, code)
    except (asyncpg.PostgresError, OSError) as exc:
        log.warning("booking_custom_role_table_unavailable error_class=%s", type(exc).__name__)
        raise Unavailable from exc
    return None if row is None else role_from_row(row)


async def role_for(pool: asyncpg.Pool, code: str) -> CustomRole | None:
    """:func:`read_role` cached :data:`CACHE_S` (an unknown code too)."""
    hit, value = _cached(_roles, code)
    if hit:
        return value
    async with pool.acquire() as conn:
        role = await read_role(conn, code)
    _store(_roles, code, role)
    return role


def effective(role: CustomRole) -> frozenset[str]:
    """The capabilities granted: the stored ones the code knows and that are not in the approve class."""
    allowed = assignable()
    dropped = sorted(role.capabilities - allowed)
    if dropped:
        log.warning("booking_custom_role_capability_dropped role=%s capabilities=%s", role.code, ",".join(dropped))
    return role.capabilities & allowed


def _refuse(detail: str, reason: str) -> HTTPException:
    log.info("booking_custom_role_refused reason=%s", reason)
    return HTTPException(status_code=403, detail=detail)


async def resolve(pool: asyncpg.Pool, sub: str) -> CustomRole:
    """The custom role of a ``booking-custom`` holder, its capabilities already narrowed (:func:`effective`).

    403 ``no_booking_role``: no grant, a code that is not a custom code, an unknown or archived role.
    403 ``custom_role_unavailable``: the view or the tables could not be read (fail closed, R4.2)."""
    try:
        grant = await grant_for(pool, sub)
        if grant is None or not isinstance(grant.role_code, str) or not CUSTOM_CODE.fullmatch(grant.role_code):
            raise _refuse(NO_BOOKING_ROLE, "no_custom_grant")
        role = await role_for(pool, grant.role_code)
    except Unavailable as exc:
        raise _refuse(UNAVAILABLE, "unavailable") from exc
    if role is None or role.archived:
        raise _refuse(NO_BOOKING_ROLE, "unknown_or_archived")
    return CustomRole(role.code, role.label, role.based_on, role.archived, effective(role))


async def is_owner(conn: asyncpg.Connection | asyncpg.Pool, cognito_sub: str | None) -> bool:
    """The CIS ``is_owner`` marker of this identity, uncached; False when there is no identity, no row or the view
    cannot be read (fail closed)."""
    if not cognito_sub:
        return False
    try:
        grant = await read_grant(conn, cognito_sub)
    except Unavailable:
        return False
    return grant is not None and grant.is_owner
