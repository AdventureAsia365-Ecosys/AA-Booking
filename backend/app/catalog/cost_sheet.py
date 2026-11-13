"""The cost sheet of a trip plan (root ADR 0005 decision 3, AA-1029 R4): its price matrix computed from its bookable
components (``booking.trip_plan_components``) and the supplier rates in force, priced per channel by the existing
engine, recomputed whenever a rate, a pricing rule or an FX rate changes. Hand rows are kept.

**Net cost per person**, per **start date** window x **pax band** (1, 2, 3-4, 5-6, 7-10, 11+; ADR 0005 d3), for the
band's smallest party (fixed costs are shared by fewer people there, so the price covers the whole band):

- a room component on day ``n`` = one night on ``start + n - 1`` for the party's room mix (``party // 2`` doubles plus
  a single when the party is odd; singles only on a room type that holds one), the rate's pax band matched against the
  room's occupancy, times the rooms of that occupancy;
- a service component on day ``n`` = the service's rate on ``start + n - 1`` for the party, ``person`` x party,
  ``group`` / ``vehicle`` / ``day`` x 1 (one day of a ``day`` service);
- the same rate choice as a B2B quote (``app.supply.rates.rates_in_force_many``: published contract, both validities
  cover the day, newest ``valid_from``, then a pax band, then the lowest net), so a quote of the plan and its matrix
  agree;
- divided by the party, rounded to the cent. Rates in one currency keep it (an FX change then re-prices the row); mixed
  currencies are summed in USD at the approved plain rate in force.

**Windows.** Start dates are cut wherever a component's rate validity starts or ends (shifted by its day offset), from
the earliest cut to the end of the latest rate, so every window prices the same rates for every departure in it;
windows over before the recompute day are left out (at most :data:`MAX_SEGMENTS` windows, the latest left out first)
and neighbouring windows with the same nets, season and peak are merged. The windows are anchored on the rate
validity, never on today, so a recompute on the next day with the same inputs computes the same rows and writes
nothing (no daily history copies, no false ``updated_at`` move for the products feed); a row changes only when a rate,
rule or FX rate changes or a window ends. A window or band with a component without a rate in force gets no row
(never a guessed or zero net). The season label = the labels of the rates
used for 2 travellers (for staff); the season itself is the plan's season calendar on the window's start dates
(AA-1091, bk-055, :mod:`app.pricing.seasons`: windows are also cut where it changes, ``season_code`` on each row),
and ``peak`` = a peak or festive season (the rule's peak uplift then applies). While the plan's calendar is not
configured (no approved range yet), the old rule stays so no price moves before staff enter seasons: ``peak`` when a
2-traveller rate label says "peak" (``season_code`` ``peak``, else ``low``).

**Channels.** ``b2b`` and ``fixed_departure`` (root ADR 0019: the B2C channels are retired; the approved AA-1029
re-scope and the R4 milestone keep these two, fixed departures being sold to agencies too), each only while an
approved rule of that channel is in force on the recompute day; the selling currency is USD (b16). The b2b rows carry
the suggested retail price (``rrp_amount``) when the b2b rule has an RRP markup (b15).

**Writing** (:func:`recompute_plan`, one transaction per plan, the plan row locked): the plan's hand rows come from
its published price list versions (AA-1091, bk-054, :mod:`app.catalog.version_matrix`: each version's rows cut to the
dates it wins today and where the season changes; their RRP refreshed), the computed rows are replaced, a computed
row overlapping a hand row of its channel and currency is left out, and the set is stored with
``app.catalog.trip_plan_prices.store`` (audit ``trip_plan.prices_recomputed``, dated history ``cost_sheet``) only when
it changed. A computed row that overlaps only a hand row already over (ended before the recompute day) starts the day
after it instead of being left out (``trimmed``). At most ``MAX_ROWS`` rows: the latest windows are left out first
(``truncated``).

**The job** ``bk_price_recompute`` (:func:`run`): every plan with a component, a computed row or a ``b2b`` row (a
plan priced by hand gets its RRP filled and refreshed under the b2b rule's markup in force), or one plan
(``payload.plan_id``). Queued by :class:`RecomputeSchedule` whenever the watermark of its inputs moves (approvals of
rules and FX rates, contract and rate edits, plan and component changes, and the UTC day, so a rule or FX rate dated
to start today and a window that ended are picked up; an unchanged day writes nothing);
staff can also recompute one plan at once (``POST …/prices/recompute``).
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import asyncpg

from app.catalog import trip_plan_prices as prices
from app.catalog import version_matrix
from app.jobs import queue
from app.jobs.queue import Job
from app.pricing import engine, repository, seasons
from app.supply.rates import RateAsk, rates_in_force_many

log = logging.getLogger(__name__)

KIND = "bk_price_recompute"
ACTOR = "system:bk_price_recompute"
SCHEDULER = "system:price_recompute"
MAX_ATTEMPTS = 3
ACTION = "trip_plan.prices_recomputed"

PAX_BANDS: tuple[tuple[int, int | None], ...] = ((1, 1), (2, 2), (3, 4), (5, 6), (7, 10), (11, None))
CHANNELS: tuple[engine.Channel, ...] = (engine.Channel.B2B, engine.Channel.FIXED_DEPARTURE)
SELLING_CURRENCY = engine.BASE_CURRENCY
MAX_SEGMENTS = 60
FOREVER = date(9999, 12, 31)
SEASON_MAX = 80
CENT = Decimal("0.01")

COMPONENTS_SQL = """
SELECT c.day_no, c.hotel_room_type_id::text AS room_type_id, c.service_id::text AS service_id, rt.max_occupancy,
       coalesce(rt.status, s.status) AS status, coalesce(h.status, 'active') AS hotel_status
FROM booking.trip_plan_components c
LEFT JOIN booking.hotel_room_types rt ON rt.id = c.hotel_room_type_id
LEFT JOIN booking.hotels h ON h.id = rt.hotel_id
LEFT JOIN booking.services s ON s.id = c.service_id
WHERE c.trip_plan_id = $1::uuid
ORDER BY c.day_no, coalesce(c.hotel_room_type_id, c.service_id)
"""
# Validity spans of every published rate of the plan's items (the rate's validity inside its contract's); also read by
# the price readiness report (``app.catalog.price_readiness``), which uses the currency too.
SPANS_SQL = """
SELECT x.hotel_room_type_id::text AS room_type_id, x.service_id::text AS service_id, x.currency::text AS currency,
       greatest(x.valid_from, k.valid_from) AS start,
       least(coalesce(x.valid_to, '9999-12-31'::date), coalesce(k.valid_to, '9999-12-31'::date)) AS stop
FROM booking.supplier_rates x JOIN booking.supplier_contracts k ON k.id = x.supplier_contract_id
WHERE k.status = 'published' AND (x.hotel_room_type_id = ANY($1::uuid[]) OR x.service_id = ANY($2::uuid[]))
"""
PLANS_SQL = """
SELECT p.id::text AS id, p.source_id FROM booking.trip_plans p
WHERE ($1::uuid IS NULL OR p.id = $1::uuid)
  AND (EXISTS (SELECT 1 FROM booking.trip_plan_components c WHERE c.trip_plan_id = p.id)
       OR EXISTS (SELECT 1 FROM booking.trip_plan_prices r
                  WHERE r.trip_plan_id = p.id AND (r.computed OR r.channel = 'b2b'))
       OR EXISTS (SELECT 1 FROM booking.price_list_versions v
                  WHERE v.trip_plan_id = p.id AND v.status IN ('published', 'superseded')))
ORDER BY p.source_id
"""
LOCK_SQL = "SELECT id::text AS id, source_id FROM booking.trip_plans WHERE id = $1::uuid FOR UPDATE"
# What a recompute depends on; a change of any part queues one run (the idempotency key is the hash).
WATERMARK_SQL = """
SELECT concat_ws('|',
    (SELECT count(*) || ':' || coalesce(max(approved_at)::text, '') FROM booking.policy_approvals),
    (SELECT count(*) || ':' || coalesce(max(updated_at)::text, '') FROM booking.supplier_contracts),
    (SELECT count(*) || ':' || coalesce(max(updated_at)::text, '') FROM booking.supplier_rates),
    (SELECT count(*) || ':' || coalesce(max(updated_at)::text, '') FROM booking.trip_plans),
    (SELECT count(*) FROM booking.trip_plan_components),
    (SELECT count(*) || ':' || coalesce(max(updated_at)::text, '') FROM booking.price_list_versions),
    (SELECT count(*) || ':' || coalesce(max(greatest(decided_at, ended_at))::text, '') FROM booking.season_ranges),
    (SELECT count(*) || ':' || coalesce(max(greatest(decided_at, ended_at))::text, '') FROM booking.holiday_events),
    (SELECT count(*) || ':' || coalesce(max(updated_at)::text, '') FROM booking.season_calendars),
    (now() AT TIME ZONE 'UTC')::date::text) AS mark
"""


# Why a plan gets no computed row (``compute`` info ``reason``; the price readiness report uses the same words).
NO_COMPONENTS = "no_components"
INACTIVE_COMPONENT = "inactive_component"
NO_RULE = "no_rule"
NO_RATES = "no_rates"


# ── Pure part ────────────────────────────────────────────────────────────────


def component_active(row: Any) -> bool:
    """A component prices only while its room type (and hotel) or service is active."""
    return row["status"] == "active" and row["hotel_status"] == "active"


def blocking_reason(parts: int, active: bool, has_rule: bool) -> str | None:
    """The first reason, in the cost sheet's order, that stops a plan before any rate is read; None = read the rates."""
    if not parts:
        return NO_COMPONENTS
    if not active:
        return INACTIVE_COMPONENT
    if not has_rule:
        return NO_RULE
    return None


@dataclass(frozen=True)
class Part:
    """One component of the plan: a room type or a service on a day."""

    day_no: int
    room_type_id: str | None
    service_id: str | None
    max_occupancy: int | None = None

    @property
    def item(self) -> str:
        return self.room_type_id or self.service_id or ""

    @property
    def offset(self) -> int:
        return self.day_no - 1


@dataclass(frozen=True)
class Span:
    item: str
    start: date
    stop: date


@dataclass(frozen=True)
class Window:
    start: date
    end: date


@dataclass(frozen=True)
class NetRow:
    """One computed net cost per person (before pricing)."""

    valid_from: date
    valid_to: date
    pax_min: int
    pax_max: int | None
    net_amount: Decimal
    net_currency: str
    season: str | None
    peak: bool
    # bk-047 (AA-1075, bk-15): the single supplement per person (net_currency) of the window, None without a room.
    single_net: Decimal | None = None
    # AA-1091 (bk-055): the season of the plan's calendar on the window's start dates (``peak`` follows from it).
    season_code: str | None = None


@dataclass
class Cell:
    """The cost of one window and band while it is summed."""

    costs: dict[str, Decimal] = field(default_factory=dict)
    seasons: set[str] = field(default_factory=set)
    complete: bool = True

    def add(self, amount: Decimal, currency: str, season: str | None) -> None:
        self.costs[currency] = self.costs.get(currency, Decimal(0)) + amount
        if season:
            self.seasons.add(season.strip())


def room_mix(party: int, max_occupancy: int | None) -> list[tuple[int, int]]:
    """``[(occupancy, rooms)]`` for a party: doubles plus a single when odd; singles on a one-person room type."""
    if max_occupancy is not None and max_occupancy < 2:
        return [(1, party)]
    mix = [(2, party // 2), (1, party % 2)]
    return [(pax, n) for pax, n in mix if n]


def rate_cuts(parts: Sequence[Part], spans: Sequence[Span]) -> set[date]:
    """The start dates where the rates a departure of the plan uses can change: every rate validity start and the day
    after every end, shifted back by the component's day offset. Between two cuts every departure prices the same
    rates (``app.b2b.tariff`` splits the matrix windows at them too)."""
    by_item: dict[str, list[Span]] = {}
    for span in spans:
        by_item.setdefault(span.item, []).append(span)
    cuts: set[date] = set()
    for part in parts:
        shift = timedelta(days=part.offset)
        for span in by_item.get(part.item, ()):
            cuts.add(span.start - shift)
            if span.stop < FOREVER:  # an open-ended rate has no end to cut at (and FOREVER + 1 day overflows)
                cuts.add(span.stop + timedelta(days=1) - shift)
    return cuts


def windows(
    today: date, parts: Sequence[Part], spans: Sequence[Span], calendar: seasons.Calendar = seasons.EMPTY
) -> list[Window]:
    """Start-date windows inside which every component prices the same rates, anchored on the rate validity (cut at
    every rate start and end shifted by the component's day offset, up to the end of the latest rate), without the
    ones over before ``today``. Nothing in them depends on ``today`` but which windows are over, so the same inputs
    on the next day give the same windows. AA-1091 (bk-055): also cut where the plan's season calendar changes season
    (a window has one season on its start dates)."""
    end = horizon(today, spans)
    if end is None:
        return []
    cuts = rate_cuts(parts, spans)
    if cuts:
        first = min(cuts)
        cuts |= set(calendar.cuts(first, min(end, date.max - timedelta(days=1)) + timedelta(days=1)))
    ordered = sorted(day for day in cuts if day <= end)
    if not ordered:
        return []
    out = [Window(day, nxt - timedelta(days=1)) for day, nxt in zip(ordered, ordered[1:], strict=False)]
    out.append(Window(ordered[-1], end))
    return [w for w in out if w.end >= today]


def horizon(today: date, spans: Iterable[Span]) -> date | None:
    """The last start date worth pricing: the end of the latest rate (``FOREVER`` when open-ended); None = every rate
    is over before ``today``. Never derived from ``today``, so it does not move day by day."""
    stops = [s.stop for s in spans if s.stop >= today]
    return max(stops) if stops else None


def asks(wins: Sequence[Window], parts: Sequence[Part]) -> list[RateAsk]:
    """Every rate lookup the windows x bands x components need (the batched query dedupes repeats)."""
    out: list[RateAsk] = []
    for win in wins:
        for low, _ in PAX_BANDS:
            for part in parts:
                on = win.start + timedelta(days=part.offset)
                if part.room_type_id:
                    for occupancy, _rooms in room_mix(low, part.max_occupancy):
                        out.append(RateAsk(part.room_type_id, None, on, occupancy))
                else:
                    out.append(RateAsk(None, part.service_id, on, low))
    return out


def cells(
    wins: Sequence[Window], parts: Sequence[Part], found: dict[RateAsk, dict[str, Any] | None]
) -> list[list[Cell]]:
    """``[window][band]`` cost cells from the rates found for :func:`asks`."""
    grid: list[list[Cell]] = []
    for win in wins:
        row: list[Cell] = []
        for low, _ in PAX_BANDS:
            cell = Cell()
            for part in parts:
                on = win.start + timedelta(days=part.offset)
                if part.room_type_id:
                    for occupancy, rooms in room_mix(low, part.max_occupancy):
                        rate = found.get(RateAsk(part.room_type_id, None, on, occupancy))
                        if rate is None:
                            cell.complete = False
                            continue
                        cell.add(Decimal(str(rate["net_amount"])) * rooms, rate["currency"], rate.get("season"))
                else:
                    rate = found.get(RateAsk(None, part.service_id, on, low))
                    if rate is None:
                        cell.complete = False
                        continue
                    qty = low if rate["unit"] == "person" else 1
                    cell.add(Decimal(str(rate["net_amount"])) * qty, rate["currency"], rate.get("season"))
            row.append(cell)
        grid.append(row)
    return grid


def per_person(cell: Cell, party: int, fx: dict[str, engine.FxRate]) -> tuple[Decimal, str] | None:
    """The net cost per person of a complete cell, in its one currency or in USD; None when it cannot be priced."""
    if not cell.complete or not cell.costs:
        return None
    if len(cell.costs) == 1:
        ((currency, total),) = cell.costs.items()
    else:
        try:
            total = sum((engine.to_usd(a, c, fx) for c, a in cell.costs.items()), Decimal(0))
        except engine.MissingFxRate:
            return None
        currency = engine.BASE_CURRENCY
    return (total / party).quantize(CENT, rounding=ROUND_HALF_UP), currency


def single_supplement(
    win: Window, parts: Sequence[Part], found: dict[RateAsk, dict[str, Any] | None], fx: dict[str, engine.FxRate]
) -> tuple[Decimal, str] | None:
    """AA-1075 (bk-15): the net single supplement per person of a start date window: for every room component that
    holds two, the single room rate less half the double rate of its night (never below 0). None when the plan has
    no such room or a rate is missing. The asks of bands 1 and 2 already read both rates."""
    cell = Cell()
    rooms = 0
    for part in parts:
        if not part.room_type_id or (part.max_occupancy is not None and part.max_occupancy < 2):
            continue
        rooms += 1
        on = win.start + timedelta(days=part.offset)
        single = found.get(RateAsk(part.room_type_id, None, on, 1))
        double = found.get(RateAsk(part.room_type_id, None, on, 2))
        if single is None or double is None:
            return None
        cell.add(single_part(single, double, fx), engine.BASE_CURRENCY if single["currency"] != double["currency"]
                 else single["currency"], None)  # fmt: skip
    if not rooms:
        return None
    return per_person(cell, 1, fx)


def single_part(single: dict[str, Any], double: dict[str, Any], fx: dict[str, engine.FxRate]) -> Decimal:
    """single - double / 2 of one night, in the rates' currency (USD when the two differ), never below 0."""
    one, two = Decimal(str(single["net_amount"])), Decimal(str(double["net_amount"]))
    if single["currency"] != double["currency"]:
        one, two = engine.to_usd(one, single["currency"], fx), engine.to_usd(two, double["currency"], fx)
    return max(one - two / 2, Decimal(0))


def in_currency(amount: Decimal, currency: str, target: str, fx: dict[str, engine.FxRate]) -> Decimal | None:
    """``amount`` of ``currency`` in ``target`` at the plain rates (None without a rate), to the cent."""
    if currency == target:
        return amount
    try:
        usd = engine.to_usd(amount, currency, fx)
        out = engine.from_usd(usd, fx.get(target), buffer=False) if target != engine.BASE_CURRENCY else usd
    except engine.MissingFxRate:
        return None
    if target != engine.BASE_CURRENCY and target not in fx:
        return None
    return out.quantize(CENT, rounding=ROUND_HALF_UP)


def season_label(cell: Cell) -> str | None:
    """The supplier rates' season labels of a cell (a label for staff; the season itself comes from the calendar)."""
    return " / ".join(sorted(cell.seasons))[:SEASON_MAX] or None


def label_peak(cell: Cell) -> bool:
    """The old rule (before bk-055), kept while the plan's calendar is not configured: a rate label says "peak"."""
    return any(seasons.LEGACY_PEAK in s.lower() for s in cell.seasons)


def net_rows(
    wins: Sequence[Window],
    grid: Sequence[Sequence[Cell]],
    fx: dict[str, engine.FxRate],
    singles: Sequence[tuple[Decimal, str] | None] | None = None,
    codes: Sequence[str] | None = None,
) -> tuple[list[NetRow], int]:
    """Merge the windows whose nets, season and single supplement match; returns the rows and the count of unpriced
    cells. ``singles`` (AA-1075): the single supplement of each window (:func:`single_supplement`). ``codes``
    (AA-1091, bk-055): the calendar season of each window's start dates (:mod:`app.pricing.seasons`); ``peak`` =
    a peak or festive season (the rule's peak uplift). Without codes (the plan's calendar is not configured) the old
    rule: a window is ``peak`` when a 2-traveller rate's season label says "peak", else ``low``."""
    unpriced = 0
    keyed: list[tuple[Window, tuple[Any, ...], str | None, str, Any]] = []
    two = next(i for i, (low, _) in enumerate(PAX_BANDS) if low == 2)
    for i, (win, row) in enumerate(zip(wins, grid, strict=True)):
        nets = []
        for (low, _), cell in zip(PAX_BANDS, row, strict=True):
            value = per_person(cell, low, fx)
            unpriced += value is None
            nets.append(value)
        code = codes[i] if codes else seasons.legacy_code(label_peak(row[two]))
        keyed.append((win, tuple(nets), season_label(row[two]), code, singles[i] if singles else None))
    merged: list[tuple[Window, tuple[Any, ...], str | None, str, Any]] = []
    for win, nets, season, code, single in keyed:
        if (
            merged
            and merged[-1][1:] == (nets, season, code, single)
            and merged[-1][0].end + timedelta(days=1) == win.start
        ):
            merged[-1] = (Window(merged[-1][0].start, win.end), nets, season, code, single)
        else:
            merged.append((win, nets, season, code, single))
    out: list[NetRow] = []
    for win, nets, season, code, single in merged:
        for (low, high), value in zip(PAX_BANDS, nets, strict=True):
            if value is not None:
                single_net = in_currency(single[0], single[1], value[1], fx) if single else None
                out.append(
                    NetRow(
                        win.start, win.end, low, high, value[0], value[1], season, code in seasons.PEAK_CODES,
                        single_net, code,
                    )
                )  # fmt: skip
    return out, unpriced


def overlaps(row: dict[str, Any], other: dict[str, Any]) -> bool:
    if (row["channel"], row["currency"].strip()) != (other["channel"], other["currency"].strip()):
        return False
    dates = row["valid_from"] <= other["valid_to"] and other["valid_from"] <= row["valid_to"]
    top, other_top = row["pax_max"] or 999, other["pax_max"] or 999
    return dates and row["pax_min"] <= other_top and other["pax_min"] <= top


# ── Database part ────────────────────────────────────────────────────────────


@dataclass
class Pricing:
    """The rules and FX rates in force for one recompute day (read once per run)."""

    on: date
    rules: dict[engine.Channel, repository.DatedRule]
    fx: dict[str, engine.FxRate]

    @property
    def markup(self) -> Decimal | None:
        rule = self.rules.get(engine.Channel.B2B)
        return rule.rrp_markup_pct if rule else None


async def load_pricing(conn: asyncpg.Connection, on: date) -> Pricing:
    rules: dict[engine.Channel, repository.DatedRule] = {}
    for channel in CHANNELS:
        dated = await repository.rule_in_force(conn, channel, on)
        if dated is not None:
            rules[channel] = dated
    codes = await conn.fetch("SELECT DISTINCT currency::text AS c FROM booking.fx_rates")
    dated_fx = await repository.fx_rates_in_force(conn, [r["c"].strip() for r in codes], on)
    return Pricing(on=on, rules=rules, fx={c: d.rate for c, d in dated_fx.items()})


def _dec(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def kept_row(row: dict[str, Any], pricing: Pricing) -> dict[str, Any]:
    """A hand row as the matrix holds it, typed for the insert, with its RRP under today's markup."""
    out = {
        **row,
        "currency": row["currency"].strip(),
        "net_currency": (row["net_currency"] or "").strip() or None,
        "amount": _dec(row["amount"]),
        "net_amount": _dec(row["net_amount"]),
        "effective_margin": _dec(row["effective_margin"]),
        "computed": bool(row["computed"]),
        "overridden": bool(row["overridden"]),
    }
    assert out["amount"] is not None  # noqa: S101 — amount is NOT NULL
    out["rrp_amount"] = prices.rrp_for(out["channel"], out["amount"], out["currency"], pricing.markup)
    return out


def priced_rows(nets: Sequence[NetRow], pricing: Pricing) -> tuple[list[dict[str, Any]], int]:
    """Price every net row for every channel with a rule in force; returns the rows and the count refused."""
    out: list[dict[str, Any]] = []
    refused = 0
    for channel in CHANNELS:
        dated = pricing.rules.get(channel)
        if dated is None:
            continue
        for net in nets:
            try:
                quote = engine.price_quote(
                    [engine.CostLine(net.net_amount, net.net_currency)],
                    dated.rule,
                    currency=SELLING_CURRENCY,
                    peak=net.peak,
                    rates=pricing.fx,
                )
            except engine.PricingError:
                refused += 1
                continue
            if quote.final_total <= 0 or quote.final_total >= prices.MAX_AMOUNT:
                refused += 1
                continue
            extra = pax_type_prices(net, dated, pricing)
            out.append(
                {
                    "channel": channel.value,
                    "season": net.season,
                    "peak": net.peak,
                    "season_code": net.season_code,
                    "valid_from": net.valid_from,
                    "valid_to": net.valid_to,
                    "pax_min": net.pax_min,
                    "pax_max": net.pax_max,
                    "currency": SELLING_CURRENCY,
                    "amount": quote.final_total,
                    "net_amount": net.net_amount,
                    "net_currency": net.net_currency,
                    "effective_margin": quote.effective_margin,
                    "priced_on": pricing.on,
                    "overridden": False,
                    "computed": True,
                    "rrp_amount": prices.rrp_for(channel.value, quote.final_total, SELLING_CURRENCY, pricing.markup),
                    "created_by": None,
                    **extra,
                }
            )
    return out, refused


def _sell(net: Decimal | None, row: NetRow, rule: repository.DatedRule, pricing: Pricing) -> Decimal | None:
    """One per-person net priced by the channel's rule like the adult price (None without a net or on an error)."""
    if net is None:
        return None
    if net == 0:
        return Decimal("0.00")
    try:
        quote = engine.price_quote(
            [engine.CostLine(net, row.net_currency)], rule.rule, currency=SELLING_CURRENCY, peak=row.peak,
            rates=pricing.fx,
        )  # fmt: skip
    except engine.PricingError:
        return None
    return quote.final_total if 0 < quote.final_total < prices.MAX_AMOUNT else None


def pax_type_prices(row: NetRow, rule: repository.DatedRule, pricing: Pricing) -> dict[str, Decimal | None]:
    """AA-1075 (bk-15): the child, infant and single supplement nets and prices of one computed row: child = the adult
    net x the rule's ``child_pct``; infant = the rule's ``infant_flat_usd`` in the row's net currency; single = the
    window's single supplement (:func:`single_supplement`). Each priced by the same rule; None when not set."""
    child_net = (
        (row.net_amount * rule.child_pct).quantize(CENT, rounding=ROUND_HALF_UP) if rule.child_pct is not None else None
    )
    infant_net = (
        in_currency(rule.infant_flat_usd, engine.BASE_CURRENCY, row.net_currency, pricing.fx)
        if rule.infant_flat_usd is not None
        else None
    )
    return {
        "child_net_amount": child_net,
        "child_amount": _sell(child_net, row, rule, pricing),
        "infant_net_amount": infant_net,
        "infant_amount": _sell(infant_net, row, rule, pricing),
        "single_supplement_net_amount": row.single_net,
        "single_supplement_amount": _sell(row.single_net, row, rule, pricing),
    }


def fit(row: dict[str, Any], kept: Sequence[dict[str, Any]], today: date) -> dict[str, Any] | None:
    """The computed row as it fits beside the hand rows: started the day after each hand row it overlaps that is over
    before ``today`` (the computed windows are anchored on the rate validity and may start in the past), or None when
    a hand row still in force or ahead overlaps it (the hand row wins)."""
    out = row
    for hand in sorted(kept, key=lambda k: k["valid_to"]):
        if not overlaps(out, hand):
            continue
        if hand["valid_to"] < today and hand["valid_to"] < out["valid_to"]:
            out = {**out, "valid_from": hand["valid_to"] + timedelta(days=1)}
            continue
        return None
    return out


def assemble(
    existing: Sequence[dict[str, Any]], computed: Sequence[dict[str, Any]], pricing: Pricing
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Hand rows first (kept), then the computed rows that fit; positions 1..n. Returns the set and counters."""
    kept = [kept_row(r, pricing) for r in existing if not r["computed"] or r["overridden"]]
    fitted = [(r, fit(r, kept, pricing.on)) for r in computed]
    fitting = [f for _, f in fitted if f is not None]
    trimmed = sum(1 for r, f in fitted if f is not None and f is not r)
    room = max(prices.MAX_ROWS - len(kept), 0)
    fitting.sort(key=lambda r: (r["valid_from"], r["channel"], r["pax_min"]))
    truncated = max(len(fitting) - room, 0)
    chosen = fitting[:room]
    chosen.sort(key=lambda r: (r["channel"], r["valid_from"], r["pax_min"]))
    final = [*kept, *chosen]
    for i, row in enumerate(final, start=1):
        row["position"] = i
    return final, {
        "kept": len(kept),
        "computed": len(chosen),
        "shadowed": len(computed) - len(fitting),
        "trimmed": trimmed,
        "truncated": truncated,
    }


async def _parts(conn: asyncpg.Connection, plan_id: str) -> tuple[list[Part], bool]:
    rows = await conn.fetch(COMPONENTS_SQL, plan_id)
    active = all(component_active(r) for r in rows)
    parts = [Part(int(r["day_no"]), r["room_type_id"], r["service_id"], r["max_occupancy"]) for r in rows]
    return parts, active


async def compute(
    conn: asyncpg.Connection, plan_id: str, pricing: Pricing, calendar: seasons.Calendar | None = None
) -> tuple[list[dict[str, Any]], dict]:
    """The computed rows of one plan and why some are missing (no DB write)."""
    info: dict[str, Any] = {"windows": 0, "unpriced_cells": 0, "refused": 0, "reason": None}
    parts, active = await _parts(conn, plan_id)
    if reason := blocking_reason(len(parts), active, bool(pricing.rules)):
        info["reason"] = reason
        return [], info
    rooms = sorted({p.room_type_id for p in parts if p.room_type_id})
    services = sorted({p.service_id for p in parts if p.service_id})
    spans = [
        Span(r["room_type_id"] or r["service_id"], r["start"], r["stop"])
        for r in await conn.fetch(SPANS_SQL, rooms, services)
    ]
    cal = calendar if calendar is not None else await seasons.for_plan(conn, plan_id)
    wins = windows(pricing.on, parts, spans, cal)[:MAX_SEGMENTS]
    if not wins:
        info["reason"] = NO_RATES
        return [], info
    info["windows"] = len(wins)
    wanted = asks(wins, parts)
    found = dict(zip(wanted, await rates_in_force_many(conn, wanted), strict=True))
    singles = [single_supplement(w, parts, found, pricing.fx) for w in wins]
    codes = [cal.season_on(w.start).code for w in wins] if cal.configured else None
    nets, info["unpriced_cells"] = net_rows(wins, cells(wins, parts, found), pricing.fx, singles, codes)
    rows, info["refused"] = priced_rows(nets, pricing)
    if not rows:
        info["reason"] = NO_RATES
    return rows, info


async def recompute_plan(
    conn: asyncpg.Connection,
    plan_id: str,
    pricing: Pricing,
    *,
    actor_type: str = "system",
    actor_ref: str = ACTOR,
    action: str = ACTION,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Recompute and store one plan's matrix (call inside a transaction): the hand rows of its published price list
    versions as they win today (AA-1091, :mod:`app.catalog.version_matrix`), then the computed rows around them. A
    publish or withdrawal of a version stores through here too, under its staff actor and action."""
    plan = await conn.fetchrow(LOCK_SQL, plan_id)
    if plan is None:
        return {"outcome": "not_found"}
    cal = await seasons.for_plan(conn, plan_id)
    computed, info = await compute(conn, plan_id, pricing, cal)
    existing = [dict(r) for r in await conn.fetch(prices.ROWS_SQL, plan_id)]
    hand = await version_matrix.hand_rows(conn, plan_id, pricing.on, existing, cal)
    final, counts = assemble(hand, computed, pricing)
    changed = await prices.store(
        conn,
        plan_id,
        plan["source_id"],
        final,
        actor_type=actor_type,
        actor_ref=actor_ref,
        created_by=None,
        action=action,
        extra={"computed_rows": counts["computed"], "reason": info["reason"], **(extra or {})},
        summary_suffix=" (cost sheet)" if actor_type == "system" else " (price version)",
    )
    return {"outcome": "changed" if changed else "unchanged", **counts, **info}


def today() -> date:
    return datetime.now(UTC).date()


async def run(pool: asyncpg.Pool, plan_id: str | None = None, *, on: date | None = None) -> dict[str, Any]:
    """Recompute every plan with a component, a computed row or a b2b row (or one plan). One transaction per plan; a
    plan that fails is counted and the run raises at the end so the job is retried (the others are stored)."""
    day = on or today()
    async with pool.acquire() as conn:
        pricing = await load_pricing(conn, day)
        plans = await conn.fetch(PLANS_SQL, plan_id)
    totals: dict[str, int] = {"plans": len(plans), "changed": 0, "unchanged": 0, "failed": 0, "computed_rows": 0}
    reasons: dict[str, int] = {}
    errors: list[str] = []
    for plan in plans:
        try:
            async with pool.acquire() as conn, conn.transaction():
                out = await recompute_plan(conn, plan["id"], pricing)
        except Exception as exc:  # noqa: BLE001 — one bad plan must not stop the others; raised below
            totals["failed"] += 1
            errors.append(f"{plan['source_id']}: {type(exc).__name__}")
            log.warning("bk_price_recompute plan %s failed: %s", plan["source_id"], type(exc).__name__)
            continue
        totals[out["outcome"]] = totals.get(out["outcome"], 0) + 1
        totals["computed_rows"] += int(out.get("computed", 0))
        if out.get("reason"):
            reasons[out["reason"]] = reasons.get(out["reason"], 0) + 1
    result = {
        "outcome": "ok",
        "priced_on": day.isoformat(),
        "rules": sorted(c.value for c in pricing.rules),
        "rrp_markup": None if pricing.markup is None else str(pricing.markup),
        **totals,
        "reasons": reasons,
    }
    if errors:
        raise RuntimeError(f"{len(errors)} plan(s) failed: {'; '.join(errors[:10])}")
    if totals["changed"]:
        log.info("bk_price_recompute: %s", result)
    return result


async def handle(pool: asyncpg.Pool, job: Job) -> dict[str, Any]:
    plan_id = (job.payload or {}).get("plan_id")
    return await run(pool, str(plan_id) if plan_id else None)


HANDLERS = {KIND: handle}


class RecomputeSchedule:
    """Worker maintenance hook: one ``bk_price_recompute`` run per new watermark of its inputs (see the module doc);
    the idempotency key is the watermark's hash, so a watermark is run once whatever the number of workers."""

    def __init__(self, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._now = now
        self._done: str | None = None

    async def __call__(self, pool: asyncpg.Pool) -> str | None:
        async with pool.acquire() as conn:
            mark = await conn.fetchval(WATERMARK_SQL)
        key = f"{KIND}:wm:{hashlib.sha256(str(mark).encode()).hexdigest()[:32]}"
        if key == self._done:
            return None
        job_id = await queue.enqueue(
            pool, KIND, {"trigger": "watermark"}, idempotency_key=key, max_attempts=MAX_ATTEMPTS, created_by=SCHEDULER
        )
        self._done = key
        return job_id
