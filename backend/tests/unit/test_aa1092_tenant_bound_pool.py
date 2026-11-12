"""AA-1092 residual: ``app.b2b.access.TenantBoundPool`` binds ``app.tenant_id`` on every connection it hands out.

Each path (acquire, fetch, fetchrow, fetchval, execute) binds before the caller's statement and clears after it, also
when the caller raises; a connection whose clearing fails is terminated (never returned bound); the pool exposes no
other method; the B2B dependency builds its context on a bound pool. Real row level security: the integration test
``test_aa1092_b2b_tenant_binding.py``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest

from app.b2b.access import BIND_SQL, UNBIND_SQL, TenantBoundPool

TENANT_A = "7b1e0000-0000-4000-8000-0000000010a1"


class RecordingConn:
    def __init__(self, *, unbind_fails: bool = False) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.unbind_fails = unbind_fails
        self.terminated = False

    async def execute(self, sql: str, *args: Any) -> str:
        self.calls.append((sql, args))
        if sql == UNBIND_SQL and self.unbind_fails:
            raise OSError("connection lost")
        return "OK"

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        self.calls.append((sql, args))
        return [{"n": 1}]

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        self.calls.append((sql, args))
        return {"n": 1}

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.calls.append((sql, args))
        return 1

    def terminate(self) -> None:
        self.terminated = True


class RecordingPool:
    def __init__(self, conn: RecordingConn) -> None:
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


def _sqls(conn: RecordingConn) -> list[str]:
    return [sql for sql, _ in conn.calls]


@pytest.mark.asyncio
async def test_acquire_binds_first_and_clears_last() -> None:
    conn = RecordingConn()
    async with TenantBoundPool(RecordingPool(conn), TENANT_A).acquire() as c:  # type: ignore[arg-type]
        await c.fetchval("SELECT 1")
    assert conn.calls == [(BIND_SQL, (TENANT_A,)), ("SELECT 1", ()), (UNBIND_SQL, ())]
    assert "set_config('app.tenant_id', $1, false)" in BIND_SQL
    assert "set_config('app.tenant_id', '', false)" in UNBIND_SQL


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["fetch", "fetchrow", "fetchval", "execute"])
async def test_pool_shortcuts_run_on_a_bound_connection(method: str) -> None:
    conn = RecordingConn()
    await getattr(TenantBoundPool(RecordingPool(conn), TENANT_A), method)("SELECT $1::int", 7)  # type: ignore[arg-type]
    assert _sqls(conn) == [BIND_SQL, "SELECT $1::int", UNBIND_SQL]


@pytest.mark.asyncio
async def test_cleared_when_the_caller_raises() -> None:
    conn = RecordingConn()
    with pytest.raises(RuntimeError):
        async with TenantBoundPool(RecordingPool(conn), TENANT_A).acquire():  # type: ignore[arg-type]
            raise RuntimeError("handler failed")
    assert _sqls(conn) == [BIND_SQL, UNBIND_SQL] and conn.terminated is False


@pytest.mark.asyncio
async def test_a_connection_that_cannot_be_cleared_is_terminated() -> None:
    conn = RecordingConn(unbind_fails=True)
    async with TenantBoundPool(RecordingPool(conn), TENANT_A).acquire():  # type: ignore[arg-type]
        pass
    assert conn.terminated is True


def test_no_side_door_to_the_plain_pool() -> None:
    bound = TenantBoundPool(RecordingPool(RecordingConn()), TENANT_A)  # type: ignore[arg-type]
    for name in ("transaction", "release", "close", "expire_connections", "_pool_acquire"):
        assert not hasattr(bound, name)
    assert bound.tenant_id == TENANT_A
