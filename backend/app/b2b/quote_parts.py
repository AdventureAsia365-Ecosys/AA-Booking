"""The sold snapshot of a tour line (AA-1149 T15, bk-065 ``booking.b2b_quote_line_parts``).

:func:`app.b2b.components.price_lines` returns, for each ``tp:`` line, one :class:`~app.b2b.components.PartCost` per
component it priced. :func:`store` writes them in the transaction that stores the quote (append-only; role ``booking``
has INSERT and SELECT only). The hold confirms one fixed quote, so the parts of the confirmed quote are what was sold:
Generate on the Suppliers tab makes one supplier booking per part (:mod:`app.ops.component_bookings`), and a later
edit of the tour's components changes nothing sold. Parts of a line on a fixed departure are stored too (the
departure's costs are booked per departure, so nothing generates from them)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import asyncpg

from app.b2b.components import LineCost

CENT = Decimal("0.01")
INSERT_SQL = """
INSERT INTO booking.b2b_quote_line_parts (quote_id, line_no, part_no, day_no, service_date, hotel_room_type_id,
    service_id, supplier_id, quantity, nights, pax, rooms, net_amount, net_currency, supplier_rate_ids)
VALUES ($1::uuid, $2, $3, $4, $5, $6::uuid, $7::uuid, $8::uuid, $9, 1, $10, $11::jsonb, $12, $13, $14::uuid[])
"""


def rows(quote_id: str, line_nos: Sequence[int], costs: Sequence[LineCost]) -> list[tuple[Any, ...]]:
    """The INSERT arguments of every part of every line (``line_nos`` = the stored line number of each cost)."""
    out: list[tuple[Any, ...]] = []
    for line_no, cost in zip(line_nos, costs, strict=True):
        for p in cost.parts:
            out.append(
                (
                    quote_id,
                    line_no,
                    p.part_no,
                    p.day_no,
                    p.service_date,
                    p.room_type_id,
                    p.service_id,
                    p.supplier_id,
                    p.quantity,
                    p.pax,
                    json.dumps(p.rooms) if p.rooms is not None else None,
                    p.net.quantize(CENT, rounding=ROUND_HALF_UP) if p.net is not None else None,
                    p.currency,
                    p.rate_ids,
                )
            )
    return out


async def store(conn: asyncpg.Connection, quote_id: str, line_nos: Sequence[int], costs: Sequence[LineCost]) -> int:
    """Write the parts of the quote's tp: lines (inside the caller's transaction); returns how many."""
    args = rows(quote_id, line_nos, costs)
    if args:
        await conn.executemany(INSERT_SQL, args)
    return len(args)
