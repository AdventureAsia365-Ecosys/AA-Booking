"""AA-825 — count, delete or redact the expired rows of one retention part (data standards C5.4c, DM-7.2).

Work runs in batches (at most :data:`BATCH_SIZE_MAX` rows, one transaction each, ``statement_timeout`` 30 s), selected
by keyset on the indexed ``(retention column, id)`` pair (bk-012), oldest first, ``FOR UPDATE SKIP LOCKED`` so a row in
use is left for the next run, with a short pause between batches.

- Delete part: for an archived part each batch is written to S3 first; the DELETE runs in the same transaction as the
  row lock and must remove exactly the locked (and archived) rows, or the transaction rolls back.
- Redact part: each locked row is redacted in Python (``app.retention.redact``) and written back with ``redacted_at``;
  the UPDATE must touch exactly the locked rows.

Rows referenced by a foreign key are never deleted: a ``NOT EXISTS`` per referencing column found in ``pg_constraint``
(none point at ``booking.job`` today; X6 adds ``parent_job_id``). Never ``VACUUM FULL``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

import asyncpg

from app.retention import redact
from app.retention.policy import Part, Spec

log = logging.getLogger(__name__)

BATCH_SIZE_MAX = 1_000
BATCH_STATEMENT_TIMEOUT = "30s"
OBJECT_KEYS_KEPT = 50  # keys listed in the job result per part (the counts are always exact)
_INDEX_RE = re.compile(r"\[\d+\]")


class CountMismatch(Exception):
    """The DELETE / UPDATE did not touch exactly the rows that were locked (and archived)."""


class UnsupportedForeignKey(Exception):
    """A foreign key points at the table in a shape the NOT EXISTS guard cannot express."""


@dataclass
class PartResult:
    part: str
    window_days: int
    archive: bool
    cutoff: str
    candidates: int = 0
    oldest: str | None = None
    newest: str | None = None
    affected: int = 0
    archived_rows: int = 0
    batches: int = 0
    objects: int = 0
    object_keys: list[str] = field(default_factory=list)
    fields_redacted: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


async def cutoff(conn: asyncpg.Connection, window_days: int) -> datetime:
    """UTC midnight ``window_days`` days before today (UTC): every expired day is a whole day."""
    return await conn.fetchval(
        "SELECT (date_trunc('day', now() AT TIME ZONE 'UTC') - make_interval(days => $1)) AT TIME ZONE 'UTC'",
        window_days,
    )


_FK_REFS_SQL = """
    SELECT format('%I.%I', n.nspname, cl.relname) AS ref_table, a.attname AS ref_col,
           array_length(c.conkey, 1) AS ncols, ta.attname AS target_col, c.conname
      FROM pg_catalog.pg_constraint c
      JOIN pg_catalog.pg_class cl ON cl.oid = c.conrelid
      JOIN pg_catalog.pg_namespace n ON n.oid = cl.relnamespace
      JOIN pg_catalog.pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]
      JOIN pg_catalog.pg_attribute ta ON ta.attrelid = c.confrelid AND ta.attnum = c.confkey[1]
     WHERE c.contype = 'f' AND c.confrelid = $1::regclass
     ORDER BY 1, 2
"""


async def fk_refs(conn: asyncpg.Connection, table: str) -> list[tuple[str, str]]:
    """Foreign keys pointing at ``table``. Only single-column FKs onto ``id`` are supported; any other shape stops the
    run instead of letting a DELETE cascade or fail half-way."""
    out = []
    for r in await conn.fetch(_FK_REFS_SQL, table):
        if r["ncols"] != 1 or r["target_col"] != "id":
            raise UnsupportedForeignKey(
                f"{r['conname']} on {r['ref_table']} references {table} by {r['ncols']} column(s) onto "
                f"{r['target_col']!r}; the retention guard only handles single-column FKs onto id"
            )
        out.append((r["ref_table"], r["ref_col"]))
    return out


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def predicate(spec: Spec, part: Part, refs: list[tuple[str, str]]) -> str:
    """WHERE clause on alias ``t`` with $1 = cutoff. FK guards only matter for deletes."""
    clauses = [f"t.{spec.ts_col} < $1", f"({part.where})"]
    if spec.action == "delete":
        for i, (ref_table, ref_col) in enumerate(refs):
            # ref_table comes from format('%I.%I') in pg_constraint, ref_col is quoted: no caller-supplied text.
            ref = f"SELECT 1 FROM {ref_table} r{i} WHERE r{i}.{_quote_ident(ref_col)} = t.id"  # noqa: S608
            clauses.append(f"NOT EXISTS ({ref})")
    return " AND ".join(clauses)


def count_sql(spec: Spec, part: Part, refs: list[tuple[str, str]]) -> str:
    return (
        f"SELECT count(*)::bigint AS n, min(t.{spec.ts_col}) AS oldest, max(t.{spec.ts_col}) AS newest "  # noqa: S608
        f"FROM {spec.table} t WHERE {predicate(spec, part, refs)}"
    )


def batch_sql(spec: Spec, part: Part, refs: list[tuple[str, str]], *, after: bool) -> str:
    """$1 cutoff, $2 limit, [$3 last ts, $4 last id]. Locks the rows it returns (skipping rows locked elsewhere)."""
    if spec.action == "redact":
        extra = ", t.status, t.payload::text AS payload, t.error"
    else:
        extra = ", to_jsonb(t)::text AS doc" if part.archive else ""
    keyset = f" AND (t.{spec.ts_col}, t.id) > ($3, $4::uuid)" if after else ""
    return (
        f"SELECT t.{spec.ts_col} AS ts, t.id AS id{extra} FROM {spec.table} t "  # noqa: S608 — fixed fragments
        f"WHERE {predicate(spec, part, refs)}{keyset} "
        f"ORDER BY t.{spec.ts_col}, t.id LIMIT $2 FOR UPDATE OF t SKIP LOCKED"
    )


def delete_sql(spec: Spec) -> str:
    return f"DELETE FROM {spec.table} WHERE id = ANY($1::uuid[])"  # noqa: S608 — fixed table name


REDACT_SQL = """
    UPDATE booking.inbox_events e
       SET payload = u.payload::jsonb, error = u.error, redacted_at = now()
      FROM unnest($1::uuid[], $2::text[], $3::text[]) AS u(id, payload, error)
     WHERE e.id = u.id AND e.redacted_at IS NULL
"""


def same_month_prefix(rows: list[Any]) -> list[Any]:
    """The leading rows that share the first row's UTC month (one archive object = one month)."""
    if not rows:
        return rows
    first = rows[0]["ts"].astimezone(UTC)
    out = []
    for r in rows:
        ts = r["ts"].astimezone(UTC)
        if (ts.year, ts.month) != (first.year, first.month):
            break
        out.append(r)
    return out


async def count_part(
    conn: asyncpg.Connection, spec: Spec, part: Part, refs: list[tuple[str, str]], cut: datetime
) -> PartResult:
    row = await conn.fetchrow(count_sql(spec, part, refs), cut)
    return PartResult(
        part=part.name,
        window_days=part.window_days,
        archive=part.archive,
        cutoff=_iso(cut) or "",
        candidates=int(row["n"]),
        oldest=_iso(row["oldest"]),
        newest=_iso(row["newest"]),
    )


async def process_part(
    conn: asyncpg.Connection,
    spec: Spec,
    part: Part,
    refs: list[tuple[str, str]],
    cut: datetime,
    res: PartResult,
    *,
    limit_rows: int,
    batch_size: int = BATCH_SIZE_MAX,
    archiver: Any = None,
    pause_s: float = 0.2,
) -> PartResult:
    """Deletes or redacts up to ``limit_rows`` candidates of one part, oldest first, into ``res``."""
    if part.archive and archiver is None:
        raise ValueError(f"{spec.kind}/{part.name} archives before delete but no archiver was given")
    batch_size = max(1, min(int(batch_size), BATCH_SIZE_MAX))
    first_sql = batch_sql(spec, part, refs, after=False)
    next_sql = batch_sql(spec, part, refs, after=True)
    cursor: tuple[datetime, Any] | None = None
    while res.affected < limit_rows:
        n = min(batch_size, limit_rows - res.affected)
        key = None
        async with conn.transaction():
            await conn.execute(f"SET LOCAL statement_timeout = '{BATCH_STATEMENT_TIMEOUT}'")
            if cursor is None:
                rows = await conn.fetch(first_sql, cut, n)
            else:
                rows = await conn.fetch(next_sql, cut, n, cursor[0], cursor[1])
            if not rows:
                break
            if spec.action == "redact":
                touched = await _redact_rows(conn, rows, res)
            else:
                if part.archive:
                    rows = same_month_prefix(rows)
                    key = await archiver.put(
                        spec.archive_name, part.name, res.batches + 1, rows[0]["ts"], [r["doc"] for r in rows]
                    )
                status = await conn.execute(delete_sql(spec), [r["id"] for r in rows])
                touched = int(status.split()[-1])
            if touched != len(rows):
                raise CountMismatch(f"{spec.table}: locked {len(rows)} rows, {spec.action} touched {touched}")
        # committed
        res.batches += 1
        res.affected += touched
        if key is not None:
            res.archived_rows += touched
            res.objects += 1
            if len(res.object_keys) < OBJECT_KEYS_KEPT:
                res.object_keys.append(key)
        cursor = (rows[-1]["ts"], rows[-1]["id"])
        if pause_s:
            await asyncio.sleep(pause_s)
    log.info("retention %s/%s: %s %d rows in %d batches", spec.kind, part.name, spec.action, res.affected, res.batches)
    return res


async def _redact_rows(conn: asyncpg.Connection, rows: list[Any], res: PartResult) -> int:
    ids, payloads, errors = [], [], []
    for r in rows:
        payload, paths = redact.redact_payload(json.loads(r["payload"]))
        for path in paths:
            label = _INDEX_RE.sub("[]", path)
            res.fields_redacted[label] = res.fields_redacted.get(label, 0) + 1
        ids.append(r["id"])
        payloads.append(json.dumps(payload))
        errors.append(redact.redact_error(r["status"], r["error"]))
    status = await conn.execute(REDACT_SQL, ids, payloads, errors)
    return int(status.split()[-1])
