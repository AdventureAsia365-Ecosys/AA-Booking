"""Supplier rate-sheet Excel import (root ADR 0005 decisions 7 and 8, AA-1029 R4): one AA template workbook -> one
**draft** supplier contract with its net rates, through the same service code as the admin forms (``create_contract``,
``create_rate``), so every rule and audit row is the same. Publishing stays the second person's step (maker-checker,
bk-008): the importer becomes the draft's maker.

Template (:func:`template`; ``GET /booking/rate-sheets/template``):

- sheet ``Contract``: two columns, field / value: Title, Reference, Valid from, Valid to, Currency, Payment terms,
  Cancellation policy (Title, Valid from and Currency required; Valid to empty = open-ended);
- one sheet per service type (ADR 0005 d8): ``Hotel`` (Hotel code, Room type, Season, Valid from, Valid to, Meal plan,
  Pax min, Pax max, Net, Notes) and ``Transport``, ``Guide``, ``Entrance``, ``Activity``, ``Meal`` (Service code,
  Season, Valid from, Valid to, Pax min, Pax max, Net, Notes). Row 1 holds the headers (matched by name, any order),
  data from row 2, empty rows skipped. A ``Currency`` column, when present, must equal the contract's. The unit comes
  from the room type (per room night) or the service (its pricing unit); a service must be of the sheet's type.
- other sheets (instructions) are ignored and reported.
- AA-1145 (bk-060): optional columns on every rate sheet: ``Published`` + ``Commission %`` (B7: a commissionable
  supplier price; the net is derived as published x (100 - commission %) / 100, half-up to the cent, and both source
  numbers are kept on the rate; ``Net`` may then be empty, or must equal the derived net), and on the service sheets
  ``Pax type`` (adult, child, infant; empty = adult) with ``Age min`` / ``Age max`` (a child or infant band, B4).

AA-1153 part 1 (bk-069; S285 advisory r2 hotels T8, T10, B7): a DMC rate sheet for hotels it shares with other DMCs.

- **Hotels the supplier sells**: a hotel row matches a hotel **linked** to the supplier (``hotel_supplier_links``), by
  the DMC's own code for it first, then the AA code; a room type by its name, then by an alias (the supplier's own
  alias first, then an any-DMC alias). An unknown room type names the closest room types of the hotel.
- optional sheet ``Seasons`` (Season, From, To; one season may have several rows): the contract's named seasons
  (``supplier_contract_seasons``). A rate row naming a season with no dates becomes one rate per range of that season.
- optional sheet ``Hotel children`` (Hotel code, Age min, Age max, Basis = sharing | extra bed, Meal plan, Net): the
  DMC's child price per night for the hotel (``hotel_child_policies``).
- **Business checks** (:mod:`app.supply.rate_checks`): seasons that overlap (an error: nothing is imported), contract
  days no season covers, a board cheaper than one with fewer meals, a price at least ``rate_sheet_jump_warn_pct`` %
  above the same rate of the supplier's previous contract (warnings). The dry run lists them with the **diff** against
  that previous contract (added / changed / removed rates); an import stores both in ``booking.rate_sheet_imports``
  (file hash and name), and the second person who publishes the draft acknowledges the warnings
  (``app.suppliers.service``; the live review is :func:`review`).

Items are matched to the supplier's **existing** hotels (by code, ``HTL-000123``, then the room type name, case and
spaces ignored) and services (by code, ``SVC-000123``): supply facts are entered and checked in the admin (ADR 0004a),
a rate sheet only prices them. Every problem is reported per sheet, row and column (``errors``) and **nothing is
written** while one is left; ``dry_run`` validates and previews without writing, and adds the sheet's ``impact``
(``app.catalog.price_readiness.impact``: the trip plans whose components use the sheet's items, whether each would
have a rate for every component once the draft is published, and how many would move to priced; a read only).
Bounded: at most :data:`MAX_BYTES` and :data:`MAX_ROWS` rows. The workbook is read with openpyxl in read-only,
values-only mode (no formulas are run, no macros: an ``.xlsm`` is refused by its content type and openpyxl never
executes VBA).
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from difflib import get_close_matches
from typing import Any

import asyncpg
from openpyxl import Workbook, load_workbook

from app.auth.staff import Staff
from app.catalog import cost_sheet, price_readiness
from app.errors import Invalid
from app.pricing import policy_values
from app.pricing.policy_params_hotels import JUMP_WARN_PCT
from app.suppliers import service as suppliers
from app.supply import rate_checks, rates

MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 2000
CONTRACT_SHEET = "Contract"
HOTEL_SHEET = "Hotel"
SEASONS_SHEET = "Seasons"
CHILDREN_SHEET = "Hotel children"
SEASON_COLUMNS = ("Season", "From", "To")
CHILDREN_COLUMNS = ("Hotel code", "Age min", "Age max", "Basis", "Meal plan", "Net")
BASES = {"sharing": "sharing", "extra bed": "extra_bed", "extra_bed": "extra_bed"}
SERVICE_SHEETS = {
    "Transport": "transfer",
    "Guide": "guide",
    "Entrance": "entrance",
    "Activity": "activity",
    "Meal": "meal",
}
CONTRACT_FIELDS = {
    "title": "title",
    "reference": "reference",
    "valid from": "valid_from",
    "valid to": "valid_to",
    "currency": "currency",
    "payment terms": "payment_terms",
    "cancellation policy": "cancellation_policy",
}
HOTEL_COLUMNS = (
    "Hotel code",
    "Room type",
    "Season",
    "Valid from",
    "Valid to",
    "Meal plan",
    "Pax min",
    "Pax max",
    "Net",
    "Notes",
    "Published",
    "Commission %",
)
SERVICE_COLUMNS = (
    "Service code",
    "Season",
    "Valid from",
    "Valid to",
    "Pax min",
    "Pax max",
    "Net",
    "Notes",
    "Pax type",
    "Age min",
    "Age max",
    "Published",
    "Commission %",
)
# A rate row needs a Net or the Published + Commission % pair (B7); the columns themselves are optional.
REQUIRED = {HOTEL_SHEET: ("Hotel code", "Room type"), "service": ("Service code",)}
PAX_TYPES = ("adult", "child", "infant")
AGE_MAX = 17  # technical-constant: the bk-060 CHECK bound (a child or infant is under 18)
MAX_ERRORS = 200


@dataclass
class Problem:
    sheet: str
    row: int | None
    column: str | None
    code: str
    message: str

    def out(self) -> dict[str, Any]:
        return {"sheet": self.sheet, "row": self.row, "column": self.column, "code": self.code, "message": self.message}


@dataclass
class RateRow:
    sheet: str
    row: int
    hotel_code: str | None = None
    room_type: str | None = None
    service_code: str | None = None
    values: dict[str, Any] = field(default_factory=dict)


@dataclass
class Sheet:
    contract: dict[str, Any]
    rows: list[RateRow]
    problems: list[Problem]
    ignored_sheets: list[str]
    seasons: list[dict[str, Any]] = field(default_factory=list)  # AA-1153: {name, date_from, date_to, sheet, row}
    children: list[dict[str, Any]] = field(default_factory=list)  # AA-1153: hotel child prices as read


# ── Cell values ──────────────────────────────────────────────────────────────


def _key(text: Any) -> str:
    return " ".join(str(text or "").split()).lower()


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text or None


def _date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).date()  # noqa: DTZ007 — a calendar date, no time zone
        except ValueError:
            continue
    raise ValueError("use a date (YYYY-MM-DD)")


def _amount(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("a number is expected")
    try:
        number = Decimal(str(value).replace(",", "").strip())
    except InvalidOperation:
        raise ValueError("a number is expected") from None
    if not number.is_finite() or number < 0:
        raise ValueError("a zero or positive number is expected")
    if number != number.quantize(Decimal("0.01")):
        raise ValueError("at most 2 decimals")
    return number


def _pct(value: Any) -> Decimal | None:
    number = _amount(value)
    if number is not None and number >= 100:
        raise ValueError("a commission below 100 % is expected")
    return number


def _age(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation:
        raise ValueError("a whole number is expected") from None
    if number != number.to_integral_value() or not 0 <= number <= AGE_MAX:
        raise ValueError(f"a whole number from 0 to {AGE_MAX} is expected")
    return int(number)


def _pax_type(value: Any) -> str | None:
    text = (_text(value) or "").lower()
    if not text:
        return None
    if text not in PAX_TYPES:
        raise ValueError(f"one of {', '.join(PAX_TYPES)} is expected")
    return text


def _int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation:
        raise ValueError("a whole number is expected") from None
    if number != number.to_integral_value() or not 1 <= number <= 999:
        raise ValueError("a whole number from 1 to 999 is expected")
    return int(number)


# ── Parsing (no database) ────────────────────────────────────────────────────


def _open(data: bytes) -> Any:
    if not data:
        raise Invalid("rate_sheet_empty", "upload an .xlsx rate sheet")
    if len(data) > MAX_BYTES:
        raise Invalid("rate_sheet_too_large", f"a rate sheet is at most {MAX_BYTES // (1024 * 1024)} MB")
    if not zipfile.is_zipfile(io.BytesIO(data)):
        raise Invalid("rate_sheet_not_xlsx", "the file is not an .xlsx workbook")
    try:
        return load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception:  # noqa: BLE001 — any parse error of an untrusted file is a 422, never a 500
        raise Invalid("rate_sheet_not_xlsx", "the file could not be read as an .xlsx workbook") from None


def _contract(ws: Any, problems: list[Problem]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for n, row in enumerate(ws.iter_rows(min_row=1, max_col=2, values_only=True), start=1):
        if not row or row[0] is None:
            continue
        name = CONTRACT_FIELDS.get(_key(row[0]))
        if name is None:
            continue
        value = row[1] if len(row) > 1 else None
        try:
            if name in ("valid_from", "valid_to"):
                out[name] = _date(value)
            elif name == "currency":
                code = (_text(value) or "").upper()
                if code and not re.fullmatch(r"[A-Z]{3}", code):
                    raise ValueError("a 3-letter currency code is expected")
                out[name] = code or None
            else:
                out[name] = _text(value)
        except ValueError as exc:
            problems.append(Problem(CONTRACT_SHEET, n, str(row[0]), "invalid_value", str(exc)))
    for name, label in (("title", "Title"), ("valid_from", "Valid from"), ("currency", "Currency")):
        if not out.get(name):
            problems.append(Problem(CONTRACT_SHEET, None, label, "required", f"{label} is required"))
    return out


def _applies(label: str, sheet: str) -> bool:
    """A meal plan belongs to the hotel sheet, a pax type and age band to the service sheets (bk-060)."""
    if label == "Meal plan":
        return sheet == HOTEL_SHEET
    return sheet != HOTEL_SHEET or label not in ("Pax type", "Age min", "Age max")


def _rows(ws: Any, sheet: str, problems: list[Problem], budget: list[int]) -> list[RateRow]:
    rows = ws.iter_rows(values_only=True)
    header = next(rows, None)
    if header is None:
        return []
    columns = {_key(h): i for i, h in enumerate(header) if h is not None}
    required = REQUIRED[HOTEL_SHEET] if sheet == HOTEL_SHEET else REQUIRED["service"]
    missing = [c for c in required if _key(c) not in columns]
    if missing:
        problems.append(
            Problem(sheet, 1, ", ".join(missing), "missing_column", f"missing column(s): {', '.join(missing)}")
        )
        return []

    def cell(row: tuple[Any, ...], name: str) -> Any:
        i = columns.get(_key(name))
        return row[i] if i is not None and i < len(row) else None

    out: list[RateRow] = []
    for n, row in enumerate(rows, start=2):
        if row is None or all(v is None or str(v).strip() == "" for v in row):
            continue
        budget[0] += 1
        if budget[0] > MAX_ROWS:
            raise Invalid("rate_sheet_too_many_rows", f"a rate sheet has at most {MAX_ROWS} rate rows")
        rate = RateRow(sheet=sheet, row=n)
        if sheet == HOTEL_SHEET:
            rate.hotel_code = (_text(cell(row, "Hotel code")) or "").upper() or None
            rate.room_type = _text(cell(row, "Room type"))
        else:
            rate.service_code = (_text(cell(row, "Service code")) or "").upper() or None
        for label, name, conv in (
            ("Season", "season", _text),
            ("Valid from", "valid_from", _date),
            ("Valid to", "valid_to", _date),
            ("Pax min", "pax_min", _int),
            ("Pax max", "pax_max", _int),
            ("Net", "net_amount", _amount),
            ("Notes", "notes", _text),
            ("Meal plan", "meal_plan", _text),
            ("Currency", "currency", _text),
            ("Published", "published_amount", _amount),
            ("Commission %", "commission_pct", _pct),
            ("Pax type", "pax_type", _pax_type),
            ("Age min", "age_min", _age),
            ("Age max", "age_max", _age),
        ):
            if not _applies(label, sheet):
                continue
            try:
                value = conv(cell(row, label))
            except ValueError as exc:
                problems.append(Problem(sheet, n, label, "invalid_value", str(exc)))
                continue
            if value is not None:
                rate.values[name] = value
        for label, have in (
            ("Hotel code" if sheet == HOTEL_SHEET else "Service code", rate.hotel_code or rate.service_code),
            ("Room type", rate.room_type if sheet == HOTEL_SHEET else True),
            (
                "Net",
                "net_amount" in rate.values
                or "published_amount" in rate.values
                or "commission_pct" in rate.values
                or any(p.sheet == sheet and p.row == n and p.column == "Net" for p in problems),
            ),
        ):
            if not have:
                problems.append(Problem(sheet, n, label, "required", f"{label} is required"))
        _commission(rate, problems)
        if (meal := rate.values.get("meal_plan")) is not None:
            rate.values["meal_plan"] = str(meal).upper()
        out.append(rate)
    return out


def _commission(rate: RateRow, problems: list[Problem]) -> None:
    """B7: Published + Commission % give the net (both or neither; a Net given too must match)."""
    published, pct = rate.values.get("published_amount"), rate.values.get("commission_pct")
    if published is None and pct is None:
        return
    if published is None or pct is None:
        column = "Commission %" if pct is None else "Published"
        problems.append(
            Problem(rate.sheet, rate.row, column, "commission_pair", "Published and Commission % go together")
        )
        return
    net = rates.net_from_published(published, pct)
    given = rate.values.get("net_amount")
    if given is not None and given != net:
        problems.append(
            Problem(
                rate.sheet,
                rate.row,
                "Net",
                "net_mismatch",
                f"the net of {published} less {pct} % is {net}; leave Net empty or enter {net}",
            )
        )
        return
    rate.values["net_amount"] = net


def parse(data: bytes) -> Sheet:
    """Read the workbook; shape problems are collected (never raised), except a file that is not a workbook."""
    wb = _open(data)
    problems: list[Problem] = []
    ignored: list[str] = []
    contract: dict[str, Any] = {}
    rows: list[RateRow] = []
    seasons: list[dict[str, Any]] = []
    children: list[dict[str, Any]] = []
    budget = [0]
    names = {_key(n): n for n in wb.sheetnames}
    if _key(CONTRACT_SHEET) not in names:
        problems.append(Problem(CONTRACT_SHEET, None, None, "missing_sheet", "the Contract sheet is missing"))
    try:
        for key, name in names.items():
            ws = wb[name]
            if key == _key(CONTRACT_SHEET):
                contract = _contract(ws, problems)
            elif key == _key(HOTEL_SHEET):
                rows += _rows(ws, HOTEL_SHEET, problems, budget)
            elif key == _key(SEASONS_SHEET):
                seasons = _seasons(ws, problems)
            elif key == _key(CHILDREN_SHEET):
                children = _children(ws, problems)
            elif sheet := next((s for s in SERVICE_SHEETS if _key(s) == key), None):
                rows += _rows(ws, sheet, problems, budget)
            else:
                ignored.append(name)
    finally:
        wb.close()
    if not rows and not any(p.code == "missing_column" for p in problems):
        problems.append(Problem(HOTEL_SHEET, None, None, "no_rates", "the workbook holds no rate rows"))
    rows = _expand_seasons(rows, seasons, problems)
    return Sheet(contract, rows, problems, ignored, seasons, children)


def _table(ws: Any, sheet: str, columns: tuple[str, ...], problems: list[Problem]) -> list[tuple[int, dict[str, Any]]]:
    """The rows of a small sheet as ``(row number, {column: raw value})``; every column is required."""
    rows = ws.iter_rows(values_only=True)
    header = next(rows, None)
    if header is None:
        return []
    index = {_key(h): i for i, h in enumerate(header) if h is not None}
    missing = [c for c in columns if _key(c) not in index]
    if missing:
        problems.append(
            Problem(sheet, 1, ", ".join(missing), "missing_column", f"missing column(s): {', '.join(missing)}")
        )
        return []
    out = []
    for n, row in enumerate(rows, start=2):
        if row is None or all(v is None or str(v).strip() == "" for v in row):
            continue
        out.append((n, {c: row[index[_key(c)]] if index[_key(c)] < len(row) else None for c in columns}))
    return out


def _seasons(ws: Any, problems: list[Problem]) -> list[dict[str, Any]]:
    out = []
    for n, cells in _table(ws, SEASONS_SHEET, SEASON_COLUMNS, problems):
        try:
            name, start, end = _text(cells["Season"]), _date(cells["From"]), _date(cells["To"])
        except ValueError as exc:
            problems.append(Problem(SEASONS_SHEET, n, None, "invalid_value", str(exc)))
            continue
        if not name or len(name) > 80 or start is None or end is None or end < start:
            problems.append(
                Problem(SEASONS_SHEET, n, None, "invalid_season", "a season has a name (1-80) and From <= To")
            )
            continue
        out.append({"name": name, "date_from": start, "date_to": end, "sheet": SEASONS_SHEET, "row": n})
    return out


def _children(ws: Any, problems: list[Problem]) -> list[dict[str, Any]]:
    out = []
    for n, cells in _table(ws, CHILDREN_SHEET, CHILDREN_COLUMNS, problems):
        try:
            age_min, age_max = _age(cells["Age min"]), _age(cells["Age max"])
            net = _amount(cells["Net"])
        except ValueError as exc:
            problems.append(Problem(CHILDREN_SHEET, n, None, "invalid_value", str(exc)))
            continue
        basis = BASES.get(_key(cells["Basis"]))
        board = (_text(cells["Meal plan"]) or "").upper() or None
        code = (_text(cells["Hotel code"]) or "").upper()
        if not code or age_min is None or age_max is None or age_max < age_min or net is None or basis is None:
            problems.append(
                Problem(
                    CHILDREN_SHEET,
                    n,
                    None,
                    "invalid_child_price",
                    "a child price has a hotel code, ages (min <= max), basis sharing or extra bed and a net",
                )
            )
            continue
        if board is not None and board not in rates.MEAL_PLANS:
            problems.append(Problem(CHILDREN_SHEET, n, "Meal plan", "unknown_meal_plan", "RO, BB, HB, FB or AI"))
            continue
        out.append(
            {
                "hotel_code": code,
                "age_min": age_min,
                "age_max": age_max,
                "basis": basis,
                "meal_plan": board,
                "net_amount": net,
                "row": n,
            }  # fmt: skip
        )
    return out


def _expand_seasons(rows: list[RateRow], seasons: list[dict[str, Any]], problems: list[Problem]) -> list[RateRow]:
    """A rate row naming a season of the Seasons sheet, with no dates, becomes one rate per range of that season."""
    if not seasons:
        return rows
    by_name: dict[str, list[dict[str, Any]]] = {}
    for season in seasons:
        by_name.setdefault(_key(season["name"]), []).append(season)
    out: list[RateRow] = []
    for rate in rows:
        name = rate.values.get("season")
        if not name or "valid_from" in rate.values or "valid_to" in rate.values:
            out.append(rate)
            continue
        ranges = by_name.get(_key(name))
        if ranges is None:
            problems.append(
                Problem(rate.sheet, rate.row, "Season", "unknown_season", f"'{name}' is not on the Seasons sheet")
            )
            continue
        for r in ranges:
            values = {**rate.values, "season": r["name"], "valid_from": r["date_from"], "valid_to": r["date_to"]}
            out.append(RateRow(rate.sheet, rate.row, rate.hotel_code, rate.room_type, rate.service_code, values))
    return out


# ── Matching and writing ─────────────────────────────────────────────────────

# AA-1153 (bk-069): the room types of every hotel linked (active) to the supplier, with the DMC's code for the hotel.
ROOM_TYPES_SQL = """
SELECT upper(h.code) AS hotel_code, upper(btrim(l.supplier_hotel_code)) AS dmc_code, h.id::text AS hotel_id,
       t.name, lower(regexp_replace(btrim(t.name), '\\s+', ' ', 'g')) AS room_type,
       t.id::text AS id, t.status, h.status AS hotel_status
FROM booking.hotel_supplier_links l
JOIN booking.hotels h ON h.id = l.hotel_id
JOIN booking.hotel_room_types t ON t.hotel_id = h.id
WHERE l.supplier_id = $1::uuid AND l.status = 'active'
"""
HOTELS_SQL = """
SELECT upper(h.code) AS hotel_code, upper(btrim(l.supplier_hotel_code)) AS dmc_code, h.id::text AS hotel_id
FROM booking.hotel_supplier_links l JOIN booking.hotels h ON h.id = l.hotel_id
WHERE l.supplier_id = $1::uuid AND l.status = 'active'
"""
ALIASES_SQL = """
SELECT t.hotel_id::text AS hotel_id, a.room_type_id::text AS id,
       lower(regexp_replace(btrim(a.alias), '\\s+', ' ', 'g')) AS alias, (a.supplier_id IS NOT NULL) AS own
FROM booking.hotel_room_type_aliases a
JOIN booking.hotel_room_types t ON t.id = a.room_type_id
JOIN booking.hotel_supplier_links l ON l.hotel_id = t.hotel_id AND l.supplier_id = $1::uuid AND l.status = 'active'
WHERE a.supplier_id IS NULL OR a.supplier_id = $1::uuid
"""
SERVICES_SQL = """
SELECT upper(s.code) AS code, s.id::text AS id, s.service_type, s.unit, s.status
FROM booking.services s WHERE s.supplier_id = $1::uuid
"""


class _Rooms:
    """The supplier's hotels and room types for the match (AA-1153): a hotel by the DMC's code, then the AA code; a
    room type by its name, then the supplier's alias, then an any-DMC alias."""

    def __init__(self, rows: list[Any], hotels: list[Any], aliases: list[Any]) -> None:
        self.hotel: dict[str, str] = {}
        for h in hotels:
            self.hotel.setdefault(h["hotel_code"], h["hotel_id"])
        for h in hotels:  # the DMC's code wins over an AA code that happens to read the same
            if h["dmc_code"]:
                self.hotel[h["dmc_code"]] = h["hotel_id"]
        self.room = {(r["hotel_id"], r["room_type"]): r for r in rows}
        self.names: dict[str, list[str]] = {}
        for r in rows:
            self.names.setdefault(r["hotel_id"], []).append(r["name"])
        by_id = {r["id"]: r for r in rows}
        self.alias: dict[tuple[str, str], Any] = {}
        for a in sorted(aliases, key=lambda a: not a["own"]):
            self.alias.setdefault((a["hotel_id"], a["alias"]), by_id.get(a["id"]))

    def find(self, code: str | None, room_type: str | None) -> tuple[str | None, Any]:
        hotel = self.hotel.get(code or "")
        if hotel is None:
            return None, None
        key = (hotel, _key(room_type))
        return hotel, self.room.get(key) or self.alias.get(key)

    def closest(self, hotel: str, room_type: str | None) -> list[str]:
        return get_close_matches(room_type or "", self.names.get(hotel, []), n=3, cutoff=0)


async def _match(conn: asyncpg.Connection, supplier_id: str, sheet: Sheet) -> list[dict[str, Any]]:
    rooms = _Rooms(
        list(await conn.fetch(ROOM_TYPES_SQL, supplier_id)),
        list(await conn.fetch(HOTELS_SQL, supplier_id)),
        list(await conn.fetch(ALIASES_SQL, supplier_id)),
    )
    services = {r["code"]: r for r in await conn.fetch(SERVICES_SQL, supplier_id)}
    currency = sheet.contract.get("currency")
    out: list[dict[str, Any]] = []
    for rate in sheet.rows:
        problem = None
        target: dict[str, Any] = {}
        if rate.sheet == HOTEL_SHEET:
            hotel, found = rooms.find(rate.hotel_code, rate.room_type)
            if hotel is None:
                problem = Problem(
                    rate.sheet,
                    rate.row,
                    "Hotel code",
                    "unknown_item",
                    f"no hotel {rate.hotel_code} sold by this supplier (link the supplier to the hotel first)",
                )
            elif found is None:
                near = ", ".join(rooms.closest(hotel, rate.room_type))
                problem = Problem(
                    rate.sheet,
                    rate.row,
                    "Room type",
                    "unknown_item",
                    f"no room type '{rate.room_type}' at hotel {rate.hotel_code}"
                    + (f" (closest: {near}; add an alias or a room type)" if near else ""),
                )
            elif found["status"] != "active" or found["hotel_status"] != "active":
                problem = Problem(
                    rate.sheet, rate.row, "Room type", "inactive_item", "the room type or hotel is inactive"
                )
            else:
                target = {"hotel_room_type_id": found["id"], "unit": "room_night"}
        else:
            found = services.get(rate.service_code or "")
            if found is None:
                problem = Problem(
                    rate.sheet,
                    rate.row,
                    "Service code",
                    "unknown_item",
                    f"no service {rate.service_code} of this supplier",
                )
            elif found["service_type"] != SERVICE_SHEETS[rate.sheet]:
                problem = Problem(
                    rate.sheet,
                    rate.row,
                    "Service code",
                    "wrong_sheet",
                    f"{rate.service_code} is a {found['service_type']} service, not on the {rate.sheet} sheet",
                )
            elif found["status"] != "active":
                problem = Problem(rate.sheet, rate.row, "Service code", "inactive_item", "the service is inactive")
            else:
                target = {"service_id": found["id"], "unit": found["unit"]}
        row_currency = rate.values.pop("currency", None)
        if problem is None and row_currency and currency and row_currency.upper() != currency:
            problem = Problem(
                rate.sheet,
                rate.row,
                "Currency",
                "currency_mismatch",
                f"the contract is in {currency}; this row says {row_currency}",
            )
        if problem is not None:
            sheet.problems.append(problem)
            continue
        out.append({"sheet": rate.sheet, "row": rate.row, **target, **rate.values})
    return out


def _preview(rate: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "sheet",
        "row",
        "hotel_room_type_id",
        "service_id",
        "unit",
        "season",
        "valid_from",
        "valid_to",
        "meal_plan",
        "pax_min",
        "pax_max",
        "net_amount",
        "notes",
        "pax_type",
        "age_min",
        "age_max",
        "published_amount",
        "commission_pct",
    )
    return {k: rate.get(k) for k in keys}


PREVIOUS_SQL = """
SELECT k.id::text AS id, k.title, k.valid_from, k.valid_to FROM booking.supplier_contracts k
WHERE k.supplier_id = $1::uuid AND k.status IN ('published', 'expired') AND k.valid_from < $2::date
  AND ($3::uuid IS NULL OR k.id <> $3::uuid)
ORDER BY k.valid_from DESC, k.published_at DESC NULLS LAST LIMIT 1
"""
CONTRACT_RATES_SQL = """
SELECT x.id::text AS id, x.hotel_room_type_id::text AS hotel_room_type_id, x.service_id::text AS service_id,
       x.meal_plan, x.pax_type, x.pax_min, x.pax_max, x.season, x.valid_from, x.valid_to, x.net_amount
FROM booking.supplier_rates x WHERE x.supplier_contract_id = $1::uuid
"""
LABELS_SQL = """
SELECT t.id::text AS id, h.code || ' ' || h.name || ' · ' || t.name AS label
FROM booking.hotel_room_types t JOIN booking.hotels h ON h.id = t.hotel_id WHERE t.id = ANY($1::uuid[])
UNION ALL
SELECT s.id::text, s.code || ' ' || s.title FROM booking.services s WHERE s.id = ANY($1::uuid[])
"""
SEASONS_OF_SQL = """
SELECT name, date_from, date_to FROM booking.supplier_contract_seasons WHERE supplier_contract_id = $1::uuid
ORDER BY date_from, lower(name)
"""
IMPORT_SQL = """
SELECT i.id::text AS id, i.file_sha256, i.file_name, i.rate_rows, i.checks::text AS checks, i.diff::text AS diff,
       i.previous_contract_id::text AS previous_contract_id, i.imported_by, i.imported_at
FROM booking.rate_sheet_imports i WHERE i.supplier_contract_id = $1::uuid
"""


async def _jump_pct(conn: asyncpg.Connection) -> Decimal:
    """``rate_sheet_jump_warn_pct`` in force today (503 ``policy_unavailable`` without it; never a literal)."""
    policy = await policy_values.require_staff(conn, await policy_values.today_utc(conn), JUMP_WARN_PCT)
    return Decimal(policy.integer(JUMP_WARN_PCT))


async def _labels(conn: asyncpg.Connection, rates_: list[dict[str, Any]]) -> dict[str, str]:
    ids = sorted({rate_checks.target(r) for r in rates_ if rate_checks.target(r)})
    return {r["id"]: r["label"] for r in await conn.fetch(LABELS_SQL, ids)} if ids else {}


async def _checks(
    conn: asyncpg.Connection,
    supplier_id: str,
    contract: dict[str, Any],
    rates_: list[dict[str, Any]],
    seasons: list[dict[str, Any]],
    *,
    exclude: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any] | None]:
    """The business checks and the diff of ``rates_`` against the supplier's previous published contract."""
    previous = await conn.fetchrow(PREVIOUS_SQL, supplier_id, contract.get("valid_from"), exclude)
    before = [dict(r) for r in await conn.fetch(CONTRACT_RATES_SQL, previous["id"])] if previous else []
    names = await _labels(conn, [*rates_, *before])
    findings = rate_checks.run(
        rates_,
        seasons=seasons,
        valid_from=contract.get("valid_from"),
        valid_to=contract.get("valid_to"),
        previous=before,
        jump_pct=await _jump_pct(conn),
        names=names,
    )
    diff = {**rate_checks.diff(rates_, before, names), "previous_contract": _contract_ref(previous)}
    return findings, diff, _contract_ref(previous)


def _contract_ref(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    return {"id": row["id"], "title": row["title"], "valid_from": row["valid_from"], "valid_to": row["valid_to"]}


def _children_matched(sheet: Sheet, hotels: dict[str, str]) -> list[dict[str, Any]]:
    out = []
    for child in sheet.children:
        hotel = hotels.get(child["hotel_code"])
        if hotel is None:
            sheet.problems.append(
                Problem(
                    CHILDREN_SHEET,
                    child["row"],
                    "Hotel code",
                    "unknown_item",
                    f"no hotel {child['hotel_code']} sold by this supplier",
                )  # fmt: skip
            )
            continue
        out.append({**{k: v for k, v in child.items() if k != "hotel_code"}, "hotel_id": hotel})
    return out


def _json_ready(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


async def import_sheet(
    conn: asyncpg.Connection,
    staff: Staff,
    supplier_ref: str,
    data: bytes,
    *,
    dry_run: bool,
    filename: str | None = None,
) -> dict[str, Any]:
    """Validate the workbook against the supplier's items; unless ``dry_run``, create the draft contract and its rates
    (call inside a transaction: any refusal rolls every row back). 422 ``rate_sheet_invalid`` lists every problem."""
    supplier = await suppliers.resolve_supplier(conn, supplier_ref)
    sheet = parse(data)
    matched = await _match(conn, supplier["id"], sheet) if sheet.rows else []
    hotels = _Rooms([], list(await conn.fetch(HOTELS_SQL, supplier["id"])), []).hotel if sheet.children else {}
    children = _children_matched(sheet, hotels)
    findings: list[dict[str, Any]] = []
    diff: dict[str, Any] = {}
    previous = None
    if not sheet.problems:
        # AA-1153: the business checks and the diff against the previous contract (an error blocks the import).
        findings, diff, previous = await _checks(conn, supplier["id"], sheet.contract, matched, sheet.seasons)
        for f in rate_checks.errors(findings):
            sheet.problems.append(Problem(f.get("sheet") or SEASONS_SHEET, f.get("row"), None, f["code"], f["message"]))
    if sheet.problems:
        raise Invalid(
            "rate_sheet_invalid",
            f"{len(sheet.problems)} problem(s) in the rate sheet; nothing was imported",
            errors=[p.out() for p in sheet.problems[:MAX_ERRORS]],
            ignored_sheets=sheet.ignored_sheets,
        )
    out: dict[str, Any] = {
        "dry_run": dry_run,
        "supplier": {"id": supplier["id"], "code": supplier["code"]},
        "contract": {k: sheet.contract.get(k) for k in CONTRACT_FIELDS.values()},
        "rates": [_preview(r) for r in matched],
        "seasons": [{k: s[k] for k in ("name", "date_from", "date_to")} for s in sheet.seasons],
        "child_prices": [
            {k: c[k] for k in ("hotel_id", "age_min", "age_max", "basis", "meal_plan", "net_amount")} for c in children
        ],  # fmt: skip
        "checks": findings,
        "diff": diff,
        "ignored_sheets": sheet.ignored_sheets,
    }
    if dry_run:
        # A child or infant rate never prices a trip plan component (bk-060): the impact counts adult rates only.
        adults = [r for r in matched if (r.get("pax_type") or "adult") == "adult"]
        out["impact"] = await price_readiness.impact(conn, sheet.contract, adults, on=cost_sheet.today())
        return out
    contract = await suppliers.create_contract(
        conn, staff, supplier["id"], {k: v for k, v in sheet.contract.items() if v is not None}
    )
    await _write_parts(conn, staff, contract["id"], sheet.seasons, children, sheet.ignored_sheets)
    for rate in matched:
        data_row = {k: v for k, v in rate.items() if k not in ("sheet", "row", "unit")}
        try:
            await rates.create_rate(conn, staff, contract["id"], data_row)
        except Invalid as exc:
            # A rule the service code checks (validity inside the contract, pax band, meal plan): name the row.
            raise Invalid(
                "rate_sheet_invalid",
                f"{rate['sheet']} row {rate['row']}: {exc.message}; nothing was imported",
                errors=[Problem(rate["sheet"], rate["row"], None, exc.code, exc.message).out()],
                ignored_sheets=sheet.ignored_sheets,
            ) from None
    await conn.execute(
        "INSERT INTO booking.rate_sheet_imports (supplier_id, supplier_contract_id, previous_contract_id, file_sha256,"
        " file_name, rate_rows, checks, diff, imported_by)"
        " VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5, $6, $7::jsonb, $8::jsonb, $9)",
        supplier["id"],
        contract["id"],
        previous["id"] if previous else None,
        hashlib.sha256(data).hexdigest(),
        (filename or "").strip()[:200] or None,
        len(matched),
        json.dumps(_json_ready(findings)),
        json.dumps(_json_ready(diff)),
        staff.cis_user_id,
    )
    out["contract"] = {"id": contract["id"], "status": contract["status"], "title": contract["title"]}
    out["imported"] = len(matched)
    return out


async def _write_parts(
    conn: asyncpg.Connection,
    staff: Staff,
    contract_id: str,
    seasons: list[dict[str, Any]],
    children: list[dict[str, Any]],
    ignored: list[str],
) -> None:
    """The contract's seasons and hotel child prices (draft only, bk-069 guard; a refusal names the row)."""
    try:
        for s in seasons:
            await conn.execute(
                "INSERT INTO booking.supplier_contract_seasons (supplier_contract_id, name, date_from, date_to,"
                " created_by) VALUES ($1::uuid, $2, $3, $4, $5)",
                contract_id,
                s["name"],
                s["date_from"],
                s["date_to"],
                staff.cis_user_id,
            )
        for c in children:
            await conn.execute(
                "INSERT INTO booking.hotel_child_policies (supplier_contract_id, hotel_id, age_min, age_max, basis,"
                " meal_plan, net_amount, created_by) VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8)",
                contract_id,
                c["hotel_id"],
                c["age_min"],
                c["age_max"],
                c["basis"],
                c["meal_plan"],
                c["net_amount"],
                staff.cis_user_id,
            )
    except (asyncpg.CheckViolationError, asyncpg.UniqueViolationError) as exc:
        raise Invalid(
            "rate_sheet_invalid",
            f"a season or child price was refused: {str(exc).splitlines()[0]}; nothing was imported",
            errors=[Problem(SEASONS_SHEET, None, None, "invalid_value", str(exc).splitlines()[0]).out()],
            ignored_sheets=ignored,
        ) from None


async def review(conn: asyncpg.Connection, contract_id: str) -> dict[str, Any]:
    """AA-1153: what the second person sees before publishing a draft: the business checks and the diff against the
    supplier's previous contract, live (the draft may have been edited since the import), its seasons, hotel child
    prices and the import it came from (with the checks and diff as they were at import time). ``acknowledge`` is true
    when publishing it needs the warnings acknowledged."""
    contract = await suppliers.get_contract(conn, contract_id)
    current = [dict(r) for r in await conn.fetch(CONTRACT_RATES_SQL, contract["id"])]
    seasons = [dict(r) for r in await conn.fetch(SEASONS_OF_SQL, contract["id"])]
    findings, diff, previous = await _checks(
        conn, contract["supplier_id"], contract, current, seasons, exclude=contract["id"]
    )
    row = await conn.fetchrow(IMPORT_SQL, contract["id"])
    imported = None
    if row is not None:
        imported = {**dict(row), "checks": json.loads(row["checks"]), "diff": json.loads(row["diff"])}
    children = await conn.fetch(
        "SELECT p.id::text AS id, p.hotel_id::text AS hotel_id, h.code AS hotel_code, h.name AS hotel_name, p.age_min,"
        " p.age_max, p.basis, p.meal_plan, p.net_amount::text AS net_amount FROM booking.hotel_child_policies p"
        " JOIN booking.hotels h ON h.id = p.hotel_id WHERE p.supplier_contract_id = $1::uuid"
        " ORDER BY h.code, p.meal_plan NULLS FIRST, p.age_min",
        contract["id"],
    )
    return {
        "contract": {k: contract[k] for k in ("id", "title", "status", "valid_from", "valid_to", "currency")},
        "previous_contract": previous,
        "checks": findings,
        "diff": diff,
        "seasons": seasons,
        "child_prices": [dict(r) for r in children],
        "import": imported,
        "acknowledge": imported is not None and bool(rate_checks.warnings(findings)),
    }


# ── Template ─────────────────────────────────────────────────────────────────

CONTRACT_HELP = (
    ("Title", "required, e.g. 2027 rates"),
    ("Reference", "optional, the supplier's own contract number"),
    ("Valid from", "required, YYYY-MM-DD"),
    ("Valid to", "optional, YYYY-MM-DD; empty = open-ended"),
    ("Currency", "required, the supplier's currency (USD, VND, THB, ...)"),
    ("Payment terms", "optional"),
    ("Cancellation policy", "optional"),
)


def build_workbook(
    contract: dict[str, Any] | None = None,
    hotel_rows: list[tuple[Any, ...]] | None = None,
    service_rows: dict[str, list[tuple[Any, ...]]] | None = None,
    season_rows: list[tuple[Any, ...]] | None = None,
    child_rows: list[tuple[Any, ...]] | None = None,
) -> bytes:
    """An AA rate-sheet workbook: the empty template, or (tests, fixtures) one filled with the given rows (AA-1153:
    with the Seasons and Hotel children sheets)."""
    wb = Workbook()
    ws = wb.active
    ws.title = CONTRACT_SHEET
    values = contract or {}
    for label, hint in CONTRACT_HELP:
        ws.append([label, values.get(label, ""), hint if not contract else None])
    hotel = wb.create_sheet(HOTEL_SHEET)
    hotel.append(list(HOTEL_COLUMNS))
    for row in hotel_rows or []:
        hotel.append(list(row))
    for name in SERVICE_SHEETS:
        sheet = wb.create_sheet(name)
        sheet.append(list(SERVICE_COLUMNS))
        for row in (service_rows or {}).get(name, []):
            sheet.append(list(row))
    for title, columns, extra in (
        (SEASONS_SHEET, SEASON_COLUMNS, season_rows),
        (CHILDREN_SHEET, CHILDREN_COLUMNS, child_rows),
    ):
        sheet = wb.create_sheet(title)
        sheet.append(list(columns))
        for row in extra or []:
            sheet.append(list(row))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def template() -> bytes:
    return build_workbook()
