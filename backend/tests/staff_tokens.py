"""Test-only token issuers: a local CIS bridge and a local Cognito WORKFORCE pool, each with its own RSA key and JWKS.

``LocalCis().verifier()`` is the Booking staff verifier built from the production issuer profiles
(``app.auth.identity_config``): WORKFORCE x {booking-staff, breakglass} plus the bridge. ``cis.token()`` mints a bridge
token, ``cis.workforce.token()`` a Cognito-shaped access token.
"""

from __future__ import annotations

import time
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from app.auth import identity_config
from app.auth.identity_verifier import MultiIssuerVerifier
from app.settings import Settings

ISSUER = "https://cis.test"
JWKS_URL = f"{ISSUER}/auth/.well-known/jwks.json"
AUDIENCE = identity_config.BRIDGE_AUDIENCE
WORKFORCE_ISSUER = "https://cognito-idp.us-west-1.amazonaws.com/us-west-1_TESTPOOL"
BOOKING_STAFF_CLIENT = "bookingstaffclient00000001"
BREAKGLASS_CLIENT = "breakglassclient0000000001"
CIS_ADMIN_CLIENT = "cisadminclient000000000001"
WORKFORCE_SUB = "3a1b0c9e-0000-4000-8000-00000000c0de"


def _jwk(private_key: Any, kid: str) -> dict[str, Any]:
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    return {**jwk, "kid": kid, "use": "sig", "alg": "RS256"}


class LocalWorkforce:
    """A Cognito WORKFORCE pool stand-in: access tokens with client_id, token_use, cognito:groups, email."""

    def __init__(self, kid: str = "w1") -> None:
        self.kid = kid
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def jwks(self) -> dict[str, Any]:
        return {"keys": [_jwk(self.private_key, self.kid)]}

    def token(
        self,
        groups: list[str] | None = None,
        *,
        sub: str = WORKFORCE_SUB,
        email: str | None = "thu@adventure.asia",
        client_id: str = BOOKING_STAFF_CLIENT,
        token_use: str = "access",  # noqa: S107 — a claim value, not a password
        iss: str = WORKFORCE_ISSUER,
        exp_in: int = 900,
        kid: str | None = None,
        key: Any = None,
        algorithm: str = "RS256",
        **extra: Any,
    ) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "sub": sub,
            "iss": iss,
            "client_id": client_id,
            "token_use": token_use,
            "username": sub,
            "iat": now,
            "exp": now + exp_in,
            "cognito:groups": ["booking-sales"] if groups is None else groups,
            **extra,
        }
        if email is not None:
            claims["email"] = email
        return jwt.encode(claims, key or self.private_key, algorithm=algorithm, headers={"kid": kid or self.kid})


class LocalCis:
    """The CIS bridge stand-in (and, as ``.workforce``, the WORKFORCE pool next to it)."""

    def __init__(self, kid: str = "k1") -> None:
        self.kid = kid
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.workforce = LocalWorkforce()
        self.fetches = 0
        self.fail_next = False

    def jwks(self) -> dict[str, Any]:
        return {"keys": [_jwk(self.private_key, self.kid)]}

    async def fetch(self, url: str = JWKS_URL) -> dict[str, Any]:
        """JWKS by URL: the bridge's or the WORKFORCE pool's. ``fail_next`` makes every fetch fail (JWKS down)."""
        self.fetches += 1
        if self.fail_next:
            raise OSError("JWKS down")
        if url == JWKS_URL:
            return self.jwks()
        if url == f"{WORKFORCE_ISSUER}/.well-known/jwks.json":
            return self.workforce.jwks()
        raise OSError(f"unexpected JWKS URL {url}")

    def token(
        self,
        sub: str = "6f1c1f9e-0000-4000-8000-000000000001",
        cis_role: str | None = "admin",
        *,
        email: str | None = "staff@example.test",
        name: str | None = "Staff One",
        aud: str = AUDIENCE,
        iss: str = ISSUER,
        exp_in: int = 900,
        kid: str | None = None,
        key: Any = None,
        algorithm: str = "RS256",
        **extra: Any,
    ) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {"sub": sub, "aud": aud, "iss": iss, "iat": now, "exp": now + exp_in, **extra}
        if cis_role is not None:
            claims["cis_role"] = cis_role
        if email is not None:
            claims["email"] = email
        if name is not None:
            claims["name"] = name
        headers = {} if kid == "" else {"kid": kid or self.kid}
        return jwt.encode(claims, key or self.private_key, algorithm=algorithm, headers=headers)

    def settings(self) -> Settings:
        return Settings(
            database_url=None,
            environment="test",
            staff_jwt_issuer=ISSUER,
            cis_jwks_url=JWKS_URL,
            cognito_workforce_issuer=WORKFORCE_ISSUER,
            cognito_booking_staff_client_id=BOOKING_STAFF_CLIENT,
            cognito_breakglass_client_id=BREAKGLASS_CLIENT,
        )

    def verifier(self) -> MultiIssuerVerifier:
        """The Booking staff verifier (production issuer profiles) over the two local issuers."""
        return identity_config.build_staff_verifier(self.settings(), self.fetch)
