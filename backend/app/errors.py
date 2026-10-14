"""Service-layer errors and their HTTP mapping (registered on the app in ``app.main``)."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse


class ServiceError(Exception):
    status_code = 400

    def __init__(self, code: str, message: str = "", **extra: Any) -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.extra = extra

    def body(self) -> dict[str, Any]:
        return {"detail": self.code, "message": self.message, **self.extra}


class Forbidden(ServiceError):
    status_code = 403


class NotFound(ServiceError):
    status_code = 404


class Conflict(ServiceError):
    status_code = 409


class Invalid(ServiceError):
    status_code = 422


async def service_error_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ServiceError)  # noqa: S101 — registered for ServiceError only
    return JSONResponse(status_code=exc.status_code, content=exc.body())
