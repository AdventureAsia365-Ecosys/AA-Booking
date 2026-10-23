"""FastAPI entrypoint: ``uvicorn app.main:app``.

Every public path is under ``/booking``: the shared ALB forwards ``/booking/*`` to this service and
the Booking REST API proxies ``/booking/{proxy+}`` (root ADR 0004 decision 5).

Staff routes (everything under ``/booking`` except ``/booking/health*`` and ``/booking/ops/*``) verify a Cognito
WORKFORCE access token (clients booking-staff / breakglass) or, during the transition, the CIS bridge token, with the
multi-issuer verifier (``app.auth.identity_config``, AA-680 Phase 2), and require an active ``booking.staff_users``
row. The lifespan prefetches both issuers' JWKS in the background. The one machine
route, ``/booking/ops/deploy-gate``, accepts only the Deploy Dev workflow's GitHub OIDC token (``app.auth.ci``).
The B2B API ``/booking/b2b/v1`` (AA-972, contract booking.b2b.v1) accepts only aa-m2m client-credentials tokens
(``app.auth.m2m``, its own verifier; none when the M2M settings are unset, then every B2B route answers 503) and
answers errors in the contract's Problem shape (``app.b2b.errors``).

When ``WORKER_ENABLED`` is on, the lifespan also runs the ``booking.job`` worker loop
(``app.jobs.worker``) as one background task of this process — never from a request path — and, next to it, the
backlog metrics sampler (``app.jobs.metrics``, AA-751: ``AA/Jobs/Booking`` to CloudWatch every 5 min).

Observability (AA-871, ``app.observability``): JSON log lines on stdout (stdlib and structlog calls alike), a request id
per request (``X-Request-ID`` in, or generated; echoed on every response and bound to every log line), and Sentry when
``SENTRY_DSN`` is set. :func:`build_app` sets logging and Sentry up once per process; :func:`create_app` only adds the
middleware, so tests that build their own app never touch either.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from app.auth import identity_config, m2m
from app.auth.ci import GithubDeployVerifier
from app.auth.identity_verifier import MultiIssuerVerifier
from app.b2b import expiry as b2b_expiry
from app.b2b.errors import B2BError, b2b_error_handler, validation_error_handler
from app.db import Database
from app.errors import ServiceError, service_error_handler
from app.inquiry import intake
from app.jobs.metrics import JobMetricsSampler
from app.jobs.worker import Worker
from app.notifications import staff_email
from app.observability import setup as setup_observability
from app.observability.request_id import RequestIdMiddleware, unhandled_error_handler
from app.retention import jobs as retention_jobs
from app.routers import (
    b2b,
    b2b_partners,
    catalog,
    customers,
    docs,
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

# On shutdown a job in flight gets this long to finish; after that the worker task is cancelled and releases the
# job back to the queue at once (app.jobs.queue.release, AA-751), before the pool closes. ECS gives the container
# 30 s after SIGTERM (stopTimeout), so 20 s + the release UPDATE fit.
WORKER_STOP_TIMEOUT_S = 20.0


def create_app(
    settings: Settings | None = None,
    db: Database | None = None,
    verifier: MultiIssuerVerifier | None = None,
    deploy_verifier: GithubDeployVerifier | None = None,
    m2m_verifier: MultiIssuerVerifier | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    db = db or Database(settings)
    verifier = verifier or identity_config.build_staff_verifier(settings)
    deploy_verifier = deploy_verifier or GithubDeployVerifier.default()
    m2m_verifier = m2m_verifier or m2m.build_m2m_verifier(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        stop = asyncio.Event()
        task: asyncio.Task[None] | None = None
        sampler_task: asyncio.Task[None] | None = None
        # Off when ENVIRONMENT=test (no network in TestClient runs); never delays start-up, never raises.
        prefetch_task = identity_config.start_staff_jwks_prefetch(verifier, settings)
        m2m_prefetch_task = (
            identity_config.start_staff_jwks_prefetch(m2m_verifier, settings) if m2m_verifier is not None else None
        )
        if settings.worker_enabled and db.configured:
            handlers = {
                **intake.HANDLERS,
                staff_email.KIND: staff_email.build_handler(settings),
                **retention_jobs.build_handlers(),
                **b2b_expiry.HANDLERS,
            }
            worker = Worker(
                db.pool,
                handlers,
                recurring=intake.RECURRING,
                hooks=(retention_jobs.DailySchedule(), b2b_expiry.ExpirySchedule()),
                no_release=retention_jobs.NOT_RELEASED,
            )
            task = asyncio.create_task(worker.run_forever(stop), name="booking-job-worker")
            # Independent of the worker task (AA-751): a stuck worker loop must still show in the metrics.
            sampler_task = asyncio.create_task(JobMetricsSampler(db.pool).run_forever(stop), name="booking-job-metrics")
        yield
        stop.set()
        await identity_config.stop_staff_jwks_prefetch(prefetch_task)
        await identity_config.stop_staff_jwks_prefetch(m2m_prefetch_task)
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
        # AA-876: FastAPI's own docs routes are off; app.routers.docs serves both paths behind a staff-admin gate.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.db = db
    app.state.staff_verifier = verifier
    app.state.deploy_verifier = deploy_verifier
    app.state.m2m_verifier = m2m_verifier
    app.add_exception_handler(ServiceError, service_error_handler)
    app.add_exception_handler(B2BError, b2b_error_handler)
    # B2B paths answer validation errors as the contract's Problem; every other path keeps FastAPI's default body.
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    # AA-871: the unhandled 500 keeps the request id (it is answered outside the middleware below).
    app.add_exception_handler(Exception, unhandled_error_handler)
    app.add_middleware(RequestIdMiddleware)
    app.include_router(health.router)
    app.include_router(docs.router)
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
    app.include_router(b2b.router)
    app.include_router(b2b_partners.router)
    return app


def build_app() -> FastAPI:
    """The process's app (``uvicorn app.main:app``): settings from the environment, JSON logging and Sentry, then
    :func:`create_app`."""
    settings = load_settings()
    setup_observability(settings)
    return create_app(settings)


app = build_app()
