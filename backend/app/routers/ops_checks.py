"""``GET /booking/ops/checks`` — the Booking in-app check registry for the CIS alerts catalogue (AA-1171, S287 decision
D1: "Booking / CRM in-app checks from a registry each repo exports, no cross-schema reads").

The CIS admin page Platform › Alerts › Catalogue lists every alert condition of the platform. Booking's two job checks
(:data:`app.jobs.ops_checks.REGISTRY`) are described here with their thresholds **in force** (the pricing policy
parameters of bk-063, Pricing > Policy) and the last alert each sent (the ``bk_ops_alert`` ledger rows of
``booking.job``), so the catalogue shows live values without reading the ``booking`` schema.

Auth: a WORKFORCE (staff Cognito) access token of the booking-staff client or, for this route only, of the CIS admin
client (``COGNITO_CIS_ADMIN_CLIENT_ID``, :func:`app.auth.identity_config.build_ops_reader_verifier`; the CIS API relays
the signed-in staff member's own token), of a member of ``cis-admin`` or of any ``booking-*`` group. Unlike the staff
routes it writes no ``staff_users`` row and needs no Booking role: it returns no customer, case or price, only check
names, thresholds and alert times. 401 without a valid token, 403 ``not_allowed`` for another group, 503
``policy_unavailable`` when a threshold has no value in force. The body follows the shared "ops check registry v1"
shape (AA-CIS-App ``api/services/alerts_catalogue.py`` ``peer_rows``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel

from app.auth import identity_config
from app.auth.identity_verifier import AuthUnavailable, InvalidToken, Principal
from app.auth.staff import BOOKING_GROUP_ROLES, bearer, get_pool
from app.errors import Unavailable
from app.jobs import ops_checks
from app.pricing import policy_params, policy_values

router = APIRouter(prefix="/booking/ops", tags=["ops"])

READER_GROUPS = frozenset({"cis-admin", *(group for group, _ in BOOKING_GROUP_ROLES)})
WHERE = "Booking staff admin › Pricing › Policy (section Job checks)"
LAST_ALERTS_SQL = """
SELECT split_part(idempotency_key, ':', 2) AS check_key, max(created_at) AS last_at
FROM booking.job
WHERE kind = $1 AND created_at >= now() - interval '7 days'
GROUP BY 1
"""


class RegistryThreshold(BaseModel):
    setting: str
    value: Any
    unit: str | None
    editable_via: str
    where: str
    label: str


class RegistryCheck(BaseModel):
    key: str
    title: str
    component: str
    severity: str
    kind: str
    action: str
    thresholds: list[RegistryThreshold]
    channel: list[str]
    last_alert_at: str | None


class CheckRegistry(BaseModel):
    """The "ops check registry v1" shape (the CIS alerts catalogue reads it; AA-Partner serves the same)."""

    app: str
    version: int
    as_of: str
    checks: list[RegistryCheck]


async def platform_reader(
    request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]
) -> Principal:
    if credentials is None or credentials.scheme.lower() != "bearer" or not credentials.credentials:
        raise HTTPException(status_code=401, detail="missing_bearer_token", headers={"WWW-Authenticate": "Bearer"})
    # The booking-staff client first (a Booking staff member), then the CIS admin client (the CIS catalogue relay).
    verifiers = [
        v
        for v in (request.app.state.staff_verifier, getattr(request.app.state, "ops_reader_verifier", None))
        if v is not None
    ]
    for verifier in verifiers:
        try:
            principal = await verifier.verify(credentials.credentials)
        except InvalidToken:
            continue
        except AuthUnavailable as exc:
            raise HTTPException(status_code=503, detail="auth_unavailable") from exc
        if identity_config.issuer_kind(verifier, principal.iss) != identity_config.WORKFORCE:
            raise HTTPException(status_code=403, detail="not_allowed")
        if not READER_GROUPS.intersection(principal.groups):
            raise HTTPException(status_code=403, detail="not_allowed")
        return principal
    raise HTTPException(status_code=401, detail="invalid_token", headers={"WWW-Authenticate": "Bearer"})


def registry_body(policy_values_in_force: dict[str, Any], last_alerts: dict[str, datetime], now: datetime) -> dict:
    """The registry answer from the policy values in force and the last alert per check. Pure."""
    checks = []
    for entry in ops_checks.REGISTRY:
        thresholds = []
        for key in entry["thresholds"]:
            param = policy_params.PARAMS[key]
            thresholds.append(
                {
                    "setting": key,
                    "value": policy_values_in_force.get(key),
                    "unit": param.unit,
                    "editable_via": "settings",
                    "where": WHERE,
                    "label": param.label,
                }
            )
        last = last_alerts.get(entry["key"])
        checks.append(
            {
                "key": entry["key"],
                "title": entry["title"],
                "component": entry["component"],
                "severity": entry["severity"],
                "kind": entry["kind"],
                "action": entry["action"],
                "thresholds": thresholds,
                "channel": ["alerts topic e-mail (once per check and UTC day)"],
                "last_alert_at": last.isoformat() if last else None,
            }
        )
    return {"app": "booking", "version": 1, "as_of": now.isoformat(), "checks": checks}


@router.get("/checks", response_model=CheckRegistry)
async def ops_check_registry(request: Request, _: Annotated[Principal, Depends(platform_reader)]) -> dict[str, Any]:
    pool = await get_pool(request)
    keys = [k for entry in ops_checks.REGISTRY for k in entry["thresholds"]]
    async with pool.acquire() as conn:
        try:
            policy = await policy_values.require(conn, await policy_values.today_utc(conn), *keys)
        except policy_values.PolicyUnavailable as exc:
            raise Unavailable("policy_unavailable", str(exc)) from None
        rows = await conn.fetch(LAST_ALERTS_SQL, ops_checks.OPS_ALERT_KIND)
    values = {k: policy.get(k) for k in keys}
    return registry_body(values, {r["check_key"]: r["last_at"] for r in rows}, datetime.now(UTC))
