"""The Booking role editor (AA-1155 T6; root spec R2.2, R4.4): custom staff roles cloned from a built-in role.

``GET /booking/staff-roles`` lists the built-in roles (code, read-only), the custom roles, every capability with
whether a custom role may hold it, and ``can_edit``. Readers: ``staff.admin`` or the CIS ``is_owner`` marker. Writes
(``POST``, ``PATCH``) are ``is_owner`` only (403 ``owner_only``), audited before and after; an approve-class or unknown
capability is 400 (:mod:`app.auth.role_editor`).
"""

from __future__ import annotations

from typing import Annotated, Any

import asyncpg
from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, ConfigDict, Field

from app.auth import role_editor
from app.auth.staff import Staff, current_staff, get_pool

router = APIRouter(prefix="/booking", tags=["staff"])

Pool = Annotated[asyncpg.Pool, Depends(get_pool)]
Me = Annotated[Staff, Depends(current_staff)]
RoleCode = Annotated[str, Path(min_length=1, max_length=60)]


class NewRole(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(max_length=60, description="custom_ then a lower-case letter and 2-47 letters, digits or _")
    label: str = Field(max_length=200)
    based_on: str = Field(max_length=40, description="the built-in role cloned: admin, sales, contracting, ...")
    capabilities: list[str] | None = Field(
        default=None,
        max_length=200,
        description="the full capability set; omitted = the built-in role's capabilities without the approve class",
    )


class RolePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str | None = Field(default=None, max_length=200)
    capabilities: list[str] | None = Field(default=None, max_length=200, description="the full capability set")
    archived: bool | None = None


@router.get("/staff-roles")
async def list_roles(staff: Me, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await role_editor.list_roles(conn, staff)


@router.post("/staff-roles", status_code=201)
async def create_role(body: NewRole, staff: Me, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await role_editor.create_role(
            conn, staff, code=body.code, label=body.label, based_on=body.based_on, capabilities=body.capabilities
        )


@router.patch("/staff-roles/{code}")
async def update_role(body: RolePatch, code: RoleCode, staff: Me, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await role_editor.update_role(
            conn, staff, code, label=body.label, capabilities=body.capabilities, archived=body.archived
        )
