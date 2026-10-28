"""AA-996 perf guard for reads of the CIS catalog (after #130 / hotfix #131).

#130 read ``catalog.v_tour_day`` once per list row, keyed by ``source_id``. On Dev the day text
(``catalog._tour_day_text``, CIS 289) parses every live itinerary and is ``DISTINCT ON (tour_id, day_number)`` behind a
security barrier, so only a ``tour_id`` filter reaches the parse: a ``source_id`` filter is applied after all of them
were parsed (~1.5 s a read). 25 such reads ran past the pool's 5 s ``command_timeout`` and the list answered 503.

The guard has three parts, all here so the unit and integration tests share them:

* ``static_findings``: SQL text in ``app/`` that reads a parse-backed view (``PARSE_BACKED``) must be keyed by
  ``tour_id`` and must not filter it by ``source_id``.
* ``QueryRecorder``: an asyncpg query logger (``app.db.Database(query_loggers=...)``) that keeps every statement a
  request ran, to count them (no N+1) and to explain the catalog ones.
* ``explain`` + ``plan_findings`` / ``parse_rows``: the plan of a statement against the stub catalog
  (``tests/integration/stub_catalog.sql``), whose parse-backed views have the live plan shape over the base tables
  ``PARSE_BASE_TABLES``. A scan of a base table without a ``tour_id`` condition is the #130 class.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asyncpg

APP_DIR = Path(__file__).resolve().parents[1] / "app"

# Catalog views whose rows come from the itinerary parse (CIS 225 / 256 / 289; v_tour_place_name CIS 298).
PARSE_BACKED = ("v_tour_day", "v_tour_place_name", "v_tour_place", "_tour_day_text")
# Catalog views a trip-plans LIST page must never read (AA-996 S256): v_tour_place resolves a place card, a destination
# and a route segment per place (~10 ms a tour on Dev); the list reads the names from v_tour_place_name (CIS 298).
LIST_FORBIDDEN_RE = re.compile(r"\bcatalog\.v_tour_place\b(?!_)")
PARSE_BACKED_RE = re.compile(r"\bcatalog\.(" + "|".join(PARSE_BACKED) + r")\b")
CATALOG_RE = re.compile(r"\bcatalog\.(?!pg_)[a-z_]+")
# The stub tables the parse-backed views read (stub_catalog.sql); the noise table holds NOISE_ROWS itinerary days of
# tours that are not in catalog.v_tour, so a read that reaches it unfiltered reads at least that many rows.
PARSE_BASE_TABLES = frozenset({"stub_parsed_day", "stub_parsed_day_noise"})
NOISE_ROWS = 800
_SQL_KEYWORDS = frozenset(
    "where join left right inner full cross on order group limit offset union with natural lateral as using".split()
)
_ALIAS_RE = re.compile(r"\bcatalog\.(" + "|".join(PARSE_BACKED) + r")\b(?:\s+(?:AS\s+)?([A-Za-z_]\w*))?", re.I)
_TOUR_ID_EQ_RE = re.compile(r"\btour_id = ")
_STATEMENT_HEAD_RE = re.compile(r"^\s*(SELECT|WITH|INSERT|UPDATE|DELETE)\b", re.I)


# ── Static scan of the SQL text ──────────────────────────────────────────────


def static_findings(sql: str) -> list[str]:
    """Why ``sql`` reads a parse-backed view in a way that parses every itinerary (empty when it does not)."""
    findings: list[str] = []
    for match in _ALIAS_RE.finditer(sql):
        view, alias = match.group(1), match.group(2)
        if alias and alias.lower() in _SQL_KEYWORDS:
            alias = None
        prefix = rf"\b{re.escape(alias)}\." if alias else r"(?<![\w.])"
        if re.search(prefix + r"source_id\b", sql):
            findings.append(f"catalog.{view} is filtered by source_id (only tour_id reaches the itinerary parse)")
        # Keyed = compared with a bind parameter. A join or IN (subquery) on tour_id is NOT pushed into the
        # DISTINCT ON view (the view is hashed whole: every itinerary is parsed), nor is a source_id filter.
        if not re.search(prefix + r"tour_id\s*(?:=\s*(?:ANY\s*\(\s*)?|IN\s*\(\s*)\$\d", sql, re.I):
            findings.append(f"catalog.{view} is not keyed by tour_id (= $n or = ANY($n::uuid[]))")
    return findings


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                ids.add(id(first.value))
    return ids


def _text(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):  # f-string: the constant parts, each placeholder as {}
        return "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in node.values)
    return None


def sql_strings(source: str) -> Iterator[tuple[int, str]]:
    """``(line, text)`` of every string literal of a module that looks like SQL reading a parse-backed view."""
    tree = ast.parse(source)
    skip = _docstring_ids(tree)
    for node in ast.walk(tree):  # the constant parts of an f-string are read with it
        if isinstance(node, ast.JoinedStr):
            skip.update(id(v) for v in node.values)
    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        text = _text(node)
        if text and "SELECT" in text and PARSE_BACKED_RE.search(text):
            yield node.lineno, text


def app_findings(app_dir: Path = APP_DIR) -> list[str]:
    """Every parse-backed read in the app that is not keyed by tour_id, as ``path:line: why``."""
    out: list[str] = []
    for path in sorted(app_dir.rglob("*.py")):
        for line, text in sql_strings(path.read_text(encoding="utf-8")):
            out.extend(f"{path.relative_to(app_dir.parent)}:{line}: {why}" for why in static_findings(text))
    return out


# ── Statements of a request ──────────────────────────────────────────────────


@dataclass(frozen=True)
class Statement:
    sql: str
    args: tuple[Any, ...]
    elapsed: float
    error: str | None

    @property
    def flat(self) -> str:
        return " ".join(self.sql.split())

    @property
    def reads_catalog(self) -> bool:
        return CATALOG_RE.search(self.sql) is not None

    @property
    def parse_backed(self) -> bool:
        return PARSE_BACKED_RE.search(self.sql) is not None

    @property
    def explainable(self) -> bool:
        return _STATEMENT_HEAD_RE.match(self.sql) is not None


class QueryRecorder:
    """asyncpg query logger keeping every statement (asyncpg calls it with a ``LoggedQuery``)."""

    def __init__(self) -> None:
        self.statements: list[Statement] = []

    def __call__(self, record: Any) -> None:
        args = record.args
        if isinstance(args, list):  # executemany: the first row is enough to plan the statement
            args = args[0] if args else ()
        error = type(record.exception).__name__ if record.exception is not None else None
        self.statements.append(Statement(record.query, tuple(args or ()), record.elapsed, error))

    def clear(self) -> None:
        self.statements.clear()

    def catalog(self) -> list[Statement]:
        return [s for s in self.statements if s.reads_catalog]

    def parse_backed(self) -> list[Statement]:
        return [s for s in self.statements if s.parse_backed and s.explainable]


# ── Plans ────────────────────────────────────────────────────────────────────


async def explain(conn: asyncpg.Connection, statement: Statement, *, analyze: bool = False) -> dict[str, Any]:
    """The JSON plan (top ``Plan`` node) of a recorded statement with its own arguments. With ``analyze`` the
    statement runs, inside a transaction that is rolled back."""
    options = "ANALYZE, FORMAT JSON" if analyze else "FORMAT JSON"
    tr = conn.transaction()
    await tr.start()
    try:
        raw = await conn.fetchval(f"EXPLAIN ({options}) {statement.sql}", *statement.args)
    finally:
        await tr.rollback()
    return (json.loads(raw) if isinstance(raw, str) else raw)[0]["Plan"]


def _nodes(plan: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield plan
    for child in plan.get("Plans", []):
        yield from _nodes(child)


def base_scans(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [n for n in _nodes(plan) if n.get("Relation Name") in PARSE_BASE_TABLES]


def plan_findings(plan: dict[str, Any]) -> list[str]:
    """Every scan of an itinerary base table that has no ``tour_id`` condition: the itinerary parse of every tour."""
    out = []
    for node in base_scans(plan):
        conditions = " ".join(str(node.get(k, "")) for k in ("Filter", "Index Cond", "Recheck Cond"))
        if not _TOUR_ID_EQ_RE.search(conditions):  # an equality: ``tour_id IS NOT NULL`` keys nothing
            out.append(f"{node['Node Type']} on catalog.{node['Relation Name']} without a tour_id condition")
    return out


def parse_rows(plan: dict[str, Any]) -> int:
    """Itinerary rows the statement fed into the parse (EXPLAIN ANALYZE plan): actual rows x loops of the base scans."""
    return sum(int(n.get("Actual Rows", 0)) * int(n.get("Actual Loops", 1)) for n in base_scans(plan))


async def statement_findings(conn: asyncpg.Connection, statements: list[Statement]) -> list[str]:
    """``plan_findings`` of each statement (plain EXPLAIN: nothing runs), as readable lines."""
    out: list[str] = []
    for statement in statements:
        for why in dict.fromkeys(plan_findings(await explain(conn, statement))):
            out.append(f"{why}\n    in: {statement.flat[:400]}")
    return out


# ── The statement that broke Dev (kept to prove the guard fires) ─────────────

# The list page of #130 (AA-996 slice 2), reduced to the catalog part: the destinations and cover-photo extras as
# per-row LATERAL reads keyed by source_id, verbatim from #130. On Dev it ran past the 5 s pool timeout (503); against
# the stub it reads the whole itinerary base once per page row.
PR130_LIST_SQL = """
SELECT pg.*, ph.path AS first_photo_path, dst.places AS destinations
FROM (
    SELECT t.source_system, t.source_id, t.tour_id::text AS tour_id, t.cover_photo_path,
           row_number() OVER (ORDER BY t.title, t.source_id) AS rn
    FROM catalog.v_tour t LEFT JOIN booking.trip_plans p
      ON p.source_system = t.source_system AND p.source_id = t.source_id
    ORDER BY t.title, t.source_id LIMIT $1 OFFSET $2
) pg
LEFT JOIN LATERAL (
    SELECT array_agg(x.place ORDER BY x.first_seen) AS places FROM (
        SELECT btrim(u.place) AS place, min(d.day_number * 1000 + u.ord) AS first_seen
        FROM catalog.v_tour_day d CROSS JOIN LATERAL unnest(d.places) WITH ORDINALITY AS u(place, ord)
        WHERE d.source_system = pg.source_system AND d.source_id = pg.source_id AND btrim(u.place) <> ''
        GROUP BY btrim(u.place)
    ) x
) dst ON true
LEFT JOIN LATERAL (
    SELECT ph.path FROM catalog.v_tour_photo ph
    WHERE pg.cover_photo_path IS NULL AND ph.source_system = pg.source_system AND ph.source_id = pg.source_id
    ORDER BY ph.position LIMIT 1
) ph ON true
ORDER BY pg.rn
"""
