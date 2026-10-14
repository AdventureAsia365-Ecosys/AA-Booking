"""Customer endpoints for staff (S236 UI spec §4.12): directory, detail, manual lead, edit / block, archive."""

from __future__ import annotations

from typing import Annotated, Any

import asyncpg
from fastapi import APIRouter, Depends, Path, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from app.auth.staff import Staff, get_pool, require
from app.customers import service
from app.routers.trip_cases import Email, NewCustomer

router = APIRouter(prefix="/booking", tags=["customers"])

CustomerRef = Annotated[str, Path(min_length=1, max_length=64, description="Customer UUID or code (CUS-000123)")]
Pool = Annotated[asyncpg.Pool, Depends(get_pool)]
CanRead = Annotated[Staff, Depends(require("customer.read"))]
CanWrite = Annotated[Staff, Depends(require("customer.write"))]
CanAdmin = Annotated[Staff, Depends(require("customer.admin"))]


class CustomerPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    email: Email | None = None
    phone: str | None = Field(default=None, min_length=3, max_length=40)
    address: str | None = Field(default=None, max_length=500)
    is_blocked: bool | None = None


@router.get("/customers")
async def list_customers(
    _: CanRead,
    pool: Pool,
    q: Annotated[str | None, Query(max_length=200, description="name, e-mail, phone or code")] = None,
    status: Annotated[str | None, Query(max_length=20, description="lead | active | past | blocked")] = None,
    merge_review: Annotated[bool | None, Query()] = None,
    source: Annotated[str | None, Query(max_length=20, description="manual | otp | tripplanner_lead")] = None,
    sort: Annotated[str | None, Query(max_length=30, description="last_activity_at | created_at | name, -desc")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await service.list_customers(
            conn, q=q, status=status, merge_review=merge_review, source=source, sort=sort, limit=limit, offset=offset
        )


@router.post("/customers", status_code=201)
async def create_customer(body: NewCustomer, staff: CanWrite, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        created = await service.create_lead(conn, staff, body.model_dump())
        return await service.get_customer(conn, created["id"])


@router.get("/customers/{customer_ref}")
async def get_customer(customer_ref: CustomerRef, _: CanRead, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await service.get_customer(conn, customer_ref)


@router.patch("/customers/{customer_ref}")
async def patch_customer(body: CustomerPatch, customer_ref: CustomerRef, staff: CanWrite, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await service.update_customer(conn, staff, customer_ref, body.model_dump(exclude_unset=True))


@router.delete("/customers/{customer_ref}", status_code=204)
async def archive_customer(customer_ref: CustomerRef, staff: CanAdmin, pool: Pool) -> Response:
    async with pool.acquire() as conn, conn.transaction():
        await service.archive_customer(conn, staff, customer_ref)
    return Response(status_code=204)
