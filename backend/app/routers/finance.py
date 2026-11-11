"""Staff finance routes (AA-1074, bk-045): departure shared costs, supplier purchase orders, profit.

Staff tokens only. Costs and orders carry net amounts, so those services also need ``pricing.read`` (403 otherwise);
``profit.read`` is itself the commercial permission (finance has no ``pricing.read``).

Departure costs (``app.finance.departure_costs``; read ``bill.read``, write ``ops.write``):
- ``GET /departures/{id}/costs``: shared costs, each sold case (revenue, own cost, share), bills of the departure,
  planned against actual in USD, profit.
- ``POST /departures/{id}/costs`` (rate: ``service_id``; manual: ``supplier_id``, ``basis``, ``unit_amount``,
  ``currency``) · ``PATCH /departure-costs/{id}`` · ``POST /departure-costs/{id}/order`` (makes its departure-level
  supplier booking) · ``POST /departure-costs/{id}/remove`` (reason).
- ``GET /finance/departures?from&to``: departures with their tour title, sold seats, shared cost lines and profit.

Purchase orders (``app.finance.supplier_orders``; read ``ops.read``, write ``ops.write``):
- ``GET /supplier-orders`` (``status``, ``supplier_id``) · ``POST /supplier-orders`` (``trip_cases`` codes / ids,
  ``departures`` ids: group their pending bookings into draft orders) · ``GET /supplier-orders/{ref}`` ·
  ``GET /supplier-orders/{ref}/document`` (printable HTML) · ``POST /supplier-orders/{ref}/send`` (``note``) ·
  ``POST /supplier-orders/{ref}/cancel`` (reason) · ``POST /supplier-orders/{ref}/lines/{booking_id}/remove``.

Profit (``app.finance.profit``; ``profit.read``: admin, finance): ``GET /finance/profit/cases`` (``from``, ``to``,
``partner_id``), ``/finance/profit/departures``, ``/finance/profit/agencies``.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, Path, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from app.auth.staff import Staff, get_pool, require
from app.finance import departure_costs as costs
from app.finance import profit
from app.finance import supplier_orders as orders

router = APIRouter(prefix="/booking", tags=["finance"])

Pool = Annotated[asyncpg.Pool, Depends(get_pool)]
CanReadCosts = Annotated[Staff, Depends(require("bill.read"))]
CanOpsRead = Annotated[Staff, Depends(require("ops.read"))]
CanOpsWrite = Annotated[Staff, Depends(require("ops.write"))]
CanProfit = Annotated[Staff, Depends(require("profit.read"))]
DepartureId = Annotated[UUID, Path(description="Departure UUID")]
CostId = Annotated[UUID, Path(description="Departure cost UUID")]
OrderRef = Annotated[str, Path(min_length=1, max_length=64, description="Purchase order UUID or code (PO-000123)")]
Reason = Annotated[str, Field(min_length=1, max_length=500)]
# The printable order is plain HTML with inline styles: no script, no remote resource.
DOCUMENT_CSP = "default-src 'none'; style-src 'unsafe-inline'"


class NewCost(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pricing: Literal["rate", "manual"] | None = None
    service_id: UUID | None = None
    supplier_id: UUID | None = None
    item_description: str | None = Field(default=None, max_length=500)
    service_date: date | None = None
    basis: Literal["group", "vehicle", "day", "person"] | None = None
    quantity: int = Field(default=1, ge=1, le=costs.MAX_QUANTITY)
    unit_amount: Decimal | None = Field(default=None, ge=0, max_digits=16, decimal_places=2)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    notes: str | None = Field(default=None, max_length=2000)


class CostChanges(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_description: str | None = Field(default=None, max_length=500)
    service_date: date | None = None
    basis: Literal["group", "vehicle", "day", "person"] | None = None
    quantity: int | None = Field(default=None, ge=1, le=costs.MAX_QUANTITY)
    unit_amount: Decimal | None = Field(default=None, ge=0, max_digits=16, decimal_places=2)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    notes: str | None = Field(default=None, max_length=2000)


class ReasonIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Reason


class OrderScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trip_cases: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(
        default_factory=list, max_length=orders.MAX_SCOPE
    )
    departures: list[UUID] = Field(default_factory=list, max_length=orders.MAX_SCOPE)


class SendIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(default=None, max_length=2000)


# ── Departure costs ──────────────────────────────────────────────────────────


@router.get("/departures/{departure_id}/costs")
async def departure_costs(departure_id: DepartureId, staff: CanReadCosts, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await costs.departure_costs(conn, staff, str(departure_id))


@router.post("/departures/{departure_id}/costs", status_code=201)
async def add_cost(body: NewCost, departure_id: DepartureId, staff: CanOpsWrite, pool: Pool) -> dict[str, Any]:
    data = body.model_dump(exclude_none=True)
    for key in ("service_id", "supplier_id"):
        if key in data:
            data[key] = str(data[key])
    async with pool.acquire() as conn, conn.transaction():
        return await costs.create(conn, staff, str(departure_id), data)


@router.patch("/departure-costs/{cost_id}")
async def patch_cost(body: CostChanges, cost_id: CostId, staff: CanOpsWrite, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await costs.update(conn, staff, str(cost_id), body.model_dump(exclude_unset=True))


@router.post("/departure-costs/{cost_id}/order")
async def order_cost(cost_id: CostId, staff: CanOpsWrite, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await costs.order(conn, staff, str(cost_id))


@router.post("/departure-costs/{cost_id}/remove")
async def remove_cost(body: ReasonIn, cost_id: CostId, staff: CanOpsWrite, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await costs.remove(conn, staff, str(cost_id), body.reason)


@router.get("/finance/departures")
async def finance_departures(
    staff: CanReadCosts,
    pool: Pool,
    start: Annotated[date | None, Query(alias="from")] = None,
    end: Annotated[date | None, Query(alias="to")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await costs.list_departures(conn, staff, start=start, end=end, limit=limit, offset=offset)


# ── Purchase orders ──────────────────────────────────────────────────────────


@router.get("/supplier-orders")
async def list_orders(
    staff: CanOpsRead,
    pool: Pool,
    status: Annotated[Literal["draft", "sending", "sent", "cancelled", "open"] | None, Query()] = None,
    supplier_id: Annotated[UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=orders.MAX_PAGE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await orders.list_orders(
            conn,
            staff,
            status=status,
            supplier=str(supplier_id) if supplier_id else None,
            limit=limit,
            offset=offset,
        )


@router.post("/supplier-orders", status_code=201)
async def create_orders(body: OrderScope, staff: CanOpsWrite, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await orders.create_orders(
            conn, staff, trip_cases=body.trip_cases, departures=[str(d) for d in body.departures]
        )


@router.get("/supplier-orders/{ref}")
async def get_order(ref: OrderRef, staff: CanOpsRead, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await orders.detail(conn, staff, ref)


@router.get("/supplier-orders/{ref}/document", response_class=HTMLResponse)
async def order_document(ref: OrderRef, staff: CanOpsRead, pool: Pool) -> HTMLResponse:
    async with pool.acquire() as conn:
        body = await orders.document(conn, staff, ref)
    return HTMLResponse(
        body,
        headers={"Cache-Control": "no-store", "Content-Security-Policy": DOCUMENT_CSP},
    )


@router.post("/supplier-orders/{ref}/send")
async def send_order(body: SendIn, ref: OrderRef, staff: CanOpsWrite, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await orders.send(conn, staff, ref, note=body.note)


@router.post("/supplier-orders/{ref}/cancel")
async def cancel_order(body: ReasonIn, ref: OrderRef, staff: CanOpsWrite, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await orders.cancel(conn, staff, ref, body.reason)


@router.post("/supplier-orders/{ref}/lines/{booking_id}/remove")
async def remove_order_line(
    ref: OrderRef, booking_id: Annotated[UUID, Path()], staff: CanOpsWrite, pool: Pool
) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await orders.remove_line(conn, staff, ref, str(booking_id))


# ── Profit ───────────────────────────────────────────────────────────────────


@router.get("/finance/profit/cases")
async def profit_cases(
    _: CanProfit,
    pool: Pool,
    start: Annotated[date | None, Query(alias="from")] = None,
    end: Annotated[date | None, Query(alias="to")] = None,
    partner_id: Annotated[UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=profit.MAX_PAGE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await profit.cases(
            conn,
            start=start,
            end=end,
            partner=str(partner_id) if partner_id else None,
            limit=limit,
            offset=offset,
        )


@router.get("/finance/profit/departures")
async def profit_departures(
    _: CanProfit,
    pool: Pool,
    start: Annotated[date | None, Query(alias="from")] = None,
    end: Annotated[date | None, Query(alias="to")] = None,
    limit: Annotated[int, Query(ge=1, le=profit.MAX_PAGE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await profit.departures(conn, start=start, end=end, limit=limit, offset=offset)


@router.get("/finance/profit/agencies")
async def profit_agencies(
    _: CanProfit,
    pool: Pool,
    start: Annotated[date | None, Query(alias="from")] = None,
    end: Annotated[date | None, Query(alias="to")] = None,
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await profit.agencies(conn, start=start, end=end)
