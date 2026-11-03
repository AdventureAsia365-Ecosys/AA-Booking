"""The frozen B2C part of Booking (AA-1034 R0, root ADR 0019 d8 "B2B-only ecosystem model").

Adventure Asia sells to partner agencies (tenants) only and keeps no end-traveller record of its own. Until the B2C
tables are dropped (destructive-ops DRY RUN, later), the B2C intake is switched off by ``B2C_INTAKE_ENABLED`` (default
off, :class:`app.settings.Settings`):

- the TripPlanner intake kinds :data:`INTAKE_KINDS` are neither scheduled nor claimed, and their queued rows are
  cancelled (``Worker(disabled_kinds=...)`` in ``app.main``);
- every staff path that would create a customer answers 410 ``b2b_only`` (:func:`refuse_new_customer`) before any
  write, instead of the 42501 that bk-036 (REVOKE INSERT on ``booking.customer*`` from role ``booking``) would give.

Reading, editing (redacting) and archiving the existing customers stays, and so do cases for an existing customer:
they insert no traveller record.
"""

from __future__ import annotations

from fastapi import Request

from app.errors import Gone
from app.inquiry.intake import PROCESS_KIND, PULL_KIND

INTAKE_KINDS: frozenset[str] = frozenset({PULL_KIND, PROCESS_KIND})

CODE = "b2b_only"
MESSAGE = (
    "Adventure Asia works with partner agencies only: Booking no longer creates traveller records. "
    "Partner bookings arrive through the B2B API."
)


def intake_enabled(request: Request) -> bool:
    return bool(request.app.state.settings.b2c_intake_enabled)


def refuse_new_customer(request: Request) -> None:
    """Raise 410 ``b2b_only`` while the B2C intake is off (call it before the transaction that would insert)."""
    if not intake_enabled(request):
        raise Gone(CODE, MESSAGE)
