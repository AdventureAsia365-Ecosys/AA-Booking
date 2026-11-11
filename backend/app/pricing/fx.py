"""FX normalisation of costs (AA-1074, bk-045; S275 study item bk-10).

Supplier rates, bookings, bills and payouts are kept in their own ISO currency. To total a mixed-currency trip case or
departure, every native amount is converted to USD at the **plain approved rate** in force on a day: the
``rate_per_usd`` of the newest APPROVED ``booking.fx_rates`` row dated on or before it, **no buffer** (the buffer is a
selling margin of the pricing engine, never a cost). USD is 1. The SQL twin is ``booking.fx_plain_rate(currency,
day)`` (bk-045), used by the cost and profit views for rows without a snapshot.

Snapshots: a supplier booking takes the rate when it is confirmed, a supplier bill when it is approved
(``fx_rate_per_usd`` + ``fx_as_of``). A currency without an approved rate leaves the snapshot empty (NULL): the views
then read the rate in force, and flag ``fx_missing`` while none exists.

Rounding (one rule, also in the views): each native amount is converted on its own and rounded **half up to the cent**
(``ROUND_HALF_UP``, as PostgreSQL ``round(numeric, 2)``); a total is the sum of those cents, so it always equals the sum
of its lines.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.pricing.engine import BASE_CURRENCY
from app.pricing.repository import fx_rates_in_force

CENT = Decimal("0.01")


@dataclass(frozen=True)
class PlainRate:
    """A plain approved rate: ``rate_per_usd`` units of ``currency`` buy one USD (USD: 1, ``as_of`` = the day asked)."""

    currency: str
    rate_per_usd: Decimal
    as_of: date


def to_usd(amount: Decimal | str | int, rate_per_usd: Decimal | str | int) -> Decimal:
    """``amount / rate_per_usd`` rounded half up to the cent. A rate must be > 0."""
    rate = Decimal(str(rate_per_usd))
    if rate <= 0:
        raise ValueError("rate_per_usd must be > 0")
    return (Decimal(str(amount)) / rate).quantize(CENT, rounding=ROUND_HALF_UP)


def from_usd(amount_usd: Decimal | str | int, rate_per_usd: Decimal | str | int) -> Decimal:
    """``amount_usd x rate_per_usd`` rounded half up to the cent (a USD figure shown in a quote currency)."""
    return (Decimal(str(amount_usd)) * Decimal(str(rate_per_usd))).quantize(CENT, rounding=ROUND_HALF_UP)


def fx_difference_usd(native: Decimal | str, *, booked_rate: Decimal | str, billed_rate: Decimal | str) -> Decimal:
    """The realised FX difference of one billed amount: the same native amount at the bill's rate minus at the
    booking's rate, each rounded to the cent first (positive = the cost in USD went up because the currency moved)."""
    return to_usd(native, billed_rate) - to_usd(native, booked_rate)


def share(amount: Decimal | str, part: int, whole: int) -> Decimal:
    """``amount x part / whole`` rounded half up to the cent (a departure cost split by seats; the remainder stays
    with the departure). ``whole`` must be > 0."""
    if whole <= 0:
        raise ValueError("whole must be > 0")
    return (Decimal(str(amount)) * part / whole).quantize(CENT, rounding=ROUND_HALF_UP)


async def plain_rates(conn: Any, currencies: list[str], on: date) -> dict[str, PlainRate]:
    """The plain approved rate in force on ``on`` for each currency that has one (USD always)."""
    wanted = {c.strip().upper() for c in currencies if c}
    out: dict[str, PlainRate] = {}
    if BASE_CURRENCY in wanted:
        out[BASE_CURRENCY] = PlainRate(BASE_CURRENCY, Decimal(1), on)
    for currency, dated in (await fx_rates_in_force(conn, wanted, on)).items():
        out[currency] = PlainRate(currency, Decimal(str(dated.rate.rate_per_usd)), dated.as_of)
    return out


async def snapshot(conn: Any, currency: str | None, on: date) -> dict[str, Any]:
    """The snapshot columns for a row in ``currency`` taken on ``on``: ``fx_rate_per_usd`` and ``fx_as_of``, both None
    when the currency has no approved rate (or no currency is known)."""
    if not currency:
        return {"fx_rate_per_usd": None, "fx_as_of": None}
    rate = (await plain_rates(conn, [currency], on)).get(currency.strip().upper())
    if rate is None:
        return {"fx_rate_per_usd": None, "fx_as_of": None}
    return {"fx_rate_per_usd": rate.rate_per_usd, "fx_as_of": rate.as_of}
