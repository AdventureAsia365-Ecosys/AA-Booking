"""The pilot tenant in Booking (AA-969 part 2, root ADR 0019 d16).

ADR 0019 d16 lets one real agency (the pilot, a CIS tenant marked ``shared.tenants.is_pilot``, CIS 354) use Dev for at
most 8 weeks, and requires every clean-up job to skip it. Booking reads the marker from the CIS contract view
``catalog.v_tenant_account_v1`` (CIS 357, column ``is_pilot``; ``booking`` holds SELECT on it), feature-detected like
every catalog view (:mod:`app.catalog.liveness`). The ``booking.job`` kinds that delete or redact data on a schedule
leave the pilot tenant's rows alone:

- ``bk_case_lifecycle``'s clearing of trip-check emergency contacts and notes (a case whose ``party_tenant_id`` is the
  pilot), :mod:`app.ops.lifecycle`;
- ``bk_retention_inbox_redact`` (an inbox row whose trip case belongs to the pilot), :mod:`app.retention.jobs`.

The other kinds are not clean-up of tenant data: ``bk_retention_job`` deletes finished ``booking.job`` rows (no tenant
data), ``bk_b2b_hold_expire`` applies the hold's own expiry, ``bk_tenant_pull`` / the price and plan jobs project CIS
state, the reminders only notify, the case moves follow the travel dates.

Fail-open when the view cannot be read (a missing grant or CIS 357 not applied): the job runs as before and logs
``pilot_view_unreadable``. Both clean-ups only reach rows 90 days past a trip end or an inquiry, longer than the pilot
(at most 8 weeks), so data written during the pilot is never in reach even then.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import asyncpg

from app.catalog import liveness

log = logging.getLogger(__name__)

VIEW = liveness.View("catalog", "v_tenant_account_v1", ("tenant_id", "is_pilot"))
PILOT_SQL = f"SELECT tenant_id FROM {VIEW.qualified} WHERE is_pilot ORDER BY tenant_id"  # noqa: S608 — fixed name


async def pilot_tenants(conn: asyncpg.Connection | asyncpg.Pool) -> list[uuid.UUID]:
    """The pilot tenant ids (at most one: CIS ``tenants_one_pilot_key``); empty when there is none or the view is not
    readable."""
    if not await liveness.view_ready(conn, VIEW):
        log.warning("pilot_view_unreadable view=%s", VIEW.qualified)
        return []
    return [uuid.UUID(str(r["tenant_id"])) for r in await conn.fetch(PILOT_SQL)]


def uuid_array_literal(ids: list[Any]) -> str:
    """A SQL ``uuid[]`` literal of validated ids (each one parsed by :class:`uuid.UUID`, so no other text gets in)."""
    return "'{" + ",".join(str(uuid.UUID(str(i))) for i in ids) + "}'::uuid[]"
