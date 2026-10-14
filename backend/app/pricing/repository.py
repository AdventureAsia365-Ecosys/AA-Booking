"""Reads of the pricing data tables (``bk-004``): the rule and FX rates in force on a date.

Both tables are append-only (a change is a new row with a later ``valid_from`` / ``as_of``), so
"in force" = the newest row dated on or before the day asked for.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any

from app.pricing.engine import BASE_CURRENCY, Channel, FxRate, PricingRule

RULE_SQL = """
SELECT channel, target_margin, floor_margin, peak_uplift_pts, card_fee_pct, valid_from
FROM booking.pricing_rules
WHERE channel = $1 AND valid_from <= $2
ORDER BY valid_from DESC
LIMIT 1
"""

FX_SQL = """
SELECT DISTINCT ON (currency) currency, rate_per_usd, buffer_pct, as_of, source
FROM booking.fx_rates
WHERE currency = ANY($1::text[]) AND as_of <= $2
ORDER BY currency, as_of DESC
"""


@dataclass(frozen=True)
class DatedRule:
    rule: PricingRule
    valid_from: date


@dataclass(frozen=True)
class DatedFxRate:
    rate: FxRate
    as_of: date
    source: str


async def rule_in_force(conn: Any, channel: Channel, on: date) -> DatedRule | None:
    row = await conn.fetchrow(RULE_SQL, channel.value, on)
    if row is None:
        return None
    return DatedRule(
        rule=PricingRule(
            channel=Channel(row["channel"]),
            target_margin=row["target_margin"],
            floor_margin=row["floor_margin"],
            peak_uplift_pts=row["peak_uplift_pts"],
            card_fee_pct=row["card_fee_pct"],
        ),
        valid_from=row["valid_from"],
    )


async def fx_rates_in_force(conn: Any, currencies: Iterable[str], on: date) -> dict[str, DatedFxRate]:
    wanted = sorted({c for c in currencies if c != BASE_CURRENCY})
    if not wanted:
        return {}
    rows = await conn.fetch(FX_SQL, wanted, on)
    out: dict[str, DatedFxRate] = {}
    for row in rows:
        currency = row["currency"].strip()
        out[currency] = DatedFxRate(
            rate=FxRate(currency=currency, rate_per_usd=row["rate_per_usd"], buffer_pct=row["buffer_pct"]),
            as_of=row["as_of"],
            source=row["source"],
        )
    return out
