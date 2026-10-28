"""``booking.audit_logs``: one row per write (``before`` / ``after`` snapshots) and the admin read."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import asyncpg

from app import paging

MAX_PAGE = 200


def as_uuid(value: str | None) -> str | None:
    """``value`` when it is a UUID (``audit_logs.actor_id`` / ``entity_id`` are UUID columns), else None."""
    if value is None:
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def _json(value: Any) -> str | None:
    return None if value is None else json.dumps(value, default=str, sort_keys=True)


async def record(
    conn: asyncpg.Connection,
    *,
    actor_type: str,
    actor_ref: str | None,
    action: str,
    entity_type: str,
    entity_id: str | None,
    before: Any = None,
    after: Any = None,
    entity_label: str | None = None,
    summary: str | None = None,
) -> None:
    """``entity_label`` (e.g. ``TC-000123 V2``) and ``summary`` are written at write time (bk-005), so the
    audit log page never has to re-derive them from a row that may have changed since."""
    await conn.execute(
        """
        INSERT INTO booking.audit_logs
            (actor_type, actor_id, actor_ref, action, entity_type, entity_id, before, after, entity_label, summary)
        VALUES ($1, $2::uuid, $3, $4, $5, $6::uuid, $7::jsonb, $8::jsonb, $9, $10)
        """,
        actor_type,
        as_uuid(actor_ref),
        actor_ref,
        action,
        entity_type,
        as_uuid(entity_id),
        _json(before),
        _json(after),
        entity_label,
        summary[:500] if summary else None,
    )


def _day_start(value: date | None) -> datetime | None:
    return None if value is None else datetime.combine(value, time.min, tzinfo=UTC)


def _loads(raw: str | None) -> Any:
    return None if raw is None else json.loads(raw)


async def list_logs(
    conn: asyncpg.Connection,
    *,
    actor: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    q: str | None = None,
    before: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Newest first, keyset-paged (``before`` = the ``next_before`` of the previous page, ``app.paging``).

    ``date_to`` is inclusive (the whole day). ``q`` (AA-996) matches the summary, the action, the record label or the
    actor's name anywhere, case-insensitive, with ``%`` / ``_`` taken literally."""
    where: list[str] = []
    args: list[Any] = []

    def add(fragment: str, value: Any) -> None:
        args.append(value)
        where.append(fragment.format(p=f"${len(args)}"))

    if actor:
        add("l.actor_ref = {p}", actor)
    if entity_type:
        add("l.entity_type = {p}", entity_type)
    if entity_id:
        add("l.entity_id = {p}::uuid", entity_id)
    if date_from:
        add("l.created_at >= {p}", _day_start(date_from))
    if date_to:
        add("l.created_at < {p}", _day_start(date_to + timedelta(days=1)))
    if q and q.strip():
        add(
            "(l.summary ILIKE {p} OR l.action ILIKE {p} OR l.entity_label ILIKE {p} OR s.name ILIKE {p})",
            paging.contains(q),
        )
    cursor = paging.decode(before)
    if cursor:
        args.append(cursor[0])
        add(f"(l.created_at, l.id) < (${len(args)}, {{p}}::uuid)", cursor[1])
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    args.append(min(limit, MAX_PAGE))
    rows = await conn.fetch(
        f"""
        SELECT l.id::text AS id, l.created_at, l.actor_type, l.actor_ref, s.name AS actor_name, l.action,
               l.entity_type, l.entity_id::text AS entity_id, l.entity_label, l.summary,
               l.before::text AS before, l.after::text AS after
        FROM booking.audit_logs l LEFT JOIN booking.staff_users s ON s.cis_user_id = l.actor_ref
        {clause}
        ORDER BY l.created_at DESC, l.id DESC
        LIMIT ${len(args)}
        """,  # noqa: S608 — fixed fragments, values are bound
        *args,
    )
    items = [
        {
            "id": r["id"],
            "at": r["created_at"],
            "actor": {"type": r["actor_type"], "id": r["actor_ref"], "name": r["actor_name"]},
            "action": r["action"],
            "entity_type": r["entity_type"],
            "entity_id": r["entity_id"],
            "entity_label": r["entity_label"],
            "summary": r["summary"],
            "before": _loads(r["before"]),
            "after": _loads(r["after"]),
        }
        for r in rows
    ]
    return {"items": items, "next_before": paging.next_cursor(items, args[-1])}
