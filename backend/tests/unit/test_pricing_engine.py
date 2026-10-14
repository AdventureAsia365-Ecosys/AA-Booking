"""Pricing arithmetic (root ADR 0005 decisions 4 and 5), worked examples by hand.

Seed rules (bk-004, ADR 0005 table, to confirm with Ms. Thư): B2C tailor-made 30% / floor 20%,
fixed departure 25% / 18%, B2B 15% / 10%; peak +3 points; card fee 3% built in.
"""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from app.pricing import engine
from app.pricing.engine import Channel, CostLine, FxRate, PricingRule, price_quote

B2C = PricingRule(Channel.B2C_TAILOR_MADE, D("0.30"), D("0.20"), D("0.03"), D("0.03"))
FIXED = PricingRule(Channel.FIXED_DEPARTURE, D("0.25"), D("0.18"), D("0.03"), D("0.03"))
B2B = PricingRule(Channel.B2B, D("0.15"), D("0.10"), D("0.03"), D("0.03"))
B2C_NO_FEE = PricingRule(Channel.B2C_TAILOR_MADE, D("0.30"), D("0.20"))

EUR = FxRate("EUR", D("0.92"), D("0.02"))
GBP = FxRate("GBP", D("0.79"), D("0.03"))
VND = FxRate("VND", D("25400"))
RATES = {"EUR": EUR, "GBP": GBP, "VND": VND}


def usd(amount: str) -> list[CostLine]:
    return [CostLine(D(amount), "USD")]


# ── ADR formula: sell = net / (1 - margin), card fee built in, USD rounded up to $5 ─────────


def test_margin_is_on_selling_price_not_markup() -> None:
    # 700 / (1 - 0.30) = 1000 exactly: 30% of 1000 is the margin (a 30% markup would give 910).
    q = price_quote(usd("700"), B2C_NO_FEE)
    assert q.total == D("1000.00")
    assert q.effective_margin == D("0.3000")
    assert q.below_floor is False


def test_b2c_with_card_fee_built_in() -> None:
    # 700 / (1 - 0.30 - 0.03) = 700 / 0.67 = 1044.776... → rounded up to 1045.
    q = price_quote(usd("700"), B2C)
    assert q.lines[0].sell_usd == D("1044.78")
    assert q.subtotal == D("1044.78")
    assert q.total == D("1045.00")
    assert q.rounding_increment == D("5")
    # After the 3% fee AA keeps 1 - 0.03 - 700/1045 = 30.01% (rounding up only adds margin).
    assert q.effective_margin == D("0.3001")
    assert q.margin_applied == D("0.3000")
    assert q.card_fee_pct == D("0.0300")
    assert q.floor_margin == D("0.2000")
    assert q.final_total == q.total and q.override_total is None


def test_fee_share_equals_card_fee_of_selling_price() -> None:
    sell = engine.sell_price_usd(D("700"), D("0.30"), D("0.03"))
    fee = sell * D("0.03")
    margin = sell - D("700") - fee
    assert engine.cents(margin / sell) == D("0.30")


def test_peak_adds_uplift_points() -> None:
    # 700 / (1 - 0.33 - 0.03) = 700 / 0.64 = 1093.75 → 1095.
    q = price_quote(usd("700"), B2C, peak=True)
    assert q.margin_applied == D("0.3300")
    assert q.total == D("1095.00")
    assert q.peak is True


def test_fixed_departure_channel() -> None:
    # 700 / (1 - 0.25 - 0.03) = 972.22 → 975.
    assert price_quote(usd("700"), FIXED).total == D("975.00")


def test_b2b_channel() -> None:
    # 700 / (1 - 0.15 - 0.03) = 853.66 → 855.
    q = price_quote(usd("700"), B2B)
    assert q.total == D("855.00")
    assert q.channel is Channel.B2B


def test_exact_multiple_of_five_is_not_bumped() -> None:
    assert engine.round_price(D("1000"), "USD") == D("1000.00")
    assert engine.round_price(D("1000.01"), "USD") == D("1005.00")
    assert engine.round_price(D("0"), "USD") == D("0.00")


def test_round_price_rejects_non_selling_currency() -> None:
    with pytest.raises(engine.PricingError):
        engine.round_price(D("10"), "VND")


# ── FX (decision 5) ──────────────────────────────────────────────────────────


def test_supplier_cost_in_vnd_converted_at_plain_rate() -> None:
    # 17,780,000 VND / 25,400 = 700 USD → same price as a 700 USD cost.
    q = price_quote([CostLine(D("17780000"), "VND")], B2C, rates=RATES)
    assert q.lines[0].net_usd == D("700.00")
    assert q.net_total_usd == D("700.00")
    assert q.total == D("1045.00")


def test_eur_selling_price_uses_rate_and_buffer() -> None:
    # 1044.776... USD × 0.92 × 1.02 = 980.42 EUR → rounded up to 985 EUR.
    q = price_quote(usd("700"), B2C, currency="EUR", rates=RATES)
    assert q.lines[0].sell_usd == D("1044.78")
    assert q.lines[0].sell_amount == D("980.42")
    assert q.total == D("985.00")
    # Margin measured back at the plain rate: 985 / 0.92 = 1070.65 USD → 1 - 0.03 - 700/1070.65.
    assert q.effective_margin == D("0.3162")
    assert q.below_floor is False


def test_gbp_selling_price() -> None:
    # 1044.776... × 0.79 × 1.03 = 850.13 → 855 GBP.
    q = price_quote(usd("700"), B2C, currency="GBP", rates=RATES)
    assert q.subtotal == D("850.13")
    assert q.total == D("855.00")


def test_mixed_currency_lines_sum_before_rounding() -> None:
    # 700 USD + 2,540,000 VND (= 100 USD): lines 1044.78 + 149.25 = 1194.03, rounded once on the
    # total (never per line) → 1195.
    q = price_quote([CostLine(D("700"), "USD"), CostLine(D("2540000"), "VND")], B2C, rates=RATES)
    assert [line.sell_usd for line in q.lines] == [D("1044.78"), D("149.25")]
    assert q.net_total_usd == D("800.00")
    assert q.subtotal == D("1194.03")
    assert q.total == D("1195.00")


def test_missing_cost_rate_raises() -> None:
    with pytest.raises(engine.MissingFxRate) as exc:
        price_quote([CostLine(D("100"), "THB")], B2C, rates=RATES)
    assert exc.value.currency == "THB"
    assert str(exc.value) == "no FX rate for THB"


def test_missing_selling_rate_raises() -> None:
    with pytest.raises(engine.MissingFxRate):
        price_quote(usd("700"), B2C, currency="AUD", rates=RATES)


def test_unknown_selling_currency_rejected() -> None:
    with pytest.raises(engine.PricingError, match="not a selling currency"):
        price_quote(usd("700"), B2C, currency="VND", rates=RATES)


def test_from_usd_without_buffer_and_for_usd() -> None:
    assert engine.from_usd(D("100"), EUR, buffer=False) == D("92.00")
    assert engine.from_usd(D("100"), None) == D("100")
    assert engine.to_usd(D("92"), "USD", {}) == D("92")


# ── Floor and sales override ─────────────────────────────────────────────────


def test_override_below_floor_is_flagged() -> None:
    # 1 - 0.03 - 700/900 = 19.22% < 20% floor.
    q = price_quote(usd("700"), B2C, override_total=D("900"))
    assert q.total == D("1045.00")
    assert q.override_total == D("900")
    assert q.final_total == D("900")
    assert q.effective_margin == D("0.1922")
    assert q.below_floor is True


def test_override_above_floor_is_not_flagged() -> None:
    q = price_quote(usd("700"), B2C, override_total=D("925"))
    assert q.effective_margin == D("0.2132")
    assert q.below_floor is False


def test_floor_boundary_is_inclusive() -> None:
    # net 770: 1 - 0.03 - 770/1000 = 0.20 exactly → at the floor, not below.
    assert price_quote(usd("770"), B2C, override_total=D("1000")).below_floor is False
    assert price_quote(usd("770"), B2C, override_total=D("999.99")).below_floor is True


def test_override_in_selling_currency_measured_at_plain_rate() -> None:
    # 828 EUR / 0.92 = 900 USD → same 19.22% as the USD override.
    q = price_quote(usd("700"), B2C, currency="EUR", rates=RATES, override_total=D("828"))
    assert q.effective_margin == D("0.1922")
    assert q.below_floor is True


def test_override_must_be_positive() -> None:
    with pytest.raises(engine.PricingError):
        price_quote(usd("700"), B2C, override_total=D("0"))


def test_zero_cost_lines() -> None:
    q = price_quote(usd("0"), B2C)
    assert q.total == D("0.00")
    assert q.below_floor is True  # nothing sold: margin 0
    q = price_quote(usd("0"), B2C, override_total=D("50"))
    assert q.effective_margin == D("0.9700")
    assert q.below_floor is False


def test_effective_margin_needs_positive_price() -> None:
    with pytest.raises(engine.PricingError):
        engine.effective_margin(D("1"), D("0"), D("0"))


# ── Input validation ─────────────────────────────────────────────────────────


def test_no_lines_rejected() -> None:
    with pytest.raises(engine.PricingError):
        price_quote([], B2C)


def test_negative_cost_rejected() -> None:
    with pytest.raises(engine.PricingError):
        price_quote(usd("-1"), B2C)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"target_margin": D("1")}, "target_margin"),
        ({"floor_margin": D("-0.1")}, "floor_margin"),
        ({"floor_margin": D("0.35")}, "must not exceed"),
        ({"card_fee_pct": D("NaN")}, "finite"),
        ({"target_margin": 0.3}, "finite Decimal"),
        ({"target_margin": D("0.60"), "peak_uplift_pts": D("0.30"), "card_fee_pct": D("0.10")}, "below 1"),
    ],
)
def test_invalid_rules_rejected(kwargs: dict[str, object], message: str) -> None:
    base: dict[str, object] = {
        "channel": Channel.B2C_TAILOR_MADE,
        "target_margin": D("0.30"),
        "floor_margin": D("0.20"),
    }
    with pytest.raises(engine.PricingError, match=message):
        PricingRule(**{**base, **kwargs})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("USD", D("1")), "base currency"),
        (("EUR", D("0")), "positive"),
        (("EUR", D("Infinity")), "positive"),
        (("EUR", D("0.9"), D("1")), r"\[0, 1\)"),
        (("EUR", D("0.9"), 0.02), "finite Decimal"),
    ],
)
def test_invalid_fx_rates_rejected(args: tuple[object, ...], message: str) -> None:
    with pytest.raises(engine.PricingError, match=message):
        FxRate(*args)  # type: ignore[arg-type]


def test_sell_price_divisor_guard() -> None:
    with pytest.raises(engine.PricingError):
        engine.sell_price_usd(D("1"), D("0.97"), D("0.03"))


def test_every_selling_currency_has_an_increment() -> None:
    assert set(engine.ROUNDING_INCREMENTS) == engine.SELLING_CURRENCIES


def test_total_rounded_once_not_per_line() -> None:
    # Three 101 USD lines: 150.75 each (no fee, 33%) → per-line rounding would give 3 × 155 = 465;
    # the total is 452.24 → 455.
    rule = PricingRule(Channel.B2C_TAILOR_MADE, D("0.33"), D("0.20"))
    q = price_quote(usd("101") * 3, rule)
    assert q.subtotal == D("452.24")
    assert q.total == D("455.00")
