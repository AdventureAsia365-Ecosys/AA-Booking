"""Staff catalog endpoints over the CIS catalog read model (CIS migration 225 grants role ``booking`` SELECT).

``GET /booking/catalog/destinations`` is the place picker of the supplier form (``catalog.v_destination``, AA-822;
any active staff).

Sellable trip plans (AA-679, S237 decision D4, bk-009):
``GET /booking/catalog/trip-plans`` lists every ``catalog.v_tour`` row with its Booking commercial state
(``catalog.read``: every staff role); ``?orphaned=true`` lists instead the plans whose tour left the catalog
(AA-986 L5, with the reason and what is still attached). ``GET …/facets`` gives the filter options and counts.
``GET …/{source_id}`` is one tour with its CIS days and photos plus the commercial row.
``PUT …/{source_id}/commercial`` upserts the commercial row (``trip_plan.write``: admin, contracting).
``GET …/{source_id}/prices`` (``pricing.read``) and ``PUT …/{source_id}/prices`` (``trip_plan.write``, replaces the
set) keep the plan's per-person price matrix (bk-032, AA-996; ``app/catalog/trip_plan_prices.py``).
``POST …/{source_id}/trip-cases`` starts a trip case, or a new version of an existing case, from a sellable plan
(``trip_case.write`` + ``version.write``: admin, sales).
``GET …/{source_id}/components`` (``supply.read``) and ``PUT …/{source_id}/components`` (``trip_plan.write``, replaces
the set) keep the plan's bookable supply items (bk-019, AA-972), which ``/booking/b2b/v1/products`` lists.

Countries and destinations (AA-996, bk-029; ``app/catalog/geo.py``): ``GET /booking/catalog/geo/destinations`` (paged
CIS places + Booking status + summary), ``GET …/destinations/{destination_id}`` (+ photos and active supply at the
place), ``GET /booking/catalog/geo/countries`` (countries of the catalog + Booking state), ``GET …/countries/{iso}``
(+ its places and tours) — ``catalog.read``; ``PATCH …/destinations/{destination_id}`` and ``PATCH …/countries/{iso}``
change only the Booking state (status, reason, area, notes; soft, nothing is deleted) — ``catalog.admin`` (admin).

``source_id`` is the stable CIS catalog key of contract catalog.tour.v1 (a uuid string), never the CIS ``tour_id``.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Literal

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app import catalog
from app.auth.staff import Staff, current_staff, get_pool, require
from app.b2b import departures, plan_components
from app.catalog import geo, trip_plan_prices, trip_plans
from app.pricing import engine
from app.routers.trip_cases import CountryCode, Currency, NewCustomer
from app.trip_cases import service

router = APIRouter(prefix="/booking/catalog", tags=["catalog"])

Pool = Annotated[asyncpg.Pool, Depends(get_pool)]
Me = Annotated[Staff, Depends(current_staff)]
CanRead = Annotated[Staff, Depends(require("catalog.read"))]
CanWritePlans = Annotated[Staff, Depends(require("trip_plan.write"))]
CanStartCase = Annotated[Staff, Depends(require("trip_case.write"))]
CanReadSupply = Annotated[Staff, Depends(require("supply.read"))]
CanReadPrices = Annotated[Staff, Depends(require("pricing.read"))]
CanAdminCatalog = Annotated[Staff, Depends(require("catalog.admin"))]
IsoCode = Annotated[str, Path(pattern=r"^[A-Za-z]{2}$", description="ISO 3166-1 alpha-2 country code")]
GeoStatus = Literal["active", "inactive"]


def _source_id(source_id: Annotated[uuid.UUID, Path(description="CIS catalog source_id (catalog.tour.v1)")]) -> str:
    return str(source_id)


SourceId = Annotated[str, Depends(_source_id)]


class PriceHint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=2, allow_inf_nan=False)]
    currency: Currency
    basis: Literal["per_person", "per_group"] = "per_person"


class CommercialBody(BaseModel):
    """Every commercial field (PUT): a field left out is cleared, except ``supplier_id`` (left out = kept, AA-996)."""

    model_config = ConfigDict(extra="forbid")

    sellable: bool
    trip_style: str | None = Field(default=None, max_length=60)
    default_channel: engine.Channel | None = None
    price_hint: PriceHint | None = None
    commercial_notes: str | None = Field(default=None, max_length=5_000)
    supplier_id: uuid.UUID | None = None

    def data(self) -> dict[str, Any]:
        out = self.model_dump()
        if "supplier_id" not in self.model_fields_set:
            out.pop("supplier_id")
        elif out["supplier_id"] is not None:
            out["supplier_id"] = str(out["supplier_id"])
        return out


PerPerson = Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=2, allow_inf_nan=False)]
NetCost = Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=2, allow_inf_nan=False)]
Pax = Annotated[int, Field(ge=1, le=999)]


class PriceRowIn(BaseModel):
    """One price matrix row: the selling price per person, or the net cost per person to price it from (AA-996)."""

    model_config = ConfigDict(extra="forbid")

    channel: engine.Channel
    season: str | None = Field(default=None, max_length=80)
    peak: bool = False
    valid_from: date
    valid_to: date
    pax_min: Pax = 1
    pax_max: Pax | None = None
    currency: Currency = engine.BASE_CURRENCY
    amount: PerPerson | None = Field(default=None, description="price per person; with a net cost, an override")
    net_amount: NetCost | None = Field(default=None, description="net cost per person to price the row from")
    net_currency: Currency | None = None


class PricesBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[PriceRowIn] = Field(max_length=trip_plan_prices.MAX_ROWS)
    below_floor_reason: str | None = Field(default=None, max_length=500, description="required below the floor")


class DestinationPatch(BaseModel):
    """Booking state of one CIS place; a field left out keeps its value. ``status = active`` clears the reason."""

    model_config = ConfigDict(extra="forbid")

    status: GeoStatus | None = None
    status_reason: str | None = Field(default=None, max_length=500)
    internal_notes: str | None = Field(default=None, max_length=5_000)


class CountryPatch(DestinationPatch):
    area: str | None = Field(default=None, max_length=60)


def _patch(body: BaseModel) -> dict[str, Any]:
    data = body.model_dump(exclude_unset=True)
    if data.get("status", "") is None:
        data.pop("status")
    return data


class UseForTripCase(BaseModel):
    """``trip_case`` (a case UUID or code) adds a version to that case; otherwise a new case is created for
    ``customer_id`` or ``new_customer`` (same fields as ``POST /booking/trip-cases``)."""

    model_config = ConfigDict(extra="forbid")

    trip_case: str | None = Field(default=None, min_length=1, max_length=64)
    customer_id: uuid.UUID | None = None
    new_customer: NewCustomer | None = None
    start_date: date | None = None
    pax: int | None = Field(default=None, ge=1, le=999)
    pax_adults: int | None = Field(default=None, ge=1, le=999)
    pax_children: int | None = Field(default=None, ge=0, le=998)
    currency: Currency = "USD"
    trip_style: str | None = Field(default=None, max_length=60)
    notes: str | None = Field(default=None, max_length=10_000)
    countries: list[CountryCode] = Field(default_factory=list, max_length=service.MAX_COUNTRIES)
    assignee: str | None = Field(default=None, min_length=1, max_length=128)
    initial_status: Literal["new_inquiry", "discovery"] = "new_inquiry"

    @model_validator(mode="after")
    def _one_target(self) -> UseForTripCase:
        has_customer = self.customer_id is not None or self.new_customer is not None
        if self.trip_case is not None:
            if has_customer:
                raise ValueError("send trip_case or a customer, not both")
            return self
        if (self.customer_id is None) == (self.new_customer is None):
            raise ValueError("send trip_case, or exactly one of customer_id or new_customer")
        return self


@router.get("/destinations")
async def search_destinations(
    _: Me,
    pool: Pool,
    q: Annotated[str | None, Query(max_length=120, description="part of the place name")] = None,
    country: Annotated[str | None, Query(min_length=2, max_length=2, description="ISO-2 country code")] = None,
    limit: Annotated[int, Query(ge=1, le=catalog.MAX_SEARCH)] = 20,
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await catalog.search_destinations(conn, q=q, country=country, limit=limit)


@router.get("/geo/destinations")
async def list_geo_destinations(
    _: CanRead,
    pool: Pool,
    q: Annotated[str | None, Query(max_length=120)] = None,
    country: Annotated[str | None, Query(min_length=2, max_length=2, description="ISO-2 country code")] = None,
    status: GeoStatus | None = None,
    sort: Annotated[str | None, Query(max_length=40)] = None,
    limit: Annotated[int, Query(ge=1, le=geo.MAX_PAGE)] = 25,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await geo.list_destinations(
            conn, q=q, country=country, status=status, sort=sort, limit=limit, offset=offset
        )


@router.get("/geo/destinations/{destination_id}")
async def get_geo_destination(destination_id: uuid.UUID, _: CanRead, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await geo.get_destination(conn, str(destination_id))


@router.patch("/geo/destinations/{destination_id}")
async def patch_geo_destination(
    body: DestinationPatch, destination_id: uuid.UUID, staff: CanAdminCatalog, pool: Pool
) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await geo.update_destination(conn, staff, str(destination_id), _patch(body))


@router.get("/geo/countries")
async def list_geo_countries(
    _: CanRead,
    pool: Pool,
    q: Annotated[str | None, Query(max_length=100)] = None,
    status: GeoStatus | None = None,
    area: Annotated[str | None, Query(max_length=60)] = None,
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await geo.list_countries(conn, q=q, status=status, area=area)


@router.get("/geo/countries/{iso}")
async def get_geo_country(iso: IsoCode, _: CanRead, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await geo.get_country(conn, iso)


@router.patch("/geo/countries/{iso}")
async def patch_geo_country(body: CountryPatch, iso: IsoCode, staff: CanAdminCatalog, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await geo.update_country(conn, staff, iso, _patch(body))


@router.get("/trip-plans")
async def list_trip_plans(
    staff: CanRead,
    pool: Pool,
    q: Annotated[str | None, Query(max_length=200)] = None,
    country: Annotated[str | None, Query(max_length=100)] = None,
    sellable: bool | None = None,
    trip_style: Annotated[str | None, Query(max_length=60)] = None,
    sort: Annotated[str | None, Query(max_length=40)] = None,
    limit: Annotated[int, Query(ge=1, le=trip_plans.MAX_PAGE)] = 25,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
    orphaned: Annotated[
        bool, Query(description="AA-986: only the plans whose tour left the catalog (q = exact source_id)")
    ] = False,
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        if orphaned:
            return await trip_plans.list_orphaned(conn, staff, q=q, limit=limit, offset=offset)
        return await trip_plans.list_plans(
            conn,
            staff,
            q=q,
            country=country,
            sellable=sellable,
            trip_style=trip_style,
            sort=sort,
            limit=limit,
            offset=offset,
        )


@router.get("/trip-plans/facets")
async def trip_plan_facets(_: CanRead, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await trip_plans.facets(conn)


@router.get("/trip-plans/{source_id}")
async def get_trip_plan(source_id: SourceId, staff: CanRead, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await trip_plans.get_plan(conn, staff, source_id)


@router.put("/trip-plans/{source_id}/commercial")
async def put_trip_plan_commercial(
    body: CommercialBody, source_id: SourceId, staff: CanWritePlans, pool: Pool
) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await trip_plans.save_commercial(conn, staff, source_id, body.data())


@router.get("/trip-plans/{source_id}/prices")
async def get_trip_plan_prices(source_id: SourceId, staff: CanReadPrices, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await trip_plan_prices.list_prices(conn, staff, source_id)


@router.put("/trip-plans/{source_id}/prices")
async def put_trip_plan_prices(
    body: PricesBody, source_id: SourceId, staff: CanWritePlans, pool: Pool
) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await trip_plan_prices.replace_prices(
            conn,
            staff,
            source_id,
            [r.model_dump() for r in body.items],
            below_floor_reason=body.below_floor_reason,
        )


@router.post("/trip-plans/{source_id}/trip-cases", status_code=201)
async def use_trip_plan(body: UseForTripCase, source_id: SourceId, staff: CanStartCase, pool: Pool) -> dict[str, Any]:
    if not staff.can("version.write"):
        raise HTTPException(status_code=403, detail="forbidden")
    data = body.model_dump()
    if data.get("customer_id"):
        data["customer_id"] = str(data["customer_id"])
    async with pool.acquire() as conn, conn.transaction():
        return await trip_plans.use_for_trip_case(conn, staff, source_id, data)


# ── Bookable components of a plan (bk-019, AA-972: what booking.b2b.v1 /products lists and a tp: line prices) ──


class PlanComponentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    day_no: int = Field(ge=1, le=366)
    hotel_room_type_id: uuid.UUID | None = None
    service_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _one_target(self) -> PlanComponentIn:
        if (self.hotel_room_type_id is None) == (self.service_id is None):
            raise ValueError("name exactly one of hotel_room_type_id, service_id")
        return self


class PlanComponentsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[PlanComponentIn] = Field(max_length=plan_components.MAX_COMPONENTS)


@router.get("/trip-plans/{source_id}/components")
async def get_trip_plan_components(source_id: SourceId, _: CanReadSupply, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await plan_components.list_components(conn, source_id)


@router.put("/trip-plans/{source_id}/components")
async def put_trip_plan_components(
    body: PlanComponentsBody, source_id: SourceId, staff: CanWritePlans, pool: Pool
) -> dict[str, Any]:
    items = [
        {
            "day_no": i.day_no,
            "hotel_room_type_id": str(i.hotel_room_type_id) if i.hotel_room_type_id else None,
            "service_id": str(i.service_id) if i.service_id else None,
        }
        for i in body.items
    ]
    async with pool.acquire() as conn, conn.transaction():
        return await plan_components.replace_components(conn, staff, source_id, items)


# ── Fixed departures of a plan (bk-020, AA-973: what booking.b2b.v1 /departures lists and holds take seats from) ──


class DepartureIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_date: date
    capacity: int = Field(ge=0, le=departures.MAX_CAPACITY)
    auto_confirm: bool = True
    status: Literal["scheduled", "guaranteed"] = "scheduled"


class DeparturePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capacity: int | None = Field(default=None, ge=0, le=departures.MAX_CAPACITY)
    status: Literal["scheduled", "guaranteed", "cancelled"] | None = None
    auto_confirm: bool | None = None


@router.get("/trip-plans/{source_id}/departures")
async def get_trip_plan_departures(
    source_id: SourceId,
    _: CanReadSupply,
    pool: Pool,
    start: Annotated[date | None, Query(alias="from")] = None,
    end: Annotated[date | None, Query(alias="to")] = None,
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await departures.list_departures(conn, source_id, start=start, end=end)


@router.post("/trip-plans/{source_id}/departures", status_code=201)
async def post_trip_plan_departure(
    body: DepartureIn, source_id: SourceId, staff: CanWritePlans, pool: Pool
) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await departures.create_departure(conn, staff, source_id, body.model_dump())


@router.patch("/departures/{departure_id}")
async def patch_departure(
    body: DeparturePatch, departure_id: Annotated[uuid.UUID, Path()], staff: CanWritePlans, pool: Pool
) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await departures.update_departure(conn, staff, str(departure_id), body.model_dump(exclude_none=True))
