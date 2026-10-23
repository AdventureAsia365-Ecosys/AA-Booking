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
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
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


@dataclass(frozen=True)
class B2BContext:
    caller: M2MCaller
    partner: dict[str, Any]
    pool: asyncpg.Pool

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
        async with pool.acquire() as conn:
            row = await conn.fetchrow(PARTNER_SQL, tenant)
        if row is None or row["status"] != "active":
            raise B2BError("unknown_partner", "no active B2B partner for this tenant")
        if row["m2m_client_id"] != caller.client_id:
            log.info("b2b_partner_client_mismatch partner=%s", row["id"])
            raise B2BError("forbidden", "this client may not act for this partner")
        return B2BContext(caller=caller, partner=dict(row), pool=pool)

    return dependency
