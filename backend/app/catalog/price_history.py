"""Dated price history of a trip plan (``booking.trip_plan_price_history``, bk-039, AA-1029).

The price matrix (``booking.trip_plan_prices``) is a current-state list replaced as a set. Each change of it, by staff
(``PUT …/prices``) or by the cost sheet (``bk_price_recompute``), is recorded here in the same transaction: the open
history rows of the plan are closed (``effective_to = now()``) and the new set is inserted open (``effective_from =
now()``). "The price list in force at time T" is then the rows with ``effective_from <= T < coalesce(effective_to,
infinity)`` (:func:`as_of`). Role ``booking`` may only insert rows and set ``effective_to`` (column grant), so a past
price can never be rewritten.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

import asyncpg

SOURCES = ("staff", "cost_sheet")
COLUMNS = (
    "channel",
    "season",
    "peak",
    "valid_from",
    "valid_to",
    "pax_min",
    "pax_max",
    "currency",
    "amount",
    "rrp_amount",
    "net_amount",
    "net_currency",
    "effective_margin",
    "computed",
    "overridden",
)

CLOSE_SQL = """
UPDATE booking.trip_plan_price_history SET effective_to = now()
WHERE trip_plan_id = $1::uuid AND effective_to IS NULL
"""
INSERT_SQL = f"""
INSERT INTO booking.trip_plan_price_history (trip_plan_id, {", ".join(COLUMNS)}, change_source, changed_by)
VALUES ($1::uuid, {", ".join(f"${i + 2}" for i in range(len(COLUMNS)))}, ${len(COLUMNS) + 2}, ${len(COLUMNS) + 3})
"""  # noqa: S608 — constant column list
AS_OF_SQL = f"""
SELECT {", ".join(f"h.{c}" for c in COLUMNS)}, h.change_source, h.changed_by, h.effective_from, h.effective_to
FROM booking.trip_plan_price_history h
WHERE h.trip_plan_id = $1::uuid AND h.effective_from <= $2 AND (h.effective_to IS NULL OR h.effective_to > $2)
ORDER BY h.channel, h.currency, h.valid_from, h.pax_min
"""  # noqa: S608 — constant column list
CHANGES_SQL = """
SELECT effective_from AS at, change_source, changed_by, count(*) AS rows
FROM booking.trip_plan_price_history
WHERE trip_plan_id = $1::uuid
GROUP BY effective_from, change_source, changed_by
ORDER BY effective_from DESC
LIMIT $2
"""


def _value(row: dict[str, Any], column: str) -> Any:
    value = row.get(column)
    if column in ("computed", "overridden", "peak"):
        return bool(value)
    if column in ("currency", "net_currency") and isinstance(value, str):
        return value.strip() or None
    return value


async def record(
    conn: asyncpg.Connection, plan_id: str, rows: Sequence[dict[str, Any]], *, source: str, by: str
) -> None:
    """Close the plan's open rows and open the new set (call inside the transaction that replaced the matrix)."""
    if source not in SOURCES:
        raise ValueError(source)
    await conn.execute(CLOSE_SQL, plan_id)
    if rows:
        await conn.executemany(
            INSERT_SQL, [(plan_id, *(_value(r, c) for c in COLUMNS), source, by[:128]) for r in rows]
        )


async def as_of(conn: asyncpg.Connection, plan_id: str, at: datetime) -> list[dict[str, Any]]:
    """The plan's price list as it was in force at ``at``."""
    return [dict(r) for r in await conn.fetch(AS_OF_SQL, plan_id, at)]


async def changes(conn: asyncpg.Connection, plan_id: str, limit: int = 50) -> list[dict[str, Any]]:
    """The plan's matrix changes, newest first: when, by what, by whom and how many rows were in force after."""
    return [dict(r) for r in await conn.fetch(CHANGES_SQL, plan_id, limit)]
