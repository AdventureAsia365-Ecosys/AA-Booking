"""AA-996 perf guard (after #130 / hotfix #131), the parts that need no database: the SQL text of every catalog read in
``app/`` (parse-backed views keyed by ``tour_id``), the statement count of the catalog list reads (no N+1: a page of
25 rows issues as many statements as a page of 1), the timing budget log on every pool connection, and the plan
helpers the integration guard uses (``tests/catalog_perf.py``)."""

from __future__ import annotations

import logging
from collections import namedtuple
from typing import Any

import pytest

import app.db as db_module
from app.auth.staff import Staff
from app.catalog import liveness, trip_plans
from app.db import SLOW_QUERY_MS, Database, slow_query_logger
from app.settings import Settings
from tests import catalog_perf
from tests.unit.fakes import ScriptedConn

SALES = Staff("u-1", None, "Sales One", "sales", True, "reviewer")
LoggedQuery = namedtuple("LoggedQuery", "query args timeout elapsed exception conn_addr conn_params")


# ── Static: parse-backed views are read by tour_id ───────────────────────────


def test_every_app_read_of_a_parse_backed_view_is_keyed_by_tour_id() -> None:
    assert catalog_perf.app_findings() == []


def test_the_scan_sees_the_app_reads_it_checks() -> None:
    # The scan must find the hotfix reads, or an empty finding list would prove nothing.
    texts = [
        " ".join(text.split())
        for path in catalog_perf.APP_DIR.rglob("*.py")
        for _, text in catalog_perf.sql_strings(path.read_text(encoding="utf-8"))
    ]
    assert any("FROM catalog.v_tour_place_name p" in t for t in texts)
    assert any("FROM catalog.v_tour_day WHERE tour_id = $1::uuid" in t for t in texts)


def test_the_list_reads_destinations_from_the_names_view_only() -> None:
    """AA-996 S256: v_tour_place resolves a card, a destination and a segment per place (~10 ms a tour on Dev, 994 ms
    for a page of 100); the list reads the names from v_tour_place_name (CIS 298) and never the full view."""
    source = (catalog_perf.APP_DIR / "catalog" / "trip_plans.py").read_text(encoding="utf-8")
    texts = [" ".join(text.split()) for _, text in catalog_perf.sql_strings(source)]
    assert [t for t in texts if catalog_perf.LIST_FORBIDDEN_RE.search(t)] == []
    assert "FROM catalog.v_tour_place_name p" in " ".join(trip_plans.DESTINATIONS_SQL.split())
    assert catalog_perf.LIST_FORBIDDEN_RE.search("SELECT 1 FROM catalog.v_tour_place p WHERE p.tour_id = $1")
    assert not catalog_perf.LIST_FORBIDDEN_RE.search("SELECT 1 FROM catalog.v_tour_place_name p WHERE p.tour_id = $1")


def test_static_guard_fails_the_130_list_statement() -> None:
    findings = catalog_perf.static_findings(catalog_perf.PR130_LIST_SQL)
    assert findings == ["catalog.v_tour_day is filtered by source_id (only tour_id reaches the itinerary parse)"] + [
        "catalog.v_tour_day is not keyed by tour_id (= $n or = ANY($n::uuid[]))"
    ]


@pytest.mark.parametrize(
    ("sql", "ok"),
    [
        ("SELECT * FROM catalog.v_tour_day WHERE tour_id = $1::uuid", True),
        ("SELECT * FROM catalog.v_tour_place p WHERE p.tour_id = ANY($1::uuid[])", True),
        ("SELECT * FROM catalog.v_tour_day WHERE tour_id IN ($1::uuid, $2::uuid)", True),
        # A join or IN (subquery) on tour_id is not pushed into the DISTINCT ON view: Postgres hashes the whole view
        # (every itinerary parsed), checked with EXPLAIN on the stub.
        (
            "SELECT * FROM catalog.v_tour_day d JOIN catalog.v_tour t ON d.tour_id = t.tour_id WHERE t.source_id = $1",
            False,
        ),
        ("SELECT * FROM catalog.v_tour_day WHERE tour_id IN (SELECT tour_id FROM x)", False),
        ("SELECT * FROM catalog.v_tour_day WHERE tour_id IS NOT NULL", False),
        ("SELECT * FROM catalog.v_tour_day WHERE source_system = 'cis' AND source_id = $1", False),
        ("SELECT * FROM catalog.v_tour_place AS p WHERE p.source_id = ANY($1)", False),
        # CIS 298: the names-only view has the same parse behind it, so the same rule.
        ("SELECT * FROM catalog.v_tour_place_name p WHERE p.tour_id = ANY($1::uuid[])", True),
        ("SELECT * FROM catalog.v_tour_place_name p WHERE p.source_id = $1", False),
        ("SELECT count(*) FROM catalog._tour_day_text", False),
    ],
)
def test_static_findings(sql: str, ok: bool) -> None:
    assert (catalog_perf.static_findings(sql) == []) is ok


def test_sql_strings_skip_docstrings_and_read_f_strings() -> None:
    source = '''
"""Module doc: SELECT FROM catalog.v_tour_day by source_id."""
X = f"SELECT d.title FROM catalog.v_tour_day d WHERE d.source_id = {1}"
def f():
    """SELECT * FROM catalog.v_tour_day"""
'''
    found = list(catalog_perf.sql_strings(source))
    assert [line for line, _ in found] == [3] and "d.source_id = {}" in found[0][1]


# ── No N+1: the statement count of a list does not grow with its page ────────


def _list_conn(n: int) -> ScriptedConn:
    rows = [
        {"source_system": "cis", "source_id": f"s-{i}", "tour_id": f"t-{i}", "plan_id": f"p-{i}",
         "cover_photo_path": None, "price_hint_amount": None}
        for i in range(n)
    ]  # fmt: skip
    return (
        ScriptedConn()
        .on("ORDER BY", rows)
        .on("SELECT count(*)", n)
        .on("FROM catalog.v_tour_place_name", [])
        .on("FROM catalog.v_tour_photo", [])
        .on("AS u(plan_id)", [])
    )


@pytest.mark.asyncio
async def test_list_plans_statement_count_does_not_grow_with_the_page() -> None:
    small, large = _list_conn(1), _list_conn(25)
    await trip_plans.list_plans(small, SALES, limit=1)
    await trip_plans.list_plans(large, SALES, limit=25)
    assert len(large.calls) == len(small.calls)
    catalog_reads = [f for f, _ in large.calls if catalog_perf.CATALOG_RE.search(f)]
    assert len(catalog_reads) <= 4  # count, page, destinations, cover photo (the from price reads booking only)


def _orphan_conn(n: int) -> ScriptedConn:
    rows = [
        {"source_system": "cis", "source_id": f"s-{i}", "plan_id": f"p-{i}", "sellable": False, "trip_style": None,
         "default_channel": None, "price_hint_amount": None, "price_hint_currency": None, "price_hint_basis": None,
         "supplier_id": None, "updated_at": None, "open_cases": 0, "future_departures": 0, "confirmed_holds": 0,
         "tomb_state": "retired", "tomb_reason": "gone", "tomb_changed_at": None}
        for i in range(n)
    ]  # fmt: skip
    return (
        ScriptedConn()
        .on("ORDER BY", rows)
        .on("SELECT count(*) FROM booking.trip_plans p WHERE", n)
        .on("FROM pg_class c JOIN pg_namespace n", True)
    )


@pytest.mark.asyncio
async def test_list_orphaned_statement_count_does_not_grow_with_the_page(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(liveness, "_ready", {})
    small = _orphan_conn(1)
    await trip_plans.list_orphaned(small, SALES, limit=1)
    monkeypatch.setattr(liveness, "_ready", {})
    large = _orphan_conn(25)
    out = await trip_plans.list_orphaned(large, SALES, limit=25)
    assert len(out["items"]) == 25 and len(large.calls) == len(small.calls)


# ── Timing budget log on every pool connection ───────────────────────────────


def _record(sql: str, seconds: float, exception: BaseException | None = None) -> LoggedQuery:
    return LoggedQuery(sql, ("secret-value",), 5.0, seconds, exception, None, None)


def test_a_statement_over_the_budget_logs_slow_query(caplog: pytest.LogCaptureFixture) -> None:
    sql = "SELECT  d.places\n FROM catalog.v_tour_day d WHERE d.source_id = $1"
    with caplog.at_level(logging.WARNING, logger="app.db"):
        slow_query_logger(_record(sql, 5.004, TimeoutError()))
        slow_query_logger(_record("SELECT 1", (SLOW_QUERY_MS - 1) / 1000))
    (line,) = [r.getMessage() for r in caplog.records]
    assert line == (
        f"slow_query duration_ms=5004 budget_ms={SLOW_QUERY_MS} catalog=true error=TimeoutError"
        " sql=SELECT d.places FROM catalog.v_tour_day d WHERE d.source_id = $1"
    )
    assert "secret-value" not in line


def test_slow_query_without_error_or_catalog(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="app.db"):
        slow_query_logger(_record("SELECT pg_sleep(1) FROM booking.trip_cases" + " x" * 300, 1.2))
    (line,) = [r.getMessage() for r in caplog.records]
    assert "duration_ms=1200 " in line and "catalog=false error=-" in line
    assert len(line.split(" sql=", 1)[1]) == db_module.SLOW_QUERY_SQL_CHARS


class _LoggingConn:
    def __init__(self) -> None:
        self.loggers: list[Any] = []

    def add_query_logger(self, cb: Any) -> None:
        self.loggers.append(cb)


@pytest.mark.asyncio
async def test_every_pool_connection_gets_the_budget_log_and_the_extra_loggers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def fake_create_pool(**kwargs: Any) -> object:
        seen.update(kwargs)
        return object()

    monkeypatch.setattr(db_module.asyncpg, "create_pool", fake_create_pool)
    recorder = catalog_perf.QueryRecorder()
    db = Database(Settings(database_url="postgresql://u:p@h/db"), query_loggers=[recorder])
    await db.pool()
    conn = _LoggingConn()
    await seen["init"](conn)
    assert conn.loggers == [slow_query_logger, recorder]


# ── Plan helpers of the integration guard ────────────────────────────────────


def test_recorder_keeps_statements_and_classifies_them() -> None:
    rec = catalog_perf.QueryRecorder()
    rec(_record("SELECT * FROM catalog.v_tour_place p WHERE p.tour_id = ANY($1)", 0.01))
    rec(LoggedQuery("INSERT INTO booking.x VALUES ($1)", [(1,), (2,)], None, 0.0, None, None, None))
    rec(_record("SET LOCAL statement_timeout = 1000", 0.0))
    rec(_record("SELECT c.relname FROM pg_catalog.pg_class c", 0.0, RuntimeError()))
    assert [s.args for s in rec.statements] == [("secret-value",), (1,), ("secret-value",), ("secret-value",)]
    assert len(rec.catalog()) == 1 and len(rec.parse_backed()) == 1 and rec.statements[3].error == "RuntimeError"
    rec.clear()
    assert rec.statements == []


def _scan(rel: str, rows: int = 1, loops: int = 1, **cond: str) -> dict[str, Any]:
    return {"Node Type": "Seq Scan", "Relation Name": rel, "Actual Rows": rows, "Actual Loops": loops, **cond}


def test_plan_findings_and_parse_rows() -> None:
    pushed = {"Node Type": "Unique", "Plans": [{"Node Type": "Append", "Plans": [
        _scan("stub_parsed_day", 3, Filter="(tour_id = ANY ($1))"),
        _scan("stub_parsed_day_noise", 0, Filter="(tour_id = ANY ($1))"),
    ]}]}  # fmt: skip
    unpushed = {"Node Type": "Nested Loop", "Plans": [
        _scan("v_tour", 25),
        {"Node Type": "Subquery Scan", "Filter": "(d.source_id = pg.source_id)", "Plans": [
            _scan("stub_parsed_day", 12, 25), _scan("stub_parsed_day_noise", 800, 25)]},
    ]}  # fmt: skip
    assert catalog_perf.plan_findings(pushed) == [] and catalog_perf.parse_rows(pushed) == 3
    assert catalog_perf.plan_findings(unpushed) == [
        "Seq Scan on catalog.stub_parsed_day without a tour_id condition",
        "Seq Scan on catalog.stub_parsed_day_noise without a tour_id condition",
    ]
    assert catalog_perf.parse_rows(unpushed) == (12 + 800) * 25
    # A tour_id condition that is not an equality keys nothing.
    not_null = {"Node Type": "Append", "Plans": [_scan("stub_parsed_day", Filter="(tour_id IS NOT NULL)")]}
    assert catalog_perf.plan_findings(not_null) == ["Seq Scan on catalog.stub_parsed_day without a tour_id condition"]
