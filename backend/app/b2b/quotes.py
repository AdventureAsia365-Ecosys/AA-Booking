"""``POST /booking/b2b/v1/quotes``: price lines at the B2B channel rule, lock FX, hash, store (AA-972, bk-019).

- **Currencies** (:data:`QUOTE_CURRENCIES`): the engine's selling currencies plus VND (AA-974, ADR 0005a d3).
- **Rule and FX**: the approved ``b2b`` pricing rule in force on the quote day (UTC) and the approved FX row in force
  that day for the quote currency and every cost currency (``app.pricing.repository``, maker-checker rows only).
  Missing = 503 ``unavailable`` (AA-side data; the CRM shows an indicative price and allows no deposit).
- **Formula**: the AA-810 engine's building blocks (``app.pricing.engine``): every net to USD at the plain rate, sell
  ``= net / (1 - margin - card fee)`` with the rule's margin (``peak = false``), then to the quote currency x rate x
  (1 + buffer). One formula for B2C and B2B (memo seam 7): no second engine. The per-line step is
  :func:`price_line` over a :class:`PriceBasis` (:func:`price_basis`), shared with the agency tariff (AA-1030 G3,
  ``app.b2b.tariff``), so a tariff row is exactly what a quote of that party and day charges.
- **Rounding** (:func:`round_line`): each line's ``b2b_price`` is rounded UP to the cent (never lowers the margin),
  a VND line UP to 1,000 VND (AA-974, :data:`LINE_INCREMENTS`); ``total_b2b_price`` is the exact sum of the lines.
  The B2C "total up to 5" rule does not apply: a B2B price is not customer-facing (the agency sets its own price).
- **price_hash** (:func:`price_hash`): SHA-256 of a canonical JSON of the currency, the FX lock, the rule, the rounding
  rule, every line (what was asked + its price) and the total. Not the quote id, time, validity or proposal ref, so
  the same request on the same data gives the same hash, and any price change gives a new one.
- **Availability** (AA-973, ``app.b2b.departures``): per line, ``available`` on a fixed departure with seats that
  auto-confirms, ``on_request`` without a departure or on one that does not auto-confirm, ``sold_out`` when the
  departure is full, cancelled or started. A quote reserves nothing; the hold re-checks under a lock. The line's
  ``departure_id`` is stored and hashed (hash version 2). The seats of the live ``active`` hold of the same
  ``(partner, proposal_ref, proposal_version)`` are not counted (AA-950, :data:`OWN_HOLD_SQL`): one version has one
  hold set, so a new quote for it replaces that hold (the caller releases it before ``POST /holds``, which counts
  every hold under the departure lock); counting it would answer ``sold_out`` for seats the same client holds.
- **Served tours only** (AA-986 L5, root ADR 0015): a ``tp:`` line whose tour is not served (retired, paused or not
  in ``catalog.v_tour``) is 409 ``tour_not_served`` before anything is priced (``app.b2b.served``).
- **Agency tier / rate card** (AA-1030, bk-043, root ADR 0019 b15, ``app.b2b.agency_pricing``): each line's
  ``b2b_price`` is the rule's price less the partner's discount in force on the quote day (the plan's contract rate
  card, else a catalogue card, else the tier's approved discount), then rounded up. The quote stores the tier and its
  discount, each line the discount applied and the card behind it. A line with a discount hashes it
  (``agency_discount``, ``rate_card_id``); a line without one hashes exactly as before, so a standard (0 %) partner's
  hash is unchanged.
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

from app.b2b import agency_pricing, departures, served
from app.b2b.components import PREFIX_PLAN, LineAsk, LineCost, parse_component, price_lines
from app.b2b.errors import B2BError
from app.pricing import engine, repository

log = logging.getLogger(__name__)

# AA-950: the live active hold set of the quote's own proposal version (at most one: the version's unique set).
OWN_HOLD_SQL = """
SELECT id::text FROM booking.b2b_holds
WHERE partner_id = $1::uuid AND proposal_ref = $2 AND proposal_version = $3 AND status = 'active'
  AND expires_at > now()
ORDER BY created_at DESC
LIMIT 1
"""
HASH_VERSION = "booking.b2b.v1/quote/2"  # /2 (AA-973): lines carry departure_id
ROUNDING = "line_ceil_0.01"
# AA-974 (ADR 0005a decision 3): VND is a B2B quote currency (not yet a B2C selling currency, so the engine's
# SELLING_CURRENCIES stays as it is). A VND line is rounded up to 1,000 VND (proposed default, to confirm with
# Ms. Thu); every other currency to the cent. The rule is hashed, so a USD quote's hash is unchanged.
QUOTE_CURRENCIES: frozenset[str] = engine.SELLING_CURRENCIES | {"VND"}
LINE_INCREMENTS: dict[str, Decimal] = {"VND": Decimal("1000")}
UNIT = "per_line"  # b2b_price is the whole line (quantity included)
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


def rounding_rule(currency: str) -> str:
    """The line rounding of a quote currency, as stored and hashed (``line_ceil_0.01``, ``line_ceil_1000``)."""
    increment = LINE_INCREMENTS.get(currency)
    return ROUNDING if increment is None else f"line_ceil_{increment}"


def round_line(value: Decimal, currency: str) -> Decimal:
    """A line's ``b2b_price``: up to the cent, or up to the currency's line increment (VND 1,000)."""
    increment = LINE_INCREMENTS.get(currency)
    if increment is None:
        return ceil_cents(value)
    return ((value / increment).to_integral_value(rounding=ROUND_CEILING) * increment).quantize(engine.CENT)


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
    rounding: str = ROUNDING,
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
        "rounding": rounding,
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
                "departure_id": line["departure_id"],
                "b2b_price": format(line["b2b_price"], "f"),
                **_discount_hash(line),
            }
            for line in lines
        ],
        "total": format(total, "f"),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def _discount_hash(line: dict[str, Any]) -> dict[str, Any]:
    """AA-1030: the agency discount of a line, hashed only when there is one (a 0 % line hashes as before)."""
    discount = line.get("agency_discount_pct") or engine.ZERO
    if discount == engine.ZERO:
        return {}
    return {"agency_discount": decimal_text(discount), "rate_card_id": line.get("rate_card_id")}


def _line_plan(component_id: str) -> str | None:
    """The trip plan id of a ``tp:`` line (a plan rate card applies to it), else None."""
    try:
        prefix, ident = parse_component(component_id)
    except ValueError:
        return None
    return ident if prefix == PREFIX_PLAN else None


async def _valid_hours(conn: asyncpg.Connection) -> int:
    raw = await conn.fetchval("SELECT value FROM booking.settings WHERE key = $1", VALID_HOURS_KEY)
    try:
        hours = int(json.loads(raw)) if isinstance(raw, str) else int(raw)
    except (TypeError, ValueError):
        return DEFAULT_VALID_HOURS
    return hours if 1 <= hours <= MAX_VALID_HOURS else DEFAULT_VALID_HOURS


async def _pricing_data(
    conn: asyncpg.Connection, currencies: set[str], on: date, *, require_all: bool = True
) -> tuple[repository.DatedRule, dict[str, repository.DatedFxRate]]:
    try:
        rule = await repository.rule_in_force(conn, engine.Channel.B2B, on)
        rates = await repository.fx_rates_in_force(conn, currencies, on)
    except Exception as exc:  # noqa: BLE001 — a DB error or an invalid stored row both mean "cannot price now"
        log.warning("b2b_pricing_data_unavailable error=%s", type(exc).__name__)
        raise B2BError("unavailable", "pricing data is not available") from None
    if rule is None:
        raise B2BError("unavailable", "no B2B pricing rule in force")
    # require_all=False (the agency tariff): the caller checks the quote currency; a cost currency without a rate
    # leaves only the lines that need it unpriced (engine.MissingFxRate in price_line).
    missing = sorted(c for c in currencies if require_all and c != engine.BASE_CURRENCY and c not in rates)
    if missing:
        raise B2BError("unavailable", f"no approved FX rate for {', '.join(missing)}")
    return rule, rates


def _lock(currency: str, rates: dict[str, repository.DatedFxRate], on: date) -> FxLock:
    if currency == engine.BASE_CURRENCY:
        return FxLock(currency, engine.ONE, engine.ZERO, on, USD_SOURCE)
    dated = rates[currency]
    return FxLock(currency, dated.rate.rate_per_usd, dated.rate.buffer_pct, dated.as_of, dated.source[:64])


@dataclass(frozen=True)
class PriceBasis:
    """What a partner's B2B lines are priced with on one day in one quote currency: the ``b2b`` rule in force, the
    approved FX rates, the quote currency's FX lock and the partner's agency terms (AA-1030). One basis, one formula
    (:func:`price_line`) for ``POST /quotes`` and the agency tariff (``app.b2b.tariff``, AA-1030 G3)."""

    on: date
    currency: str
    rule: repository.DatedRule
    rates: dict[str, engine.FxRate]
    fx: FxLock
    terms: agency_pricing.AgencyTerms

    @property
    def margin(self) -> Decimal:
        return self.rule.rule.margin(peak=False)


@dataclass(frozen=True)
class LinePrice:
    """One priced line: the net behind it (USD, never serialised), the agency discount and card, the ``b2b_price``."""

    net_usd: Decimal
    discount: Decimal
    rate_card_id: str | None
    b2b_price: Decimal


async def price_basis(
    conn: asyncpg.Connection,
    *,
    partner_id: str,
    currency: str,
    cost_currencies: set[str],
    on: date,
    require_all: bool = True,
) -> PriceBasis:
    """The basis of a partner's prices on ``on``; B2BError ``unavailable`` without a rule or a needed FX rate (with
    ``require_all=False`` only the quote currency's rate is needed)."""
    rule, rates = await _pricing_data(conn, {currency, *cost_currencies}, on, require_all=require_all)
    if currency != engine.BASE_CURRENCY and currency not in rates:
        raise B2BError("unavailable", f"no approved FX rate for {currency}")
    return PriceBasis(
        on=on,
        currency=currency,
        rule=rule,
        rates={code: dated.rate for code, dated in rates.items()},
        fx=_lock(currency, rates, on),
        terms=await agency_pricing.terms_for(conn, partner_id, on),
    )


def price_line(basis: PriceBasis, cost: LineCost, plan_id: str | None) -> LinePrice:
    """The ``b2b_price`` of one line: its nets to USD, the rule's price (``net / (1 - margin - card fee)``) in the quote
    currency, less the partner's discount for the line's plan, rounded up (:func:`round_line`). Raises
    ``engine.PricingError`` (a missing FX rate, a bad rule)."""
    net_usd = sum((engine.to_usd(amount, cur, basis.rates) for amount, cur in cost.costs), start=engine.ZERO)
    sell = engine.from_usd(
        engine.sell_price_usd(net_usd, basis.margin, basis.rule.rule.card_fee_pct), basis.rates.get(basis.currency)
    )
    discount, card_id = basis.terms.discount_for(plan_id)
    return LinePrice(
        net_usd=net_usd,
        discount=discount,
        rate_card_id=card_id,
        b2b_price=round_line(agency_pricing.apply_discount(sell, discount), basis.currency),
    )


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
    if currency not in QUOTE_CURRENCIES:
        allowed = ", ".join(sorted(QUOTE_CURRENCIES))
        raise B2BError("invalid", f"{currency} is not a quote currency (one of {allowed})")
    refs = [line.ask.line_ref for line in lines]
    if len(set(refs)) != len(refs):
        raise B2BError("invalid", "line_ref must be unique within a quote")
    now = now or datetime.now(UTC)
    on = now.date()

    async with pool.acquire() as conn:
        # AA-986 L5: a tp: line whose tour left the catalog is 409 tour_not_served, before any rate is read.
        await served.require_served(conn, [(line.ask.line_ref, line.ask.component_id) for line in lines])
        costs: list[LineCost] = await price_lines(conn, [line.ask for line in lines])
        own_hold = await conn.fetchval(OWN_HOLD_SQL, partner_id, proposal_ref, proposal_version)
        seats = await departures.availability(
            conn,
            [
                departures.SeatAsk(
                    line_ref=line.ask.line_ref,
                    component_id=line.ask.component_id,
                    service_date=line.ask.service_date,
                    departure_id=line.departure_id,
                    seats=departures.seats(line.ask.adult, line.ask.child),
                )
                for line in lines
            ],
            today=on,
            exclude_hold=own_hold,
        )
        basis = await price_basis(
            conn,
            partner_id=partner_id,
            currency=currency,
            cost_currencies={cur for lc in costs for _, cur in lc.costs},
            on=on,
        )
        fx, rule, margin, terms = basis.fx, basis.rule, basis.margin, basis.terms

        priced: list[dict[str, Any]] = []
        for no, (line, lc, (availability, departure_id, _)) in enumerate(
            zip(lines, costs, seats, strict=True), start=1
        ):
            ask = line.ask
            try:
                price = price_line(basis, lc, _line_plan(ask.component_id))
            except engine.PricingError as exc:
                raise B2BError("unavailable", f"line {ask.line_ref}: {exc}") from None
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
                    "availability": availability,
                    "departure_id": departure_id,
                    "b2b_price": price.b2b_price,
                    "net_cost_usd": price.net_usd.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP),
                    "supplier_rate_ids": lc.rate_ids,
                    "agency_discount_pct": price.discount,
                    "rate_card_id": price.rate_card_id,
                }
            )
        total = sum((p["b2b_price"] for p in priced), start=engine.ZERO)
        rounding = rounding_rule(currency)
        digest = price_hash(
            currency=currency, fx=fx, rule=rule, margin=margin, lines=priced, total=total, rounding=rounding
        )
        valid_until = now + timedelta(hours=await _valid_hours(conn))

        async with conn.transaction():
            quote_id = await conn.fetchval(
                """
                INSERT INTO booking.b2b_quotes
                    (partner_id, m2m_client_id, proposal_ref, proposal_version, currency, fx_rate_per_usd,
                     fx_buffer_pct, fx_as_of, fx_source, pricing_rule_valid_from, margin_applied, card_fee_pct,
                     rounding, priced_on, total_b2b_price, price_hash, valid_until, created_at, partner_tier_code,
                     tier_discount_pct)
                VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19,
                        $20)
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
                rounding,
                on,
                total,
                digest,
                valid_until,
                now,
                terms.tier_code,
                terms.tier_discount,
            )
            await conn.executemany(
                """
                INSERT INTO booking.b2b_quote_lines
                    (quote_id, line_no, line_ref, component_id, component_kind, service_date, nights, pax_adult,
                     pax_child, pax_infant, rooms, unit, availability, b2b_price, net_cost_usd, supplier_rate_ids,
                     departure_id, agency_discount_pct, rate_card_id)
                VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb, $12, $13, $14, $15, $16::uuid[],
                        $17::uuid, $18, $19::uuid)
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
                        p["departure_id"],
                        p["agency_discount_pct"],
                        p["rate_card_id"],
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
                "departure_id": p["departure_id"],
            }
            for p in priced
        ],
        "total_b2b_price": money(total, currency),
    }
