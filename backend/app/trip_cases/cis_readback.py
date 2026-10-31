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

**Which CIS day an item stands for.** Only the items of a tour that look like a day-by-day copy (the shape
``items_from_days`` writes) are compared day by day; ``day_copies`` and ``day_numbers`` decide, per tour on the
version:

- every item of the tour is one day long. A multi-day item (a TripPlanner leg, a multi-day B2B trip plan line, a
  tour without CIS days) is never compared;
- no item is a B2B confirm item: its notes start with ``Agency line`` (``app.b2b.holds``, AA-1013) or one of its
  costs is a ``B2B quote`` line (every B2B confirm, older cases too). A B2B line names what was sold, never a day,
  even when the trip plan spans one day or a tour sits on two one-day lines;
- no item's title is the CIS tour title (it names the tour, so it is not out of date) or the TripPlanner fallback
  ``AA tour <key>`` (``sql.leg_title``);
- the tour has at least two such items, or exactly one item and CIS has exactly one day for it. A lone one-day item
  of a multi-day CIS tour (a one-day TripPlanner leg, what is left after staff deleted days) is not a day copy.

The first item of a compared tour is CIS day 1, the next day is day 2, and so on (``day_from - first day_from + 1``),
so a tour placed later in a trip still lines up. An item that is not compared has ``day_number`` / ``title`` ``None``
and ``changed`` false; ``tour_title`` still names the tour (older B2B lines carry a technical title, and the UI shows
the tour name under every item that is not a day copy). Titles are compared with whitespace collapsed, like the S261
parity check.

**Reads.** ``catalog.v_tour`` once (by catalog key or CIS tour id, as ``app.inquiry.leg_keys`` does), then the
parse-backed ``catalog.v_tour_day`` once, keyed by ``tour_id = ANY`` only (AA-996 perf guard), and only for tours
that pass the item checks above. Fail soft: both reads run in a savepoint and any database error or timeout leaves
every ``cis`` ``None`` (the version still loads); a CIS migration can never fail a Booking read or an items save.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

import asyncpg

from app.trip_cases import sql

log = logging.getLogger(__name__)

READ_ERRORS: tuple[type[Exception], ...] = (asyncpg.PostgresError, TimeoutError)
TITLE_MAX = 500  # trip_case_version_itinerary.title, and items_from_days cuts there too
# What the B2B confirm writes (app.b2b.holds): each item's notes (AA-1013) and each item's cost line (since bk-020).
B2B_ITEM_NOTE = "Agency line "
B2B_COST_NOTE = "B2B quote "

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


def _b2b_confirm_item(item: dict[str, Any]) -> bool:
    if (item.get("notes") or "").startswith(B2B_ITEM_NOTE):
        return True
    return any((c.get("supplier_note") or "").startswith(B2B_COST_NOTE) for c in item.get("costs") or [])


def _names_the_tour(item: dict[str, Any], tour: Any) -> bool:
    title = item.get("title")
    return same_text(title, tour["title"]) or same_text(title, sql.leg_fallback_title(item["source_tour_id"]))


def day_copies(items: list[dict[str, Any]], tours: dict[str, Any]) -> dict[str, list[int]]:
    """{CIS tour_id: item indexes} for the tours whose items on the version pass the item checks of the module
    docstring (one day long, no B2B confirm item, no title naming the tour); the CIS day count check comes later."""
    groups: dict[str, list[int]] = defaultdict(list)
    for i, item in enumerate(items):
        tour = tours.get(item.get("source_tour_id") or "")
        if tour is not None:
            groups[tour["tour_id"]].append(i)
    out: dict[str, list[int]] = {}
    for tour_id, indexes in groups.items():
        tour = tours[items[indexes[0]]["source_tour_id"]]
        if any(
            items[i]["day_from"] != items[i]["day_to"] or _b2b_confirm_item(items[i]) or _names_the_tour(items[i], tour)
            for i in indexes
        ):
            continue
        out[tour_id] = indexes
    return out


def day_numbers(items: list[dict[str, Any]], copies: dict[str, list[int]], cis_days: dict[str, int]) -> dict[int, int]:
    """{item index: CIS day number} for the tours of ``day_copies`` with two or more items, or one item when CIS has
    exactly one day for the tour (``cis_days``: tour_id -> number of CIS days)."""
    out: dict[int, int] = {}
    for tour_id, indexes in copies.items():
        if len(indexes) < 2 and cis_days.get(tour_id, 0) != 1:
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
        copies = day_copies(items, tours)
        day_titles: dict[tuple[str, int], str] = {}
        cis_days: dict[str, set[int]] = defaultdict(set)
        if copies:
            for row in await conn.fetch(DAYS_SQL, sorted(copies)):
                cis_days[row["tour_id"]].add(row["day_number"])
                title = (row["title"] or "").strip()
                if title:
                    day_titles[(row["tour_id"], row["day_number"])] = title[:TITLE_MAX]
        counts = {tour_id: len(days) for tour_id, days in cis_days.items()}
        tour_of = {i: tour_id for tour_id, indexes in copies.items() for i in indexes}
        numbers = {i: (tour_of[i], n) for i, n in day_numbers(items, copies, counts).items()}
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
