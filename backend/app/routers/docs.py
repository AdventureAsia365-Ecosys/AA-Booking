"""API docs on the public host, gated (AA-876, the CIS AA-577 pattern).

FastAPI's own ``/booking/docs`` and ``/booking/openapi.json`` were public on ``api-booking``: anyone got the full
route list and schemas for free. ``create_app`` turns them off (``docs_url=None, openapi_url=None``) and this
router serves them by hand behind ``require_docs_access``:

- ``ENVIRONMENT`` ``test`` / ``local`` (``INSECURE_ENVIRONMENTS``): open, for local work and CI.
- Anywhere else (Dev on ECS included): a staff Bearer token with ``staff.admin``. Every refusal — no token, bad
  token, auth unavailable, no Booking role, inactive or not an admin — answers the same **403** ``docs_forbidden``,
  so the docs say nothing about why. Verifying the token goes through ``current_staff``, which may provision or
  sync the caller's ``booking.staff_users`` row (the same write any staff request makes).

The Swagger page loads the schema with a plain browser fetch (no Bearer), so on Dev the docs are for ``curl`` with
a token; the staff admin UI does not link them.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials

from app.auth.staff import bearer, current_staff
from app.settings import INSECURE_ENVIRONMENTS

OPENAPI_PATH = "/booking/openapi.json"
DOCS_PATH = "/booking/docs"

router = APIRouter(include_in_schema=False)


async def require_docs_access(
    request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]
) -> None:
    if request.app.state.settings.environment in INSECURE_ENVIRONMENTS:
        return
    try:
        staff = await current_staff(request, credentials)
    except HTTPException as exc:
        raise HTTPException(status_code=403, detail="docs_forbidden") from exc
    if not staff.can("staff.admin"):
        raise HTTPException(status_code=403, detail="docs_forbidden")


@router.get(OPENAPI_PATH, dependencies=[Depends(require_docs_access)])
async def openapi_json(request: Request) -> JSONResponse:
    schema: dict[str, Any] = request.app.openapi()
    return JSONResponse(schema)


@router.get(DOCS_PATH, dependencies=[Depends(require_docs_access)])
async def swagger_ui(request: Request) -> HTMLResponse:
    return get_swagger_ui_html(openapi_url=OPENAPI_PATH, title=f"{request.app.title} — docs")
