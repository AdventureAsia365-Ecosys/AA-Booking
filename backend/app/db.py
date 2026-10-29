"""Database access: a lazy asyncpg pool and the readiness probe.

The pool is created on first use, not at startup, so the process (and the ALB health check on
``/booking/health``) stays up while the database is unreachable (root ADR 0004 decision 9).
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import asyncpg

from app.settings import Settings

log = logging.getLogger(__name__)

# Every bk-NNN migration this code needs. Readiness fails until ALL of them are registered: checking only the
# newest one would pass with a gap (bk-005 applied, an older file missing) and the API would then fail per call.
REQUIRED_SCHEMA_VERSIONS: tuple[str, ...] = (
    "bk-001",
    "bk-002",
    "bk-003",
    "bk-004",
    "bk-005",
    "bk-006",
    "bk-007",
    "bk-008",
    "bk-009",
    "bk-010",
    "bk-011",
    "bk-012",
    "bk-013",
    "bk-016",  # AA-680 Phase 2: staff_users.cognito_iss / cognito_sub (bk-014 / bk-015 change no shape this code reads)
    "bk-019",  # AA-972: b2b_partners, b2b_quotes + lines, trip_plan_components (bk-017 / bk-018: no shape read here)
    "bk-020",  # AA-973: departures, b2b_holds + lines, quote line departure_id, B2B trip case columns
    "bk-021",  # AA-974: cancellation_policies, b2b_cancellations, b2b_holds extend + policy columns
    "bk-022",  # AA-975: outbox + event triggers, departure_incidents, on-request desk columns
    "bk-023",  # AA-835: entity_media (photo upload), supply_version + outbox supply stream (v_supply_* not read here)
    "bk-024",  # AA-908: customer_identities + customers.source traveller_signin
    # bk-025 (AA-992 data coverage views) is read by CIS only.
    "bk-026",  # AA-682 S1: supplier_bookings, ops_files, ops_transitions, post-sale edges + guard, supply state view
    "bk-027",  # AA-682 S2: trip_case_checks + seeding trigger, v_trip_check_board, notifications_reminder_uq
    "bk-028",  # AA-682 S3: invoices, invoice_items, payment_schedule, payments, money guards, balance views
    # bk-029 (AA-996 country / destination profiles) is not listed by AA-996 (its call, see its notes).
    "bk-030",  # AA-682 S4: supplier_bills, supplier_bill_items, supplier_payouts, settle, v_supplier_bill_balance
    # bk-031 (AA-996 countries data) changes no shape.
    "bk-032",  # AA-996 slice 2: trip_plans.supplier_id (read by every trip plan query), trip_plan_prices
    # bk-033 (AA-996 effective_margin width) changes no shape read here.
    "bk-034",  # AA-993: itinerary source_tour_id index (lifecycle read; first expand file, auto-applied before deploy)
)
REQUIRED_SCHEMA_VERSION = max(REQUIRED_SCHEMA_VERSIONS)

CONNECT_TIMEOUT_S = 5.0
PROBE_TIMEOUT_S = 5.0
# AA-996 perf guard: the timing budget of one statement. A statement at or over it logs ``slow_query`` (pool
# ``command_timeout`` is 5 s, so a list that issues a few such reads is already close to a 503).
SLOW_QUERY_MS = 1000
SLOW_QUERY_SQL_CHARS = 200
_CATALOG_RE = re.compile(r"\bcatalog\.")

QueryLogger = Callable[[Any], None]


def slow_query_logger(record: Any) -> None:
    """asyncpg query logger (``LoggedQuery``): one warning per statement at or over ``SLOW_QUERY_MS``, with its
    duration, whether it reads the CIS catalog, the error class (a read cut by ``command_timeout`` is
    ``TimeoutError``) and the compacted SQL text. Never the arguments: they can hold personal data."""
    duration_ms = int(record.elapsed * 1000)
    if duration_ms < SLOW_QUERY_MS:
        return
    sql = " ".join(str(record.query).split())
    error = type(record.exception).__name__ if record.exception is not None else "-"
    log.warning(
        "slow_query duration_ms=%d budget_ms=%d catalog=%s error=%s sql=%s",
        duration_ms,
        SLOW_QUERY_MS,
        "true" if _CATALOG_RE.search(sql) else "false",
        error,
        sql[:SLOW_QUERY_SQL_CHARS],
    )


@dataclass(frozen=True)
class Readiness:
    ok: bool
    body: dict[str, Any] = field(default_factory=dict)


class Database:
    def __init__(self, settings: Settings, query_loggers: Sequence[QueryLogger] = ()) -> None:
        """``query_loggers``: extra asyncpg query loggers added to every pool connection next to the timing budget
        log (the integration tests record the statements of each request with one)."""
        self._settings = settings
        self._pool: asyncpg.Pool | None = None
        self._lock = asyncio.Lock()
        self._query_loggers = (slow_query_logger, *query_loggers)

    async def _init_connection(self, conn: asyncpg.Connection) -> None:
        for logger in self._query_loggers:
            conn.add_query_logger(logger)

    @property
    def configured(self) -> bool:
        return self._settings.database_url is not None

    async def pool(self) -> asyncpg.Pool:
        if self._pool is not None:
            return self._pool
        async with self._lock:
            if self._pool is None:
                self._pool = await asyncpg.create_pool(
                    dsn=self._settings.database_url,
                    ssl=self._settings.db_ssl,
                    min_size=0,
                    max_size=5,
                    timeout=CONNECT_TIMEOUT_S,
                    command_timeout=PROBE_TIMEOUT_S,
                    init=self._init_connection,
                    # AA-824: named pool on the shared RDS (pg_stat_activity, data standards C2.2).
                    server_settings={"application_name": "booking-api"},
                )
        return self._pool

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def readiness(self) -> Readiness:
        """DB answers ``SELECT 1`` and every required ``bk-NNN`` is registered.

        Once the schema read succeeds the body also lists ``registered_keys``: every ``bk-NNN`` registered on this
        database, sorted (AA-993 M4a, root ADR 0017 d7). The Deploy Dev ``schema-gate`` (M4b) reads it from the task
        that is running to decide whether the keys the new code requires are applied; it shipped before the gate so the
        first gate run reads a task that already has the field (``missing_schema_versions`` is the fallback)."""
        if not self.configured:
            return Readiness(False, {"status": "not_ready", "db": "not_configured", "schema_version": None})
        try:
            pool = await self.pool()
            async with pool.acquire() as conn:
                await asyncio.wait_for(conn.fetchval("SELECT 1"), PROBE_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 — any failure means "not ready"; never leak the message
            log.warning("readiness: database unreachable (%s)", type(exc).__name__)
            return Readiness(
                False,
                {"status": "not_ready", "db": "error", "error": type(exc).__name__, "schema_version": None},
            )
        try:
            async with pool.acquire() as conn:
                registered = await asyncio.wait_for(
                    conn.fetchval(
                        "SELECT coalesce(array_agg(version ORDER BY version), '{}') FROM booking.v_schema_versions"
                    ),
                    PROBE_TIMEOUT_S,
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("readiness: schema check failed (%s)", type(exc).__name__)
            return Readiness(
                False,
                {"status": "not_ready", "db": "ok", "error": type(exc).__name__, "schema_version": None},
            )
        registered = sorted(registered or [])
        version = max(registered) if registered else None
        missing = [v for v in REQUIRED_SCHEMA_VERSIONS if v not in registered]
        if missing:
            return Readiness(
                False,
                {
                    "status": "not_ready",
                    "db": "ok",
                    "schema_version": version,
                    "required_schema_version": REQUIRED_SCHEMA_VERSION,
                    "missing_schema_versions": missing,
                    "registered_keys": registered,
                },
            )
        return Readiness(True, {"status": "ok", "db": "ok", "schema_version": version, "registered_keys": registered})
