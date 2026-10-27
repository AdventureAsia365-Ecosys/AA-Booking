"""Staff trip checks and the case lifecycle runs (bk-027; AA-682 slice S2: ``app.ops.trip_checks``,
``app.ops.lifecycle``).

Read ``ops.read``, write ``ops.write`` (admin, sales, contracting). Staff tokens only (never M2M); preview and E2E
sessions stay read-only by the global rule in ``app.auth.staff``.

- ``GET /trip-checks`` (``status``, ``due_before``, ``assignee``, ``code``, ``case``, ``q``): checks across the open
  sold cases, soonest due first (the /trip-checks page).
- ``GET /trip-cases/{ref}/checks``: one case's checks, board counts and the derived checks (suppliers, payment).
- ``POST /trip-cases/{ref}/checks``: add a custom check.
- ``PATCH /trip-checks/{id}``: status, assignee, due date, note, custom title, emergency contact (B2C only).
- ``GET /case-lifecycle/runs``: the latest ``bk_case_lifecycle`` / ``bk_ops_reminders`` runs (mode, moves, counts).
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel, ConfigDict, Field

from app.auth.staff import Staff, get_pool, require
from app.ops import lifecycle
from app.ops import trip_checks as service

router = APIRouter(prefix="/booking", tags=["operations"])

Pool = Annotated[asyncpg.Pool, Depends(get_pool)]
CanRead = Annotated[Staff, Depends(require("ops.read"))]
CanWrite = Annotated[Staff, Depends(require("ops.write"))]
CaseRef = Annotated[str, Path(min_length=1, max_length=64, description="Trip case UUID or code (TC-000123)")]
CheckId = Annotated[UUID, Path(description="trip check UUID")]
StaffKey = Annotated[str, Field(min_length=1, max_length=200)]


class NewCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    due_date: date | None = None
    assignee_ref: StaffKey | None = None
    note: str | None = Field(default=None, max_length=2000)


class CheckChanges(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["open", "done", "not_applicable"] | None = None
    title: str | None = Field(default=None, min_length=1, max_length=200)
    due_date: date | None = None
    assignee_ref: StaffKey | None = None
    note: str | None = Field(default=None, max_length=2000)
    emergency_contact_name: str | None = Field(default=None, max_length=200)
    emergency_contact_phone: str | None = Field(default=None, pattern=r"^\+?[0-9][0-9 ().-]{3,38}[0-9]$")


@router.get("/trip-checks")
async def list_checks(
    staff: CanRead,
    pool: Pool,
    status: Annotated[Literal["open", "done", "not_applicable", "all"], Query()] = "open",
    due_before: Annotated[date | None, Query(description="due on or before this date")] = None,
    assignee: Annotated[str | None, Query(max_length=200, description="staff key, me or none")] = None,
    code: Annotated[Literal["pre_trip_recheck", "emergency_contact", "insurance", "custom"] | None, Query()] = None,
    case: Annotated[str | None, Query(max_length=64, description="trip case code or UUID")] = None,
    q: Annotated[str | None, Query(max_length=100, description="case code or check title")] = None,
    limit: Annotated[int, Query(ge=1, le=service.MAX_PAGE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await service.list_checks(
            conn,
            staff,
            status=status,
            due_before=due_before,
            assignee=assignee,
            code=code,
            case=case,
            q=q,
            limit=limit,
            offset=offset,
        )


@router.get("/trip-cases/{ref}/checks")
async def case_checks(ref: CaseRef, staff: CanRead, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await service.list_for_case(conn, staff, ref)


@router.post("/trip-cases/{ref}/checks", status_code=201)
async def add_check(body: NewCheck, ref: CaseRef, staff: CanWrite, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await service.create_custom(conn, staff, ref, body.model_dump())


@router.patch("/trip-checks/{check_id}")
async def patch_check(body: CheckChanges, check_id: CheckId, staff: CanWrite, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await service.update(conn, staff, str(check_id), body.model_dump(exclude_unset=True))


@router.get("/case-lifecycle/runs")
async def lifecycle_runs(_: CanRead, pool: Pool, limit: Annotated[int, Query(ge=1, le=50)] = 20) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await lifecycle.recent_runs(conn, limit)
