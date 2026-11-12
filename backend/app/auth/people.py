"""Who made or decided a change, as a person the staff admin can show (AA-1091).

The maker-checker tables store the staff member's ``cis_user_id`` (``created_by``, ``approved_by`` ...), or a
system marker (``migration:bk-052``, ``system:bk_trip_plan_sync``, ``seed:bk-050``). The API returns each as
``{"id": ..., "name": ...}`` (the shape of Pricing > Policy, :mod:`app.pricing.policy`): ``name`` from the staff
directory (``booking.staff_users``), None for a marker or a person not in it. The admin shows the name, a label for
a marker, and the id only as a fallback.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

import asyncpg

NAMES_SQL = "SELECT cis_user_id, name FROM booking.staff_users WHERE cis_user_id = ANY($1::text[])"


def person(ref: str | None, names: dict[str, str | None]) -> dict[str, Any] | None:
    if ref is None:
        return None
    return {"id": ref, "name": names.get(ref)}


async def names_of(conn: asyncpg.Connection, refs: Iterable[str | None]) -> dict[str, str | None]:
    """The directory names of the staff refs (one query; markers never match a staff row)."""
    wanted = sorted({r for r in refs if r})
    if not wanted:
        return {}
    return {r["cis_user_id"]: r["name"] for r in await conn.fetch(NAMES_SQL, wanted)}


async def resolve(conn: asyncpg.Connection, items: Sequence[dict[str, Any]], fields: Sequence[str]) -> None:
    """Replace each of ``fields`` of every item (a staff ref or None) by its person, in place."""
    names = await names_of(conn, (item.get(f) for item in items for f in fields))
    for item in items:
        for f in fields:
            if f in item:
                item[f] = person(item[f], names)
