"""Price readiness of the catalog (AA-1029): which trip plans price, and why the others do not, so that when real
supplier data arrives staff see at once what is still missing. A read only; no LLM, no write.

**Per plan** (every ``booking.trip_plans`` row of the CIS catalog, about 892 on Dev): the tour key (``source_id``),
the CIS title, country and days (``catalog.v_tour``; a plan whose tour left the catalog is listed without them), the
commercial ``sellable`` flag, its components, the components **without a published rate** in force on any day from
today to :data:`HORIZON_DAYS` days ahead (each with its ``HTL-`` / ``SVC-`` code, room type and supplier code, never a
net), the cost currencies without an approved FX rate in force today, the computed and hand rows of its price matrix
still valid, and the from price (``booking.v_from_prices``: the b2b RRP for 2).

**Status**, one per plan, in this order:

1. ``priced``: the matrix has computed rows (the cost sheet's, not overridden) still valid;
2. ``hand_priced``: else hand rows (typed by staff, or computed rows staff overrode) still valid; a hand row that is
   over counts for nothing (``hand_rows_expired``);
3. else the cost sheet's own reason (``app.catalog.cost_sheet``, the same words and order, so this report and the
   ``bk_price_recompute`` job cannot disagree): ``no_components``, ``inactive_component``, ``no_rule`` (no approved
   b2b or fixed-departure rule in force today), then the cost sheet's ``no_rates`` split in two: ``missing_rates`` (a
   component has no published rate in the horizon) and ``missing_fx`` (every rate of a component is in a currency
   without an approved FX rate today);
4. a plan with none of these reasons and no computed row yet is checked with the cost sheet itself
   (:func:`app.catalog.cost_sheet.compute`, no write; at most :data:`MAX_PROBES` such plans a read): rows =
   ``priced`` with ``pending_recompute`` (the job stores them on its next run), none = ``missing_rates`` with a note
   (the rates do not cover one common start date or pax band).

``reason`` is the cost sheet's reason today whatever the status (None = nothing stops the cost sheet), so a priced plan
whose rule just ended shows it before the next recompute removes its rows.

**Bounded**: one set-based query for every plan (counts and the from price joined per plan, no per-plan query), then
the components of the plans that have some and their items' rate spans in batches of :data:`BATCH`; no net is read.
The CIS catalog join fails soft: without it the plans are listed without title, country and days (``degraded``).

**Rate-sheet impact** (:func:`impact`): for a rate sheet's dry run, the plans whose components use the sheet's room
types or services, whether each would have a rate for every component once this draft is published, its status after,
and how many plans would move to priced. The draft itself is never written by this read.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import asyncpg

from app.b2b.tariff import FORMULA_START
from app.catalog import cost_sheet
from app.catalog.destinations import CATALOG_ERRORS
from app.pricing import engine

SOURCE_SYSTEM = "cis"
HORIZON_DAYS = 365
MAX_PROBES = 50
BATCH = 500
MAX_IMPACT_PLANS = 500
PRICED = "priced"
HAND_PRICED = "hand_priced"
MISSING_RATES = "missing_rates"
MISSING_FX = "missing_fx"
STATUSES: tuple[str, ...] = (
    PRICED,
    HAND_PRICED,
    MISSING_RATES,
    MISSING_FX,
    cost_sheet.NO_RULE,
    cost_sheet.NO_COMPONENTS,
    cost_sheet.INACTIVE_COMPONENT,
)
NO_COUNTRY = "(no country)"
CSV_COLUMNS: tuple[str, ...] = (
    "tour_key",
    "title",
    "country",
    "duration_days",
    "sellable",
    "status",
    "reason",
    "components",
    "components_without_rate",
    "missing_fx",
    "computed_rows",
    "hand_rows",
    "from_price",
    "from_currency",
    "note",
)
NOTE_NO_COMMON_WINDOW = "every component has a rate, but the rates do not cover one common start date and pax band"
NOTE_NOT_CHECKED = "every component has a rate; not checked against the cost sheet in this read (too many such plans)"
NOTE_PENDING = "the cost sheet prices it; the next recompute stores the rows"

_COUNTS = """
LEFT JOIN (SELECT c.trip_plan_id, count(*) AS n FROM booking.trip_plan_components c GROUP BY c.trip_plan_id) c
       ON c.trip_plan_id = p.id
LEFT JOIN (SELECT r.trip_plan_id,
                  count(*) FILTER (WHERE r.computed AND NOT r.overridden AND r.valid_to >= $1) AS computed_rows,
                  count(*) FILTER (WHERE (NOT r.computed OR r.overridden) AND r.valid_to >= $1) AS hand_rows,
                  count(*) FILTER (WHERE (NOT r.computed OR r.overridden) AND r.valid_to < $1) AS hand_rows_expired
           FROM booking.trip_plan_prices r GROUP BY r.trip_plan_id) r ON r.trip_plan_id = p.id
LEFT JOIN booking.v_from_prices fp ON fp.source_system = p.source_system AND fp.source_id = p.source_id
WHERE p.source_system = 'cis' AND ($2::uuid[] IS NULL OR p.id = ANY($2::uuid[]))
ORDER BY p.source_id
"""
_PLAN_COLUMNS = """
p.id::text AS plan_id, p.source_id, p.sellable, coalesce(c.n, 0) AS components,
coalesce(r.computed_rows, 0) AS computed_rows, coalesce(r.hand_rows, 0) AS hand_rows,
coalesce(r.hand_rows_expired, 0) AS hand_rows_expired,
fp.amount::text AS from_amount, fp.currency AS from_currency, fp.season AS from_season
"""
# Every plan with its counts and from price, joined to the CIS catalog for the title, country and days.
PLANS_SQL = f"""
SELECT {_PLAN_COLUMNS}, t.title, t.country, t.duration_days, (t.source_id IS NOT NULL) AS in_catalog
FROM booking.trip_plans p
LEFT JOIN catalog.v_tour t ON t.source_system = p.source_system AND t.source_id = p.source_id
{_COUNTS}"""  # noqa: S608 — constants only, no input
# The same without the catalog (the catalog view unreadable: listed without title, country and days).
PLANS_NO_CATALOG_SQL = f"""
SELECT {_PLAN_COLUMNS}, NULL::text AS title, NULL::text AS country, NULL::int AS duration_days,
       NULL::boolean AS in_catalog
FROM booking.trip_plans p
{_COUNTS}"""  # noqa: S608 — constants only, no input
# The components of a batch of plans with the codes staff know them by (never a rate or a net).
COMPONENTS_SQL = """
SELECT c.trip_plan_id::text AS plan_id, c.day_no, c.hotel_room_type_id::text AS room_type_id,
       c.service_id::text AS service_id, coalesce(rt.status, s.status) AS status,
       coalesce(h.status, 'active') AS hotel_status, h.code AS hotel_code, rt.name AS room_type,
       s.code AS service_code, su.code AS supplier_code
FROM booking.trip_plan_components c
LEFT JOIN booking.hotel_room_types rt ON rt.id = c.hotel_room_type_id
LEFT JOIN booking.hotels h ON h.id = rt.hotel_id
LEFT JOIN booking.services s ON s.id = c.service_id
LEFT JOIN booking.suppliers su ON su.id = coalesce(h.supplier_id, s.supplier_id)
WHERE c.trip_plan_id = ANY($1::uuid[])
ORDER BY c.trip_plan_id, c.day_no, coalesce(c.hotel_room_type_id, c.service_id)
"""
# The plans whose components use one of these room types or services (a rate sheet's impact).
USING_PLANS_SQL = """
SELECT DISTINCT c.trip_plan_id::text AS plan_id FROM booking.trip_plan_components c
WHERE c.hotel_room_type_id = ANY($1::uuid[]) OR c.service_id = ANY($2::uuid[])
"""


# ── Pure part ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Component:
    day_no: int
    room_type_id: str | None
    service_id: str | None
    active: bool
    hotel_code: str | None = None
    room_type: str | None = None
    service_code: str | None = None
    supplier_code: str | None = None

    @property
    def item(self) -> str:
        return self.room_type_id or self.service_id or ""

    def out(self) -> dict[str, Any]:
        if self.room_type_id:
            return {
                "day_no": self.day_no,
                "kind": "hotel",
                "code": self.hotel_code,
                "room_type": self.room_type,
                "supplier_code": self.supplier_code,
            }
        return {
            "day_no": self.day_no,
            "kind": "service",
            "code": self.service_code,
            "room_type": None,
            "supplier_code": self.supplier_code,
        }


@dataclass
class Plan:
    """What the report knows of one plan before it is classified."""

    plan_id: str
    source_id: str
    sellable: bool
    title: str | None = None
    country: str | None = None
    duration_days: int | None = None
    in_catalog: bool | None = None
    components: list[Component] = field(default_factory=list)
    computed_rows: int = 0
    hand_rows: int = 0
    hand_rows_expired: int = 0
    from_price: dict[str, Any] | None = None


@dataclass(frozen=True)
class Verdict:
    status: str | None  # None = the cost sheet must decide (see :func:`resolve`)
    reason: str | None
    unrated: tuple[Component, ...]
    missing_fx: tuple[str, ...]


def horizon(on: date) -> date:
    return on + timedelta(days=HORIZON_DAYS)


def rated_items(spans: Iterable[Mapping[str, Any]], on: date) -> dict[str, set[str]]:
    """``{item id: currencies}`` of the published rate spans (``cost_sheet.SPANS_SQL`` rows) that cover a day from
    ``on`` to the horizon."""
    end = horizon(on)
    out: dict[str, set[str]] = {}
    for span in spans:
        if span["start"] <= end and span["stop"] >= on:
            item = span["room_type_id"] or span["service_id"]
            out.setdefault(item, set()).add(str(span["currency"]).strip())
    return out


def fx_ready(currencies: Iterable[str], fx: Iterable[str]) -> bool:
    """A component's rates can be priced in USD when one of their currencies is USD or has an approved FX rate."""
    have = set(fx)
    return any(c == engine.BASE_CURRENCY or c in have for c in currencies)


def blocker(
    plan: Plan, rated: Mapping[str, set[str]], *, has_rule: bool, fx: Iterable[str]
) -> tuple[str | None, tuple[Component, ...], tuple[str, ...]]:
    """The cost sheet's reason for the plan today (None = nothing stops it), the components without a rate in the
    horizon and the cost currencies without an approved FX rate."""
    fx = set(fx)
    active = all(c.active for c in plan.components)
    unrated = tuple(c for c in plan.components if c.item not in rated)
    unready = [c for c in plan.components if c.item in rated and not fx_ready(rated[c.item], fx)]
    missing_fx = tuple(sorted({cur for c in unready for cur in rated[c.item]} - {engine.BASE_CURRENCY}))
    reason = cost_sheet.blocking_reason(len(plan.components), active, has_rule)
    if reason is None and unrated:
        reason = MISSING_RATES
    if reason is None and unready:
        reason = MISSING_FX
    return reason, unrated, missing_fx


def classify(plan: Plan, rated: Mapping[str, set[str]], *, has_rule: bool, fx: Iterable[str]) -> Verdict:
    """The plan's status (see the module doc); ``status`` None = no reason and no computed row yet: the cost sheet
    decides (:func:`resolve`)."""
    reason, unrated, missing_fx = blocker(plan, rated, has_rule=has_rule, fx=fx)
    if plan.computed_rows:
        status: str | None = PRICED
    elif plan.hand_rows:
        status = HAND_PRICED
    else:
        status = reason
    return Verdict(status, reason, unrated, missing_fx)


def summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Counts per status and per country (every country with a count per status)."""
    by_status = dict.fromkeys(STATUSES, 0)
    by_country: dict[str, dict[str, int]] = {}
    for row in rows:
        by_status[row["status"]] += 1
        country = by_country.setdefault(row["country"] or NO_COUNTRY, {"total": 0, **dict.fromkeys(STATUSES, 0)})
        country["total"] += 1
        country[row["status"]] += 1
    countries = [{"country": name, **counts} for name, counts in sorted(by_country.items())]
    return {"total": len(rows), "by_status": by_status, "by_country": countries}


def matches(row: Mapping[str, Any], *, status: str | None, country: str | None, q: str | None) -> bool:
    if status and row["status"] != status:
        return False
    if country and (row["country"] or NO_COUNTRY).casefold() != country.strip().casefold():
        return False
    if q:
        needle = q.strip().casefold()
        if needle and needle not in (row["title"] or "").casefold() and needle not in row["tour_key"].casefold():
            return False
    return True


def _cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return f"'{text}" if text.startswith(FORMULA_START) else text


def _component_text(c: Mapping[str, Any]) -> str:
    name = f"{c['code']} {c['room_type']}" if c["room_type"] else str(c["code"])
    return f"day {c['day_no']} {name} ({c['supplier_code']})"


def to_csv(report: Mapping[str, Any]) -> str:
    """One line per listed plan, columns :data:`CSV_COLUMNS`: no net, no margin (the from price is the public RRP)."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(CSV_COLUMNS)
    for item in report["items"]:
        price = item["from_price"]
        writer.writerow(
            _cell(v)
            for v in (
                item["tour_key"],
                item["title"],
                item["country"],
                item["duration_days"],
                "yes" if item["sellable"] else "no",
                item["status"],
                item["reason"],
                item["components"],
                "; ".join(_component_text(c) for c in item["components_without_rate"]),
                " ".join(item["missing_fx"]),
                item["computed_rows"],
                item["hand_rows"],
                price["amount"] if price else None,
                price["currency"] if price else None,
                item["note"],
            )
        )
    return out.getvalue()


def filename(on: date) -> str:
    return f"aa-price-readiness-{on.isoformat()}.csv"


# ── Database part ────────────────────────────────────────────────────────────


@dataclass
class Facts:
    """Everything the report reads, once per request."""

    on: date
    pricing: cost_sheet.Pricing
    plans: list[Plan]
    rated: dict[str, set[str]]
    degraded: list[str]


def _batches(values: Sequence[str]) -> list[Sequence[str]]:
    return [values[i : i + BATCH] for i in range(0, len(values), BATCH)]


async def _plan_rows(conn: asyncpg.Connection, on: date, plan_ids: Sequence[str] | None) -> tuple[list[Any], bool]:
    """The plan rows with the catalog join; without it when the catalog view cannot be read (``True`` = degraded).
    The catalog read runs in its own savepoint, so a failure inside a caller's transaction leaves it usable."""
    ids = list(plan_ids) if plan_ids is not None else None
    try:
        async with conn.transaction():
            return await conn.fetch(PLANS_SQL, on, ids), False
    except CATALOG_ERRORS:
        return await conn.fetch(PLANS_NO_CATALOG_SQL, on, ids), True


async def load(conn: asyncpg.Connection, on: date, plan_ids: Sequence[str] | None = None) -> Facts:
    """Read every plan (or the given plans): one plan query, then components and rate spans in batches."""
    pricing = await cost_sheet.load_pricing(conn, on)
    rows, degraded = await _plan_rows(conn, on, plan_ids)
    plans: dict[str, Plan] = {}
    for r in rows:
        if r["plan_id"] in plans:  # a catalog key listed twice by the view: the plan once
            continue
        price = None
        if r["from_amount"] is not None:
            currency = str(r["from_currency"]).strip()
            price = {"amount": r["from_amount"], "currency": currency, "season": r["from_season"]}
        plans[r["plan_id"]] = Plan(
            plan_id=r["plan_id"],
            source_id=r["source_id"],
            sellable=bool(r["sellable"]),
            title=r["title"],
            country=r["country"],
            duration_days=r["duration_days"],
            in_catalog=r["in_catalog"],
            computed_rows=int(r["computed_rows"]),
            hand_rows=int(r["hand_rows"]),
            hand_rows_expired=int(r["hand_rows_expired"]),
            from_price=price,
        )
    with_parts = [r["plan_id"] for r in rows if r["components"]]
    rooms: set[str] = set()
    services: set[str] = set()
    for batch in _batches(list(dict.fromkeys(with_parts))):
        for c in await conn.fetch(COMPONENTS_SQL, list(batch)):
            plans[c["plan_id"]].components.append(
                Component(
                    day_no=int(c["day_no"]),
                    room_type_id=c["room_type_id"],
                    service_id=c["service_id"],
                    active=cost_sheet.component_active(c),
                    hotel_code=c["hotel_code"],
                    room_type=c["room_type"],
                    service_code=c["service_code"],
                    supplier_code=c["supplier_code"],
                )
            )
            if c["room_type_id"]:
                rooms.add(c["room_type_id"])
            else:
                services.add(c["service_id"])
    rated: dict[str, set[str]] = {}
    room_list, service_list = sorted(rooms), sorted(services)
    for start in range(0, max(len(room_list), len(service_list)), BATCH):
        spans = await conn.fetch(
            cost_sheet.SPANS_SQL, room_list[start : start + BATCH], service_list[start : start + BATCH]
        )
        for item, currencies in rated_items(spans, on).items():
            rated.setdefault(item, set()).update(currencies)
    return Facts(on, pricing, list(plans.values()), rated, ["catalog"] if degraded else [])


def _item(plan: Plan, verdict: Verdict, *, pending: bool = False, note: str | None = None) -> dict[str, Any]:
    return {
        "tour_key": plan.source_id,
        "title": plan.title,
        "country": plan.country,
        "duration_days": plan.duration_days,
        "in_catalog": plan.in_catalog,
        "sellable": plan.sellable,
        "status": verdict.status,
        "reason": verdict.reason,
        "components": len(plan.components),
        "components_without_rate": [c.out() for c in verdict.unrated],
        "missing_fx": list(verdict.missing_fx),
        "computed_rows": plan.computed_rows,
        "hand_rows": plan.hand_rows,
        "hand_rows_expired": plan.hand_rows_expired,
        "from_price": plan.from_price,
        "pending_recompute": pending,
        "note": note,
    }


async def resolve(conn: asyncpg.Connection, facts: Facts, rated: Mapping[str, set[str]]) -> list[dict[str, Any]]:
    """Classify every plan; a plan only the cost sheet can decide is probed with it (at most :data:`MAX_PROBES`)."""
    has_rule = bool(facts.pricing.rules)
    probes = 0
    out: list[dict[str, Any]] = []
    for plan in facts.plans:
        verdict = classify(plan, rated, has_rule=has_rule, fx=facts.pricing.fx)
        if verdict.status is not None:
            out.append(_item(plan, verdict))
            continue
        if probes >= MAX_PROBES:
            out.append(_item(plan, Verdict(MISSING_RATES, None, (), ()), note=NOTE_NOT_CHECKED))
            continue
        probes += 1
        rows, info = await cost_sheet.compute(conn, plan.plan_id, facts.pricing)
        if rows:
            out.append(_item(plan, Verdict(PRICED, None, (), ()), pending=True, note=NOTE_PENDING))
        else:
            reason = MISSING_RATES if info["reason"] in (None, cost_sheet.NO_RATES) else info["reason"]
            out.append(_item(plan, Verdict(reason, reason, (), ()), note=NOTE_NO_COMMON_WINDOW))
    return out


async def build(
    conn: asyncpg.Connection,
    *,
    on: date,
    status: str | None = None,
    country: str | None = None,
    q: str | None = None,
) -> dict[str, Any]:
    """The readiness report: the summary of every plan and the plans that match the filters."""
    facts = await load(conn, on)
    rows = await resolve(conn, facts, facts.rated)
    return {
        "today": on,
        "horizon_to": horizon(on),
        "rules": sorted(c.value for c in facts.pricing.rules),
        "fx_currencies": sorted(facts.pricing.fx),
        "summary": summary(rows),
        "items": [r for r in rows if matches(r, status=status, country=country, q=q)],
        "degraded": facts.degraded,
    }


# ── Rate-sheet impact ────────────────────────────────────────────────────────


def draft_rated(contract: Mapping[str, Any], rates: Iterable[Mapping[str, Any]], on: date) -> dict[str, set[str]]:
    """``{item id: {currency}}`` of a draft's rates that would cover a day from ``on`` to the horizon once published
    (a rate's dates default to the contract's, and stay inside them, as ``create_rate`` stores them)."""
    currency = str(contract.get("currency") or "").strip()
    k_from = contract.get("valid_from")
    k_to = contract.get("valid_to") or cost_sheet.FOREVER
    end = horizon(on)
    out: dict[str, set[str]] = {}
    if k_from is None:
        return out
    for rate in rates:
        start = max(rate.get("valid_from") or k_from, k_from)
        stop = min(rate.get("valid_to") or cost_sheet.FOREVER, k_to)
        if start > end or stop < on:
            continue
        item = rate.get("hotel_room_type_id") or rate.get("service_id")
        if item and currency:
            out.setdefault(item, set()).add(currency)
    return out


async def impact(
    conn: asyncpg.Connection, contract: Mapping[str, Any], rates: Sequence[Mapping[str, Any]], *, on: date
) -> dict[str, Any]:
    """The trip plans a draft rate sheet touches: for each, whether every component would have a rate once the draft
    is published and its status after; plus how many plans would move to priced. Reads only."""
    rooms = sorted({r["hotel_room_type_id"] for r in rates if r.get("hotel_room_type_id")})
    services = sorted({r["service_id"] for r in rates if r.get("service_id")})
    empty = {"plans": [], "plans_total": 0, "would_move_to_priced": 0, "truncated": False, "horizon_to": horizon(on)}
    if not rooms and not services:
        return empty
    ids = [r["plan_id"] for r in await conn.fetch(USING_PLANS_SQL, rooms, services)]
    if not ids:
        return empty
    ids.sort()
    truncated = len(ids) > MAX_IMPACT_PLANS
    facts = await load(conn, on, ids[:MAX_IMPACT_PLANS])
    before = {r["tour_key"]: r for r in await resolve(conn, facts, facts.rated)}
    after_rated = {k: set(v) for k, v in facts.rated.items()}
    for item, currencies in draft_rated(contract, rates, on).items():
        after_rated.setdefault(item, set()).update(currencies)
    used = set(rooms) | set(services)
    has_rule = bool(facts.pricing.rules)
    plans: list[dict[str, Any]] = []
    moving = 0
    for plan in facts.plans:
        now = before[plan.source_id]
        reason, unrated, missing_fx = blocker(plan, after_rated, has_rule=has_rule, fx=facts.pricing.fx)
        would_price = reason is None
        if now["status"] == PRICED or would_price:
            status_after = PRICED
        elif plan.hand_rows:
            status_after = HAND_PRICED
        else:
            status_after = reason
        moves = would_price and now["status"] != PRICED
        moving += int(moves)
        plans.append(
            {
                "tour_key": plan.source_id,
                "title": plan.title,
                "country": plan.country,
                "components": len(plan.components),
                "components_from_sheet": sum(1 for c in plan.components if c.item in used),
                "status": now["status"],
                "all_rated_after": not unrated,
                "status_after": status_after,
                "moves_to_priced": moves,
                "components_without_rate_after": [c.out() for c in unrated],
                "missing_fx_after": list(missing_fx),
            }
        )
    plans.sort(key=lambda p: (not p["moves_to_priced"], p["title"] or "", p["tour_key"]))
    return {
        "plans": plans,
        "plans_total": len(ids),
        "would_move_to_priced": moving,
        "truncated": truncated,
        "horizon_to": horizon(on),
    }
