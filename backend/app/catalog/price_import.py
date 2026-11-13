"""Bulk price setup from one Excel workbook (AA-1091 item 3): many tours at once, with a DRY RUN first.

**Template** (:func:`template`, ``GET /booking/catalog/price-import/template``, ``pricing.read``): a ``Read me`` sheet
and four data sheets, row 1 = headers (matched by name, any order), data from row 2, empty rows skipped. Prefilled
with the current state of the plans the workbench filters select (at most :data:`MAX_TOURS`), so exporting and
importing an unchanged file changes nothing:

- ``Tours``: Tour key, Title, Country (both for reading only), Sellable (yes / no; empty = unchanged);
- ``Components``: Tour key, Day, Hotel code + Room type, or Service code (one item a row). **A tour listed here gets
  exactly these components** (the plan's set is replaced, as ``PUT …/components`` does);
- ``Prices``: Tour key, Channel (b2b or fixed_departure; empty = b2b), Season, Peak (yes / no), Valid from, Valid to,
  Pax min, Pax max, Currency (empty = USD), Price per person, Child price, Infant price, Single supplement. AA-1091
  (bk-054): **a tour listed here gets a draft price list version with exactly these hand prices** (``source =
  import`` with the file's SHA-256, :func:`app.catalog.price_versions.import_draft`); nothing prices until its maker
  submits it from the tour's Versions tab and a second person approves it (the dry run's counts say what the approved
  version would replace). The Peak column is read but no longer decides anything: the season calendar does (bk-055);
- ``Departures``: Tour key, Start date, Capacity, Min to operate, Sales cutoff days, Decision days before, Auto confirm
  (yes / no), Status (scheduled / guaranteed). A date the tour already has is skipped (``existing``), never changed.

**Checks** (every problem by sheet, row and column; **nothing is written while one is left**, as the rate sheet):
the tour key has a trip plan; codes name an **active** hotel room type (hotel code + room type name, case and spaces
ignored) or service; a day within the tour's days; no item twice on a day; at most 200 components and 200 price rows
a tour; prices of one channel and currency do not overlap (dates and pax bands); a selling currency; a departure after
today with the bk-046 limits; a tour made sellable has components after the import and is in the catalog.

**Writing** (one transaction, through the services of the single forms, so every rule and audit row is the same):
``plan_components.replace_components`` (no write when the set is unchanged), a draft price version per tour with
prices (audit ``price_version.imported``), ``departures.create_departure``, ``price_workbench.set_sellable``; one
``trip_plan.bulk_imported`` audit row for the file. Prices have maker-checker (price versions, AA-1091); components,
departures and the sellable switch have none on their single forms either. Then the cost sheet of each touched plan
with components is recomputed in the same transaction (at most :data:`MAX_RECOMPUTE`; the ``bk_price_recompute``
job does the rest), so the result shows each tour's status after.

**Dry run**: the same checks, plus per tour what would change and its price readiness status after (the readiness
rules on the new component set, the rates in force and the new hand rows) and how many cost-priced rows a new hand row
replaces (``computed_rows_replaced``, ``net_rows_replaced``). Bounded: :data:`MAX_BYTES`,
:data:`MAX_ROWS` data rows, :data:`MAX_TOURS` tours. Read with openpyxl read-only, values only (no formula, no macro).

**Export safety**: a prefilled text cell from the database (a CIS title, a room type name, a season) that starts like a
formula (:data:`app.b2b.tariff.FORMULA_START`) is written as a text cell with Excel's quote prefix, never as a formula;
its value is unchanged, so the round trip still matches the room type and the season.
"""

from __future__ import annotations

import hashlib
import io
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

import asyncpg
from openpyxl import Workbook

from app import audit
from app.auth.staff import Staff
from app.b2b import departures, plan_components
from app.b2b.tariff import FORMULA_START
from app.catalog import cost_sheet, price_readiness, price_versions, price_workbench, trip_plan_prices
from app.catalog.destinations import CATALOG_ERRORS
from app.errors import Forbidden, Invalid
from app.pricing import engine
from app.supply import rate_sheet as rs
from app.supply.rate_sheet import Problem

MAX_BYTES = rs.MAX_BYTES
MAX_ROWS = 5000
MAX_TOURS = 300
MAX_RECOMPUTE = 50
MAX_ERRORS = 200
CHANNELS = (engine.Channel.B2B.value, engine.Channel.FIXED_DEPARTURE.value)
README = "Read me"
TOURS, COMPONENTS, PRICES, DEPARTURES = "Tours", "Components", "Prices", "Departures"
COLUMNS: dict[str, tuple[str, ...]] = {
    TOURS: ("Tour key", "Title", "Country", "Sellable"),
    COMPONENTS: ("Tour key", "Day", "Hotel code", "Room type", "Service code"),
    PRICES: (
        "Tour key",
        "Channel",
        "Season",
        "Peak",
        "Valid from",
        "Valid to",
        "Pax min",
        "Pax max",
        "Currency",
        "Price per person",
        "Child price",
        "Infant price",
        "Single supplement",
    ),
    DEPARTURES: (
        "Tour key",
        "Start date",
        "Capacity",
        "Min to operate",
        "Sales cutoff days",
        "Decision days before",
        "Auto confirm",
        "Status",
    ),
}
REQUIRED: dict[str, tuple[str, ...]] = {
    TOURS: ("Tour key",),
    COMPONENTS: ("Tour key", "Day"),
    PRICES: ("Tour key", "Valid from", "Valid to", "Price per person"),
    DEPARTURES: ("Tour key", "Start date", "Capacity"),
}
YES = frozenset({"yes", "y", "true", "1", "x"})
NO = frozenset({"no", "n", "false", "0"})
README_LINES = (
    "AA price setup workbook: many tours at once.",
    "Tour key = the tour's catalog key (Price setup page, or this file prefilled). Title and Country are for reading.",
    "Tours: Sellable yes / no (empty = unchanged). A sellable tour needs its components.",
    "Components: one hotel room type (Hotel code + Room type) or one Service code per row. A tour listed here gets "
    "exactly these components.",
    "Prices: hand prices per person per pax band and season (dates YYYY-MM-DD). A tour listed here gets a draft price "
    "version with exactly these hand prices: submit it on the tour's Versions tab; it prices once a second person "
    "approves it. Prices worked out from a cost (from supplier rates) are not in this file and stay, unless a hand "
    "price covers the same dates and pax. The season and the peak uplift come from the season calendar.",
    "Departures: new fixed departures; a date the tour already has is skipped.",
    "Upload it on Price setup -> Import: Check (nothing is written) first, then Import.",
)


@dataclass
class Sheets:
    """The parsed workbook: rows per sheet as dicts (``_row`` = the sheet row), and the problems found."""

    tours: list[dict[str, Any]] = field(default_factory=list)
    components: list[dict[str, Any]] = field(default_factory=list)
    prices: list[dict[str, Any]] = field(default_factory=list)
    departures: list[dict[str, Any]] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)
    ignored_sheets: list[str] = field(default_factory=list)

    def keys(self) -> list[str]:
        out: dict[str, None] = {}
        for rows in (self.tours, self.components, self.prices, self.departures):
            for r in rows:
                if r.get("tour_key"):
                    out.setdefault(r["tour_key"], None)
        return list(out)


# ── Cells ────────────────────────────────────────────────────────────────────


def _yes_no(value: Any) -> bool | None:
    text = (rs._text(value) or "").lower()
    if not text:
        return None
    if text in YES:
        return True
    if text in NO:
        return False
    raise ValueError("yes or no is expected")


def _count(value: Any, low: int, high: int) -> int | None:
    if value is None or value == "":
        return None
    try:
        number = Decimal(str(value).strip())
    except ArithmeticError:
        raise ValueError("a whole number is expected") from None
    if number != number.to_integral_value() or not low <= number <= high:
        raise ValueError(f"a whole number from {low} to {high} is expected")
    return int(number)


def _price(value: Any) -> Decimal | None:
    amount = rs._amount(value)
    if amount is not None and amount >= trip_plan_prices.MAX_AMOUNT:
        raise ValueError(f"must be below {trip_plan_prices.MAX_AMOUNT:,}")
    return amount


def _upper(value: Any) -> str | None:
    text = rs._text(value)
    return text.upper() if text else None


def _lower(value: Any) -> str | None:
    text = rs._text(value)
    return text.lower() if text else None


CONVERTERS: dict[str, dict[str, tuple[str, Any]]] = {
    TOURS: {"Tour key": ("tour_key", rs._text), "Sellable": ("sellable", _yes_no)},
    COMPONENTS: {
        "Tour key": ("tour_key", rs._text),
        "Day": ("day_no", lambda v: _count(v, 1, 366)),
        "Hotel code": ("hotel_code", _upper),
        "Room type": ("room_type", rs._text),
        "Service code": ("service_code", _upper),
    },
    PRICES: {
        "Tour key": ("tour_key", rs._text),
        "Channel": ("channel", _lower),
        "Season": ("season", rs._text),
        "Peak": ("peak", _yes_no),
        "Valid from": ("valid_from", rs._date),
        "Valid to": ("valid_to", rs._date),
        "Pax min": ("pax_min", rs._int),
        "Pax max": ("pax_max", rs._int),
        "Currency": ("currency", _upper),
        "Price per person": ("amount", _price),
        "Child price": ("child_amount", _price),
        "Infant price": ("infant_amount", _price),
        "Single supplement": ("single_supplement_amount", _price),
    },
    DEPARTURES: {
        "Tour key": ("tour_key", rs._text),
        "Start date": ("start_date", rs._date),
        "Capacity": ("capacity", lambda v: _count(v, 0, departures.MAX_CAPACITY)),
        "Min to operate": ("min_to_operate", lambda v: _count(v, 0, departures.MAX_CAPACITY)),
        "Sales cutoff days": ("sales_cutoff_days", lambda v: _count(v, 0, departures.MAX_CUTOFF_DAYS)),
        "Decision days before": ("decision_days_before", lambda v: _count(v, 0, departures.MAX_DECISION_DAYS)),
        "Auto confirm": ("auto_confirm", _yes_no),
        "Status": ("status", _lower),
    },
}


# ── Parsing (no database) ────────────────────────────────────────────────────


def _rows(ws: Any, sheet: str, problems: list[Problem], budget: list[int]) -> list[dict[str, Any]]:
    rows = ws.iter_rows(values_only=True)
    header = next(rows, None)
    if header is None:
        return []
    columns = {rs._key(h): i for i, h in enumerate(header) if h is not None}
    missing = [c for c in REQUIRED[sheet] if rs._key(c) not in columns]
    if missing:
        problems.append(
            Problem(sheet, 1, ", ".join(missing), "missing_column", f"missing column(s): {', '.join(missing)}")
        )
        return []
    out: list[dict[str, Any]] = []
    for n, row in enumerate(rows, start=2):
        if row is None or all(v is None or str(v).strip() == "" for v in row):
            continue
        budget[0] += 1
        if budget[0] > MAX_ROWS:
            raise Invalid("price_import_too_many_rows", f"a price setup workbook has at most {MAX_ROWS} data rows")
        values: dict[str, Any] = {"_row": n}
        for label, (name, conv) in CONVERTERS[sheet].items():
            i = columns.get(rs._key(label))
            raw = row[i] if i is not None and i < len(row) else None
            try:
                values[name] = conv(raw)
            except ValueError as exc:
                problems.append(Problem(sheet, n, label, "invalid_value", str(exc)))
                values[name] = None
                values.setdefault("_bad", set()).add(name)
        for label in REQUIRED[sheet]:
            name = CONVERTERS[sheet][label][0]
            if values.get(name) is None and name not in values.get("_bad", set()):
                problems.append(Problem(sheet, n, label, "required", f"{label} is required"))
        out.append(values)
    return out


def parse(data: bytes) -> Sheets:
    """Read the workbook; shape and cell problems are collected (never raised), except a file that is not a workbook."""
    wb = rs._open(data)
    sheets = Sheets()
    budget = [0]
    target = {rs._key(TOURS): "tours", rs._key(COMPONENTS): "components", rs._key(PRICES): "prices"}
    target[rs._key(DEPARTURES)] = "departures"
    names = {rs._key(TOURS): TOURS, rs._key(COMPONENTS): COMPONENTS, rs._key(PRICES): PRICES}
    names[rs._key(DEPARTURES)] = DEPARTURES
    try:
        for name in wb.sheetnames:
            key = rs._key(name)
            if key in target:
                getattr(sheets, target[key]).extend(_rows(wb[name], names[key], sheets.problems, budget))
            elif key != rs._key(README):
                sheets.ignored_sheets.append(name)
    finally:
        wb.close()
    if not budget[0] and not any(p.code == "missing_column" for p in sheets.problems):
        sheets.problems.append(
            Problem(TOURS, None, None, "no_rows", "the workbook holds no tour, component, price or departure row")
        )
    if len(sheets.keys()) > MAX_TOURS:
        raise Invalid("price_import_too_many_tours", f"a price setup workbook covers at most {MAX_TOURS} tours")
    return sheets


def _problem(sheets: Sheets, sheet: str, row: Mapping[str, Any], column: str | None, code: str, message: str) -> None:
    sheets.problems.append(Problem(sheet, row.get("_row"), column, code, message))


def check_shapes(sheets: Sheets, today: date) -> None:
    """The checks that need no database: a component names one item, prices and departures follow the form rules,
    no duplicate item on a day and no overlapping prices within the file."""
    seen: set[tuple[str, int, str]] = set()
    for c in sheets.components:
        has_room = bool(c.get("hotel_code") or c.get("room_type"))
        if has_room == bool(c.get("service_code")):
            _problem(
                sheets,
                COMPONENTS,
                c,
                "Service code",
                "invalid_component",
                "give a Hotel code with its Room type, or a Service code",
            )
        elif has_room and not (c.get("hotel_code") and c.get("room_type")):
            _problem(
                sheets,
                COMPONENTS,
                c,
                "Room type",
                "required",
                "a hotel component needs the Hotel code and the Room type",
            )
        item = (c.get("hotel_code") or "", rs._key(c.get("room_type")), c.get("service_code") or "")
        key = (c.get("tour_key") or "", c.get("day_no") or 0, repr(item))
        if c.get("day_no") and key in seen:
            _problem(sheets, COMPONENTS, c, "Day", "duplicate_component", "the same item is listed twice on this day")
        seen.add(key)
    for p in sheets.prices:
        p["channel"] = p.get("channel") or engine.Channel.B2B.value
        p["currency"] = p.get("currency") or engine.BASE_CURRENCY
        p["pax_min"] = p.get("pax_min") or 1
        if p["channel"] not in CHANNELS:
            _problem(sheets, PRICES, p, "Channel", "invalid_value", f"the channel is one of {', '.join(CHANNELS)}")
        if p["currency"] not in engine.SELLING_CURRENCIES:
            _problem(sheets, PRICES, p, "Currency", "invalid_currency", f"{p['currency']} is not a selling currency")
        if p.get("valid_from") and p.get("valid_to") and p["valid_to"] < p["valid_from"]:
            _problem(sheets, PRICES, p, "Valid to", "invalid_validity", "Valid to is before Valid from")
        if p.get("pax_max") is not None and p["pax_max"] < p["pax_min"]:
            _problem(sheets, PRICES, p, "Pax max", "invalid_pax_band", "Pax max must be at least Pax min")
        if p.get("amount") is not None and p["amount"] <= 0:
            _problem(sheets, PRICES, p, "Price per person", "invalid_value", "a price above 0 is expected")
    by_tour: dict[str, list[dict[str, Any]]] = {}
    for p in sheets.prices:
        if p.get("tour_key") and p.get("valid_from") and p.get("valid_to") and p.get("amount"):
            by_tour.setdefault(p["tour_key"], []).append(p)
    for tour_rows in by_tour.values():
        if len(tour_rows) > trip_plan_prices.MAX_ROWS:
            _problem(
                sheets,
                PRICES,
                tour_rows[0],
                "Tour key",
                "too_many_prices",
                f"at most {trip_plan_prices.MAX_ROWS} price rows a tour",
            )
        for i, row in enumerate(tour_rows):
            for earlier in tour_rows[:i]:
                if cost_sheet.overlaps(row, earlier):
                    _problem(
                        sheets,
                        PRICES,
                        row,
                        "Valid from",
                        "overlapping_prices",
                        f"overlaps row {earlier['_row']}: same channel and currency, overlapping dates and pax bands",
                    )
                    break
    for d in sheets.departures:
        if d.get("start_date") and d["start_date"] <= today:
            _problem(sheets, DEPARTURES, d, "Start date", "invalid_start_date", "a new departure starts after today")
        status = d.get("status") or "scheduled"
        d["status"] = status
        if status not in ("scheduled", "guaranteed"):
            _problem(sheets, DEPARTURES, d, "Status", "invalid_status", "the status is scheduled or guaranteed")
        if d.get("capacity") is not None and (d.get("min_to_operate") or 0) > d["capacity"]:
            _problem(
                sheets,
                DEPARTURES,
                d,
                "Min to operate",
                "invalid_min_to_operate",
                "Min to operate is at most the capacity",
            )
    tours_seen: dict[str, int] = {}
    for t in sheets.tours:
        if t.get("tour_key") in tours_seen:
            _problem(
                sheets,
                TOURS,
                t,
                "Tour key",
                "duplicate_tour",
                f"the tour is listed twice (row {tours_seen[t['tour_key']]})",
            )
        elif t.get("tour_key"):
            tours_seen[t["tour_key"]] = t["_row"]


# ── Matching against the database ────────────────────────────────────────────

PLANS_SQL = """
SELECT p.id::text AS id, p.source_id, p.sellable FROM booking.trip_plans p
WHERE p.source_system = 'cis' AND p.source_id = ANY($1::text[])
"""
CATALOG_SQL = """
SELECT DISTINCT ON (source_id) source_id, title, country, duration_days FROM catalog.v_tour
WHERE source_system = 'cis' AND source_id = ANY($1::text[]) ORDER BY source_id
"""
ROOMS_SQL = """
SELECT upper(h.code) AS hotel_code, lower(regexp_replace(btrim(t.name), '\\s+', ' ', 'g')) AS room_type,
       t.id::text AS id, t.status, h.status AS hotel_status
FROM booking.hotel_room_types t JOIN booking.hotels h ON h.id = t.hotel_id
WHERE upper(h.code) = ANY($1::text[])
"""
SERVICES_SQL = (
    "SELECT upper(code) AS code, id::text AS id, status FROM booking.services WHERE upper(code) = ANY($1::text[])"
)
EXISTING_DEPARTURES_SQL = """
SELECT trip_plan_id::text AS plan_id, start_date FROM booking.departures WHERE trip_plan_id = ANY($1::uuid[])
"""
COMPONENT_KEYS_SQL = """
SELECT trip_plan_id::text AS plan_id, day_no, hotel_room_type_id::text AS hotel_room_type_id,
       service_id::text AS service_id
FROM booking.trip_plan_components WHERE trip_plan_id = ANY($1::uuid[])
"""


@dataclass
class Plan:
    id: str
    key: str
    sellable: bool
    title: str | None = None
    country: str | None = None
    duration_days: int | None = None
    in_catalog: bool | None = None
    components: list[dict[str, Any]] | None = None  # the new set (None = not in the file)
    hand: list[dict[str, Any]] | None = None  # the new hand rows (None = not in the file)
    new_departures: list[dict[str, Any]] = field(default_factory=list)
    existing_departures: list[date] = field(default_factory=list)
    sellable_after: bool | None = None
    components_now: list[dict[str, Any]] = field(default_factory=list)  # the set the plan has today


async def _catalog(conn: asyncpg.Connection, keys: list[str]) -> dict[str, Any] | None:
    try:
        async with conn.transaction():
            return {r["source_id"]: r for r in await conn.fetch(CATALOG_SQL, keys)}
    except CATALOG_ERRORS:
        return None


async def match(conn: asyncpg.Connection, sheets: Sheets) -> dict[str, Plan]:
    """Resolve tour keys and item codes; problems are added to ``sheets``."""
    keys = sheets.keys()
    plans = {
        r["source_id"]: Plan(r["id"], r["source_id"], bool(r["sellable"])) for r in await conn.fetch(PLANS_SQL, keys)
    }
    catalog = await _catalog(conn, list(plans))
    for key, plan in plans.items():
        row = (catalog or {}).get(key)
        plan.in_catalog = None if catalog is None else row is not None
        if row is not None:
            plan.title, plan.country, plan.duration_days = row["title"], row["country"], row["duration_days"]
    for sheet, rows in ((TOURS, sheets.tours), (COMPONENTS, sheets.components), (PRICES, sheets.prices)):
        for r in rows:
            if r.get("tour_key") and r["tour_key"] not in plans:
                _problem(sheets, sheet, r, "Tour key", "unknown_tour", f"no trip plan with the key {r['tour_key']}")
    for d in sheets.departures:
        if d.get("tour_key") and d["tour_key"] not in plans:
            _problem(sheets, DEPARTURES, d, "Tour key", "unknown_tour", f"no trip plan with the key {d['tour_key']}")

    rooms = {
        (r["hotel_code"], r["room_type"]): r
        for r in await conn.fetch(
            ROOMS_SQL, sorted({c["hotel_code"] for c in sheets.components if c.get("hotel_code")})
        )
    }
    services = {
        r["code"]: r
        for r in await conn.fetch(
            SERVICES_SQL, sorted({c["service_code"] for c in sheets.components if c.get("service_code")})
        )
    }
    for c in sheets.components:
        plan = plans.get(c.get("tour_key") or "")
        if plan is None or not c.get("day_no"):
            continue
        if plan.duration_days and c["day_no"] > plan.duration_days:
            _problem(sheets, COMPONENTS, c, "Day", "day_out_of_tour", f"the tour has {plan.duration_days} days")
            continue
        item: dict[str, Any] = {"day_no": c["day_no"], "hotel_room_type_id": None, "service_id": None}
        if c.get("hotel_code") and c.get("room_type"):
            found = rooms.get((c["hotel_code"], rs._key(c["room_type"])))
            if found is None:
                _problem(
                    sheets,
                    COMPONENTS,
                    c,
                    "Room type",
                    "unknown_item",
                    f"no room type '{c['room_type']}' at hotel {c['hotel_code']}",
                )
                continue
            if found["status"] != "active" or found["hotel_status"] != "active":
                _problem(sheets, COMPONENTS, c, "Room type", "inactive_item", "the room type or hotel is inactive")
                continue
            item["hotel_room_type_id"] = found["id"]
        elif c.get("service_code"):
            found = services.get(c["service_code"])
            if found is None:
                _problem(sheets, COMPONENTS, c, "Service code", "unknown_item", f"no service {c['service_code']}")
                continue
            if found["status"] != "active":
                _problem(sheets, COMPONENTS, c, "Service code", "inactive_item", "the service is inactive")
                continue
            item["service_id"] = found["id"]
        else:
            continue
        plan.components = (plan.components or []) + [item]
    for plan in plans.values():
        if plan.components is not None and len(plan.components) > plan_components.MAX_COMPONENTS:
            sheets.problems.append(
                Problem(
                    COMPONENTS,
                    None,
                    "Tour key",
                    "too_many_components",
                    f"{plan.key}: at most {plan_components.MAX_COMPONENTS} components",
                )
            )
    for p in sheets.prices:
        plan = plans.get(p.get("tour_key") or "")
        if plan is not None:
            plan.hand = (plan.hand or []) + [p]
    existing: dict[str, set[date]] = {}
    for r in await conn.fetch(EXISTING_DEPARTURES_SQL, [p.id for p in plans.values()]):
        existing.setdefault(r["plan_id"], set()).add(r["start_date"])
    in_file: set[tuple[str, date]] = set()
    for d in sheets.departures:
        plan = plans.get(d.get("tour_key") or "")
        if plan is None or not d.get("start_date"):
            continue
        if d["start_date"] in existing.get(plan.id, set()) or (plan.key, d["start_date"]) in in_file:
            plan.existing_departures.append(d["start_date"])
            continue
        in_file.add((plan.key, d["start_date"]))
        plan.new_departures.append(d)
    current = {}
    for r in await conn.fetch(COMPONENT_KEYS_SQL, [p.id for p in plans.values()]):
        current.setdefault(r["plan_id"], []).append(dict(r))
    for t in sheets.tours:
        plan = plans.get(t.get("tour_key") or "")
        if plan is None or t.get("sellable") is None:
            continue
        plan.sellable_after = t["sellable"]
        if t["sellable"] and not plan.sellable:
            parts = plan.components if plan.components is not None else current.get(plan.id, [])
            if not parts:
                _problem(sheets, TOURS, t, "Sellable", "no_components", "a sellable tour needs its components first")
            elif plan.in_catalog is False:
                _problem(sheets, TOURS, t, "Sellable", "not_in_catalog", "the tour is no longer in the CIS catalog")
            elif plan.in_catalog is None:
                _problem(
                    sheets,
                    TOURS,
                    t,
                    "Sellable",
                    "catalog_unreadable",
                    "the catalog cannot be read right now; try again",
                )
    for plan in plans.values():
        plan.components_now = current.get(plan.id, [])
    return plans


# ── Price rows ───────────────────────────────────────────────────────────────


def hand_row(p: Mapping[str, Any], markup: Decimal | None, increments: engine.Increments) -> dict[str, Any]:
    """One imported hand price as ``trip_plan_prices.store`` writes it (no net cost: nothing is computed)."""
    amount = p["amount"]
    return {
        "channel": p["channel"],
        "season": str(p["season"])[: cost_sheet.SEASON_MAX] if p.get("season") else None,
        "peak": bool(p.get("peak")),
        "valid_from": p["valid_from"],
        "valid_to": p["valid_to"],
        "pax_min": int(p["pax_min"]),
        "pax_max": p.get("pax_max"),
        "currency": p["currency"],
        "amount": amount,
        "net_amount": None,
        "net_currency": None,
        "effective_margin": None,
        "priced_on": None,
        "overridden": False,
        "computed": False,
        "rrp_amount": trip_plan_prices.rrp_for(p["channel"], amount, p["currency"], markup, increments),
        "child_amount": p.get("child_amount"),
        "infant_amount": p.get("infant_amount"),
        "single_supplement_amount": p.get("single_supplement_amount"),
        "child_net_amount": None,
        "infant_net_amount": None,
        "single_supplement_net_amount": None,
    }


def cost_priced(row: Mapping[str, Any]) -> bool:
    """A row priced from a cost: computed from supplier rates, or typed with a net cost on the single price form (its
    net cost, margin and override live only in the matrix). Neither is in the workbook, so an import keeps them."""
    return bool(row["computed"]) or row["net_amount"] is not None


@dataclass(frozen=True)
class Merged:
    rows: list[dict[str, Any]]
    computed_replaced: int
    net_replaced: int


def merge_prices(
    existing: Sequence[dict[str, Any]], hand: Sequence[dict[str, Any]], pricing: cost_sheet.Pricing
) -> Merged:
    """The new matrix: the imported hand rows, then the cost-priced rows kept (as stored, their RRP under today's
    markup) that no hand row overlaps; positions 1..n. Plain hand rows (no net cost) are the file's and are replaced.
    Also counts the cost-priced rows a hand row replaces (computed, and typed with a net cost)."""
    priced = [cost_sheet.kept_row(r, pricing) for r in existing if cost_priced(r)]
    covered = [any(cost_sheet.overlaps(r, h) for h in hand) for r in priced]
    kept = [r for r, c in zip(priced, covered, strict=True) if not c]
    gone = [r for r, c in zip(priced, covered, strict=True) if c]
    final = [*(dict(h) for h in hand), *kept]
    for i, row in enumerate(final, start=1):
        row["position"] = i
    computed = sum(1 for r in gone if r["computed"])
    return Merged(final, computed, len(gone) - computed)


# ── Status after (dry run) ───────────────────────────────────────────────────


async def status_after(
    conn: asyncpg.Connection, plans: Mapping[str, Plan], facts: price_readiness.Facts, today: date
) -> dict[str, str | None]:
    """Each plan's readiness status if the import were applied (the readiness rules on the new components, the rates
    in force and the new hand rows; a plan priced from rates stays priced only when nothing it needs is missing)."""
    rooms = {c["hotel_room_type_id"] for p in plans.values() for c in p.components or [] if c["hotel_room_type_id"]}
    services = {c["service_id"] for p in plans.values() for c in p.components or [] if c["service_id"]}
    rated = {k: set(v) for k, v in facts.rated.items()}
    if rooms or services:
        spans = await conn.fetch(cost_sheet.SPANS_SQL, sorted(rooms), sorted(services))
        for item, currencies in price_readiness.rated_items(spans, today).items():
            rated.setdefault(item, set()).update(currencies)
    has_rule = bool(facts.pricing.rules)
    by_id = {p.plan_id: p for p in facts.plans}
    out: dict[str, str | None] = {}
    for plan in plans.values():
        known = by_id.get(plan.id)
        if known is None:
            out[plan.key] = None
            continue
        parts = known.components
        if plan.components is not None:
            parts = [
                price_readiness.Component(int(c["day_no"]), c["hotel_room_type_id"], c["service_id"], True)
                for c in plan.components
            ]
        probe = price_readiness.Plan(plan.id, plan.key, plan.sellable, components=list(parts))
        reason, _, _ = price_readiness.blocker(probe, rated, has_rule=has_rule, fx=facts.pricing.fx)
        hand_valid = any(h["valid_to"] >= today for h in plan.hand) if plan.hand is not None else known.hand_rows > 0
        if reason is None and (known.computed_rows or parts):
            out[plan.key] = price_readiness.PRICED
        elif hand_valid:
            out[plan.key] = price_readiness.HAND_PRICED
        else:
            out[plan.key] = reason or price_readiness.MISSING_RATES
    return out


# ── Import ───────────────────────────────────────────────────────────────────


def _raise(sheets: Sheets) -> None:
    raise Invalid(
        "price_import_invalid",
        f"{len(sheets.problems)} problem(s) in the workbook; nothing was imported",
        errors=[p.out() for p in sheets.problems[:MAX_ERRORS]],
        ignored_sheets=sheets.ignored_sheets,
    )


def _same_components(before: Sequence[Mapping[str, Any]], after: Sequence[Mapping[str, Any]]) -> bool:
    def key(c: Mapping[str, Any]) -> tuple[int, str, str]:
        return (int(c["day_no"]), c.get("hotel_room_type_id") or "", c.get("service_id") or "")

    return sorted(map(key, before)) == sorted(map(key, after))


async def import_workbook(
    conn: asyncpg.Connection,
    staff: Staff,
    data: bytes,
    *,
    dry_run: bool,
    plan_guard: Callable[[Mapping[str, Plan]], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """Check the workbook and, unless ``dry_run``, apply it (call inside a transaction: any refusal rolls every row
    back). 422 ``price_import_invalid`` lists every problem by sheet, row and column. ``plan_guard`` (the route's
    Dev-only E2E check, AA-1091 A10e-2) sees every matched tour before anything is previewed or written."""
    today = cost_sheet.today()
    sheets = parse(data)
    check_shapes(sheets, today)
    plans = await match(conn, sheets)
    if plan_guard is not None:
        await plan_guard(plans)
    if sheets.problems:
        _raise(sheets)
    pricing = await cost_sheet.load_pricing(conn, today)
    facts = await price_readiness.load(conn, today, [p.id for p in plans.values()])
    before = {r["tour_key"]: r for r in await price_readiness.resolve(conn, facts, facts.rated)}
    after = await status_after(conn, plans, facts, today)
    tours: list[dict[str, Any]] = []
    for plan in plans.values():
        existing = [dict(r) for r in await conn.fetch(trip_plan_prices.ROWS_SQL, plan.id)]
        hand_before = sum(1 for r in existing if not cost_priced(r))
        merged = Merged([], 0, 0)
        if plan.hand is not None:
            merged = merge_prices(existing, [hand_row(h, pricing.markup, pricing.rounding) for h in plan.hand], pricing)
        now = plan.components_now
        tours.append(
            {
                "tour_key": plan.key,
                "title": plan.title,
                "country": plan.country,
                "sellable_before": plan.sellable,
                "sellable_after": plan.sellable if plan.sellable_after is None else plan.sellable_after,
                "components_before": len(now),
                "components_after": len(plan.components) if plan.components is not None else len(now),
                "components_changed": plan.components is not None and not _same_components(now, plan.components),
                "hand_rows_before": hand_before,
                "hand_rows_after": len(plan.hand) if plan.hand is not None else hand_before,
                "computed_rows_replaced": merged.computed_replaced,
                "net_rows_replaced": merged.net_replaced,
                "departures_new": [d["start_date"] for d in plan.new_departures],
                "departures_existing": plan.existing_departures,
                "status_before": (before.get(plan.key) or {}).get("status"),
                "status_after": after.get(plan.key),
            }
        )
    out: dict[str, Any] = {
        "dry_run": dry_run,
        "tours": tours,
        "totals": {
            "tours": len(tours),
            "components_changed": sum(1 for t in tours if t["components_changed"]),
            "price_tours": sum(1 for p in plans.values() if p.hand is not None),
            "departures_new": sum(len(t["departures_new"]) for t in tours),
            "departures_existing": sum(len(t["departures_existing"]) for t in tours),
            "sellable_changes": sum(1 for t in tours if t["sellable_after"] != t["sellable_before"]),
            "would_be_priced": sum(
                1 for t in tours if t["status_after"] in (price_readiness.PRICED, price_readiness.HAND_PRICED)
            ),
        },
        "ignored_sheets": sheets.ignored_sheets,
    }
    if dry_run:
        return out
    drafts = await _apply(conn, staff, plans, hashlib.sha256(data).hexdigest())
    out["price_drafts"] = drafts
    recomputed = 0
    for plan in list(plans.values()):
        if recomputed >= MAX_RECOMPUTE:
            break
        parts = plan.components if plan.components is not None else plan.components_now
        if parts:
            async with conn.transaction():
                await cost_sheet.recompute_plan(conn, plan.id, pricing)
            recomputed += 1
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="trip_plan.bulk_imported",
        entity_type="trip_plan",
        entity_id=None,
        after={**out["totals"], "tour_keys": [t["tour_key"] for t in tours][:50], "recomputed": recomputed},
        entity_label="price setup workbook",
        summary=f"Price setup workbook: {len(tours)} tours",
    )
    facts = await price_readiness.load(conn, today, [p.id for p in plans.values()])
    status_now = {r["tour_key"]: r["status"] for r in await price_readiness.resolve(conn, facts, facts.rated)}
    for t in out["tours"]:
        t["status_after"] = status_now.get(t["tour_key"], t["status_after"])
    out["recomputed"] = recomputed
    return out


async def _apply(
    conn: asyncpg.Connection, staff: Staff, plans: Mapping[str, Plan], file_sha256: str
) -> list[dict[str, Any]]:
    """Write the workbook; returns the draft price versions made (one per tour with prices, AA-1091)."""
    drafts: list[dict[str, Any]] = []
    for plan in plans.values():
        if plan.components is not None:
            await plan_components.replace_components(conn, staff, plan.key, plan.components)
        if plan.hand:
            if not staff.can("price_version.write"):
                raise Forbidden("forbidden", "importing prices makes a draft price version (price_version.write)")
            made = await price_versions.import_draft(
                conn, staff, plan.id, plan.key, [draft_row(h) for h in plan.hand], file_sha256
            )
            drafts.append({"tour_key": plan.key, **made})
        for d in plan.new_departures:
            await departures.create_departure(
                conn,
                staff,
                plan.key,
                {
                    "start_date": d["start_date"],
                    "capacity": d["capacity"],
                    "status": d["status"],
                    "auto_confirm": True if d.get("auto_confirm") is None else d["auto_confirm"],
                    "min_to_operate": d.get("min_to_operate") or 0,
                    "sales_cutoff_days": d.get("sales_cutoff_days") or 0,
                    "decision_days_before": d.get("decision_days_before"),
                },
            )
        if plan.sellable_after is not None and plan.sellable_after != plan.sellable:
            await price_workbench.set_sellable(
                conn, staff, {"id": plan.id, "source_id": plan.key, "sellable": plan.sellable}, plan.sellable_after
            )
    return drafts


def draft_row(p: Mapping[str, Any]) -> dict[str, Any]:
    """One imported hand price as a draft version row (the peak comes from the season calendar)."""
    return {
        "channel": p["channel"],
        "season": str(p["season"])[: cost_sheet.SEASON_MAX] if p.get("season") else None,
        "valid_from": p["valid_from"],
        "valid_to": p["valid_to"],
        "pax_min": int(p["pax_min"]),
        "pax_max": p.get("pax_max"),
        "currency": p["currency"],
        "amount": p["amount"],
        "child_amount": p.get("child_amount"),
        "infant_amount": p.get("infant_amount"),
        "single_supplement_amount": p.get("single_supplement_amount"),
    }


# ── Template ─────────────────────────────────────────────────────────────────

TEMPLATE_COMPONENTS_SQL = """
SELECT p.source_id, c.day_no, h.code AS hotel_code, rt.name AS room_type, s.code AS service_code
FROM booking.trip_plan_components c
JOIN booking.trip_plans p ON p.id = c.trip_plan_id
LEFT JOIN booking.hotel_room_types rt ON rt.id = c.hotel_room_type_id
LEFT JOIN booking.hotels h ON h.id = rt.hotel_id
LEFT JOIN booking.services s ON s.id = c.service_id
WHERE p.source_system = 'cis' AND p.source_id = ANY($1::text[])
ORDER BY p.source_id, c.day_no, h.code NULLS LAST, s.code
"""
TEMPLATE_PRICES_SQL = """
SELECT p.source_id, r.channel, r.season, r.peak, r.valid_from, r.valid_to, r.pax_min, r.pax_max,
       r.currency::text AS currency, r.amount, r.child_amount, r.infant_amount, r.single_supplement_amount
FROM booking.trip_plan_prices r JOIN booking.trip_plans p ON p.id = r.trip_plan_id
WHERE p.source_system = 'cis' AND p.source_id = ANY($1::text[]) AND NOT r.computed AND r.net_amount IS NULL
ORDER BY p.source_id, r.position
"""
TEMPLATE_DEPARTURES_SQL = """
SELECT p.source_id, d.start_date, d.capacity, d.min_to_operate, d.sales_cutoff_days, d.decision_days_before,
       d.auto_confirm, d.status
FROM booking.departures d JOIN booking.trip_plans p ON p.id = d.trip_plan_id
WHERE p.source_system = 'cis' AND p.source_id = ANY($1::text[]) AND d.status <> 'cancelled' AND d.start_date > $2
ORDER BY p.source_id, d.start_date
"""


def _yn(value: bool | None) -> str | None:
    return None if value is None else ("yes" if value else "no")


def _num(value: Any) -> Any:
    return None if value is None else float(value) if isinstance(value, Decimal) else value


def _as_text(cell: Any) -> None:
    """A database text that starts like a formula stays text (openpyxl turns a str starting with "=" into a formula):
    a string cell with Excel's quote prefix, the value unchanged (no "'" added, so the round trip still matches)."""
    cell.data_type = "s"
    cell.quotePrefix = True


def build_workbook(
    tours: Sequence[Mapping[str, Any]] = (),
    components: Sequence[Mapping[str, Any]] = (),
    prices: Sequence[Mapping[str, Any]] = (),
    departure_rows: Sequence[Mapping[str, Any]] = (),
    *,
    note: str | None = None,
) -> bytes:
    """The workbook: empty, or prefilled with the given rows (dicts keyed like the database rows)."""
    wb = Workbook()
    ws = wb.active
    ws.title = README
    for line in README_LINES:
        ws.append([line])
    if note:
        ws.append([note])
    sheets = {name: wb.create_sheet(name) for name in (TOURS, COMPONENTS, PRICES, DEPARTURES)}
    for name, sheet in sheets.items():
        sheet.append(list(COLUMNS[name]))
        sheet.freeze_panes = "A2"
    for t in tours:
        sheets[TOURS].append([t["tour_key"], t.get("title"), t.get("country"), _yn(t.get("sellable"))])
    for c in components:
        sheets[COMPONENTS].append(
            [c["source_id"], c["day_no"], c.get("hotel_code"), c.get("room_type"), c.get("service_code")]
        )
    for p in prices:
        sheets[PRICES].append(
            [
                p["source_id"],
                p["channel"],
                p.get("season"),
                _yn(p.get("peak")),
                p["valid_from"],
                p["valid_to"],
                p["pax_min"],
                p.get("pax_max"),
                (p.get("currency") or "").strip(),
                _num(p["amount"]),
                _num(p.get("child_amount")),
                _num(p.get("infant_amount")),
                _num(p.get("single_supplement_amount")),
            ]
        )
    for d in departure_rows:
        sheets[DEPARTURES].append(
            [
                d["source_id"],
                d["start_date"],
                d["capacity"],
                d.get("min_to_operate"),
                d.get("sales_cutoff_days"),
                d.get("decision_days_before"),
                _yn(d.get("auto_confirm")),
                d.get("status"),
            ]
        )
    for sheet in sheets.values():
        for col in sheet.iter_cols(min_row=2):
            for cell in col:
                if isinstance(cell.value, date):
                    cell.number_format = "yyyy-mm-dd"
                elif isinstance(cell.value, str) and cell.value.startswith(FORMULA_START):
                    _as_text(cell)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def template() -> bytes:
    return build_workbook()


async def prefilled(conn: asyncpg.Connection, items: Sequence[Mapping[str, Any]], *, today: date) -> bytes:
    """The workbook prefilled with the current state of the given workbench rows (at most :data:`MAX_TOURS`)."""
    chosen = list(items[:MAX_TOURS])
    keys = [r["tour_key"] for r in chosen]
    note = (
        f"Prefilled with the first {MAX_TOURS} of {len(items)} tours of the filter; narrow the filter for the others."
        if len(items) > MAX_TOURS
        else None
    )
    return build_workbook(
        chosen,
        [dict(r) for r in await conn.fetch(TEMPLATE_COMPONENTS_SQL, keys)],
        [dict(r) for r in await conn.fetch(TEMPLATE_PRICES_SQL, keys)],
        [dict(r) for r in await conn.fetch(TEMPLATE_DEPARTURES_SQL, keys, today)],
        note=note,
    )


def filename(on: date) -> str:
    return f"aa-price-setup-{on.isoformat()}.xlsx"
