"""Price cost lines with the rule and FX rates in force on a day (root ADR 0005, AA-810).

Shared by ``POST /booking/pricing/quote-preview`` (writes nothing) and ``POST …/versions/{n}/price`` (stores
the total on a draft version), so the calculator, the builder preview and the applied price never disagree.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from app.pricing import engine, repository


class PricingDataUnavailable(Exception):  # noqa: N818 — reads as a state, like asyncpg's errors
    """The rule or FX rows could not be read (DB error, or a stored row the engine refuses)."""


class NoRuleInForce(engine.PricingError):
    def __init__(self, channel: engine.Channel) -> None:
        super().__init__(f"no pricing rule in force for channel {channel.value}")
        self.channel = channel


class MissingFxRates(engine.PricingError):
    def __init__(self, currencies: list[str]) -> None:
        super().__init__(f"no FX rate for {', '.join(currencies)}")
        self.currencies = currencies


@dataclass(frozen=True)
class PricedQuote:
    quote: engine.Quote
    rule: repository.DatedRule
    rates: dict[str, repository.DatedFxRate]
    priced_on: date

    @property
    def selling_fx(self) -> repository.DatedFxRate | None:
        """The dated rate of the selling currency; None for USD (the base)."""
        return self.rates.get(self.quote.currency)


async def price(
    conn: Any,
    *,
    channel: engine.Channel,
    currency: str,
    peak: bool,
    lines: Sequence[engine.CostLine],
    override_total: Decimal | None,
    on: date,
) -> PricedQuote:
    """Raises ``engine.PricingError`` (safe to show staff) or ``PricingDataUnavailable``."""
    if currency not in engine.SELLING_CURRENCIES:
        raise engine.PricingError(f"{currency} is not a selling currency")
    currencies = {currency, *(line.net_currency for line in lines)}
    try:
        dated_rule = await repository.rule_in_force(conn, channel, on)
        dated_rates = await repository.fx_rates_in_force(conn, currencies, on)
    except Exception as exc:  # noqa: BLE001 — DB errors and invalid stored rows both mean "unavailable"
        raise PricingDataUnavailable(type(exc).__name__) from None
    if dated_rule is None:
        raise NoRuleInForce(channel)
    missing = sorted(c for c in currencies if c != engine.BASE_CURRENCY and c not in dated_rates)
    if missing:
        raise MissingFxRates(missing)
    quote = engine.price_quote(
        list(lines),
        dated_rule.rule,
        currency=currency,
        peak=peak,
        rates={code: dated.rate for code, dated in dated_rates.items()},
        override_total=override_total,
    )
    return PricedQuote(quote=quote, rule=dated_rule, rates=dated_rates, priced_on=on)


def body(priced: PricedQuote) -> dict[str, Any]:
    """The quote as the API returns it (``QuotePreviewResponse`` fields)."""
    quote, dated_rule = priced.quote, priced.rule
    rule = dated_rule.rule
    return {
        "channel": quote.channel,
        "currency": quote.currency,
        "peak": quote.peak,
        "priced_on": priced.priced_on,
        "rule": {
            "channel": rule.channel,
            "target_margin": rule.target_margin,
            "floor_margin": rule.floor_margin,
            "peak_uplift_pts": rule.peak_uplift_pts,
            "card_fee_pct": rule.card_fee_pct,
            "valid_from": dated_rule.valid_from,
        },
        "fx_rates": [
            {
                "currency": code,
                "rate_per_usd": dated.rate.rate_per_usd,
                "buffer_pct": dated.rate.buffer_pct,
                "as_of": dated.as_of,
                "source": dated.source,
            }
            for code, dated in sorted(priced.rates.items())
        ],
        "margin_applied": quote.margin_applied,
        "lines": [vars(line) for line in quote.lines],
        "net_total_usd": quote.net_total_usd,
        "subtotal": quote.subtotal,
        "rounding_increment": quote.rounding_increment,
        "total": quote.total,
        "override_total": quote.override_total,
        "final_total": quote.final_total,
        "effective_margin": quote.effective_margin,
        "floor_margin": quote.floor_margin,
        "below_floor": quote.below_floor,
    }
