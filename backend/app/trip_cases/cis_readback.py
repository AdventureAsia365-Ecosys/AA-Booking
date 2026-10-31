"""CIS read-back of proposal items (AA-1013; root ADR 0002: Booking reads the catalog live and never copies it).

A version item made from a CIS tour keeps the text copied when the version was made: ``items_from_days`` (a trip
plan's "Use for trip case") writes one item per CIS day titled with that day's title, a Vn copies its items, and the
B2B confirm writes one item per trip plan line. CIS rewrites its tours (S1 v2, 25/10/2026), so a draft made before a
rewrite still shows the old day titles under a "From CIS" tag. Nothing changes the stored text by itself: this module
only tells staff what CIS says **today**, and the builder offers "Refresh from CIS" (a normal items save by staff).

Each item gets ``cis``: ``None`` when it has no catalog tour (no ``source_tour_id``, a key the catalog does not serve
any more, or the catalog could not be read), else::

    {"source_id": <catalog key>, "tour_title": <CIS tour title>, "tour_updated_at": <v_tour.updated_at>,
     "day_number": <the CIS day the item stands for, or None>, "title": <that day's CIS title, or None>,
     "changed": <the stored title differs from it>}

**Which CIS day an item stands for.** Only a tour whose items on the version are all one day long is compared day by
day (the shape ``items_from_days`` writes): the first such item is CIS day 1, the next day is day 2, and so on
(``day_from - first day_from + 1``), so a tour placed later in a trip still lines up. A multi-day item (a TripPlanner
leg, a B2B trip plan line, a tour without CIS days) is not compared: ``day_number`` / ``title`` are ``None`` and
``changed`` is false; ``tour_title`` still names the tour (the B2B lines of older cases carry a technical title).
Titles are compared with whitespace collapsed, like the S261 parity check.

**Reads.** ``catalog.v_tour`` once (by catalog key or CIS tour id, as ``app.inquiry.leg_keys`` does), then the
parse-backed ``catalog.v_tour_day`` once, keyed by ``tour_id = ANY`` only (AA-996 perf guard), and only for tours
compared day by day. Fail soft: both reads run in a savepoint and any database error or timeout leaves every ``cis``
``None`` (the version still loads); a CIS migration can never fail a Booking read or an items save.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

import asyncpg

log = logging.getLogger(__name__)

READ_ERRORS: tuple[type[Exception], ...] = (asyncpg.PostgresError, TimeoutError)
TITLE_MAX = 500  # trip_case_version_itinerary.title, and items_from_days cuts there too

# Both key kinds: staff "Use for trip case" and the B2B confirm write the catalog source_id; a TripPlanner leg item
# may carry a CIS tour_id. A source_id match wins over a tour_id match.
TOURS_SQL = (
    "SELECT source_id, tour_id::text AS tour_id, title::text AS title, updated_at FROM catalog.v_tour"
    " WHERE source_id = ANY($1::text[]) OR tour_id::text = ANY($1::text[])"
)
DAYS_SQL = (
    "SELECT tour_id::text AS tour_id, day_number, title FROM catalog.v_tour_day"
    " WHERE tour_id = ANY($1::uuid[]) ORDER BY tour_id, day_number"
)


def same_text(a: str | None, b: str | None) -> bool:
    return " ".join((a or "").split()) == " ".join((b or "").split())


def _resolve(tours: list[Any], keys: set[str]) -> dict[str, Any]:
    """{item key: tour row}; a source_id match wins over a tour_id match."""
    out: dict[str, Any] = {}
    for row in tours:
        if row["tour_id"] in keys and row["tour_id"] not in out:
            out[row["tour_id"]] = row
    for row in tours:
        if row["source_id"] in keys:
            out[row["source_id"]] = row
    return out


def day_numbers(items: list[dict[str, Any]], tour_of: dict[str, str]) -> dict[int, int]:
    """{item index: CIS day number} for the items of tours whose items on the version are all one day long."""
    groups: dict[str, list[int]] = defaultdict(list)
    for i, item in enumerate(items):
        tour_id = tour_of.get(item.get("source_tour_id") or "")
        if tour_id:
            groups[tour_id].append(i)
    out: dict[int, int] = {}
    for indexes in groups.values():
        if any(items[i]["day_from"] != items[i]["day_to"] for i in indexes):
            continue
        first = min(items[i]["day_from"] for i in indexes)
        for i in indexes:
            out[i] = items[i]["day_from"] - first + 1
    return out


Read = tuple[dict[str, Any], dict[int, tuple[str, int]], dict[tuple[str, int], str]]


async def _read(conn: asyncpg.Connection, items: list[dict[str, Any]], keys: set[str]) -> Read:
    """(tours by item key, {item index: (tour_id, CIS day number)}, {(tour_id, day number): CIS day title})."""
    async with conn.transaction():  # a savepoint inside a caller's transaction
        tours = _resolve(await conn.fetch(TOURS_SQL, sorted(keys)), keys)
        tour_of = {key: row["tour_id"] for key, row in tours.items()}
        numbers = {i: (tour_of[items[i]["source_tour_id"]], n) for i, n in day_numbers(items, tour_of).items()}
        day_titles: dict[tuple[str, int], str] = {}
        compared = sorted({tour_id for tour_id, _ in numbers.values()})
        if compared:
            for row in await conn.fetch(DAYS_SQL, compared):
                title = (row["title"] or "").strip()
                if title:
                    day_titles[(row["tour_id"], row["day_number"])] = title[:TITLE_MAX]
    return tours, numbers, day_titles


async def attach(conn: asyncpg.Connection, items: list[dict[str, Any]]) -> None:
    """Set ``item["cis"]`` on every item (see the module docstring). Never raises for a catalog problem."""
    for item in items:
        item["cis"] = None
    keys = {item["source_tour_id"] for item in items if item.get("source_tour_id")}
    if not keys:
        return
    try:
        tours, numbers, day_titles = await _read(conn, items, keys)
    except READ_ERRORS as exc:
        log.warning(
            "catalog read-back of proposal items skipped: error=%s sqlstate=%s",
            type(exc).__name__,
            getattr(exc, "sqlstate", None) or "-",
        )
        return
    for i, item in enumerate(items):
        tour = tours.get(item.get("source_tour_id") or "")
        if tour is None:
            continue
        number = numbers[i][1] if i in numbers else None
        title = day_titles.get(numbers[i]) if i in numbers else None
        item["cis"] = {
            "source_id": tour["source_id"],
            "tour_title": tour["title"],
            "tour_updated_at": tour["updated_at"],
            "day_number": number,
            "title": title,
            "changed": title is not None and not same_text(item.get("title"), title),
        }
