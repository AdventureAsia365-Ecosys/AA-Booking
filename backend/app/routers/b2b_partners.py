"""Staff admin of B2B partners (``/booking/b2b-partners``, bk-019, AA-972; permission ``b2b.admin`` = Booking admin).

``GET`` lists every partner, ``POST`` registers one (an agency's CIS tenant id + the aa-m2m client that may act for
it), ``PATCH /{id}`` edits it (``status = suspended`` stops the agency's B2B calls at once). No delete. Staff tokens
only; the machine API is ``/booking/b2b/v1`` (``app.routers.b2b``).
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

import asyncpg
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from app.auth.staff import Staff, get_pool, require
from app.b2b import partners

router = APIRouter(prefix="/booking/b2b-partners", tags=["b2b"])

Pool = Annotated[asyncpg.Pool, Depends(get_pool)]
CanAdmin = Annotated[Staff, Depends(require("b2b.admin"))]

ClientId = Annotated[str, Field(pattern=r"^[A-Za-z0-9]{1,128}$")]
Currency = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]


class PartnerCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: uuid.UUID
    agency_name: str = Field(min_length=1, max_length=200)
    m2m_client_id: ClientId
    billing_currency: Currency = "USD"
    credit_terms_ref: str | None = Field(default=None, max_length=120)
    status: Literal["active", "suspended"] = "active"


class PartnerPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agency_name: str | None = Field(default=None, min_length=1, max_length=200)
    m2m_client_id: ClientId | None = None
    billing_currency: Currency | None = None
    credit_terms_ref: str | None = Field(default=None, max_length=120)
    status: Literal["active", "suspended"] | None = None


@router.get("")
async def list_partners(_: CanAdmin, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await partners.list_partners(conn)


@router.post("", status_code=201)
async def create_partner(body: PartnerCreate, staff: CanAdmin, pool: Pool) -> dict[str, Any]:
    data = body.model_dump()
    data["tenant_id"] = str(body.tenant_id)
    async with pool.acquire() as conn, conn.transaction():
        return await partners.create_partner(conn, staff, data)


@router.patch("/{partner_id}")
async def update_partner(partner_id: uuid.UUID, body: PartnerPatch, staff: CanAdmin, pool: Pool) -> dict[str, Any]:
    # Fields sent as null are ignored except credit_terms_ref, which may be cleared.
    changes = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None or k == "credit_terms_ref"}
    async with pool.acquire() as conn, conn.transaction():
        return await partners.update_partner(conn, staff, str(partner_id), changes)
