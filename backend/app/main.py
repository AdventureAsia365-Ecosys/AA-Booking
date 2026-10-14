"""FastAPI entrypoint: ``uvicorn app.main:app``.

Every public path is under ``/booking``: the shared ALB forwards ``/booking/*`` to this service and
the Booking REST API proxies ``/booking/{proxy+}`` (root ADR 0004 decision 5).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.db import Database
from app.routers import health
from app.settings import Settings, load_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


def create_app(settings: Settings | None = None, db: Database | None = None) -> FastAPI:
    settings = settings or load_settings()
    db = db or Database(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        await app.state.db.close()

    app = FastAPI(
        title="AA-Booking API",
        lifespan=lifespan,
        docs_url="/booking/docs",
        redoc_url=None,
        openapi_url="/booking/openapi.json",
    )
    app.state.settings = settings
    app.state.db = db
    app.include_router(health.router)
    return app


app = create_app()
