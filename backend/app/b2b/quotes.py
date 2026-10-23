"""``POST /booking/b2b/v1/quotes``: price lines at the B2B channel rule, lock FX, hash, store (AA-972, bk-019).

- **Rule and FX**: the approved ``b2b`` pricing rule in force on the quote day (UTC) and the approved FX row in force
  that day for the quote currency and every cost currency (``app.pricing.repository``, maker-checker rows only).
  Missing = 503 ``unavailable`` (AA-side data; the CRM shows an indicative price and allows no deposit).
- **Formula**: the AA-810 engine's building blocks (``app.pricing.engine``): every net to USD at the plain rate, sell
  ``= net / (1 - margin - card fee)`` with the rule's margin (``peak = false``), then to the quote currency x rate x
  (1 + buffer). One formula for B2C and B2B (memo seam 7): no second engine.
- **Rounding** (:data:`ROUNDING`): each line's ``b2b_price`` is rounded UP to the cent (never lowers the margin);
  ``total_b2b_price`` is the exact sum of the lines. The B2C "total up to 5" rule does not apply: a B2B price is not
  customer-facing (the agency sets its own sell price).
- **price_hash** (:func:`price_hash`): SHA-256 of a canonical JSON of the currency, the FX lock, the rule, the rounding
  rule, every line (what was asked + its price) and the total. Not the quote id, time, validity or proposal ref, so
  the same request on the same data gives the same hash, and any price change gives a new one.
- **Stored append-only** (``b2b_quotes`` + ``b2b_quote_lines``): the net behind each line is kept as ``net_cost_usd``
  (``conf:commercial``) and never serialised. Nothing named ``supplier_net`` exists (ADR 0005a decision 2, Fix B19).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from typing import Any

import asyncpg

from app.b2b.components import LineAsk, LineCost, price_lines
from app.b2b.errors import B2BError
from app.pricing import engine, repository

log = logging.getLogger(__name__)

HASH_VERSION = "booking.b2b.v1/quote/1"
ROUNDING = "line_ceil_0.01"
UNIT = "per_line"  # b2b_price is the whole line (quantity included)
AVAILABLE = "available"  # until AA-973 adds departures and capacity
VALID_HOURS_KEY = "b2b_quote_valid_hours"  # gitleaks:allow (setting name, not a secret)
DEFAULT_VALID_HOURS = 48
MAX_VALID_HOURS = 720
FX_PLACES = Decimal("0.00000001")
USD_SOURCE = "base"


@dataclass(frozen=True)
class AskLine:
    """One request line (``QuoteLineRequest``), already validated by the router model."""

    ask: LineAsk
    rooms: list[dict[str, Any]]
    departure_id: str | None


@dataclass(frozen=True)
class FxLock:
    currency: str
    rate_per_usd: Decimal  # plain rate (1 for USD)
    buffer_pct: Decimal
    as_of: date
    source: str

    @property
    def selling_rate(self) -> Decimal:
        return (self.rate_per_usd * (engine.ONE + self.buffer_pct)).quantize(FX_PLACES, rounding=ROUND_HALF_UP)


def ceil_cents(value: Decimal) -> Decimal:
    return value.quantize(engine.CENT, rounding=ROUND_CEILING)


# ISO 4217 exponent 0: amounts carry no decimals (contract Money: "VND amounts have no decimals"), rounded up.
ZERO_DECIMAL_CURRENCIES = frozenset({"VND", "JPY", "KRW"})


def money(amount: Decimal, currency: str) -> dict[str, str]:
    if currency in ZERO_DECIMAL_CURRENCIES:
        return {"amount": format(amount.quantize(engine.ONE, rounding=ROUND_CEILING), "f"), "currency": currency}
    return {"amount": format(amount.quantize(engine.CENT), "f"), "currency": currency}


def decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f") if value != 0 else "0"


def price_hash(
    *,
    currency: str,
    fx: FxLock,
    rule: repository.DatedRule,
    margin: Decimal,
    lines: list[dict[str, Any]],
    total: Decimal,
) -> str:
    """Canonical JSON (sorted keys, no spaces, decimals as normalised strings) → SHA-256 hex."""
    payload = {
        "v": HASH_VERSION,
        "currency": currency,
        "fx": {
            "currency": fx.currency,
            "rate_per_usd": decimal_text(fx.rate_per_usd),
            "buffer_pct": decimal_text(fx.buffer_pct),
            "as_of": fx.as_of.isoformat(),
            "source": fx.source,
        },
        "rule": {
            "channel": rule.rule.channel.value,
            "margin": decimal_text(margin),
            "card_fee_pct": decimal_text(rule.rule.card_fee_pct),
            "valid_from": rule.valid_from.isoformat(),
        },
        "rounding": ROUNDING,
        "lines": [
            {
                "line_ref": line["line_ref"],
                "component_id": line["component_id"],
                "service_date": line["service_date"].isoformat(),
                "nights": line["nights"],
                "pax": [line["pax_adult"], line["pax_child"], line["pax_infant"]],
                "rooms": line["rooms"],
                "unit": line["unit"],
                "availability": line["availability"],
                "b2b_price": format(line["b2b_price"], "f"),
            }
            for line in lines
        ],
        "total": format(total, "f"),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


async def _valid_hours(conn: asyncpg.Connection) -> int:
    raw = await conn.fetchval("SELECT value FROM booking.settings WHERE key = $1", VALID_HOURS_KEY)
    try:
        hours = int(json.loads(raw)) if isinstance(raw, str) else int(raw)
    except (TypeError, ValueError):
        return DEFAULT_VALID_HOURS
    return hours if 1 <= hours <= MAX_VALID_HOURS else DEFAULT_VALID_HOURS


async def _pricing_data(
    conn: asyncpg.Connection, currencies: set[str], on: date
) -> tuple[repository.DatedRule, dict[str, repository.DatedFxRate]]:
    try:
        rule = await repository.rule_in_force(conn, engine.Channel.B2B, on)
        rates = await repository.fx_rates_in_force(conn, currencies, on)
    except Exception as exc:  # noqa: BLE001 — a DB error or an invalid stored row both mean "cannot price now"
        log.warning("b2b_pricing_data_unavailable error=%s", type(exc).__name__)
        raise B2BError("unavailable", "pricing data is not available") from None
    if rule is None:
        raise B2BError("unavailable", "no B2B pricing rule in force")
    missing = sorted(c for c in currencies if c != engine.BASE_CURRENCY and c not in rates)
    if missing:
        raise B2BError("unavailable", f"no approved FX rate for {', '.join(missing)}")
    return rule, rates


def _lock(currency: str, rates: dict[str, repository.DatedFxRate], on: date) -> FxLock:
    if currency == engine.BASE_CURRENCY:
        return FxLock(currency, engine.ONE, engine.ZERO, on, USD_SOURCE)
    dated = rates[currency]
    return FxLock(currency, dated.rate.rate_per_usd, dated.rate.buffer_pct, dated.as_of, dated.source[:64])


async def create_quote(
    pool: asyncpg.Pool,
    *,
    partner_id: str,
    client_id: str,
    proposal_ref: str,
    proposal_version: int,
    currency: str,
    lines: list[AskLine],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Price, hash and store one quote; returns the contract ``Quote`` body. Raises B2BError."""
    if currency not in engine.SELLING_CURRENCIES:
        allowed = ", ".join(sorted(engine.SELLING_CURRENCIES))
        raise B2BError("invalid", f"{currency} is not a quote currency (one of {allowed})")
    refs = [line.ask.line_ref for line in lines]
    if len(set(refs)) != len(refs):
        raise B2BError("invalid", "line_ref must be unique within a quote")
    for line in lines:
        if line.departure_id:
            raise B2BError("invalid", f"line {line.ask.line_ref}: unknown departure_id (no departures yet)")
    now = now or datetime.now(UTC)
    on = now.date()

    async with pool.acquire() as conn:
        costs: list[LineCost] = await price_lines(conn, [line.ask for line in lines])
        currencies = {currency, *(cur for lc in costs for _, cur in lc.costs)}
        rule, rates = await _pricing_data(conn, currencies, on)
        fx = _lock(currency, rates, on)
        engine_rates = {code: dated.rate for code, dated in rates.items()}
        margin = rule.rule.margin(peak=False)
        sell_rate = engine_rates.get(currency)

        priced: list[dict[str, Any]] = []
        for no, (line, lc) in enumerate(zip(lines, costs, strict=True), start=1):
            try:
                net_usd = sum((engine.to_usd(amount, cur, engine_rates) for amount, cur in lc.costs), start=engine.ZERO)
                sell = engine.from_usd(engine.sell_price_usd(net_usd, margin, rule.rule.card_fee_pct), sell_rate)
            except engine.PricingError as exc:
                raise B2BError("unavailable", f"line {line.ask.line_ref}: {exc}") from None
            ask = line.ask
            priced.append(
                {
                    "line_no": no,
                    "line_ref": ask.line_ref,
                    "component_id": ask.component_id,
                    "component_kind": lc.kind,
                    "service_date": ask.service_date,
                    "nights": ask.nights,
                    "pax_adult": ask.adult,
                    "pax_child": ask.child,
                    "pax_infant": ask.infant,
                    "rooms": line.rooms,
                    "unit": UNIT,
                    "availability": AVAILABLE,
                    "b2b_price": ceil_cents(sell),
                    "net_cost_usd": net_usd.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP),
                    "supplier_rate_ids": lc.rate_ids,
                }
            )
        total = sum((p["b2b_price"] for p in priced), start=engine.ZERO)
        digest = price_hash(currency=currency, fx=fx, rule=rule, margin=margin, lines=priced, total=total)
        valid_until = now + timedelta(hours=await _valid_hours(conn))

        async with conn.transaction():
            quote_id = await conn.fetchval(
                """
                INSERT INTO booking.b2b_quotes
                    (partner_id, m2m_client_id, proposal_ref, proposal_version, currency, fx_rate_per_usd,
                     fx_buffer_pct, fx_as_of, fx_source, pricing_rule_valid_from, margin_applied, card_fee_pct,
                     rounding, priced_on, total_b2b_price, price_hash, valid_until, created_at)
                VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18)
                RETURNING id::text
                """,
                partner_id,
                client_id,
                proposal_ref,
                proposal_version,
                currency,
                fx.rate_per_usd,
                fx.buffer_pct,
                fx.as_of,
                fx.source,
                rule.valid_from,
                margin,
                rule.rule.card_fee_pct,
                ROUNDING,
                on,
                total,
                digest,
                valid_until,
                now,
            )
            await conn.executemany(
                """
                INSERT INTO booking.b2b_quote_lines
                    (quote_id, line_no, line_ref, component_id, component_kind, service_date, nights, pax_adult,
                     pax_child, pax_infant, rooms, unit, availability, b2b_price, net_cost_usd, supplier_rate_ids)
                VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb, $12, $13, $14, $15, $16::uuid[])
                """,
                [
                    (
                        quote_id,
                        p["line_no"],
                        p["line_ref"],
                        p["component_id"],
                        p["component_kind"],
                        p["service_date"],
                        p["nights"],
                        p["pax_adult"],
                        p["pax_child"],
                        p["pax_infant"],
                        json.dumps(p["rooms"]),
                        p["unit"],
                        p["availability"],
                        p["b2b_price"],
                        p["net_cost_usd"],
                        p["supplier_rate_ids"],
                    )
                    for p in priced
                ],
            )
    log.info("b2b_quote_created quote=%s partner=%s lines=%s", quote_id, partner_id, len(priced))
    return {
        "quote_id": quote_id,
        "currency": currency,
        "fx": {
            "base_currency": engine.BASE_CURRENCY,
            "quote_currency": currency,
            "rate_per_usd": format(fx.selling_rate, "f"),
            "rate_date": fx.as_of.isoformat(),
            "source": fx.source,
        },
        "valid_until": valid_until.isoformat(),
        "price_hash": digest,
        "lines": [
            {
                "line_ref": p["line_ref"],
                "b2b_price": money(p["b2b_price"], currency),
                "unit": p["unit"],
                "availability": p["availability"],
                "departure_id": None,
            }
            for p in priced
        ],
        "total_b2b_price": money(total, currency),
    }
