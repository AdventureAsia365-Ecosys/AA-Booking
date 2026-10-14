"""Staff trip case endpoints (AA-681 slice 3). Every route needs a CIS staff JWT + an active Booking profile.

A trip case is addressed by its UUID or its code (``TC-000123``).
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any

import asyncpg
from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.auth.staff import Staff, get_pool, require
from app.trip_cases import service, versions

router = APIRouter(prefix="/booking", tags=["trip-cases"])

CaseRef = Annotated[str, Path(min_length=1, max_length=64, description="Trip case UUID or code (TC-000123)")]
VersionNumber = Annotated[int, Path(ge=1, le=32767)]
Pool = Annotated[asyncpg.Pool, Depends(get_pool)]
CanRead = Annotated[Staff, Depends(require("trip_case.read"))]
CanWrite = Annotated[Staff, Depends(require("trip_case.write"))]
CanAssign = Annotated[Staff, Depends(require("trip_case.assign"))]
CanWriteVersions = Annotated[Staff, Depends(require("version.write"))]


class CasePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pax: int | None = Field(default=None, ge=1, le=999)
    start_date: date | None = None
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    notes: str | None = Field(default=None, max_length=10_000)


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


class ItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=500)
    day_from: int = Field(ge=1, le=366)
    day_to: int = Field(ge=1, le=366)
    source_tour_id: str | None = Field(default=None, min_length=1, max_length=128)
    notes: str | None = Field(default=None, max_length=5_000)

    @model_validator(mode="after")
    def _days_in_order(self) -> ItemIn:
        if self.day_to < self.day_from:
            raise ValueError("day_to must be >= day_from")
        return self


class ItemsPut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ItemIn] = Field(max_length=versions.MAX_ITEMS)


def _statuses(raw: list[str] | None) -> list[str] | None:
    if not raw:
        return None
    return [s.strip() for value in raw for s in value.split(",") if s.strip()] or None


@router.get("/trip-cases")
async def list_trip_cases(
    staff: CanRead,
    pool: Pool,
    status: Annotated[list[str] | None, Query(description="repeat or comma-separate")] = None,
    assignee: Annotated[str | None, Query(max_length=128, description="CIS user id, 'me' or 'none'")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    if assignee == "me":
        assignee = staff.cis_user_id
    async with pool.acquire() as conn:
        return await service.list_cases(conn, statuses=_statuses(status), assignee=assignee, limit=limit, offset=offset)


@router.get("/trip-cases/{case_ref}")
async def get_trip_case(
    case_ref: CaseRef,
    _: CanRead,
    pool: Pool,
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await service.get_case(conn, case_ref)


@router.patch("/trip-cases/{case_ref}")
async def patch_trip_case(
    body: CasePatch,
    case_ref: CaseRef,
    staff: CanWrite,
    pool: Pool,
) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await service.update_case(conn, staff, case_ref, body.model_dump(exclude_unset=True))


@router.post("/trip-cases/{case_ref}/status")
async def change_trip_case_status(
    body: StatusChange,
    case_ref: CaseRef,
    staff: CanWrite,
    pool: Pool,
) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await service.change_status(conn, staff, case_ref, body.to_status, body.reason)


@router.post("/trip-cases/{case_ref}/assign")
async def assign_trip_case(
    body: AssignBody,
    case_ref: CaseRef,
    staff: CanAssign,
    pool: Pool,
) -> dict[str, Any]:
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


@router.get("/trip-cases/{case_ref}/versions/{version_number}/items")
async def get_version_items(
    case_ref: CaseRef,
    version_number: VersionNumber,
    _: CanRead,
    pool: Pool,
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await versions.get_items(conn, case_ref, version_number)


@router.put("/trip-cases/{case_ref}/versions/{version_number}/items")
async def put_version_items(
    body: ItemsPut,
    case_ref: CaseRef,
    version_number: VersionNumber,
    staff: CanWriteVersions,
    pool: Pool,
) -> dict[str, Any]:
    items = [versions.Item(**item.model_dump()) for item in body.items]
    async with pool.acquire() as conn, conn.transaction():
        return await versions.replace_items(conn, staff, case_ref, version_number, items)
