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
- optional sheet ``Inventory`` (AA-1153 part 2, :mod:`app.supply.rate_sheet_inventory`): the contract's allotment,
  free-sale and on-request terms per room type or service and dates (an allotment without release days takes the
  policy default), written under the draft with its rates.
- **Two-way** (AA-1153 part 2): ``GET /booking/contracts/{id}/rate-sheet`` exports a contract in this template
  (:mod:`app.supply.rate_sheet_export`); edited and imported back it becomes a new draft, with the checks and the
  diff against the supplier's previous contract.
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
Bounded: at most :data:`MAX_BYTES` (and :data:`MAX_UNPACKED_BYTES` once unzipped) and the pricing policy
``rate_sheet_max_rows`` rows (AA-1153 part 2, bk-072; was a code constant), counted over the rate sheets, ``Seasons``,
``Hotel children`` and ``Inventory`` together and again after a season label is expanded into its
ranges (422 ``rate_sheet_too_many_rows``; AA-1153 review: one upload must not hold the API worker). The workbook is read
with openpyxl in read-only, values-only mode (no formulas are run, no macros: an ``.xlsm`` is refused by its content
type and openpyxl never executes VBA).
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import asyncpg
from openpyxl import Workbook, load_workbook

from app.auth.staff import Staff
from app.b2b.tariff import FORMULA_START
from app.catalog import cost_sheet, price_readiness
from app.errors import Invalid
from app.pricing import policy_values
from app.pricing.policy_params_hotels import SHEET_MAX_ROWS
from app.suppliers import service as suppliers
from app.supply import rate_checks, rate_review, rate_sheet_inventory, rate_sheet_match, rates, restrictions
from app.supply.rate_sheet_cells import (  # noqa: F401 — re-exported: activity_import reads the cell helpers here
    AGE_MAX,
    PAX_TYPES,
    Budget,
    Problem,
    _age,
    _amount,
    _date,
    _int,
    _key,
    _pax_type,
    _pct,
    _table,
    _text,
)

MAX_BYTES = 2 * 1024 * 1024
MAX_UNPACKED_BYTES = 40 * 1024 * 1024  # technical-constant: zip bomb guard (a 2 MB sheet of 2000 rows unzips far below)
CONTRACT_SHEET = "Contract"
HOTEL_SHEET = "Hotel"
SEASONS_SHEET = rate_review.SEASONS
CHILDREN_SHEET = rate_review.CHILDREN
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
MAX_ERRORS = 200


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
    inventory: list[dict[str, Any]] = field(default_factory=list)  # AA-1153 part 2: allotment / free sale / on request


# ── Parsing (no database) ────────────────────────────────────────────────────


def _open(data: bytes) -> Any:
    if not data:
        raise Invalid("rate_sheet_empty", "upload an .xlsx rate sheet")
    if len(data) > MAX_BYTES:
        raise Invalid("rate_sheet_too_large", f"a rate sheet is at most {MAX_BYTES // (1024 * 1024)} MB")
    if not zipfile.is_zipfile(io.BytesIO(data)):
        raise Invalid("rate_sheet_not_xlsx", "the file is not an .xlsx workbook")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            unpacked = sum(info.file_size for info in archive.infolist())
    except zipfile.BadZipFile:
        raise Invalid("rate_sheet_not_xlsx", "the file is not an .xlsx workbook") from None
    if unpacked > MAX_UNPACKED_BYTES:
        raise Invalid("rate_sheet_too_large", "the rate sheet is too large once unpacked; split it into several sheets")
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


def _rows(ws: Any, sheet: str, problems: list[Problem], budget: Budget) -> list[RateRow]:
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
        budget.take()
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


def parse(data: bytes, max_rows: int | None = None) -> Sheet:
    """Read the workbook; shape problems are collected (never raised), except a file that is not a workbook.
    ``max_rows``: the pricing policy ``rate_sheet_max_rows`` the import passes (None: no row cap)."""
    wb = _open(data)
    problems: list[Problem] = []
    ignored: list[str] = []
    contract: dict[str, Any] = {}
    rows: list[RateRow] = []
    seasons: list[dict[str, Any]] = []
    children: list[dict[str, Any]] = []
    budget = Budget(max_rows)
    inventory: list[dict[str, Any]] = []
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
                seasons = _seasons(ws, problems, budget)
            elif key == _key(CHILDREN_SHEET):
                children = _children(ws, problems, budget)
            elif key == _key(rate_sheet_inventory.SHEET):
                inventory = rate_sheet_inventory.read(ws, problems, budget)
            elif sheet := next((s for s in SERVICE_SHEETS if _key(s) == key), None):
                rows += _rows(ws, sheet, problems, budget)
            else:
                ignored.append(name)
    finally:
        wb.close()
    if not rows and not any(p.code == "missing_column" for p in problems):
        problems.append(Problem(HOTEL_SHEET, None, None, "no_rates", "the workbook holds no rate rows"))
    rows = _expand_seasons(rows, seasons, problems, max_rows)
    return Sheet(contract, rows, problems, ignored, seasons, children, inventory)


def _seasons(ws: Any, problems: list[Problem], budget: Budget) -> list[dict[str, Any]]:
    out = []
    seen: set[tuple[str, date]] = set()
    for n, cells in _table(ws, SEASONS_SHEET, SEASON_COLUMNS, problems, budget):
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
        if (_key(name), start) in seen:  # the bk-069 key (contract, name, start): one row per range
            problems.append(Problem(SEASONS_SHEET, n, None, "duplicate_season", f"{name} from {start} is listed twice"))
            continue
        seen.add((_key(name), start))
        out.append({"name": name, "date_from": start, "date_to": end, "sheet": SEASONS_SHEET, "row": n})
    return out


def _children(ws: Any, problems: list[Problem], budget: Budget) -> list[dict[str, Any]]:
    out = []
    seen: set[tuple[str, str | None, str, int]] = set()
    for n, cells in _table(ws, CHILDREN_SHEET, CHILDREN_COLUMNS, problems, budget):
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
        key = (code, board, basis, age_min)  # the bk-069 key: one price per hotel, board, basis and band start
        if key in seen:
            why = "this hotel, meal plan, basis and age min already have a child price"
            problems.append(Problem(CHILDREN_SHEET, n, None, "duplicate_child_price", why))
            continue
        seen.add(key)
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


def _expand_seasons(
    rows: list[RateRow], seasons: list[dict[str, Any]], problems: list[Problem], max_rows: int | None
) -> list[RateRow]:
    """A rate row naming a season of the Seasons sheet, with no dates, becomes one rate per range of that season (at
    most ``max_rows`` rates once expanded: 422 ``rate_sheet_too_many_rows`` as soon as the list passes it)."""
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
        if max_rows is not None and len(out) + len(ranges) > max_rows:
            raise Invalid(
                "rate_sheet_too_many_rows",
                f"a rate sheet has at most {max_rows} rates once its seasons are expanded into their date ranges",
            )
        for r in ranges:
            values = {**rate.values, "season": r["name"], "valid_from": r["date_from"], "valid_to": r["date_to"]}
            out.append(RateRow(rate.sheet, rate.row, rate.hotel_code, rate.room_type, rate.service_code, values))
    return out


# ── Matching and writing ─────────────────────────────────────────────────────

SERVICES_SQL = """
SELECT upper(s.code) AS code, s.id::text AS id, s.service_type, s.unit, s.status
FROM booking.services s WHERE s.supplier_id = $1::uuid
"""


async def _match(conn: asyncpg.Connection, supplier_id: str, sheet: Sheet) -> list[dict[str, Any]]:
    rooms = await rate_sheet_match.load(conn, supplier_id)
    services = {r["code"]: r for r in await conn.fetch(SERVICES_SQL, supplier_id)}
    currency = sheet.contract.get("currency")
    out: list[dict[str, Any]] = []
    for rate in sheet.rows:
        if rate.sheet == HOTEL_SHEET:
            target, why = rooms.target(rate.hotel_code, rate.room_type)  # AA-1153: linked hotels, aliases
        else:
            target, why = _service_target(services, rate)
        row_currency = rate.values.pop("currency", None)
        if why is None and row_currency and currency and row_currency.upper() != currency:
            why = ("Currency", "currency_mismatch", f"the contract is in {currency}; this row says {row_currency}")
        if why is not None:
            sheet.problems.append(Problem(rate.sheet, rate.row, *why))
            continue
        out.append({"sheet": rate.sheet, "row": rate.row, **target, **rate.values})
    return out


def _service_target(services: dict[str, Any], rate: RateRow) -> tuple[dict[str, Any], tuple[str, str, str] | None]:
    """The service of a service-sheet row, or why not (column, code, message)."""
    found = services.get(rate.service_code or "")
    if found is None:
        return {}, ("Service code", "unknown_item", f"no service {rate.service_code} of this supplier")
    if found["service_type"] != SERVICE_SHEETS[rate.sheet]:
        why = f"{rate.service_code} is a {found['service_type']} service, not on the {rate.sheet} sheet"
        return {}, ("Service code", "wrong_sheet", why)
    if found["status"] != "active":
        return {}, ("Service code", "inactive_item", "the service is inactive")
    return {"service_id": found["id"], "unit": found["unit"]}, None


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


def _children_matched(sheet: Sheet, hotels: dict[str, str]) -> list[dict[str, Any]]:
    out = []
    for child in sheet.children:
        if (hotel := hotels.get(child["hotel_code"])) is None:
            why = f"no hotel {child['hotel_code']} sold by this supplier"
            sheet.problems.append(Problem(CHILDREN_SHEET, child["row"], "Hotel code", "unknown_item", why))
            continue
        out.append({**{k: v for k, v in child.items() if k != "hotel_code"}, "hotel_id": hotel})
    return out


async def max_rows(conn: asyncpg.Connection) -> int:
    """``rate_sheet_max_rows`` in force today (bk-072; 503 ``policy_unavailable`` without it; never a literal)."""
    policy = await policy_values.require_staff(conn, await policy_values.today_utc(conn), SHEET_MAX_ROWS)
    return policy.integer(SHEET_MAX_ROWS)


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
    sheet = parse(data, await max_rows(conn))
    matched = await _match(conn, supplier["id"], sheet) if sheet.rows else []
    hotels = (await rate_sheet_match.load(conn, supplier["id"])).hotel if sheet.children else {}
    children = _children_matched(sheet, hotels)
    terms = await rate_sheet_inventory.match(conn, supplier["id"], sheet.contract, sheet.inventory, sheet.problems)
    inventory = rate_sheet_inventory.preview(terms, await restrictions.default_release_days(conn)) if terms else []
    findings: list[dict[str, Any]] = []
    diff: dict[str, Any] = {}
    previous = None
    if not sheet.problems:
        # AA-1153: the business checks and the diff against the previous contract (an error blocks the import);
        # S291: the Inventory terms are compared too (an allotment cut from 5 rooms to 2 is listed).
        found = await rate_review.checks(conn, supplier["id"], sheet.contract, matched, sheet.seasons, terms=inventory)
        findings, diff, previous = found
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
        "inventory": inventory,
    }
    if dry_run:
        # A child or infant rate never prices a trip plan component (bk-060): the impact counts adult rates only.
        adults = [r for r in matched if (r.get("pax_type") or "adult") == "adult"]
        out["impact"] = await price_readiness.impact(conn, sheet.contract, adults, on=cost_sheet.today())
        return out
    contract = await suppliers.create_contract(
        conn, staff, supplier["id"], {k: v for k, v in sheet.contract.items() if v is not None}
    )
    await rate_review.write_parts(conn, staff, contract["id"], sheet.seasons, children, sheet.ignored_sheets)
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
        json.dumps(rate_review.json_ready(findings)),
        json.dumps(rate_review.json_ready(diff)),
        staff.cis_user_id,
    )
    await rate_sheet_inventory.write(conn, staff, contract["id"], terms)
    out["contract"] = {"id": contract["id"], "status": contract["status"], "title": contract["title"]}
    out["imported"] = len(matched)
    return out


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


def _append(sheet: Any, row: list[Any]) -> None:
    """Append a row; a text that starts like a formula (``=``, ``+``, ``-``, ``@``, tab, CR) stays text. openpyxl stores
    a str starting with "=" as a formula, and an export writes free text a DMC sent (titles, notes, season and room
    names): a string cell with Excel's quote prefix, the value unchanged, so the round trip still matches (as
    :mod:`app.catalog.price_import` does for the price workbook)."""
    sheet.append(row)
    for cell in sheet[sheet.max_row]:
        if isinstance(cell.value, str) and cell.value.startswith(FORMULA_START):
            cell.data_type = "s"
            cell.quotePrefix = True


def build_workbook(
    contract: dict[str, Any] | None = None,
    hotel_rows: list[tuple[Any, ...]] | None = None,
    service_rows: dict[str, list[tuple[Any, ...]]] | None = None,
    season_rows: list[tuple[Any, ...]] | None = None,
    child_rows: list[tuple[Any, ...]] | None = None,
    inventory_rows: list[tuple[Any, ...]] | None = None,
) -> bytes:
    """An AA rate-sheet workbook: the empty template, or one filled with the given rows (tests, fixtures, and the
    export of a contract, :mod:`app.supply.rate_sheet_export`; AA-1153: with the Seasons, Hotel children and
    Inventory sheets). Text cells never become formulas (:func:`_append`)."""
    wb = Workbook()
    ws = wb.active
    ws.title = CONTRACT_SHEET
    values = contract or {}
    for label, hint in CONTRACT_HELP:
        _append(ws, [label, values.get(label, ""), hint if not contract else None])
    hotel = wb.create_sheet(HOTEL_SHEET)
    hotel.append(list(HOTEL_COLUMNS))
    for row in hotel_rows or []:
        _append(hotel, list(row))
    for name in SERVICE_SHEETS:
        sheet = wb.create_sheet(name)
        sheet.append(list(SERVICE_COLUMNS))
        for row in (service_rows or {}).get(name, []):
            _append(sheet, list(row))
    for title, columns, extra in (
        (SEASONS_SHEET, SEASON_COLUMNS, season_rows),
        (CHILDREN_SHEET, CHILDREN_COLUMNS, child_rows),
        (rate_sheet_inventory.SHEET, rate_sheet_inventory.COLUMNS, inventory_rows),
    ):
        sheet = wb.create_sheet(title)
        sheet.append(list(columns))
        for row in extra or []:
            _append(sheet, list(row))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def template() -> bytes:
    return build_workbook()
