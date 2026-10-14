"""Staff JWT verification (ADR 0004 O1, AA-680): CIS signs, Booking verifies against the CIS JWKS.

Contract (CIS side, AA-656/AA-680): RS256, ``kid`` header, ``iss`` = the CIS base URL, ``aud`` =
``booking``, ``exp`` (15 min tokens), ``sub`` = CIS ``shared.admin_users.id`` as a string, claims
``email``, ``name``, ``cis_role`` (``admin`` | ``reviewer`` | ``content``).

The JWKS is cached for :data:`JWKS_TTL_S`. An unknown ``kid`` forces a refetch (key rotation), at most
once per :data:`UNKNOWN_KID_REFETCH_S` so a stream of random ``kid`` values cannot make Booking hammer
CIS. A failed fetch keeps the last good key set; with no key set at all the caller gets
:class:`AuthUnavailable` (HTTP 503), not "invalid token".
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import jwt

log = logging.getLogger(__name__)

AUDIENCE = "booking"
ALGORITHM = "RS256"
CIS_ROLES = frozenset({"admin", "reviewer", "content"})

JWKS_TTL_S = 600.0
UNKNOWN_KID_REFETCH_S = 30.0
FETCH_TIMEOUT_S = 5.0
LEEWAY_S = 30
MAX_JWKS_BYTES = 256 * 1024

JwksFetcher = Callable[[], Awaitable[dict[str, Any]]]


class InvalidToken(Exception):
    """The bearer token is missing, malformed, expired or not signed by a current CIS key (HTTP 401)."""


class AuthUnavailable(Exception):
    """No CIS key set could be loaded (HTTP 503)."""


@dataclass(frozen=True)
class StaffClaims:
    sub: str
    email: str | None
    name: str | None
    cis_role: str


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
                raise AuthUnavailable("CIS JWKS unavailable")
            if kid not in self._keys:
                raise InvalidToken("unknown signing key")
            return self._keys[kid]


class StaffTokenVerifier:
    def __init__(self, jwks: JwksCache, issuer: str, audience: str = AUDIENCE) -> None:
        self._jwks = jwks
        self._issuer = issuer
        self._audience = audience

    async def verify(self, token: str) -> StaffClaims:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise InvalidToken("malformed token") from exc
        if header.get("alg") != ALGORITHM:
            raise InvalidToken("unexpected algorithm")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise InvalidToken("missing kid")
        key = await self._jwks.key(kid)
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=[ALGORITHM],
                audience=self._audience,
                issuer=self._issuer,
                leeway=LEEWAY_S,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise InvalidToken(type(exc).__name__) from exc
        sub = claims.get("sub")
        cis_role = claims.get("cis_role")
        if not isinstance(sub, str) or not sub.strip() or len(sub) > 128:
            raise InvalidToken("invalid sub")
        if not isinstance(cis_role, str) or not cis_role:
            raise InvalidToken("missing cis_role")
        return StaffClaims(
            sub=sub,
            email=_text(claims.get("email")),
            name=_text(claims.get("name")),
            cis_role=cis_role,
        )


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:320] or None
