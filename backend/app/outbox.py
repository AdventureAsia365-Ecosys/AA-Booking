"""The shared Booking outbox ``booking.outbox`` (bk-022, AA-975): feed order, cursors and the sequencer hook.

Rows are written by triggers in the transaction of the change they report (migration bk-022), so an event exists if
and only if its change committed. What this module adds is the **feed order**:

- ``id`` is the insert order, but two transactions can commit in the opposite order of their ids. A reader paging on
  ``id`` could read 11 (committed) while 10 is still in flight, move its cursor past 10 and never see it.
- So readers page on ``feed_seq`` instead, which :func:`sequence_pending` stamps on **committed** rows, oldest ``id``
  first, from the sequence ``booking.outbox_feed_seq``, inside a transaction that holds one advisory lock. Stampers
  never run at the same time, and each stamps only rows that were already committed when it started, with numbers
  larger than every earlier stamp. Once a reader has read ``n``, every row it will ever read later has
  ``feed_seq > n``. Nothing is skipped, nothing repeats.
- Delivery is at least once (a consumer can crash after reading and before storing its cursor); consumers dedupe on
  ``event_id``, which never changes.

Who stamps: the B2B feed (``GET /booking/b2b/v1/events``) stamps before it reads, so a poll is never staler than the
last commit; the worker maintenance hook :class:`OutboxSequencer` stamps on every pass, so rows also become visible
to readers that do not stamp (AA-835's supply consumer will read a view). A stamper that finds the lock taken skips:
the holder stamps those rows.

Cursors are opaque to consumers: ``bk1.<feed_seq>`` (``bk1.0`` = before the first event).
"""

from __future__ import annotations

import logging
import re

import asyncpg

log = logging.getLogger(__name__)

# One advisory lock key for every stamper (a constant of this module; ids of other advisory locks in the shared
# database are hashtext() of a name or small integers, never this value).
SEQUENCE_LOCK = 7_022_975_000_000_001
BATCH = 1000
MAX_BATCHES = 20
CURSOR_PREFIX = "bk1."
START_CURSOR = f"{CURSOR_PREFIX}0"
_CURSOR_RE = re.compile(r"^bk1\.(\d{1,18})$")

PENDING_SQL = "SELECT id FROM booking.outbox WHERE feed_seq IS NULL ORDER BY id LIMIT $1 FOR UPDATE"
NUMBERS_SQL = "SELECT nextval('booking.outbox_feed_seq') AS n FROM generate_series(1, $1) ORDER BY 1"
STAMP_SQL = """
UPDATE booking.outbox o SET feed_seq = s.n, sequenced_at = now()
FROM unnest($1::bigint[], $2::bigint[]) AS s(id, n)
WHERE o.id = s.id
"""


class CursorError(ValueError):
    """A cursor that is not ``bk1.<n>``."""


def encode_cursor(feed_seq: int) -> str:
    return f"{CURSOR_PREFIX}{int(feed_seq)}"


def decode_cursor(cursor: str | None) -> int:
    """``None`` / empty = the start of the feed (0)."""
    if not cursor:
        return 0
    m = _CURSOR_RE.match(cursor.strip())
    if m is None:
        raise CursorError("not a cursor of this feed")
    return int(m.group(1))


async def _stamp_batch(conn: asyncpg.Connection, batch: int) -> int:
    ids = [r["id"] for r in await conn.fetch(PENDING_SQL, batch)]
    if not ids:
        return 0
    # Sorting both sides pairs the oldest row with the smallest number, whatever order nextval() ran in.
    numbers = sorted(r["n"] for r in await conn.fetch(NUMBERS_SQL, len(ids)))
    await conn.execute(STAMP_SQL, ids, numbers)
    return len(ids)


async def sequence_pending(
    conn: asyncpg.Connection, *, batch: int = BATCH, max_batches: int = MAX_BATCHES
) -> int | None:
    """Stamp ``feed_seq`` on committed, unstamped rows (oldest first). Returns how many, or ``None`` when another
    stamper holds the lock (it stamps them). Each batch is its own short transaction, so the lock is held briefly and
    a reader after the call sees every stamp it made."""
    total = 0
    for _ in range(max_batches):
        async with conn.transaction():
            if not await conn.fetchval("SELECT pg_try_advisory_xact_lock($1)", SEQUENCE_LOCK):
                return None if total == 0 else total
            done = await _stamp_batch(conn, batch)
        total += done
        if done < batch:
            break
    return total


class OutboxSequencer:
    """Worker maintenance hook: stamp pending outbox rows on each maintenance pass (none pending: one index probe)."""

    async def __call__(self, pool: asyncpg.Pool) -> None:
        async with pool.acquire() as conn:
            done = await sequence_pending(conn)
        if done:
            log.info("outbox_sequenced count=%s", done)
