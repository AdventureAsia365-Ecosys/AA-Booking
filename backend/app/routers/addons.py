"""Add-on offers, the activity catalogue import and the contract expiry list for staff (AA-1151, bk-068; PR-29 B1 /
B3 / B4 / T7).

Permissions: ``addon.read`` (every staff role) for the reads; ``addon.write`` (admin, contracting, content_creator) to
create and edit a draft, set its places, reopen or retire it; ``addon.approve`` (admin, contracting) to publish a
draft, never one the caller wrote (403 ``maker_checker``, except the business owner, flagged). The activity catalogue
import creates services, rates and a draft contract too, so it needs ``supply.write`` and ``addon.write``. The
expiring contracts list is ``supply.read``.
"""

from __future__ import annotations

from datetime import date, time
from decimal import Decimal
from typing import Annotated, Any, Literal

import asyncpg
from fastapi import APIRouter, Depends, Path, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from app.auth import e2e_staff_supply
from app.auth.staff import Staff, get_pool, require
from app.errors import Forbidden
from app.routers.suppliers import XLSX, RateSheetUpload, decode_sheet
from app.supply import activity_import, addons, contract_expiry

router = APIRouter(prefix="/booking", tags=["add-ons"])

OfferId = Annotated[str, Path(min_length=1, max_length=64, description="Add-on offer UUID")]
SupplierRef = Annotated[str, Path(min_length=1, max_length=64, description="Supplier UUID or code")]
Pool = Annotated[asyncpg.Pool, Depends(get_pool)]
CanRead = Annotated[Staff, Depends(require("addon.read"))]
CanWrite = Annotated[Staff, Depends(require("addon.write"))]
CanApprove = Annotated[Staff, Depends(require("addon.approve"))]
CanReadSupply = Annotated[Staff, Depends(require("supply.read"))]
CanWriteSupply = Annotated[Staff, Depends(require("supply.write"))]
Pct = Annotated[Decimal, Field(ge=0, le=100, max_digits=5, decimal_places=2)]


class AddonTier(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_days: int = Field(ge=0, le=730)
    fee_pct: Pct


class AddonPlace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    destination_id: str = Field(min_length=36, max_length=36, description="catalog.v_destination id")
    role: Literal["operates", "pickup"] = "operates"


class AddonRules(BaseModel):
    """Sale rules and overrides (T7, B4); a field left out is unchanged on PATCH."""

    model_config = ConfigDict(extra="forbid")

    min_pax: int | None = Field(default=None, ge=1, le=addons.MIN_PAX_MAX)
    cutoff_hours: int | None = Field(default=None, ge=0, le=addons.CUTOFF_HOURS_MAX)
    operating_weekdays: list[Annotated[int, Field(ge=1, le=7)]] | None = Field(default=None, min_length=1, max_length=7)
    start_times: list[time] | None = Field(default=None, max_length=addons.MAX_START_TIMES)
    child_pct: Pct | None = None
    cancellation_tiers: list[AddonTier] | None = Field(default=None, min_length=1, max_length=20)
    sort_order: int | None = Field(default=None, ge=-addons.SORT_BOUND, le=addons.SORT_BOUND)
    featured: bool | None = None


class NewAddonOffer(AddonRules):
    service_id: str = Field(min_length=1, max_length=64, description="service UUID or code (SVC-000123)")
    cutoff_hours: int = Field(ge=0, le=addons.CUTOFF_HOURS_MAX)
    places: list[AddonPlace] = Field(default_factory=list, max_length=addons.MAX_PLACES)


class AddonPlacesBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    places: list[AddonPlace] = Field(max_length=addons.MAX_PLACES)


def _rules(body: AddonRules, *, unset: bool) -> dict[str, Any]:
    data = body.model_dump(exclude_unset=unset, exclude={"service_id", "places"})
    if not unset:
        data = {k: v for k, v in data.items() if v is not None}
    if data.get("cancellation_tiers") is not None:
        data["cancellation_tiers"] = [
            {"min_days": t["min_days"], "fee_pct": str(t["fee_pct"])} for t in data["cancellation_tiers"]
        ]
    # PATCH: an explicit null clears the two optional overrides; the other fields keep their value.
    return {k: v for k, v in data.items() if v is not None or k in ("child_pct", "cancellation_tiers")}


@router.get("/add-ons")
async def list_offers(
    staff: CanRead,
    pool: Pool,
    status: Annotated[Literal["draft", "published", "retired"] | None, Query()] = None,
    destination_id: Annotated[str | None, Query(max_length=36)] = None,
    supplier_id: Annotated[str | None, Query(max_length=36)] = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
    approvable: Annotated[
        Literal["me"] | None, Query(description="only drafts the caller may publish (wrote none of)")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await addons.list_offers(
            conn,
            status=status,
            destination_id=destination_id,
            supplier_id=supplier_id,
            q=q,
            approvable_by=staff.cis_user_id if approvable == "me" else None,
            limit=limit,
            offset=offset,
        )


@router.get("/add-ons/services")
async def service_options(
    _: CanRead,
    pool: Pool,
    q: Annotated[str | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> dict[str, Any]:
    """Services an offer can wrap (code, title, type, supplier name; never a net)."""
    async with pool.acquire() as conn:
        return await addons.service_options(conn, q, limit)


@router.get("/add-ons/place-days")
async def place_days(
    _: CanRead,
    pool: Pool,
    destination_id: Annotated[str, Query(min_length=36, max_length=36)],
    date_from: date,
    date_to: date,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> dict[str, Any]:
    """Preview of ``booking.v_place_addons``: the add-ons a traveller at the place is offered, per day."""
    async with pool.acquire() as conn:
        return await addons.place_days(conn, destination_id, date_from, date_to, limit)


@router.get("/add-ons/{offer_id}")
async def get_offer(offer_id: OfferId, staff: CanRead, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await addons.get_offer(conn, offer_id, staff)


@router.post("/add-ons", status_code=201)
async def create_offer(body: NewAddonOffer, staff: CanWrite, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await addons.create_offer(
            conn, staff, body.service_id, _rules(body, unset=False), [p.model_dump() for p in body.places]
        )


@router.patch("/add-ons/{offer_id}")
async def update_offer(body: AddonRules, offer_id: OfferId, staff: CanWrite, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await addons.update_offer(conn, staff, offer_id, _rules(body, unset=True))


@router.put("/add-ons/{offer_id}/places")
async def set_places(body: AddonPlacesBody, offer_id: OfferId, staff: CanWrite, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await addons.set_places(conn, staff, offer_id, [p.model_dump() for p in body.places])


@router.post("/add-ons/{offer_id}/publish")
async def publish(offer_id: OfferId, staff: CanApprove, pool: Pool) -> dict[str, Any]:
    """A second person publishes the draft (never one of its writers, unless the business owner, flagged)."""
    async with pool.acquire() as conn, conn.transaction():
        return await addons.publish(conn, staff, offer_id)


@router.post("/add-ons/{offer_id}/reopen")
async def reopen(offer_id: OfferId, staff: CanWrite, pool: Pool) -> dict[str, Any]:
    """Back to a draft (stops selling at once); another person publishes it again."""
    async with pool.acquire() as conn, conn.transaction():
        return await addons.reopen(conn, staff, offer_id)


@router.post("/add-ons/{offer_id}/retire")
async def retire(offer_id: OfferId, staff: CanWrite, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await addons.retire(conn, staff, offer_id)


# ── Activity catalogue import ────────────────────────────────────────────────


@router.get("/activity-sheets/template")
async def activity_sheet_template(_: CanRead) -> Response:
    """The empty AA activity catalogue workbook (Contract + Activities sheets)."""
    return Response(
        content=activity_import.template(),
        media_type=XLSX,
        headers={"Content-Disposition": 'attachment; filename="aa-activity-catalogue-template.xlsx"'},
    )


@router.post("/suppliers/{supplier_ref}/activity-sheets")
async def import_activity_sheet(
    body: RateSheetUpload,
    supplier_ref: SupplierRef,
    staff: CanWriteSupply,
    pool: Pool,
    dry_run: Annotated[bool, Query(description="validate and preview only; nothing is written")] = False,
) -> dict[str, Any]:
    """Upload a filled activity catalogue: per row an activity service, a draft add-on and its rates in one draft
    contract, or 422 ``activity_sheet_invalid`` with every problem; second persons publish the contract and the
    add-ons."""
    if not staff.can("addon.write"):
        raise Forbidden("forbidden", "the activity import creates add-on drafts: it needs addon.write too")
    data = decode_sheet(body.data_base64)
    if staff.e2e_writer is not None:
        # A10e-4 (root ADR 0006a): a Dev-only E2E session imports marked activities only (its supplier was checked
        # synthetic from the path before the route ran); refused before anything is previewed or written.
        e2e_staff_supply.check_activity_sheet(staff.e2e_writer, activity_import.parse(data))
    async with pool.acquire() as conn, conn.transaction():
        return await activity_import.import_sheet(conn, staff, supplier_ref, data, dry_run=dry_run)


# ── Contracts ending soon (B3) ───────────────────────────────────────────────


@router.get("/contracts/expiring")
async def expiring_contracts(_: CanReadSupply, pool: Pool) -> dict[str, Any]:
    """Published supplier contracts that end within ``contract_expiry_alert_days`` (Pricing > Policy), soonest first."""
    async with pool.acquire() as conn:
        return await contract_expiry.expiring(conn)
