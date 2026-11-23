"""The sale rules of published add-on offers at quote time (AA-1151, bk-068; T7, B4), apart from
:mod:`app.supply.addons` so the B2B pricing modules can read them without importing the staff service (no import cycle:
:mod:`app.b2b.components` -> here, never back).

- :func:`sale_rules` / :func:`line_problem`: an ``sv:`` line whose service has a published offer is refused (422
  ``invalid``) below the offer's minimum party, on a day it does not run, or after its cut-off;
- :func:`child_pcts`: the offer's child % (B4), which wins over the contract's child % when the supplier gives no
  child rate (:mod:`app.supply.child_price`).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import asyncpg


@dataclass(frozen=True)
class SaleRule:
    offer_id: str
    min_pax: int
    cutoff_hours: int
    operating_weekdays: tuple[int, ...]
    start_times: tuple[time, ...]

    def closes_at(self, day: date) -> datetime:
        """When sale for ``day`` closes: ``cutoff_hours`` before its earliest departure (the day start when the offer
        has no fixed time). Local times are compared as UTC (the place's time zone is not in the catalog yet)."""
        first = min(self.start_times) if self.start_times else time(0, 0)
        return datetime.combine(day, first) - timedelta(hours=self.cutoff_hours)


RULES_SQL = """
SELECT o.service_id::text AS service_id, o.id::text AS offer_id, o.min_pax, o.cutoff_hours, o.operating_weekdays,
       o.start_times
FROM booking.addon_offers o WHERE o.service_id = ANY($1::uuid[]) AND o.status = 'published'
"""


async def sale_rules(conn: asyncpg.Connection, service_ids: Iterable[str]) -> dict[str, SaleRule]:
    """The sale rules of the published offers of ``service_ids`` (one query; none asked, none read)."""
    wanted = sorted(set(service_ids))
    if not wanted:
        return {}
    return {
        r["service_id"]: SaleRule(
            r["offer_id"],
            int(r["min_pax"]),
            int(r["cutoff_hours"]),
            tuple(int(d) for d in r["operating_weekdays"]),
            tuple(r["start_times"] or ()),
        )
        for r in await conn.fetch(RULES_SQL, wanted)
    }


def line_problem(rule: SaleRule, day: date, pax: int, now: datetime) -> tuple[str, str] | None:
    """``(code, message)`` when an add-on cannot be sold for ``day`` and ``pax`` travellers at ``now`` (UTC), else
    None. The checks: the minimum party, a running weekday, the cut-off."""
    if pax < rule.min_pax:
        return "below_min_pax", f"this activity runs for {rule.min_pax} travellers or more"
    if day.isoweekday() not in rule.operating_weekdays:
        return "not_operating", f"this activity does not run on {day.strftime('%A')}s"
    naive_now = now if now.tzinfo is None else now.astimezone(UTC).replace(tzinfo=None)
    if naive_now > rule.closes_at(day):
        return "past_cutoff", f"booking closed {rule.cutoff_hours} hours before the start on {day.isoformat()}"
    return None


OFFER_CHILD_SQL = """
SELECT service_id::text AS service_id, child_pct FROM booking.addon_offers
WHERE service_id = ANY($1::uuid[]) AND status = 'published' AND child_pct IS NOT NULL
"""


async def child_pcts(conn: asyncpg.Connection, service_ids: Iterable[str]) -> dict[str, Decimal]:
    """B4: the child % of the published offers of ``service_ids`` that set one (wins over the contract's)."""
    wanted = sorted(set(service_ids))
    if not wanted:
        return {}
    return {r["service_id"]: Decimal(str(r["child_pct"])) for r in await conn.fetch(OFFER_CHILD_SQL, wanted)}
