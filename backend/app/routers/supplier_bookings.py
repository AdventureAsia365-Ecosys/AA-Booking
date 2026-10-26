"""Staff supplier bookings and operations documents (bk-026; AA-682 slice S1, ``app.ops``).

Read ``ops.read``, write ``ops.write`` (admin, sales, contracting); confirming or declining a booking of an on-request
B2B line also needs ``b2b_desk.write``. Staff tokens only (never M2M); preview and E2E sessions stay read-only by the
global rule in ``app.auth.staff``.

- ``GET /trip-cases/{ref}/supplier-bookings``: the case's bookings, supply state and the items still to book.
- ``POST /trip-cases/{ref}/supplier-bookings/generate``: one pending booking per approved supply item (idempotent).
- ``POST /trip-cases/{ref}/supplier-bookings``: add one by hand (extra item, or the replacement of a declined one).
- ``GET /supplier-bookings`` (``supplier_id``, ``status``, ``from``, ``to``): the supplier Bookings tab, the ops queue.
- ``GET /supplier-bookings/summary``: the dashboard tile "Supplier answers pending".
- ``GET|PATCH /supplier-bookings/{id}``; ``POST …/request | confirm | decline | cancel | rerequest``.
- ``POST /ops-files`` (base64 JSON), ``GET /ops-files/{id}/download`` (presigned 300 s, audited),
  ``POST /ops-files/{id}/remove``.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.auth.staff import Staff, get_pool, require
from app.ops import files as ops_files
from app.ops import supplier_bookings as service

router = APIRouter(prefix="/booking", tags=["operations"])

Pool = Annotated[asyncpg.Pool, Depends(get_pool)]
CanRead = Annotated[Staff, Depends(require("ops.read"))]
CanWrite = Annotated[Staff, Depends(require("ops.write"))]
CaseRef = Annotated[str, Path(min_length=1, max_length=64, description="Trip case UUID or code (TC-000123)")]
BookingId = Annotated[UUID, Path(description="supplier booking UUID")]
FileId = Annotated[UUID, Path(description="operations file UUID")]
Money = Annotated[Decimal, Field(ge=0, max_digits=16, decimal_places=2)]
Currency = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
Reason = Annotated[str, Field(min_length=1, max_length=500)]


def _store(request: Request) -> ops_files.OpsStore | None:
    return request.app.state.ops_store


Store = Annotated[ops_files.OpsStore | None, Depends(_store)]


class BookingFacts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    supplier_id: UUID | None = None
    hotel_room_type_id: UUID | None = None
    service_id: UUID | None = None
    item_description: str | None = Field(default=None, max_length=500)
    service_date: date | None = None
    nights: int | None = Field(default=None, ge=0, le=60)
    pax: int | None = Field(default=None, ge=1, le=120)
    net_amount: Money | None = None
    net_currency: Currency | None = None
    notes: str | None = Field(default=None, max_length=2000)


class NewBooking(BookingFacts):
    supplier_id: UUID
    version_item_id: UUID | None = None


class ConfirmIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmation_ref: str = Field(min_length=1, max_length=100)
    voucher_file_id: UUID | None = None


class DeclineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decline_code: Literal["sold_out", "supplier_unavailable", "minimum_not_met", "other"]


class ReasonIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Reason


class NewFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["payment_evidence", "supplier_voucher", "supplier_confirmation", "supplier_bill", "payout_evidence"]
    entity_type: Literal["payment", "supplier_booking", "supplier_bill", "supplier_payout"]
    entity_id: UUID
    file_name: str = Field(min_length=1, max_length=200)
    content_type: Literal["application/pdf", "image/jpeg", "image/png", "image/webp"]
    data_base64: str = Field(min_length=1, max_length=ops_files.MAX_BASE64_CHARS + 16)


def _ids(data: dict[str, Any]) -> dict[str, Any]:
    return {k: (str(v) if isinstance(v, UUID) else v) for k, v in data.items()}


@router.get("/trip-cases/{ref}/supplier-bookings")
async def case_bookings(ref: CaseRef, staff: CanRead, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await service.list_for_case(conn, staff, ref)


@router.post("/trip-cases/{ref}/supplier-bookings/generate")
async def generate(ref: CaseRef, staff: CanWrite, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await service.generate(conn, staff, ref)


@router.post("/trip-cases/{ref}/supplier-bookings", status_code=201)
async def create(body: NewBooking, ref: CaseRef, staff: CanWrite, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await service.create(conn, staff, ref, _ids(body.model_dump()))


@router.get("/supplier-bookings")
async def list_bookings(
    staff: CanRead,
    pool: Pool,
    supplier_id: Annotated[str | None, Query(max_length=64, description="supplier UUID or code")] = None,
    status: Annotated[
        Literal["pending", "requested", "confirmed", "declined", "cancelled", "live"] | None, Query()
    ] = None,
    start: Annotated[date | None, Query(alias="from")] = None,
    end: Annotated[date | None, Query(alias="to")] = None,
    limit: Annotated[int, Query(ge=1, le=service.MAX_PAGE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await service.list_bookings(
            conn, staff, supplier_id=supplier_id, status=status, start=start, end=end, limit=limit, offset=offset
        )


@router.get("/supplier-bookings/summary")
async def bookings_summary(_: CanRead, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await service.summary(conn)


@router.get("/supplier-bookings/{booking_id}")
async def get_booking(booking_id: BookingId, staff: CanRead, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await service.get_booking(conn, staff, str(booking_id))


@router.patch("/supplier-bookings/{booking_id}")
async def patch_booking(body: BookingFacts, booking_id: BookingId, staff: CanWrite, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await service.update(conn, staff, str(booking_id), _ids(body.model_dump(exclude_unset=True)))


@router.post("/supplier-bookings/{booking_id}/request")
async def request_booking(booking_id: BookingId, staff: CanWrite, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await service.request(conn, staff, str(booking_id))


@router.post("/supplier-bookings/{booking_id}/confirm")
async def confirm_booking(body: ConfirmIn, booking_id: BookingId, staff: CanWrite, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await service.confirm(
            conn,
            staff,
            str(booking_id),
            confirmation_ref=body.confirmation_ref,
            voucher_file_id=str(body.voucher_file_id) if body.voucher_file_id else None,
        )


@router.post("/supplier-bookings/{booking_id}/decline")
async def decline_booking(body: DeclineIn, booking_id: BookingId, staff: CanWrite, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await service.decline(conn, staff, str(booking_id), decline_code=body.decline_code)


@router.post("/supplier-bookings/{booking_id}/cancel")
async def cancel_booking(body: ReasonIn, booking_id: BookingId, staff: CanWrite, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await service.cancel(conn, staff, str(booking_id), reason=body.reason)


@router.post("/supplier-bookings/{booking_id}/rerequest")
async def rerequest_booking(body: ReasonIn, booking_id: BookingId, staff: CanWrite, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await service.rerequest(conn, staff, str(booking_id), reason=body.reason)


# ── Operations documents ─────────────────────────────────────────────────────


@router.post("/ops-files", status_code=201)
async def upload_file(body: NewFile, staff: CanWrite, pool: Pool, store: Store) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await ops_files.upload(
            conn,
            store,
            staff,
            kind=body.kind,
            entity_type=body.entity_type,
            entity_id=str(body.entity_id),
            file_name=body.file_name,
            content_type=body.content_type,
            data_base64=body.data_base64,
        )


@router.get("/ops-files/{file_id}/download")
async def download_file(file_id: FileId, staff: CanRead, pool: Pool, store: Store) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await ops_files.download(conn, store, staff, str(file_id))


@router.post("/ops-files/{file_id}/remove")
async def remove_file(body: ReasonIn, file_id: FileId, staff: CanWrite, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await ops_files.remove(conn, staff, str(file_id), body.reason)
