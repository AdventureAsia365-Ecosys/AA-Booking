"""Staff notification feed (bell popover, /notifications) and read state. Any active staff member."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

import asyncpg
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.auth.staff import Staff, current_staff, get_pool
from app.notifications import service

router = APIRouter(prefix="/booking", tags=["notifications"])

Pool = Annotated[asyncpg.Pool, Depends(get_pool)]
Me = Annotated[Staff, Depends(current_staff)]


class ReadBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ids: list[uuid.UUID] | None = Field(default=None, max_length=500)
    all: bool = False

    @model_validator(mode="after")
    def _one(self) -> ReadBody:
        if self.all == bool(self.ids):
            raise ValueError("send ids or all=true")
        return self


@router.get("/notifications")
async def list_notifications(
    staff: Me,
    pool: Pool,
    unread: Annotated[bool | None, Query()] = None,
    type: Annotated[str | None, Query(max_length=60)] = None,  # noqa: A002 — the API field name
    q: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    before: Annotated[str | None, Query(max_length=100, description="next_before of the previous page")] = None,
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await service.list_feed(conn, staff, unread=unread, type=type, q=q, before=before, limit=limit)


@router.post("/notifications/read")
async def mark_notifications_read(body: ReadBody, staff: Me, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        return await service.mark_read(conn, staff, ids=[str(i) for i in body.ids] if body.ids else None, all_=body.all)
