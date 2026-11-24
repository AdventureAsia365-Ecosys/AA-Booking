"""Export a supplier contract as an AA rate sheet (AA-1153 part 2, S285 advisory r2 hotels T10: the two-way Excel).

``GET /booking/contracts/{id}/rate-sheet`` (``supply.read``) returns the contract in the import template
(:func:`app.supply.rate_sheet.build_workbook`): the Contract block, the Hotel and service sheets with every rate
(dates, board, pax band, net and, B7, the Published + Commission % it came from), the Seasons, the Hotel children and
the Inventory terms (allotment, free sale, on request; never a stop-sale, which is an event, not a contract term).

Round trip: contracting edits the file (a new price, a new season, an allotment) and imports it back with the usual
import (``POST /booking/suppliers/{ref}/rate-sheets``, DRY RUN first): it becomes a new **draft** contract, checked by
the business rules and shown as a diff against the supplier's previous published contract, and a second person
publishes it (maker-checker). Nothing is changed in place: a published contract stays fixed.

- A hotel is written with the DMC's own code for it when the link has one (the importer matches that code first),
  else its AA code; a room type by its AA name.
- A contract with more rows than the pricing policy ``rate_sheet_max_rows`` (the import's cap) is refused with 422
  ``rate_sheet_too_many_rows``, so an exported sheet always imports back.
"""

from __future__ import annotations

import re
from typing import Any

import asyncpg

from app.errors import Invalid
from app.suppliers import service as suppliers
from app.supply import rate_sheet, rate_sheet_inventory

# The hotel code a sheet of this supplier uses: the DMC's own code for the hotel, else the AA code.
_HOTEL_CODE = """coalesce(nullif(upper(btrim(
    (SELECT l.supplier_hotel_code FROM booking.hotel_supplier_links l
      WHERE l.hotel_id = h.id AND l.supplier_id = $2::uuid AND l.status = 'active'))), ''), upper(h.code))"""
RATES_SQL = f"""
SELECT {_HOTEL_CODE} AS hotel_code, t.name AS room_type, upper(s.code) AS service_code, s.service_type,
       r.season, r.valid_from, r.valid_to, r.meal_plan, r.pax_min, r.pax_max, r.net_amount, r.notes, r.pax_type,
       r.age_min, r.age_max, r.published_amount, r.commission_pct
FROM booking.supplier_rates r
LEFT JOIN booking.hotel_room_types t ON t.id = r.hotel_room_type_id
LEFT JOIN booking.hotels h ON h.id = t.hotel_id
LEFT JOIN booking.services s ON s.id = r.service_id
WHERE r.supplier_contract_id = $1::uuid
ORDER BY h.code NULLS LAST, t.name, s.code, r.valid_from, r.meal_plan NULLS FIRST, r.pax_min NULLS FIRST, r.pax_type,
         r.age_min NULLS FIRST, r.id
LIMIT $3
"""  # noqa: S608 — constant fragment
SEASONS_SQL = """
SELECT name, date_from, date_to FROM booking.supplier_contract_seasons WHERE supplier_contract_id = $1::uuid
ORDER BY date_from, lower(name) LIMIT $2
"""
CHILDREN_SQL = f"""
SELECT {_HOTEL_CODE} AS hotel_code, c.age_min, c.age_max, c.basis, c.meal_plan, c.net_amount
FROM booking.hotel_child_policies c JOIN booking.hotels h ON h.id = c.hotel_id
WHERE c.supplier_contract_id = $1::uuid
ORDER BY h.code, c.meal_plan NULLS FIRST, c.basis, c.age_min
LIMIT $3
"""  # noqa: S608 — constant fragment
INVENTORY_SQL = f"""
SELECT {_HOTEL_CODE} AS hotel_code, t.name AS room_type, upper(s.code) AS service_code, r.inventory_kind,
       r.date_from, r.date_to, r.units, r.release_days, r.max_pax, r.note
FROM booking.supply_restrictions r
LEFT JOIN booking.hotel_room_types t ON t.id = r.hotel_room_type_id
LEFT JOIN booking.hotels h ON h.id = t.hotel_id
LEFT JOIN booking.services s ON s.id = r.service_id
WHERE r.supplier_contract_id = $1::uuid AND r.inventory_kind <> 'stop_sale'
ORDER BY h.code NULLS LAST, t.name, s.code, r.date_from, r.inventory_kind, r.id
LIMIT $3
"""  # noqa: S608 — constant fragment
SUPPLIER_CODE_SQL = "SELECT code FROM booking.suppliers WHERE id = $1::uuid"
BASIS_LABEL = {"sharing": "sharing", "extra_bed": "extra bed"}
SERVICE_SHEET = {service_type: sheet for sheet, service_type in rate_sheet.SERVICE_SHEETS.items()}


def _hotel_row(r: Any) -> tuple[Any, ...]:
    return (
        r["hotel_code"], r["room_type"], r["season"], r["valid_from"], r["valid_to"], r["meal_plan"], r["pax_min"],
        r["pax_max"], r["net_amount"], r["notes"], r["published_amount"], r["commission_pct"],
    )  # fmt: skip


def _service_row(r: Any) -> tuple[Any, ...]:
    return (
        r["service_code"], r["season"], r["valid_from"], r["valid_to"], r["pax_min"], r["pax_max"], r["net_amount"],
        r["notes"], r["pax_type"], r["age_min"], r["age_max"], r["published_amount"], r["commission_pct"],
    )  # fmt: skip


def _inventory_row(r: Any) -> tuple[Any, ...]:
    return (
        r["hotel_code"] if r["room_type"] else None, r["room_type"], r["service_code"],
        rate_sheet_inventory.LABELS.get(r["inventory_kind"], r["inventory_kind"]), r["date_from"], r["date_to"],
        r["units"], r["release_days"], r["max_pax"], r["note"],
    )  # fmt: skip


def filename(supplier_code: str, title: str) -> str:
    """``aa-rate-sheet-SUP-000017-2027-rates.xlsx``: ASCII letters, digits and dashes only (a safe header value)."""
    slug = re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-").lower()[:60] or "contract"
    return f"aa-rate-sheet-{re.sub(r'[^A-Za-z0-9-]', '', supplier_code)}-{slug}.xlsx"


async def export(conn: asyncpg.Connection, contract_id: str) -> tuple[bytes, str]:
    """The contract as a filled rate-sheet workbook and its file name; 404 ``contract_not_found``; 422
    ``rate_sheet_too_many_rows`` past the pricing policy cap (the import's)."""
    contract = await suppliers.get_contract(conn, contract_id)
    cap = await rate_sheet.max_rows(conn)
    args = (contract["id"], contract["supplier_id"], cap + 1)
    rates = await conn.fetch(RATES_SQL, *args)
    seasons = await conn.fetch(SEASONS_SQL, contract["id"], cap + 1)
    children = await conn.fetch(CHILDREN_SQL, *args)
    terms = await conn.fetch(INVENTORY_SQL, *args)
    if len(rates) + len(seasons) + len(children) + len(terms) > cap:
        raise Invalid(
            "rate_sheet_too_many_rows",
            f"this contract has more than {cap} rows; a rate sheet holds at most {cap} (Pricing > Policy)",
        )
    services: dict[str, list[tuple[Any, ...]]] = {}
    for r in rates:
        if r["service_code"]:
            services.setdefault(SERVICE_SHEET.get(r["service_type"], "Activity"), []).append(_service_row(r))
    data = rate_sheet.build_workbook(
        {
            "Title": contract["title"],
            "Reference": contract["reference"] or "",
            "Valid from": contract["valid_from"],
            "Valid to": contract["valid_to"] or "",
            "Currency": contract["currency"],
            "Payment terms": contract["payment_terms"] or "",
            "Cancellation policy": contract["cancellation_policy"] or "",
        },
        [_hotel_row(r) for r in rates if r["room_type"] is not None],
        services,
        [(s["name"], s["date_from"], s["date_to"]) for s in seasons],
        [
            (
                c["hotel_code"],
                c["age_min"],
                c["age_max"],
                BASIS_LABEL.get(c["basis"], c["basis"]),
                c["meal_plan"],
                c["net_amount"],
            )
            for c in children
        ],  # fmt: skip
        [_inventory_row(t) for t in terms],
    )
    code = await conn.fetchval(SUPPLIER_CODE_SQL, contract["supplier_id"])
    return data, filename(str(code or "supplier"), str(contract["title"] or ""))
