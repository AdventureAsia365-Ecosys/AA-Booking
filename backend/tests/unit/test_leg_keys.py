"""AA-830: catalog tour key resolution and the trip case leg back-fill (real SQL: tests/integration)."""

from __future__ import annotations

from typing import Any

import asyncpg
import pytest

from app.inquiry import leg_keys
from tests.unit.fakes import ScriptedConn

CATALOG = [
    {"source_system": "cis", "source_id": "src-1", "tour_id": "tour-v2"},
    {"source_system": "cis", "source_id": "src-2", "tour_id": "tour-x"},
]
LEGS = [
    {"id": "l1", "code": "TC-000001", "position": 1, "source_tour_id": "tour-v2"},  # a version tour_id
    {"id": "l2", "code": "TC-000001", "position": 2, "source_tour_id": "src-2"},  # already the source_id
    {"id": "l3", "code": "TC-000002", "position": 1, "source_tour_id": "tour-gone"},  # superseded / unknown
]


def conn_with(legs: list[dict[str, Any]] = LEGS, catalog: Any = CATALOG) -> ScriptedConn:
    return ScriptedConn(
        [
            ("FROM booking.trip_case_legs l JOIN booking.trip_cases tc", legs),
            ("FROM catalog.v_tour", catalog),
            ("UPDATE booking.trip_case_legs", None),
        ]
    )


@pytest.mark.asyncio
async def test_resolve_accepts_both_key_kinds_and_skips_unknown() -> None:
    conn = conn_with()
    keys = await leg_keys.resolve(conn, ["tour-v2", "src-2", None, "tour-v2", "tour-gone"])
    assert keys == {"tour-v2": ("cis", "src-1"), "src-2": ("cis", "src-2")}
    assert conn.sql("FROM catalog.v_tour") == [(["src-2", "tour-gone", "tour-v2"],)]


@pytest.mark.asyncio
async def test_resolve_without_keys_reads_nothing() -> None:
    conn = conn_with()
    assert await leg_keys.resolve(conn, [None, ""]) == {}
    assert conn.calls == []


@pytest.mark.asyncio
async def test_resolve_on_an_unreadable_catalog_is_empty() -> None:
    conn = conn_with(catalog=asyncpg.UndefinedTableError("relation catalog.v_tour does not exist"))
    assert await leg_keys.resolve(conn, ["tour-v2"]) == {}


@pytest.mark.asyncio
async def test_dry_run_reports_and_writes_nothing() -> None:
    conn = conn_with()
    report = await leg_keys.backfill(conn)
    assert report["mode"] == "dry_run"
    assert (report["legs_without_key"], report["matched"], report["written"]) == (3, 2, 0)
    assert report["unmatched"] == [{"trip_case": "TC-000002", "position": 1, "source_tour_id": "tour-gone"}]
    assert [(w["source_tour_id"], w["source_id"]) for w in report["would_write"]] == [
        ("tour-v2", "src-1"),
        ("src-2", "src-2"),
    ]
    assert conn.sql("UPDATE booking.trip_case_legs") == []


@pytest.mark.asyncio
async def test_apply_writes_only_matched_legs_still_without_a_key() -> None:
    conn = conn_with()

    async def execute(sql: str, *args: Any) -> str:
        conn._answer(sql, args)
        return "UPDATE 1"

    conn.execute = execute  # type: ignore[method-assign]
    report = await leg_keys.backfill(conn, apply=True)
    assert (report["mode"], report["written"], report["would_write"]) == ("apply", 2, [])
    assert conn.sql("UPDATE booking.trip_case_legs") == [("l1", "cis", "src-1"), ("l2", "cis", "src-2")]
    assert "source_id IS NULL" in leg_keys.FILL_SQL  # a re-run never overwrites a key


@pytest.mark.asyncio
async def test_outbox_report_counts_payload_keys_and_matches() -> None:
    outbox = [
        {"idempotency_key": "a:1", "position": 1, "source_tour_id": "tour-v2", "source_id": None, "version": "1.1"},
        {"idempotency_key": "b:2", "position": 1, "source_tour_id": "tour-old", "source_id": "src-2", "version": "1.2"},
        {"idempotency_key": "c:3", "position": 1, "source_tour_id": "tour-gone", "source_id": None, "version": "1"},
    ]
    conn = conn_with().on("FROM tripplanner.v_inquiry_outbox", outbox)
    report = await leg_keys.outbox_report(conn)
    assert (report["legs"], report["with_key_in_payload"], report["matched"]) == (3, 1, 2)
    assert [leg["idempotency_key"] for leg in report["unmatched"]] == ["c:3"]


@pytest.mark.asyncio
async def test_outbox_report_without_the_grant_says_so() -> None:
    conn = conn_with().on("FROM tripplanner.v_inquiry_outbox", asyncpg.InsufficientPrivilegeError("no grant"))
    assert await leg_keys.outbox_report(conn) == {"error": "InsufficientPrivilegeError"}


def test_main_without_a_database_url_exits_2(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert leg_keys.main([]) == 2
    assert "DATABASE_URL" in capsys.readouterr().out
