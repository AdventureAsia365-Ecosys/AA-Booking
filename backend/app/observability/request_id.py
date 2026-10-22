"""Request id middleware (AA-871).

Each HTTP request gets one id:

- the caller's ``X-Request-ID`` when it is well formed (API Gateway maps its ``context.requestId`` into it; a UUID);
- otherwise a new ``uuid4().hex``. A malformed header is replaced, never echoed or logged.

The id is bound to :mod:`structlog.contextvars` for the request (so every log line written while serving it carries
``request_id``), set as the Sentry tag ``request_id``, stored in ``request.state.request_id`` and returned as
``X-Request-ID`` on the response. One ``request_completed`` line per request records method, path, route template,
status and duration (no query string: it can hold search text).

Pure ASGI (not ``BaseHTTPMiddleware``): no extra task, no body buffering, and the contextvars are visible to the
endpoint.
"""

from __future__ import annotations

import logging
import re
import time
import uuid

import sentry_sdk
import structlog
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

HEADER = "X-Request-ID"
_HEADER_RAW = HEADER.lower().encode("latin-1")
_VALID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

log = logging.getLogger("app.request")


def new_request_id() -> str:
    return uuid.uuid4().hex


def accept_request_id(value: str | None) -> str | None:
    """The caller's id when it is safe to log and echo, else None."""
    if value and _VALID.match(value):
        return value
    return None


def _incoming(scope: Scope) -> str | None:
    for name, value in scope.get("headers") or ():
        if name == _HEADER_RAW:
            return accept_request_id(value.decode("latin-1").strip())
    return None


def request_id_of(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


class RequestIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _incoming(scope) or new_request_id()
        scope.setdefault("state", {})["request_id"] = request_id
        sentry_sdk.set_tag("request_id", request_id)
        status = 500
        started = time.perf_counter()

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)[HEADER] = request_id
            await send(message)

        with structlog.contextvars.bound_contextvars(request_id=request_id):
            try:
                await self.app(scope, receive, send_with_id)
            finally:
                route = scope.get("route")
                log.info(
                    "request_completed",
                    extra={
                        "method": scope.get("method"),
                        "path": scope.get("path"),
                        "route": getattr(route, "path", None),
                        "status": status,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                    },
                )


async def unhandled_error_handler(request: Request, exc: Exception) -> Response:
    """The unhandled-500 answer, with the request id (Starlette's ServerErrorMiddleware sends it outside every user
    middleware, so the header is added here). Same body as Starlette's default; the exception is re-raised by Starlette
    afterwards, so uvicorn and Sentry still see it."""
    headers = {HEADER: rid} if (rid := request_id_of(request)) else None
    return PlainTextResponse("Internal Server Error", status_code=500, headers=headers)
