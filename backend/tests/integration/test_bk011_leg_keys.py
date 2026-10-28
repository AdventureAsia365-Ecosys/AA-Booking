"""bk-011 (AA-830) on a real PostgreSQL, as role ``booking``: the catalog tour key on trip case legs.

Intake fills it from an inquiry.submitted v1.2 payload, or resolves a v1 / v1.1 ``source_tour_id`` (either key kind)
against ``catalog.v_tour`` (CI stub table); the back-fill (``app.inquiry.leg_keys``) is a DRY RUN by default and a
re-run never overwrites.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import asyncpg
import pytest
import pytest_asyncio

from app.inquiry import leg_keys
from tests import inquiry_samples as samples
from tests.integration.test_bk002_intake import INTAKE_TABLES, drain, publish

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    await conn.execute(f"TRUNCATE {INTAKE_TABLES} CASCADE")
    await conn.execute("TRUNCATE tripplanner.outbox RESTART IDENTITY")
    await conn.execute("TRUNCATE catalog.v_tour, catalog.stub_parsed_day, catalog.v_tour_photo")
    try:
        yield conn
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def pool(migrated_booking_dsn: str, admin: asyncpg.Connection) -> AsyncIterator[asyncpg.Pool]:
    p = await asyncpg.create_pool(migrated_booking_dsn, min_size=1, max_size=3)
    try:
        yield p
    finally:
        await p.close()


async def catalog_tour(admin: asyncpg.Connection, source_id: str, tour_id: str) -> None:
    await admin.execute(
        "INSERT INTO catalog.v_tour (source_id, tour_id, title) VALUES ($1, $2::uuid, 'Stub tour')", source_id, tour_id
    )


async def legs(conn: asyncpg.Connection) -> dict[str, tuple[str | None, str | None]]:
    rows = await conn.fetch("SELECT source_tour_id, source_system, source_id FROM booking.trip_case_legs")
    return {r["source_tour_id"]: (r["source_system"], r["source_id"]) for r in rows}


def tour_ids(payload: dict) -> list[str]:
    return [leg["source_tour_id"] for leg in sorted(payload["itinerary"]["legs"], key=lambda leg: leg["position"])]


# ── Intake ───────────────────────────────────────────────────────────────────


async def test_v1_2_payload_key_is_stored_as_sent(admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    payload = samples.v1_2(keys=["src-a", None])
    await publish(admin, payload)
    await drain(pool)
    first, second = tour_ids(payload)
    # leg 2 came unresolved and the catalog does not know it either: stays null, the case still opens.
    assert await legs(admin) == {first: ("cis", "src-a"), second: (None, None)}
    assert (await admin.fetchval("SELECT count(*) FROM booking.trip_cases")) == 1
    inbox = await admin.fetchrow("SELECT contract_version, status FROM booking.inbox_events")
    assert (inbox["contract_version"], inbox["status"]) == ("1.2", "processed")


async def test_v1_1_tour_keys_are_resolved_by_either_kind(admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    payload = samples.v1_1()
    version_tour_id, already_source_id = tour_ids(payload)
    await catalog_tour(admin, "src-1", version_tour_id)  # leg 1 sent a version tour_id
    await catalog_tour(admin, already_source_id, str(uuid.uuid4()))  # leg 2 sent the source_id itself
    await publish(admin, payload)
    await drain(pool)
    assert await legs(admin) == {version_tour_id: ("cis", "src-1"), already_source_id: ("cis", already_source_id)}


# ── Back-fill ────────────────────────────────────────────────────────────────


async def test_backfill_dry_run_then_apply_then_rerun(admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    payload = samples.v1_1()
    await publish(admin, payload)
    await drain(pool)  # catalog empty at intake: both legs keep a null key (a pre-bk-011 leg looks the same)
    known, gone = tour_ids(payload)
    await catalog_tour(admin, "src-known", known)

    async with pool.acquire() as conn:
        dry = await leg_keys.backfill(conn)
    assert (dry["mode"], dry["legs_without_key"], dry["matched"], dry["written"]) == ("dry_run", 2, 1, 0)
    assert [u["source_tour_id"] for u in dry["unmatched"]] == [gone]
    assert [(w["source_tour_id"], w["source_id"]) for w in dry["would_write"]] == [(known, "src-known")]
    assert await legs(admin) == {known: (None, None), gone: (None, None)}

    async with pool.acquire() as conn:
        applied = await leg_keys.backfill(conn, apply=True)
    assert (applied["mode"], applied["written"]) == ("apply", 1)
    assert await legs(admin) == {known: ("cis", "src-known"), gone: (None, None)}

    async with pool.acquire() as conn:
        again = await leg_keys.backfill(conn, apply=True)
    assert (again["legs_without_key"], again["written"]) == (1, 0)


async def test_backfill_never_overwrites_an_existing_key(admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    payload = samples.v1_2(keys=["src-sent", "src-other"])
    await publish(admin, payload)
    await drain(pool)
    first, _ = tour_ids(payload)
    await catalog_tour(admin, "src-catalog", first)
    async with pool.acquire() as conn:
        report = await leg_keys.backfill(conn, apply=True)
    assert (report["legs_without_key"], report["written"]) == (0, 0)
    assert (await legs(admin))[first] == ("cis", "src-sent")


async def test_outbox_report_reads_the_view_as_role_booking(admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    old, new = samples.v1_1(), samples.v1_2(keys=["src-a", None])
    await publish(admin, old)
    await publish(admin, new)
    await catalog_tour(admin, "src-a", str(uuid.uuid4()))
    async with pool.acquire() as conn:
        report = await leg_keys.outbox_report(conn)
    assert (report["legs"], report["with_key_in_payload"], report["matched"]) == (4, 1, 1)
    assert len(report["unmatched"]) == 3
