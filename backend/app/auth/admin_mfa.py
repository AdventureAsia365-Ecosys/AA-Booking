"""Administrator MFA check of the Booking staff API (AA-1111 slice S2; root ADR 0006c; bk-061).

Tech lead decisions: S283 "admin MFA = TOTP" (an authenticator app code, never SMS), S285 "the WAF geo rule of the
staff hosts goes once admin MFA is enforced". The WORKFORCE pool allows TOTP as optional MFA; a staff member with TOTP
enrolled signs in with password + TOTP and Amazon Cognito writes the factors into the access token's ``amr`` claim
(``["pwd", "otp", "mfa"]``; an e-mail code sign-in gives ``["email_otp"]``). Cognito computes ``amr`` (a pre token
trigger cannot change it) and the verifier already checked the signature of this exact token string, so the claim is
read here from it (the verifier is a byte copy of AA-CIS-App's and its ``Principal`` carries no ``amr``).

Rule (:func:`check`, called by :func:`app.auth.staff.current_staff` for a WORKFORCE ``booking-admin`` session):

- MFA is satisfied when ``amr`` holds ``otp`` (:data:`MFA_AMR`);
- not satisfied and the pricing policy parameter ``staff_admin_mfa_required`` (Pricing > Policy, section "Staff
  sign-in", approved by the administrator group) is **true** -> 403 ``mfa_required``;
- not satisfied and the parameter is **false** (the bk-061 seed) -> served, logged ``staff_admin_mfa_check
  outcome=would_refuse`` (``satisfied`` when the code was used), one line per identity and outcome per
  :data:`LOG_EVERY_S`.

The caller leaves out every session the check must not see: the other roles, the capped Preview session and the
Dev-only E2E identities (AA-910 / AA-1087; they cannot exist outside Dev).

The parameter is read once per :data:`CACHE_S` per process (the staff dependency runs on every request). A parameter
with no approved version in force (bk-061 not applied) reads false, logged: that is today's behaviour, and refusing
would lock out the administrators who approve the parameter.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import asyncpg
import jwt
from fastapi import HTTPException

from app.pricing import policy_values

log = logging.getLogger(__name__)

PARAM = "staff_admin_mfa_required"
MFA_REQUIRED = "mfa_required"
# The Cognito amr value of a TOTP code from an authenticator app (the tech lead chose TOTP, S283).
MFA_AMR = "otp"
CACHE_S = 30.0  # technical-constant: how long one read of the switch is reused, not a policy value
LOG_EVERY_S = 600.0  # technical-constant: log throttle of the check line, not a policy value
LOG_TABLE_SIZE = 1024  # technical-constant: size of the throttle table

_switch: dict[str, Any] = {"at": None, "value": False}
_logged: dict[tuple[str, str, str], float] = {}


def reset() -> None:
    """Forget the cached switch and the log throttle (tests)."""
    _switch.update(at=None, value=False)
    _logged.clear()


def token_amr(token: Any) -> tuple[str, ...]:
    """The ``amr`` values of an already verified access token (empty when absent or malformed)."""
    if not isinstance(token, str) or not token:
        return ()
    try:
        claims = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError:
        return ()
    value = claims.get("amr") if isinstance(claims, dict) else None
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list | tuple):
        return ()
    return tuple(v for v in value if isinstance(v, str))


def mfa_used(amr: tuple[str, ...]) -> bool:
    return MFA_AMR in amr


async def required(pool: asyncpg.Pool) -> bool:
    """The switch in force today (cached :data:`CACHE_S`); no approved version -> false, logged."""
    now = time.monotonic()
    if _switch["at"] is not None and now - _switch["at"] < CACHE_S:
        return bool(_switch["value"])
    async with pool.acquire() as conn:
        policy = await policy_values.load_today(conn)
    try:
        value = policy.flag(PARAM)
    except policy_values.PolicyUnavailable:
        log.warning("staff_admin_mfa_switch_unavailable param=%s (read as off)", PARAM)
        value = False
    _switch.update(at=now, value=value)
    return value


def _log_once(iss: str, sub: str, outcome: str, level: int, amr: tuple[str, ...]) -> None:
    now = time.monotonic()
    key = (iss, sub, outcome)
    last = _logged.get(key)
    if last is not None and now - last < LOG_EVERY_S:
        return
    if len(_logged) >= LOG_TABLE_SIZE:
        _logged.clear()
    _logged[key] = now
    log.log(level, "staff_admin_mfa_check outcome=%s sub=%s role=admin amr=%s", outcome, sub, list(amr))


async def check(pool: asyncpg.Pool, iss: str, sub: str, token: str) -> None:
    """Refuse an administrator session without an authenticator code while the switch is on (403 mfa_required)."""
    amr = token_amr(token)
    if mfa_used(amr):
        _log_once(iss, sub, "satisfied", logging.INFO, amr)
        return
    if not await required(pool):
        _log_once(iss, sub, "would_refuse", logging.INFO, amr)
        return
    _log_once(iss, sub, "refused", logging.WARNING, amr)
    raise HTTPException(status_code=403, detail=MFA_REQUIRED)
