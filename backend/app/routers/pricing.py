"""Staff pricing endpoints (root ADR 0005, AA-810).

``POST /booking/pricing/quote-preview`` prices a list of net costs for one channel and selling
currency with the pricing rule and FX rates in force today. It writes nothing: it is the calculator
staff use while building a version; locking a price on a sent quote comes with the quote slice.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from app.auth.staff import Staff, require
from app.pricing import engine, repository

log = logging.getLogger(__name__)

router = APIRouter(prefix="/booking/pricing", tags=["pricing"])

MAX_ITEMS = 200

# Margins and net costs are commercial data (ADR 0005 decision 8): permission ``pricing.read`` in the
# staff role matrix (admin, sales, contracting; not content_creator).
CanPrice = Annotated[Staff, Depends(require("pricing.read"))]

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

    on = _today()
    currencies = {body.currency, *(item.net_currency for item in body.items)}
    try:
        pool = await db.pool()
        async with pool.acquire() as conn:
            dated_rule = await repository.rule_in_force(conn, body.channel, on)
            dated_rates = await repository.fx_rates_in_force(conn, currencies, on)
    except Exception as exc:  # noqa: BLE001 — never leak DB errors to the caller
        log.warning("quote-preview: pricing data unavailable (%s)", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="pricing data unavailable"
        ) from None

    if dated_rule is None:
        raise _unprocessable(f"no pricing rule in force for channel {body.channel.value}")
    missing = sorted(c for c in currencies if c != engine.BASE_CURRENCY and c not in dated_rates)
    if missing:
        raise _unprocessable(f"no FX rate for {', '.join(missing)}")

    try:
        quote = engine.price_quote(
            [engine.CostLine(item.net_amount, item.net_currency) for item in body.items],
            dated_rule.rule,
            currency=body.currency,
            peak=body.peak,
            rates={code: dated.rate for code, dated in dated_rates.items()},
            override_total=body.override_total,
        )
    except engine.PricingError as exc:
        raise _unprocessable(str(exc)) from None

    rule = dated_rule.rule
    log.info(
        "quote-preview staff=%s channel=%s currency=%s lines=%d below_floor=%s",
        staff.cis_user_id,
        rule.channel.value,
        quote.currency,
        len(quote.lines),
        quote.below_floor,
    )
    return QuotePreviewResponse(
        channel=quote.channel,
        currency=quote.currency,
        peak=quote.peak,
        priced_on=on,
        rule=RuleOut(
            channel=rule.channel,
            target_margin=rule.target_margin,
            floor_margin=rule.floor_margin,
            peak_uplift_pts=rule.peak_uplift_pts,
            card_fee_pct=rule.card_fee_pct,
            valid_from=dated_rule.valid_from,
        ),
        fx_rates=[
            FxOut(
                currency=code,
                rate_per_usd=dated.rate.rate_per_usd,
                buffer_pct=dated.rate.buffer_pct,
                as_of=dated.as_of,
                source=dated.source,
            )
            for code, dated in sorted(dated_rates.items())
        ],
        margin_applied=quote.margin_applied,
        lines=[LineOut(**vars(line)) for line in quote.lines],
        net_total_usd=quote.net_total_usd,
        subtotal=quote.subtotal,
        rounding_increment=quote.rounding_increment,
        total=quote.total,
        override_total=quote.override_total,
        final_total=quote.final_total,
        effective_margin=quote.effective_margin,
        floor_margin=quote.floor_margin,
        below_floor=quote.below_floor,
    )
