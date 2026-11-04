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

Items are matched to the supplier's **existing** hotels (by code, ``HTL-000123``, then the room type name, case and
spaces ignored) and services (by code, ``SVC-000123``): supply facts are entered and checked in the admin (ADR 0004a),
a rate sheet only prices them. Every problem is reported per sheet, row and column (``errors``) and **nothing is
written** while one is left; ``dry_run`` validates and previews without writing. Bounded: at most :data:`MAX_BYTES`
and :data:`MAX_ROWS` rows. The workbook is read with openpyxl in read-only, values-only mode (no formulas are run, no
macros: an ``.xlsm`` is refused by its content type and openpyxl never executes VBA).
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import asyncpg
from openpyxl import Workbook, load_workbook

from app.auth.staff import Staff
from app.errors import Invalid
from app.suppliers import service as suppliers
from app.supply import rates

MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 2000
CONTRACT_SHEET = "Contract"
HOTEL_SHEET = "Hotel"
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
)
SERVICE_COLUMNS = ("Service code", "Season", "Valid from", "Valid to", "Pax min", "Pax max", "Net", "Notes")
REQUIRED = {HOTEL_SHEET: ("Hotel code", "Room type", "Net"), "service": ("Service code", "Net")}
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
        ):
            if label == "Meal plan" and sheet != HOTEL_SHEET:
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
            ("Net", "net_amount" in rate.values or any(p.row == n and p.column == "Net" for p in problems)),
        ):
            if not have:
                problems.append(Problem(sheet, n, label, "required", f"{label} is required"))
        if (meal := rate.values.get("meal_plan")) is not None:
            rate.values["meal_plan"] = str(meal).upper()
        out.append(rate)
    return out


def parse(data: bytes) -> Sheet:
    """Read the workbook; shape problems are collected (never raised), except a file that is not a workbook."""
    wb = _open(data)
    problems: list[Problem] = []
    ignored: list[str] = []
    contract: dict[str, Any] = {}
    rows: list[RateRow] = []
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
            elif sheet := next((s for s in SERVICE_SHEETS if _key(s) == key), None):
                rows += _rows(ws, sheet, problems, budget)
            else:
                ignored.append(name)
    finally:
        wb.close()
    if not rows and not any(p.code == "missing_column" for p in problems):
        problems.append(Problem(HOTEL_SHEET, None, None, "no_rates", "the workbook holds no rate rows"))
    return Sheet(contract=contract, rows=rows, problems=problems, ignored_sheets=ignored)


# ── Matching and writing ─────────────────────────────────────────────────────

ROOM_TYPES_SQL = """
SELECT upper(h.code) AS hotel_code, lower(regexp_replace(btrim(t.name), '\\s+', ' ', 'g')) AS room_type,
       t.id::text AS id, t.status, h.status AS hotel_status
FROM booking.hotel_room_types t JOIN booking.hotels h ON h.id = t.hotel_id
WHERE h.supplier_id = $1::uuid
"""
SERVICES_SQL = """
SELECT upper(s.code) AS code, s.id::text AS id, s.service_type, s.unit, s.status
FROM booking.services s WHERE s.supplier_id = $1::uuid
"""


async def _match(conn: asyncpg.Connection, supplier_id: str, sheet: Sheet) -> list[dict[str, Any]]:
    rooms = {(r["hotel_code"], r["room_type"]): r for r in await conn.fetch(ROOM_TYPES_SQL, supplier_id)}
    services = {r["code"]: r for r in await conn.fetch(SERVICES_SQL, supplier_id)}
    currency = sheet.contract.get("currency")
    out: list[dict[str, Any]] = []
    for rate in sheet.rows:
        problem = None
        target: dict[str, Any] = {}
        if rate.sheet == HOTEL_SHEET:
            found = rooms.get((rate.hotel_code or "", _key(rate.room_type)))
            if found is None:
                problem = Problem(
                    rate.sheet,
                    rate.row,
                    "Room type",
                    "unknown_item",
                    f"no room type '{rate.room_type}' at hotel {rate.hotel_code} of this supplier",
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
    )
    return {k: rate.get(k) for k in keys}


async def import_sheet(
    conn: asyncpg.Connection, staff: Staff, supplier_ref: str, data: bytes, *, dry_run: bool
) -> dict[str, Any]:
    """Validate the workbook against the supplier's items; unless ``dry_run``, create the draft contract and its rates
    (call inside a transaction: any refusal rolls every row back). 422 ``rate_sheet_invalid`` lists every problem."""
    supplier = await suppliers.resolve_supplier(conn, supplier_ref)
    sheet = parse(data)
    matched = await _match(conn, supplier["id"], sheet) if sheet.rows else []
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
        "ignored_sheets": sheet.ignored_sheets,
    }
    if dry_run:
        return out
    contract = await suppliers.create_contract(
        conn, staff, supplier["id"], {k: v for k, v in sheet.contract.items() if v is not None}
    )
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


def build_workbook(
    contract: dict[str, Any] | None = None,
    hotel_rows: list[tuple[Any, ...]] | None = None,
    service_rows: dict[str, list[tuple[Any, ...]]] | None = None,
) -> bytes:
    """An AA rate-sheet workbook: the empty template, or (tests, fixtures) one filled with the given rows."""
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
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def template() -> bytes:
    return build_workbook()
