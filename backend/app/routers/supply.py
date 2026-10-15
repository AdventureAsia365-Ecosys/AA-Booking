"""Hotels, room types, services and supplier rates for staff (bk-010, AA-823; ADR 0004a d1, UI spec §6.3).

Permissions: ``supply.read`` (admin, contracting, sales) for every GET; ``supply.write`` (admin, contracting) for every
write. Facts only: there is no customer-facing description or photo field anywhere here (ADR 0004a d2, Q17).
"""

from __future__ import annotations

from datetime import date, time
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Body, Depends, Path, Query, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.auth.staff import Staff, get_pool, require
from app.suppliers import service as suppliers
from app.supply import rates, records
from app.supply.records import HOTEL, SERVICE

router = APIRouter(prefix="/booking", tags=["supply"])

HotelRef = Annotated[str, Path(min_length=1, max_length=64, description="Hotel UUID or code (HTL-000123)")]
ServiceRef = Annotated[str, Path(min_length=1, max_length=64, description="Service UUID or code (SVC-000123)")]
RecordId = Annotated[str, Path(min_length=1, max_length=64)]
Pool = Annotated[asyncpg.Pool, Depends(get_pool)]
CanRead = Annotated[Staff, Depends(require("supply.read"))]
CanWrite = Annotated[Staff, Depends(require("supply.write"))]

Country = Annotated[str, Field(pattern=r"^\s*([A-Za-z]{2})?\s*$")]
ShortText = Annotated[str, Field(max_length=120)]
ServiceType = Literal["activity", "transfer", "guide", "entrance", "meal"]
ServiceUnit = Literal["person", "group", "vehicle", "day"]
Money = Annotated[Decimal, Field(ge=0, max_digits=16, decimal_places=2)]
Pax = Annotated[int, Field(ge=1, le=999)]


class Link(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str | None = Field(default=None, max_length=80)
    url: str = Field(max_length=500, pattern=r"^\s*https?://\S+\s*$")


class HotelFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=200)
    hotel_style: str | None = Field(default=None, max_length=60)
    star_rating: int | None = Field(default=None, ge=1, le=5)
    address: str | None = Field(default=None, max_length=500)
    destination_id: UUID | None = None
    country_code: Country | None = None
    latitude: Decimal | None = Field(default=None, ge=-90, le=90, max_digits=9, decimal_places=6)
    longitude: Decimal | None = Field(default=None, ge=-180, le=180, max_digits=9, decimal_places=6)
    check_in_time: time | None = None
    check_out_time: time | None = None
    amenities: list[ShortText] | None = Field(default=None, max_length=50)
    links: list[Link] | None = Field(default=None, max_length=10)
    internal_notes: str | None = Field(default=None, max_length=5000)


class NewHotel(HotelFields):
    supplier_id: str = Field(min_length=1, max_length=64, description="supplier UUID or code (SUP-000123)")
    name: str = Field(min_length=1, max_length=200)


class ServiceFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service_type: ServiceType | None = None
    title: str | None = Field(default=None, max_length=200)
    unit: ServiceUnit | None = None
    destination_id: UUID | None = None
    country_code: Country | None = None
    source_id: str | None = Field(default=None, max_length=120, description="optional CIS catalog reference")
    max_group_size: int | None = Field(default=None, ge=1, le=999)
    duration_minutes: int | None = Field(default=None, ge=1, le=43200)
    inclusions: list[ShortText] | None = Field(default=None, max_length=50)
    internal_notes: str | None = Field(default=None, max_length=5000)


class NewService(ServiceFields):
    supplier_id: str = Field(min_length=1, max_length=64, description="supplier UUID or code (SUP-000123)")
    service_type: ServiceType
    title: str = Field(min_length=1, max_length=200)


class StatusNote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=500)


class RoomTypeFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=120)
    max_occupancy: int | None = Field(default=None, ge=1, le=20)
    bed_types: list[Annotated[str, Field(max_length=60)]] | None = Field(default=None, max_length=10)
    internal_notes: str | None = Field(default=None, max_length=2000)


class NewRoomType(RoomTypeFields):
    name: str = Field(min_length=1, max_length=120)


class RoomTypePatch(RoomTypeFields):
    status: Literal["active", "inactive"] | None = None


class RateFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    season: str | None = Field(default=None, max_length=80)
    valid_from: date | None = None
    valid_to: date | None = None
    meal_plan: Literal["RO", "BB", "HB", "FB", "AI"] | None = None
    pax_min: Pax | None = None
    pax_max: Pax | None = None
    net_amount: Money | None = None
    notes: str | None = Field(default=None, max_length=2000)


class NewRate(RateFields):
    hotel_room_type_id: UUID | None = None
    service_id: UUID | None = None
    net_amount: Money

    @model_validator(mode="after")
    def _one_target(self) -> NewRate:
        if (self.hotel_room_type_id is None) == (self.service_id is None):
            raise ValueError("give exactly one of hotel_room_type_id or service_id")
        return self


def _payload(body: BaseModel) -> dict[str, Any]:
    data = body.model_dump(exclude_unset=True)
    for key in ("destination_id", "hotel_room_type_id", "service_id"):
        if data.get(key) is not None:
            data[key] = str(data[key])
    return data


# ── Hotels ───────────────────────────────────────────────────────────────────


@router.get("/hotels")
async def list_hotels(
    _: CanRead,
    pool: Pool,
    q: Annotated[str | None, Query(max_length=200, description="name, code, address or supplier")] = None,
    status: Annotated[str | None, Query(max_length=20)] = None,
    supplier: Annotated[str | None, Query(max_length=64, description="supplier UUID or code")] = None,
    country: Annotated[str | None, Query(min_length=2, max_length=2)] = None,
    destination_id: Annotated[str | None, Query(max_length=64)] = None,
    star_rating: Annotated[int | None, Query(ge=1, le=5)] = None,
    sort: Annotated[str | None, Query(max_length=30)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await records.list_records(
            conn,
            HOTEL,
            q=q,
            status=status,
            supplier=supplier,
            country=country,
            destination_id=destination_id,
            star_rating=star_rating,
            sort=sort,
            limit=limit,
            offset=offset,
        )


@router.post("/hotels", status_code=201)
async def create_hotel(body: NewHotel, staff: CanWrite, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await records.create_record(conn, HOTEL, staff, _payload(body))


@router.get("/hotels/{hotel_ref}")
async def get_hotel(hotel_ref: HotelRef, _: CanRead, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await records.get_record(conn, HOTEL, hotel_ref)


@router.patch("/hotels/{hotel_ref}")
async def patch_hotel(body: HotelFields, hotel_ref: HotelRef, staff: CanWrite, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await records.update_record(conn, HOTEL, staff, hotel_ref, _payload(body))


@router.post("/hotels/{hotel_ref}/deactivate")
async def deactivate_hotel(
    hotel_ref: HotelRef, staff: CanWrite, pool: Pool, body: Annotated[StatusNote | None, Body()] = None
) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await records.set_status(conn, HOTEL, staff, hotel_ref, "inactive", body.reason if body else None)


@router.post("/hotels/{hotel_ref}/activate")
async def activate_hotel(
    hotel_ref: HotelRef, staff: CanWrite, pool: Pool, body: Annotated[StatusNote | None, Body()] = None
) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await records.set_status(conn, HOTEL, staff, hotel_ref, "active", body.reason if body else None)


@router.post("/hotels/{hotel_ref}/room-types", status_code=201)
async def create_room_type(body: NewRoomType, hotel_ref: HotelRef, staff: CanWrite, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await records.create_room_type(conn, staff, hotel_ref, _payload(body))


@router.patch("/room-types/{room_type_id}")
async def patch_room_type(body: RoomTypePatch, room_type_id: RecordId, staff: CanWrite, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await records.update_room_type(conn, staff, room_type_id, _payload(body))


@router.get("/hotels/{hotel_ref}/rates")
async def hotel_rates(hotel_ref: HotelRef, _: CanRead, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        hotel = await records.resolve(conn, HOTEL, hotel_ref)
        return await rates.list_rates(conn, hotel_id=hotel["id"])


# ── Services ─────────────────────────────────────────────────────────────────


@router.get("/services")
async def list_services(
    _: CanRead,
    pool: Pool,
    q: Annotated[str | None, Query(max_length=200, description="title, code or supplier")] = None,
    status: Annotated[str | None, Query(max_length=20)] = None,
    type: Annotated[
        str | None, Query(max_length=20, description="activity | transfer | guide | entrance | meal")
    ] = None,  # noqa: A002
    supplier: Annotated[str | None, Query(max_length=64, description="supplier UUID or code")] = None,
    country: Annotated[str | None, Query(min_length=2, max_length=2)] = None,
    destination_id: Annotated[str | None, Query(max_length=64)] = None,
    sort: Annotated[str | None, Query(max_length=30)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await records.list_records(
            conn,
            SERVICE,
            q=q,
            status=status,
            supplier=supplier,
            country=country,
            destination_id=destination_id,
            service_type=type,
            sort=sort,
            limit=limit,
            offset=offset,
        )


@router.post("/services", status_code=201)
async def create_service(body: NewService, staff: CanWrite, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await records.create_record(conn, SERVICE, staff, _payload(body))


@router.get("/services/{service_ref}")
async def get_service(service_ref: ServiceRef, _: CanRead, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await records.get_record(conn, SERVICE, service_ref)


@router.patch("/services/{service_ref}")
async def patch_service(body: ServiceFields, service_ref: ServiceRef, staff: CanWrite, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await records.update_record(conn, SERVICE, staff, service_ref, _payload(body))


@router.post("/services/{service_ref}/deactivate")
async def deactivate_service(
    service_ref: ServiceRef, staff: CanWrite, pool: Pool, body: Annotated[StatusNote | None, Body()] = None
) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await records.set_status(conn, SERVICE, staff, service_ref, "inactive", body.reason if body else None)


@router.post("/services/{service_ref}/activate")
async def activate_service(
    service_ref: ServiceRef, staff: CanWrite, pool: Pool, body: Annotated[StatusNote | None, Body()] = None
) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await records.set_status(conn, SERVICE, staff, service_ref, "active", body.reason if body else None)


@router.get("/services/{service_ref}/rates")
async def service_rates(service_ref: ServiceRef, _: CanRead, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        service = await records.resolve(conn, SERVICE, service_ref)
        return await rates.list_rates(conn, service_id=service["id"])


# ── Rates (under a supplier contract) ────────────────────────────────────────


@router.get("/contracts/{contract_id}/rates")
async def contract_rates(contract_id: RecordId, _: CanRead, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        contract = await suppliers.get_contract(conn, contract_id)
        return await rates.list_rates(conn, contract_id=contract["id"])


@router.post("/contracts/{contract_id}/rates", status_code=201)
async def create_rate(body: NewRate, contract_id: RecordId, staff: CanWrite, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await rates.create_rate(conn, staff, contract_id, _payload(body))


@router.patch("/rates/{rate_id}")
async def patch_rate(body: RateFields, rate_id: RecordId, staff: CanWrite, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await rates.update_rate(conn, staff, rate_id, _payload(body))


@router.delete("/rates/{rate_id}", status_code=204)
async def delete_rate(rate_id: RecordId, staff: CanWrite, pool: Pool) -> Response:
    async with pool.acquire() as conn, conn.transaction():
        await rates.delete_rate(conn, staff, rate_id)
    return Response(status_code=204)


# ── Version item helpers ─────────────────────────────────────────────────────


@router.get("/supply/options")
async def supply_options(
    _: CanRead,
    pool: Pool,
    q: Annotated[str | None, Query(max_length=200)] = None,
    kind: Annotated[str | None, Query(max_length=20, description="room_type | service")] = None,
    supplier_id: Annotated[str | None, Query(max_length=64, description="only this supplier's records")] = None,
    limit: Annotated[int, Query(ge=1, le=records.MAX_OPTIONS)] = 20,
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await records.search_options(conn, q=q, kind=kind, supplier_id=supplier_id, limit=limit)


@router.get("/supply/rate-in-force")
async def rate_in_force(
    _: CanRead,
    pool: Pool,
    on: date,
    hotel_room_type_id: UUID | None = None,
    service_id: UUID | None = None,
    pax: Annotated[int | None, Query(ge=1, le=999)] = None,
) -> dict[str, Any]:
    """The rate a version item would take as its default net (``rate`` is null when none is in force)."""
    async with pool.acquire() as conn:
        rate = await rates.rate_in_force(
            conn,
            room_type_id=str(hotel_room_type_id) if hotel_room_type_id else None,
            service_id=str(service_id) if service_id else None,
            on=on,
            pax=pax,
        )
    return {"rate": rate}
