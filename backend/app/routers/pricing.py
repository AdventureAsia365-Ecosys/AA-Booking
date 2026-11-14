"""Staff pricing endpoints (root ADR 0005, AA-810).

``POST /booking/pricing/quote-preview`` prices a list of net costs for one channel and selling
currency with the pricing rule and FX rates in force today. It writes nothing: it is the calculator
staff use while building a version. ``POST …/versions/{n}/price`` (routers/trip_cases.py) runs the same
``app.pricing.service.price`` on the version's stored cost lines and saves the total.

``/booking/pricing/rules`` and ``/booking/pricing/fx-rates`` (GET list, POST new pending row, POST ``/{id}/approve``;
S280 bk-058: ``/{id}/reject`` with a reason by a second person, ``/{id}/withdraw`` by its maker)
manage the policy rows themselves with maker-checker (``app.pricing.policy``, bk-007): reads need ``pricing.read``,
writes and approvals ``pricing.write`` (admin, contracting), and the creator of a row cannot approve it (403).
``/booking/pricing/cancellation-policies`` (AA-974, bk-021) does the same for the B2B cancellation-fee schedules
(``app.b2b.cancellation``): a row per trip plan (``source_id``) or the B2B default, in force once approved.
``/booking/pricing/policy`` (AA-1091, bk-050, root ADR 0005b d15): the pricing policy parameters (GET every parameter
with the value in force, scheduled and open changes and history; POST a change as a draft or pending; POST
``/{id}/submit``, ``/withdraw`` by its maker, ``/approve`` and ``/reject`` by a second person of the parameter's
approver group, ``/discard`` of another maker's draft by that group (S280); ``app.pricing.policy_admin``).
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
from app.b2b import cancellation
from app.pricing import engine, policy, policy_admin, service

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
    # bk-039 (AA-1029, root ADR 0019 b15): b2b only, the suggested retail price markup on the agency net price.
    rrp_markup_pct: (
        Annotated[Decimal, Field(ge=0, lt=10, max_digits=6, decimal_places=4, allow_inf_nan=False)] | None
    ) = None
    # bk-047 (AA-1075, bk-15): a child pays this fraction of the adult price of a trip plan (None = like an adult);
    # each infant adds this net (USD) priced by the rule (None = infants free).
    child_pct: Annotated[Decimal, Field(ge=0, le=1, max_digits=5, decimal_places=4, allow_inf_nan=False)] | None = None
    infant_flat_usd: (
        Annotated[Decimal, Field(ge=0, lt=10000, max_digits=10, decimal_places=2, allow_inf_nan=False)] | None
    ) = None
    # bk-048 (AA-1073, bk-17): b2b and fixed_departure only; a quote made on or before start - early_days of the
    # departure takes early_discount_pts off the margin (both or neither).
    early_days: Annotated[int, Field(ge=1, le=365)] | None = None
    early_discount_pts: (
        Annotated[Decimal, Field(gt=0, lt=1, max_digits=5, decimal_places=4, allow_inf_nan=False)] | None
    ) = None


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


class RowDecision(BaseModel):
    """S280 (bk-058): a rejection of a pending pricing rule, FX rate or cancellation policy (reason required)."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=policy.MAX_REASON)


@router.post("/rules/{rule_id}/reject")
async def reject_rule(rule_id: uuid.UUID, body: RowDecision, staff: CanWritePricing, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await policy.close_rule(conn, staff, str(rule_id), event="rejected", reason=body.reason, today=_today())


@router.post("/rules/{rule_id}/withdraw")
async def withdraw_rule(rule_id: uuid.UUID, staff: CanWritePricing, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await policy.close_rule(conn, staff, str(rule_id), event="withdrawn", reason=None, today=_today())


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


@router.post("/fx-rates/{fx_id}/reject")
async def reject_fx_rate(fx_id: uuid.UUID, body: RowDecision, staff: CanWritePricing, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await policy.close_fx_rate(conn, staff, str(fx_id), event="rejected", reason=body.reason, today=_today())


@router.post("/fx-rates/{fx_id}/withdraw")
async def withdraw_fx_rate(fx_id: uuid.UUID, staff: CanWritePricing, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await policy.close_fx_rate(conn, staff, str(fx_id), event="withdrawn", reason=None, today=_today())


# ── B2B cancellation-fee policies (AA-974, bk-021, same maker-checker) ───────


class TierIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_days: int = Field(ge=0, le=cancellation.MAX_DAYS)
    fee_pct: Annotated[Decimal, Field(ge=0, le=100, max_digits=5, decimal_places=2, allow_inf_nan=False)]


class CancellationPolicyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=120)
    source_id: str | None = Field(default=None, min_length=1, max_length=128)
    valid_from: date
    tiers: list[TierIn] = Field(min_length=1, max_length=cancellation.MAX_TIERS)
    note: str | None = Field(default=None, max_length=500)


@router.get("/cancellation-policies")
async def list_cancellation_policies(
    staff: CanPrice,
    pool: Pool,
    source_id: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
    limit: Annotated[int, Query(ge=1, le=cancellation.MAX_ROWS)] = cancellation.MAX_ROWS,
) -> dict[str, Any]:
    today = _today()
    async with pool.acquire() as conn:
        items = await cancellation.list_policies(conn, staff, today=today, source_id=source_id, limit=limit)
    return {"items": items, "today": today}


@router.post("/cancellation-policies", status_code=201)
async def create_cancellation_policy(
    body: CancellationPolicyCreate, staff: CanWritePricing, pool: Pool
) -> dict[str, Any]:
    data = body.model_dump()
    data["tiers"] = [{"min_days": t.min_days, "fee_pct": t.fee_pct} for t in body.tiers]
    async with pool.acquire() as conn, conn.transaction():
        return await cancellation.create_policy(conn, staff, data, today=_today())


@router.post("/cancellation-policies/{policy_id}/approve")
async def approve_cancellation_policy(policy_id: uuid.UUID, staff: CanWritePricing, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await cancellation.approve_policy(conn, staff, str(policy_id), today=_today())


@router.post("/cancellation-policies/{policy_id}/reject")
async def reject_cancellation_policy(
    policy_id: uuid.UUID, body: RowDecision, staff: CanWritePricing, pool: Pool
) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await cancellation.close_policy(
            conn, staff, str(policy_id), event="rejected", reason=body.reason, today=_today()
        )


@router.post("/cancellation-policies/{policy_id}/withdraw")
async def withdraw_cancellation_policy(policy_id: uuid.UUID, staff: CanWritePricing, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await cancellation.close_policy(
            conn, staff, str(policy_id), event="withdrawn", reason=None, today=_today()
        )


# ── Pricing policy parameters (AA-1091, bk-050, root ADR 0005b d15) ─────────

CanReadPolicy = Annotated[Staff, Depends(require("pricing_policy.read"))]
CanWritePolicy = Annotated[Staff, Depends(require("pricing_policy.write"))]
# Approve / reject: the per-parameter approver group (pricing lead or finance) is checked in app.pricing.policy_admin.
CanDecidePolicy = Annotated[Staff, Depends(require("pricing_policy.read"))]
PolicyKey = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")]


class PolicyProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: PolicyKey
    # The typed value (app.pricing.policy_params: a number, a decimal string, a boolean, "MM-DD", a list or a map).
    # list[int] before list[str] (S280): an ``integers`` parameter (agency_reminder_steps_days) comes as JSON numbers,
    # which Pydantic never turns into strings, so without it every such change was a 422.
    value: int | str | bool | list[int] | list[str] | dict[str, str]
    effective_from: date
    reason: str = Field(min_length=1, max_length=policy_admin.MAX_REASON)
    # true = submit for approval at once (pending); false = save a draft the maker submits later.
    submit: bool = True


class PolicyDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=policy_admin.MAX_REASON)


@router.get("/policy")
async def list_policy(staff: CanReadPolicy, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await policy_admin.list_params(conn, staff, today=_today())


@router.post("/policy", status_code=201)
async def propose_policy(body: PolicyProposal, staff: CanWritePolicy, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await policy_admin.propose(
            conn,
            staff,
            key=body.key,
            value=body.value,
            effective_from=body.effective_from,
            reason=body.reason,
            submit=body.submit,
            today=_today(),
        )


@router.post("/policy/{version_id}/submit")
async def submit_policy(version_id: uuid.UUID, staff: CanWritePolicy, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await policy_admin.submit(conn, staff, str(version_id), today=_today())


@router.post("/policy/{version_id}/withdraw")
async def withdraw_policy(version_id: uuid.UUID, staff: CanWritePolicy, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await policy_admin.withdraw(conn, staff, str(version_id), today=_today())


@router.post("/policy/{version_id}/approve")
async def approve_policy(version_id: uuid.UUID, staff: CanDecidePolicy, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await policy_admin.approve(conn, staff, str(version_id), today=_today())


@router.post("/policy/{version_id}/reject")
async def reject_policy(
    version_id: uuid.UUID, body: PolicyDecision, staff: CanDecidePolicy, pool: Pool
) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await policy_admin.reject(conn, staff, str(version_id), reason=body.reason, today=_today())


@router.post("/policy/{version_id}/discard")
async def discard_policy(
    version_id: uuid.UUID, body: PolicyDecision, staff: CanDecidePolicy, pool: Pool
) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await policy_admin.discard(conn, staff, str(version_id), reason=body.reason, today=_today())
