"""Staff billing: customer invoices, the payment schedule and manual payments (bk-028; AA-682 slice S3,
``app.ops.invoices`` / ``app.ops.payments``).

Read ``billing.read``, write ``billing.write`` (admin, sales), verify ``payment.verify`` (admin, never the person who
recorded the payment: 403 ``maker_checker``). Staff tokens only (never M2M); preview sessions do not get billing at all
and the E2E session stays read-only (global rule in ``app.auth.staff``). Creating an invoice and recording a payment
need an ``Idempotency-Key`` header (8-200 characters): the same key with the same body returns the stored row (200),
another body is 409 ``idempotency_conflict``. No payment provider: money is marked by hand (design D6).

- ``GET /invoices`` (``status``, ``overdue``, ``case``, ``q``, ``from``, ``to``) · ``GET /invoices/{number}``
- ``GET /trip-cases/{ref}/billing``: the case Billing tab (can it be invoiced, defaults, live invoice, history).
- ``POST /trip-cases/{ref}/invoices``: draft from the approved version (+ adjustments, schedule; ``send_now``).
- ``PATCH /invoices/{number}`` (draft: adjustments, schedule, notes, terms; sent: notes) · ``DELETE`` (draft, soft).
- ``POST /invoices/{number}/send | cancel | amend | remind``.
- ``POST /invoices/{number}/payments``: record a receipt or a refund.
- ``GET /payments`` (``state``, ``kind``, ``method``, ``invoice``, ``case``, ``q``, ``from``, ``to``) ·
  ``GET|PATCH /payments/{ref}`` · ``POST /payments/{ref}/verify | void``.
- ``GET /billing/summary``: the /billing KPIs.

Evidence files: ``POST /ops-files`` with ``kind = payment_evidence``, ``entity_type = payment``
(``routers.supplier_bookings``).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Literal

import asyncpg
from fastapi import APIRouter, Depends, Header, Path, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from app.auth.staff import Staff, get_pool, require
from app.ops import invoices, payments

router = APIRouter(prefix="/booking", tags=["billing"])

Pool = Annotated[asyncpg.Pool, Depends(get_pool)]
CanRead = Annotated[Staff, Depends(require("billing.read"))]
CanWrite = Annotated[Staff, Depends(require("billing.write"))]
CanVerify = Annotated[Staff, Depends(require("payment.verify"))]
CaseRef = Annotated[str, Path(min_length=1, max_length=64, description="Trip case UUID or code (TC-000123)")]
Number = Annotated[str, Path(min_length=1, max_length=64, description="Invoice number (INV-000123) or UUID")]
Ref = Annotated[str, Path(min_length=1, max_length=64, description="Payment ref (PMT-000123) or UUID")]
IdempotencyKey = Annotated[
    str | None, Header(alias="Idempotency-Key", max_length=200, description="8-200 characters, one per request")
]
Amount = Annotated[Decimal, Field(gt=0, max_digits=16, decimal_places=2)]
SignedAmount = Annotated[Decimal, Field(max_digits=16, decimal_places=2)]
Reason = Annotated[str, Field(min_length=1, max_length=500)]


class AdjustmentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1, max_length=500)
    quantity: Annotated[Decimal, Field(gt=0, le=9999, max_digits=6, decimal_places=2)] = Decimal(1)
    unit_price: SignedAmount
    reason: Reason


class ScheduleRowIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["deposit", "second", "final", "other"]
    amount: Amount
    due_date: date


class NewInvoice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    adjustments: list[AdjustmentIn] = Field(default_factory=list, max_length=invoices.MAX_ADJUSTMENTS)
    schedule: list[ScheduleRowIn] | None = Field(default=None, min_length=1, max_length=10)
    notes: str | None = Field(default=None, max_length=2000)
    terms: str | None = Field(default=None, max_length=2000)
    send_now: bool = False


class InvoiceChanges(BaseModel):
    model_config = ConfigDict(extra="forbid")

    adjustments: list[AdjustmentIn] | None = Field(default=None, max_length=invoices.MAX_ADJUSTMENTS)
    schedule: list[ScheduleRowIn] | None = Field(default=None, min_length=1, max_length=10)
    notes: str | None = Field(default=None, max_length=2000)
    terms: str | None = Field(default=None, max_length=2000)


class Amendment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    adjustments: list[AdjustmentIn] = Field(min_length=1, max_length=10)
    schedule: list[ScheduleRowIn] | None = Field(
        default=None, max_length=10, description="the new rows after the covered ones; omit to move the difference"
    )


class ReasonIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Reason


class NewPayment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["receipt", "refund"] = "receipt"
    amount: Amount
    method: Literal["bank_transfer", "card_offline", "cash", "other"]
    received_on: date
    reference: str | None = Field(default=None, max_length=100, description="bank or receipt reference, never a card")
    note: str | None = Field(default=None, max_length=2000)
    received_amount: Amount | None = Field(default=None, description="what arrived, when another currency")
    received_currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    allow_duplicate_reference: bool = False


class PaymentChanges(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reference: str | None = Field(default=None, max_length=100)
    note: str | None = Field(default=None, max_length=2000)


def _dump(model: BaseModel, **kw: Any) -> dict[str, Any]:
    return model.model_dump(**kw)


# ── Invoices ─────────────────────────────────────────────────────────────────


@router.get("/invoices")
async def list_invoices(
    staff: CanRead,
    pool: Pool,
    status: Annotated[
        Literal["draft", "sent", "partially_paid", "paid", "cancelled", "open", "all"] | None, Query()
    ] = None,
    overdue: Annotated[bool | None, Query()] = None,
    case: Annotated[str | None, Query(max_length=64, description="trip case code or UUID")] = None,
    q: Annotated[str | None, Query(max_length=100, description="invoice number, case code or customer name")] = None,
    start: Annotated[date | None, Query(alias="from")] = None,
    end: Annotated[date | None, Query(alias="to")] = None,
    limit: Annotated[int, Query(ge=1, le=invoices.MAX_PAGE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await invoices.list_invoices(
            conn,
            staff,
            status=status,
            overdue=overdue,
            case=case,
            q=q,
            start=start,
            end=end,
            limit=limit,
            offset=offset,
        )


@router.get("/billing/summary")
async def billing_summary(_: CanRead, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await invoices.summary(conn)


@router.get("/trip-cases/{ref}/billing")
async def case_billing(ref: CaseRef, staff: CanRead, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await invoices.for_case(conn, staff, ref)


@router.post("/trip-cases/{ref}/invoices", status_code=201)
async def create_invoice(
    body: NewInvoice, ref: CaseRef, staff: CanWrite, pool: Pool, response: Response, key: IdempotencyKey = None
) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        out, replayed = await invoices.create(conn, staff, ref, _dump(body), key)
    if replayed:
        response.status_code = 200
    return {**out, "replayed": replayed}


@router.get("/invoices/{number}")
async def get_invoice(number: Number, staff: CanRead, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await invoices.get_invoice(conn, staff, number)


@router.patch("/invoices/{number}")
async def patch_invoice(body: InvoiceChanges, number: Number, staff: CanWrite, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await invoices.update_draft(conn, staff, number, _dump(body, exclude_unset=True))


@router.delete("/invoices/{number}")
async def delete_invoice(number: Number, staff: CanWrite, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await invoices.delete_draft(conn, staff, number)


@router.post("/invoices/{number}/send")
async def send_invoice(number: Number, staff: CanWrite, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await invoices.send(conn, staff, number)


@router.post("/invoices/{number}/cancel")
async def cancel_invoice(body: ReasonIn, number: Number, staff: CanWrite, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await invoices.cancel(conn, staff, number, body.reason)


@router.post("/invoices/{number}/amend")
async def amend_invoice(body: Amendment, number: Number, staff: CanWrite, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await invoices.amend(conn, staff, number, _dump(body, exclude_unset=True))


@router.post("/invoices/{number}/remind")
async def remind_invoice(number: Number, staff: CanWrite, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await invoices.remind(conn, staff, number)


# ── Payments ─────────────────────────────────────────────────────────────────


@router.post("/invoices/{number}/payments", status_code=201)
async def record_payment(
    body: NewPayment, number: Number, staff: CanWrite, pool: Pool, response: Response, key: IdempotencyKey = None
) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        out, replayed = await payments.record(conn, staff, number, _dump(body), key)
    if replayed:
        response.status_code = 200
    return {**out, "replayed": replayed}


@router.get("/payments")
async def list_payments(
    staff: CanRead,
    pool: Pool,
    state: Annotated[Literal["recorded", "verified", "voided"] | None, Query()] = None,
    kind: Annotated[Literal["receipt", "refund"] | None, Query()] = None,
    method: Annotated[Literal["bank_transfer", "card_offline", "cash", "other"] | None, Query()] = None,
    invoice: Annotated[str | None, Query(max_length=64)] = None,
    case: Annotated[str | None, Query(max_length=64)] = None,
    q: Annotated[
        str | None, Query(max_length=100, description="payment ref, invoice number, case code, reference")
    ] = None,
    start: Annotated[date | None, Query(alias="from")] = None,
    end: Annotated[date | None, Query(alias="to")] = None,
    limit: Annotated[int, Query(ge=1, le=payments.MAX_PAGE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await payments.list_payments(
            conn,
            staff,
            state=state,
            kind=kind,
            method=method,
            invoice=invoice,
            case=case,
            q=q,
            start=start,
            end=end,
            limit=limit,
            offset=offset,
        )


@router.get("/payments/{ref}")
async def get_payment(ref: Ref, staff: CanRead, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await payments.get_payment(conn, staff, ref)


@router.patch("/payments/{ref}")
async def patch_payment(body: PaymentChanges, ref: Ref, staff: CanWrite, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await payments.update(conn, staff, ref, _dump(body, exclude_unset=True))


@router.post("/payments/{ref}/verify")
async def verify_payment(ref: Ref, staff: CanVerify, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await payments.verify(conn, staff, ref)


@router.post("/payments/{ref}/void")
async def void_payment(body: ReasonIn, ref: Ref, staff: CanWrite, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await payments.void(conn, staff, ref, body.reason)
