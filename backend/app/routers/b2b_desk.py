"""Staff B2B desk ``/booking/b2b-desk`` (bk-022, AA-975): on-request lines and departure incidents (``app.b2b.desk``).

Read ``b2b_desk.read``, write ``b2b_desk.write`` (admin, sales, contracting). Staff tokens only; the agencies see the
outcome as events on ``GET /booking/b2b/v1/events``.

- ``GET /summary``: open / overdue / due-soon on-request lines and open incidents (page header, dashboard).
- ``GET /on-request?state=open|answered|all``: lines of confirmed B2B bookings held on request, with the SLA badge.
- ``POST /on-request/{hold_id}/lines/{line_ref}/accept`` and ``…/decline`` (``decline_code``).
- ``GET /departures?from&to``: departures with seats, the agencies on them and the open incident.
- ``GET /departures/{id}/incidents``, ``POST /departures/{id}/incidents`` (``incident_code``) = "Flag incident on
  departure"; ``POST /incidents/{id}/clear``.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Any, Literal

import asyncpg
from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel, ConfigDict

from app.auth.staff import Staff, get_pool, require
from app.b2b import desk

router = APIRouter(prefix="/booking/b2b-desk", tags=["b2b"])

Pool = Annotated[asyncpg.Pool, Depends(get_pool)]
CanRead = Annotated[Staff, Depends(require("b2b_desk.read"))]
CanWrite = Annotated[Staff, Depends(require("b2b_desk.write"))]
LineRef = Annotated[str, Path(min_length=1, max_length=64)]


class DeclineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decline_code: Literal["sold_out", "supplier_unavailable", "minimum_not_met", "other"]


class IncidentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident_code: Literal["weather", "road", "health", "political", "supplier", "other"]


@router.get("/summary")
async def get_summary(_: CanRead, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await desk.summary(conn)


@router.get("/on-request")
async def get_on_request(
    _: CanRead,
    pool: Pool,
    state: Literal["open", "answered", "all"] = "open",
    limit: Annotated[int, Query(ge=1, le=desk.MAX_PAGE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await desk.list_on_request(conn, state=state, limit=limit, offset=offset)


@router.post("/on-request/{hold_id}/lines/{line_ref}/accept")
async def accept_line(hold_id: Annotated[uuid.UUID, Path()], line_ref: LineRef, staff: CanWrite, pool: Pool) -> dict:
    async with pool.acquire() as conn, conn.transaction():
        return await desk.answer_line(conn, staff, str(hold_id), line_ref, accept=True)


@router.post("/on-request/{hold_id}/lines/{line_ref}/decline")
async def decline_line(
    body: DeclineIn, hold_id: Annotated[uuid.UUID, Path()], line_ref: LineRef, staff: CanWrite, pool: Pool
) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await desk.answer_line(conn, staff, str(hold_id), line_ref, accept=False, decline_code=body.decline_code)


@router.get("/departures")
async def get_departures(
    _: CanRead,
    pool: Pool,
    start: Annotated[date | None, Query(alias="from")] = None,
    end: Annotated[date | None, Query(alias="to")] = None,
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await desk.list_departures(conn, start=start, end=end)


@router.get("/departures/{departure_id}/incidents")
async def get_incidents(departure_id: Annotated[uuid.UUID, Path()], _: CanRead, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await desk.list_incidents(conn, str(departure_id))


@router.post("/departures/{departure_id}/incidents", status_code=201)
async def flag_incident(
    body: IncidentIn, departure_id: Annotated[uuid.UUID, Path()], staff: CanWrite, pool: Pool
) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await desk.flag_incident(conn, staff, str(departure_id), body.incident_code)


@router.post("/incidents/{incident_id}/clear")
async def clear_incident(incident_id: Annotated[uuid.UUID, Path()], staff: CanWrite, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await desk.clear_incident(conn, staff, str(incident_id))
