"""The ``Inventory`` sheet of a rate sheet (AA-1153 part 2, S285 advisory r2 hotels B8): the DMC's allotment, free-sale
and on-request terms of the contract, per room type (or service) and date range, in the same workbook as its prices.

Columns (row 1, any order; every column present, cells may be empty): Hotel code, Room type, Service code, Kind,
From, To, Units, Release days, Max pax, Notes.

- A row names a room type (Hotel code + Room type, matched like the Hotel sheet: the DMC's own hotel code first, then
  the AA code, then room type name or alias) or a service (Service code), never both.
- Kind: ``allotment`` (Units required: rooms per night or seats per day; Release days optional: empty = the pricing
  policy ``allotment_release_days_default`` in force on the import day, stored on the term), ``free sale`` (Max pax
  optional: the most travellers per booking AA confirms without asking), ``on request`` (no numbers).
- From / To: inclusive, inside the contract validity, at most two years (the bk-060 CHECK).

The rows count against the workbook's row cap. Nothing is written while any row has a problem; the import writes each
term through :func:`app.supply.restrictions.create_term` (same rules and audit as the contract form) under the new
draft contract, so the second person who publishes the draft publishes its terms with it (bk-008).
"""

from __future__ import annotations

from datetime import date
from typing import Any

import asyncpg

from app.auth.staff import Staff
from app.errors import Invalid
from app.supply import rate_sheet_match, restrictions
from app.supply.rate_sheet_cells import Budget, Problem, _date, _int, _key, _table, _text

SHEET = "Inventory"
COLUMNS = ("Hotel code", "Room type", "Service code", "Kind", "From", "To", "Units", "Release days", "Max pax", "Notes")
KINDS = {
    "allotment": restrictions.ALLOTMENT,
    "free sale": restrictions.FREE_SALE,
    "free_sale": restrictions.FREE_SALE,
    "on request": restrictions.ON_REQUEST_KIND,
    "on_request": restrictions.ON_REQUEST_KIND,
}
LABELS = {
    restrictions.ALLOTMENT: "allotment",
    restrictions.FREE_SALE: "free sale",
    restrictions.ON_REQUEST_KIND: "on request",
}
RELEASE_MAX = 365  # technical-constant: the bk-060 CHECK bound of release_days
SERVICES_SQL = "SELECT upper(code) AS code, id::text AS id, status FROM booking.services WHERE supplier_id = $1::uuid"


def _release(value: Any) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip()
    if not text.isdigit() or int(text) > RELEASE_MAX:
        raise ValueError(f"a whole number from 0 to {RELEASE_MAX} is expected")
    return int(text)


def _numbers(term: dict[str, Any]) -> str | None:
    """Why the numbers do not fit the kind (the rules of :func:`app.supply.restrictions.create_term`), or None."""
    kind, units, release, max_pax = term["kind"], term["units"], term["release_days"], term["max_pax"]
    if kind == restrictions.ALLOTMENT:
        if units is None:
            return "an allotment needs Units (rooms per night or seats per day)"
        return "Max pax belongs to a free-sale row" if max_pax is not None else None
    if kind == restrictions.FREE_SALE:
        return "a free-sale row has no Units or Release days (Max pax only)" if units or release is not None else None
    return "an on-request row carries no numbers" if any(v is not None for v in (units, release, max_pax)) else None


def _row(n: int, cells: dict[str, Any], problems: list[Problem]) -> dict[str, Any] | None:
    term: dict[str, Any] = {"row": n, "kind": KINDS.get(_key(cells["Kind"]))}
    try:
        term["date_from"], term["date_to"] = _date(cells["From"]), _date(cells["To"])
        term["units"], term["max_pax"] = _int(cells["Units"]), _int(cells["Max pax"])
        term["release_days"] = _release(cells["Release days"])
    except ValueError as exc:
        problems.append(Problem(SHEET, n, None, "invalid_value", str(exc)))
        return None
    term["hotel_code"] = (_text(cells["Hotel code"]) or "").upper() or None
    term["room_type"] = _text(cells["Room type"])
    term["service_code"] = (_text(cells["Service code"]) or "").upper() or None
    term["note"] = _text(cells["Notes"])
    why: tuple[str, str] | None = None
    if term["kind"] is None:
        why = ("Kind", "allotment, free sale or on request")
    elif bool(term["hotel_code"] or term["room_type"]) == bool(term["service_code"]):
        why = ("Hotel code", "name a room type (Hotel code + Room type) or a service (Service code), not both")
    elif term["hotel_code"] and not term["room_type"]:
        why = ("Room type", "Room type is required with a Hotel code")
    elif term["date_from"] is None or term["date_to"] is None or term["date_to"] < term["date_from"]:
        why = ("From", "From and To are required, To on or after From")
    elif (term["date_to"] - term["date_from"]).days > restrictions.MAX_SPAN_DAYS:
        why = ("To", "one row covers at most two years")
    elif term["note"] is not None and len(term["note"]) > restrictions.MAX_NOTE:
        why = ("Notes", f"at most {restrictions.MAX_NOTE} characters")
    elif (numbers := _numbers(term)) is not None:
        why = ("Units", numbers)
    if why is not None:
        problems.append(Problem(SHEET, n, why[0], "invalid_inventory", why[1]))
        return None
    return term


def read(ws: Any, problems: list[Problem], budget: Budget) -> list[dict[str, Any]]:
    """The sheet's terms as read (no database); every problem by row."""
    return [t for n, cells in _table(ws, SHEET, COLUMNS, problems, budget) if (t := _row(n, cells, problems))]


def _inside(term: dict[str, Any], contract: dict[str, Any]) -> bool:
    start: date | None = contract.get("valid_from")
    end: date | None = contract.get("valid_to")
    return (start is None or term["date_from"] >= start) and (end is None or term["date_to"] <= end)


async def match(
    conn: asyncpg.Connection, supplier_id: str, contract: dict[str, Any], terms: list[dict[str, Any]],
    problems: list[Problem],
) -> list[dict[str, Any]]:  # fmt: skip
    """Each term with its component (``rt:`` / ``sv:``), or a problem by row: an item the supplier does not sell, an
    inactive one, or dates outside the contract."""
    if not terms:
        return []
    rooms = await rate_sheet_match.load(conn, supplier_id)
    services = {r["code"]: r for r in await conn.fetch(SERVICES_SQL, supplier_id)}
    out = []
    for term in terms:
        if term["hotel_code"]:
            target, why = rooms.target(term["hotel_code"], term["room_type"])
            component = f"rt:{target['hotel_room_type_id']}" if target else None
        else:
            found = services.get(term["service_code"] or "")
            why = (
                None
                if found
                else ("Service code", "unknown_item", f"no service {term['service_code']} of this supplier")
            )
            if found and found["status"] != "active":
                why = ("Service code", "inactive_item", "the service is inactive")
            component = f"sv:{found['id']}" if found and why is None else None
        if why is None and not _inside(term, contract):
            why = ("From", "restriction_outside_contract", "the dates are outside the contract validity")
        if why is not None:
            problems.append(Problem(SHEET, term["row"], *why))
            continue
        out.append({**term, "component_id": component})
    return out


def preview(terms: list[dict[str, Any]], release_default: int) -> list[dict[str, Any]]:
    """What the import writes per term (``release_days`` filled with the policy default when the row has none)."""
    keys = ("row", "component_id", "hotel_code", "room_type", "service_code", "date_from", "date_to", "units",
            "max_pax", "note")  # fmt: skip
    return [
        {
            **{k: t.get(k) for k in keys},
            "inventory_kind": t["kind"],
            "release_days": t["release_days"]
            if t["kind"] != restrictions.ALLOTMENT or t["release_days"] is not None
            else release_default,
            "release_days_default": t["kind"] == restrictions.ALLOTMENT and t["release_days"] is None,
        }
        for t in terms
    ]


async def write(conn: asyncpg.Connection, staff: Staff, contract_id: str, terms: list[dict[str, Any]]) -> int:
    """Create every term under the new draft (call inside the import's transaction); a refusal names its row."""
    for t in terms:
        body = {
            "component_id": t["component_id"],
            "inventory_kind": t["kind"],
            "date_from": t["date_from"],
            "date_to": t["date_to"],
            "units": t["units"],
            "release_days": t["release_days"],
            "max_pax": t["max_pax"],
            "note": t["note"],
        }
        try:
            await restrictions.create_term(conn, staff, contract_id, body)
        except Invalid as exc:
            raise Invalid(
                "rate_sheet_invalid",
                f"{SHEET} row {t['row']}: {exc.message}; nothing was imported",
                errors=[Problem(SHEET, t["row"], None, exc.code, exc.message).out()],
            ) from None
    return len(terms)
