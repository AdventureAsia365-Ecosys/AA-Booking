"""The choices of the supplier profile (AA-1158 part 2, bk-067; :mod:`app.suppliers.profile_options`).

- ``GET /booking/supplier-profile-options`` (``supply.read``): contact roles, payment terms bases and methods, active
  and retired, in page order.
- ``POST /booking/supplier-profile-options`` (``staff.admin``): add a choice.
- ``PATCH /booking/supplier-profile-options/{kind}/{code}`` (``staff.admin``): its labels, order or ``active``.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

import asyncpg
from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, ConfigDict, Field

from app.auth.staff import Staff, get_pool, require
from app.suppliers import profile_options

router = APIRouter(prefix="/booking", tags=["supplier profile choices"])

Pool = Annotated[asyncpg.Pool, Depends(get_pool)]
CanRead = Annotated[Staff, Depends(require("supply.read"))]
CanEdit = Annotated[Staff, Depends(require("staff.admin"))]
Kind = Literal["contact_role", "terms_basis", "terms_method"]
Code = Annotated[str, Path(pattern=r"^[a-z][a-z0-9_]{1,39}$")]


class NewOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Kind
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{1,39}$", description="stored value; never changes once created")
    label_en: str = Field(min_length=1, max_length=80)
    label_vi: str = Field(min_length=1, max_length=80)
    position: int | None = Field(default=None, ge=1, le=999, description="page order; default: after the last one")


class OptionChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label_en: str | None = Field(default=None, min_length=1, max_length=80)
    label_vi: str | None = Field(default=None, min_length=1, max_length=80)
    position: int | None = Field(default=None, ge=1, le=999)
    active: bool | None = Field(default=None, description="false retires the choice (suppliers holding it keep it)")


@router.get("/supplier-profile-options")
async def list_profile_options(_: CanRead, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return {"kinds": await profile_options.list_all(conn)}


@router.post("/supplier-profile-options", status_code=201)
async def add_profile_option(body: NewOption, staff: CanEdit, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await profile_options.create(conn, staff, body.model_dump())


@router.patch("/supplier-profile-options/{kind}/{code}")
async def change_profile_option(
    body: OptionChange, kind: Kind, code: Code, staff: CanEdit, pool: Pool
) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await profile_options.update(conn, staff, kind, code, body.model_dump(exclude_none=True))
