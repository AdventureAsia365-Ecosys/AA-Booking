"""Liveness and readiness (root ADR 0004 decision 9).

- ``GET /booking/health``: process liveness, no DB call. The ALB target group checks this one,
  so a DB outage cannot trip the ECS circuit breaker into a rollback loop.
- ``GET /booking/health/ready``: DB reachable and the required ``bk-NNN`` migration registered.
  Used by the deploy smoke test and alarms.

Both bodies are declared as models (AA-899) so the exported OpenAPI document (``docs/api/openapi.json``) names every
field and the breaking-change check sees a removed one. ``/health/ready`` still answers with ``JSONResponse`` (200 or
503, the same body shape); the model only documents it.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.settings import SERVICE_NAME

router = APIRouter(prefix="/booking", tags=["health"])


class Health(BaseModel):
    status: str
    service: str
    version: str
    git_sha: str


class Readiness(BaseModel):
    """``status`` ``ok`` / ``not_ready``; ``db`` ``ok`` / ``error`` / ``not_configured``. ``error`` is the exception
    class name only; the last two fields appear when a required ``bk-NNN`` migration is not registered."""

    status: str
    db: str
    schema_version: str | None
    error: str | None = None
    required_schema_version: str | None = None
    missing_schema_versions: list[str] | None = None


@router.get("/health", response_model=Health)
async def health(request: Request) -> dict[str, str]:
    settings = request.app.state.settings
    return {"status": "ok", "service": SERVICE_NAME, "version": settings.app_version, "git_sha": settings.git_sha}


@router.get(
    "/health/ready",
    response_model=None,
    responses={200: {"model": Readiness}, 503: {"model": Readiness, "description": "Not ready"}},
)
async def ready(request: Request) -> JSONResponse:
    result = await request.app.state.db.readiness()
    return JSONResponse(status_code=200 if result.ok else 503, content=result.body)
