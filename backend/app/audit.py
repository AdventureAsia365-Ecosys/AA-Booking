"""``booking.audit_logs`` writes: one row per write, with ``before`` / ``after`` snapshots."""

from __future__ import annotations

import json
import uuid
from typing import Any

import asyncpg


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
) -> None:
    await conn.execute(
        """
        INSERT INTO booking.audit_logs (actor_type, actor_id, actor_ref, action, entity_type, entity_id, before, after)
        VALUES ($1, $2::uuid, $3, $4, $5, $6::uuid, $7::jsonb, $8::jsonb)
        """,
        actor_type,
        as_uuid(actor_ref),
        actor_ref,
        action,
        entity_type,
        as_uuid(entity_id),
        _json(before),
        _json(after),
    )
