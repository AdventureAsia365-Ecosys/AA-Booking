"""Staff profile endpoints (AA-680, Booking side): who am I, and admin activation / roles."""

from __future__ import annotations

from typing import Annotated, Any

import asyncpg
from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel, ConfigDict, Field

from app.auth import admin
from app.auth.staff import Staff, current_staff, get_pool, require

router = APIRouter(prefix="/booking", tags=["staff"])

Pool = Annotated[asyncpg.Pool, Depends(get_pool)]
Admin = Annotated[Staff, Depends(require("staff.admin"))]


class StaffPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    active: bool | None = None
    role: str | None = Field(default=None, max_length=40)


@router.get("/staff/me")
async def me(staff: Annotated[Staff, Depends(current_staff)]) -> dict[str, Any]:
    return {**staff.public(), "permissions": staff.permissions}


@router.get("/staff")
async def list_staff(
    _: Admin,
    pool: Pool,
    active: Annotated[bool | None, Query()] = None,
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return {"items": await admin.list_staff(conn, active=active)}


@router.patch("/staff/{cis_user_id}")
async def patch_staff(
    body: StaffPatch,
    cis_user_id: Annotated[str, Path(min_length=1, max_length=128)],
    staff: Admin,
    pool: Pool,
) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await admin.update_staff(conn, staff, cis_user_id, active=body.active, role=body.role)
