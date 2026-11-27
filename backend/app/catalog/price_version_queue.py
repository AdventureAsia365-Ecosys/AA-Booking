"""The checker's queue of price list versions (AA-1190): one WHERE for the pending list
(``GET /catalog/price-versions?status=pending[&approvable=me]``, :func:`app.catalog.price_versions.pending`) and the My
work tile "Price versions to approve" (:func:`approval_queue`), so the tile equals the list.

``$1`` = a maker whose versions are left out (``approvable=me``: the caller, unless the business owner may approve her
own work, AA-1158 Q5), NULL = every pending version.
"""

from __future__ import annotations

from typing import Any

import asyncpg

from app.auth import self_approval
from app.auth.staff import Staff

PENDING_WHERE = "v.status = 'pending' AND ($1::text IS NULL OR v.created_by IS DISTINCT FROM $1)"
APPROVAL_QUEUE_SQL = f"""
SELECT count(*)::int AS count, min(v.submitted_at) AS oldest_at FROM booking.price_list_versions v WHERE {PENDING_WHERE}
"""  # noqa: S608 — constants only


async def not_by(conn: asyncpg.Connection, staff: Staff) -> str | None:
    """The maker left out of ``approvable=me``: the caller, unless they may approve their own work."""
    return None if await self_approval.may_self_approve(conn, staff) else staff.cis_user_id


async def pending_rows(
    conn: asyncpg.Connection, staff: Staff, version_sql: str, *, approvable: bool = False
) -> list[asyncpg.Record]:
    """The pending list's rows (``version_sql`` = the list's SELECT ... FROM), oldest first."""
    maker = await not_by(conn, staff) if approvable else None
    return await conn.fetch(f"{version_sql} WHERE {PENDING_WHERE} ORDER BY v.submitted_at", maker)  # noqa: S608


async def approval_queue(conn: asyncpg.Connection, staff: Staff) -> dict[str, Any]:
    """How many pending versions the caller may approve and when the oldest was submitted."""
    row = await conn.fetchrow(APPROVAL_QUEUE_SQL, await not_by(conn, staff))
    return {"count": row["count"], "oldest_at": row["oldest_at"]}
