"""Staff pricing endpoints (root ADR 0005, AA-810).

``POST /booking/pricing/quote-preview`` prices a list of net costs for one channel and selling
currency with the pricing rule and FX rates in force today. It writes nothing: it is the calculator
staff use while building a version. ``POST …/versions/{n}/price`` (routers/trip_cases.py) runs the same
``app.pricing.service.price`` on the version's stored cost lines and saves the total.

``/booking/pricing/rules`` and ``/booking/pricing/fx-rates`` (GET list, POST new pending row, POST ``/{id}/approve``)
manage the policy rows themselves with maker-checker (``app.pricing.policy``, bk-007): reads need ``pricing.read``,
writes and approvals ``pricing.write`` (admin, contracting), and the creator of a row cannot approve it (403).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from app.auth.staff import Staff, get_pool, require
from app.pricing import engine, policy, service

log = logging.getLogger(__name__)

router = APIRouter(prefix="/booking/pricing", tags=["pricing"])

MAX_ITEMS = 200

# Margins and net costs are commercial data (ADR 0005 decision 8): permission ``pricing.read`` in the
# staff role matrix (admin, sales, contracting; not content_creator).
CanPrice = Annotated[Staff, Depends(require("pricing.read"))]
# Entering and approving rules / FX rows: contracting owns rates, admin backs it up (ADR 0005 d8).
CanWritePricing = Annotated[Staff, Depends(require("pricing.write"))]
Pool = Annotated[asyncpg.Pool, Depends(get_pool)]

Currency = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
Money = Annotated[Decimal, Field(ge=0, max_digits=16, decimal_places=2, allow_inf_nan=False)]
PositiveMoney = Annotated[Decimal, Field(gt=0, max_digits=16, decimal_places=2, allow_inf_nan=False)]


class CostItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    net_amount: Money
    net_currency: Currency


class QuotePreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel: engine.Channel
    currency: Currency = engine.BASE_CURRENCY
    peak: bool = False
    items: list[CostItem] = Field(min_length=1, max_length=MAX_ITEMS)
    override_total: PositiveMoney | None = None


class RuleOut(BaseModel):
    channel: engine.Channel
    target_margin: Decimal
    floor_margin: Decimal
    peak_uplift_pts: Decimal
    card_fee_pct: Decimal
    valid_from: date


class FxOut(BaseModel):
    currency: str
    rate_per_usd: Decimal
    buffer_pct: Decimal
    as_of: date
    source: str


class LineOut(BaseModel):
    net_amount: Decimal
    net_currency: str
    net_usd: Decimal
    sell_usd: Decimal
    sell_amount: Decimal


class QuotePreviewResponse(BaseModel):
    channel: engine.Channel
    currency: str
    peak: bool
    priced_on: date
    rule: RuleOut
    fx_rates: list[FxOut]
    margin_applied: Decimal
    lines: list[LineOut]
    net_total_usd: Decimal
    subtotal: Decimal
    rounding_increment: Decimal
    total: Decimal
    override_total: Decimal | None
    final_total: Decimal
    effective_margin: Decimal
    floor_margin: Decimal
    below_floor: bool


def _today() -> date:
    return datetime.now(UTC).date()


def _unprocessable(message: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=message)


@router.post("/quote-preview", response_model=QuotePreviewResponse)
async def quote_preview(
    body: QuotePreviewRequest,
    request: Request,
    staff: CanPrice,
) -> QuotePreviewResponse:
    if body.currency not in engine.SELLING_CURRENCIES:
        raise _unprocessable(f"{body.currency} is not a selling currency")
    db = request.app.state.db
    if not db.configured:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="database not configured")

    try:
        pool = await db.pool()
        async with pool.acquire() as conn:
            priced = await service.price(
                conn,
                channel=body.channel,
                currency=body.currency,
                peak=body.peak,
                lines=[engine.CostLine(item.net_amount, item.net_currency) for item in body.items],
                override_total=body.override_total,
                on=_today(),
            )
    except engine.PricingError as exc:
        raise _unprocessable(str(exc)) from None
    except Exception as exc:  # noqa: BLE001 — never leak DB errors to the caller
        log.warning("quote-preview: pricing data unavailable (%s)", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="pricing data unavailable"
        ) from None

    quote = priced.quote
    log.info(
        "quote-preview staff=%s channel=%s currency=%s lines=%d below_floor=%s",
        staff.cis_user_id,
        quote.channel.value,
        quote.currency,
        len(quote.lines),
        quote.below_floor,
    )
    return QuotePreviewResponse(**service.body(priced))


# ── Pricing rules and FX rates (maker-checker, bk-007) ───────────────────────

Ratio = Annotated[Decimal, Field(ge=0, lt=1, max_digits=5, decimal_places=4, allow_inf_nan=False)]


class RuleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel: engine.Channel
    target_margin: Ratio
    floor_margin: Ratio
    peak_uplift_pts: Ratio = Decimal("0")
    card_fee_pct: Ratio = Decimal("0")
    valid_from: date
    note: str | None = Field(default=None, max_length=500)


class FxRateCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    currency: Currency
    rate_per_usd: Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=8, allow_inf_nan=False)]
    buffer_pct: Ratio = Decimal("0")
    as_of: date
    source: str = Field(min_length=1, max_length=200)


@router.get("/rules")
async def list_rules(
    staff: CanPrice,
    pool: Pool,
    channel: engine.Channel | None = None,
    limit: Annotated[int, Query(ge=1, le=policy.MAX_ROWS)] = policy.MAX_ROWS,
) -> dict[str, Any]:
    today = _today()
    async with pool.acquire() as conn:
        items = await policy.list_rules(
            conn, staff, today=today, channel=channel.value if channel else None, limit=limit
        )
    return {"items": items, "today": today}


@router.post("/rules", status_code=201)
async def create_rule(body: RuleCreate, staff: CanWritePricing, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await policy.create_rule(conn, staff, body.model_dump(), today=_today())


@router.post("/rules/{rule_id}/approve")
async def approve_rule(rule_id: uuid.UUID, staff: CanWritePricing, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await policy.approve_rule(conn, staff, str(rule_id), today=_today())


@router.get("/fx-rates")
async def list_fx_rates(
    staff: CanPrice,
    pool: Pool,
    currency: Annotated[str | None, Query(pattern=r"^[A-Z]{3}$")] = None,
    limit: Annotated[int, Query(ge=1, le=policy.MAX_ROWS)] = policy.MAX_ROWS,
) -> dict[str, Any]:
    today = _today()
    async with pool.acquire() as conn:
        items = await policy.list_fx_rates(conn, staff, today=today, currency=currency, limit=limit)
    return {"items": items, "today": today}


@router.post("/fx-rates", status_code=201)
async def create_fx_rate(body: FxRateCreate, staff: CanWritePricing, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await policy.create_fx_rate(conn, staff, body.model_dump(), today=_today())


@router.post("/fx-rates/{fx_id}/approve")
async def approve_fx_rate(fx_id: uuid.UUID, staff: CanWritePricing, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await policy.approve_fx_rate(conn, staff, str(fx_id), today=_today())
