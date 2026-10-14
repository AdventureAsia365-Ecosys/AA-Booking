"""Test-only CIS stand-in: a local RSA key, its JWKS document and staff JWTs signed with it."""

from __future__ import annotations

import time
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from app.auth.tokens import AUDIENCE, JwksCache, StaffTokenVerifier

ISSUER = "https://cis.test"


class LocalCis:
    def __init__(self, kid: str = "k1") -> None:
        self.kid = kid
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.fetches = 0
        self.fail_next = False

    def jwks(self) -> dict[str, Any]:
        jwk = jwt.algorithms.RSAAlgorithm.to_jwk(self.private_key.public_key(), as_dict=True)
        return {"keys": [{**jwk, "kid": self.kid, "use": "sig", "alg": "RS256"}]}

    async def fetch(self) -> dict[str, Any]:
        self.fetches += 1
        if self.fail_next:
            raise OSError("CIS down")
        return self.jwks()

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

    def verifier(self) -> StaffTokenVerifier:
        return StaffTokenVerifier(JwksCache(self.fetch), ISSUER)
