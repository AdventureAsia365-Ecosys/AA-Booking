"""Pure pricing arithmetic (root ADR 0005 decisions 4 and 5). No I/O, ``Decimal`` only.

The formula, per quote:

1. Every cost line is converted to USD (the base currency) at its dated FX rate, without buffer:
   ``net_usd = net_amount / rate_per_usd``. A USD cost is used as is.
2. Margin applied = the channel's target margin, plus the peak uplift points when the quote is for
   a peak date.
3. Selling price in USD, card fee built in (ADR 0005: the ~3% card/payment fee is part of the price,
   never a surcharge): ``sell_usd = net_usd / (1 - margin - card_fee)``. The margin is a gross margin
   on the selling price (not a markup), and the card fee is a cost of sale on the same base, so the
   margin left after the fee is exactly ``margin``.
4. Non-USD selling price: ``sell = sell_usd * rate_per_usd * (1 + buffer_pct)``.
5. The quote total is the sum of the unrounded lines, rounded UP to the currency's increment
   (``ROUNDING_INCREMENTS``; USD to 5 per ADR 0005). Rounding up never lowers the margin.
6. Effective margin of a total (computed or overridden by sales):
   ``1 - card_fee - net_total_usd / total_usd``, where ``total_usd`` converts the total back at the
   plain rate (no buffer) — the margin AA actually earns at today's rate. ``below_floor`` is
   ``effective_margin < floor_margin``.

Business numbers (margins, fee, uplift, FX buffer) are data from ``booking.pricing_rules`` and
``booking.fx_rates``; nothing here hard-codes them. Only the rounding increments live in code.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from enum import StrEnum

BASE_CURRENCY = "USD"

# Selling currencies (ADR 0005 decision 5). More per tenant market later.
SELLING_CURRENCIES: frozenset[str] = frozenset({"USD", "EUR", "GBP", "AUD", "SGD"})

# Customer-facing totals are rounded UP to these increments. USD to 5 is ADR 0005; the other four
# reuse 5 units until Ms. Thư confirms a per-currency rule (open question on AA-810).
ROUNDING_INCREMENTS: Mapping[str, Decimal] = {
    "USD": Decimal("5"),
    "EUR": Decimal("5"),
    "GBP": Decimal("5"),
    "AUD": Decimal("5"),
    "SGD": Decimal("5"),
}

CENT = Decimal("0.01")
RATIO = Decimal("0.0001")  # margins and percentages are shown to 4 decimals (0.3000 = 30%)
ZERO = Decimal("0")
ONE = Decimal("1")


class Channel(StrEnum):
    """Sales channel of a quote (ADR 0005 decision 4 table)."""

    B2C_TAILOR_MADE = "b2c_tailor_made"
    FIXED_DEPARTURE = "fixed_departure"
    B2B = "b2b"


class PricingError(ValueError):
    """Inputs the engine cannot price; the message is safe to show to staff."""


class MissingFxRate(PricingError):
    def __init__(self, currency: str) -> None:
        super().__init__(f"no FX rate for {currency}")
        self.currency = currency


@dataclass(frozen=True)
class PricingRule:
    """One row of ``booking.pricing_rules``. Fractions: 0.30 = 30%, 0.03 = 3 points."""

    channel: Channel
    target_margin: Decimal
    floor_margin: Decimal
    peak_uplift_pts: Decimal = ZERO
    card_fee_pct: Decimal = ZERO

    def __post_init__(self) -> None:
        for name in ("target_margin", "floor_margin", "peak_uplift_pts", "card_fee_pct"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite():
                raise PricingError(f"{name} must be a finite Decimal")
            if value < ZERO or value >= ONE:
                raise PricingError(f"{name} must be in [0, 1)")
        if self.floor_margin > self.target_margin:
            raise PricingError("floor_margin must not exceed target_margin")
        if self.target_margin + self.peak_uplift_pts + self.card_fee_pct >= ONE:
            raise PricingError("target_margin + peak_uplift_pts + card_fee_pct must be below 1")

    def margin(self, *, peak: bool) -> Decimal:
        return self.target_margin + (self.peak_uplift_pts if peak else ZERO)


@dataclass(frozen=True)
class FxRate:
    """One row of ``booking.fx_rates``: ``rate_per_usd`` units of ``currency`` buy 1 USD."""

    currency: str
    rate_per_usd: Decimal
    buffer_pct: Decimal = ZERO

    def __post_init__(self) -> None:
        if self.currency == BASE_CURRENCY:
            raise PricingError("USD has no FX rate; it is the base currency")
        if not isinstance(self.rate_per_usd, Decimal) or not self.rate_per_usd.is_finite() or self.rate_per_usd <= 0:
            raise PricingError(f"rate_per_usd for {self.currency} must be a positive Decimal")
        if not isinstance(self.buffer_pct, Decimal) or not self.buffer_pct.is_finite():
            raise PricingError(f"buffer_pct for {self.currency} must be a finite Decimal")
        if self.buffer_pct < ZERO or self.buffer_pct >= ONE:
            raise PricingError(f"buffer_pct for {self.currency} must be in [0, 1)")


@dataclass(frozen=True)
class CostLine:
    net_amount: Decimal
    net_currency: str


@dataclass(frozen=True)
class PricedLine:
    net_amount: Decimal
    net_currency: str
    net_usd: Decimal  # cents, half-up (display)
    sell_usd: Decimal  # cents, half-up (display)
    sell_amount: Decimal  # in the quote currency, cents, half-up (display; the total uses full precision)


@dataclass(frozen=True)
class Quote:
    channel: Channel
    currency: str
    peak: bool
    margin_applied: Decimal
    card_fee_pct: Decimal
    floor_margin: Decimal
    lines: tuple[PricedLine, ...]
    net_total_usd: Decimal
    subtotal: Decimal  # unrounded sum in the quote currency, cents
    rounding_increment: Decimal
    total: Decimal  # computed price, rounded up to the increment
    override_total: Decimal | None
    final_total: Decimal  # override_total when given, else total
    effective_margin: Decimal
    below_floor: bool


# ── Building blocks ──────────────────────────────────────────────────────────


def to_usd(amount: Decimal, currency: str, rates: Mapping[str, FxRate]) -> Decimal:
    """Cost conversion to USD at the plain rate (no buffer). Full precision."""
    if currency == BASE_CURRENCY:
        return amount
    rate = rates.get(currency)
    if rate is None:
        raise MissingFxRate(currency)
    return amount / rate.rate_per_usd


def sell_price_usd(net_usd: Decimal, margin: Decimal, card_fee_pct: Decimal) -> Decimal:
    """``net / (1 - margin - card_fee)``: gross margin on the selling price, card fee built in."""
    divisor = ONE - margin - card_fee_pct
    if divisor <= ZERO:
        raise PricingError("margin + card fee must be below 100%")
    return net_usd / divisor


def from_usd(amount_usd: Decimal, rate: FxRate | None, *, buffer: bool = True) -> Decimal:
    """USD → selling currency, ``× rate × (1 + buffer)``. ``rate=None`` means USD."""
    if rate is None:
        return amount_usd
    factor = rate.rate_per_usd * ((ONE + rate.buffer_pct) if buffer else ONE)
    return amount_usd * factor


def round_price(amount: Decimal, currency: str) -> Decimal:
    """Round a customer-facing total UP to the currency's increment (USD 1,044.78 → 1,045)."""
    increment = ROUNDING_INCREMENTS.get(currency)
    if increment is None:
        raise PricingError(f"{currency} is not a selling currency")
    steps = (amount / increment).to_integral_value(rounding=ROUND_CEILING)
    return (steps * increment).quantize(CENT)


def effective_margin(net_usd: Decimal, sell_usd: Decimal, card_fee_pct: Decimal) -> Decimal:
    """Margin left after the card fee: ``1 - fee - net / sell``. Full precision."""
    if sell_usd <= ZERO:
        raise PricingError("a selling price must be positive")
    return ONE - card_fee_pct - net_usd / sell_usd


def cents(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def ratio(value: Decimal) -> Decimal:
    return value.quantize(RATIO, rounding=ROUND_HALF_UP)


# ── The quote ────────────────────────────────────────────────────────────────


def price_quote(
    lines: Sequence[CostLine],
    rule: PricingRule,
    *,
    currency: str = BASE_CURRENCY,
    peak: bool = False,
    rates: Mapping[str, FxRate] | None = None,
    override_total: Decimal | None = None,
) -> Quote:
    """Price cost lines for one channel and selling currency.

    ``rates`` holds an ``FxRate`` for every non-USD currency used (cost currencies and the selling
    currency). ``override_total`` is a sales override of the total in the selling currency; it is
    never rounded, only checked against the floor.
    """
    if not lines:
        raise PricingError("at least one cost line is required")
    if currency not in SELLING_CURRENCIES:
        raise PricingError(f"{currency} is not a selling currency")
    rates = rates or {}
    sell_rate: FxRate | None = None
    if currency != BASE_CURRENCY:
        sell_rate = rates.get(currency)
        if sell_rate is None:
            raise MissingFxRate(currency)

    margin = rule.margin(peak=peak)
    priced: list[PricedLine] = []
    net_total_usd = ZERO
    subtotal = ZERO
    for line in lines:
        if not line.net_amount.is_finite() or line.net_amount < ZERO:
            raise PricingError("net_amount must be zero or positive")
        net_usd = to_usd(line.net_amount, line.net_currency, rates)
        sell_usd = sell_price_usd(net_usd, margin, rule.card_fee_pct)
        sell_amount = from_usd(sell_usd, sell_rate)
        net_total_usd += net_usd
        subtotal += sell_amount
        priced.append(
            PricedLine(
                net_amount=line.net_amount,
                net_currency=line.net_currency,
                net_usd=cents(net_usd),
                sell_usd=cents(sell_usd),
                sell_amount=cents(sell_amount),
            )
        )

    total = round_price(subtotal, currency)
    if override_total is not None and (not override_total.is_finite() or override_total <= ZERO):
        raise PricingError("override_total must be positive")
    final_total = override_total if override_total is not None else total
    if net_total_usd == ZERO:
        # Nothing to earn a margin on (all lines free): the whole price is margin.
        margin_now = ONE - rule.card_fee_pct if final_total > ZERO else ZERO
    else:
        final_usd = final_total / sell_rate.rate_per_usd if sell_rate is not None else final_total
        margin_now = effective_margin(net_total_usd, final_usd, rule.card_fee_pct)

    return Quote(
        channel=rule.channel,
        currency=currency,
        peak=peak,
        margin_applied=ratio(margin),
        card_fee_pct=ratio(rule.card_fee_pct),
        floor_margin=ratio(rule.floor_margin),
        lines=tuple(priced),
        net_total_usd=cents(net_total_usd),
        subtotal=cents(subtotal),
        rounding_increment=ROUNDING_INCREMENTS[currency],
        total=total,
        override_total=override_total,
        final_total=final_total,
        effective_margin=ratio(margin_now),
        below_floor=margin_now < rule.floor_margin,
    )
