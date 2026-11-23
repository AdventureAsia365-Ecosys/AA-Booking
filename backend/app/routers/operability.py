"""The can-operate check of a tour and its admin sale exception (AA-1149 B13, bk-065; ``app/catalog/operability.py``,
``app/catalog/sell_exceptions.py``).

- ``GET /booking/catalog/trip-plans/{source_id}/operability`` (``pricing.read``): status (ready, warnings, blocked,
  exception), every finding, the live exception, the nights that need no hotel, the days and the policy's default
  and maximum exception length.
- ``POST …/sell-exception`` (``operable_exception.approve``: admin only, never a custom role): allow the sale for the
  chosen rules' current findings (N1 nights without a hotel, N3 suppliers not active) with a reason of 20..500
  characters, for ``days`` (default and maximum from the pricing policy). A missing rate or an inactive item is never
  overridable (422).
- ``POST …/sell-exception/revoke`` (same capability): end the live exception now, with a reason.
- ``PUT …/no-hotel-nights`` (``trip_plan.write``): the nights of the tour that need no hotel.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

import asyncpg
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from app.auth.staff import Staff, get_pool, require
from app.catalog import sell_exceptions
from app.routers.catalog import SourceId

router = APIRouter(prefix="/booking/catalog", tags=["catalog"])

Pool = Annotated[asyncpg.Pool, Depends(get_pool)]
CanReadPrices = Annotated[Staff, Depends(require("pricing.read"))]
CanException = Annotated[Staff, Depends(require("operable_exception.approve"))]
CanWritePlans = Annotated[Staff, Depends(require("trip_plan.write"))]


class ExceptionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rules: list[Literal["N1", "N3"]] = Field(min_length=1, max_length=2)
    reason: str = Field(min_length=sell_exceptions.REASON_MIN, max_length=sell_exceptions.REASON_MAX)
    days: int | None = Field(default=None, ge=1, le=90, description="default and maximum: the pricing policy")


class RevokeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=sell_exceptions.REVOKE_MAX)


class NoHotelNightsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nights: list[Annotated[int, Field(ge=1, le=365)]] = Field(max_length=365)


@router.get("/trip-plans/{source_id}/operability")
async def get_operability(source_id: SourceId, _: CanReadPrices, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await sell_exceptions.operability_of(conn, source_id)


@router.post("/trip-plans/{source_id}/sell-exception")
async def create_sell_exception(
    body: ExceptionIn, source_id: SourceId, staff: CanException, pool: Pool
) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await sell_exceptions.create_exception(
            conn, staff, source_id, rules=list(body.rules), reason=body.reason, days=body.days
        )


@router.post("/trip-plans/{source_id}/sell-exception/revoke")
async def revoke_sell_exception(body: RevokeIn, source_id: SourceId, staff: CanException, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await sell_exceptions.revoke_exception(conn, staff, source_id, reason=body.reason)


@router.put("/trip-plans/{source_id}/no-hotel-nights")
async def put_no_hotel_nights(
    body: NoHotelNightsIn, source_id: SourceId, staff: CanWritePlans, pool: Pool
) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await sell_exceptions.set_no_hotel_nights(conn, staff, source_id, list(body.nights))
