"""AA-1171 (bk-063) on a real PostgreSQL: the two job check thresholds are pricing policy parameters with an approved
version 1 equal to the old code constants (900 s, 2 x), listed on Pricing > Policy in section "ops" for the
administrator; the sampler reads them, and the registry route's ledger read parses. Invented staff ids only."""

from __future__ import annotations

from datetime import UTC, datetime

import asyncpg
import httpx
import pytest

from app.jobs import metrics
from app.routers import ops_checks as route
from tests.integration.test_aa1091_pricing_policy import SALES, _item, admin, api  # noqa: F401 — fixtures by name

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


class OnePool:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self.conn = conn

    def acquire(self) -> object:
        conn = self.conn

        class _Ctx:
            async def __aenter__(self) -> asyncpg.Connection:
                return conn

            async def __aexit__(self, *exc: object) -> None:
                return None

        return _Ctx()


async def test_the_thresholds_are_seeded_and_listed_for_the_administrator(
    api: tuple[httpx.AsyncClient, dict],  # noqa: F811
    admin: asyncpg.Connection,  # noqa: F811
) -> None:
    registered = await admin.fetchval("SELECT version AS ok FROM shared.schema_versions WHERE version = 'bk-063'")
    assert registered == "bk-063"
    th = await metrics.read_thresholds(OnePool(admin))  # type: ignore[arg-type]
    assert (th.stuck_after_s, th.overdue_factor) == (900, 2.0)
    client, h = api
    listing = await client.get("/policy", headers=h[SALES])
    assert listing.status_code == 200, listing.text
    for key in ("ops_jobs_stuck_after_s", "ops_jobs_overdue_factor"):
        item = _item(listing.json(), key)
        assert item["section"] == "ops" and item["approver"] == "admin"
        assert item["current"]["created_by"]["id"] == "seed:bk-063"


async def test_the_registry_ledger_read_parses(admin: asyncpg.Connection) -> None:  # noqa: F811
    rows = await admin.fetch(route.LAST_ALERTS_SQL, "bk_ops_alert")
    body = route.registry_body(
        {"ops_jobs_stuck_after_s": 900, "ops_jobs_overdue_factor": "2"},
        {r["check_key"]: r["last_at"] for r in rows},
        datetime.now(UTC),
    )
    assert [c["key"] for c in body["checks"]] == ["jobs_stuck", "jobs_overdue"]
