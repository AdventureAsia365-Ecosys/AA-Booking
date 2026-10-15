"""Database access: a lazy asyncpg pool and the readiness probe.

The pool is created on first use, not at startup, so the process (and the ALB health check on
``/booking/health``) stays up while the database is unreachable (root ADR 0004 decision 9).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import asyncpg

from app.settings import Settings

log = logging.getLogger(__name__)

# Every bk-NNN migration this code needs. Readiness fails until ALL of them are registered: checking only the
# newest one would pass with a gap (bk-005 applied, an older file missing) and the API would then fail per call.
# bk-008 (AA-822 suppliers, a parallel branch) is not listed: no code here needs it; add it with that code.
REQUIRED_SCHEMA_VERSIONS: tuple[str, ...] = (
    "bk-001",
    "bk-002",
    "bk-003",
    "bk-004",
    "bk-005",
    "bk-006",
    "bk-007",
    "bk-009",
)
REQUIRED_SCHEMA_VERSION = max(REQUIRED_SCHEMA_VERSIONS)

CONNECT_TIMEOUT_S = 5.0
PROBE_TIMEOUT_S = 5.0


@dataclass(frozen=True)
class Readiness:
    ok: bool
    body: dict[str, Any] = field(default_factory=dict)


class Database:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._pool: asyncpg.Pool | None = None
        self._lock = asyncio.Lock()

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
                )
        return self._pool

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def readiness(self) -> Readiness:
        """DB answers ``SELECT 1`` and every required ``bk-NNN`` is registered."""
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
        registered = list(registered or [])
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
                },
            )
        return Readiness(True, {"status": "ok", "db": "ok", "schema_version": version})
