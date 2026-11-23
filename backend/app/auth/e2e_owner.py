"""The Dev-only E2E owner (AA-1176, S288, root ADR 0006a A10h).

The WORKFORCE address :data:`E2E_OWNER_EMAIL` (groups exactly ``cis-admin`` + ``booking-admin``) is an administrator
"like the tech lead" for the orchestrating agent's live checks. Unlike the A10e / A10g identities of
:mod:`app.auth.e2e_staff` it is NOT an E2E writer and NOT read-only: its session is a plain ``admin`` session, and the
owner marker, when CIS holds it, comes from ``shared.v_staff_booking_grant`` like any owner's. It is held only to
(:func:`check_session`, called by ``app.auth.staff.current_staff`` before any DB access):

- **Dev only.** The process ``ENVIRONMENT`` must be exactly ``"dev"`` (403 ``e2e_not_allowed_here`` anywhere else, reads
  included). The WORKFORCE pre token trigger issues its token only where ``E2E_DEV_ONLY_STAFF_ENABLED`` is ``"true"``
  (AA-CIS-Infra Terraform: dev only, with a precondition).
- **Exact scope.** The exact ASCII address and exactly :data:`E2E_OWNER_GROUPS` (403 ``e2e_scope``; a value that only
  lower-cases to the address is refused the same way).

It is exempt from the administrator MFA switch (its e-mail code is readable only by the MFA admin role, AA-CIS-Infra
e2e_mailbox.tf prefix inbound-admin/), never self-approves (``app.auth.self_approval``) and is named
:data:`E2E_OWNER_NAME` on its first-sign-in staff row and audit row.

TEMPORARY: removed with the A10 family at go-live (AA-1054): drop this module and its calls.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from fastapi import HTTPException

from app.auth.e2e_staff import E2E_NOT_ALLOWED_HERE, E2E_SCOPE, dev_only_environment

log = logging.getLogger(__name__)

# Same constant as AA-CIS-Infra workforce.E2E_OWNER_TEST_STAFF_EMAIL, AA-CIS-App e2e_staff.E2E_OWNER_TEST_STAFF_EMAIL
# and the CIS 473 admin_users_email_chk branch.
E2E_OWNER_EMAIL = "e2e-owner@e2e-mail.lumiguides.it.com"
E2E_OWNER_GROUPS: frozenset[str] = frozenset({"cis-admin", "booking-admin"})
E2E_OWNER_NAME = "E2E owner (agent, Dev only)"


def is_e2e_owner(email: Any) -> bool:
    """The exact address (ASCII raw value, then trim + lower-case)."""
    return isinstance(email, str) and email.isascii() and email.strip().lower() == E2E_OWNER_EMAIL


def is_e2e_owner_like(email: Any) -> bool:
    """Any value that trims and lower-cases to the address (a non-ASCII look-alike included)."""
    return isinstance(email, str) and email.strip().lower() == E2E_OWNER_EMAIL


def check_session(email: Any, groups: Iterable[str]) -> bool:
    """False for every principal that is not (like) the E2E owner. For the E2E owner: 403 ``e2e_not_allowed_here``
    outside ``ENVIRONMENT`` exactly ``"dev"``, 403 ``e2e_scope`` for a look-alike or a group set other than exactly
    :data:`E2E_OWNER_GROUPS`; else True (a plain administrator session)."""
    if not is_e2e_owner_like(email):
        return False
    if not dev_only_environment():
        log.warning("booking_e2e_owner_refused reason=environment")
        raise HTTPException(status_code=403, detail=E2E_NOT_ALLOWED_HERE)
    if not is_e2e_owner(email) or frozenset(groups or ()) != E2E_OWNER_GROUPS:
        log.warning("booking_e2e_owner_refused reason=scope")
        raise HTTPException(status_code=403, detail=E2E_SCOPE)
    return True
