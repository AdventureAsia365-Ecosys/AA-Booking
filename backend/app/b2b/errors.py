"""Errors of the B2B API in the contract's ``Problem`` shape (``{"error": …, "message": …}``, booking.b2b.v1).

The staff API answers ``{"detail": …}`` (``app.errors``); the B2B API is a machine contract with its own error enum, so
it has its own exception and handlers. Request validation errors on ``/booking/b2b/v1`` paths are answered as
``invalid`` too (no field values echoed); every other path keeps FastAPI's default 422 body.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

PREFIX = "/booking/b2b/v1"

# Contract enum Problem.error -> HTTP status used by this service.
STATUS: dict[str, int] = {
    "unauthorized": 401,
    "forbidden": 403,
    "unknown_partner": 403,
    "not_found": 404,
    "price_changed": 409,
    "extend_limit": 409,
    "tour_not_served": 409,  # AA-986 L5 (ADR 0015 decision 5): the tour left the catalog (retired / paused)
    "hold_expired": 410,
    "invalid": 422,
    "sold_out": 422,
    "rehold_limit": 429,
    "unavailable": 503,
}
MAX_MESSAGE = 500


class B2BError(Exception):
    def __init__(self, error: str, message: str, **extra: Any) -> None:
        if error not in STATUS:
            raise ValueError(f"unknown B2B error code {error!r}")
        super().__init__(message)
        self.error = error
        self.message = message[:MAX_MESSAGE]
        self.extra = extra

    @property
    def status_code(self) -> int:
        return STATUS[self.error]

    def body(self) -> dict[str, Any]:
        return {"error": self.error, "message": self.message, **self.extra}


async def b2b_error_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, B2BError)  # noqa: S101 — registered for B2BError only
    headers = {"WWW-Authenticate": "Bearer"} if exc.error == "unauthorized" else None
    return JSONResponse(status_code=exc.status_code, content=exc.body(), headers=headers)


def _where(error: dict[str, Any]) -> str:
    loc = [str(p) for p in error.get("loc", ()) if p not in ("body",)]
    return ".".join(loc) or "request"


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """B2B paths: one ``invalid`` Problem naming the fields (never their values); elsewhere FastAPI's default."""
    assert isinstance(exc, RequestValidationError)  # noqa: S101 — registered for RequestValidationError only
    if not request.url.path.startswith(PREFIX):
        return await request_validation_exception_handler(request, exc)
    fields = sorted({_where(e) for e in exc.errors()})
    message = f"invalid request: {', '.join(fields)}"
    return JSONResponse(status_code=422, content={"error": "invalid", "message": message[:MAX_MESSAGE]})
