"""Supplier and supplier contract endpoints for staff (bk-008, AA-822; UI spec §6.3, module spec 08).

Permissions: ``supply.read`` (admin, contracting, sales) for every GET; ``supply.write`` (admin, contracting) for
every write. AA-1029: ``GET /booking/rate-sheets/template`` and ``POST /booking/suppliers/{ref}/rate-sheets`` (the
Excel import into a draft contract, ``app/supply/rate_sheet.py``). Publishing a contract also needs a second person
(maker-checker, ADR 0005 d8): 403 ``maker_checker``.
"""

from __future__ import annotations

import base64
import binascii
from datetime import date
from typing import Annotated, Any, Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Body, Depends, Path, Query, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.auth.staff import Staff, get_pool, require
from app.errors import Invalid
from app.routers.trip_cases import Email
from app.suppliers import service
from app.supply import rate_sheet

router = APIRouter(prefix="/booking", tags=["suppliers"])

SupplierRef = Annotated[str, Path(min_length=1, max_length=64, description="Supplier UUID or code (SUP-000123)")]
ContractId = Annotated[str, Path(min_length=1, max_length=64, description="Contract UUID")]
Pool = Annotated[asyncpg.Pool, Depends(get_pool)]
CanRead = Annotated[Staff, Depends(require("supply.read"))]
CanWrite = Annotated[Staff, Depends(require("supply.write"))]

Category = Literal["hotel", "transport", "guide", "entrance", "activity", "meal"]
Website = Annotated[str, Field(max_length=500, pattern=r"^\s*(https?://\S+)?\s*$")]
Country = Annotated[str, Field(pattern=r"^\s*([A-Za-z]{2})?\s*$")]
Currency = Annotated[str, Field(pattern=r"^[A-Za-z]{3}$")]


class SupplierFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company_name: str | None = Field(default=None, max_length=200)
    categories: list[Category] | None = Field(default=None, max_length=6)
    website: Website | None = None
    internal_notes: str | None = Field(default=None, max_length=5000)
    contact_name: str | None = Field(default=None, max_length=200)
    contact_title: str | None = Field(default=None, max_length=120)
    contact_email: Email | None = None
    contact_phone: str | None = Field(default=None, max_length=40)
    address: str | None = Field(default=None, max_length=500)
    region: str | None = Field(default=None, max_length=120)
    postal_code: str | None = Field(default=None, max_length=20)
    destination_id: UUID | None = None
    country_code: Country | None = None


class NewSupplier(SupplierFields):
    company_name: str = Field(min_length=1, max_length=200)
    categories: list[Category] = Field(min_length=1, max_length=6)


class StatusNote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=500)


class ContractFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=200)
    reference: str | None = Field(default=None, max_length=120)
    valid_from: date | None = None
    valid_to: date | None = None
    currency: Currency | None = None
    payment_terms: str | None = Field(default=None, max_length=2000)
    cancellation_policy: str | None = Field(default=None, max_length=5000)
    notes: str | None = Field(default=None, max_length=5000)


class NewContract(ContractFields):
    title: str = Field(min_length=1, max_length=200)
    valid_from: date
    currency: Currency

    @model_validator(mode="after")
    def _validity(self) -> NewContract:
        if self.valid_to is not None and self.valid_to < self.valid_from:
            raise ValueError("valid_to must be on or after valid_from")
        return self


class ContractPatch(ContractFields):
    status: Literal["published", "expired"] | None = Field(
        default=None, description="publish a draft (second person) or expire a published contract; on its own"
    )


def _payload(body: BaseModel) -> dict[str, Any]:
    data = body.model_dump(exclude_unset=True)
    if data.get("destination_id") is not None:
        data["destination_id"] = str(data["destination_id"])
    return data


@router.get("/suppliers")
async def list_suppliers(
    _: CanRead,
    pool: Pool,
    q: Annotated[str | None, Query(max_length=200, description="name, code, contact name or e-mail")] = None,
    status: Annotated[str | None, Query(max_length=20, description="active | inactive")] = None,
    category: Annotated[str | None, Query(max_length=20)] = None,
    country: Annotated[str | None, Query(max_length=2, min_length=2)] = None,
    sort: Annotated[
        str | None, Query(max_length=30, description="company_name | updated_at | created_at, -desc")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await service.list_suppliers(
            conn, q=q, status=status, category=category, country=country, sort=sort, limit=limit, offset=offset
        )


@router.get("/suppliers/summary")
async def supplier_summary(_: CanRead, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await service.summary(conn)


@router.post("/suppliers", status_code=201)
async def create_supplier(body: NewSupplier, staff: CanWrite, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        created = await service.create_supplier(conn, staff, _payload(body))
        return await service.get_supplier(conn, created["id"])


@router.get("/suppliers/{supplier_ref}")
async def get_supplier(supplier_ref: SupplierRef, _: CanRead, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await service.get_supplier(conn, supplier_ref)


@router.patch("/suppliers/{supplier_ref}")
async def patch_supplier(
    body: SupplierFields, supplier_ref: SupplierRef, staff: CanWrite, pool: Pool
) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await service.update_supplier(conn, staff, supplier_ref, _payload(body))


@router.post("/suppliers/{supplier_ref}/deactivate")
async def deactivate_supplier(
    supplier_ref: SupplierRef, staff: CanWrite, pool: Pool, body: Annotated[StatusNote | None, Body()] = None
) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await service.set_status(conn, staff, supplier_ref, "inactive", body.reason if body else None)


@router.post("/suppliers/{supplier_ref}/activate")
async def activate_supplier(
    supplier_ref: SupplierRef, staff: CanWrite, pool: Pool, body: Annotated[StatusNote | None, Body()] = None
) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await service.set_status(conn, staff, supplier_ref, "active", body.reason if body else None)


@router.get("/suppliers/{supplier_ref}/contracts")
async def list_contracts(supplier_ref: SupplierRef, _: CanRead, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await service.list_contracts(conn, supplier_ref)


@router.post("/suppliers/{supplier_ref}/contracts", status_code=201)
async def create_contract(body: NewContract, supplier_ref: SupplierRef, staff: CanWrite, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await service.create_contract(conn, staff, supplier_ref, _payload(body))


@router.get("/contracts/{contract_id}")
async def get_contract(contract_id: ContractId, _: CanRead, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await service.get_contract(conn, contract_id)


@router.patch("/contracts/{contract_id}")
async def patch_contract(body: ContractPatch, contract_id: ContractId, staff: CanWrite, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await service.update_contract(conn, staff, contract_id, _payload(body))


# ── Rate-sheet Excel import (AA-1029, root ADR 0005 d7 / d8) ─────────────────

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
RATE_SHEET_B64_MAX = (rate_sheet.MAX_BYTES * 4) // 3 + 4


@router.get("/rate-sheets/template")
async def rate_sheet_template(_: CanRead) -> Response:
    """The empty AA rate-sheet workbook (Contract sheet + one sheet per service type)."""
    return Response(
        content=rate_sheet.template(),
        media_type=XLSX,
        headers={"Content-Disposition": 'attachment; filename="aa-rate-sheet-template.xlsx"'},
    )


class RateSheetUpload(BaseModel):
    """The filled template as base64 (the staff BFF forwards JSON bodies only, like supplier photos)."""

    model_config = ConfigDict(extra="forbid")

    data_base64: str = Field(min_length=4, max_length=RATE_SHEET_B64_MAX)
    filename: str | None = Field(default=None, max_length=200)


@router.post("/suppliers/{supplier_ref}/rate-sheets")
async def import_rate_sheet(
    body: RateSheetUpload,
    supplier_ref: SupplierRef,
    staff: CanWrite,
    pool: Pool,
    dry_run: Annotated[bool, Query(description="validate and preview only; nothing is written")] = False,
) -> dict[str, Any]:
    """Upload a filled template. Creates one DRAFT contract with its rates, or 422 ``rate_sheet_invalid`` with every
    problem by sheet, row and column; a second person publishes the draft."""
    data = decode_sheet(body.data_base64)
    async with pool.acquire() as conn, conn.transaction():
        return await rate_sheet.import_sheet(conn, staff, supplier_ref, data, dry_run=dry_run)


def decode_sheet(text: str) -> bytes:
    try:
        data = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        raise Invalid("rate_sheet_not_xlsx", "the upload is not valid base64") from None
    if len(data) > rate_sheet.MAX_BYTES:
        raise Invalid("rate_sheet_too_large", "a rate sheet is at most 2 MB")
    return data
