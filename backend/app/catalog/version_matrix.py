"""Which published price list version prices which date, and the hand rows of the effective matrix (AA-1091, bk-052;
root ADR 0005b decisions 2, 4 and 12).

**Overlap rule (d2).** Among the published versions of a plan whose travel window covers a date (and whose sale
window, when set, covers the quote day), a non-provisional version wins over a provisional one; among the rest the
latest approval wins, then the highest ``version_no`` (:func:`rank`). A partly overlapped version keeps pricing the
dates no newer version covers. It is **superseded** only when versions that beat it together cover all of its window
(:func:`superseded`); a version with an end to its sale window never supersedes another (its offer ends, the older
price must come back for later quote days). Withdrawing a published version applies the rule again to those left, so
a version it had superseded prices its uncovered dates again.

**The effective matrix** (``booking.trip_plan_prices``, read by the tariff, the RRP, the from price and the readiness
report) holds the hand rows of the published versions, each row cut to the dates its version wins today and cut again
where the plan's season calendar changes season (:mod:`app.pricing.seasons`: ``season_code`` and the ``peak`` flag of
each piece), plus the cost sheet's computed rows around them (:mod:`app.catalog.cost_sheet`). Pure functions here; the
one database read is :func:`hand_rows`.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import asyncpg

from app.pricing import seasons

ONE_DAY = timedelta(days=1)
Interval = tuple[date, date]  # half-open [start, end)


@dataclass(frozen=True)
class Version:
    """What the overlap rule needs of a published version."""

    id: str
    version_no: int
    travel_from: date
    travel_to: date
    approved_at: datetime
    provisional: bool = False
    sale_from: date | None = None
    sale_to: date | None = None

    @property
    def window(self) -> Interval:
        return (self.travel_from, self.travel_to)

    def sells_on(self, day: date) -> bool:
        return (self.sale_from is None or self.sale_from <= day) and (self.sale_to is None or day < self.sale_to)


def rank(v: Version) -> tuple[bool, datetime, int]:
    """Higher wins: non-provisional first, then the latest approval, then the highest version number."""
    return (not v.provisional, v.approved_at, v.version_no)


def subtract(spans: Sequence[Interval], cut: Interval) -> list[Interval]:
    """``spans`` without ``cut``."""
    out: list[Interval] = []
    for start, end in spans:
        if cut[1] <= start or end <= cut[0]:
            out.append((start, end))
            continue
        if start < cut[0]:
            out.append((start, cut[0]))
        if cut[1] < end:
            out.append((cut[1], end))
    return out


def intersect(a: Interval, b: Interval) -> Interval | None:
    start, end = max(a[0], b[0]), min(a[1], b[1])
    return (start, end) if start < end else None


def winning(versions: Iterable[Version], day: date) -> dict[str, list[Interval]]:
    """The dates each published version prices for a quote made on ``day`` (empty list = it prices nothing)."""
    out: dict[str, list[Interval]] = {}
    taken: list[Interval] = []
    for v in sorted(versions, key=rank, reverse=True):
        if not v.sells_on(day):
            out[v.id] = []
            continue
        spans = [v.window]
        for cut in taken:
            spans = subtract(spans, cut)
        out[v.id] = spans
        taken.append(v.window)
    return out


def superseded(versions: Iterable[Version]) -> set[str]:
    """The published versions that versions beating them (with no end to their sale window) cover entirely."""
    ordered = sorted(versions, key=rank, reverse=True)
    out: set[str] = set()
    for i, v in enumerate(ordered):
        left = [v.window]
        for better in ordered[:i]:
            if better.sale_to is None:
                left = subtract(left, better.window)
        if not left:
            out.add(v.id)
    return out


def uncovered(window: Interval, versions: Iterable[Version], day: date) -> list[Interval]:
    """The dates of ``window`` no version prices for a quote made on ``day`` (they are not quotable)."""
    left = [window]
    for spans in winning(versions, day).values():
        for span in spans:
            left = subtract(left, span)
    return left


def pieces(row_window: Interval, wins: Sequence[Interval], calendar: seasons.Calendar) -> list[tuple[date, date, str]]:
    """A version row cut to the dates its version wins, then where the calendar's season changes:
    ``(start, end, season_code)`` half-open."""
    out: list[tuple[date, date, str]] = []
    for win in wins:
        part = intersect(row_window, win)
        if part is None:
            continue
        for seg in calendar.segments(*part):
            out.append((seg.start, seg.end, seg.code or calendar.default))
    return out


# ── Database part ────────────────────────────────────────────────────────────

PUBLISHED_SQL = """
SELECT v.id::text AS id, v.version_no, v.travel_from, v.travel_to, v.sale_from, v.sale_to, v.approved_at,
       v.source = 'provisional' AS provisional, v.status
FROM booking.price_list_versions v
WHERE v.trip_plan_id = $1::uuid AND v.status IN ('published', 'superseded')
"""
# The hand rows of versions (a version migrated from the history may hold the cost sheet's computed rows as a
# snapshot: those stay with the cost sheet). created_by only when it is a staff member (the matrix column's FK).
VERSION_ROWS_SQL = """
SELECT r.version_id::text AS version_id, r.position, r.channel, r.season, r.travel_from, r.travel_to, r.pax_min,
       r.pax_max, r.currency::text AS currency, r.amount, r.rrp_amount, r.net_amount,
       r.net_currency::text AS net_currency,
       r.effective_margin, r.priced_on, r.overridden, r.child_amount, r.infant_amount, r.single_supplement_amount,
       r.child_net_amount, r.infant_net_amount, r.single_supplement_net_amount,
       CASE WHEN EXISTS (SELECT 1 FROM booking.staff_users s WHERE s.cis_user_id = v.created_by)
            THEN v.created_by END AS created_by
FROM booking.price_list_version_rows r
JOIN booking.price_list_versions v ON v.id = r.version_id
WHERE r.version_id = ANY($1::uuid[]) AND (NOT r.computed OR r.overridden)
ORDER BY v.version_no, r.position
"""
PAX_FIELDS = (
    "child_amount",
    "infant_amount",
    "single_supplement_amount",
    "child_net_amount",
    "infant_net_amount",
    "single_supplement_net_amount",
)


def version_of(row: Any) -> Version:
    return Version(
        id=row["id"],
        version_no=int(row["version_no"]),
        travel_from=row["travel_from"],
        travel_to=row["travel_to"],
        approved_at=row["approved_at"],
        provisional=bool(row["provisional"]),
        sale_from=row["sale_from"],
        sale_to=row["sale_to"],
    )


async def published(conn: asyncpg.Connection, plan_id: str) -> list[Version]:
    """The plan's published and superseded versions (the set the overlap rule runs on)."""
    return [version_of(r) for r in await conn.fetch(PUBLISHED_SQL, plan_id)]


def matrix_row(row: Any, start: date, end: date, code: str) -> dict[str, Any]:
    """One piece of a version row in the shape of ``booking.trip_plan_prices`` (inclusive ``valid_to``)."""
    return {
        "channel": row["channel"],
        "season": row["season"],
        "peak": code in seasons.PEAK_CODES,
        "season_code": code,
        "valid_from": start,
        "valid_to": end - ONE_DAY,
        "pax_min": row["pax_min"],
        "pax_max": row["pax_max"],
        "currency": str(row["currency"]).strip(),
        "amount": row["amount"],
        "rrp_amount": row["rrp_amount"],
        "net_amount": row["net_amount"],
        "net_currency": (row["net_currency"] or "").strip() or None,
        "effective_margin": row["effective_margin"],
        "priced_on": row["priced_on"],
        "overridden": bool(row["overridden"]),
        "computed": False,
        "created_by": row["created_by"],
        "version_id": row["version_id"],
        **{k: row[k] for k in PAX_FIELDS},
    }


async def hand_rows(
    conn: asyncpg.Connection,
    plan_id: str,
    day: date,
    existing: Sequence[dict[str, Any]] = (),
    calendar: seasons.Calendar | None = None,
) -> list[dict[str, Any]]:
    """The hand rows of the plan's effective matrix for quotes made on ``day``. A plan with no published version
    keeps the unversioned hand rows of ``existing`` (written before the code that versions them was deployed)."""
    versions = await published(conn, plan_id)
    if not versions:
        return [dict(r) for r in existing if (not r["computed"] or r["overridden"]) and not r.get("version_id")]
    cal = calendar if calendar is not None else await seasons.for_plan(conn, plan_id)
    wins = winning(versions, day)
    live = [vid for vid, spans in wins.items() if spans]
    out: list[dict[str, Any]] = []
    for row in await conn.fetch(VERSION_ROWS_SQL, live) if live else []:
        for start, end, code in pieces((row["travel_from"], row["travel_to"]), wins[row["version_id"]], cal):
            out.append(matrix_row(row, start, end, code))
    return out
