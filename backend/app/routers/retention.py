"""Retention admin endpoints (AA-825; data standards DM-6.6, DM-7.2). Booking admins only (``retention.admin``).

- ``GET /booking/retention``: per kind the policy, the latest active / live / dry_run / held / failed run and what the
  next scheduled run will do (``scheduled_mode``).
- ``POST /booking/retention/runs`` ``{kind, dry_run = true, max_rows, older_than}``: queue one manual run. ``dry_run``
  defaults to true (counts only). A live run (``dry_run: false``) needs ``max_rows``, the count approved from a DRY RUN
  (``skill/aa-cis-schema/references/destructive-ops.md``). ``older_than`` (a UTC date) makes it a ``manual_override``
  run: that date replaces every part's window (Dev / test / local only, at least ``OVERRIDE_MIN_AGE_DAYS`` days ago,
  a live one needs ``max_rows <= OVERRIDE_MAX_ROWS``; refused with 422 otherwise). The request itself is one audit
  row.
- ``GET /booking/retention/runs/{job_id}``: the queued / finished job with its result (counts per part, archive keys).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal

import asyncpg
from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app import audit
from app.auth.staff import Staff, get_pool, require
from app.errors import Invalid, NotFound
from app.retention import jobs

router = APIRouter(prefix="/booking/retention", tags=["retention"])

Pool = Annotated[asyncpg.Pool, Depends(get_pool)]
CanAdmin = Annotated[Staff, Depends(require("retention.admin"))]


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["bk_retention_job", "bk_retention_inbox_redact"]
    dry_run: bool = True
    max_rows: int | None = Field(default=None, ge=1, le=jobs.RUN_MAX_ROWS)
    older_than: date | None = None

    @model_validator(mode="after")
    def live_needs_max_rows(self) -> RunRequest:
        if not self.dry_run and self.max_rows is None:
            raise ValueError("a live run (dry_run false) needs max_rows: the approved count from a DRY RUN")
        return self


@router.get("")
async def retention_status(_: CanAdmin, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await jobs.status(conn)


def _check_override(body: RunRequest, environment: str) -> None:
    """The same checks the job makes (it checks again with the database's date), answered at once as 422."""
    payload: dict[str, Any] = {
        "trigger": jobs.OVERRIDE_TRIGGER,
        "older_than": body.older_than.isoformat() if body.older_than else None,
        "dry_run": body.dry_run,
        "max_rows": body.max_rows,
    }
    try:
        jobs.override_cutoff(body.kind, payload, datetime.now(UTC).date(), environment)
        if not body.dry_run:
            jobs.override_max_rows(payload)
    except jobs.OverrideRefused as exc:
        raise Invalid("retention_override_refused", str(exc)) from None


@router.post("/runs", status_code=status.HTTP_202_ACCEPTED)
async def queue_run(body: RunRequest, staff: CanAdmin, pool: Pool, request: Request) -> dict[str, Any]:
    if body.older_than is not None:
        _check_override(body, request.app.state.settings.environment)
    max_rows = None if body.dry_run else body.max_rows
    async with pool.acquire() as conn, conn.transaction():
        job_id = await jobs.enqueue_manual(
            conn,
            body.kind,
            dry_run=body.dry_run,
            max_rows=max_rows,
            requested_by=f"staff:{staff.cis_user_id}",
            older_than=body.older_than,
        )
        what = {"kind": body.kind, "dry_run": body.dry_run, "max_rows": max_rows}
        if body.older_than is not None:
            what["older_than"] = body.older_than.isoformat()
        await audit.record(
            conn,
            actor_type="staff",
            actor_ref=staff.cis_user_id,
            action="retention.requested",
            entity_type=jobs.AUDIT_ENTITY,
            entity_id=job_id,
            after=what,
            entity_label=body.kind,
            summary=("DRY RUN" if body.dry_run else f"live run, max_rows {body.max_rows}")
            + f" of {body.kind}"
            + (f", override older_than {body.older_than.isoformat()}" if body.older_than else ""),
        )
    return {"job_id": job_id, "status": "queued", **what}


@router.get("/runs/{job_id}")
async def get_run(job_id: uuid.UUID, _: CanAdmin, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        detail = await jobs.job_detail(conn, str(job_id))
    if detail is None:
        raise NotFound("retention_run_not_found")
    return detail
