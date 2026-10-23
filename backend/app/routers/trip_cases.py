"""Staff trip case and proposal version endpoints. Every route needs a CIS staff JWT + an active Booking profile.

A trip case is addressed by its UUID or its code (``TC-000123``).
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Literal

import asyncpg
from fastapi import APIRouter, Depends, Path, Query, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.auth.staff import Staff, get_pool, require
from app.pricing import engine
from app.trip_cases import service, versions

router = APIRouter(prefix="/booking", tags=["trip-cases"])

CaseRef = Annotated[str, Path(min_length=1, max_length=64, description="Trip case UUID or code (TC-000123)")]
VersionNumber = Annotated[int, Path(ge=1, le=32767)]
Pool = Annotated[asyncpg.Pool, Depends(get_pool)]
CanRead = Annotated[Staff, Depends(require("trip_case.read"))]
CanWrite = Annotated[Staff, Depends(require("trip_case.write"))]
CanAssign = Annotated[Staff, Depends(require("trip_case.assign"))]
CanWriteVersions = Annotated[Staff, Depends(require("version.write"))]
# Applying a price writes the version (version.write: admin, sales); both roles also hold pricing.read.
CanPriceVersions = CanWriteVersions
Currency = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
CountryCode = Annotated[str, Field(pattern=r"^[A-Za-z]{2}$")]
Money = Annotated[Decimal, Field(ge=0, max_digits=16, decimal_places=2, allow_inf_nan=False)]
PositiveMoney = Annotated[Decimal, Field(gt=0, max_digits=16, decimal_places=2, allow_inf_nan=False)]
# A plain shape check (no email-validator dependency); deliverability is not Booking's to judge.
Email = Annotated[str, Field(max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]


class CasePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pax: int | None = Field(default=None, ge=1, le=999)
    pax_adults: int | None = Field(default=None, ge=1, le=999)
    pax_children: int | None = Field(default=None, ge=0, le=998)
    start_date: date | None = None
    currency: Currency | None = None
    trip_style: str | None = Field(default=None, max_length=60)
    notes: str | None = Field(default=None, max_length=10_000)
    countries: list[CountryCode] | None = Field(default=None, max_length=service.MAX_COUNTRIES)


class NewCustomer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    email: Email | None = None
    phone: str | None = Field(default=None, min_length=3, max_length=40)
    address: str | None = Field(default=None, max_length=500)
    contact_consent: bool = False

    @model_validator(mode="after")
    def _contact(self) -> NewCustomer:
        if not self.email and not self.phone:
            raise ValueError("email or phone is required")
        return self


class CaseCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

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
    def _one_customer(self) -> CaseCreate:
        if (self.customer_id is None) == (self.new_customer is None):
            raise ValueError("send exactly one of customer_id or new_customer")
        return self


class StatusChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to_status: str = Field(min_length=1, max_length=40)
    reason: str | None = Field(default=None, max_length=2_000)


class AssignBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cis_user_id: str | None = Field(min_length=1, max_length=128, description="null closes the role")
    role: str = Field(default="sales", max_length=40)


class VersionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    notes: str | None = Field(default=None, max_length=10_000)


class VersionPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=200)
    notes: str | None = Field(default=None, max_length=10_000)


class CostIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    net_amount: Money = Field(description="supplier net cost in net_currency; 0 for an included item")
    net_currency: Currency
    supplier_note: str | None = Field(default=None, max_length=200)
    supplier_rate_id: uuid.UUID | None = Field(default=None, description="the published rate this line comes from")


class ItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID | None = Field(default=None, description="keep an existing item (and what references it)")
    title: str = Field(min_length=1, max_length=500)
    day_from: int = Field(ge=1, le=366)
    day_to: int = Field(ge=1, le=366)
    source_tour_id: str | None = Field(default=None, min_length=1, max_length=128)
    notes: str | None = Field(default=None, max_length=5_000)
    costs: list[CostIn] | None = Field(
        default=None,
        max_length=versions.MAX_COSTS_PER_ITEM,
        description="replaces the item's net cost lines; omit to keep them (or to take the rate in force)",
    )
    hotel_room_type_id: uuid.UUID | None = Field(default=None, description="the hotel room type this item books")
    service_id: uuid.UUID | None = Field(default=None, description="the service this item books")

    @model_validator(mode="after")
    def _days_in_order(self) -> ItemIn:
        if self.day_to < self.day_from:
            raise ValueError("day_to must be >= day_from")
        if self.hotel_room_type_id and self.service_id:
            raise ValueError("an item books one room type or one service, not both")
        return self


class ItemsPut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ItemIn] = Field(max_length=versions.MAX_ITEMS)


class PriceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel: engine.Channel
    currency: Currency = engine.BASE_CURRENCY
    peak: bool = False
    override_total: PositiveMoney | None = None
    override_reason: str | None = Field(default=None, max_length=500, description="required when below the floor")


class SendBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to: Email | None = Field(default=None, description="must be the customer's e-mail (read-only in the UI)")
    subject: str = Field(min_length=1, max_length=200)
    message: str = Field(min_length=1, max_length=10_000)
    valid_until: date | None = Field(default=None, description="default: today + quote_validity_days (7-30)")


class ApproveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(default=None, max_length=2_000)


def _statuses(raw: list[str] | None) -> list[str] | None:
    if not raw:
        return None
    return [s.strip() for value in raw for s in value.split(",") if s.strip()] or None


def _assignee(raw: str | None, staff: Staff) -> str | None:
    return staff.cis_user_id if raw == "me" else raw


@router.get("/trip-cases")
async def list_trip_cases(
    staff: CanRead,
    pool: Pool,
    status: Annotated[list[str] | None, Query(description="repeat or comma-separate")] = None,
    assignee: Annotated[str | None, Query(max_length=128, description="CIS user id, 'me' or 'none'")] = None,
    q: Annotated[str | None, Query(max_length=200, description="case code, customer code, name or e-mail")] = None,
    source: Annotated[str | None, Query(max_length=20, description="tripplanner | manual | cis | crm")] = None,
    customer_id: Annotated[uuid.UUID | None, Query()] = None,
    created_from: Annotated[date | None, Query()] = None,
    created_to: Annotated[date | None, Query(description="inclusive")] = None,
    sort: Annotated[str | None, Query(max_length=20, description="created_at | updated_at | start_date, -desc")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await service.list_cases(
            conn,
            statuses=_statuses(status),
            assignee=_assignee(assignee, staff),
            q=q,
            source=source,
            customer_id=str(customer_id) if customer_id else None,
            created_from=created_from,
            created_to=created_to,
            sort=sort,
            limit=limit,
            offset=offset,
        )


@router.post("/trip-cases", status_code=201)
async def create_trip_case(body: CaseCreate, staff: CanWrite, pool: Pool) -> dict[str, Any]:
    data = body.model_dump()
    data["customer_id"] = str(body.customer_id) if body.customer_id else None
    async with pool.acquire() as conn, conn.transaction():
        return await service.create_case(conn, staff, data)


@router.get("/trip-cases/summary")
async def trip_case_summary(staff: CanRead, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await service.summary(conn, staff)


@router.get("/trip-cases/{case_ref}")
async def get_trip_case(case_ref: CaseRef, _: CanRead, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await service.get_case(conn, case_ref)


@router.get("/trip-cases/{case_ref}/activity")
async def trip_case_activity(
    case_ref: CaseRef,
    _: CanRead,
    pool: Pool,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    before: Annotated[str | None, Query(max_length=100, description="next_before of the previous page")] = None,
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await service.activity(conn, case_ref, before=before, limit=limit)


@router.patch("/trip-cases/{case_ref}")
async def patch_trip_case(body: CasePatch, case_ref: CaseRef, staff: CanWrite, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await service.update_case(conn, staff, case_ref, body.model_dump(exclude_unset=True))


@router.post("/trip-cases/{case_ref}/status")
async def change_trip_case_status(body: StatusChange, case_ref: CaseRef, staff: CanWrite, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await service.change_status(conn, staff, case_ref, body.to_status, body.reason)


@router.post("/trip-cases/{case_ref}/assign")
async def assign_trip_case(body: AssignBody, case_ref: CaseRef, staff: CanAssign, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await service.assign(conn, staff, case_ref, body.cis_user_id, body.role)


@router.post("/trip-cases/{case_ref}/versions", status_code=201)
async def create_trip_case_version(
    case_ref: CaseRef,
    staff: CanWriteVersions,
    pool: Pool,
    body: VersionCreate | None = None,
) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await versions.create_version(conn, staff, case_ref, body.notes if body else None)


@router.get("/trip-cases/{case_ref}/versions/{version_number}")
async def get_version(case_ref: CaseRef, version_number: VersionNumber, _: CanRead, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await versions.get_version(conn, case_ref, version_number)


@router.patch("/trip-cases/{case_ref}/versions/{version_number}")
async def patch_version(
    body: VersionPatch, case_ref: CaseRef, version_number: VersionNumber, staff: CanWriteVersions, pool: Pool
) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await versions.update_version(conn, staff, case_ref, version_number, body.model_dump(exclude_unset=True))


@router.delete("/trip-cases/{case_ref}/versions/{version_number}", status_code=204)
async def delete_version(
    case_ref: CaseRef, version_number: VersionNumber, staff: CanWriteVersions, pool: Pool
) -> Response:
    async with pool.acquire() as conn, conn.transaction():
        await versions.delete_version(conn, staff, case_ref, version_number)
    return Response(status_code=204)


@router.get("/trip-cases/{case_ref}/versions/{version_number}/items")
async def get_version_items(case_ref: CaseRef, version_number: VersionNumber, _: CanRead, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await versions.get_items(conn, case_ref, version_number)


@router.put("/trip-cases/{case_ref}/versions/{version_number}/items")
async def put_version_items(
    body: ItemsPut, case_ref: CaseRef, version_number: VersionNumber, staff: CanWriteVersions, pool: Pool
) -> dict[str, Any]:
    items = [
        versions.Item(
            title=item.title,
            day_from=item.day_from,
            day_to=item.day_to,
            source_tour_id=item.source_tour_id,
            notes=item.notes,
            id=str(item.id) if item.id else None,
            costs=None
            if item.costs is None
            else tuple(
                versions.Cost(
                    c.net_amount,
                    c.net_currency,
                    (c.supplier_note or "").strip() or None,
                    str(c.supplier_rate_id) if c.supplier_rate_id else None,
                )
                for c in item.costs
            ),
            hotel_room_type_id=str(item.hotel_room_type_id) if item.hotel_room_type_id else None,
            service_id=str(item.service_id) if item.service_id else None,
        )
        for item in body.items
    ]
    async with pool.acquire() as conn, conn.transaction():
        return await versions.replace_items(conn, staff, case_ref, version_number, items)


@router.post("/trip-cases/{case_ref}/versions/{version_number}/price")
async def price_version(
    body: PriceBody, case_ref: CaseRef, version_number: VersionNumber, staff: CanPriceVersions, pool: Pool
) -> dict[str, Any]:
    """Price a draft from its stored net cost lines (same engine as quote-preview) and save the total."""
    async with pool.acquire() as conn, conn.transaction():
        return await versions.price_version(
            conn,
            staff,
            case_ref,
            version_number,
            channel=body.channel,
            currency=body.currency,
            peak=body.peak,
            override_total=body.override_total,
            override_reason=body.override_reason,
        )


@router.post("/trip-cases/{case_ref}/versions/{version_number}/send")
async def send_version(
    body: SendBody, case_ref: CaseRef, version_number: VersionNumber, staff: CanWriteVersions, pool: Pool
) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await versions.send_version(
            conn,
            staff,
            case_ref,
            version_number,
            to=str(body.to) if body.to else None,
            subject=body.subject,
            message=body.message,
            valid_until=body.valid_until,
        )


@router.post("/trip-cases/{case_ref}/versions/{version_number}/approve")
async def approve_version(
    case_ref: CaseRef,
    version_number: VersionNumber,
    staff: CanWriteVersions,
    pool: Pool,
    body: ApproveBody | None = None,
) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await versions.approve_version(conn, staff, case_ref, version_number, body.note if body else None)


@router.get("/versions")
async def list_versions(
    staff: CanRead,
    pool: Pool,
    q: Annotated[str | None, Query(max_length=200, description="case code, customer, version title")] = None,
    badge: Annotated[str | None, Query(max_length=20, description="draft | sent | approved | superseded")] = None,
    assignee: Annotated[str | None, Query(max_length=128, description="sales owner: id, 'me' or 'none'")] = None,
    updated_from: Annotated[date | None, Query()] = None,
    updated_to: Annotated[date | None, Query(description="inclusive")] = None,
    sort: Annotated[str | None, Query(max_length=20, description="updated_at | created_at | version_number")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await versions.list_versions(
            conn,
            q=q,
            badge=badge,
            assignee=_assignee(assignee, staff),
            updated_from=updated_from,
            updated_to=updated_to,
            sort=sort,
            limit=limit,
            offset=offset,
        )
