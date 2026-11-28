"""AA-1054 (S297): the pilot fence read of AA-Booking (:mod:`app.auth.pilot_fence`) on real PostgreSQL, as role
``booking``.

``catalog.v_pilot_fence_v1`` (CIS 496) is the stub's plain table (tests/integration/stub_catalog.sql: one row, off,
version 0). Covers, both ways: the default reads off; an approved "on" reads on and :func:`pilot_fence.check` refuses
an E2E identity (403 e2e_pilot_fence) while real staff pass; back to off it serves again; fail closed: no row, no grant
or no view reads unknown and refuses the E2E identity (403 e2e_pilot_fence_unknown). The stub row is always put back.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import asyncpg
import pytest
import pytest_asyncio
from fastapi import HTTPException

from app.auth import pilot_fence

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

E2E = "e2e-booking@e2e-mail.lumiguides.it.com"
STAFF = "lan.tran@adventure.asia"
STUB = "catalog.v_pilot_fence_v1"
RESTORE = (
    f"DROP TABLE IF EXISTS {STUB}; "
    f"CREATE TABLE {STUB} (fence_on boolean NOT NULL, version integer NOT NULL, "
    "effective_from timestamptz NOT NULL DEFAULT now()); "
    f"INSERT INTO {STUB} (fence_on, version) VALUES (false, 0); "
    f"GRANT SELECT ON {STUB} TO booking;"
)


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    pilot_fence.reset()
    try:
        yield conn
    finally:
        await conn.execute(RESTORE)
        pilot_fence.reset()
        await conn.close()


@pytest_asyncio.fixture
async def booking(migrated_booking_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_booking_dsn)
    try:
        yield conn
    finally:
        await conn.close()


async def test_the_default_reads_off_and_serves(admin: asyncpg.Connection, booking: asyncpg.Connection) -> None:
    assert await booking.fetchval("SELECT current_user AS who") == "booking"
    assert await pilot_fence.read_state(booking) == pilot_fence.OFF
    await pilot_fence.check(booking, E2E)


async def test_on_refuses_an_e2e_identity_and_off_serves_it_again(
    admin: asyncpg.Connection, booking: asyncpg.Connection
) -> None:
    await admin.execute(f"UPDATE {STUB} SET fence_on = true, version = 1")
    assert await pilot_fence.read_state(booking) == pilot_fence.ON
    with pytest.raises(HTTPException) as exc:
        await pilot_fence.check(booking, E2E)
    assert (exc.value.status_code, exc.value.detail) == (403, "e2e_pilot_fence")
    await pilot_fence.check(booking, STAFF)  # real staff never read it
    await admin.execute(f"UPDATE {STUB} SET fence_on = false, version = 2")
    pilot_fence.reset()
    await pilot_fence.check(booking, E2E)


@pytest.mark.parametrize(
    "break_it",
    [
        f"DELETE FROM {STUB}",
        f"REVOKE SELECT ON {STUB} FROM booking",
        f"DROP TABLE {STUB}",
    ],
    ids=["no-row", "no-grant", "no-view"],
)
async def test_unreadable_fails_closed(admin: asyncpg.Connection, booking: asyncpg.Connection, break_it: str) -> None:
    await admin.execute(break_it)
    assert await pilot_fence.read_state(booking) == pilot_fence.UNKNOWN
    with pytest.raises(HTTPException) as exc:
        await pilot_fence.check(booking, E2E)
    assert (exc.value.status_code, exc.value.detail) == (403, "e2e_pilot_fence_unknown")
    await pilot_fence.check(booking, STAFF)
