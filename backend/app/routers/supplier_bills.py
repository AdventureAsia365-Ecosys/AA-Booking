"""Staff supplier bills and payouts (bk-030; AA-682 slice S4, ``app.ops.supplier_bills``).

Read ``bill.read`` (admin, sales, contracting); enter / edit / cancel ``bill.write`` and approve ``bill.approve``
(admin, contracting; never the person who entered the bill nor the one who last changed its amounts or lines: 403
``maker_checker``); record / void a payout ``payout.write`` (admin; never on a bill they entered or last changed).
Staff tokens only (never M2M); preview sessions do not get bills at all and the E2E session stays read-only (global
rule in ``app.auth.staff``). Entering a bill and recording a
payout need an ``Idempotency-Key`` header (8-200 characters): the same key with the same body returns the stored row
(200, ``replayed``), another body is 409 ``idempotency_conflict``. No payment provider: payouts are recorded by hand.

- ``GET /supplier-bills`` (``status`` received / approved / paid / cancelled / open / all, ``supplier_id``, ``case``,
  ``overdue``, ``due_before``, ``q``) · ``GET /supplier-bills/summary`` (KPIs) ·
  ``GET /supplier-bills/billable?supplier_id=&case=&q=`` (confirmed bookings a bill can name).
- ``POST /supplier-bills`` · ``GET|PATCH /supplier-bills/{id}`` · ``POST /supplier-bills/{id}/approve | cancel``.
- ``POST /supplier-bills/{id}/payouts`` · ``POST /supplier-payouts/{id}/void``.
- ``GET /trip-cases/{ref}/supplier-costs``: the case's cost vs billed panel (``bill.read`` + ``pricing.read``).

Documents: ``POST /ops-files`` with ``kind = supplier_bill`` (entity the bill) or ``payout_evidence`` (entity the
payout) (``routers.supplier_bookings``).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, Header, Path, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from app.auth.staff import Staff, get_pool, require
from app.ops import supplier_bills as bills

router = APIRouter(prefix="/booking", tags=["supplier-bills"])

Pool = Annotated[asyncpg.Pool, Depends(get_pool)]
CanRead = Annotated[Staff, Depends(require("bill.read"))]
CanWrite = Annotated[Staff, Depends(require("bill.write"))]
CanApprove = Annotated[Staff, Depends(require("bill.approve"))]
CanPay = Annotated[Staff, Depends(require("payout.write"))]
BillRef = Annotated[str, Path(min_length=1, max_length=64, description="Supplier bill UUID or code (BIL-000123)")]
PayoutRef = Annotated[str, Path(min_length=1, max_length=64, description="Payout UUID or code (SPO-000123)")]
CaseRef = Annotated[str, Path(min_length=1, max_length=64, description="Trip case UUID or code (TC-000123)")]
IdempotencyKey = Annotated[
    str | None, Header(alias="Idempotency-Key", max_length=200, description="8-200 characters, one per request")
]
Amount = Annotated[Decimal, Field(gt=0, max_digits=16, decimal_places=2)]
Reason = Annotated[str, Field(min_length=1, max_length=500)]
Currency = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]


class LineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    supplier_booking_id: UUID
    amount: Amount


class NewBill(BaseModel):
    model_config = ConfigDict(extra="forbid")

    supplier_id: UUID
    trip_case: str | None = Field(default=None, max_length=64, description="trip case code or UUID, when one")
    bill_number: str = Field(min_length=1, max_length=100, description="the supplier's own bill number")
    currency: Currency
    amount: Amount
    issue_date: date
    due_date: date
    notes: str | None = Field(default=None, max_length=2000)
    lines: list[LineIn] = Field(default_factory=list, max_length=bills.MAX_LINES)


class BillChanges(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bill_number: str | None = Field(default=None, min_length=1, max_length=100)
    currency: Currency | None = None
    amount: Amount | None = None
    issue_date: date | None = None
    due_date: date | None = None
    notes: str | None = Field(default=None, max_length=2000)
    lines: list[LineIn] | None = Field(default=None, max_length=bills.MAX_LINES, description="the whole new set")


class ReasonIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Reason


class NewPayout(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: Amount
    method: Literal["bank_transfer", "card_offline", "cash", "other"]
    paid_on: date
    reference: str | None = Field(default=None, max_length=100, description="bank or receipt reference, never a card")
    note: str | None = Field(default=None, max_length=2000)
    allow_duplicate_reference: bool = False


def _body(model: BaseModel, **kw: Any) -> dict[str, Any]:
    data = model.model_dump(**kw)
    if "supplier_id" in data and data["supplier_id"] is not None:
        data["supplier_id"] = str(data["supplier_id"])
    if data.get("lines") is not None:
        data["lines"] = [{**x, "supplier_booking_id": str(x["supplier_booking_id"])} for x in data["lines"]]
    return data


@router.get("/supplier-bills")
async def list_bills(
    staff: CanRead,
    pool: Pool,
    status: Annotated[Literal["received", "approved", "paid", "cancelled", "open", "all"] | None, Query()] = None,
    supplier_id: Annotated[UUID | None, Query()] = None,
    case: Annotated[str | None, Query(max_length=64, description="trip case code or UUID")] = None,
    overdue: Annotated[bool | None, Query()] = None,
    due_before: Annotated[date | None, Query()] = None,
    q: Annotated[
        str | None, Query(max_length=100, description="bill code, supplier bill number, supplier, case")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=bills.MAX_PAGE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await bills.list_bills(
            conn,
            staff,
            status=status,
            supplier=str(supplier_id) if supplier_id else None,
            case=case,
            overdue=overdue,
            due_before=due_before,
            q=q,
            limit=limit,
            offset=offset,
        )


@router.get("/supplier-bills/summary")
async def bills_summary(_: CanRead, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await bills.summary(conn)


@router.get("/supplier-bills/billable")
async def billable(
    staff: CanRead,
    pool: Pool,
    supplier_id: Annotated[UUID, Query()],
    case: Annotated[str | None, Query(max_length=64)] = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await bills.billable(conn, staff, supplier=str(supplier_id), case=case, q=q)


@router.post("/supplier-bills", status_code=201)
async def create_bill(
    body: NewBill, staff: CanWrite, pool: Pool, response: Response, key: IdempotencyKey = None
) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        out, replayed = await bills.create(conn, staff, _body(body), key)
    if replayed:
        response.status_code = 200
    return {**out, "replayed": replayed}


@router.get("/supplier-bills/{bill}")
async def get_bill(bill: BillRef, staff: CanRead, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await bills.detail(conn, staff, bill)


@router.patch("/supplier-bills/{bill}")
async def patch_bill(body: BillChanges, bill: BillRef, staff: CanWrite, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await bills.update(conn, staff, bill, _body(body, exclude_unset=True))


@router.post("/supplier-bills/{bill}/approve")
async def approve_bill(bill: BillRef, staff: CanApprove, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await bills.approve(conn, staff, bill)


@router.post("/supplier-bills/{bill}/cancel")
async def cancel_bill(body: ReasonIn, bill: BillRef, staff: CanWrite, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await bills.cancel(conn, staff, bill, body.reason)


@router.post("/supplier-bills/{bill}/payouts", status_code=201)
async def record_payout(
    body: NewPayout, bill: BillRef, staff: CanPay, pool: Pool, response: Response, key: IdempotencyKey = None
) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        out, replayed = await bills.record_payout(conn, staff, bill, _body(body), key)
    if replayed:
        response.status_code = 200
    return {**out, "replayed": replayed}


@router.post("/supplier-payouts/{payout}/void")
async def void_payout(body: ReasonIn, payout: PayoutRef, staff: CanPay, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await bills.void_payout(conn, staff, payout, body.reason)


@router.get("/trip-cases/{ref}/supplier-costs")
async def case_costs(ref: CaseRef, staff: CanRead, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await bills.case_costs(conn, staff, ref)
