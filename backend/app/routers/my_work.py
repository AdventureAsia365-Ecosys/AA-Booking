"""Staff My work band (AA-1159, ``app.my_work``).

- ``GET /my-work/summary``: one tile per work queue the caller's capabilities allow (count, oldest / earliest due, the
  target list's filter). Any staff role holding at least one tile capability; none is 403 ``forbidden``. Read-only, so a
  preview or E2E session reads it with its capped permissions.
"""

from __future__ import annotations

from typing import Annotated, Any

import asyncpg
from fastapi import APIRouter, Depends

from app import my_work
from app.auth.staff import Staff, current_staff, get_pool

router = APIRouter(prefix="/booking", tags=["my-work"])

Pool = Annotated[asyncpg.Pool, Depends(get_pool)]
Caller = Annotated[Staff, Depends(current_staff)]


@router.get("/my-work/summary")
async def my_work_summary(staff: Caller, pool: Pool) -> dict[str, Any]:
    my_work.require_any(staff)
    async with pool.acquire() as conn:
        return await my_work.summary(conn, staff)
