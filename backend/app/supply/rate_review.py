"""The review of a supplier's rates before a second person publishes them (AA-1153 part 1, T10; split out of
:mod:`app.supply.rate_sheet`, file size limit): the business checks (:mod:`app.supply.rate_checks`) and the diff
against the supplier's previous published contract, the ``rate_sheet_jump_warn_pct`` policy value they read, the
contract seasons and hotel child prices an import writes (draft only, bk-069 guard), and :func:`review`, the live view
``GET /booking/contracts/{id}/review`` answers and the publish step reads (``app.suppliers.service``)."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import asyncpg

from app.auth.staff import Staff
from app.errors import Conflict, Invalid
from app.pricing import policy_values
from app.pricing.policy_params_hotels import JUMP_WARN_PCT
from app.suppliers import service as suppliers
from app.supply import rate_checks

SEASONS = "Seasons"  # the workbook sheet names (app.supply.rate_sheet reads them from here)
CHILDREN = "Hotel children"
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


async def jump_pct(conn: asyncpg.Connection) -> Decimal:
    """``rate_sheet_jump_warn_pct`` in force today (503 ``policy_unavailable`` without it; never a literal)."""
    policy = await policy_values.require_staff(conn, await policy_values.today_utc(conn), JUMP_WARN_PCT)
    return Decimal(policy.integer(JUMP_WARN_PCT))


async def labels(conn: asyncpg.Connection, rates_: list[dict[str, Any]]) -> dict[str, str]:
    ids = sorted({rate_checks.target(r) for r in rates_ if rate_checks.target(r)})
    return {r["id"]: r["label"] for r in await conn.fetch(LABELS_SQL, ids)} if ids else {}


async def checks(
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
    names = await labels(conn, [*rates_, *before])
    findings = rate_checks.run(
        rates_,
        seasons=seasons,
        valid_from=contract.get("valid_from"),
        valid_to=contract.get("valid_to"),
        previous=before,
        jump_pct=await jump_pct(conn),
        names=names,
    )
    diff = {**rate_checks.diff(rates_, before, names), "previous_contract": contract_ref(previous)}
    return findings, diff, contract_ref(previous)


def contract_ref(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    return {"id": row["id"], "title": row["title"], "valid_from": row["valid_from"], "valid_to": row["valid_to"]}


def json_ready(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


async def write_parts(
    conn: asyncpg.Connection,
    staff: Staff,
    contract_id: str,
    seasons: list[dict[str, Any]],
    children: list[dict[str, Any]],
    ignored: list[str],
) -> None:
    """The contract's seasons and hotel child prices (draft only, bk-069 guard; a refusal names its sheet and row)."""
    for s in seasons:
        await _insert(
            conn,
            "INSERT INTO booking.supplier_contract_seasons (supplier_contract_id, name, date_from, date_to,"
            " created_by) VALUES ($1::uuid, $2, $3, $4, $5)",
            (contract_id, s["name"], s["date_from"], s["date_to"], staff.cis_user_id),
            where=(SEASONS, s.get("row")),
            ignored=ignored,
        )
    for c in children:
        await _insert(
            conn,
            "INSERT INTO booking.hotel_child_policies (supplier_contract_id, hotel_id, age_min, age_max, basis,"
            " meal_plan, net_amount, created_by) VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8)",
            (
                contract_id,
                c["hotel_id"],
                c["age_min"],
                c["age_max"],
                c["basis"],
                c["meal_plan"],
                c["net_amount"],
                staff.cis_user_id,
            ),
            where=(CHILDREN, c.get("row")),
            ignored=ignored,
        )


async def _insert(
    conn: asyncpg.Connection,
    sql: str,
    args: tuple[Any, ...],
    *,
    where: tuple[str, int | None],
    ignored: list[str],
) -> None:
    """One season or child price row; a refusal of the database is 422 naming the sheet and row it came from."""
    try:
        await conn.execute(sql, *args)
    except (asyncpg.CheckViolationError, asyncpg.UniqueViolationError) as exc:
        why = str(exc).splitlines()[0]
        sheet, row = where
        raise Invalid(
            "rate_sheet_invalid",
            f"{sheet} row {row}: refused ({why}); nothing was imported",
            errors=[{"sheet": sheet, "row": row, "column": None, "code": "invalid_value", "message": why}],
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
    findings, diff, previous = await checks(
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


async def require_acknowledged(conn: asyncpg.Connection, contract_id: str, acknowledged: bool) -> None:
    """AA-1153 (T10): the publisher of a draft made by a rate-sheet import acknowledges the warnings of its live
    review (``acknowledge_checks``), else 409 ``checks_unacknowledged``."""
    if not await conn.fetchval(
        "SELECT 1 FROM booking.rate_sheet_imports WHERE supplier_contract_id = $1::uuid", contract_id
    ):
        return
    found = await review(conn, contract_id)
    if found["acknowledge"] and not acknowledged:
        raise Conflict(
            "checks_unacknowledged",
            f"{len([c for c in found['checks'] if c['severity'] == 'warning'])} warning(s) in the rate review: read "
            "them, then publish with acknowledge_checks",
            checks=found["checks"],
        )
