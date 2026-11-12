"""AA-1091: makers and checkers as people ({id, name}) for the staff admin; one directory query for every ref, markers
and unknown ids keep a None name. Names are invented."""

from __future__ import annotations

from typing import Any

import pytest

from app.auth import people


class FakeConn:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, list[str]]] = []

    async def fetch(self, sql: str, refs: list[str]) -> list[dict[str, Any]]:
        self.calls.append((sql, refs))
        return [r for r in self.rows if r["cis_user_id"] in refs]


@pytest.mark.asyncio
async def test_resolve_replaces_refs_by_people_in_one_query() -> None:
    conn = FakeConn([{"cis_user_id": "u-1", "name": "Invented Maker"}, {"cis_user_id": "u-2", "name": None}])
    items = [
        {"created_by": "u-1", "approved_by": "migration:bk-053", "other": "kept"},
        {"created_by": "u-2", "approved_by": None},
    ]
    await people.resolve(conn, items, ("created_by", "approved_by", "absent_by"))  # type: ignore[arg-type]
    assert items == [
        {
            "created_by": {"id": "u-1", "name": "Invented Maker"},
            "approved_by": {"id": "migration:bk-053", "name": None},
            "other": "kept",
        },
        {"created_by": {"id": "u-2", "name": None}, "approved_by": None},
    ]
    assert len(conn.calls) == 1 and conn.calls[0][1] == ["migration:bk-053", "u-1", "u-2"]


@pytest.mark.asyncio
async def test_no_ref_no_query() -> None:
    conn = FakeConn([])
    items: list[dict[str, Any]] = [{"created_by": None}]
    await people.resolve(conn, items, ("created_by",))  # type: ignore[arg-type]
    assert items == [{"created_by": None}] and conn.calls == []
