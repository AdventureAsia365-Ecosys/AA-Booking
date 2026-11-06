"""The retired B2C half of Booking (AA-1046; AA-1034 R0 and AA-1037 before it; root ADR 0019 d8 "B2B-only model").

Adventure Asia sells to partner agencies (tenants) only and keeps no end-traveller record of its own. The B2C code
is gone: the customer routes and tables (customers, traveller sign-in, My Payments ``booking.customer.v1``), the
customer invoices and payments, the TripPlanner inquiry intake and its staff e-mail. Booking bk-045 (destructive)
drops their tables after this build is deployed; its money rows are archived in ``booking.retired_b2c_record``.

What stays is history: the ``b2c`` trip cases and every row about them (versions, supplier bookings, bills, payouts,
operations files, checks, status history, audit), kept under DM-7.1 and readable by staff. They are **read-only**:

- every staff write on a ``b2c`` case answers 410 ``b2b_only`` before any write of the service
  (:func:`refuse_b2c_case_write` and its siblings read only the case's ``channel``; a B2B case is never refused);
- a staff-made trip case (``POST /booking/trip-cases``, a new case from a trip plan) was always a B2C case for a
  customer: :func:`refuse_new_case` answers the same 410. B2B cases come only from the B2B confirm path
  (``POST /booking/b2b/v1/holds/{id}/confirm``);
- the B2C job kinds :data:`RETIRED_KINDS` have no handler; the worker cancels a leftover queued row
  (``Worker(disabled_kinds=...)`` in ``app.main``).

Supplier bills and payouts stay open whatever the case (AA's own payables). Retention keeps running on every row:
``bk_retention_inbox_redact`` (DM-6.6) and the DM-6.7 traveller erasure, a reviewed DB Migrate ``run-script`` (root
``docs/legal/dsar-erasure-runbook.md``).
"""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg

from app.errors import Gone

# The kinds of the B2C half (the TripPlanner inquiry intake and the staff e-mail of a new inquiry): no handler since
# AA-1046; Booking bk-045 refuses to apply while an intake job is queued or running.
RETIRED_KINDS: frozenset[str] = frozenset({"bk_inquiry_pull", "bk_inquiry_process", "bk_notify_email"})

CODE = "b2b_only"
MESSAGE = (
    "Adventure Asia works with partner agencies only: Booking no longer creates traveller records. "
    "Partner bookings arrive through the B2B API."
)

# The 410 every guarded route may answer, for the OpenAPI document (``responses=b2c.GONE_RESPONSE``).
GONE_RESPONSE: dict[int | str, dict[str, Any]] = {
    410: {"description": "b2b_only: the B2C part is retired (root ADR 0019 d8); b2c records are read-only"}
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
# Ops files that belonged to B2C money (customer payment evidence; ``booking.payments`` is retired).
B2C_FILE_ENTITIES = frozenset({"payment"})


def _gone() -> Gone:
    return Gone(CODE, MESSAGE)


def refuse_new_case() -> None:
    """Raise 410 ``b2b_only``: a staff-made trip case was always for a traveller customer (a B2C case)."""
    raise _gone()


async def case_channel(conn: asyncpg.Connection, ref: str) -> str | None:
    """The ``channel`` of a live trip case addressed by UUID or code (``TC-000123``), or None when there is none."""
    try:
        return await conn.fetchval(_CASE_BY_ID, str(uuid.UUID(ref)))
    except ValueError:
        return await conn.fetchval(_CASE_BY_CODE, ref.strip().upper())


async def refuse_b2c_case_write(conn: asyncpg.Connection, ref: str) -> None:
    """Raise 410 ``b2b_only`` for a write on a ``b2c`` trip case (history, read-only); B2B cases pass."""
    if await case_channel(conn, ref) == "b2c":
        raise _gone()


async def refuse_b2c_supplier_booking_write(conn: asyncpg.Connection, booking_id: str) -> None:
    """The same for a supplier booking, by the channel of its trip case."""
    if await conn.fetchval(_SUPPLIER_BOOKING_CASE, booking_id) == "b2c":
        raise _gone()


async def refuse_b2c_check_write(conn: asyncpg.Connection, check_id: str) -> None:
    """The same for a trip check, by the channel of its trip case."""
    if await conn.fetchval(_CHECK_CASE, check_id) == "b2c":
        raise _gone()


async def refuse_b2c_file_upload(conn: asyncpg.Connection, entity_type: str, entity_id: str) -> None:
    """An ops file is B2C when it is customer payment evidence, or when its supplier booking belongs to a ``b2c``
    case. Supplier bills and payouts are AA's own payables (supply side) and stay open."""
    if entity_type in B2C_FILE_ENTITIES:
        raise _gone()
    if entity_type == "supplier_booking":
        await refuse_b2c_supplier_booking_write(conn, entity_id)


async def refuse_b2c_file_remove(conn: asyncpg.Connection, file_id: str) -> None:
    """The same for removing an ops file, with the upload's rule: payment evidence, or a supplier booking's file on a
    ``b2c`` case. Supplier bill and payout files stay open whatever the case (AA's own payables)."""
    row: Any = await conn.fetchrow(_FILE_CASE, file_id)
    if row is None:
        return
    on_b2c_booking = row["entity_type"] == "supplier_booking" and row["channel"] == "b2c"
    if row["entity_type"] in B2C_FILE_ENTITIES or on_b2c_booking:
        raise _gone()
