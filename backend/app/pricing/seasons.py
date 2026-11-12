"""The one season function (AA-1091, bk-053; root ADR 0005b decision 4).

A **season calendar** (``booking.season_calendars``) belongs to a destination (AA's sell-side seasons) or to a
supplier contract. Its approved, not ended ranges (``booking.season_ranges``) give each date one season code:

- a **festive range that names a holiday event** (country + event code, with days before and after) takes its dates
  from every approved, not ended row of that event (``booking.holiday_events``, one per year): entering next year's
  Tet fills the festive range of every calendar that references it, and no holiday date is ever written in code. Such
  a range lies over the fixed-date ranges on its days (the event's dates move every year; refusing the overlap would
  force staff to cut the high season around Tet by hand each year, copying the dates d4 forbids);
- else the **fixed-date season range** covering the date (they never overlap inside one calendar: the admin refuses
  a save that would make two cover one date, :func:`overlapping`);
- else the calendar's ``default_season`` (``low`` unless set).

A **stop-sale range** is an overlay: it never changes the season or the price, only availability (a quote line on a
stop-sale date goes ``on_request``, :mod:`app.b2b.quotes`).

**Peak.** The pricing rule has one uplift (``peak_uplift_pts``); it applies on ``peak`` and ``festive`` dates
(:data:`PEAK_CODES`). This replaces the old guess "a season label containing the word peak" in the cost sheet and the
``peak = false`` of the B2B quote: the cost sheet (each start-date window), the B2B quote and tariff (the line's
service date) and the price list versions (each matrix row piece) all ask :meth:`Calendar.season_on` of the trip
plan's calendar (``trip_plans.season_calendar_id``), so the agency price and the RRP follow one season logic.

**Until staff enter seasons** (:attr:`Calendar.configured` is false: no calendar, or one with no approved season
range and the default ``low``), nothing prices differently from before bk-053: the B2B quote and the tariff price
every date ``low`` (their old ``peak = false``), and the matrix keeps the old peak flags (:data:`LEGACY_PEAK`): the cost
sheet's computed rows take the peak uplift when a 2-traveller rate's season label says "peak" (the old rule), and a
hand row keeps the peak flag it was written with. The first approved range (or default season) of the plan's calendar
switches the plan to the calendar.

Dates are half-open ``[start, end)`` inside (d10); the API shows inclusive end dates.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import asyncpg

SEASON_CODES: tuple[str, ...] = ("low", "high", "peak", "festive")
PEAK_CODES = frozenset({"peak", "festive"})
DEFAULT_SEASON = "low"
ONE_DAY = timedelta(days=1)


@dataclass(frozen=True)
class Span:
    """A half-open date range ``[start, end)`` with a season code (None for a stop-sale range)."""

    start: date
    end: date
    code: str | None = None
    range_id: str | None = None

    def covers(self, day: date) -> bool:
        return self.start <= day < self.end

    def overlaps(self, other: Span) -> bool:
        return self.start < other.end and other.start < self.end


@dataclass(frozen=True)
class SeasonDay:
    """What the calendar says about one date."""

    code: str
    stop_sale: bool

    @property
    def peak(self) -> bool:
        return self.code in PEAK_CODES

    @property
    def festive(self) -> bool:
        return self.code == "festive"


@dataclass(frozen=True)
class Calendar:
    """The approved ranges of one calendar, holiday events already resolved to dates."""

    id: str | None = None
    default: str = DEFAULT_SEASON
    fixed: tuple[Span, ...] = ()
    festive: tuple[Span, ...] = ()
    stop_sales: tuple[Span, ...] = ()
    name: str | None = None

    @property
    def configured(self) -> bool:
        """True once the calendar says something about seasons: an approved season range that resolves to dates, or
        a default season other than ``low``. Until then the old peak flags stay (see the module doc)."""
        return bool(self.fixed or self.festive) or self.default != DEFAULT_SEASON

    def season_on(self, day: date) -> SeasonDay:
        """THE season function: the season code of ``day`` and whether it is a stop-sale date."""
        code = self.default
        if any(s.covers(day) for s in self.festive):
            code = "festive"
        else:
            for span in self.fixed:
                if span.covers(day):
                    code = span.code or self.default
                    break
        return SeasonDay(code, any(s.covers(day) for s in self.stop_sales))

    def cuts(self, start: date, end: date) -> list[date]:
        """The dates in ``(start, end)`` where the season may change (range edges), sorted."""
        edges = {d for s in (*self.fixed, *self.festive) for d in (s.start, s.end)}
        return sorted(d for d in edges if start < d < end)

    def segments(self, start: date, end: date) -> list[Span]:
        """``[start, end)`` cut into pieces of one season each (neighbours of the same season joined)."""
        if end <= start:
            return []
        bounds = [start, *self.cuts(start, end), end]
        out: list[Span] = []
        for a, b in zip(bounds, bounds[1:], strict=False):
            code = self.season_on(a).code
            if out and out[-1].code == code and out[-1].end == a:
                out[-1] = Span(out[-1].start, b, code)
            else:
                out.append(Span(a, b, code))
        return out


EMPTY = Calendar()


def legacy_code(peak: bool) -> str:
    """The season code a row of a plan whose calendar is not :attr:`~Calendar.configured` carries: its old peak flag
    as ``peak``, else the default season."""
    return "peak" if peak else DEFAULT_SEASON


LEGACY_PEAK = "peak"  # the substring of a supplier rate's season label that made a computed row peak before bk-053


@dataclass
class _Rows:
    fixed: list[Span] = field(default_factory=list)
    festive: list[tuple[str, str, int, int, str]] = field(default_factory=list)
    stop: list[Span] = field(default_factory=list)


CALENDARS_SQL = """
SELECT c.id::text AS id, c.default_season, c.name FROM booking.season_calendars c WHERE c.id = ANY($1::uuid[])
"""
# Approved, not ended ranges of the calendars.
RANGES_SQL = """
SELECT r.id::text AS id, r.calendar_id::text AS calendar_id, r.kind, r.season_code, r.date_from, r.date_to,
       r.holiday_country::text AS holiday_country, r.holiday_code, r.days_before, r.days_after
FROM booking.season_ranges r
WHERE r.calendar_id = ANY($1::uuid[]) AND r.status = 'approved' AND r.ended_at IS NULL
"""
# Approved, not ended holiday event rows (every year) of the events the ranges name.
EVENTS_SQL = """
SELECT e.country_iso2::text AS country, e.code, e.date_from, e.date_to
FROM booking.holiday_events e
WHERE e.status = 'approved' AND e.ended_at IS NULL AND (e.country_iso2::text || ':' || e.code) = ANY($1::text[])
"""


def event_spans(refs: Iterable[tuple[str, str, int, int, str]], events: Sequence[Any]) -> list[Span]:
    """Festive spans of event references ``(country, code, days_before, days_after, range_id)`` from event rows."""
    by_key: dict[tuple[str, str], list[Any]] = {}
    for e in events:
        by_key.setdefault((str(e["country"]).strip(), e["code"]), []).append(e)
    out: list[Span] = []
    for country, code, before, after, rid in refs:
        for e in by_key.get((country, code), ()):
            out.append(
                Span(e["date_from"] - timedelta(days=before), e["date_to"] + timedelta(days=after), "festive", rid)
            )
    return sorted(out, key=lambda s: (s.start, s.end))


async def load_many(conn: asyncpg.Connection, calendar_ids: Iterable[str]) -> dict[str, Calendar]:
    """The calendars by id (three queries whatever the number)."""
    ids = sorted({c for c in calendar_ids if c})
    if not ids:
        return {}
    heads = {r["id"]: r for r in await conn.fetch(CALENDARS_SQL, ids)}
    rows: dict[str, _Rows] = {i: _Rows() for i in heads}
    for r in await conn.fetch(RANGES_SQL, ids):
        bucket = rows[r["calendar_id"]]
        if r["kind"] == "stop_sale":
            bucket.stop.append(Span(r["date_from"], r["date_to"], None, r["id"]))
        elif r["holiday_code"]:
            bucket.festive.append(
                (str(r["holiday_country"]).strip(), r["holiday_code"], int(r["days_before"]), int(r["days_after"]),
                 r["id"])
            )  # fmt: skip
        else:
            bucket.fixed.append(Span(r["date_from"], r["date_to"], r["season_code"], r["id"]))
    keys = sorted({f"{c}:{code}" for b in rows.values() for c, code, *_ in b.festive})
    events = list(await conn.fetch(EVENTS_SQL, keys)) if keys else []
    return {
        cid: Calendar(
            id=cid,
            default=head["default_season"],
            fixed=tuple(sorted(rows[cid].fixed, key=lambda s: s.start)),
            festive=tuple(event_spans(rows[cid].festive, events)),
            stop_sales=tuple(sorted(rows[cid].stop, key=lambda s: s.start)),
            name=head["name"],
        )
        for cid, head in heads.items()
    }


PLAN_CALENDARS_SQL = """
SELECT p.id::text AS plan_id, p.season_calendar_id::text AS calendar_id
FROM booking.trip_plans p WHERE p.id = ANY($1::uuid[])
"""


async def for_plans(conn: asyncpg.Connection, plan_ids: Iterable[str]) -> dict[str, Calendar]:
    """Each plan's sell-side calendar (:data:`EMPTY` for a plan without one)."""
    ids = sorted({p for p in plan_ids if p})
    if not ids:
        return {}
    links = {r["plan_id"]: r["calendar_id"] for r in await conn.fetch(PLAN_CALENDARS_SQL, ids)}
    cals = await load_many(conn, [c for c in links.values() if c])
    return {p: cals.get(links.get(p) or "", EMPTY) for p in ids}


async def for_plan(conn: asyncpg.Connection, plan_id: str) -> Calendar:
    return (await for_plans(conn, [plan_id])).get(plan_id, EMPTY)


def overlapping(new: Span, existing: Iterable[Span]) -> list[Span]:
    """The fixed-date season ranges of a calendar a new one would overlap (a save that returns any is refused)."""
    return [s for s in existing if s.overlaps(new)]
