"""Service-to-service tokens of the Cognito M2M pool ``aa-m2m-<env>`` (root ADR 0006b decision 7, AA-972).

The B2B API (``/booking/b2b/v1``, contract booking.b2b.v1) accepts exactly one route class: the M2M pool issuer x the
AA-Partner (CRM) app client, client-credentials access tokens. It reuses the vendored multi-issuer verifier
(``app.auth.identity_verifier``, never edited here) with a Cognito access-token profile whose "groups" claim is
``scope``: a client-credentials token carries ``scope`` as one space-separated string, which the verifier returns as a
one-element ``groups`` tuple and :func:`scopes_of` splits. So the token is decoded and checked once, by the shared
module.

Outcomes of :func:`verify_caller`:

- no key set ever loaded for the pool → :class:`~app.auth.identity_verifier.AuthUnavailable` (503);
- malformed, expired, badly signed, wrong issuer, ID token → 401 ``unauthorized``;
- validly signed by the pool but for a client Booking does not accept → 403 ``forbidden``. The verifier raises
  ``InvalidToken("client not allowed")`` only after the signature, issuer, expiry and ``token_use`` checks passed
  (it decodes first, then checks the client), so that message alone means "authenticated, not allowed".
  ``tests/unit/test_m2m_auth.py`` runs the real verifier and fails if the vendored message ever changes.

Scopes per route and the partner check live in ``app.b2b.access``. The pool issuer and the client id come from
``COGNITO_M2M_ISSUER`` / ``COGNITO_M2M_CRM_CLIENT_ID`` (``app.settings``); unset = no verifier, every B2B route 503.
Client ids are public values; the client secret lives only with the caller (AA-Partner) and never here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.auth.identity_verifier import (
    InvalidToken,
    IssuerConfig,
    JwksFetcher,
    MultiIssuerVerifier,
    Principal,
)

if TYPE_CHECKING:
    from app.settings import Settings

M2M = "m2m"

# Resource server "booking.b2b" of the M2M pool (AA-CIS-Infra identity_m2m.tf); scope strings as in the contract.
SCOPE_READ = "booking.b2b/read"
SCOPE_QUOTE = "booking.b2b/quote"
SCOPE_HOLD = "booking.b2b/hold"
SCOPE_BOOK = "booking.b2b/book"
SCOPES = frozenset({SCOPE_READ, SCOPE_QUOTE, SCOPE_HOLD, SCOPE_BOOK})

# The vendored verifier's message for a validly signed token of a client this route class does not accept.
CLIENT_NOT_ALLOWED = "client not allowed"


class CallerRefused(Exception):  # noqa: N818 — reads as an outcome, like InvalidToken
    """A validly signed M2M token whose client is not accepted here (HTTP 403)."""


@dataclass(frozen=True)
class M2MCaller:
    client_id: str
    scopes: frozenset[str]

    def has(self, scope: str) -> bool:
        return scope in self.scopes


def m2m_config(settings: Settings) -> IssuerConfig | None:
    """The M2M pool x {CRM client}, Cognito access-token profile with ``scope`` as the groups claim; None if unset."""
    if not settings.cognito_m2m_issuer or not settings.cognito_m2m_crm_client_id:
        return None
    return IssuerConfig(
        issuer=settings.cognito_m2m_issuer,
        client_ids=frozenset({settings.cognito_m2m_crm_client_id}),
        groups_claim="scope",
        name=M2M,
    )


def build_m2m_verifier(settings: Settings, fetcher: JwksFetcher | None = None) -> MultiIssuerVerifier | None:
    cfg = m2m_config(settings)
    return MultiIssuerVerifier((cfg,), fetcher) if cfg is not None else None


def scopes_of(principal: Principal) -> frozenset[str]:
    """Every scope of the token (the ``scope`` claim is one space-separated string; a list is accepted too)."""
    return frozenset(s for group in principal.groups for s in group.split() if s)


async def verify_caller(verifier: MultiIssuerVerifier, token: str) -> M2MCaller:
    """Raises InvalidToken (401), CallerRefused (403) or AuthUnavailable (503)."""
    try:
        principal = await verifier.verify(token)
    except InvalidToken as exc:
        if str(exc) == CLIENT_NOT_ALLOWED:
            raise CallerRefused(CLIENT_NOT_ALLOWED) from exc
        raise
    # Client-credentials tokens: sub == client_id. A user token of the same pool (none exist: the pool has no users)
    # would have another sub; refuse it rather than treat a person as a service.
    if principal.sub != principal.client_id:
        raise CallerRefused("not a client-credentials token")
    return M2MCaller(client_id=principal.client_id, scopes=scopes_of(principal))
