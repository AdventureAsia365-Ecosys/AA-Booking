"""Staff profile endpoints (AA-680, Booking side): who am I, preferences, the picker directory, detail and
admin activation / roles."""

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
Me = Annotated[Staff, Depends(current_staff)]
CanPick = Annotated[Staff, Depends(require("staff.directory"))]
StaffId = Annotated[str, Path(min_length=1, max_length=128)]


class StaffPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    active: bool | None = None
    role: str | None = Field(default=None, max_length=40)


class Preferences(BaseModel):
    model_config = ConfigDict(extra="forbid")

    notify: dict[str, bool] = Field(default_factory=dict, description="notification type -> on/off")


@router.get("/staff/me")
async def me(staff: Me) -> dict[str, Any]:
    return {**staff.public(), "permissions": staff.permissions}


@router.get("/staff/me/preferences")
async def get_preferences(staff: Me, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await admin.get_preferences(conn, staff)


@router.put("/staff/me/preferences")
async def put_preferences(body: Preferences, staff: Me, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await admin.set_preferences(conn, staff, body.notify)


@router.get("/staff/directory")
async def staff_directory(
    _: CanPick,
    pool: Pool,
    role: Annotated[str | None, Query(max_length=40)] = None,
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return {"items": await admin.directory(conn, role=role)}


@router.get("/staff")
async def list_staff(
    _: Admin,
    pool: Pool,
    active: Annotated[bool | None, Query()] = None,
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return {"items": await admin.list_staff(conn, active=active)}


@router.get("/staff/{cis_user_id}")
async def get_staff(cis_user_id: StaffId, staff: Me, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await admin.staff_detail(conn, staff, cis_user_id)


@router.patch("/staff/{cis_user_id}")
async def patch_staff(
    body: StaffPatch,
    cis_user_id: StaffId,
    staff: Admin,
    pool: Pool,
) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await admin.update_staff(conn, staff, cis_user_id, active=body.active, role=body.role)
