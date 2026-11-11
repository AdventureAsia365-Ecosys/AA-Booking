"""The agency tariff: one partner's own price per tour, season and pax band (AA-1030 G3; root research note
``docs/research/2026-11-05-asian-trails-b2b-benchmark``, AA's answer to a "Cloud Tariff").

- **Grid = the price matrix, cut where a rate changes.** The ``b2b`` rows of ``booking.trip_plan_prices`` (bk-032 /
  bk-039) still valid on the day (``valid_to`` >= today) give the season label, the pax band and the window, from
  today at the earliest. Each window is then split at every date where a supplier rate of one of the plan's
  components starts or ends (:func:`app.catalog.cost_sheet.rate_cuts`, the cost sheet's own cuts), because only the
  cost sheet's computed rows are cut there, and only as of their last recompute: a hand row (not ``computed``) or a
  row staff ``overridden`` keeps whatever window staff gave it, and may span a rate change. So every departure date
  of a tariff row's ``valid_from`` .. ``valid_to`` uses the same rates and a quote of any of them answers the row's
  price; neighbouring pieces of one window that price the same are joined back. Only sellable trip plans whose tour
  is served (in ``catalog.v_tour``, not retired or paused: ``app.catalog.liveness``), the same set ``GET /products``
  offers. A window and band held in several currencies is listed once (the USD row first).
- **Price = what ``POST /quotes`` charges.** Each row is priced by the quote engine itself (``app.b2b.components.
  price_lines`` for the plan's ``tp:`` line, then ``app.b2b.quotes.price_line`` with the partner's
  :class:`~app.b2b.quotes.PriceBasis`): the plan's components at the supplier rates in force on the tariff row's
  first day (``priced_for_date`` = ``valid_from``; any day of the row prices the same, see above), for the band's
  smallest party (all adults, the default room mix: doubles plus a single when odd), at the ``b2b`` rule, less the
  partner's discount in force today (its plan
  rate card, else its catalogue card, else its tier: ``app.b2b.agency_pricing``), rounded up like a quote line.
  ``price`` is that line's ``b2b_price`` for the party (a quote of that party on that day answers the same amount);
  ``price_per_person`` is it divided by the party, rounded up the same way. No second formula: a tariff row and a
  quote line can only differ when an input changes between the two reads (a rate, a rule, an FX rate, a discount).
- **Not the matrix amount.** The matrix's b2b ``amount`` is the plan's public price list (rounded up to 5 USD per
  person, the peak uplift applied); the RRP is built on it. B2B quotes never read it (they price the components), so
  the tariff does not either. The row's RRP (``rrp_amount``, the public suggested retail price, ADR 0019 b15) is
  shown as it is, in the row's currency.
- **Rows that cannot be quoted** (a plan without bookable components, a component without a rate in force, a cost
  currency without an approved FX rate) are left out of the agency's tariff; the staff read lists them with the
  reason (``unpriced``).
- **The asked currency must price today** (S270): a quote currency without an approved FX rate in force is refused
  as ``invalid`` (422, :func:`check_currency`, listing the currencies that do price), before any row is read; only
  the tariff does this, ``POST /quotes`` keeps its 503 ``unavailable``.
- **Child, infant and single prices** (1.7, AA-1075 bk-15), each asked of the quote engine the same way, so a CRM
  can show them before it asks for a quote: ``child_price_per_person`` = the engine's price of one child of the band's
  smallest party (the rule's ``child_pct`` of the adult price; the adult price when the rule has none), so a quote of
  a adults and c children charges a x ``price_per_person`` + c x ``child_price_per_person``, within the line's
  rounding;
  ``infant_price`` = what one infant adds to that party's line (the rule's ``infant_flat_usd`` sold at the rule; 0
  when it has none); ``single_supplement_per_person`` = the party's line with every traveller in a single room,
  per person, less ``price_per_person`` (0 for a tour without hotel rooms or a party of one, who already sleeps in a
  single; ``null`` when a single room has no rate in force: ask a quote). The age bands that make a child or an
  infant are on the product (``GET /products`` ``age_bands``). Sell side only, never a net.
- **Never** a net cost, a supplier rate or a margin (the agency's own discount and the public RRP only).

Read by staff (``GET /booking/pricing/partner-tariffs/{partner_id}``, ``pricing.read``, ``app.routers.
agency_pricing``) and by the agency (``GET /booking/b2b/v1/tariff``, contract ``booking.b2b.v1`` 1.4, scope ``read``),
as JSON or CSV (:func:`to_csv`, the same columns for both, so staff can send the agency the file it would download).
"""

from __future__ import annotations

import csv
import io
import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import asyncpg

from app.b2b import quotes
from app.b2b.components import (
    MAX_RATE_LOOKUPS,
    PLAN_COMPONENTS_SQL,
    PREFIX_PLAN,
    LineAsk,
    LineCost,
    RoomAsk,
    component_id,
    price_lines,
)
from app.b2b.errors import B2BError
from app.catalog import cost_sheet, liveness
from app.catalog.destinations import CATALOG_ERRORS
from app.pricing import engine, repository

log = logging.getLogger(__name__)

SOURCE_SYSTEM = "cis"
CHANNEL = engine.Channel.B2B.value
BASIS = "per_person_band_min_twin_share"
MAX_ROWS = 3000  # matrix rows per tariff (the read stops there and says so: ``truncated``)
MAX_CELLS = 6000  # tariff rows once the windows are cut at rate changes (``truncated`` too)
# Rate lookups a tp: line of the plan needs for the band's party: one per service, up to two room mixes per room.
LOOKUPS_PER_COMPONENT = 2

ROWS_SQL = """
SELECT DISTINCT ON (p.source_id, pr.valid_from, pr.valid_to, pr.pax_min, pr.pax_max)
       p.id::text AS plan_id, p.source_id, t.title, t.country, t.duration_days,
       (SELECT count(*) FROM booking.trip_plan_components c WHERE c.trip_plan_id = p.id) AS components,
       pr.season, pr.peak, pr.valid_from, pr.valid_to, pr.pax_min, pr.pax_max,
       pr.rrp_amount, pr.currency::text AS rrp_currency
FROM booking.trip_plans p
JOIN booking.trip_plan_prices pr ON pr.trip_plan_id = p.id AND pr.channel = 'b2b' AND pr.valid_to >= $1
JOIN LATERAL (SELECT t.title, t.country, t.duration_days FROM catalog.v_tour t
              WHERE t.source_system = p.source_system AND t.source_id = p.source_id LIMIT 1) t ON true
WHERE p.sellable AND p.source_system = 'cis' AND ($2::text IS NULL OR p.source_id = $2)
ORDER BY p.source_id, pr.valid_from, pr.valid_to, pr.pax_min, pr.pax_max, (pr.currency = 'USD') DESC, pr.currency
LIMIT $3
"""

CSV_COLUMNS: tuple[str, ...] = (
    "tour_key",
    "title",
    "country",
    "duration_days",
    "season",
    "valid_from",
    "valid_to",
    "pax_min",
    "pax_max",
    "priced_for_pax",
    "currency",
    "price_per_person",
    "price_for_party",
    "agency_discount_pct",
    "rrp_per_person",
    "rrp_currency",
    "child_price_per_person",
    "infant_price",
    "single_supplement_per_person",
)
# Spreadsheet formula injection (OWASP CSV injection): a text cell starting with one of these is prefixed with "'".
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


@dataclass(frozen=True)
class Cell:
    """One piece of a matrix row to price (``on`` .. ``end``, inside one rate span of every component): the plan's
    ``tp:`` line for the band's smallest party on its first day. ``row_no`` = the matrix row it comes from."""

    no: int
    row: Any
    party: int
    on: date
    end: date
    row_no: int = 0
    singles: bool = False  # AA-1075: every traveller in a single room (the single supplement's line)

    @property
    def ask(self) -> LineAsk:
        return LineAsk(
            line_ref=f"row{self.no}",
            component_id=component_id(PREFIX_PLAN, self.row["plan_id"]),
            service_date=self.on,
            nights=None,
            adult=self.party,
            child=0,
            infant=0,
            rooms=(RoomAsk("single", self.party),) if self.singles else (),
        )

    @property
    def lookups(self) -> int:
        return max(1, int(self.row["components"]) * LOOKUPS_PER_COMPONENT)


def pieces(start: date, end: date, cuts: Iterable[date]) -> list[tuple[date, date]]:
    """``start`` .. ``end`` split at every cut inside it (a cut is the first day of a new piece)."""
    inner = sorted({cut for cut in cuts if start < cut <= end})
    bounds = [start, *inner]
    ends = [cut - timedelta(days=1) for cut in inner] + [end]
    return list(zip(bounds, ends, strict=True))


async def rate_cuts(conn: asyncpg.Connection, plan_ids: Sequence[str]) -> dict[str, set[date]]:
    """``{plan id: the start dates where a rate of its components starts or ends}`` in two queries (the plans'
    components, then the validity spans of every published rate of their items: the cost sheet's own query)."""
    if not plan_ids:
        return {}
    parts: dict[str, list[cost_sheet.Part]] = {}
    for r in await conn.fetch(PLAN_COMPONENTS_SQL, sorted(set(plan_ids))):
        part = cost_sheet.Part(int(r["day_no"]), r["room_type_id"], r["service_id"])
        parts.setdefault(r["trip_plan_id"], []).append(part)
    rooms = sorted({p.room_type_id for ps in parts.values() for p in ps if p.room_type_id})
    services = sorted({p.service_id for ps in parts.values() for p in ps if p.service_id})
    if not rooms and not services:
        return {}
    spans = [
        cost_sheet.Span(r["room_type_id"] or r["service_id"], r["start"], r["stop"])
        for r in await conn.fetch(cost_sheet.SPANS_SQL, rooms, services)
    ]
    return {plan: cost_sheet.rate_cuts(ps, spans) for plan, ps in parts.items()}


async def check_currency(conn: asyncpg.Connection, currency: str, on: date) -> None:
    """Refuse a tariff currency AA does not price in on ``on`` as ``invalid`` (422, the caller's choice): not a quote
    currency, or a quote currency without an approved FX rate in force (S270, AA-1029 / AA-1030 follow-up: an agency
    asking AUD before AUD has a rate is told to pick another currency, not that AA is down). The message lists the
    currencies that do price today (USD always). Only the tariff does this: ``POST /quotes`` and
    :func:`app.b2b.quotes.price_basis` still answer ``unavailable`` (503) for a quote currency without a rate, and a
    supplier cost currency without a rate stays AA's data problem (the tariff leaves those rows unpriced, a quote
    answers 503). A DB error while reading the rates is ``unavailable`` too."""
    if currency not in quotes.QUOTE_CURRENCIES:
        allowed = ", ".join(sorted(quotes.QUOTE_CURRENCIES))
        raise B2BError("invalid", f"{currency} is not a quote currency (one of {allowed})")
    if currency == engine.BASE_CURRENCY:
        return
    try:
        rates = await repository.fx_rates_in_force(conn, quotes.QUOTE_CURRENCIES, on)
    except Exception as exc:  # noqa: BLE001 — a DB error or an invalid stored row both mean "cannot price now"
        log.warning("b2b_tariff_fx_unavailable error=%s", type(exc).__name__)
        raise B2BError("unavailable", "pricing data is not available") from None
    if currency not in rates:
        priced = ", ".join([engine.BASE_CURRENCY, *sorted(rates)])
        raise B2BError("invalid", f"no approved FX rate for {currency} today; AA prices this tariff in {priced}")


def per_person(price: Decimal, party: int, currency: str) -> Decimal:
    """The line price shared over the party, rounded up like a quote line (cent, VND 1,000)."""
    return quotes.round_line(price / party, currency)


def _reason(exc: B2BError, ref: str) -> str:
    """The quote's refusal text without the internal line ref ("line row3: no rate in force …")."""
    return re.sub(rf"^line {re.escape(ref)}: ", "", exc.message)


def chunks(cells: Sequence[Cell]) -> list[list[Cell]]:
    """Cells grouped so one ``price_lines`` call stays within a quote's rate lookup budget."""
    out: list[list[Cell]] = []
    used = 0
    for cell in cells:
        if out and used + cell.lookups <= MAX_RATE_LOOKUPS:
            out[-1].append(cell)
            used += cell.lookups
        else:
            out.append([cell])
            used = cell.lookups
    return out


async def _costs(conn: asyncpg.Connection, cells: Sequence[Cell]) -> dict[int, LineCost | str]:
    """``{cell no: LineCost or the refusal reason}``: whole chunks first, one cell at a time where a chunk fails."""
    out: dict[int, LineCost | str] = {}
    for group in chunks(cells):
        try:
            costs = await price_lines(conn, [c.ask for c in group])
        except B2BError as exc:
            if len(group) == 1:
                out[group[0].no] = _reason(exc, group[0].ask.line_ref)
            else:
                for cell in group:
                    out.update(await _costs(conn, [cell]))
            continue
        out.update({c.no: lc for c, lc in zip(group, costs, strict=True)})
    return out


def _money(amount: Decimal | None, currency: str) -> dict[str, str] | None:
    return None if amount is None else quotes.money(amount, currency)


def _decimal(value: Decimal) -> str:
    return format(value.quantize(engine.RATIO), "f")


async def build(
    conn: asyncpg.Connection,
    *,
    partner_id: str,
    currency: str,
    on: date,
    source_id: str | None = None,
    limit: int = MAX_ROWS,
) -> dict[str, Any]:
    """The tariff of one partner in one quote currency on ``on``. Raises B2BError: ``invalid`` when ``currency`` is
    not a quote currency or has no approved FX rate in force on ``on`` (:func:`check_currency`, the caller's choice);
    ``unavailable`` without a b2b rule, without readable pricing data or without a readable catalog (AA's side)."""
    await check_currency(conn, currency, on)
    try:
        rows = await conn.fetch(ROWS_SQL, on, source_id, limit + 1)
        served = await liveness.states(conn, [r["source_id"] for r in rows], strict=True)
    except CATALOG_ERRORS:
        raise B2BError("unavailable", "the catalog is not readable right now", retry_after_s=60) from None
    truncated = len(rows) > limit
    rows = [r for r in rows[:limit] if (state := served.get(r["source_id"])) is not None and state.served]
    cuts = await rate_cuts(conn, [r["plan_id"] for r in rows if r["components"]])

    cells: list[Cell] = []
    unpriced: list[dict[str, Any]] = []
    for row_no, row in enumerate(rows, start=1):
        start = max(row["valid_from"], on)
        if not row["components"]:
            unpriced.append(_unpriced(row, start, row["valid_to"], "the trip plan has no bookable components"))
            continue
        for first, last in pieces(start, row["valid_to"], cuts.get(row["plan_id"], ())):
            if len(cells) == MAX_CELLS:
                truncated = True
                break
            cell = Cell(no=len(cells) + 1, row=row, party=int(row["pax_min"]), on=first, end=last, row_no=row_no)
            cells.append(cell)
    # AA-1075: the same cells with every traveller in a single room (a refusal there only empties the supplement).
    singles = [replace(cell, no=cell.no + len(cells), singles=True) for cell in cells]
    costs = await _costs(conn, [*cells, *singles])
    basis = await quotes.price_basis(
        conn,
        partner_id=partner_id,
        currency=currency,
        cost_currencies={cur for lc in costs.values() if isinstance(lc, LineCost) for _, cur in lc.costs},
        on=on,
        require_all=False,
    )

    items: list[tuple[int, dict[str, Any]]] = []
    refused: list[tuple[int, dict[str, Any]]] = []
    for cell in cells:
        cost = costs[cell.no]
        if isinstance(cost, str):
            refused.append((cell.row_no, _unpriced(cell.row, cell.on, cell.end, cost)))
            continue
        try:
            price = quotes.price_line(basis, cost, cell.row["plan_id"])
            extras = _by_age_and_room(basis, cell, cost, costs[cell.no + len(cells)], price, currency)
        except engine.PricingError as exc:
            refused.append((cell.row_no, _unpriced(cell.row, cell.on, cell.end, str(exc))))
            continue
        items.append((cell.row_no, {**_item(cell, price, currency), **extras}))
    unpriced.extend(_joined(refused, ("reason",)))
    return {
        "currency": currency,
        "priced_on": on,
        "basis": BASIS,
        "tier_code": basis.terms.tier_code,
        "fx": {
            "base_currency": engine.BASE_CURRENCY,
            "quote_currency": currency,
            "rate_per_usd": format(basis.fx.selling_rate, "f"),
            "rate_date": basis.fx.as_of.isoformat(),
            "source": basis.fx.source,
        },
        "items": _joined(items, SAME_PRICE),
        "unpriced": unpriced,
        "truncated": truncated,
    }


def _by_age_and_room(
    basis: quotes.PriceBasis,
    cell: Cell,
    cost: LineCost,
    singles: LineCost | str,
    price: quotes.LinePrice,
    currency: str,
) -> dict[str, Any]:
    """AA-1075 (bk-15): the child price per person, the infant price and the single supplement per person of one
    tariff row, each priced by the quote engine (see the module doc). Raises ``engine.PricingError`` like the row."""
    plan, party = cell.row["plan_id"], cell.party
    adult_pp = per_person(price.b2b_price, party, currency)
    children = quotes.price_line(basis, cost, plan, pax=(0, party, 0)).b2b_price
    with_infant = quotes.price_line(basis, cost, plan, pax=(party, 0, 1)).b2b_price
    supplement: Decimal | None = None
    if isinstance(singles, LineCost):
        try:
            single_pp = per_person(quotes.price_line(basis, singles, plan).b2b_price, party, currency)
        except engine.PricingError:  # a single room's cost currency without a rate: the row stands, no supplement
            single_pp = None
        supplement = None if single_pp is None else max(single_pp - adult_pp, engine.ZERO)
    return {
        "child_price_per_person": quotes.money(per_person(children, party, currency), currency),
        "infant_price": quotes.money(max(with_infant - price.b2b_price, engine.ZERO), currency),
        "single_supplement_per_person": _money(supplement, currency),
    }


# Two neighbouring pieces of one matrix row with these equal are one tariff row (the later one's valid_to kept).
SAME_PRICE = (
    "price",
    "price_per_person",
    "agency_discount_pct",
    "rate_card_id",
    "child_price_per_person",
    "infant_price",
    "single_supplement_per_person",
)


def _joined(pieces_of: Sequence[tuple[int, dict[str, Any]]], same: Sequence[str]) -> list[dict[str, Any]]:
    """Join the neighbouring pieces of one matrix row that answer the same (``same`` keys), in order."""
    out: list[dict[str, Any]] = []
    last_row = None
    for row_no, entry in pieces_of:
        prev = out[-1] if out and last_row == row_no else None
        if (
            prev is not None
            and prev["valid_to"] + timedelta(days=1) == entry["valid_from"]
            and all(prev[k] == entry[k] for k in same)
        ):
            prev["valid_to"] = entry["valid_to"]
        else:
            out.append(dict(entry))
        last_row = row_no
    return out


def _tour(row: Any, start: date, end: date) -> dict[str, Any]:
    return {
        "tour": {"source_system": SOURCE_SYSTEM, "source_id": row["source_id"]},
        "title": row["title"],
        "country": row["country"],
        "duration_days": row["duration_days"],
        "season": row["season"],
        "valid_from": start,
        "valid_to": end,
        "pax_min": row["pax_min"],
        "pax_max": row["pax_max"],
    }


def _unpriced(row: Any, start: date, end: date, reason: str) -> dict[str, Any]:
    return {**_tour(row, start, end), "reason": reason[:300]}


def _item(cell: Cell, price: quotes.LinePrice, currency: str) -> dict[str, Any]:
    row = cell.row
    rrp_currency = (row["rrp_currency"] or "").strip()
    return {
        **_tour(row, cell.on, cell.end),
        "priced_for_pax": cell.party,
        "priced_for_date": cell.on,
        "price": quotes.money(price.b2b_price, currency),
        "price_per_person": quotes.money(per_person(price.b2b_price, cell.party, currency), currency),
        "agency_discount_pct": _decimal(price.discount),
        "rate_card_id": price.rate_card_id,
        "rrp_per_person": _money(row["rrp_amount"], rrp_currency) if row["rrp_amount"] is not None else None,
    }


def agency_view(tariff: dict[str, Any]) -> dict[str, Any]:
    """The contract body (``booking.b2b.v1`` ``Tariff``): no unpriced rows, no rate card ids."""
    return {
        **{k: tariff[k] for k in ("currency", "priced_on", "basis", "tier_code", "fx", "truncated")},
        "items": [{k: v for k, v in item.items() if k != "rate_card_id"} for item in tariff["items"]],
    }


def _cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return f"'{text}" if text.startswith(FORMULA_START) else text


def to_csv(tariff: dict[str, Any]) -> str:
    """One line per tariff row, columns :data:`CSV_COLUMNS` (an open pax band has an empty ``pax_max``)."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(CSV_COLUMNS)
    for item in tariff["items"]:
        rrp = item["rrp_per_person"]
        writer.writerow(
            _cell(v)
            for v in (
                item["tour"]["source_id"],
                item["title"],
                item["country"],
                item["duration_days"],
                item["season"],
                item["valid_from"],
                item["valid_to"],
                item["pax_min"],
                item["pax_max"],
                item["priced_for_pax"],
                item["price"]["currency"],
                item["price_per_person"]["amount"],
                item["price"]["amount"],
                item["agency_discount_pct"],
                rrp["amount"] if rrp else None,
                rrp["currency"] if rrp else None,
                (item.get("child_price_per_person") or {}).get("amount"),
                (item.get("infant_price") or {}).get("amount"),
                (item.get("single_supplement_per_person") or {}).get("amount"),
            )
        )
    return out.getvalue()


def filename(agency_name: str, currency: str, on: date) -> str:
    """``aa-tariff-<agency slug>-<currency>-<date>.csv`` (ASCII only, for a Content-Disposition header)."""
    slug = re.sub(r"[^a-z0-9]+", "-", agency_name.lower()).strip("-")[:40] or "agency"
    return f"aa-tariff-{slug}-{currency.lower()}-{on.isoformat()}.csv"
