"""Keyset cursors for newest-first feeds (audit log, notifications, case activity).

Several rows written in one transaction share ``now()``, so a cursor on the timestamp alone would skip
rows at a page boundary. The cursor is ``<iso timestamp>|<uuid>`` and the feeds order by
``(created_at DESC, id DESC)``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from app.errors import Invalid


def encode(at: datetime, row_id: str) -> str:
    return f"{at.isoformat()}|{row_id}"


def decode(cursor: str | None) -> tuple[datetime, str] | None:
    if not cursor:
        return None
    at_raw, _, id_raw = cursor.partition("|")
    try:
        at = datetime.fromisoformat(at_raw)
        row_id = str(uuid.UUID(id_raw))
    except ValueError as exc:
        raise Invalid("invalid_cursor", "before must be a next_before value from a previous page") from exc
    if at.tzinfo is None:
        raise Invalid("invalid_cursor", "before must carry a time zone")
    return at, row_id


def next_cursor(items: list[dict[str, Any]], limit: int, *, at_key: str = "at") -> str | None:
    """Cursor of the last item when the page is full (there may be more), else None."""
    if len(items) < limit or not items:
        return None
    last = items[-1]
    return encode(last[at_key], last["id"])


def contains(value: str) -> str:
    """An ``ILIKE`` pattern matching ``value`` anywhere, with ``%``, ``_`` and ``\\`` taken literally."""
    escaped = value.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"
