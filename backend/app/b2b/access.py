"""Who may call a B2B route: an M2M token with the route's scope, for an active partner bound to that client.

Order of checks (each refusal in the contract's Problem shape, ``app.b2b.errors``):

1. B2B not configured (no M2M pool settings) or no DB → 503 ``unavailable``.
2. Bearer token (``app.auth.m2m.verify_caller``): missing / invalid → 401 ``unauthorized``; JWKS never loaded → 503
   ``unavailable``; validly signed for a client Booking does not accept → 403 ``forbidden``.
3. The route's scope missing from the token → 403 ``forbidden``.
4. ``X-AA-Tenant-Id`` missing or not a UUID → 422 ``invalid``.
5. No ``booking.b2b_partners`` row for the tenant, or the row is suspended → 403 ``unknown_partner``.
6. The token's client is not the partner's ``m2m_client_id`` → 403 ``forbidden``.

Authentication comes before any partner lookup, so an unauthenticated caller learns nothing about partners.

**Tenant binding (AA-1092, the database fence of bk-049).** Every connection a B2B request takes, from the partner
lookup on, comes from a :class:`TenantBoundPool`: ``app.tenant_id`` is set to the agency's tenant for as long as the
connection is held and cleared before it goes back to the pool. Under the bk-049 policies such a connection reads only
that tenant's ``booking.b2b_partners`` row and writes none, so a partner-facing query that forgets its ``partner_id``
filter still cannot see another agency's registry row. The lookup binds the ``X-AA-Tenant-Id`` it was given (it reads
only that row anyway); the handlers bind the tenant of the partner the token was proven to act for (the same value).
The back office (staff routes, jobs) takes connections from the plain pool and binds nothing, as bk-049 designed.

The binding is session level (``set_config(..., false)``) for the time the connection is held, not transaction local:
most B2B reads run single statements outside an explicit transaction, where a transaction-local setting would end
with the first statement. It is cleared in a ``finally``; if clearing fails the connection is terminated instead of
returned, and asyncpg's own release reset (``RESET ALL``) clears it again, so a pooled connection never carries a
tenant into a back office request.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any

import asyncpg
from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.auth.identity_verifier import AuthUnavailable, InvalidToken
from app.auth.m2m import SCOPES, CallerRefused, M2MCaller, verify_caller
from app.b2b.errors import B2BError

log = logging.getLogger(__name__)

m2m_bearer = HTTPBearer(
    auto_error=False, scheme_name="m2m", description="Cognito aa-m2m client-credentials access token (booking.b2b.v1)"
)

PARTNER_SQL = """
SELECT id::text AS id, tenant_id::text AS tenant_id, agency_name, status, billing_currency::text AS billing_currency,
       m2m_client_id
FROM booking.b2b_partners WHERE tenant_id = $1::uuid
"""
BIND_SQL = "SELECT set_config('app.tenant_id', $1, false)"
UNBIND_SQL = "SELECT set_config('app.tenant_id', '', false)"


class TenantBoundPool:
    """The pool a B2B request uses: each connection it hands out has ``app.tenant_id`` bound to one tenant.

    The asyncpg pool methods the B2B code calls (``acquire`` as an async context manager, ``fetch``, ``fetchrow``,
    ``fetchval``, ``execute``); anything else is not there on purpose, so no code path reaches the plain pool by a
    side door.
    """

    def __init__(self, pool: asyncpg.Pool, tenant_id: str) -> None:
        self._pool = pool
        self.tenant_id = str(tenant_id)

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[asyncpg.Connection]:
        async with self._pool.acquire() as conn:
            await conn.execute(BIND_SQL, self.tenant_id)
            try:
                yield conn
            finally:
                await _unbind(conn)

    async def fetch(self, sql: str, *args: Any) -> Any:
        async with self.acquire() as conn:
            return await conn.fetch(sql, *args)

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        async with self.acquire() as conn:
            return await conn.fetchrow(sql, *args)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        async with self.acquire() as conn:
            return await conn.fetchval(sql, *args)

    async def execute(self, sql: str, *args: Any) -> str:
        async with self.acquire() as conn:
            return await conn.execute(sql, *args)


async def _unbind(conn: asyncpg.Connection) -> None:
    try:
        await conn.execute(UNBIND_SQL)
    except Exception:  # noqa: BLE001 — a connection that cannot be cleared must never be reused
        log.warning("b2b_tenant_unbind_failed: connection terminated")
        conn.terminate()


@dataclass(frozen=True)
class B2BContext:
    caller: M2MCaller
    partner: dict[str, Any]
    pool: TenantBoundPool  # every connection bound to the partner's tenant (AA-1092)

    @property
    def partner_id(self) -> str:
        return str(self.partner["id"])


async def _pool(request: Request) -> asyncpg.Pool:
    db = request.app.state.db
    if not db.configured:
        raise B2BError("unavailable", "the service is not ready")
    return await db.pool()


async def _caller(request: Request, credentials: HTTPAuthorizationCredentials | None) -> M2MCaller:
    verifier = getattr(request.app.state, "m2m_verifier", None)
    if verifier is None:
        raise B2BError("unavailable", "the B2B API is not configured")
    if credentials is None or credentials.scheme.lower() != "bearer" or not credentials.credentials:
        raise B2BError("unauthorized", "a bearer token is required")
    try:
        return await verify_caller(verifier, credentials.credentials)
    except CallerRefused as exc:
        log.info("b2b_caller_refused reason=%s", exc)
        raise B2BError("forbidden", "this client may not call the B2B API") from exc
    except InvalidToken as exc:
        raise B2BError("unauthorized", "invalid token") from exc
    except AuthUnavailable as exc:
        raise B2BError("unavailable", "token keys are not available yet", retry_after_s=5) from exc


def require_scope(scope: str) -> Callable[..., Awaitable[B2BContext]]:
    if scope not in SCOPES:
        raise ValueError(f"unknown scope {scope!r}")

    async def dependency(
        request: Request,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(m2m_bearer)],
        x_aa_tenant_id: Annotated[
            str | None, Header(alias="X-AA-Tenant-Id", description="The calling agency's CIS tenant id (uuid)")
        ] = None,
    ) -> B2BContext:
        caller = await _caller(request, credentials)
        if not caller.has(scope):
            raise B2BError("forbidden", f"the token lacks scope {scope}")
        try:
            tenant = uuid.UUID((x_aa_tenant_id or "").strip())
        except ValueError as exc:
            raise B2BError("invalid", "X-AA-Tenant-Id must be a UUID") from exc
        pool = await _pool(request)
        async with TenantBoundPool(pool, str(tenant)).acquire() as conn:
            row = await conn.fetchrow(PARTNER_SQL, tenant)
        if row is None or row["status"] != "active":
            raise B2BError("unknown_partner", "no active B2B partner for this tenant")
        if row["m2m_client_id"] != caller.client_id:
            log.info("b2b_partner_client_mismatch partner=%s", row["id"])
            raise B2BError("forbidden", "this client may not act for this partner")
        return B2BContext(caller=caller, partner=dict(row), pool=TenantBoundPool(pool, row["tenant_id"]))

    return dependency
