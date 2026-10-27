"""Signed-in travellers on the customer routes ``/booking/customer/v1`` (AA-682 S5; root ADR 0006 client table,
ADR 0004 O8; contract ``booking.customer.v1``).

One route class, one verifier: the Cognito EXTERNAL pool issuer (``COGNITO_EXTERNAL_ISSUER``) x the two public
traveller clients {``tripplanner``, ``booking-customer``}, Cognito access tokens only (``token_use == "access"``,
audience = ``client_id``). It reuses the vendored multi-issuer verifier (``app.auth.identity_verifier``, never edited
here), so a WORKFORCE (staff) token, an M2M token, the CIS bridge token, an ID token or a token of another EXTERNAL
client (``cis-portal``) is simply an unknown issuer or client: 401.

Outcomes of :func:`current_traveller` (bodies ``{"detail": <code>}``, like the staff routes):

- no bearer token, or any token this route class does not accept → 401 ``sign_in_required`` (one code: the caller,
  the TripPlanner BFF, shows its sign-in prompt either way and learns nothing about why);
- no key set ever loaded for the EXTERNAL pool → 503 ``auth_unavailable``.

The verified identity is the pair ``(iss, sub)`` (ADR 0006 decision 9: never ``sub`` alone). Mapping it to a Booking
customer (``booking.customer_identities``, bk-024) is the reader's job (``app.ops.my_payments``). Nothing about the
identity (iss, sub, email) is ever logged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.auth.identity_verifier import AuthUnavailable, InvalidToken, IssuerConfig, JwksFetcher, MultiIssuerVerifier

if TYPE_CHECKING:
    from app.settings import Settings

log = logging.getLogger(__name__)

EXTERNAL = "external"
SIGN_IN_REQUIRED = "sign_in_required"

customer_bearer = HTTPBearer(
    auto_error=False,
    scheme_name="traveller",
    description="Cognito EXTERNAL access token of a signed-in traveller (clients tripplanner, booking-customer)",
)


@dataclass(frozen=True)
class Traveller:
    """A verified traveller identity. ``client_id`` is the EXTERNAL client the token was issued to."""

    iss: str
    sub: str
    client_id: str


def customer_client_ids(settings: Settings) -> frozenset[str]:
    return frozenset({settings.cognito_tripplanner_client_id, settings.cognito_booking_customer_client_id})


def customer_config(settings: Settings) -> IssuerConfig:
    """EXTERNAL x {tripplanner, booking-customer}, Cognito access-token profile (the IssuerConfig defaults)."""
    return IssuerConfig(
        issuer=settings.cognito_external_issuer,
        client_ids=customer_client_ids(settings),
        name=EXTERNAL,
    )


def build_customer_verifier(settings: Settings, fetcher: JwksFetcher | None = None) -> MultiIssuerVerifier:
    return MultiIssuerVerifier((customer_config(settings),), fetcher)


def _refused() -> HTTPException:
    return HTTPException(status_code=401, detail=SIGN_IN_REQUIRED, headers={"WWW-Authenticate": "Bearer"})


async def verify_traveller(verifier: MultiIssuerVerifier, token: str) -> Traveller:
    """Raises InvalidToken (401) or AuthUnavailable (503)."""
    principal = await verifier.verify(token)
    if principal.sub == principal.client_id:
        # A client-credentials token (sub = client id) is a service, never a traveller. The EXTERNAL pool has no
        # resource server today; refuse rather than treat a service as a person if one is ever added.
        raise InvalidToken("not a user token")
    return Traveller(iss=principal.iss, sub=principal.sub, client_id=principal.client_id)


async def current_traveller(
    request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(customer_bearer)]
) -> Traveller:
    verifier = getattr(request.app.state, "customer_verifier", None)
    if verifier is None:
        raise HTTPException(status_code=503, detail="auth_unavailable")
    if credentials is None or credentials.scheme.lower() != "bearer" or not credentials.credentials:
        raise _refused()
    try:
        return await verify_traveller(verifier, credentials.credentials)
    except InvalidToken as exc:
        log.info("customer_auth outcome=invalid")
        raise _refused() from exc
    except AuthUnavailable as exc:
        log.warning("customer_auth outcome=unavailable")
        raise HTTPException(status_code=503, detail="auth_unavailable") from exc
