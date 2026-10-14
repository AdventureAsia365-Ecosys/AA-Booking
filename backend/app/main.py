"""FastAPI entrypoint: ``uvicorn app.main:app``.

Every public path is under ``/booking``: the shared ALB forwards ``/booking/*`` to this service and
the Booking REST API proxies ``/booking/{proxy+}`` (root ADR 0004 decision 5).

When ``WORKER_ENABLED`` is on, the lifespan also runs the ``booking.job`` worker loop
(``app.jobs.worker``) as one background task of this process — never from a request path.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.db import Database
from app.inquiry import intake
from app.jobs.worker import Worker
from app.routers import health
from app.settings import Settings, load_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

# A job in flight finishes (each handler is one short transaction) or is re-queued by the reaper
# when its lease expires; ECS gives the container 30 s after SIGTERM.
WORKER_STOP_TIMEOUT_S = 20.0


def create_app(settings: Settings | None = None, db: Database | None = None) -> FastAPI:
    settings = settings or load_settings()
    db = db or Database(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        stop = asyncio.Event()
        task: asyncio.Task[None] | None = None
        if settings.worker_enabled and db.configured:
            worker = Worker(db.pool, intake.HANDLERS, recurring=intake.RECURRING)
            task = asyncio.create_task(worker.run_forever(stop), name="booking-job-worker")
        yield
        stop.set()
        if task is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(task, timeout=WORKER_STOP_TIMEOUT_S)
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
