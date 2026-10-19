"""RS256 JWT verification against one JWKS: the cache and decoder of the deploy-gate verifier (``app.auth.ci``, AA-751).

Until AA-680 Phase 2 this module also verified the CIS staff JWT. Staff tokens (Cognito WORKFORCE and the CIS bridge)
now go through the multi-issuer verifier ``app.auth.identity_verifier`` (a byte copy of the CIS module), configured in
``app.auth.identity_config``; only the GitHub OIDC deploy gate still uses the cache and decoder here.

The JWKS is cached for :data:`JWKS_TTL_S`. An unknown ``kid`` forces a refetch (key rotation), at most
once per :data:`UNKNOWN_KID_REFETCH_S` so a stream of random ``kid`` values cannot make Booking hammer
the issuer. A failed fetch keeps the last good key set; with no key set at all the caller gets
:class:`AuthUnavailable` (HTTP 503), not "invalid token".
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.request
from collections.abc import Awaitable, Callable
from typing import Any

import jwt

log = logging.getLogger(__name__)

ALGORITHM = "RS256"

JWKS_TTL_S = 600.0
UNKNOWN_KID_REFETCH_S = 30.0
FETCH_TIMEOUT_S = 5.0
LEEWAY_S = 30
MAX_JWKS_BYTES = 256 * 1024

JwksFetcher = Callable[[], Awaitable[dict[str, Any]]]


class InvalidToken(Exception):
    """The bearer token is missing, malformed, expired or not signed by a current key (HTTP 401)."""


class AuthUnavailable(Exception):
    """No key set could be loaded from the JWKS (GitHub for the deploy gate): HTTP 503."""


def http_fetcher(url: str, timeout: float = FETCH_TIMEOUT_S) -> JwksFetcher:
    """GET ``url`` with the standard library in a worker thread (no extra HTTP client dependency)."""

    def _get() -> dict[str, Any]:
        request = urllib.request.Request(url, headers={"Accept": "application/json"})  # noqa: S310 — https enforced by Settings
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            body = response.read(MAX_JWKS_BYTES + 1)
        if len(body) > MAX_JWKS_BYTES:
            raise ValueError("JWKS response too large")
        return json.loads(body)

    async def fetch() -> dict[str, Any]:
        return await asyncio.to_thread(_get)

    return fetch


def _rsa_keys(document: dict[str, Any]) -> dict[str, Any]:
    """kid → public key, for every RSA signing key in a JWKS document; other keys are ignored."""
    keys: dict[str, Any] = {}
    for jwk in document.get("keys", []) if isinstance(document, dict) else []:
        if not isinstance(jwk, dict) or jwk.get("kty") != "RSA" or not isinstance(jwk.get("kid"), str):
            continue
        if jwk.get("use", "sig") != "sig" or jwk.get("alg", ALGORITHM) != ALGORITHM:
            continue
        try:
            keys[jwk["kid"]] = jwt.PyJWK(jwk, algorithm=ALGORITHM).key
        except jwt.PyJWTError as exc:
            log.warning("JWKS: skipping key %s (%s)", jwk["kid"], type(exc).__name__)
    return keys


class JwksCache:
    def __init__(
        self,
        fetcher: JwksFetcher,
        *,
        ttl: float = JWKS_TTL_S,
        unknown_kid_refetch: float = UNKNOWN_KID_REFETCH_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetcher = fetcher
        self._ttl = ttl
        self._unknown_kid_refetch = unknown_kid_refetch
        self._clock = clock
        self._keys: dict[str, Any] | None = None
        self._fetched_at = 0.0
        self._attempted_at: float | None = None
        self._lock = asyncio.Lock()

    async def _refresh(self) -> None:
        self._attempted_at = self._clock()
        try:
            keys = _rsa_keys(await self._fetcher())
        except Exception as exc:  # noqa: BLE001 — any fetch/parse failure keeps the last good key set
            log.warning("JWKS fetch failed (%s); keeping %s cached key(s)", type(exc).__name__, len(self._keys or {}))
            return
        self._keys = keys
        self._fetched_at = self._clock()

    async def key(self, kid: str) -> Any:
        async with self._lock:
            now = self._clock()
            if self._keys is None or now - self._fetched_at >= self._ttl:
                if self._attempted_at is None or now - self._attempted_at >= self._unknown_kid_refetch:
                    await self._refresh()
            elif kid not in self._keys and now - (self._attempted_at or 0.0) >= self._unknown_kid_refetch:
                await self._refresh()
            if self._keys is None:
                raise AuthUnavailable("JWKS unavailable")
            if kid not in self._keys:
                raise InvalidToken("unknown signing key")
            return self._keys[kid]


async def decode_rs256(
    jwks: JwksCache, token: str, *, audience: str, issuer: str, required: list[str]
) -> dict[str, Any]:
    """Verify an RS256 JWT against ``jwks`` (``kid`` header required) and return its claims.

    Raises :class:`InvalidToken` for anything wrong with the token itself and :class:`AuthUnavailable` (from the
    cache) when no key set could be loaded. Used by the deploy-gate verifier (``app.auth.ci``).
    """
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise InvalidToken("malformed token") from exc
    if header.get("alg") != ALGORITHM:
        raise InvalidToken("unexpected algorithm")
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise InvalidToken("missing kid")
    key = await jwks.key(kid)
    try:
        return jwt.decode(
            token,
            key,
            algorithms=[ALGORITHM],
            audience=audience,
            issuer=issuer,
            leeway=LEEWAY_S,
            options={"require": required},
        )
    except jwt.PyJWTError as exc:
        raise InvalidToken(type(exc).__name__) from exc
