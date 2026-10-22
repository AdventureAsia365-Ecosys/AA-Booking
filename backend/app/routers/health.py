"""Liveness and readiness (root ADR 0004 decision 9).

- ``GET /booking/health``: process liveness, no DB call. The ALB target group checks this one,
  so a DB outage cannot trip the ECS circuit breaker into a rollback loop.
- ``GET /booking/health/ready``: DB reachable and the required ``bk-NNN`` migration registered.
  Used by the deploy smoke test and alarms.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.settings import SERVICE_NAME

router = APIRouter(prefix="/booking", tags=["health"])


@router.get("/health")
async def health(request: Request) -> dict[str, str]:
    settings = request.app.state.settings
    return {"status": "ok", "service": SERVICE_NAME, "version": settings.app_version, "git_sha": settings.git_sha}


@router.get("/health/ready")
async def ready(request: Request) -> JSONResponse:
    result = await request.app.state.db.readiness()
    return JSONResponse(status_code=200 if result.ok else 503, content=result.body)
