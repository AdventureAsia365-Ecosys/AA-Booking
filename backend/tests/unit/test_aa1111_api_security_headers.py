"""AA-1111 (S284, ZAP baseline): nosniff + HSTS on every Booking API response, no Server header from uvicorn."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Response
from fastapi.testclient import TestClient

from app.main import create_app
from app.observability.request_id import RequestIdMiddleware
from app.observability.security_headers import SECURITY_HEADERS, SecurityHeadersMiddleware
from app.settings import Settings

SETTINGS = Settings(database_url=None, environment="test", git_sha="abc1234")
HSTS = "max-age=63072000; includeSubDomains"
BACKEND = Path(__file__).resolve().parents[2]


def _app():
    app = create_app(SETTINGS)
    probe = APIRouter()

    @probe.get("/booking/_own_headers")
    async def _own() -> Response:
        return Response("<p>x</p>", media_type="text/html", headers={"X-Content-Type-Options": "custom"})

    @probe.get("/booking/_boom")
    async def _boom() -> dict:
        raise RuntimeError("boom")

    app.include_router(probe)
    return app


def test_values() -> None:
    assert SECURITY_HEADERS == {"X-Content-Type-Options": "nosniff", "Strict-Transport-Security": HSTS}


def test_health_404_and_refusal_carry_the_headers() -> None:
    with TestClient(_app()) as client:
        for path in ("/booking/health", "/booking/no-such-path", "/booking/trip-cases"):
            r = client.get(path)
            assert r.headers["x-content-type-options"] == "nosniff", (path, r.status_code)
            assert r.headers["strict-transport-security"] == HSTS, (path, r.status_code)


def test_a_header_the_endpoint_set_wins() -> None:
    with TestClient(_app()) as client:
        r = client.get("/booking/_own_headers")
    assert r.headers["x-content-type-options"] == "custom"
    assert r.headers["strict-transport-security"] == HSTS


def test_unhandled_500_carries_the_headers() -> None:
    with TestClient(_app(), raise_server_exceptions=False) as client:
        r = client.get("/booking/_boom")
    assert r.status_code == 500
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["strict-transport-security"] == HSTS
    assert r.headers["x-request-id"]


def test_registered_just_inside_the_request_id() -> None:
    classes = [m.cls for m in create_app(SETTINGS).user_middleware]
    assert classes[:2] == [RequestIdMiddleware, SecurityHeadersMiddleware]


def test_image_starts_uvicorn_without_the_server_header() -> None:
    cmd = next(line for line in (BACKEND / "Dockerfile").read_text().splitlines() if line.startswith("CMD"))
    assert '"uvicorn"' in cmd and '"--no-server-header"' in cmd
