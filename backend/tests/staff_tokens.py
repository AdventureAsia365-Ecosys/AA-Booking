"""Test-only token issuers: a local Cognito WORKFORCE pool and a generic local RSA issuer, each with its own key + JWKS.

``LocalStaff().verifier()`` is the Booking staff verifier built from the production issuer profile
(``app.auth.identity_config``): WORKFORCE x {booking-staff, breakglass} only (AA-911: the CIS bridge is retired).
``staff.token(sub, role)`` mints a Cognito-shaped access token in the ``booking-<role>`` group;
``staff.workforce.token(...)`` gives full control over the claims.

``LocalRsaIssuer`` is any other RS256 issuer (a foreign signer): the retired CIS bridge shape for refusal tests, or a
non-GitHub key for the deploy gate.

Fixture rows that a test wants to sign in to by their own key (not ``cog:<sub>``) must be linked to the WORKFORCE
identity: insert them with ``cognito_iss = WORKFORCE_ISSUER`` and ``cognito_sub = <the key>`` (or call
:func:`seed_linked_staff`), then sign in with ``staff.token(<the key>, <the row's role>)``.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from app import audit
from app.auth import identity_config
from app.auth.identity_verifier import MultiIssuerVerifier
from app.auth.staff import STAFF_COLUMNS, Staff
from app.settings import Settings

if TYPE_CHECKING:
    import asyncpg

# The retired CIS bridge issuer (production value) and audience: only ever used to prove such tokens are refused.
BRIDGE_ISSUER = "https://api-cis.lumiguides.it.com"
BRIDGE_AUDIENCE = "booking"
FOREIGN_ISSUER = "https://foreign.test"
WORKFORCE_ISSUER = "https://cognito-idp.us-west-1.amazonaws.com/us-west-1_TESTPOOL"
WORKFORCE_JWKS_URL = f"{WORKFORCE_ISSUER}/.well-known/jwks.json"
BOOKING_STAFF_CLIENT = "bookingstaffclient00000001"
BREAKGLASS_CLIENT = "breakglassclient0000000001"
CIS_ADMIN_CLIENT = "cisadminclient000000000001"
WORKFORCE_SUB = "3a1b0c9e-0000-4000-8000-00000000c0de"


def _jwk(private_key: Any, kid: str) -> dict[str, Any]:
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    return {**jwk, "kid": kid, "use": "sig", "alg": "RS256"}


def booking_group(role: str) -> str:
    """The WORKFORCE group that carries a Booking role (AA-CIS-Infra identity.tf local.id_staff_groups)."""
    return f"booking-{role}"


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


class LocalRsaIssuer:
    """Any other RS256 issuer with its own key and JWKS. Default claims = the retired CIS bridge token shape."""

    def __init__(self, kid: str = "k1") -> None:
        self.kid = kid
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.fetches = 0
        self.fail_next = False

    def jwks(self) -> dict[str, Any]:
        return {"keys": [_jwk(self.private_key, self.kid)]}

    async def fetch(self, url: str | None = None) -> dict[str, Any]:
        """This issuer's JWKS (any URL). ``fail_next`` makes every fetch fail (JWKS down)."""
        self.fetches += 1
        if self.fail_next:
            raise OSError("JWKS down")
        return self.jwks()

    def token(
        self,
        sub: str = "6f1c1f9e-0000-4000-8000-000000000001",
        cis_role: str | None = "admin",
        *,
        email: str | None = "staff@example.test",
        name: str | None = "Staff One",
        aud: str = BRIDGE_AUDIENCE,
        iss: str = BRIDGE_ISSUER,
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


class LocalStaff:
    """The Booking staff sign-in in tests: the local WORKFORCE pool and the production verifier profile over it.

    ``foreign`` (optional) is served by :meth:`fetch` at its own ``/auth/.well-known/jwks.json`` URL, so a test can
    prove the verifier never asks for it.
    """

    def __init__(self, foreign: LocalRsaIssuer | None = None) -> None:
        self.workforce = LocalWorkforce()
        self.foreign = foreign
        self.fetches = 0
        self.fetched: list[str] = []
        self.fail_next = False

    async def fetch(self, url: str = WORKFORCE_JWKS_URL) -> dict[str, Any]:
        """JWKS by URL. ``fail_next`` makes every fetch fail (JWKS down)."""
        self.fetches += 1
        self.fetched.append(url)
        if self.fail_next:
            raise OSError("JWKS down")
        if url == WORKFORCE_JWKS_URL:
            return self.workforce.jwks()
        if self.foreign is not None and url == f"{BRIDGE_ISSUER}/auth/.well-known/jwks.json":
            return self.foreign.jwks()
        raise OSError(f"unexpected JWKS URL {url}")

    def token(self, sub: str = WORKFORCE_SUB, role: str = "sales", *, email: str | None = None, **extra: Any) -> str:
        """A WORKFORCE access token for ``sub`` in the ``booking-<role>`` group (no e-mail by default, so a linked
        fixture row keeps its own e-mail)."""
        return self.workforce.token([booking_group(role)], sub=sub, email=email, **extra)

    def settings(self) -> Settings:
        return Settings(
            database_url=None,
            environment="test",
            cognito_workforce_issuer=WORKFORCE_ISSUER,
            cognito_booking_staff_client_id=BOOKING_STAFF_CLIENT,
            cognito_breakglass_client_id=BREAKGLASS_CLIENT,
        )

    def verifier(self) -> MultiIssuerVerifier:
        """The Booking staff verifier (production issuer profile) over the local WORKFORCE pool."""
        return identity_config.build_staff_verifier(self.settings(), self.fetch)


async def seed_linked_staff(
    conn: asyncpg.Connection, sub: str, role: str, *, active: bool = True, cis_role: str | None = None
) -> Staff:
    """A staff row keyed by ``sub`` and linked to the local WORKFORCE identity ``sub``, as an earlier sign-in would have
    left it (replaces the retired bridge ``provision()`` in DB-level tests). The first insert writes one
    ``staff.provisioned`` audit row like a real first sign-in; a repeat returns the existing row unchanged."""
    row = await conn.fetchrow(
        f"""
        INSERT INTO booking.staff_users AS s
            (cis_user_id, email, name, role, active, cis_role, cognito_iss, cognito_sub, last_seen_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $1, now())
        ON CONFLICT (cis_user_id) DO UPDATE SET last_seen_at = s.last_seen_at
        RETURNING {STAFF_COLUMNS}, (xmax = 0) AS inserted
        """,  # noqa: S608 — constants only
        sub,
        f"{sub}@example.test",
        sub.title(),
        role,
        active,
        cis_role,
        WORKFORCE_ISSUER,
    )
    staff = Staff.from_row(row)
    if row["inserted"]:
        await audit.record(
            conn,
            actor_type="system",
            actor_ref=staff.cis_user_id,
            action="staff.provisioned",
            entity_type="staff_user",
            entity_id=staff.cis_user_id,
            after=staff.public(),
        )
    return staff
