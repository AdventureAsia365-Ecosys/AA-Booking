"""Read-only "view as a lower role" for an owner (AA-1157; :mod:`app.auth.view_as`).

- ``POST /booking/view-as``                      — start: ``{role, minutes 5-60, reason?}``; owner only (403
  ``owner_only``), a built-in or active custom role (404 ``view_as_unknown_role``), downward only (403
  ``view_as_not_downward``). Ends the caller's other open views, writes one ``staff.view_as_started`` audit row with the
  role's full capability list. Answers the session id, which the staff BFF keeps in an httpOnly cookie.
- ``GET  /booking/view-as``                      — the view this request runs in (``active`` false outside one).
  Outside a view it first ends the caller's own sessions that are past their expiry (one ``staff.view_as_ended`` row
  each, reason ``expired``): the banner's check at 0 minutes and the Roles page call it. The session id is never in
  the answer (the BFF holds it).
- ``POST /booking/view-as/{session_id}/end``     — end the caller's own view (idempotent; the BFF sends it WITHOUT the
  view header, as the owner). One ``staff.view_as_ended`` row.
- ``POST /booking/view-as/end-all``              — end every open view of the caller (the BFF's sign-out).

Under ``/booking/view-as`` (not ``/booking/staff/...``): ``GET /booking/staff/{cis_user_id}`` would capture a literal.
"""

from __future__ import annotations

from typing import Annotated, Any

import asyncpg
from fastapi import APIRouter, Depends, Path, Request
from pydantic import BaseModel, ConfigDict, Field

from app.auth import view_as
from app.auth.staff import Staff, current_staff, get_pool

router = APIRouter(prefix="/booking/view-as", tags=["staff"])

Pool = Annotated[asyncpg.Pool, Depends(get_pool)]
Me = Annotated[Staff, Depends(current_staff)]
SessionId = Annotated[str, Path(pattern=r"^[0-9a-fA-F-]{36}$")]


class StartView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str = Field(min_length=2, max_length=60)
    minutes: int = Field(default=view_as.DEFAULT_MINUTES, ge=view_as.MIN_MINUTES, le=view_as.MAX_MINUTES)
    reason: str | None = Field(default=None, max_length=500)


@router.post("", status_code=201)
async def start_view(body: StartView, request: Request, staff: Me, pool: Pool) -> dict[str, Any]:
    sub = staff.cognito_sub
    reason = (body.reason or "").strip() or None
    return await view_as.start(pool, request.app, staff, sub, body.role, body.minutes, reason)


@router.get("")
async def current_view(staff: Me, pool: Pool) -> dict[str, Any]:
    if staff.view_as is None:
        await view_as.close_expired(pool, staff)
        return {"active": False}
    return {"active": True, "read_only": True, **staff.view_as.public()}


@router.post("/end-all")
async def end_all(staff: Me, pool: Pool) -> dict[str, Any]:
    return {"ended": await view_as.end_all(pool, staff)}


@router.post("/{session_id}/end")
async def end_view(session_id: SessionId, staff: Me, pool: Pool) -> dict[str, Any]:
    return await view_as.end(pool, staff, session_id.lower())
