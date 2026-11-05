"""The agency tariff: one partner's own price per tour, season and pax band (AA-1030 G3; root research note
``docs/research/2026-11-05-asian-trails-b2b-benchmark``, AA's answer to a "Cloud Tariff").

- **Grid = the price matrix.** One tariff row per ``b2b`` row of ``booking.trip_plan_prices`` (bk-032 / bk-039) still
  valid on the day (``valid_to`` >= today): its season label, validity window and pax band. Only sellable trip plans
  whose tour is served (in ``catalog.v_tour``, not retired or paused: ``app.catalog.liveness``), the same set
  ``GET /products`` offers. A window and band held in several currencies is listed once (the USD row first).
- **Price = what ``POST /quotes`` charges.** Each row is priced by the quote engine itself (``app.b2b.components.
  price_lines`` for the plan's ``tp:`` line, then ``app.b2b.quotes.price_line`` with the partner's
  :class:`~app.b2b.quotes.PriceBasis`): the plan's components at the supplier rates in force on the row's first
  bookable day (the window start, or today when the window has started; the cost sheet cuts windows where a rate
  changes, so every day of a window prices the same), for the band's smallest party (all adults, the default room
  mix: doubles plus a single when odd), at the ``b2b`` rule, less the partner's discount in force today (its plan
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
- **Never** a net cost, a supplier rate or a margin (the agency's own discount and the public RRP only).

Read by staff (``GET /booking/pricing/partner-tariffs/{partner_id}``, ``pricing.read``, ``app.routers.
agency_pricing``) and by the agency (``GET /booking/b2b/v1/tariff``, contract ``booking.b2b.v1`` 1.3, scope ``read``),
as JSON or CSV (:func:`to_csv`, the same columns for both, so staff can send the agency the file it would download).
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import asyncpg

from app.b2b import quotes
from app.b2b.components import MAX_RATE_LOOKUPS, PREFIX_PLAN, LineAsk, LineCost, component_id, price_lines
from app.b2b.errors import B2BError
from app.catalog import liveness
from app.catalog.destinations import CATALOG_ERRORS
from app.pricing import engine

SOURCE_SYSTEM = "cis"
CHANNEL = engine.Channel.B2B.value
BASIS = "per_person_band_min_twin_share"
MAX_ROWS = 3000  # matrix rows per tariff (the read stops there and says so: ``truncated``)
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
)
# Spreadsheet formula injection (OWASP CSV injection): a text cell starting with one of these is prefixed with "'".
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


@dataclass(frozen=True)
class Cell:
    """One matrix row to price: the plan's ``tp:`` line for the band's smallest party on the row's first day."""

    no: int
    row: Any
    party: int
    on: date

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
        )

    @property
    def lookups(self) -> int:
        return max(1, int(self.row["components"]) * LOOKUPS_PER_COMPONENT)


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
    """The tariff of one partner in one quote currency on ``on``. Raises B2BError (``invalid`` currency,
    ``unavailable`` without a b2b rule, an FX rate for ``currency`` or a readable catalog)."""
    if currency not in quotes.QUOTE_CURRENCIES:
        allowed = ", ".join(sorted(quotes.QUOTE_CURRENCIES))
        raise B2BError("invalid", f"{currency} is not a quote currency (one of {allowed})")
    try:
        rows = await conn.fetch(ROWS_SQL, on, source_id, limit + 1)
        served = await liveness.states(conn, [r["source_id"] for r in rows], strict=True)
    except CATALOG_ERRORS:
        raise B2BError("unavailable", "the catalog is not readable right now", retry_after_s=60) from None
    truncated = len(rows) > limit
    rows = [r for r in rows[:limit] if (state := served.get(r["source_id"])) is not None and state.served]

    cells: list[Cell] = []
    unpriced: list[dict[str, Any]] = []
    for no, row in enumerate(rows, start=1):
        if not row["components"]:
            unpriced.append(_unpriced(row, "the trip plan has no bookable components"))
            continue
        cells.append(Cell(no=no, row=row, party=int(row["pax_min"]), on=max(row["valid_from"], on)))
    costs = await _costs(conn, cells)
    basis = await quotes.price_basis(
        conn,
        partner_id=partner_id,
        currency=currency,
        cost_currencies={cur for lc in costs.values() if isinstance(lc, LineCost) for _, cur in lc.costs},
        on=on,
        require_all=False,
    )

    items: list[dict[str, Any]] = []
    for cell in cells:
        cost = costs[cell.no]
        if isinstance(cost, str):
            unpriced.append(_unpriced(cell.row, cost))
            continue
        try:
            price = quotes.price_line(basis, cost, cell.row["plan_id"])
        except engine.PricingError as exc:
            unpriced.append(_unpriced(cell.row, str(exc)))
            continue
        items.append(_item(cell, price, currency))
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
        "items": items,
        "unpriced": unpriced,
        "truncated": truncated,
    }


def _tour(row: Any) -> dict[str, Any]:
    return {
        "tour": {"source_system": SOURCE_SYSTEM, "source_id": row["source_id"]},
        "title": row["title"],
        "country": row["country"],
        "duration_days": row["duration_days"],
        "season": row["season"],
        "valid_from": row["valid_from"],
        "valid_to": row["valid_to"],
        "pax_min": row["pax_min"],
        "pax_max": row["pax_max"],
    }


def _unpriced(row: Any, reason: str) -> dict[str, Any]:
    return {**_tour(row), "reason": reason[:300]}


def _item(cell: Cell, price: quotes.LinePrice, currency: str) -> dict[str, Any]:
    row = cell.row
    rrp_currency = (row["rrp_currency"] or "").strip()
    return {
        **_tour(row),
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
            )
        )
    return out.getvalue()


def filename(agency_name: str, currency: str, on: date) -> str:
    """``aa-tariff-<agency slug>-<currency>-<date>.csv`` (ASCII only, for a Content-Disposition header)."""
    slug = re.sub(r"[^a-z0-9]+", "-", agency_name.lower()).strip("-")[:40] or "agency"
    return f"aa-tariff-{slug}-{currency.lower()}-{on.isoformat()}.csv"
