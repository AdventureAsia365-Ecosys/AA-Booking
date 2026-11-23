"""Service-layer errors and their HTTP mapping (registered on the app in ``app.main``)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
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


class BadRequest(ServiceError):
    """A request the rules refuse outright (AA-1155 T6: an approve-class or unknown capability on a custom role)."""

    status_code = 400


class Forbidden(ServiceError):
    status_code = 403


class NotFound(ServiceError):
    status_code = 404


class Conflict(ServiceError):
    status_code = 409


class Gone(ServiceError):
    """The feature was switched off for good (e.g. the B2C intake under root ADR 0019, ``app.b2c``)."""

    status_code = 410


class Invalid(ServiceError):
    status_code = 422


class Unavailable(ServiceError):
    status_code = 503


async def service_error_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ServiceError)  # noqa: S101 — registered for ServiceError only
    return JSONResponse(status_code=exc.status_code, content=exc.body())


# ── Routes whose body carries secrets (AA-1158 part 2 review: supplier bank details) ────────────────────────────


def redact_validation[F: Callable[..., Any]](endpoint: F) -> F:
    """Mark a route whose request body carries secrets (bank details, a call-back note): its 422 names the fields and
    the error types only, never the submitted value (FastAPI's default body echoes ``input`` and ``ctx``). Put it
    under the ``@router.<method>`` decorator."""
    endpoint.redact_validation = True  # type: ignore[attr-defined]
    return endpoint


def redacts_validation(request: Request) -> bool:
    """The matched route's endpoint carries :func:`redact_validation`."""
    endpoint = getattr(request.scope.get("route"), "endpoint", None) or request.scope.get("endpoint")
    return bool(getattr(endpoint, "redact_validation", False))


def redacted_validation(exc: RequestValidationError) -> JSONResponse:
    """``{"detail": [{"loc", "type", "msg"}]}``: the shape the staff admin reads, ``msg`` the error type itself (a
    pydantic message may quote a pattern or a limit, never the input; the type is enough and is never data)."""
    detail = []
    for error in exc.errors():
        kind = str(error.get("type") or "invalid")
        detail.append({"loc": [str(p) for p in error.get("loc", ())], "type": kind, "msg": kind})
    return JSONResponse(status_code=422, content={"detail": detail})
