"""Price list versions (AA-1091, bk-054, ``app/catalog/price_versions.py``) and season calendars and holiday events
(bk-055, ``app/pricing/seasons_admin.py``).

Versions of a plan (reads ``pricing.read``; drafts ``price_version.write``; decisions ``price_version.approve``):
``GET|POST /booking/catalog/trip-plans/{source_id}/price-versions``, ``GET /booking/catalog/price-versions?status=
pending`` (the checker's queue), ``GET|PUT /booking/catalog/price-versions/{id}``, ``POST …/{id}/submit|approve|
reject|withdraw``, ``GET …/{id}/withdraw-preview``. The plan's season calendar: ``GET /booking/catalog/trip-plans/
{source_id}/season-calendar`` (``pricing.read``), ``PUT`` asks for a change with a reason (``season.write``),
``POST …/season-calendar/approve|reject|withdraw`` (a second person with ``season.approve``; the requester withdraws).

Seasons (reads ``pricing.read``; proposals ``season.write``; decisions ``season.approve``; a reason on every
proposal, withdrawal, removal request and rejection):
``GET|POST /booking/pricing/season-calendars``, ``GET …/{id}?year=``, ``POST …/{id}/ranges``, ``POST …/{id}/default``
and ``…/{id}/default/approve|reject``; ``POST /booking/pricing/season-ranges/{id}/{action}``;
``GET|POST /booking/pricing/holiday-events``, ``POST /booking/pricing/holiday-events/{id}/{action}``. Inclusive end
dates in and out.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Any, Literal

import asyncpg
from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel, ConfigDict, Field

from app.auth.staff import Staff, get_pool, require
from app.catalog import price_versions
from app.pricing import seasons_admin
from app.routers.catalog import PriceRowIn, SourceId

router = APIRouter(tags=["price-versions"])

Pool = Annotated[asyncpg.Pool, Depends(get_pool)]
CanReadPrices = Annotated[Staff, Depends(require("pricing.read"))]
CanWriteVersions = Annotated[Staff, Depends(require("price_version.write"))]
CanApproveVersions = Annotated[Staff, Depends(require("price_version.approve"))]
CanWriteSeasons = Annotated[Staff, Depends(require("season.write"))]
CanApproveSeasons = Annotated[Staff, Depends(require("season.approve"))]
VersionId = Annotated[uuid.UUID, Path(description="price list version id")]
Ident = Annotated[uuid.UUID, Path(description="id")]
Reason = Annotated[str, Field(min_length=1, max_length=price_versions.REASON_MAX)]
Season = Literal["low", "high", "peak", "festive"]
Year = Annotated[int, Field(ge=2000, le=2100)]


class DraftIn(BaseModel):
    """A new draft: travel window (inclusive last day; default the tariff year of today), optional sale window."""

    model_config = ConfigDict(extra="forbid")

    copy_from: str = Field(default="in_force", description="in_force | none | a version id of the same plan")
    label: str | None = Field(default=None, max_length=price_versions.LABEL_MAX)
    tariff_year: Year | None = None
    travel_from: date | None = None
    travel_to: date | None = None
    sale_from: date | None = None
    sale_to: date | None = None
    reason: str | None = Field(default=None, max_length=price_versions.REASON_MAX)


class DraftBody(BaseModel):
    """The whole draft (PUT): facts and rows."""

    model_config = ConfigDict(extra="forbid")

    label: str | None = Field(default=None, max_length=price_versions.LABEL_MAX)
    tariff_year: Year | None = None
    travel_from: date
    travel_to: date
    sale_from: date | None = None
    sale_to: date | None = None
    reason: str | None = Field(default=None, max_length=price_versions.REASON_MAX)
    items: list[PriceRowIn] = Field(max_length=200)
    below_floor_reason: str | None = Field(default=None, max_length=price_versions.REASON_MAX)


class ReasonBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Reason


class SubmitBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=price_versions.REASON_MAX)


class DecisionBody(BaseModel):
    """A decision's reason: required to reject, withdraw, ask for a removal or reject one (checked in the service);
    optional to approve."""

    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=seasons_admin.REASON_MAX)


class ApproveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    diff_sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$", description="the SHA-256 of the diff the checker saw")


class PlanCalendarBody(BaseModel):
    """Ask for another calendar of the plan (None = no calendar), with the reason a second person reads."""

    model_config = ConfigDict(extra="forbid")

    season_calendar_id: uuid.UUID | None
    reason: Reason


# ── Versions ─────────────────────────────────────────────────────────────────


@router.get("/booking/catalog/trip-plans/{source_id}/price-versions")
async def list_versions(source_id: SourceId, staff: CanReadPrices, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await price_versions.list_versions(conn, staff, source_id)


@router.post("/booking/catalog/trip-plans/{source_id}/price-versions", status_code=201)
async def create_version(body: DraftIn, source_id: SourceId, staff: CanWriteVersions, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await price_versions.create_draft(conn, staff, source_id, body.model_dump())


@router.get("/booking/catalog/trip-plans/{source_id}/season-calendar")
async def get_plan_calendar(source_id: SourceId, staff: CanReadPrices, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await seasons_admin.plan_calendar(conn, source_id)


@router.put("/booking/catalog/trip-plans/{source_id}/season-calendar")
async def request_plan_calendar(
    body: PlanCalendarBody, source_id: SourceId, staff: CanWriteSeasons, pool: Pool
) -> dict[str, Any]:
    """Ask for another season calendar of the plan; it applies once a second person approves it."""
    cal = str(body.season_calendar_id) if body.season_calendar_id else None
    async with pool.acquire() as conn, conn.transaction():
        return await seasons_admin.request_plan_calendar(conn, staff, source_id, cal, body.reason)


@router.post("/booking/catalog/trip-plans/{source_id}/season-calendar/{action}")
async def decide_plan_calendar(
    source_id: SourceId,
    action: Literal["approve", "reject", "withdraw"],
    body: DecisionBody,
    staff: CanReadPrices,
    pool: Pool,
) -> dict[str, Any]:
    """Permissions by action in the service: approve / reject season.approve (never the requester), withdraw the
    requester (season.write). A reason is required to reject or withdraw."""
    async with pool.acquire() as conn, conn.transaction():
        return await seasons_admin.decide_plan_calendar(conn, staff, source_id, action, body.reason)


@router.get("/booking/catalog/price-versions")
async def pending_versions(
    staff: CanReadPrices, pool: Pool, status: Annotated[Literal["pending"], Query()] = "pending"
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await price_versions.pending(conn, staff)


@router.get("/booking/catalog/price-versions/{version_id}")
async def get_version(version_id: VersionId, staff: CanReadPrices, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await price_versions.get_version(conn, staff, str(version_id))


@router.put("/booking/catalog/price-versions/{version_id}")
async def save_version(version_id: VersionId, body: DraftBody, staff: CanWriteVersions, pool: Pool) -> dict[str, Any]:
    data = body.model_dump()
    async with pool.acquire() as conn, conn.transaction():
        return await price_versions.save_draft(conn, staff, str(version_id), data)


@router.post("/booking/catalog/price-versions/{version_id}/submit")
async def submit_version(
    version_id: VersionId, body: SubmitBody, staff: CanWriteVersions, pool: Pool
) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await price_versions.submit(conn, staff, str(version_id), body.reason)


@router.post("/booking/catalog/price-versions/{version_id}/approve")
async def approve_version(
    version_id: VersionId, body: ApproveBody, staff: CanApproveVersions, pool: Pool
) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await price_versions.approve(conn, staff, str(version_id), body.diff_sha256)


@router.post("/booking/catalog/price-versions/{version_id}/reject")
async def reject_version(
    version_id: VersionId, body: ReasonBody, staff: CanApproveVersions, pool: Pool
) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await price_versions.reject(conn, staff, str(version_id), body.reason)


@router.get("/booking/catalog/price-versions/{version_id}/withdraw-preview")
async def withdraw_preview(version_id: VersionId, staff: CanReadPrices, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await price_versions.withdraw_preview(conn, staff, str(version_id))


@router.post("/booking/catalog/price-versions/{version_id}/withdraw")
async def withdraw_version(version_id: VersionId, body: ReasonBody, staff: CanReadPrices, pool: Pool) -> dict[str, Any]:
    """The maker withdraws a draft or pending version (price_version.write); a second person a published one
    (price_version.approve). The permission is checked by state in the service."""
    async with pool.acquire() as conn, conn.transaction():
        return await price_versions.withdraw(conn, staff, str(version_id), body.reason)


# ── Season calendars ─────────────────────────────────────────────────────────


class CalendarIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner_kind: Literal["destination", "supplier_contract"]
    destination_id: uuid.UUID | None = None
    supplier_contract_id: uuid.UUID | None = None
    name: str = Field(min_length=1, max_length=seasons_admin.NAME_MAX)


class RangeIn(BaseModel):
    """Fixed dates (inclusive), or for a festive range a holiday event (country + code, days before and after)."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["season", "stop_sale"] = "season"
    season_code: Season | None = None
    date_from: date | None = None
    date_to: date | None = None
    holiday_country: str | None = Field(default=None, pattern=r"^[A-Za-z]{2}$")
    holiday_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{1,39}$")
    days_before: int = Field(default=0, ge=0, le=60)
    days_after: int = Field(default=0, ge=0, le=60)
    note: str = Field(min_length=1, max_length=seasons_admin.REASON_MAX, description="why the range (required)")


class DefaultIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_season: Season
    reason: Reason


Action = Literal["approve", "reject", "withdraw", "end", "end_approve", "end_reject"]


@router.get("/booking/pricing/season-calendars")
async def list_calendars(
    staff: CanReadPrices,
    pool: Pool,
    owner_kind: Annotated[Literal["destination", "supplier_contract"] | None, Query()] = None,
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await seasons_admin.list_calendars(conn, staff, owner_kind)


@router.post("/booking/pricing/season-calendars", status_code=201)
async def create_calendar(body: CalendarIn, staff: CanWriteSeasons, pool: Pool) -> dict[str, Any]:
    data = {k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in body.model_dump().items()}
    async with pool.acquire() as conn, conn.transaction():
        return await seasons_admin.create_calendar(conn, staff, data)


@router.get("/booking/pricing/season-calendars/{calendar_id}")
async def get_calendar(
    calendar_id: Ident, staff: CanReadPrices, pool: Pool, year: Annotated[int | None, Query(ge=2000, le=2100)] = None
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await seasons_admin.get_calendar(conn, staff, str(calendar_id), year)


@router.post("/booking/pricing/season-calendars/{calendar_id}/ranges", status_code=201)
async def propose_range(calendar_id: Ident, body: RangeIn, staff: CanWriteSeasons, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await seasons_admin.propose_range(conn, staff, str(calendar_id), body.model_dump())


@router.post("/booking/pricing/season-calendars/{calendar_id}/default")
async def propose_default(calendar_id: Ident, body: DefaultIn, staff: CanWriteSeasons, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await seasons_admin.propose_default(conn, staff, str(calendar_id), body.default_season, body.reason)


@router.post("/booking/pricing/season-calendars/{calendar_id}/default/{decision}")
async def decide_default(
    calendar_id: Ident,
    decision: Literal["approve", "reject"],
    body: DecisionBody,
    staff: CanApproveSeasons,
    pool: Pool,
) -> dict[str, Any]:
    """A reject needs a reason."""
    async with pool.acquire() as conn, conn.transaction():
        return await seasons_admin.decide_default(
            conn, staff, str(calendar_id), approve=decision == "approve", reason=body.reason
        )


@router.post("/booking/pricing/season-ranges/{range_id}/{action}")
async def decide_range(
    range_id: Ident, action: Action, body: DecisionBody, staff: CanReadPrices, pool: Pool
) -> dict[str, Any]:
    """Permissions by action in the service: proposals (withdraw, end) season.write, decisions season.approve."""
    async with pool.acquire() as conn, conn.transaction():
        return await seasons_admin.decide_range(conn, staff, str(range_id), action, body.reason)


# ── Holiday events ───────────────────────────────────────────────────────────


class EventIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    country_iso2: str = Field(pattern=r"^[A-Za-z]{2}$")
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{1,39}$", description="the event's code, the same every year")
    name: str = Field(min_length=1, max_length=seasons_admin.NAME_MAX)
    date_from: date
    date_to: date
    reason: Reason


@router.get("/booking/pricing/holiday-events")
async def list_events(
    staff: CanReadPrices,
    pool: Pool,
    country: Annotated[str | None, Query(pattern=r"^[A-Za-z]{2}$")] = None,
    year: Annotated[int | None, Query(ge=2000, le=2100)] = None,
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await seasons_admin.list_events(conn, staff, country, year)


@router.post("/booking/pricing/holiday-events", status_code=201)
async def propose_event(body: EventIn, staff: CanWriteSeasons, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await seasons_admin.propose_event(conn, staff, body.model_dump())


@router.post("/booking/pricing/holiday-events/{event_id}/{action}")
async def decide_event(
    event_id: Ident, action: Action, body: DecisionBody, staff: CanReadPrices, pool: Pool
) -> dict[str, Any]:
    """Permissions by action in the service: proposals (withdraw, end) season.write, decisions season.approve."""
    async with pool.acquire() as conn, conn.transaction():
        return await seasons_admin.decide_event(conn, staff, str(event_id), action, body.reason)
