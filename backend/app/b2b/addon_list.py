"""The AA add-ons an agency can quote (booking.b2b.v1 1.11 ``GET /add-ons``; AA-1152, S285 advisories PR-29 T6 / B2,
slice L9 of the root ``docs/audits/2026-11-19-S285-session/advisory-r2-plan.md``).

An add-on is a published offer of :mod:`app.supply.addons` (bk-068): one ``sv:`` service of an AA supplier sold at
some CIS places on the days it runs. The agency CRM lists them in its "AA add-on" drawer (one day of a proposal: its
date and party) and in the agency's own "from" prices (option C of B2), then quotes the chosen one with ``POST /quotes``
like any ``sv:`` line (prices are fixed at quote time; the sale rules are checked there, 422 below the minimum, not a
running day, past the cut-off).

What a row says:

- **which days**: the rows of ``booking.v_place_addons`` (contract ``supply.place_addons.v1``): a published offer, an
  in-force adult rate under a published contract, a running weekday, no stop-sale. With ``date`` only that day's
  offers; without, each offer once with the first day it can be sold (``first_date``);
- **where**: its places (``destination_id``, ``role``, the CIS name and ISO-2 country, names fail soft);
- **a price for the party**: ``b2b_price_per_person``, the agency's own B2B price (its tier or contract discount off)
  for ``pax`` adults on ``first_date``, per traveller (a group or vehicle unit is shared over the party), and
  ``rrp_per_person``, AA's suggested retail price (root ADR 0019 b15: the same sell price before the agency discount,
  plus the B2B rule's RRP markup, rounded up to the currency's rounding increment of the pricing policy). A row that
  cannot be priced today (no FX rate for a cost currency, a rate the engine refuses) keeps both null: it is still
  listed, the quote decides.

Never a net, a margin, a supplier identity or a rate id (the contract's rule, ``tests/unit/test_b2b_api.py`` walks
the answer). Bounded: at most :data:`MAX_ITEMS` offers per answer, priced with one batched rate query.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any

import asyncpg

from app.b2b import quotes
from app.b2b.components import LineAsk, LineCost, price_lines
from app.b2b.errors import B2BError
from app.catalog import destinations as catalog
from app.pricing import engine, policy_values

log = logging.getLogger(__name__)

MAX_ITEMS = 200  # technical-constant: one answer's bound (the contract's maxItems), not a business value
PAX_MAX = 60  # technical-constant: the contract Pax.adult bound

# One row per offer: the asked day, else the first day it can be sold (the view lists today onwards).
OFFERS_SQL = """
SELECT DISTINCT ON (v.offer_id)
       v.component_id, v.offer_id::text AS offer_id, v.service_id::text AS service_id, v.service_type, v.unit,
       v.duration_minutes, v.max_group_size, v.country_code, v.min_pax, v.cutoff_hours, v.operating_weekdays,
       v.start_times, v.sort_order, v.featured, v.inventory_kind, v.free_sale_max_pax, v.service_date,
       s.title
FROM booking.v_place_addons v JOIN booking.services s ON s.id = v.service_id
WHERE ($1::uuid IS NULL OR v.destination_id = $1::uuid) AND ($2::date IS NULL OR v.service_date = $2::date)
ORDER BY v.offer_id, v.service_date
"""
PLACES_SQL = """
SELECT DISTINCT v.offer_id::text AS offer_id, v.destination_id::text AS destination_id, v.place_role
FROM booking.v_place_addons v
WHERE v.offer_id = ANY($1::uuid[]) AND ($2::date IS NULL OR v.service_date = $2::date)
ORDER BY 1, 2
"""


def _order(row: Any) -> tuple[bool, int, str, str]:
    return (not row["featured"], int(row["sort_order"]), str(row["title"]).lower(), row["offer_id"])


def per_person(total: Decimal, party: int, currency: str) -> Decimal:
    """A party price shared per traveller, rounded up like a quote line (cent, VND 1,000)."""
    return quotes.round_line(total / party, currency)


def rrp_per_person(
    sell_per_person: Decimal, markup: Decimal | None, currency: str, increments: dict[str, Decimal]
) -> Decimal | None:
    """AA's suggested retail price per traveller (root ADR 0019 b15) from the sell price before the agency discount:
    up to the currency's rounding increment (Pricing > Policy); a quote currency the policy does not round (VND) is
    rounded like a quote line. None without an RRP markup on the B2B rule."""
    if markup is None:
        return None
    try:
        return engine.rrp_price(sell_per_person, markup, currency, increments)
    except engine.PricingError:
        return quotes.round_line(sell_per_person * (engine.ONE + markup), currency)


def _sell_before_discount(basis: quotes.PriceBasis, cost: LineCost) -> Decimal:
    """The line at the B2B rule's target margin in the quote currency, before the agency's discount (unrounded)."""
    net_usd = sum((engine.to_usd(amount, cur, basis.rates) for amount, cur in cost.costs), start=engine.ZERO)
    sell_usd = engine.sell_price_usd(net_usd, basis.margin, basis.rule.rule.card_fee_pct)
    return engine.from_usd(sell_usd, basis.rates.get(basis.currency))


async def _costs(conn: asyncpg.Connection, asks: list[LineAsk]) -> dict[str, LineCost]:
    """``{line_ref: cost}`` for the asks that price; one batched query, then one ask at a time when the batch fails."""
    if not asks:
        return {}
    try:
        return {a.line_ref: c for a, c in zip(asks, await price_lines(conn, asks), strict=True)}
    except B2BError:
        if len(asks) == 1:
            return {}
    out: dict[str, LineCost] = {}
    for ask in asks:
        out.update(await _costs(conn, [ask]))
    return out


async def _places(
    conn: asyncpg.Connection, rows: list[Any], on_date: date | None
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    """``({offer id: [place]}, {destination id: catalog place})`` of the listed offers (names fail soft)."""
    places: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if not rows:
        return places, {}
    for p in await conn.fetch(PLACES_SQL, [r["offer_id"] for r in rows], on_date):
        places[p["offer_id"]].append({"destination_id": p["destination_id"], "role": p["place_role"]})
    names = await catalog.destinations_by_id(conn, [p["destination_id"] for ps in places.values() for p in ps])
    return places, names


async def _increments(conn: asyncpg.Connection, today: date) -> dict[str, Decimal]:
    try:
        return (await policy_values.load(conn, today)).rounding_increments
    except policy_values.PolicyUnavailable:
        return {}


def _prices(
    basis: quotes.PriceBasis, cost: LineCost | None, pax: int, increments: dict[str, Decimal], offer_id: str
) -> tuple[Decimal | None, Decimal | None]:
    """``(b2b per person, rrp per person)`` of one offer, ``(None, None)`` when it cannot be priced today."""
    if cost is None:
        return None, None
    currency = basis.currency
    try:
        b2b = per_person(quotes.price_line(basis, cost, None).b2b_price, pax, currency)
        rrp = rrp_per_person(_sell_before_discount(basis, cost) / pax, basis.rule.rrp_markup_pct, currency, increments)
    except engine.PricingError as exc:
        log.info("b2b_addon_unpriced offer=%s error=%s", offer_id, type(exc).__name__)
        return None, None
    return b2b, rrp


def check_ask(currency: str, on_date: date | None, today: date) -> None:
    if currency not in quotes.QUOTE_CURRENCIES:
        allowed = ", ".join(sorted(quotes.QUOTE_CURRENCIES))
        raise B2BError("invalid", f"{currency} is not a quote currency (one of {allowed})")
    if on_date is not None and on_date < today:
        raise B2BError("invalid", "date must be today or later")


async def list_addons(
    conn: asyncpg.Connection,
    *,
    partner_id: str,
    currency: str,
    pax: int,
    destination_id: str | None,
    on_date: date | None,
    today: date,
) -> dict[str, Any]:
    """The contract ``AddonPage``. Raises B2BError ``invalid`` (not a quote currency, a past date) / ``unavailable``
    (no B2B rule or no FX rate for the quote currency)."""
    check_ask(currency, on_date, today)
    rows = sorted(await conn.fetch(OFFERS_SQL, destination_id, on_date), key=_order)
    truncated = len(rows) > MAX_ITEMS
    rows = rows[:MAX_ITEMS]
    places, names = await _places(conn, rows, on_date)
    asks = [
        LineAsk(line_ref=r["offer_id"], component_id=r["component_id"], service_date=r["service_date"], nights=None,
                adult=pax, child=0, infant=0)
        for r in rows
    ]  # fmt: skip
    costs = await _costs(conn, asks)
    basis = await quotes.price_basis(
        conn,
        partner_id=partner_id,
        currency=currency,
        cost_currencies={cur for lc in costs.values() for _, cur in lc.costs},
        on=today,
        require_all=False,
    )
    increments = await _increments(conn, today)
    items = []
    for row in rows:
        b2b, rrp = _prices(basis, costs.get(row["offer_id"]), pax, increments, row["offer_id"])
        items.append(_item(row, places.get(row["offer_id"], []), names, b2b, rrp, currency))
    return {"currency": currency, "priced_for_pax": pax, "date": on_date, "truncated": truncated, "items": items}


def _item(
    row: Any,
    places: list[dict[str, Any]],
    names: dict[str, dict[str, Any]],
    b2b: Decimal | None,
    rrp: Decimal | None,
    currency: str,
) -> dict[str, Any]:
    return {
        "component_id": row["component_id"],
        "offer_id": row["offer_id"],
        "kind": row["service_type"],
        "title": row["title"],
        "unit": row["unit"],
        "duration_minutes": row["duration_minutes"],
        "max_group_size": row["max_group_size"],
        "country_code": row["country_code"],
        "min_pax": row["min_pax"],
        "cutoff_hours": row["cutoff_hours"],
        "operating_weekdays": list(row["operating_weekdays"] or []),
        "start_times": list(row["start_times"] or []),
        "inventory_kind": row["inventory_kind"],
        "featured": row["featured"],
        "first_date": row["service_date"],
        "places": [
            {
                **p,
                "name": (names.get(p["destination_id"]) or {}).get("name"),
                "country_code": (names.get(p["destination_id"]) or {}).get("country_code"),
            }
            for p in places
        ],  # fmt: skip
        "b2b_price_per_person": None if b2b is None else quotes.money(b2b, currency),
        "rrp_per_person": None if rrp is None else quotes.money(rrp, currency),
    }
