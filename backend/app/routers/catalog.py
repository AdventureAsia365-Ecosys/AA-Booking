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
set) keep the plan's per-person price matrix (bk-032, AA-996; ``app/catalog/trip_plan_prices.py``). AA-1029 (bk-039):
``POST …/prices/recompute`` runs the plan's cost sheet now, ``GET …/prices/history?at=`` reads the dated price list,
``POST …/reference/review`` marks the CIS source price text reviewed (``trip_plan.write``).
``GET /booking/catalog/price-readiness`` (``pricing.read``, AA-1029) is the catalog-wide price readiness report: per
plan its status (priced, hand priced, or why the cost sheet cannot price it) with ``?status=&country=&q=`` filters, as
JSON or a staff CSV (``?format=csv``, fixed columns, never a net or a margin; ``app/catalog/price_readiness.py``).
AA-1091 price setup workbench (``app/catalog/price_workbench.py``, ``price_import.py``, ``component_suggest.py``):
``GET /booking/catalog/price-workbench`` (``pricing.read``: every plan with its checks and next step; filters
``status``, ``country``, ``q``, ``missing``, ``next_step``), ``POST …/price-workbench/actions`` (``trip_plan.write``: a
bulk action on up to 200 plans), ``GET /booking/catalog/price-import/template`` (``pricing.read``: the workbook, empty
or prefilled with the filtered plans) and ``POST /booking/catalog/price-import`` (``trip_plan.write``, ``?dry_run=true``
checks only), ``GET …/trip-plans/{source_id}/component-suggestions`` (``supply.read``: components suggested from the
CIS itinerary; nothing is applied).
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

import base64
import binascii
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app import b2c, catalog
from app.auth.staff import Staff, current_staff, get_pool, require
from app.b2b import allotments, departures, plan_components
from app.catalog import (
    component_suggest,
    cost_sheet,
    geo,
    price_history,
    price_import,
    price_readiness,
    price_workbench,
    trip_plan_prices,
    trip_plans,
)
from app.errors import Conflict, Invalid, NotFound
from app.pricing import engine
from app.routers.trip_cases import Currency

router = APIRouter(prefix="/booking/catalog", tags=["catalog"])

Pool = Annotated[asyncpg.Pool, Depends(get_pool)]
Me = Annotated[Staff, Depends(current_staff)]
CanRead = Annotated[Staff, Depends(require("catalog.read"))]
CanWritePlans = Annotated[Staff, Depends(require("trip_plan.write"))]
CanStartCase = Annotated[Staff, Depends(require("trip_case.write"))]
CanReadSupply = Annotated[Staff, Depends(require("supply.read"))]
CanReadPrices = Annotated[Staff, Depends(require("pricing.read"))]
CanAdminCatalog = Annotated[Staff, Depends(require("catalog.admin"))]
CanReadDesk = Annotated[Staff, Depends(require("b2b_desk.read"))]
CanWriteDesk = Annotated[Staff, Depends(require("b2b_desk.write"))]
IsoCode = Annotated[str, Path(pattern=r"^[A-Za-z]{2}$", description="ISO 3166-1 alpha-2 country code")]
GeoStatus = Literal["active", "inactive"]
ReadinessStatus = Literal[
    "priced", "hand_priced", "missing_rates", "missing_fx", "no_rule", "no_components", "inactive_component"
]
WorkbenchCheck = Literal["components", "rates", "fx", "rule", "price", "sellable", "departures"]
WorkbenchStep = Literal[
    "add_components", "fix_inactive", "load_rates", "approve_fx", "approve_rule", "recompute", "make_sellable", "done"
]
WorkbenchAction = Literal["make_sellable", "make_not_sellable", "apply_provisional", "clear_provisional", "recompute"]
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PRICE_IMPORT_B64_MAX = (price_import.MAX_BYTES * 4) // 3 + 4


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
    computed: bool = Field(
        default=False, description="a cost sheet row sent back as read; with an amount it becomes overridden"
    )
    # bk-047 (AA-1075, bk-15): per child / infant and the single supplement, per person (the cost sheet fills them;
    # staff may enter the sell amounts on a hand row). The nets only ride along on a cost sheet row sent back.
    child_amount: NetCost | None = None
    infant_amount: NetCost | None = None
    single_supplement_amount: NetCost | None = None
    child_net_amount: NetCost | None = None
    infant_net_amount: NetCost | None = None
    single_supplement_net_amount: NetCost | None = None


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
    """``trip_case`` (a case UUID or code) gets a new version from the plan. Without it the request asked for a new
    staff-made case, a B2C case retired with the B2C half (AA-1046): 410 ``b2b_only``."""

    model_config = ConfigDict(extra="forbid")

    trip_case: str | None = Field(default=None, min_length=1, max_length=64)
    notes: str | None = Field(default=None, max_length=10_000)


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
    visibility: Annotated[
        Literal["shown", "hidden"] | None, Query(description="hidden = CIS keeps the place off customer maps")
    ] = None,
    sort: Annotated[str | None, Query(max_length=40)] = None,
    limit: Annotated[int, Query(ge=1, le=geo.MAX_PAGE)] = 25,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await geo.list_destinations(
            conn,
            q=q,
            country=country,
            status=status,
            visibility=visibility,
            sort=sort,
            limit=limit,
            offset=offset,
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
    # AA-986: the orphaned-plans view of the catalog.
    orphaned: Annotated[
        bool, Query(description="Only the plans whose tour left the catalog (q = exact source_id)")
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


@router.get(
    "/price-readiness",
    responses={200: {"content": {"application/json": {}, "text/csv": {"schema": {"type": "string"}}}}},
)
async def price_readiness_report(
    _: CanReadPrices,
    pool: Pool,
    status: ReadinessStatus | None = None,
    country: Annotated[str | None, Query(max_length=100, description="country name as the catalog gives it")] = None,
    q: Annotated[str | None, Query(max_length=200, description="part of the title or the tour key")] = None,
    format: Annotated[Literal["json", "csv"], Query()] = "json",  # noqa: A002 — the query parameter's name
) -> Any:
    """AA-1029: which trip plans price and why the others do not (see ``app/catalog/price_readiness.py``). The
    summary counts every plan; ``items`` are the plans that match the filters."""
    day = cost_sheet.today()
    async with pool.acquire() as conn:
        report = await price_readiness.build(conn, on=day, status=status, country=country, q=q)
    if format == "csv":
        return Response(
            price_readiness.to_csv(report),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{price_readiness.filename(day)}"',
                "Cache-Control": "no-store",
            },
        )
    return report


# ── Price setup workbench (AA-1091) ──


class WorkbenchActionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: WorkbenchAction
    tour_keys: list[Annotated[str, Field(min_length=1, max_length=128)]] = Field(
        min_length=1, max_length=price_workbench.MAX_ACTION_PLANS
    )


class PriceImportUpload(BaseModel):
    """The filled workbook as base64 (the staff BFF forwards JSON bodies only, like rate sheets)."""

    model_config = ConfigDict(extra="forbid")

    data_base64: str = Field(min_length=4, max_length=PRICE_IMPORT_B64_MAX)
    filename: str | None = Field(default=None, max_length=200)


CountryQuery = Annotated[str | None, Query(max_length=100, description="country name as the catalog gives it")]
SearchQuery = Annotated[str | None, Query(max_length=200, description="part of the title or the tour key")]


@router.get("/price-workbench")
async def price_workbench_report(
    _: CanReadPrices,
    pool: Pool,
    status: ReadinessStatus | None = None,
    country: CountryQuery = None,
    q: SearchQuery = None,
    missing: WorkbenchCheck | None = None,
    next_step: WorkbenchStep | None = None,
) -> dict[str, Any]:
    """AA-1091: every trip plan with its checks, its next step, its reference and provisional price."""
    async with pool.acquire() as conn:
        return await price_workbench.build(
            conn, on=cost_sheet.today(), status=status, country=country, q=q, missing_check=missing, step=next_step
        )


@router.post("/price-workbench/actions")
async def price_workbench_action(body: WorkbenchActionBody, staff: CanWritePlans, pool: Pool) -> dict[str, Any]:
    """AA-1091: one bulk action on many plans; a plan that does not qualify is skipped with its reason."""
    async with pool.acquire() as conn, conn.transaction():
        return await price_workbench.apply_action(conn, staff, body.action, body.tour_keys, on=cost_sheet.today())


@router.get("/price-import/template", responses={200: {"content": {XLSX: {}}}})
async def price_import_template(
    _: CanReadPrices,
    pool: Pool,
    status: ReadinessStatus | None = None,
    country: CountryQuery = None,
    q: SearchQuery = None,
    missing: WorkbenchCheck | None = None,
    next_step: WorkbenchStep | None = None,
    blank: bool = False,
) -> Response:
    """AA-1091: the price setup workbook, prefilled with the plans the workbench filters select (``blank`` = empty)."""
    day = cost_sheet.today()
    if blank:
        content = price_import.template()
    else:
        async with pool.acquire() as conn:
            report = await price_workbench.build(
                conn, on=day, status=status, country=country, q=q, missing_check=missing, step=next_step
            )
            content = await price_import.prefilled(conn, report["items"], today=day)
    return Response(
        content=content,
        media_type=XLSX,
        headers={
            "Content-Disposition": f'attachment; filename="{price_import.filename(day)}"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/price-import")
async def post_price_import(
    body: PriceImportUpload,
    staff: CanWritePlans,
    pool: Pool,
    dry_run: Annotated[bool, Query(description="check and preview only; nothing is written")] = False,
) -> dict[str, Any]:
    """AA-1091: import a filled price setup workbook (422 ``price_import_invalid`` lists every problem)."""
    try:
        data = base64.b64decode(body.data_base64, validate=True)
    except (binascii.Error, ValueError):
        raise Invalid("rate_sheet_not_xlsx", "the upload is not valid base64") from None
    if len(data) > price_import.MAX_BYTES:
        raise Invalid("rate_sheet_too_large", "a workbook is at most 2 MB")
    async with pool.acquire() as conn, conn.transaction():
        return await price_import.import_workbook(conn, staff, data, dry_run=dry_run)


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


class AgeBandsBody(BaseModel):
    """bk-047 (AA-1075, bk-15): the oldest ages at the child and infant prices (None = the default)."""

    model_config = ConfigDict(extra="forbid")

    child_age_max: int | None = Field(default=None, ge=2, le=17)
    infant_age_max: int | None = Field(default=None, ge=0, le=4)


@router.put("/trip-plans/{source_id}/age-bands")
async def put_trip_plan_age_bands(
    body: AgeBandsBody, source_id: SourceId, staff: CanWritePlans, pool: Pool
) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await trip_plans.save_age_bands(
            conn, staff, source_id, child_age_max=body.child_age_max, infant_age_max=body.infant_age_max
        )


@router.get("/trip-plans/{source_id}/prices")
async def get_trip_plan_prices(source_id: SourceId, staff: CanReadPrices, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await trip_plan_prices.list_prices(conn, staff, source_id)


@router.put("/trip-plans/{source_id}/prices", responses={409: {"description": "prices change through versions"}})
async def put_trip_plan_prices(
    body: PricesBody, source_id: SourceId, staff: CanWritePlans, pool: Pool
) -> dict[str, Any]:
    """Retired by AA-1091 (bk-053, root ADR 0005b d1 / d2): a hand price is never overwritten. The rows go into a
    draft price list version (``POST …/price-versions``, ``PUT /booking/catalog/price-versions/{id}``) that a second
    person approves. Always 409 ``price_versions``."""
    raise Conflict(
        "price_versions",
        "prices change through price versions: make a draft, submit it, a second person approves it",
        versions_url=f"/booking/catalog/trip-plans/{source_id}/price-versions",
    )


@router.post("/trip-plans/{source_id}/prices/recompute")
async def recompute_trip_plan_prices(source_id: SourceId, staff: CanWritePlans, pool: Pool) -> dict[str, Any]:
    """AA-1029: run the cost sheet of this plan now (the same as the bk_price_recompute job does for every plan)."""
    async with pool.acquire() as conn:
        plan_id = await conn.fetchval(
            "SELECT id::text FROM booking.trip_plans WHERE source_system = 'cis' AND source_id = $1", source_id
        )
    if plan_id is None:
        raise NotFound("trip_plan_not_found", "this tour has no trip plan yet")
    day = cost_sheet.today()
    async with pool.acquire() as conn:
        pricing = await cost_sheet.load_pricing(conn, day)
    async with pool.acquire() as conn, conn.transaction():
        result = await cost_sheet.recompute_plan(conn, plan_id, pricing)
    async with pool.acquire() as conn:
        return {"recompute": result, **(await trip_plan_prices.list_prices(conn, staff, source_id))}


@router.get("/trip-plans/{source_id}/prices/history")
async def trip_plan_price_history(
    source_id: SourceId,
    staff: CanReadPrices,
    pool: Pool,
    at: Annotated[datetime | None, Query(description="the price list in force at this time (default now)")] = None,
) -> dict[str, Any]:
    """AA-1029 (bk-039): the plan's price list as it was in force at ``at``, from the dated history."""
    when = at or datetime.now(UTC)
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    async with pool.acquire() as conn:
        plan_id = await conn.fetchval(
            "SELECT id::text FROM booking.trip_plans WHERE source_system = 'cis' AND source_id = $1", source_id
        )
        rows = await price_history.as_of(conn, plan_id, when) if plan_id else []
    return {"source_id": source_id, "at": when, "items": rows}


@router.post("/trip-plans/{source_id}/reference/review")
async def review_trip_plan_reference(source_id: SourceId, staff: CanWritePlans, pool: Pool) -> dict[str, Any]:
    """AA-1029 (ADR 0005 d7): mark the CIS source price text of this plan as reviewed (reference only)."""
    async with pool.acquire() as conn, conn.transaction():
        await trip_plan_prices.review_reference(conn, staff, source_id)
        return await trip_plan_prices.list_prices(conn, staff, source_id)


@router.post("/trip-plans/{source_id}/trip-cases", status_code=201, responses=b2c.GONE_RESPONSE)
async def use_trip_plan(body: UseForTripCase, source_id: SourceId, staff: CanStartCase, pool: Pool) -> dict[str, Any]:
    if not staff.can("version.write"):
        raise HTTPException(status_code=403, detail="forbidden")
    if body.trip_case is None:
        b2c.refuse_new_case()  # AA-1046: a new case here was always for a traveller customer, a B2C case
    async with pool.acquire() as conn, conn.transaction():
        await b2c.refuse_b2c_case_write(conn, body.trip_case)  # AA-1037: a b2c case is read-only
        return await trip_plans.use_for_trip_case(conn, staff, source_id, body.model_dump())


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


@router.get("/trip-plans/{source_id}/component-suggestions")
async def get_component_suggestions(source_id: SourceId, _: CanReadSupply, pool: Pool) -> dict[str, Any]:
    """AA-1091: hotels and services suggested per day from the CIS itinerary (read only; save with PUT components)."""
    async with pool.acquire() as conn:
        return await component_suggest.build(conn, source_id)


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


MinToOperate = Annotated[int, Field(ge=0, le=departures.MAX_CAPACITY)]
CutoffDays = Annotated[int, Field(ge=0, le=departures.MAX_CUTOFF_DAYS)]
DecisionDays = Annotated[int, Field(ge=0, le=departures.MAX_DECISION_DAYS)]


class DepartureIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_date: date
    capacity: int = Field(ge=0, le=departures.MAX_CAPACITY)
    auto_confirm: bool = True
    status: Literal["scheduled", "guaranteed"] = "scheduled"
    # AA-1073 (bk-046): 0 = no minimum; sales close on start - cutoff; the go / cancel decision on start - decision.
    min_to_operate: MinToOperate = 0
    sales_cutoff_days: CutoffDays = 0
    decision_days_before: DecisionDays = departures.DEFAULT_DECISION_DAYS


class DeparturePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capacity: int | None = Field(default=None, ge=0, le=departures.MAX_CAPACITY)
    status: Literal["scheduled", "guaranteed", "cancelled"] | None = None
    auto_confirm: bool | None = None
    min_to_operate: MinToOperate | None = None
    sales_cutoff_days: CutoffDays | None = None
    decision_days_before: DecisionDays | None = None


class WeeklyPattern(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["weekly"]
    weekdays: list[Annotated[int, Field(ge=1, le=7)]] = Field(min_length=1, max_length=7)


class MonthlyPattern(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["monthly"]
    day: int = Field(ge=1, le=31)


class DepartureGenerateIn(BaseModel):
    """AA-1073 (bk-02): departures of a weekly or monthly pattern; dates the plan already has are skipped."""

    model_config = ConfigDict(extra="forbid")

    date_from: date
    date_to: date
    pattern: Annotated[WeeklyPattern | MonthlyPattern, Field(discriminator="kind")]
    capacity: int = Field(ge=0, le=departures.MAX_CAPACITY)
    min_to_operate: MinToOperate = 0
    sales_cutoff_days: CutoffDays = 0
    decision_days_before: DecisionDays = departures.DEFAULT_DECISION_DAYS
    auto_confirm: bool = True
    dry_run: bool = False


class AllotmentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    partner_id: uuid.UUID
    seats: int = Field(ge=1, le=allotments.MAX_SEATS)
    release_days_before: int = Field(ge=0, le=allotments.MAX_RELEASE_DAYS)


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


@router.post("/trip-plans/{source_id}/departures:generate")
async def post_generate_departures(
    body: DepartureGenerateIn, source_id: SourceId, staff: CanWritePlans, pool: Pool
) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await departures.generate_departures(conn, staff, source_id, body.model_dump())


@router.patch("/departures/{departure_id}")
async def patch_departure(
    body: DeparturePatch, departure_id: Annotated[uuid.UUID, Path()], staff: CanWritePlans, pool: Pool
) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await departures.update_departure(conn, staff, str(departure_id), body.model_dump(exclude_none=True))


# ── Agency allotments on a departure (bk-046, AA-1073: seats kept for one agency until a release date) ──


@router.get("/departures/{departure_id}/allotments")
async def get_departure_allotments(
    departure_id: Annotated[uuid.UUID, Path()], _: CanReadDesk, pool: Pool
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await allotments.list_for_departure(conn, str(departure_id))


@router.post("/departures/{departure_id}/allotments", status_code=201)
async def post_departure_allotment(
    body: AllotmentIn, departure_id: Annotated[uuid.UUID, Path()], staff: CanWriteDesk, pool: Pool
) -> dict[str, Any]:
    data = body.model_dump() | {"partner_id": str(body.partner_id)}
    async with pool.acquire() as conn, conn.transaction():
        return await allotments.create(conn, staff, str(departure_id), data)


@router.post("/allotments/{allotment_id}/approve")
async def post_allotment_approve(
    allotment_id: Annotated[uuid.UUID, Path()], staff: CanWriteDesk, pool: Pool
) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await allotments.approve(conn, staff, str(allotment_id))


@router.post("/allotments/{allotment_id}/end")
async def post_allotment_end(
    allotment_id: Annotated[uuid.UUID, Path()], staff: CanWriteDesk, pool: Pool
) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await allotments.end(conn, staff, str(allotment_id))
