"""AA-1159 — the supplier bills list filter, one WHERE for the /billing list and the My work tiles.

:func:`bill_filter` builds the WHERE text over ``app.ops.supplier_bills.BILL_FROM`` (aliases ``b`` bill, ``bal``
``v_supplier_bill_balance``, ``s`` supplier, ``tc`` trip case). ``supplier_bills.list_bills`` and
``supplier_bills.queue`` (``app.my_work``) both call it, so a tile equals the list total for the same filter.

"Approvable by me" (``actionable_by``) = bills the caller may approve or pay: not entered by them and not last
changed by them (``created_by`` / ``facts_edited_by``), and an approved bill not approved by them either (AA-1158
R3), the maker-checker rules ``supplier_bills.approve`` and ``record_payout`` enforce, so the My work approval tiles
never count a bill the viewer would be refused. The business owner (``owner_approve`` / ``owner_pay``, decided by
:mod:`app.auth.self_approval`) also sees her own received bills and the approved bills she approved.

"Due soon" = open bills not overdue yet that fall due within ``supplier_bill_due_soon_days``: the window of the
``supplier_bill.due`` reminder (Pricing > Policy, maker-checker; :func:`due_soon_days`), never a number in code.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import date
from typing import Any

import asyncpg

from app import paging
from app.errors import Invalid
from app.pricing import policy_values

STATUSES = ("received", "approved", "paid", "cancelled")
DUE_SOON_KEY = "supplier_bill_due_soon_days"

Add = Callable[[str, Any], None]


async def due_soon_days(conn: asyncpg.Connection) -> int:
    """The "due soon" window in force today (none in force is 503 ``policy_unavailable``)."""
    on = await policy_values.today_utc(conn)
    return (await policy_values.require_staff(conn, on, DUE_SOON_KEY)).integer(DUE_SOON_KEY)


def _uuid(value: Any) -> str | None:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        return None


def _status(add: Add, where: list[str], status: str | None) -> None:
    if status not in (None, "open", "all", *STATUSES):
        raise Invalid("invalid_filter", "unknown status")
    if status == "open":
        where.append("b.status IN ('received', 'approved')")
    elif status and status != "all":
        add("b.status = {p}", status)


def _case(add: Add, case: str) -> None:
    """The bills of a case: named on the bill or with a line of one of its supplier bookings (UUID or code)."""
    ident = _uuid(case)
    if ident:
        add(
            "(b.trip_case_id = {p}::uuid OR EXISTS (SELECT 1 FROM booking.supplier_bill_items it JOIN"
            " booking.supplier_bookings sb ON sb.id = it.supplier_booking_id WHERE it.bill_id = b.id"
            " AND sb.trip_case_id = {p}::uuid))",
            ident,
        )
        return
    add(
        "(tc.code = {p} OR EXISTS (SELECT 1 FROM booking.supplier_bill_items it JOIN"
        " booking.supplier_bookings sb ON sb.id = it.supplier_booking_id JOIN booking.trip_cases c2"
        " ON c2.id = sb.trip_case_id WHERE it.bill_id = b.id AND c2.code = {p}))",
        case.strip().upper(),
    )


def _supplier(add: Add, supplier: str) -> None:
    ident = _uuid(supplier)
    if ident is None:
        raise Invalid("invalid_filter", "supplier is a supplier id")
    add("b.supplier_id = {p}::uuid", ident)


def bill_filter(
    *,
    status: str | None = None,
    supplier: str | None = None,
    case: str | None = None,
    overdue: bool | None = None,
    due_before: date | None = None,
    due_soon_days: int | None = None,
    actionable_by: str | None = None,
    q: str | None = None,
    owner_approve: bool = False,
    owner_pay: bool = False,
) -> tuple[str, list[Any]]:
    """The WHERE text and its arguments. ``due_soon_days``: open bills not overdue yet that fall due within that many
    days (the view's overdue is "past the due date in the session's day", ``current_date``: the same clock here).
    ``actionable_by``: a staff id; only the bills that person neither entered nor last changed (maker-checker)."""
    args: list[Any] = []
    where = ["true"]

    def add(sql: str, value: Any) -> None:
        args.append(value)
        where.append(sql.format(p=f"${len(args)}"))

    _status(add, where, status)
    if supplier:
        _supplier(add, supplier)
    if case:
        _case(add, case)
    if overdue is not None:
        where.append("bal.overdue" if overdue else "NOT bal.overdue")
    if due_before:
        add("b.due_date <= {p}", due_before)
    if due_soon_days is not None:
        add(
            "b.status IN ('received', 'approved') AND NOT bal.overdue AND b.due_date <= current_date + {p}::int",
            due_soon_days,
        )
    if actionable_by:
        maker = "b.created_by <> {p} AND b.facts_edited_by IS DISTINCT FROM {p}"
        payer = maker + ("" if owner_pay else " AND b.approved_by IS DISTINCT FROM {p}")
        approver = "true" if owner_approve else maker
        add(f"(CASE WHEN b.status = 'approved' THEN ({payer}) ELSE ({approver}) END)", actionable_by)
    if q:
        add(
            "(b.code ILIKE {p} OR b.bill_number ILIKE {p} OR s.company_name ILIKE {p} OR tc.code ILIKE {p})",
            paging.contains(q),
        )
    return " AND ".join(where), args
