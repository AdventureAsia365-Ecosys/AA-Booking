"""The frozen B2C part of Booking (AA-1034 R0 and AA-1037, root ADR 0019 d8 "B2B-only ecosystem model").

Adventure Asia sells to partner agencies (tenants) only and keeps no end-traveller record of its own. Until the B2C
tables are dropped (destructive-ops DRY RUN, later), the B2C part is switched off by ``B2C_INTAKE_ENABLED`` (default
off, :class:`app.settings.Settings`). One switch turns all of it back on:

- the TripPlanner intake kinds :data:`INTAKE_KINDS` are neither scheduled nor claimed, and their queued rows are
  cancelled (``Worker(disabled_kinds=...)`` in ``app.main``);
- every staff path that would create a customer answers 410 ``b2b_only`` (:func:`refuse_new_customer`) before any
  write, instead of the 42501 that bk-036 (REVOKE INSERT on ``booking.customer*`` from role ``booking``) would give;
- AA-1037 "no B2C write path is left": every staff write on the B2C records answers the same 410 before any read or
  write of the service. :func:`refuse_b2c_write` guards the routes that only exist for B2C (customer invoices and
  payments, ``booking.invoices`` being B2C-only; customer edit / archive; a staff-made case, always for a customer;
  sending or approving a proposal to a customer). :func:`refuse_b2c_case_write` and its siblings guard the routes
  shared with B2B cases: they read only the case's ``channel`` and refuse a ``b2c`` case (status moves, notes,
  assignment, proposal versions, supplier bookings, trip checks and their files). A B2B case is never refused here.

Reads stay (the GET routes, until the drop), and so does retention: the DM-7 jobs never go through these routes
(``app.retention``, the trip-check clearing of ``app.ops.lifecycle``), and the DM-6.7 traveller erasure runs as a
reviewed DB Migrate ``run-script`` (``docs/legal/dsar-erasure-runbook.md`` in the root repo), not as a staff route.
The jobs that would write on B2C rows skip them while the switch is off (``app.ops.lifecycle``).
"""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg
from fastapi import Request

from app.errors import Gone
from app.inquiry.intake import PROCESS_KIND, PULL_KIND

INTAKE_KINDS: frozenset[str] = frozenset({PULL_KIND, PROCESS_KIND})

CODE = "b2b_only"
MESSAGE = (
    "Adventure Asia works with partner agencies only: Booking no longer creates traveller records. "
    "Partner bookings arrive through the B2B API."
)

# The 410 every guarded route may answer, for the OpenAPI document (``responses=b2c.GONE_RESPONSE``).
GONE_RESPONSE: dict[int | str, dict[str, Any]] = {
    410: {
        "description": "b2b_only: the B2C part is switched off (B2C_INTAKE_ENABLED, root ADR 0019 d8); nothing was"
        " written"
    }
}

# The channel of the trip case a write would touch, by the case itself or by a record of the case. A ref that matches
# nothing reads NULL: the service then answers its own 404.
_CASE_BY_ID = "SELECT channel FROM booking.trip_cases WHERE id = $1::uuid AND deleted_at IS NULL"
_CASE_BY_CODE = "SELECT channel FROM booking.trip_cases WHERE code = $1 AND deleted_at IS NULL"
_SUPPLIER_BOOKING_CASE = (
    "SELECT tc.channel FROM booking.supplier_bookings sb JOIN booking.trip_cases tc ON tc.id = sb.trip_case_id"
    " WHERE sb.id = $1::uuid"
)
_CHECK_CASE = (
    "SELECT tc.channel FROM booking.trip_case_checks k JOIN booking.trip_cases tc ON tc.id = k.trip_case_id"
    " WHERE k.id = $1::uuid"
)
_FILE_CASE = (
    "SELECT f.entity_type, tc.channel FROM booking.ops_files f"
    " LEFT JOIN booking.trip_cases tc ON tc.id = f.trip_case_id WHERE f.id = $1::uuid"
)
# Ops files that belong to B2C money (``booking.payments`` is B2C-only, like ``booking.invoices``).
B2C_FILE_ENTITIES = frozenset({"payment"})


def intake_enabled(request: Request) -> bool:
    return bool(request.app.state.settings.b2c_intake_enabled)


def writes_open(request: Request) -> bool:
    """True when the B2C part is switched on (``B2C_INTAKE_ENABLED``): the same switch as the intake."""
    return intake_enabled(request)


def _gone() -> Gone:
    return Gone(CODE, MESSAGE)


def refuse_new_customer(request: Request) -> None:
    """Raise 410 ``b2b_only`` while the B2C intake is off (call it before the transaction that would insert)."""
    if not intake_enabled(request):
        raise _gone()


def refuse_b2c_write(request: Request) -> None:
    """Raise 410 ``b2b_only`` while the B2C part is off: the first call of a route that only writes B2C records."""
    if not writes_open(request):
        raise _gone()


async def case_channel(conn: asyncpg.Connection, ref: str) -> str | None:
    """The ``channel`` of a live trip case addressed by UUID or code (``TC-000123``), or None when there is none."""
    try:
        return await conn.fetchval(_CASE_BY_ID, str(uuid.UUID(ref)))
    except ValueError:
        return await conn.fetchval(_CASE_BY_CODE, ref.strip().upper())


async def refuse_b2c_case_write(request: Request, conn: asyncpg.Connection, ref: str) -> None:
    """Raise 410 ``b2b_only`` for a write on a ``b2c`` trip case while the B2C part is off (B2B cases pass)."""
    if writes_open(request):
        return
    if await case_channel(conn, ref) == "b2c":
        raise _gone()


async def refuse_b2c_supplier_booking_write(request: Request, conn: asyncpg.Connection, booking_id: str) -> None:
    """The same for a supplier booking, by the channel of its trip case."""
    if writes_open(request):
        return
    if await conn.fetchval(_SUPPLIER_BOOKING_CASE, booking_id) == "b2c":
        raise _gone()


async def refuse_b2c_check_write(request: Request, conn: asyncpg.Connection, check_id: str) -> None:
    """The same for a trip check, by the channel of its trip case."""
    if writes_open(request):
        return
    if await conn.fetchval(_CHECK_CASE, check_id) == "b2c":
        raise _gone()


async def refuse_b2c_file_upload(request: Request, conn: asyncpg.Connection, entity_type: str, entity_id: str) -> None:
    """An ops file is B2C when it is payment evidence, or when its supplier booking belongs to a ``b2c`` case.
    Supplier bills and payouts are AA's own payables (supply side) and stay open."""
    if writes_open(request):
        return
    if entity_type in B2C_FILE_ENTITIES:
        raise _gone()
    if entity_type == "supplier_booking":
        await refuse_b2c_supplier_booking_write(request, conn, entity_id)


async def refuse_b2c_file_remove(request: Request, conn: asyncpg.Connection, file_id: str) -> None:
    """The same for removing an ops file: by its entity type, or by the channel of the case it belongs to."""
    if writes_open(request):
        return
    row: Any = await conn.fetchrow(_FILE_CASE, file_id)
    if row is not None and (row["entity_type"] in B2C_FILE_ENTITIES or row["channel"] == "b2c"):
        raise _gone()


def frozen_view(value: Any) -> Any:
    """A billing read with every write affordance turned off: each ``actions`` map all false and ``invoiceable``
    false (reason ``b2b_only`` unless a case reason is already given). The staff admin draws its invoice and payment
    buttons from these flags, so the read pages stay as they are while no button offers a write the API would
    refuse."""
    if isinstance(value, list):
        return [frozen_view(v) for v in value]
    if not isinstance(value, dict):
        return value
    out = {k: frozen_view(v) for k, v in value.items()}
    if isinstance(out.get("actions"), dict):
        out["actions"] = dict.fromkeys(out["actions"], False)
    if "invoiceable" in out and (out["invoiceable"] or out.get("reason") is None):
        out["invoiceable"] = False
        out["reason"] = CODE
    return out
