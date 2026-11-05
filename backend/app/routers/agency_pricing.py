"""Staff endpoints for agency price tiers and contract rate cards (AA-1030, bk-045; root ADR 0019 b15).

``/booking/pricing/partner-tiers`` lists the tiers with the discount in force and the margin it leaves under the
``b2b`` rule; ``POST`` adds a tier, ``PATCH /{code}`` relabels or reorders one. ``/booking/pricing/partner-tier-
discounts`` and ``/booking/pricing/partner-rate-cards`` (GET list, POST new pending row, POST ``/{id}/approve``)
manage the dated discount rows with maker-checker, like the pricing rules (``app.routers.pricing``).
``/booking/pricing/partner-terms`` lists every B2B partner with its tier and discount today; ``PATCH /{partner_id}``
moves a partner to another tier (audited). Reads need ``pricing.read``, writes ``pricing.write``. Service:
``app.b2b.agency_pricing``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Any

import asyncpg
from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel, ConfigDict, Field

from app.auth.staff import Staff, get_pool, require
from app.b2b import agency_pricing

router = APIRouter(prefix="/booking/pricing", tags=["pricing"])

CanPrice = Annotated[Staff, Depends(require("pricing.read"))]
CanWritePricing = Annotated[Staff, Depends(require("pricing.write"))]
Pool = Annotated[asyncpg.Pool, Depends(get_pool)]

TierCode = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{1,31}$")]
Discount = Annotated[Decimal, Field(ge=0, lt=Decimal("0.5"), max_digits=5, decimal_places=4, allow_inf_nan=False)]


def _today() -> date:
    return datetime.now(UTC).date()


class TierCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: TierCode
    label: str = Field(min_length=1, max_length=60)
    sort_order: int = Field(default=0, ge=0, le=999)


class TierPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str | None = Field(default=None, min_length=1, max_length=60)
    sort_order: int | None = Field(default=None, ge=0, le=999)


class TierDiscountCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tier_code: TierCode
    discount_pct: Discount
    valid_from: date
    note: str | None = Field(default=None, max_length=500)


class RateCardCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    partner_id: uuid.UUID
    source_id: str | None = Field(default=None, min_length=1, max_length=128)
    discount_pct: Discount
    valid_from: date
    valid_to: date | None = None
    contract_ref: str | None = Field(default=None, max_length=120)
    note: str | None = Field(default=None, max_length=500)


class PartnerTierPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tier_code: TierCode


# ── Tiers ────────────────────────────────────────────────────────────────────


@router.get("/partner-tiers")
async def list_partner_tiers(_: CanPrice, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await agency_pricing.list_tiers(conn, today=_today())


@router.post("/partner-tiers", status_code=201)
async def create_partner_tier(body: TierCreate, staff: CanWritePricing, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await agency_pricing.create_tier(conn, staff, body.model_dump(), today=_today())


@router.patch("/partner-tiers/{code}")
async def update_partner_tier(
    code: Annotated[str, Path(pattern=r"^[a-z][a-z0-9_]{1,31}$")], body: TierPatch, staff: CanWritePricing, pool: Pool
) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await agency_pricing.update_tier(conn, staff, code, body.model_dump(exclude_unset=True), today=_today())


# ── Tier discounts ───────────────────────────────────────────────────────────


@router.get("/partner-tier-discounts")
async def list_tier_discounts(
    staff: CanPrice,
    pool: Pool,
    tier_code: Annotated[str | None, Query(pattern=r"^[a-z][a-z0-9_]{1,31}$")] = None,
    limit: Annotated[int, Query(ge=1, le=agency_pricing.MAX_ROWS)] = agency_pricing.MAX_ROWS,
) -> dict[str, Any]:
    today = _today()
    async with pool.acquire() as conn:
        items = await agency_pricing.list_tier_discounts(conn, staff, today=today, tier_code=tier_code, limit=limit)
    return {"items": items, "today": today}


@router.post("/partner-tier-discounts", status_code=201)
async def create_tier_discount(body: TierDiscountCreate, staff: CanWritePricing, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await agency_pricing.create_tier_discount(conn, staff, body.model_dump(), today=_today())


@router.post("/partner-tier-discounts/{row_id}/approve")
async def approve_tier_discount(row_id: uuid.UUID, staff: CanWritePricing, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await agency_pricing.approve_tier_discount(conn, staff, str(row_id), today=_today())


# ── Contract rate cards ──────────────────────────────────────────────────────


@router.get("/partner-rate-cards")
async def list_rate_cards(
    staff: CanPrice,
    pool: Pool,
    partner_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=agency_pricing.MAX_ROWS)] = agency_pricing.MAX_ROWS,
) -> dict[str, Any]:
    today = _today()
    async with pool.acquire() as conn:
        items = await agency_pricing.list_rate_cards(
            conn, staff, today=today, partner_id=str(partner_id) if partner_id else None, limit=limit
        )
    return {"items": items, "today": today}


@router.post("/partner-rate-cards", status_code=201)
async def create_rate_card(body: RateCardCreate, staff: CanWritePricing, pool: Pool) -> dict[str, Any]:
    data = body.model_dump()
    data["partner_id"] = str(body.partner_id)
    async with pool.acquire() as conn, conn.transaction():
        return await agency_pricing.create_rate_card(conn, staff, data, today=_today())


@router.post("/partner-rate-cards/{row_id}/approve")
async def approve_rate_card(row_id: uuid.UUID, staff: CanWritePricing, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await agency_pricing.approve_rate_card(conn, staff, str(row_id), today=_today())


# ── Partners and their tier ──────────────────────────────────────────────────


@router.get("/partner-terms")
async def list_partner_terms(_: CanPrice, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await agency_pricing.list_partner_terms(conn, today=_today())


@router.patch("/partner-terms/{partner_id}")
async def set_partner_tier(
    partner_id: uuid.UUID, body: PartnerTierPatch, staff: CanWritePricing, pool: Pool
) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await agency_pricing.set_partner_tier(conn, staff, str(partner_id), body.tier_code, today=_today())
