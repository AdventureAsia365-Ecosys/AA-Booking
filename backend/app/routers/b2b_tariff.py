"""``GET /booking/b2b/v1/tariff`` (booking.b2b.v1 1.4, AA-1030 G3; twin share, availability per row and coverage 1.12,
AA-1188): the calling agency's own price per tour, season and pax band, as JSON or ``?format=csv`` (``app.b2b.tariff``).
Split out of ``app.routers.b2b`` (its models and route), like ``app.routers.b2b_addons``; same prefix, scope and tag.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import Field

from app.auth.m2m import SCOPE_READ
from app.b2b import tariff
from app.b2b.access import B2BContext, require_scope
from app.b2b.errors import PREFIX
from app.routers.b2b import Currency, FxLock, Money, Strict, TourKey

router = APIRouter(prefix=PREFIX, tags=["b2b"])

CanRead = Annotated[B2BContext, Depends(require_scope(SCOPE_READ))]


class TariffItem(Strict):
    """1.4 (AA-1030 G3). One price matrix window x pax band of a tour, cut where a supplier rate changes (every
    departure from ``valid_from`` to ``valid_to`` prices the same), priced as ``POST /quotes`` would price the plan's
    ``tp:`` line for the band's smallest party on ``priced_for_date`` (= ``valid_from``)."""

    tour: TourKey
    title: str | None = Field(default=None, max_length=500)
    country: str | None = Field(default=None, max_length=120)
    duration_days: int | None = None
    season: str | None = Field(default=None, max_length=80)
    valid_from: date
    valid_to: date
    pax_min: int = Field(ge=1)
    pax_max: int | None = Field(default=None, ge=1)
    priced_for_pax: int = Field(ge=1)
    priced_for_date: date
    price: Money
    price_per_person: Money
    agency_discount_pct: str = Field(pattern=r"^\d+(\.\d{1,4})?$")
    rrp_per_person: Money | None = None
    # 1.7 (AA-1075, bk-15): sell prices by age band and room, priced by the quote engine (never a net).
    child_price_per_person: Money | None = None
    infant_price: Money | None = None
    single_supplement_per_person: Money | None = None
    # 1.12 (AA-1188, D5): price_per_person is the twin share (single_supplement_per_person on top for a room alone);
    # a band that reaches the group size AA quotes on request is on_request; a departure inside the row's dates with a
    # price adjustment in force makes a quote on it differ from the row.
    availability: Literal["available", "on_request"] = "available"
    departure_adjusted: bool = False


class Tariff(Strict):
    """1.4 (AA-1030 G3). The calling agency's own tariff: its price per tour, season and pax band."""

    currency: Currency
    priced_on: date
    basis: Literal["per_person_band_min_twin_share"]
    tier_code: str = Field(max_length=32)
    fx: FxLock
    items: list[TariffItem]
    truncated: bool
    # 1.12 (AA-1188): the pricing policy's group size quoted on request, the b2b rule's early-booking days (null: no
    # early-booking discount), the tours with a priced row and the tours AA serves (null for a one-tour read).
    group_on_request_min_pax: int | None = Field(default=None, ge=1)
    early_booking_days: int | None = Field(default=None, ge=0)
    tours_priced: int = Field(default=0, ge=0)
    catalog_tours: int | None = Field(default=None, ge=0)


@router.get(
    "/tariff",
    response_model=Tariff,
    responses={200: {"content": {"text/csv": {"schema": {"type": "string"}}}}},
)
async def get_tariff(
    ctx: CanRead,
    currency: Annotated[str, Query(pattern=r"^[A-Z]{3}$")] = "USD",
    source_id: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
    format: Annotated[Literal["json", "csv"], Query()] = "json",  # noqa: A002 — the query parameter's name
) -> Any:
    """1.4 (AA-1030 G3): the agency's own price per tour, season and pax band (``app.b2b.tariff``); ``format=csv``
    answers the same rows as a CSV file."""
    on = datetime.now(UTC).date()
    async with ctx.pool.acquire() as conn:
        body = await tariff.build(conn, partner_id=ctx.partner_id, currency=currency, on=on, source_id=source_id)
    if format == "csv":
        name = tariff.filename(str(ctx.partner["agency_name"]), currency, on)
        return Response(
            tariff.to_csv(body),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"},
        )
    return tariff.agency_view(body)
