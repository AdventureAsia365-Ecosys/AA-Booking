"""How a B2B hold holds each line (``POST /holds``, :mod:`app.b2b.holds`): the departure answer, the quote's own
on-request answer, and the hotel allotment of a room line (AA-1153 part 2, S285 advisory r2 hotels B8).

- **The quote's on-request answer** (AA-1091, root ADR 0005b Q9): a line the quote answered on request (e.g. a group of
  the policy's size) is held on request too, never auto-confirmed because its departure has seats.
- **Room lines** (``rt:``, no departure): the departure check answers them on request. A room line the quote answered
  ``available`` (every night from an allotment of the pricing DMC with rooms left, or a free-sale term;
  :func:`app.supply.availability.room_line`) is checked again here under a transaction lock per room type, taken in id
  order (two holds of the same room type wait for each other, so the last room of an allotment is held once): still
  available on every night = held ``available`` (the confirm confirms it at once, no on-request desk), else held
  ``on_request`` (the desk asks the DMC). The rooms held count against the allotment from here on
  (:data:`app.supply.availability.HELD_ROOMS_SQL`); a released or expired hold frees them.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import asyncpg

from app.b2b import departures
from app.supply import availability
from app.supply.restrictions import SupplyAsk

LineResult = tuple[str, str | None, str | None]

# The room lines of a quote: room type, nights, room count (the room mix asked, else the default mix) and the DMC of
# the line's first rate (one DMC prices every night of a room line).
ROOM_LINES_SQL = f"""
SELECT q.line_no, substr(q.component_id, 4) AS room_type_id, q.service_date,
       greatest(coalesce(q.nights, 1), 1) AS nights, q.pax_adult + q.pax_child AS party,
       {availability.ROOMS_OF_LINE} AS rooms, k.supplier_id::text AS supplier_id
FROM booking.b2b_quote_lines q
JOIN booking.supplier_rates x ON x.id = q.supplier_rate_ids[1]
JOIN booking.supplier_contracts k ON k.id = x.supplier_contract_id
WHERE q.quote_id = $1::uuid AND q.line_no = ANY($2::smallint[]) AND q.component_id LIKE 'rt:%'
ORDER BY q.line_no
"""  # noqa: S608 — constant fragment
LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtextextended('booking.room_allotment:' || $1::text, 0))"


def quote_answer(lines: list[Any], result: list[LineResult]) -> list[LineResult]:
    """Q9: an available line the quote answered on request is held on request."""
    return [
        (departures.ON_REQUEST, dep_id, why)
        if availability_ == departures.AVAILABLE and r.get("availability") == departures.ON_REQUEST
        else (availability_, dep_id, why)
        for r, (availability_, dep_id, why) in zip(lines, result, strict=True)
    ]


def _room_lines(lines: list[Any], result: list[LineResult]) -> list[int]:
    """Line numbers of the room lines the quote answered available (no departure)."""
    return [
        int(r["line_no"])
        for r, (_, dep_id, _) in zip(lines, result, strict=True)
        if str(r["component_id"]).startswith("rt:") and r.get("availability") == departures.AVAILABLE and not dep_id
    ]


async def held(
    conn: asyncpg.Connection, quote_id: str, lines: list[Any], result: list[LineResult], *, today: date
) -> list[LineResult]:
    """The per-line result a hold writes (call inside the hold's transaction): :func:`quote_answer`, then a room line
    the quote answered available held ``available`` only when its allotment or free-sale term still holds it."""
    result = quote_answer(lines, result)
    wanted = _room_lines(lines, result)
    if not wanted:
        return result
    rows = await conn.fetch(ROOM_LINES_SQL, quote_id, wanted)
    for room_type in sorted({r["room_type_id"] for r in rows}):
        await conn.execute(LOCK_SQL, room_type)
    asks: list[tuple[int, SupplyAsk]] = []
    for r in rows:
        for n in range(int(r["nights"])):
            on = r["service_date"] + timedelta(days=n)
            ask = SupplyAsk(r["room_type_id"], None, on, max(int(r["party"]), 1), int(r["rooms"]), r["supplier_id"])
            asks.append((int(r["line_no"]), ask))
    verdicts = await availability.many(conn, [a for _, a in asks], today=today)
    fits: dict[int, bool] = {}
    for (line_no, _), verdict in zip(asks, verdicts, strict=True):
        fits[line_no] = fits.get(line_no, True) and verdict.status == departures.AVAILABLE
    return [
        (departures.AVAILABLE if fits.get(int(r["line_no"])) else departures.ON_REQUEST, dep_id, why)
        if int(r["line_no"]) in wanted
        else (availability_, dep_id, why)
        for r, (availability_, dep_id, why) in zip(lines, result, strict=True)
    ]
