"""Reads of the CIS catalog read model (CIS migration 225, ``catalog.v_destination``; grant to role ``booking``).

Booking never writes CIS places (ADR 0002). A supplier stores a CIS ``destination_id`` without a foreign key (no
REFERENCES across schemas), so the API checks it here on every write and resolves names on reads.

Each query runs in its own (sub)transaction: inside a caller's transaction that is a savepoint, so a missing view
or grant fails only this read, not the caller's whole transaction.

- Writes fail closed: an unknown id is 422, an unreadable catalog is 503 (``catalog_unavailable``).
- Reads fail soft: when the catalog cannot be read, the place name is simply missing.

``v_destination.country`` is a country name (``Vietnam``), mapped to ISO-2 through ``booking.countries`` (bk-005).
"""

from __future__ import annotations

import logging
from typing import Any

import asyncpg

from app import paging
from app.errors import Invalid, Unavailable

log = logging.getLogger(__name__)

# A catalog that is not there (225 not applied), not granted, or too slow to answer reads as "unavailable", never as a
# 500. The views are security_barrier views over CIS tables, read live: a read past the server ``statement_timeout``
# is ``QueryCanceledError``, a read past the pool's client-side ``command_timeout`` is ``TimeoutError`` (asyncio's
# alias since Python 3.11). Fail-closed callers answer 503, fail-soft callers drop the catalog fields.
CATALOG_ERRORS: tuple[type[Exception], ...] = (
    asyncpg.UndefinedTableError,
    asyncpg.InvalidSchemaNameError,
    asyncpg.InsufficientPrivilegeError,
    asyncpg.QueryCanceledError,
    TimeoutError,
)

MAX_SEARCH = 50

DESTINATION_SQL = """
    SELECT d.destination_id::text AS destination_id, d.name, d.country, c.iso2::text AS country_code
    FROM catalog.v_destination d
    LEFT JOIN booking.countries c ON lower(c.name) = lower(d.country)
"""


async def destinations_by_id(conn: asyncpg.Connection, ids: list[str]) -> dict[str, dict[str, Any]]:
    """``{destination_id: {destination_id, name, country, country_code}}`` for the ids found; ``{}`` when the catalog
    cannot be read (fail soft, logged)."""
    wanted = sorted({i for i in ids if i})
    if not wanted:
        return {}
    try:
        async with conn.transaction():
            rows = await conn.fetch(f"{DESTINATION_SQL} WHERE d.destination_id = ANY($1::uuid[])", wanted)  # noqa: S608
    except CATALOG_ERRORS as exc:
        log.warning("catalog.v_destination unreadable, place names omitted: %s", type(exc).__name__)
        return {}
    return {r["destination_id"]: dict(r) for r in rows}


async def require_destination(conn: asyncpg.Connection, destination_id: str) -> dict[str, Any]:
    """The catalog place, or 422 ``unknown_destination``; 503 ``catalog_unavailable`` when the view cannot be read."""
    try:
        async with conn.transaction():
            row = await conn.fetchrow(f"{DESTINATION_SQL} WHERE d.destination_id = $1::uuid", destination_id)  # noqa: S608
    except CATALOG_ERRORS as exc:
        raise Unavailable("catalog_unavailable", "the CIS catalog cannot be read right now; try again later") from exc
    if row is None:
        raise Invalid("unknown_destination", f"destination {destination_id} is not a CIS catalog place")
    return dict(row)


async def search_destinations(
    conn: asyncpg.Connection, *, q: str | None = None, country: str | None = None, limit: int = 20
) -> dict[str, Any]:
    """Places for a picker: name contains ``q``, optional ISO-2 ``country``. 503 when the catalog cannot be read."""
    pattern = paging.contains(q) if q and q.strip() else None
    try:
        async with conn.transaction():
            rows = await conn.fetch(
                f"""{DESTINATION_SQL}
                WHERE ($1::text IS NULL OR d.name ILIKE $1)
                  AND ($2::text IS NULL OR c.iso2 = $2)
                ORDER BY lower(d.name), d.country, d.destination_id
                LIMIT $3""",  # noqa: S608 — constant
                pattern,
                country.upper() if country else None,
                min(limit, MAX_SEARCH),
            )
    except CATALOG_ERRORS as exc:
        raise Unavailable("catalog_unavailable", "the CIS catalog cannot be read right now; try again later") from exc
    return {"items": [dict(r) for r in rows]}
