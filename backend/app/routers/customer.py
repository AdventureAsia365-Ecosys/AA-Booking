"""Customer routes: My Payments for a signed-in traveller (AA-682 S5; contract ``booking.customer.v1``, root
``docs/contracts/booking.customer.v1.openapi.yaml``).

- ``GET /booking/customer/v1/payments``: the traveller's payment requests (invoices), newest first, each with its
  schedule, verified payments and the payment instructions while money is owed.
- ``GET /booking/customer/v1/invoices/{number}``: one of them; 404 ``invoice_not_found`` for anything else.

Read only (no pay button, no upload; design D14). Auth: a Cognito EXTERNAL access token of the ``tripplanner`` or
``booking-customer`` client (``app.auth.customer``): 401 ``sign_in_required`` otherwise, 503 ``auth_unavailable``
while the pool's keys cannot be loaded. The TripPlanner BFF calls these routes with the traveller's token; staff and
M2M tokens are refused. Scope and field policy: ``app.ops.my_payments``. The response models below are the field
allow-list: FastAPI serialises only the declared fields, so a column added to the SQL never leaks by accident.
Every answer is ``Cache-Control: no-store``, refusals included: :class:`_NoStoreRoute` stamps it on the 200, on the
401/503 the auth dependency raises and on the 404 ``invoice_not_found`` (answered here, not by the app-wide
``service_error_handler``, which would drop the header).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date, datetime
from typing import Annotated, Literal

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from app.auth.customer import Traveller, current_traveller
from app.auth.staff import get_pool
from app.errors import ServiceError
from app.ops import my_payments

NO_STORE = {"Cache-Control": "no-store"}


class _NoStoreRoute(APIRoute):
    """Every answer of this router is ``Cache-Control: no-store`` (contract ``booking.customer.v1``).

    The dependencies (the traveller's token, the pool) run inside the handler this wraps, so their 401/503
    ``HTTPException`` passes through here and gets the header before the app's handler renders it. A
    ``ServiceError`` (404 ``invoice_not_found``) is rendered here with the same body ``service_error_handler`` gives.
    """

    def get_route_handler(self) -> Callable[[Request], Awaitable[Response]]:
        handler = super().get_route_handler()

        async def no_store_handler(request: Request) -> Response:
            try:
                response = await handler(request)
            except HTTPException as exc:
                exc.headers = {**(exc.headers or {}), **NO_STORE}
                raise
            except ServiceError as exc:
                return JSONResponse(status_code=exc.status_code, content=exc.body(), headers=NO_STORE)
            response.headers.update(NO_STORE)
            return response

        return no_store_handler


router = APIRouter(prefix="/booking/customer/v1", tags=["customer"], route_class=_NoStoreRoute)

Pool = Annotated[asyncpg.Pool, Depends(get_pool)]
SignedIn = Annotated[Traveller, Depends(current_traveller)]
Money = Annotated[str, Field(pattern=r"^-?[0-9]+\.[0-9]{2}$", description="Decimal string, 2 places")]
Currency = Annotated[str, Field(pattern=r"^[A-Z]{3}$", description="ISO 4217")]


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TripRef(_Out):
    code: str = Field(description="Booking trip reference (TC-000123)")
    planner_trip_id: str | None = Field(description="The TripPlanner trip this booking came from, if any")
    start_date: date | None
    end_date: date | None


class ScheduleRow(_Out):
    seq: int
    kind: Literal["deposit", "second", "final", "other"]
    amount: Money
    due_date: date
    remaining_amount: Money
    state: Literal["scheduled", "due_soon", "paid", "overdue", "cancelled"]


class Payment(_Out):
    ref: str = Field(description="Payment reference (PMT-000123)")
    kind: Literal["receipt", "refund"]
    amount: Money
    currency: Currency
    method: Literal["bank_transfer", "card_offline", "cash", "other"]
    received_on: date


class Invoice(_Out):
    invoice_number: str
    status: Literal["sent", "partially_paid", "paid", "cancelled"]
    document_type: Literal["proforma"]
    currency: Currency
    total_amount: Money
    paid_amount: Money = Field(description="Verified receipts minus verified refunds")
    balance: Money | None = Field(description="Total minus paid; negative = overpaid; null when cancelled")
    next_due_date: date | None
    overdue_amount: Money
    issued_at: datetime | None
    trip: TripRef
    schedule: list[ScheduleRow]
    payments: list[Payment] = Field(description="Verified payments only")
    payment_instructions: str | None = Field(description="How to pay; only while money is owed")


class Payments(_Out):
    invoices: list[Invoice]


@router.get("/payments", response_model=Payments, operation_id="listMyPayments")
async def list_my_payments(traveller: SignedIn, pool: Pool) -> Payments:
    async with pool.acquire() as conn:
        return Payments.model_validate(await my_payments.list_payments(conn, traveller))


@router.get("/invoices/{number}", response_model=Invoice, operation_id="getMyInvoice")
async def get_my_invoice(
    traveller: SignedIn,
    pool: Pool,
    # No max_length: an over-long number must be the contract's 404 invoice_not_found, not a 422 validation array
    # (my_payments.get_invoice refuses anything that is not NUMBER_RE without a read).
    number: Annotated[str, Path(min_length=1, description="Invoice number (INV-000123)")],
) -> Invoice:
    async with pool.acquire() as conn:
        return Invoice.model_validate(await my_payments.get_invoice(conn, traveller, number))
