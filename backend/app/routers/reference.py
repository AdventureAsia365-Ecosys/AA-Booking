"""Read-only reference endpoints (settings rows, countries) and the admin audit log."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Any

import asyncpg
from fastapi import APIRouter, Depends, Query

from app import audit, reference
from app.auth.staff import Staff, current_staff, get_pool, require

router = APIRouter(prefix="/booking", tags=["reference"])

Pool = Annotated[asyncpg.Pool, Depends(get_pool)]
Me = Annotated[Staff, Depends(current_staff)]
CanReadAudit = Annotated[Staff, Depends(require("audit.read"))]


@router.get("/settings")
async def get_settings(_: Me, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await reference.list_settings(conn)


@router.get("/countries")
async def list_countries(_: Me, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return {"items": await reference.list_countries(conn)}


@router.get("/audit-logs")
async def list_audit_logs(
    _: CanReadAudit,
    pool: Pool,
    actor: Annotated[str | None, Query(max_length=128, description="CIS user id")] = None,
    entity_type: Annotated[str | None, Query(max_length=60)] = None,
    entity_id: Annotated[uuid.UUID | None, Query()] = None,
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to", description="inclusive")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before: Annotated[str | None, Query(max_length=100, description="next_before of the previous page")] = None,
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await audit.list_logs(
            conn,
            actor=actor,
            entity_type=entity_type,
            entity_id=str(entity_id) if entity_id else None,
            date_from=date_from,
            date_to=date_to,
            before=before,
            limit=limit,
        )
