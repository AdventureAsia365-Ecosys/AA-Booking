"""Retention admin endpoints (AA-825; data standards DM-6.6, DM-7.2). Booking admins only (``retention.admin``).

- ``GET /booking/retention``: per kind the policy, the latest active / live / dry_run / held / failed run and what the
  next scheduled run will do (``scheduled_mode``).
- ``POST /booking/retention/runs`` ``{kind, dry_run = true, max_rows}``: queue one manual run. ``dry_run`` defaults
  to true (counts only). A live run (``dry_run: false``) needs ``max_rows``, the count approved from a DRY RUN
  (``skill/aa-cis-schema/references/destructive-ops.md``). The request itself is one audit row.
- ``GET /booking/retention/runs/{job_id}``: the queued / finished job with its result (counts per part, archive keys).
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

import asyncpg
from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app import audit
from app.auth.staff import Staff, get_pool, require
from app.errors import NotFound
from app.retention import jobs

router = APIRouter(prefix="/booking/retention", tags=["retention"])

Pool = Annotated[asyncpg.Pool, Depends(get_pool)]
CanAdmin = Annotated[Staff, Depends(require("retention.admin"))]


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["bk_retention_job", "bk_retention_inbox_redact"]
    dry_run: bool = True
    max_rows: int | None = Field(default=None, ge=1, le=jobs.RUN_MAX_ROWS)

    @model_validator(mode="after")
    def live_needs_max_rows(self) -> RunRequest:
        if not self.dry_run and self.max_rows is None:
            raise ValueError("a live run (dry_run false) needs max_rows: the approved count from a DRY RUN")
        return self


@router.get("")
async def retention_status(_: CanAdmin, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await jobs.status(conn)


@router.post("/runs", status_code=status.HTTP_202_ACCEPTED)
async def queue_run(body: RunRequest, staff: CanAdmin, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction():
        job_id = await jobs.enqueue_manual(
            conn,
            body.kind,
            dry_run=body.dry_run,
            max_rows=None if body.dry_run else body.max_rows,
            requested_by=f"staff:{staff.cis_user_id}",
        )
        request = {"kind": body.kind, "dry_run": body.dry_run, "max_rows": None if body.dry_run else body.max_rows}
        await audit.record(
            conn,
            actor_type="staff",
            actor_ref=staff.cis_user_id,
            action="retention.requested",
            entity_type=jobs.AUDIT_ENTITY,
            entity_id=job_id,
            after=request,
            entity_label=body.kind,
            summary=("DRY RUN" if body.dry_run else f"live run, max_rows {body.max_rows}") + f" of {body.kind}",
        )
    return {"job_id": job_id, "status": "queued", **request}


@router.get("/runs/{job_id}")
async def get_run(job_id: uuid.UUID, _: CanAdmin, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        detail = await jobs.job_detail(conn, str(job_id))
    if detail is None:
        raise NotFound("retention_run_not_found")
    return detail
