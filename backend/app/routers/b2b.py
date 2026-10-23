"""The Booking B2B API ``/booking/b2b/v1`` for AA-Partner (contract booking.b2b.v1, root ADR 0005a; AA-972).

Machine routes only: an M2M access token of the ``aa-m2m`` pool with the route's scope and the agency's
``X-AA-Tenant-Id`` (``app.b2b.access``). No staff token is accepted here, and no M2M token is accepted on staff routes
(separate verifiers). Errors are the contract's ``Problem`` (``app.b2b.errors``). No personal data crosses this API.

AA-972 serves ``GET /products``, ``POST /quotes``, ``GET /fx-rates`` and ``GET /price-parity``. Departures and holds are
AA-973, commercial rules (VND, cancellation fees) AA-974, the event feed AA-975.

The response models mirror the contract with ``extra="forbid"``; the agency-facing price is ``b2b_price`` and nothing
named ``supplier_net`` (or any net, cost or margin) is ever in a response (``tests/unit/test_b2b_api.py``).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from app.auth.m2m import SCOPE_QUOTE, SCOPE_READ
from app.b2b import quotes, reads
from app.b2b.access import B2BContext, require_scope
from app.b2b.components import LineAsk
from app.b2b.errors import PREFIX

router = APIRouter(prefix=PREFIX, tags=["b2b"])

CanRead = Annotated[B2BContext, Depends(require_scope(SCOPE_READ))]
CanQuote = Annotated[B2BContext, Depends(require_scope(SCOPE_QUOTE))]

Currency = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
Kind = Literal["trip_plan", "hotel_room", "transfer", "guide", "activity", "entrance", "meal"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ── Request models ───────────────────────────────────────────────────────────


class Pax(Strict):
    adult: int = Field(ge=1, le=60)
    child: int = Field(default=0, ge=0, le=60)
    infant: int = Field(default=0, ge=0, le=20)


class Room(Strict):
    room_type_id: str | None = Field(default=None, max_length=64)
    occupancy: Literal["single", "double", "twin", "triple", "family"]
    count: int = Field(default=1, ge=1, le=30)


class QuoteLineRequest(Strict):
    line_ref: str = Field(min_length=1, max_length=64)
    component_id: str = Field(min_length=1, max_length=64)
    departure_id: str | None = Field(default=None, max_length=64)
    service_date: date
    nights: int | None = Field(default=None, ge=1, le=60)
    pax: Pax
    rooms: list[Room] = Field(default_factory=list, max_length=30)


class QuoteRequest(Strict):
    proposal_ref: str = Field(min_length=1, max_length=64)
    proposal_version: int = Field(ge=1)
    currency: Currency
    lines: list[QuoteLineRequest] = Field(min_length=1, max_length=200)


# ── Response models (contract booking.b2b.v1 components.schemas) ─────────────


class Money(Strict):
    amount: str = Field(pattern=r"^-?\d+(\.\d{1,2})?$")
    currency: Currency


class TourKey(Strict):
    source_system: Literal["cis"]
    source_id: str = Field(min_length=1, max_length=128)


class Component(Strict):
    component_id: str = Field(min_length=1, max_length=64)
    kind: Kind
    day_no: int | None = Field(default=None, ge=0)
    destination_id: str | None = Field(default=None, max_length=64)


class Product(Strict):
    tour: TourKey
    trip_plan_id: str = Field(max_length=64)
    sellable: bool
    has_fixed_departures: bool
    components: list[Component]
    updated_at: datetime


class ProductPage(Strict):
    items: list[Product]
    next_cursor: str | None = Field(default=None, max_length=200)


class FxLock(Strict):
    base_currency: Literal["USD"]
    quote_currency: Currency
    rate_per_usd: str = Field(pattern=r"^\d+(\.\d{1,8})?$")
    rate_date: date
    source: str = Field(max_length=64)


class QuoteLine(Strict):
    line_ref: str = Field(max_length=64)
    b2b_price: Money
    unit: Literal["per_person", "per_group", "per_room_night", "per_line"]
    availability: Literal["available", "on_request", "sold_out"]
    departure_id: str | None = Field(default=None, max_length=64)


class Quote(Strict):
    quote_id: str = Field(max_length=64)
    currency: Currency
    fx: FxLock
    valid_until: datetime
    price_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    lines: list[QuoteLine]
    total_b2b_price: Money


class FxRate(Strict):
    currency: Currency
    rate_per_usd: str = Field(pattern=r"^\d+(\.\d{1,8})?$")
    as_of: date
    source: str = Field(max_length=64)


class FxRates(Strict):
    date: date
    base_currency: Literal["USD"]
    rates: list[FxRate]


class PriceParity(Strict):
    tour: TourKey
    currency: Currency
    from_price: Money | None
    basis: Literal["per_person_2pax_low_season"]


# ── Routes ───────────────────────────────────────────────────────────────────


@router.get("/products", response_model=ProductPage)
async def list_products(
    ctx: CanRead,
    updated_since: datetime | None = None,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
) -> dict:
    return await reads.list_products(ctx.pool, cursor=cursor, updated_since=updated_since)


@router.post("/quotes", response_model=Quote, status_code=201)
async def create_quote(body: QuoteRequest, ctx: CanQuote) -> dict:
    lines = [
        quotes.AskLine(
            ask=LineAsk(
                line_ref=line.line_ref,
                component_id=line.component_id,
                service_date=line.service_date,
                nights=line.nights,
                adult=line.pax.adult,
                child=line.pax.child,
                infant=line.pax.infant,
                room_count=sum(r.count for r in line.rooms) or None,
            ),
            rooms=[r.model_dump() for r in line.rooms],
            departure_id=line.departure_id,
        )
        for line in body.lines
    ]
    return await quotes.create_quote(
        ctx.pool,
        partner_id=ctx.partner_id,
        client_id=ctx.caller.client_id,
        proposal_ref=body.proposal_ref,
        proposal_version=body.proposal_version,
        currency=body.currency,
        lines=lines,
    )


@router.get("/fx-rates", response_model=FxRates)
async def get_fx_rates(ctx: CanRead, on: Annotated[date, Query(alias="date")]) -> dict:
    return await reads.fx_rates(ctx.pool, on)


@router.get("/price-parity", response_model=PriceParity)
async def get_price_parity(
    ctx: CanRead,
    source_system: Literal["cis"],
    source_id: Annotated[str, Query(min_length=1, max_length=128)],
    currency: Annotated[str, Query(pattern=r"^[A-Z]{3}$")],
) -> dict:
    return await reads.price_parity(ctx.pool, source_id=source_id, currency=currency, on=datetime.now(UTC).date())
