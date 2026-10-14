"""A scripted stand-in for an asyncpg pool/connection: answers by SQL substring, records calls.

Real SQL behaviour is covered by tests/integration (Postgres). These fakes only drive the Python
branches around the SQL.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any

Answer = Any | Callable[..., Any]


class ScriptedConn:
    def __init__(self, rules: list[tuple[str, Answer]] | None = None) -> None:
        self.rules = list(rules or [])
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def on(self, fragment: str, answer: Answer) -> ScriptedConn:
        self.rules.insert(0, (fragment, answer))
        return self

    def _answer(self, sql: str, args: tuple[Any, ...]) -> Any:
        flat = " ".join(sql.split())
        self.calls.append((flat, args))
        for fragment, answer in self.rules:
            if fragment in flat:
                value = answer(*args) if callable(answer) else answer
                if isinstance(value, BaseException):
                    raise value
                return value
        return None

    def sql(self, fragment: str) -> list[tuple[Any, ...]]:
        """Args of every recorded call whose SQL contains ``fragment``."""
        return [args for flat, args in self.calls if fragment in flat]

    async def execute(self, sql: str, *args: Any) -> str:
        self._answer(sql, args)
        return "OK"

    async def executemany(self, sql: str, rows: list[tuple[Any, ...]]) -> None:
        for row in rows:
            self._answer(sql, row)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        return self._answer(sql, args)

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        return self._answer(sql, args)

    async def fetch(self, sql: str, *args: Any) -> Any:
        return self._answer(sql, args) or []

    @asynccontextmanager
    async def transaction(self, **_: Any):
        yield self

    # Pool interface: the same object hands itself out.
    @asynccontextmanager
    async def acquire(self):
        yield self
