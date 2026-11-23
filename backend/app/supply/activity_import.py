"""Activity catalogue Excel import (AA-1151, bk-068; S285 advisory PR-29 "Import Excel"): one AA workbook of a
supplier's activities -> per row one **activity service**, one **draft add-on offer** with its places and sale rules,
and its net rates in one **draft supplier contract**. Everything goes through the same service code as the admin forms
(``records.create_record``, ``addons.create_offer``, ``suppliers.create_contract``, ``rates.create_rate``), so every
rule and audit row is the same. Nothing sells until a second person publishes the contract (bk-008 / bk-064) and each
offer (bk-068): the importer is the maker of all of them.

Workbook (:func:`template`; ``GET /booking/activity-sheets/template``):

- sheet ``Contract``: the rate-sheet contract block (:mod:`app.supply.rate_sheet`: Title, Reference, Valid from, Valid
  to, Currency, Payment terms, Cancellation policy);
- sheet ``Activities``, one row per activity (headers matched by name, any order): ``Title`` and ``Places`` (CIS place
  ids or exact CIS place names, ``;`` between them; the first is the service's place), ``Unit`` (person, group,
  vehicle, day; empty = person), ``Duration minutes``, ``Max group size``, ``Inclusions`` (``;`` between them),
  ``Min pax`` (empty = 1), ``Cut-off hours`` (required), ``Weekdays`` (``Mon, Wed`` or ``1,3``; empty = daily),
  ``Start times`` (``08:00, 13:30``; empty = flexible), ``Net adult`` (required), ``Net child`` (a child rate, per
  person only) with ``Child age max``, ``Child %`` (the offer's B4 child % when the supplier gives no child rate),
  ``Season``, ``Valid from``, ``Valid to`` (empty = the contract's).

Every problem is reported per sheet, row and column and **nothing is written** while one is left (one transaction:
a refusal while writing rolls every row back); ``dry_run`` validates and previews only. A title the supplier already
has at that place is a problem (``service_exists``): an import never changes an existing activity. Bounded by the
rate sheet's file size and :data:`MAX_ROWS`.
"""

from __future__ import annotations

import io
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import asyncpg
from openpyxl import Workbook

from app.auth.staff import Staff
from app.catalog import destinations as catalog
from app.errors import Invalid, ServiceError
from app.suppliers import service as suppliers
from app.supply import addons, rate_sheet, rates, records
from app.supply.rate_sheet import Problem

SHEET = "Activities"
MAX_ROWS = 500  # technical-constant: rows one upload may hold (an activity list, not a tariff; the file is <= 2 MB)
MAX_ERRORS = 200  # technical-constant: problems listed in one 422 (the rate sheet's bound)
UNITS = ("person", "group", "vehicle", "day")
COLUMNS = (
    "Title",
    "Places",
    "Unit",
    "Duration minutes",
    "Max group size",
    "Inclusions",
    "Min pax",
    "Cut-off hours",
    "Weekdays",
    "Start times",
    "Net adult",
    "Net child",
    "Child age max",
    "Child %",
    "Season",
    "Valid from",
    "Valid to",
)
REQUIRED = ("Title", "Places", "Cut-off hours", "Net adult")
SEP = ";"
PLACE_BY_NAME_SQL = f"{catalog.DESTINATION_SQL} WHERE lower(d.name) = lower($1) AND rd.keeper_id IS NULL"  # noqa: S608


@dataclass
class Row:
    row: int
    title: str | None = None
    places: list[str] = field(default_factory=list)
    service: dict[str, Any] = field(default_factory=dict)
    rules: dict[str, Any] = field(default_factory=dict)
    adult: dict[str, Any] = field(default_factory=dict)
    child: dict[str, Any] | None = None
    resolved: list[dict[str, Any]] = field(default_factory=list)


def _items(value: Any) -> list[str]:
    text = rate_sheet._text(value) or ""
    return [t.strip() for t in text.split(SEP) if t.strip()]


def _hours(value: Any) -> int | None:
    if value is None or value == "":
        return None
    number = Decimal(str(value).strip())
    if number != number.to_integral_value() or not 0 <= number <= addons.CUTOFF_HOURS_MAX:
        raise ValueError(f"a whole number of hours from 0 to {addons.CUTOFF_HOURS_MAX} is expected")
    return int(number)


def _minutes(value: Any) -> int | None:
    if value is None or value == "":
        return None
    number = Decimal(str(value).strip())
    if number != number.to_integral_value() or not 1 <= number <= 43200:
        raise ValueError("a whole number of minutes from 1 to 43200 is expected")
    return int(number)


def _optional(parse: Any) -> Any:
    """A parser that leaves an empty cell empty."""
    return lambda v: parse(v) if rate_sheet._text(v) else None


# (column, parser, part of the row, key): the cells each activity row reads (Title, Places, Unit, Inclusions apart).
CELLS: tuple[tuple[str, Any, str, str], ...] = (
    ("Duration minutes", _minutes, "service", "duration_minutes"),
    ("Max group size", rate_sheet._int, "service", "max_group_size"),
    ("Min pax", rate_sheet._int, "rules", "min_pax"),
    ("Cut-off hours", _hours, "rules", "cutoff_hours"),
    ("Weekdays", _optional(addons.parse_weekdays), "rules", "operating_weekdays"),
    ("Start times", _optional(addons.parse_start_times), "rules", "start_times"),
    ("Child %", addons._pct, "rules", "child_pct"),
    ("Net adult", rate_sheet._amount, "adult", "net_amount"),
    ("Season", rate_sheet._text, "adult", "season"),
    ("Valid from", rate_sheet._date, "adult", "valid_from"),
    ("Valid to", rate_sheet._date, "adult", "valid_to"),
    ("Net child", rate_sheet._amount, "child", "net_amount"),
    ("Child age max", rate_sheet._age, "child", "age_max"),
)


class _Reader:
    """The cells of one header layout (columns matched by name, any order)."""

    def __init__(self, header: tuple[Any, ...]) -> None:
        self.columns = {rate_sheet._key(h): i for i, h in enumerate(header) if h is not None}

    def missing(self) -> list[str]:
        return [c for c in REQUIRED if rate_sheet._key(c) not in self.columns]

    def cell(self, values: tuple[Any, ...], name: str) -> Any:
        i = self.columns.get(rate_sheet._key(name))
        return values[i] if i is not None and i < len(values) else None


def _take(value: Any, parse: Any, row: int, label: str, problems: list[Problem]) -> Any:
    try:
        return parse(value)
    except (ValueError, ArithmeticError, ServiceError) as exc:
        message = exc.message if isinstance(exc, ServiceError) else str(exc) or "invalid value"
        problems.append(Problem(SHEET, row, label, "invalid_value", message))
        return None


def _child(r: Row, child: dict[str, Any], problems: list[Problem]) -> None:
    """B4: a child rate (per person only) takes the adult rate's season and validity."""
    if "net_amount" in child:
        if r.service["unit"] != "person":
            problems.append(
                Problem(SHEET, r.row, "Net child", "child_rate_per_person", "a child rate needs unit person")
            )
        r.child = {"pax_type": "child", **child, **{k: v for k, v in r.adult.items() if k != "net_amount"}}
    elif "age_max" in child:
        problems.append(Problem(SHEET, r.row, "Child age max", "child_rate_missing", "give Net child with its age"))


def _required(r: Row, problems: list[Problem]) -> None:
    have = {
        "Title": r.title,
        "Places": r.places,
        "Cut-off hours": "cutoff_hours" in r.rules,
        "Net adult": "net_amount" in r.adult,
    }
    for label in REQUIRED:
        if not have[label] and not any(p.row == r.row and p.column == label for p in problems):
            problems.append(Problem(SHEET, r.row, label, "required", f"{label} is required"))


def _row(reader: _Reader, n: int, values: tuple[Any, ...], problems: list[Problem]) -> Row:
    r = Row(row=n, title=rate_sheet._text(reader.cell(values, "Title")), places=_items(reader.cell(values, "Places")))
    unit = (rate_sheet._text(reader.cell(values, "Unit")) or "person").lower()
    if unit not in UNITS:
        problems.append(Problem(SHEET, n, "Unit", "invalid_value", f"one of {', '.join(UNITS)} is expected"))
    r.service = {
        "service_type": "activity",
        "title": r.title,
        "unit": unit,
        "inclusions": _items(reader.cell(values, "Inclusions")),
    }
    child: dict[str, Any] = {}
    parts = {"service": r.service, "rules": r.rules, "adult": r.adult, "child": child}
    for label, parse, part, key in CELLS:
        value = _take(reader.cell(values, label), parse, n, label, problems)
        if value is not None:
            parts[part][key] = value
    _child(r, child, problems)
    _required(r, problems)
    return r


def _blank(values: tuple[Any, ...] | None) -> bool:
    return values is None or all(v is None or str(v).strip() == "" for v in values)


def _rows(ws: Any, problems: list[Problem]) -> list[Row]:
    rows = ws.iter_rows(values_only=True)
    header = next(rows, None)
    if header is None:
        problems.append(Problem(SHEET, None, None, "no_rows", "the Activities sheet is empty"))
        return []
    reader = _Reader(header)
    if missing := reader.missing():
        problems.append(
            Problem(SHEET, 1, ", ".join(missing), "missing_column", f"missing column(s): {', '.join(missing)}")
        )
        return []
    out: list[Row] = []
    for n, values in enumerate(rows, start=2):
        if _blank(values):
            continue
        if len(out) >= MAX_ROWS:
            raise Invalid("activity_sheet_too_many_rows", f"an activity sheet has at most {MAX_ROWS} rows")
        out.append(_row(reader, n, values, problems))
    if not out and not problems:
        problems.append(Problem(SHEET, None, None, "no_rows", "the Activities sheet holds no activity"))
    return out


@dataclass
class Sheet:
    contract: dict[str, Any]
    rows: list[Row]
    problems: list[Problem]
    ignored_sheets: list[str]


def parse(data: bytes) -> Sheet:
    """Read the workbook; shape problems are collected (never raised), except a file that is not a workbook."""
    wb = rate_sheet._open(data)
    problems: list[Problem] = []
    ignored: list[str] = []
    contract: dict[str, Any] = {}
    rows: list[Row] = []
    names = {rate_sheet._key(n): n for n in wb.sheetnames}
    try:
        for want in (rate_sheet.CONTRACT_SHEET, SHEET):
            if rate_sheet._key(want) not in names:
                problems.append(Problem(want, None, None, "missing_sheet", f"the {want} sheet is missing"))
        for key, name in names.items():
            if key == rate_sheet._key(rate_sheet.CONTRACT_SHEET):
                contract = rate_sheet._contract(wb[name], problems)
            elif key == rate_sheet._key(SHEET):
                rows = _rows(wb[name], problems)
            else:
                ignored.append(name)
    finally:
        wb.close()
    return Sheet(contract=contract, rows=rows, problems=problems, ignored_sheets=ignored)


# ── Matching (reads only) ────────────────────────────────────────────────────


async def _place(conn: asyncpg.Connection, token: str) -> tuple[dict[str, Any] | None, str | None]:
    """A CIS place by id or exact name: ``(place, None)`` or ``(None, problem text)``."""
    try:
        place_id = str(uuid.UUID(token))
    except ValueError:
        place_id = None
    if place_id:
        try:
            return await catalog.require_destination(conn, place_id), None
        except ServiceError as exc:
            return None, exc.message
    try:
        async with conn.transaction():
            found = await conn.fetch(PLACE_BY_NAME_SQL, token)
    except catalog.CATALOG_ERRORS:
        return None, "the CIS catalog cannot be read right now; try again later"
    if not found:
        return None, f"no CIS place named '{token}'"
    if len(found) > 1:
        ids = ", ".join(r["destination_id"] for r in found[:5])
        return None, f"several CIS places are named '{token}'; use the id ({ids})"
    return dict(found[0]), None


async def _resolve_places(conn: asyncpg.Connection, sheet: Sheet) -> None:
    cache: dict[str, tuple[dict[str, Any] | None, str | None]] = {}
    for r in sheet.rows:
        for token in r.places:
            if token not in cache:
                cache[token] = await _place(conn, token)
            place, problem = cache[token]
            if problem:
                sheet.problems.append(Problem(SHEET, r.row, "Places", "unknown_place", problem))
            elif place is not None:
                r.resolved.append(place)


def _duplicates(sheet: Sheet, existing: set[tuple[str, str | None]]) -> None:
    """A title the supplier already has at that place, or twice in the file, is a problem (no silent update)."""
    seen: set[tuple[str, str | None]] = set()
    for r in sheet.rows:
        if not r.title or not r.resolved:
            continue
        key = (r.title.strip().lower(), r.resolved[0]["destination_id"])
        if key in existing:
            sheet.problems.append(
                Problem(SHEET, r.row, "Title", "service_exists", "the supplier already has this activity at this place")
            )
        elif key in seen:
            sheet.problems.append(
                Problem(SHEET, r.row, "Title", "duplicate_row", "the same activity twice in the file")
            )
        seen.add(key)


async def _match(conn: asyncpg.Connection, supplier: dict[str, Any], sheet: Sheet) -> None:
    existing = {
        (r["title"], r["destination_id"])
        for r in await conn.fetch(
            "SELECT lower(btrim(title)) AS title, destination_id::text AS destination_id FROM booking.services"
            " WHERE supplier_id = $1::uuid AND service_type = 'activity'",
            supplier["id"],
        )
    }
    await _resolve_places(conn, sheet)
    _duplicates(sheet, existing)
    if "activity" not in supplier["categories"]:
        sheet.problems.append(
            Problem(
                SHEET,
                None,
                None,
                "supplier_category_missing",
                f"supplier {supplier['code']} lacks the activity category",
            )
        )
    if supplier["status"] != "active":
        sheet.problems.append(
            Problem(SHEET, None, None, "supplier_inactive", f"supplier {supplier['code']} is inactive")
        )


def _preview(r: Row) -> dict[str, Any]:
    return {
        "row": r.row,
        "title": r.title,
        "places": [{"destination_id": p["destination_id"], "name": p["name"]} for p in r.resolved],
        "service": {k: v for k, v in r.service.items() if k != "service_type"},
        "rules": addons._audit_values(r.rules),
        "net_adult": r.adult.get("net_amount"),
        "net_child": (r.child or {}).get("net_amount"),
    }


async def import_sheet(
    conn: asyncpg.Connection, staff: Staff, supplier_ref: str, data: bytes, *, dry_run: bool
) -> dict[str, Any]:
    """Validate the workbook; unless ``dry_run``, create the services, draft offers and the draft contract with its
    rates (call inside a transaction: any refusal rolls every row back). 422 ``activity_sheet_invalid`` lists every
    problem."""
    supplier = await suppliers.resolve_supplier(conn, supplier_ref)
    sheet = parse(data)
    if sheet.rows:
        await _match(conn, supplier, sheet)
    if sheet.problems:
        raise Invalid(
            "activity_sheet_invalid",
            f"{len(sheet.problems)} problem(s) in the activity sheet; nothing was imported",
            errors=[p.out() for p in sheet.problems[:MAX_ERRORS]],
            ignored_sheets=sheet.ignored_sheets,
        )
    out: dict[str, Any] = {
        "dry_run": dry_run,
        "supplier": {"id": supplier["id"], "code": supplier["code"]},
        "contract": {k: sheet.contract.get(k) for k in rate_sheet.CONTRACT_FIELDS.values()},
        "activities": [_preview(r) for r in sheet.rows],
        "ignored_sheets": sheet.ignored_sheets,
    }
    if dry_run:
        return out
    contract = await suppliers.create_contract(
        conn, staff, supplier["id"], {k: v for k, v in sheet.contract.items() if v is not None}
    )
    created: list[dict[str, Any]] = []
    for r in sheet.rows:
        try:
            service = await records.create_record(
                conn,
                records.SERVICE,
                staff,
                {**r.service, "supplier_id": supplier["id"], "destination_id": r.resolved[0]["destination_id"]},
            )
            for rate in (r.adult, r.child):
                if rate is not None:
                    await rates.create_rate(conn, staff, contract["id"], {**rate, "service_id": service["id"]})
            places = [{"destination_id": p["destination_id"], "role": "operates"} for p in r.resolved]
            offer = await addons.create_offer(conn, staff, service["id"], r.rules, places)
        except ServiceError as exc:
            # A rule the service code checks (validity inside the contract, place country, ...): name the row.
            raise Invalid(
                "activity_sheet_invalid",
                f"{SHEET} row {r.row}: {exc.message}; nothing was imported",
                errors=[Problem(SHEET, r.row, None, exc.code, exc.message).out()],
                ignored_sheets=sheet.ignored_sheets,
            ) from None
        created.append(
            {"row": r.row, "service_id": service["id"], "service_code": service["code"], "offer_id": offer["id"]}
        )
    out["contract"] = {"id": contract["id"], "status": contract["status"], "title": contract["title"]}
    out["created"] = created
    out["imported"] = len(created)
    return out


# ── Template ─────────────────────────────────────────────────────────────────


def build_workbook(contract: dict[str, Any] | None = None, rows: list[tuple[Any, ...]] | None = None) -> bytes:
    """An AA activity catalogue workbook: the empty template, or (tests, fixtures) one filled with the given rows."""
    wb = Workbook()
    ws = wb.active
    ws.title = rate_sheet.CONTRACT_SHEET
    values = contract or {}
    for label, hint in rate_sheet.CONTRACT_HELP:
        ws.append([label, values.get(label, ""), hint if not contract else None])
    sheet = wb.create_sheet(SHEET)
    sheet.append(list(COLUMNS))
    for row in rows or []:
        sheet.append(list(row))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def template() -> bytes:
    return build_workbook()
