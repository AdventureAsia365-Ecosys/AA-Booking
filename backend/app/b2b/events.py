"""The B2B event feed ``GET /booking/b2b/v1/events`` (AA-975, bk-022; contract booking.b2b.v1 ``EventPage``).

The calling agency reads its own events (``booking.outbox`` rows with ``stream = 'b2b'`` and its ``partner_id``) after
a cursor, oldest first. The feed stamps pending rows first (:func:`app.outbox.sequence_pending`), so a poll sees
every change committed before it.

- **Cursor**: ``after`` is the ``cursor`` of the last event the caller stored (``bk1.<n>``); omitted = from the start.
  ``next_cursor`` is the cursor of the last item, or ``after`` itself when there is nothing new, so a caller always
  stores ``next_cursor`` and polls again with it. A malformed cursor, or one ahead of the feed (never issued), is 422
  ``invalid``.
- **At least once**: replaying from an older cursor returns the same events again, same ``event_id`` and order, each
  once (Linear AA-975 E); consumers dedupe on ``event_id``.
- **No personal data**: ids and codes only (the outbox has no other column).
"""

from __future__ import annotations

from typing import Any

import asyncpg

from app import outbox
from app.b2b.errors import B2BError

DEFAULT_LIMIT = 200
MAX_LIMIT = 500

FEED_SQL = """
SELECT event_id::text AS event_id, feed_seq, event_type, occurred_at, hold_id::text AS hold_id,
       booking_id::text AS booking_id, line_ref, departure_id::text AS departure_id, incident_code, decline_code
FROM booking.outbox
WHERE stream = 'b2b' AND partner_id = $1::uuid AND feed_seq IS NOT NULL AND feed_seq > $2
ORDER BY feed_seq
LIMIT $3
"""
HEAD_SQL = "SELECT coalesce(max(feed_seq), 0) FROM booking.outbox WHERE feed_seq IS NOT NULL"


def event_out(row: Any) -> dict[str, Any]:
    return {
        "event_id": row["event_id"],
        "cursor": outbox.encode_cursor(row["feed_seq"]),
        "type": row["event_type"],
        "occurred_at": row["occurred_at"],
        "hold_id": row["hold_id"],
        "booking_id": row["booking_id"],
        "line_ref": row["line_ref"],
        "departure_id": row["departure_id"],
        "incident_code": row["incident_code"],
        "decline_code": row["decline_code"],
        "new_start_time": None,  # departure.time_change has no producer yet (departures carry no start time)
    }


async def list_events(pool: asyncpg.Pool, *, partner_id: str, after: str | None, limit: int) -> dict[str, Any]:
    try:
        position = outbox.decode_cursor(after)
    except outbox.CursorError:
        raise B2BError("invalid", "after must be a cursor returned by this feed") from None
    if not 1 <= limit <= MAX_LIMIT:
        raise B2BError("invalid", f"limit must be between 1 and {MAX_LIMIT}")
    async with pool.acquire() as conn:
        await outbox.sequence_pending(conn)
        if position and position > await conn.fetchval(HEAD_SQL):
            raise B2BError("invalid", "after is ahead of the feed; use a cursor returned by this feed")
        rows = await conn.fetch(FEED_SQL, partner_id, position, limit)
    items = [event_out(r) for r in rows]
    next_cursor = items[-1]["cursor"] if items else outbox.encode_cursor(position)
    return {"items": items, "next_cursor": next_cursor}
