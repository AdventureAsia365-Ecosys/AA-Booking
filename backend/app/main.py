"""FastAPI entrypoint: ``uvicorn app.main:app``.

Every public path is under ``/booking``: the shared ALB forwards ``/booking/*`` to this service and
the Booking REST API proxies ``/booking/{proxy+}`` (root ADR 0004 decision 5).

Staff routes (everything under ``/booking`` except ``/booking/health*`` and ``/booking/ops/*``) verify a CIS staff JWT
against the CIS JWKS (``app.auth``, ADR 0004 O1) and require an active ``booking.staff_users`` row. The one machine
route, ``/booking/ops/deploy-gate``, accepts only the Deploy Dev workflow's GitHub OIDC token (``app.auth.ci``).

When ``WORKER_ENABLED`` is on, the lifespan also runs the ``booking.job`` worker loop
(``app.jobs.worker``) as one background task of this process — never from a request path — and, next to it, the
backlog metrics sampler (``app.jobs.metrics``, AA-751: ``AA/Jobs/Booking`` to CloudWatch every 5 min).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.auth.ci import GithubDeployVerifier
from app.auth.tokens import JwksCache, StaffTokenVerifier, http_fetcher
from app.db import Database
from app.errors import ServiceError, service_error_handler
from app.inquiry import intake
from app.jobs.metrics import JobMetricsSampler
from app.jobs.worker import Worker
from app.notifications import staff_email
from app.retention import jobs as retention_jobs
from app.routers import (
    catalog,
    customers,
    health,
    notifications,
    ops,
    pricing,
    reference,
    retention,
    staff,
    suppliers,
    supply,
    trip_cases,
)
from app.settings import Settings, load_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

# On shutdown a job in flight gets this long to finish; after that the worker task is cancelled and releases the
# job back to the queue at once (app.jobs.queue.release, AA-751), before the pool closes. ECS gives the container
# 30 s after SIGTERM (stopTimeout), so 20 s + the release UPDATE fit.
WORKER_STOP_TIMEOUT_S = 20.0


def create_app(
    settings: Settings | None = None,
    db: Database | None = None,
    verifier: StaffTokenVerifier | None = None,
    deploy_verifier: GithubDeployVerifier | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    db = db or Database(settings)
    verifier = verifier or StaffTokenVerifier(JwksCache(http_fetcher(settings.cis_jwks_url)), settings.staff_jwt_issuer)
    deploy_verifier = deploy_verifier or GithubDeployVerifier.default()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        stop = asyncio.Event()
        task: asyncio.Task[None] | None = None
        sampler_task: asyncio.Task[None] | None = None
        if settings.worker_enabled and db.configured:
            handlers = {
                **intake.HANDLERS,
                staff_email.KIND: staff_email.build_handler(settings),
                **retention_jobs.build_handlers(),
            }
            worker = Worker(
                db.pool,
                handlers,
                recurring=intake.RECURRING,
                hooks=(retention_jobs.DailySchedule(),),
                no_release=retention_jobs.NOT_RELEASED,
            )
            task = asyncio.create_task(worker.run_forever(stop), name="booking-job-worker")
            # Independent of the worker task (AA-751): a stuck worker loop must still show in the metrics.
            sampler_task = asyncio.create_task(JobMetricsSampler(db.pool).run_forever(stop), name="booking-job-metrics")
        yield
        stop.set()
        if sampler_task is not None:
            # Cancelled at once (a sample in flight is simply lost), before the pool closes.
            sampler_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sampler_task
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
    app.state.staff_verifier = verifier
    app.state.deploy_verifier = deploy_verifier
    app.add_exception_handler(ServiceError, service_error_handler)
    app.include_router(health.router)
    app.include_router(staff.router)
    app.include_router(trip_cases.router)
    app.include_router(customers.router)
    app.include_router(notifications.router)
    app.include_router(reference.router)
    app.include_router(pricing.router)
    app.include_router(suppliers.router)
    app.include_router(catalog.router)
    app.include_router(supply.router)
    app.include_router(retention.router)
    app.include_router(ops.router)
    return app


app = create_app()
