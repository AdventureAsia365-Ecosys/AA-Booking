"""Supplier contract terms, supply restrictions and stop-sale for staff (AA-1145, bk-060; T12, B4, B8, T7, T13).

Permissions: ``supply.read`` (admin, contracting, sales, finance) for every GET; ``supply.write`` (admin, contracting)
for the contract terms and the allotment / free-sale / on-request rows of a draft contract (the writer becomes the
draft's maker, so a second person publishes them with the contract); ``stop_sale.write`` (admin, contracting; sales 403)
to set, review and end a stop-sale (``app.supply.restrictions``: in effect at once, a second person reviews it; ending
an approved one takes a second person, 403 ``maker_checker``). AA-1150: the review (approve / reject) and the end
decision also need ``stop_sale.approve`` (admin, contracting); setting one, withdrawing one's own, asking to end one and
taking that request back stay ``stop_sale.write``.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Literal

import asyncpg
from fastapi import APIRouter, Depends, Path, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from app.auth.staff import Staff, get_pool, require
from app.pricing import policy_values
from app.supply import contract_terms, restrictions

router = APIRouter(prefix="/booking", tags=["supply"])

ContractId = Annotated[str, Path(min_length=1, max_length=64, description="Contract UUID")]
RestrictionId = Annotated[str, Path(min_length=1, max_length=64, description="Restriction UUID")]
Pool = Annotated[asyncpg.Pool, Depends(get_pool)]
CanRead = Annotated[Staff, Depends(require("supply.read"))]
CanWrite = Annotated[Staff, Depends(require("supply.write"))]
CanStop = Annotated[Staff, Depends(require("stop_sale.write"))]
CanApproveStop = Annotated[Staff, Depends(require("stop_sale.approve"))]
Component = Annotated[str, Field(min_length=3, max_length=64, description="rt:<room type id> or sv:<service id>")]
Days = Annotated[int, Field(ge=0, le=contract_terms.DAYS_MAX)]
Pct = Annotated[Decimal, Field(ge=0, le=100, max_digits=5, decimal_places=2)]
Note = Annotated[str, Field(min_length=1, max_length=restrictions.MAX_NOTE)]
Count = Annotated[int, Field(ge=1, le=999)]


class Tier(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_days: int = Field(ge=0, le=730)
    fee_pct: Pct


class TermsBody(BaseModel):
    """The whole terms of a draft contract (a field left out is cleared)."""

    model_config = ConfigDict(extra="forbid")

    cancellation_tiers: list[Tier] | None = Field(default=None, min_length=1, max_length=20)
    deposit_pct: Pct | None = None
    deposit_due_days: Days | None = None
    balance_due_days_before: Days | None = None
    credit_days_after: Days | None = None
    child_pct: Pct | None = None


class NewTerm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    component_id: Component
    inventory_kind: Literal["allotment", "free_sale", "on_request"]
    date_from: date
    date_to: date
    units: Count | None = None
    release_days: Annotated[int, Field(ge=0, le=365)] | None = None
    max_pax: Count | None = None
    note: str | None = Field(default=None, max_length=restrictions.MAX_NOTE)


class TermPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date_from: date | None = None
    date_to: date | None = None
    units: Count | None = None
    release_days: Annotated[int, Field(ge=0, le=365)] | None = None
    max_pax: Count | None = None
    note: str | None = Field(default=None, max_length=restrictions.MAX_NOTE)


class NewStopSale(BaseModel):
    model_config = ConfigDict(extra="forbid")

    component_id: Component
    date_from: date
    date_to: date
    note: Note = Field(description="why, e.g. the supplier e-mail that announced it")


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["approve", "reject", "withdraw"]
    reason: str | None = Field(default=None, max_length=restrictions.MAX_NOTE)


class EndRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Note


def _terms_payload(body: TermsBody) -> dict[str, Any]:
    data = body.model_dump()
    if data["cancellation_tiers"] is not None:
        data["cancellation_tiers"] = [
            {"min_days": t["min_days"], "fee_pct": str(t["fee_pct"])} for t in data["cancellation_tiers"]
        ]
    return data


@router.get("/contracts/{contract_id}/terms")
async def get_terms(contract_id: ContractId, _: CanRead, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await contract_terms.get_terms(conn, contract_id)


@router.put("/contracts/{contract_id}/terms")
async def put_terms(body: TermsBody, contract_id: ContractId, staff: CanWrite, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await contract_terms.put_terms(conn, staff, contract_id, _terms_payload(body))


@router.get("/supply/restrictions")
async def list_restrictions(
    staff: CanRead,
    pool: Pool,
    contract_id: Annotated[str | None, Query(max_length=64)] = None,
    component_id: Annotated[str | None, Query(max_length=64, description="rt:<id> or sv:<id>")] = None,
    kind: Annotated[
        str | None, Query(max_length=20, description="allotment | free_sale | on_request | stop_sale")
    ] = None,
    active: Annotated[bool, Query(description="only the rows in effect")] = False,
    review: Annotated[
        Literal["waiting", "me"] | None,
        Query(
            description="AA-1159: stop-sales waiting for a second person (waiting), or only those the caller may"
            " decide, not entered nor asked to end by them (me); the longest waiting first"
        ),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await restrictions.list_restrictions(
            conn,
            contract_id=contract_id,
            component_id=component_id,
            kind=kind,
            active_only=active,
            review=review,
            reviewer=staff.cis_user_id,
            limit=limit,
        )


@router.get("/supply/restrictions/{restriction_id}")
async def get_restriction(restriction_id: RestrictionId, _: CanRead, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await restrictions.get_restriction(conn, restriction_id)


@router.post("/contracts/{contract_id}/restrictions", status_code=201)
async def create_term(body: NewTerm, contract_id: ContractId, staff: CanWrite, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await restrictions.create_term(conn, staff, contract_id, body.model_dump())


@router.patch("/supply/restrictions/{restriction_id}")
async def patch_term(body: TermPatch, restriction_id: RestrictionId, staff: CanWrite, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await restrictions.update_term(conn, staff, restriction_id, body.model_dump(exclude_unset=True))


@router.delete("/supply/restrictions/{restriction_id}", status_code=204)
async def delete_term(restriction_id: RestrictionId, staff: CanWrite, pool: Pool) -> Response:
    async with pool.acquire() as conn, conn.transaction():
        await restrictions.delete_term(conn, staff, restriction_id)
    return Response(status_code=204)


@router.post("/contracts/{contract_id}/stop-sales", status_code=201)
async def create_stop_sale(body: NewStopSale, contract_id: ContractId, staff: CanStop, pool: Pool) -> dict[str, Any]:
    """A stop-sale on a published contract: in effect at once (pending), a second person reviews it."""
    async with pool.acquire() as conn, conn.transaction():
        return await restrictions.create_stop_sale(conn, staff, contract_id, body.model_dump())


@router.post("/supply/stop-sales/{restriction_id}/decision")
async def decide_stop_sale(body: Decision, restriction_id: RestrictionId, staff: CanStop, pool: Pool) -> dict[str, Any]:
    """``approve`` / ``reject`` (a second person) or ``withdraw`` (its maker) a pending stop-sale."""
    async with pool.acquire() as conn, conn.transaction():
        return await restrictions.decide_stop_sale(conn, staff, restriction_id, body.action, body.reason)


@router.post("/supply/stop-sales/{restriction_id}/end-request")
async def request_end(body: EndRequest, restriction_id: RestrictionId, staff: CanStop, pool: Pool) -> dict[str, Any]:
    """Ask to lift an approved stop-sale; it stays in effect until a second person approves the end."""
    async with pool.acquire() as conn, conn.transaction():
        return await restrictions.request_end(conn, staff, restriction_id, body.reason)


@router.post("/supply/stop-sales/{restriction_id}/end")
async def approve_end(restriction_id: RestrictionId, staff: CanApproveStop, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await restrictions.approve_end(conn, staff, restriction_id)


@router.delete("/supply/stop-sales/{restriction_id}/end-request")
async def cancel_end_request(restriction_id: RestrictionId, staff: CanStop, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await restrictions.cancel_end_request(conn, staff, restriction_id)


@router.get("/supply/availability")
async def availability(
    _: CanRead,
    pool: Pool,
    component_id: Annotated[str, Query(min_length=3, max_length=64, description="rt:<id> or sv:<id>")],
    date_from: date,
    date_to: date,
    pax: Annotated[int, Query(ge=1, le=999)] = 2,
    units: Annotated[int, Query(ge=1, le=999, description="rooms or seats the booking needs")] = 1,
) -> dict[str, Any]:
    """The availability engine per date (stop_sale > allotment / free_sale > on_request), with its reason."""
    async with pool.acquire() as conn:
        today = await policy_values.today_utc(conn)
        return await restrictions.check(
            conn, component_id=component_id, date_from=date_from, date_to=date_to, pax=pax, units=units, today=today
        )
