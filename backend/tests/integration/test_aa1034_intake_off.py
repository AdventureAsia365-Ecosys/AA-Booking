"""AA-1034 R0 (root ADR 0019 d8) on real Postgres: with the B2C intake off the worker never schedules or claims
``bk_inquiry_pull`` / ``bk_inquiry_process``, cancels their queued rows, and a staff lead answers 410 before any
write; other kinds keep running."""

from __future__ import annotations

from collections.abc import AsyncIterator

import asyncpg
import httpx
import pytest
import pytest_asyncio

from app import b2c
from app.db import Database
from app.inquiry import intake
from app.jobs import queue
from app.jobs.worker import DISABLED_ERROR, Recurring, Worker
from app.main import create_app
from app.settings import Settings
from tests.staff_tokens import LocalCis

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    await conn.execute("DELETE FROM booking.job WHERE kind IN ('bk_inquiry_pull', 'bk_inquiry_process', 'bk_probe')")
    try:
        yield conn
    finally:
        await conn.execute(
            "DELETE FROM booking.job WHERE kind IN ('bk_inquiry_pull', 'bk_inquiry_process', 'bk_probe')"
        )
        await conn.close()


@pytest_asyncio.fixture
async def pool(migrated_booking_dsn: str, admin: asyncpg.Connection) -> AsyncIterator[asyncpg.Pool]:
    p = await asyncpg.create_pool(migrated_booking_dsn, min_size=1, max_size=2)
    try:
        yield p
    finally:
        await p.close()


async def probe(pool: asyncpg.Pool, job: queue.Job) -> dict[str, str]:
    return {"ran": job.kind}


async def test_intake_kinds_are_cancelled_and_never_claimed(admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    async def provide() -> asyncpg.Pool:
        return pool

    left = await queue.enqueue(pool, intake.PULL_KIND, {})  # a pull row left by the previous image
    proc = await queue.enqueue(pool, intake.PROCESS_KIND, {"inbox_event_id": "x"})
    other = await queue.enqueue(pool, "bk_probe", {})
    w = Worker(
        provide,
        {**intake.HANDLERS, "bk_probe": probe},
        recurring=intake.RECURRING,
        disabled_kinds=b2c.INTAKE_KINDS,
    )
    for _ in range(3):
        await w.run_once()
    rows = {
        r["id"]: (r["kind"], r["status"], r["error"])
        for r in await admin.fetch("SELECT id::text, kind, status, error FROM booking.job")
        if r["kind"] in {intake.PULL_KIND, intake.PROCESS_KIND, "bk_probe"}
    }
    assert rows[left] == (intake.PULL_KIND, "cancelled", DISABLED_ERROR)
    assert rows[proc] == (intake.PROCESS_KIND, "cancelled", DISABLED_ERROR)
    assert rows[other] == ("bk_probe", "succeeded", None)
    # No new pull was scheduled: the three rows above are all there is.
    assert len(rows) == 3


async def test_flag_on_keeps_the_recurring_pull(admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    async def provide() -> asyncpg.Pool:
        return pool

    w = Worker(provide, intake.HANDLERS, recurring=(Recurring(intake.PULL_KIND, 60),))
    await w.maintenance(pool)
    statuses = await admin.fetch("SELECT status FROM booking.job WHERE kind = 'bk_inquiry_pull'")
    assert [r["status"] for r in statuses] == ["queued"]


async def test_new_lead_is_refused_before_any_write(admin: asyncpg.Connection, migrated_booking_dsn: str) -> None:
    settings = Settings(database_url=migrated_booking_dsn, db_ssl="disable", environment="test")
    db, cis = Database(settings), LocalCis()
    app = create_app(settings, db, cis.verifier())
    boss = {"Authorization": f"Bearer {cis.token(sub='boss', cis_role='admin')}"}
    before = await admin.fetchval("SELECT count(*) AS n FROM booking.customers")
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
            assert (await client.get("/booking/staff/me", headers=boss)).status_code == 200
            r = await client.post(
                "/booking/customers", json={"name": "Ira Example", "phone": "+84 100 200"}, headers=boss
            )
    finally:
        await db.close()
    assert r.status_code == 410 and r.json()["detail"] == "b2b_only"
    assert await admin.fetchval("SELECT count(*) AS n FROM booking.customers") == before
