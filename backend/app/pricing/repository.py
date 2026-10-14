"""Reads of the pricing data tables (``bk-004``): the rule and FX rates in force on a date.

Both tables are append-only (a change is a new row with a later ``valid_from`` / ``as_of``) and a row counts only
once a second person approved it (``bk-007`` ``policy_approvals``, maker-checker), so "in force" = the newest
APPROVED row dated on or before the day asked for. A pending row never prices anything.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any

from app.pricing.engine import BASE_CURRENCY, Channel, FxRate, PricingRule

RULE_SQL = """
SELECT r.channel, r.target_margin, r.floor_margin, r.peak_uplift_pts, r.card_fee_pct, r.valid_from
FROM booking.pricing_rules r
JOIN booking.policy_approvals a ON a.pricing_rule_id = r.id
WHERE r.channel = $1 AND r.valid_from <= $2
ORDER BY r.valid_from DESC
LIMIT 1
"""

FX_SQL = """
SELECT DISTINCT ON (f.currency) f.currency, f.rate_per_usd, f.buffer_pct, f.as_of, f.source
FROM booking.fx_rates f
JOIN booking.policy_approvals a ON a.fx_rate_id = f.id
WHERE f.currency = ANY($1::text[]) AND f.as_of <= $2
ORDER BY f.currency, f.as_of DESC
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
