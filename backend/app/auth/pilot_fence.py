"""The pilot fence of the Booking staff API (AA-1054, S297; tech lead decision S295 sec-Q11; root ADR 0006a).

Root ADR 0006a keeps Dev-only E2E staff identities (A10a, A10e, A10e-2, A10g, A10h here; every address of the E2E
mailbox domain, :data:`E2E_MAIL_DOMAIN`) so the agent's E2E suites can sign in without a person. ADR 0019 d16 lets one
real agency (the pilot) work on Dev. On the pilot day the tech lead turns on ONE switch for CIS and Booking, the CIS
platform setting ``cis.platform.pilot_e2e_fence`` (CIS 495; Platform › Settings, maker-checker, audited by the CIS
settings registry). Booking does not hold its own copy and has no grant on the CIS settings tables: it reads the value
in force through the read-only contract view ``catalog.v_pilot_fence_v1`` (CIS 496, SELECT to role ``booking`` only,
like every other catalog view Booking reads), so no app writes another app's data.

Rule (:func:`check`, called by :func:`app.auth.staff.current_staff` for every WORKFORCE token right before its first
database read; the checks before it need no database and only refuse more: the Preview cap, the read-only E2E admin's
writes, the E2E owner's and the E2E writers' environment and group scope):

- an address that is not of the E2E mailbox domain (real staff) -> nothing is read, nothing changes;
- an E2E address and the view says ``fence_on`` false -> served under its own Dev-only rules, as before;
- an E2E address and ``fence_on`` true -> 403 :data:`E2E_PILOT_FENCE`;
- an E2E address and the view cannot be read (CIS 496 not applied, no grant, no row, a database error) -> 403
  :data:`E2E_PILOT_FENCE_UNKNOWN` (fail closed: only test accounts are refused, logged ``pilot_fence_unreadable``).

The answer is read once per :data:`CACHE_S` per process and only for an E2E identity. The E2E Cognito users are
disabled on the same day by AA-CIS-Infra ``tools/identity/e2e_identity_fence.py`` (root runbook
docs/ops/runbooks/pilot-agency-onboarding.md, "Pilot day"); this fence also holds whatever that step misses.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import asyncpg
from fastapi import HTTPException

log = logging.getLogger(__name__)

# The domain of every Dev-only E2E address (AA-CIS-Infra e2e_mailbox.tf; AA-CIS-App api/core/e2e_staff.E2E_MAIL_DOMAIN).
E2E_MAIL_DOMAIN = "e2e-mail.lumiguides.it.com"
VIEW = "catalog.v_pilot_fence_v1"
FENCE_SQL = f"SELECT fence_on FROM {VIEW}"  # noqa: S608 — fixed name
E2E_PILOT_FENCE = "e2e_pilot_fence"
E2E_PILOT_FENCE_UNKNOWN = "e2e_pilot_fence_unknown"
OFF, ON, UNKNOWN = "off", "on", "unknown"
CACHE_S = 30.0  # technical-constant: how long one read of the switch is reused, not a policy value

_cache: dict[str, Any] = {"at": None, "state": UNKNOWN}


def reset() -> None:
    """Forget the cached answer (tests)."""
    _cache.update(at=None, state=UNKNOWN)


def is_e2e_identity(email: Any) -> bool:
    """True when ``email`` is an address of the E2E mailbox domain (after trimming and lower-casing, any local part: a
    new or a look-alike E2E address is fenced too)."""
    if not isinstance(email, str):
        return False
    local, sep, domain = email.strip().lower().rpartition("@")
    return bool(sep) and domain == E2E_MAIL_DOMAIN


async def read_state(pool: asyncpg.Pool | asyncpg.Connection) -> str:
    """:data:`ON` / :data:`OFF` from the view, :data:`UNKNOWN` when it cannot be read or answers no boolean."""
    try:
        value = await pool.fetchval(FENCE_SQL)
    except (asyncpg.PostgresError, OSError) as exc:
        log.warning("pilot_fence_unreadable view=%s error_class=%s", VIEW, type(exc).__name__)
        return UNKNOWN
    if value is True:
        return ON
    if value is False:
        return OFF
    log.warning("pilot_fence_unreadable view=%s value=%s", VIEW, "no_row" if value is None else "not_boolean")
    return UNKNOWN


async def state(pool: asyncpg.Pool | asyncpg.Connection) -> str:
    """The switch in force, cached :data:`CACHE_S`."""
    now = time.monotonic()
    if _cache["at"] is not None and now - _cache["at"] < CACHE_S:
        return str(_cache["state"])
    value = await read_state(pool)
    _cache.update(at=now, state=value)
    return value


async def check(pool: asyncpg.Pool | asyncpg.Connection, email: Any, *, sub: str | None = None) -> None:
    """Refuse an E2E identity while the fence is on (403 e2e_pilot_fence) or unreadable (e2e_pilot_fence_unknown)."""
    if not is_e2e_identity(email):
        return
    current = await state(pool)
    if current == OFF:
        return
    detail = E2E_PILOT_FENCE if current == ON else E2E_PILOT_FENCE_UNKNOWN
    log.warning("booking_staff_refused reason=%s sub=%s", detail, sub)
    raise HTTPException(status_code=403, detail=detail)
