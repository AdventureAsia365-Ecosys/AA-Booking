"""Booking staff-surface issuers for the multi-issuer verifier (root ADR 0006 §8, ADR 0006a; AA-680 Phase 2).

Booking staff routes accept exactly one issuer:

- **WORKFORCE** (Cognito staff pool) x {booking-staff, breakglass, booking-staff-preview}: access tokens only
  (``token_use == "access"``), audience = the ``client_id`` claim (Cognito access tokens carry no ``aud``), roles from
  ``cognito:groups``. ``booking-staff-preview`` (AA-911 decision D-1, root ADR 0006a A11) is the client of the fixed
  Preview host ``booking-preview.lumiguides.it.com`` only: its sessions are capped at read-only by ``app.auth.staff``
  (:func:`is_preview_client`), whatever groups the user holds. Its id is empty until the AA-CIS-Infra apply creates
  the client; while it is empty, a preview token is simply refused (401), never accepted uncapped.
  ``cis-admin`` is deliberately left out until a CIS screen really calls the Booking API (least privilege; ADR 0006
  §8 rule 6 lists it, the AA-680 notes record the deviation).

The CIS-minted bridge token (ADR 0004 O1, CIS PR #721) is retired (AA-911, root ADR 0006a A11): its issuer is not
configured, so a bridge token, even one correctly signed by the old CIS key, is refused as ``invalid_token``.

The Dev values are code constants with optional env overrides read by ``app.settings``
(``COGNITO_WORKFORCE_ISSUER``, ``COGNITO_BOOKING_STAFF_CLIENT_ID``, ``COGNITO_BREAKGLASS_CLIENT_ID``,
``COGNITO_BOOKING_STAFF_PREVIEW_CLIENT_ID``, same names as CIS
``api/core/identity_config.py``), so the ECS task definition needs no new variable. Client ids are public values
(they appear in every authorize URL); client secrets never live here. The issuer is the pool issuer (``identity.tf``
outputs and the pool's ``/.well-known/openid-configuration``), never the custom sign-in domain.

The verifier (``app.auth.identity_verifier``) is a byte copy of AA-CIS-App ``api/core/identity_verifier.py``: never edit
it here. One JWKS cache per issuer; the lifespan prefetches the WORKFORCE key set in the background (the JWKS is reached
through the single NAT instance) and logs ``identity_jwks_prefetched`` per issuer.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from app.auth.identity_verifier import IssuerConfig, JwksFetcher, MultiIssuerVerifier

if TYPE_CHECKING:
    from app.settings import Settings

log = logging.getLogger(__name__)

# Public by design (every token and the JWKS URL carry it); the AA-902 resource-ID check skips this line.
DEV_WORKFORCE_ISSUER = "https://cognito-idp.us-west-1.amazonaws.com/us-west-1_bCcFvbSOS"  # docs-lint: ignore
DEV_BOOKING_STAFF_CLIENT_ID = "5ifl9dgkr8a5outnmsjc8v1kt3"
DEV_BREAKGLASS_CLIENT_ID = "2g3f3ncfuqdiuu1o5b70o4h0lq"
# AA-911 D-1: the Preview-only booking-staff-preview client (AA-CIS-Infra identity.tf). Empty until the Infra apply
# creates it; then the id is filled in here. Empty = no preview client accepted (fail closed).
DEV_BOOKING_STAFF_PREVIEW_CLIENT_ID = ""

# IssuerConfig.name of the accepted issuer; app.auth.staff checks it.
WORKFORCE = "workforce"


def preview_client_ids(settings: Settings) -> frozenset[str]:
    """The Preview-only client ids (AA-911 D-1): accepted, but every session from them is capped at read-only."""
    preview = settings.cognito_booking_staff_preview_client_id
    return frozenset({preview}) if preview else frozenset()


def is_preview_client(settings: Settings, client_id: str | None) -> bool:
    return bool(client_id) and client_id in preview_client_ids(settings)


def workforce_config(settings: Settings) -> IssuerConfig:
    """WORKFORCE x {booking-staff, breakglass, booking-staff-preview when configured}, Cognito access-token profile."""
    return IssuerConfig(
        issuer=settings.cognito_workforce_issuer,
        client_ids=frozenset({settings.cognito_booking_staff_client_id, settings.cognito_breakglass_client_id})
        | preview_client_ids(settings),
        name=WORKFORCE,
    )


def booking_staff_issuers(settings: Settings) -> tuple[IssuerConfig, ...]:
    """WORKFORCE only (AA-911: the CIS bridge issuer is gone)."""
    return (workforce_config(settings),)


def build_staff_verifier(settings: Settings, fetcher: JwksFetcher | None = None) -> MultiIssuerVerifier:
    return MultiIssuerVerifier(booking_staff_issuers(settings), fetcher)


def issuer_kind(verifier: MultiIssuerVerifier, iss: str) -> str | None:
    """:data:`WORKFORCE` (the IssuerConfig name of ``iss``) or None for an issuer it does not hold."""
    for cfg in verifier.issuers:
        if cfg.issuer == iss:
            return cfg.name or None
    return None


async def prefetch_staff_jwks(verifier: MultiIssuerVerifier) -> dict[str, int]:
    """Load every issuer's key set once. Never raises: a failure is logged and the verifier retries on first use."""
    try:
        counts = await verifier.prefetch()
    except Exception as exc:  # noqa: BLE001 — start-up must never fail on the identity prefetch
        log.warning("identity_jwks_prefetch_failed error=%s", type(exc).__name__)
        return {}
    for iss, count in counts.items():
        if count:
            log.info("identity_jwks_prefetched issuer=%s keys=%s", iss, count)
        else:
            log.warning("identity_jwks_prefetch_empty issuer=%s", iss)
    return counts


def start_staff_jwks_prefetch(verifier: MultiIssuerVerifier, settings: Settings) -> asyncio.Task[dict[str, int]] | None:
    """Lifespan hook: :func:`prefetch_staff_jwks` in the background, so a slow JWKS fetch through the NAT never delays
    start-up. None (nothing started) when ``ENVIRONMENT == "test"``, so TestClient runs make no network call."""
    if settings.environment == "test":
        return None
    return asyncio.create_task(prefetch_staff_jwks(verifier), name="booking-identity-jwks-prefetch")


async def stop_staff_jwks_prefetch(task: asyncio.Task[dict[str, int]] | None) -> None:
    """Cancel the prefetch if it is still running at shutdown; swallows whatever it raised."""
    if task is None:
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
