"""The sellable gate of the workbook import (AA-1158 Q9; split out of :mod:`app.catalog.price_import`: file size).

A tour made sellable by the workbook needs an approved price once the import is applied: a matrix row still valid
today (:func:`app.catalog.trip_plan_prices.price_ready`: computed by the cost sheet, or from a published price
version), or rates the cost sheet prices it from when the import's own recompute runs (readiness status ``priced``
after the import). The draft price versions the import makes never count: nothing prices until a second person
approves them. The check runs in the dry run (a problem on the Tours sheet) and again after the recompute, on the
matrix it left, before the switch is written.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date
from typing import Any

import asyncpg

from app.auth.staff import Staff
from app.catalog import price_readiness, price_workbench, trip_plan_prices
from app.errors import Invalid
from app.supply.rate_sheet import Problem

COLUMN = "Sellable"
MESSAGE = "a sellable tour needs an approved price (an approved price version or supplier rates)"


async def check_import(
    conn: asyncpg.Connection,
    sheets: Any,
    plans: Mapping[str, Any],
    after: Mapping[str, str | None],
    today: date,
    *,
    sheet: str,
    refuse: Callable[[Any], None],
) -> None:
    """Add ``no_approved_price`` for every tour going sellable without a price after the import, then ``refuse``."""
    going = {p.id for p in plans.values() if p.sellable_after and not p.sellable}
    if not going:
        return
    ready = await trip_plan_prices.price_ready(conn, sorted(going), today)
    for row in sheets.tours:
        plan = plans.get(row.get("tour_key") or "")
        if plan is None or plan.id not in going or plan.id in ready or after.get(plan.key) == price_readiness.PRICED:
            continue
        sheets.problems.append(Problem(sheet, row.get("_row"), COLUMN, trip_plan_prices.NO_APPROVED_PRICE, MESSAGE))
    if sheets.problems:
        refuse(sheets)


async def apply_import(
    conn: asyncpg.Connection, staff: Staff, plans: Mapping[str, Any], today: date, *, sheet: str
) -> None:
    """Write the sellable switch after the recompute, re-checked against the matrix it left (nothing is written when
    one tour still has no price: the import's transaction rolls back)."""
    going = [p for p in plans.values() if p.sellable_after is not None and p.sellable_after != p.sellable]
    ready = await trip_plan_prices.price_ready(conn, [p.id for p in going if p.sellable_after], today)
    for plan in going:
        if plan.sellable_after and plan.id not in ready:
            problem = Problem(sheet, None, COLUMN, trip_plan_prices.NO_APPROVED_PRICE, f"{plan.key}: {MESSAGE}")
            raise Invalid(
                "price_import_invalid", f"{plan.key}: {MESSAGE}; nothing was imported", errors=[problem.out()]
            )
        await price_workbench.set_sellable(
            conn, staff, {"id": plan.id, "source_id": plan.key, "sellable": plan.sellable}, bool(plan.sellable_after)
        )
