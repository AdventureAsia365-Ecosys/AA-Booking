"""``booking.notifications`` for staff: the feed, read state and the write helper.

A staff member sees the shared staff queue (``recipient_type = 'staff'`` with no recipient) plus the rows
addressed to them (``recipient_ref`` = their CIS user id, bk-005). Read state is per person in
``booking.notification_reads`` (bk-005), because one queue row is shared by every staff member; the bk-002
``read_at`` column is not used for staff rows. Customer rows (``recipient_type = 'customer'``) never show
here: they are the customer's "My Trips" feed (O8).

A notification is a row. The only e-mail is the staff "new inquiry" e-mail, sent by the ``bk_notify_email`` job
that intake queues next to its notification (``app/notifications/staff_email.py``, AA-811); these helpers send none.
"""

from __future__ import annotations

import json
from typing import Any

import asyncpg

from app import audit, paging
from app.auth.staff import Staff

# Fixed vocabulary (S236 UI spec §4.3, shell C-8). trip_case.new_inquiry is what bk-002 intake writes.
STAFF_TYPES = (
    "trip_case.new_inquiry",
    "trip_case.assigned",
    "trip_case.status_changed",
    "proposal.approved",
    # AA-986 L5 (root ADR 0015, Q4): a CIS tour used by an open trip case or a trip plan left the catalog / came back
    # (app.catalog.lifecycle; entity trip_case or trip_plan).
    "tour.retired",
    "tour.reinstated",
    # AA-975: a confirmed B2B booking has on-request lines for the desk to accept or decline (app.b2b.desk).
    "b2b.on_request",
)
CUSTOMER_TYPES = ("proposal.sent",)
MAX_PAGE = 100

VISIBLE = (
    "n.recipient_type = 'staff' AND ((n.recipient_ref IS NULL AND n.recipient_id IS NULL) OR n.recipient_ref = $1)"
)
IS_READ = "EXISTS (SELECT 1 FROM booking.notification_reads r WHERE r.notification_id = n.id AND r.cis_user_id = $1)"


async def notify_staff(
    conn: asyncpg.Connection,
    *,
    type: str,  # noqa: A002 — the column name
    title: str,
    message: str | None,
    entity_type: str | None,
    entity_id: str | None,
    data: dict[str, Any] | None = None,
    recipient_ref: str | None = None,
) -> None:
    """``recipient_ref`` None = the shared staff queue."""
    await conn.execute(
        """
        INSERT INTO booking.notifications
            (recipient_type, recipient_id, recipient_ref, type, title, message, entity_type, entity_id, data)
        VALUES ('staff', NULL, $1, $2, $3, $4, $5, $6::uuid, $7::jsonb)
        """,
        recipient_ref,
        type,
        title,
        message,
        entity_type,
        entity_id,
        json.dumps(data or {}, default=str),
    )


async def notify_staff_once(
    conn: asyncpg.Connection,
    *,
    type: str,  # noqa: A002 — the column name
    title: str,
    message: str | None,
    entity_type: str,
    entity_id: str,
    reminder_key: str,
    data: dict[str, Any] | None = None,
    recipient_ref: str | None = None,
) -> bool:
    """A reminder written at most once per ``(type, entity_id, reminder_key)`` (bk-027 ``notifications_reminder_uq``,
    AA-682 critic B7). A duplicate is skipped with ON CONFLICT DO NOTHING, so it never aborts the caller's (job)
    transaction. True when the row was written."""
    written = await conn.fetchval(
        """
        INSERT INTO booking.notifications
            (recipient_type, recipient_id, recipient_ref, type, title, message, entity_type, entity_id, data)
        VALUES ('staff', NULL, $1, $2, $3, $4, $5, $6::uuid, $7::jsonb)
        ON CONFLICT DO NOTHING
        RETURNING id::text
        """,
        recipient_ref,
        type,
        title,
        message,
        entity_type,
        entity_id,
        json.dumps({**(data or {}), "reminder_key": reminder_key}, default=str),
    )
    return written is not None


async def notify_customer(
    conn: asyncpg.Connection,
    *,
    customer_id: str,
    type: str,  # noqa: A002
    title: str,
    message: str | None,
    entity_type: str,
    entity_id: str,
    data: dict[str, Any],
) -> str:
    return await conn.fetchval(
        """
        INSERT INTO booking.notifications
            (recipient_type, recipient_id, type, title, message, entity_type, entity_id, data)
        VALUES ('customer', $1::uuid, $2, $3, $4, $5, $6::uuid, $7::jsonb)
        RETURNING id::text
        """,
        customer_id,
        type,
        title,
        message,
        entity_type,
        entity_id,
        json.dumps(data, default=str),
    )


async def unread_count(conn: asyncpg.Connection, staff: Staff) -> int:
    return await conn.fetchval(
        f"SELECT count(*) FROM booking.notifications n WHERE {VISIBLE} AND NOT {IS_READ}",  # noqa: S608 — constants
        staff.cis_user_id,
    )


async def list_feed(
    conn: asyncpg.Connection,
    staff: Staff,
    *,
    unread: bool | None = None,
    type: str | None = None,  # noqa: A002
    q: str | None = None,
    before: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    args: list[Any] = [staff.cis_user_id]
    where = [VISIBLE]
    if unread is True:
        where.append(f"NOT {IS_READ}")
    elif unread is False:
        where.append(IS_READ)
    if type:
        args.append(type)
        where.append(f"n.type = ${len(args)}")
    if q:
        args.append(paging.contains(q))
        where.append(f"(n.title ILIKE ${len(args)} OR n.message ILIKE ${len(args)})")
    cursor = paging.decode(before)
    if cursor:
        args.extend(cursor)
        where.append(f"(n.created_at, n.id) < (${len(args) - 1}, ${len(args)}::uuid)")
    args.append(min(limit, MAX_PAGE))
    rows = await conn.fetch(
        f"""
        SELECT n.id::text AS id, n.type, n.title, n.message, n.entity_type, n.entity_id::text AS entity_id,
               n.data::text AS data, n.created_at, {IS_READ} AS read
        FROM booking.notifications n
        WHERE {" AND ".join(where)}
        ORDER BY n.created_at DESC, n.id DESC
        LIMIT ${len(args)}
        """,  # noqa: S608 — fixed fragments, values are bound
        *args,
    )
    items = [{**dict(r), "data": json.loads(r["data"]) if r["data"] else {}} for r in rows]
    return {
        "items": items,
        "unread_count": await unread_count(conn, staff),
        "next_before": paging.next_cursor(items, args[-1], at_key="created_at"),
    }


async def mark_read(conn: asyncpg.Connection, staff: Staff, *, ids: list[str] | None, all_: bool) -> dict[str, Any]:
    """Mark the given visible notifications (or all of them) read for this staff member. Idempotent."""
    if all_:
        status = await conn.execute(
            f"""
            INSERT INTO booking.notification_reads (notification_id, cis_user_id)
            SELECT n.id, $1 FROM booking.notifications n WHERE {VISIBLE}
            ON CONFLICT DO NOTHING
            """,  # noqa: S608 — constants
            staff.cis_user_id,
        )
    else:
        status = await conn.execute(
            f"""
            INSERT INTO booking.notification_reads (notification_id, cis_user_id)
            SELECT n.id, $1 FROM booking.notifications n WHERE {VISIBLE} AND n.id = ANY($2::uuid[])
            ON CONFLICT DO NOTHING
            """,  # noqa: S608 — constants
            staff.cis_user_id,
            ids or [],
        )
    marked = _rowcount(status)
    if marked:
        await audit.record(
            conn,
            actor_type="staff",
            actor_ref=staff.cis_user_id,
            action="notification.read",
            entity_type="staff_user",
            entity_id=staff.cis_user_id,
            after={"marked": marked, "all": all_},
            entity_label=staff.name or staff.cis_user_id,
            summary=f"Marked {marked} notification(s) as read",
        )
    return {"marked": marked, "unread_count": await unread_count(conn, staff)}


def _rowcount(status: str | None) -> int:
    """asyncpg ``execute`` returns a command tag such as ``INSERT 0 3``."""
    try:
        return int((status or "").rsplit(" ", 1)[-1])
    except ValueError:
        return 0
