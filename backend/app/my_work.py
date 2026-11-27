"""AA-1159 — My work: the Booking band of the staff dashboard, one tile per work queue the caller's capabilities allow.

The Booking half of "My work" (the CIS half is ``/admin/my-work``, AA-CIS-App ``api/routers/admin_my_work.py``). Each
tile is the number of items waiting in one queue, when the oldest of them entered it (``oldest_at``) or the earliest
due date among them (``earliest_due``), and the query string of the list page that shows exactly those items:

    key                         needs                   group     target page (filter)
    my_open_cases               trip_case.read          work      /cases?tab=mine&status=<every open status>
    trip_checks_overdue         ops.read                work      /trip-checks?due=overdue&owner=me (AA-1190: yours)
    trip_checks_unassigned      ops.write               work      /trip-checks?due=overdue&owner=none
    b2b_on_request              b2b_desk.read           work      /b2b-desk (its default: On request, open)
    supplier_bills_overdue      bill.read               work      /billing?bill_status=open&bill_due=overdue
    supplier_bills_due_soon     bill.read               work      /billing?bill_status=open&bill_due=soon
    agency_invoices_overdue     agency_billing.read     work      /agency-billing?status=overdue
    supplier_bills_to_approve   bill.approve            approve   /billing?bill_status=received&approvable=me
    supplier_bills_to_pay       payout.write            approve   /billing?bill_status=approved&approvable=me
    agency_payments_to_approve  agency_payment.approve  approve   /agency-billing?tab=payments&approvable=me
                                                                  (state default: recorded)
    stop_sales_to_review        stop_sale.approve       approve   /stop-sales?review=me
    suppliers_to_activate       supplier.activate       approve   /suppliers?status=pending&approvable=me
    billing_changes_to_approve  billing_config.approve  approve   /agency-billing?tab=settings&approvable=me
    supplier_bank_to_approve    supplier_bank.approve   approve   /suppliers?bank=pending&approvable=me
    price_versions_to_approve   price_version.approve   approve   /pricing/workbench?pending_versions=me
    addons_to_publish           addon.approve           approve   /add-ons?approvable=me
    season_changes_to_approve   season.approve          approve   /pricing/seasons?approvable=me
    holiday_events_to_approve   season.approve          approve   /pricing/holiday-events?approvable=me
    contracts_ending_soon       supply.write            work      /suppliers?contracts=ending (the expiring list)
    owner_self_approvals        audit.read              work      /audit-log?owner_self_approved=true&from=<month start>

AA-1177: ``contracts_ending_soon`` counts the published supplier contracts of every kind that end within the policy
value ``contract_expiry_alert_days`` (``app.supply.contract_expiry``, the list the Suppliers page shows with that
filter), for the people who renew them (``supply.write``: admin, contracting); ``days`` is that window.

AA-1158: ``owner_self_approvals`` counts the business-owner self-approvals of the current UTC calendar month (audit
rows with ``owner_self_approved``). It is shown to every ``audit.read`` holder, not only to the marker holder, so the
oversight never rests with the person it lists. The approval tiles include the business owner's own items when she may
self-approve (:mod:`app.auth.self_approval`).

The approval tiles count only the items the caller may act on (maker-checker: not entered, last changed, recorded or
asked to end by them), with the same ``approvable=me`` / ``review=me`` filter their page applies. AA-1190: the season
and holiday tiles leave out the caller's own proposals and removal requests (the business owner's own proposal stays
in while she may self-approve, never a stop-sale range or a removal) and a stop-sale range for a caller without
``stop_sale.approve``, the approve paths' rules (``seasons_admin.RANGE_APPROVABLE`` and its siblings).

AA-1190 (slice 1): the price setup progress card (``GET /booking/my-work/price-setup``, :func:`price_setup`) counts
the trip plans per next step of the price workbench (``app.catalog.price_workbench``, its own summary, so the card and
the workbench never disagree), for the people who set prices up (``price_version.write`` with ``pricing.read``).

Shown by capability, never by person: a tile is in the answer only when :meth:`Staff.can` grants its capability (so a
preview or E2E session gets its capped set, and a later custom role gets exactly the tiles of its capabilities). Every
count runs the WHERE text of its target list (the helper sits next to that list's query and shares it:
``bill_filters.bill_filter``, ``trip_cases.service.case_filter``, ``agency_billing.invoices.invoice_filter``,
``agency_billing.payments.payment_filter``, ``trip_checks.COUNT_WHERE`` + ``OVERDUE``, ``b2b.desk.STATE_WHERE``,
``supply.restrictions.restriction_filter``), so a
tile equals the total its page shows for the same filter. "Due soon" is the ``supplier_bill.due`` reminder's own window
(policy ``supplier_bill_due_soon_days``); there is no threshold in this module. A tile whose data cannot be read (a
table not migrated yet, no policy value in force) answers ``available: false`` and the other tiles still load.
Read-only, no cache: the dashboard loads it on open and on Refresh (no polling).

The frontend keeps the page path of each key (``frontend/lib/myWork.ts`` MY_WORK_TILES, same keys and capabilities:
``tests/unit/test_aa1159_my_work.py`` keeps the two in step) and adds ``filter`` as the query string.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import asyncpg

from app import audit
from app.agency_billing import entity_changes
from app.agency_billing import invoices as agency_invoices
from app.agency_billing import payments as agency_payments
from app.auth import self_approval
from app.auth.staff import Staff
from app.b2b import desk
from app.catalog import cost_sheet, price_version_queue, price_workbench
from app.errors import Forbidden, Unavailable
from app.ops import bill_approvals, bill_filters, supplier_bills, trip_checks
from app.pricing import seasons_admin
from app.suppliers import bank_accounts
from app.suppliers import service as suppliers
from app.supply import addons, contract_expiry, restrictions
from app.trip_cases import service as trip_cases

log = logging.getLogger(__name__)

WORK = "work"
APPROVE = "approve"
GROUPS = (WORK, APPROVE)

# Every status but the closed ones, in the list's order (the /cases status filter of "my open cases").
OPEN_CASE_STATUSES = tuple(s for s in trip_cases.STATUSES if s not in trip_cases.TERMINAL_STATUSES)

Counter = Callable[[asyncpg.Connection, Staff], Awaitable[dict[str, Any]]]


async def _my_open_cases(conn: asyncpg.Connection, staff: Staff) -> dict[str, Any]:
    return await trip_cases.case_queue(conn, statuses=list(OPEN_CASE_STATUSES), assignee=staff.cis_user_id)


async def _trip_checks_overdue(conn: asyncpg.Connection, staff: Staff) -> dict[str, Any]:
    # AA-1190: the overdue checks the caller owns (the page's owner=me); the unowned ones are the next tile
    return await trip_checks.overdue_queue(conn, assignee=staff.cis_user_id)


async def _trip_checks_unassigned(conn: asyncpg.Connection, _: Staff) -> dict[str, Any]:
    return await trip_checks.overdue_queue(conn, assignee="none")


async def _b2b_on_request(conn: asyncpg.Connection, _: Staff) -> dict[str, Any]:
    return await desk.on_request_queue(conn, state="open")


async def _bills_overdue(conn: asyncpg.Connection, _: Staff) -> dict[str, Any]:
    return await supplier_bills.queue(conn, status="open", overdue=True)


async def _bills_due_soon(conn: asyncpg.Connection, _: Staff) -> dict[str, Any]:
    days = await bill_filters.due_soon_days(conn)
    return {**await supplier_bills.queue(conn, status="open", due_soon_days=days), "days": days}


async def _agency_invoices_overdue(conn: asyncpg.Connection, _: Staff) -> dict[str, Any]:
    return await agency_invoices.queue(conn, status="overdue")


# The approval tiles count only what the caller may act on (maker-checker): never a bill they entered or last
# changed, never a payment they recorded, never a stop-sale they entered or asked to end. The page opens the same
# subset (``approvable=me`` / ``review=me``), so the tile still equals its list.


async def _bills_to_approve(conn: asyncpg.Connection, staff: Staff) -> dict[str, Any]:
    owner = await bill_approvals.owner_paths(conn, staff)
    return await supplier_bills.queue(
        conn, status="received", actionable_by=staff.cis_user_id, owner_approve=owner.approve, owner_pay=owner.pay
    )


async def _bills_to_pay(conn: asyncpg.Connection, staff: Staff) -> dict[str, Any]:
    owner = await bill_approvals.owner_paths(conn, staff)
    return await supplier_bills.queue(
        conn, status="approved", actionable_by=staff.cis_user_id, owner_approve=owner.approve, owner_pay=owner.pay
    )


async def _agency_payments_to_approve(conn: asyncpg.Connection, staff: Staff) -> dict[str, Any]:
    owner = await self_approval.may_self_approve(conn, staff)
    return await agency_payments.queue(conn, state="recorded", actionable_by=staff.cis_user_id, owner=owner)


async def _suppliers_to_activate(conn: asyncpg.Connection, staff: Staff) -> dict[str, Any]:
    return await suppliers.queue(conn, staff)


async def _billing_changes_to_approve(conn: asyncpg.Connection, staff: Staff) -> dict[str, Any]:
    return await entity_changes.queue(conn, staff)


async def _supplier_bank_to_approve(conn: asyncpg.Connection, staff: Staff) -> dict[str, Any]:
    return await bank_accounts.queue(conn, staff)


async def _owner_self_approvals(conn: asyncpg.Connection, _: Staff) -> dict[str, Any]:
    return await audit.owner_self_approvals_month(conn)


async def _contracts_ending_soon(conn: asyncpg.Connection, _: Staff) -> dict[str, Any]:
    return await contract_expiry.queue_count(conn)


async def _stop_sales_to_review(conn: asyncpg.Connection, staff: Staff) -> dict[str, Any]:
    return await restrictions.review_queue(conn, reviewable_by=staff.cis_user_id)


async def _price_versions_to_approve(conn: asyncpg.Connection, staff: Staff) -> dict[str, Any]:
    return await price_version_queue.approval_queue(conn, staff)


async def _addons_to_publish(conn: asyncpg.Connection, staff: Staff) -> dict[str, Any]:
    return await addons.publish_queue(conn, approvable_by=staff.cis_user_id)


async def _season_changes(conn: asyncpg.Connection, staff: Staff) -> dict[str, Any]:
    return await seasons_admin.seasons_waiting_queue(conn, staff)


async def _holiday_events(conn: asyncpg.Connection, staff: Staff) -> dict[str, Any]:
    return await seasons_admin.events_waiting_queue(conn, staff)


@dataclass(frozen=True)
class Tile:
    key: str
    capability: str
    group: str
    count: Counter
    # the target page's query string for exactly these items ({} = the page's default view)
    filter: dict[str, str] = field(default_factory=dict)


# Display order within each group. frontend/lib/myWork.ts MY_WORK_TILES lists the same keys and capabilities.
TILES: tuple[Tile, ...] = (
    Tile(
        "my_open_cases",
        "trip_case.read",
        WORK,
        _my_open_cases,
        {"tab": "mine", "status": ",".join(OPEN_CASE_STATUSES)},
    ),
    Tile("trip_checks_overdue", "ops.read", WORK, _trip_checks_overdue, {"due": "overdue", "owner": "me"}),
    Tile("trip_checks_unassigned", "ops.write", WORK, _trip_checks_unassigned, {"due": "overdue", "owner": "none"}),
    Tile("b2b_on_request", "b2b_desk.read", WORK, _b2b_on_request),
    Tile("supplier_bills_overdue", "bill.read", WORK, _bills_overdue, {"bill_status": "open", "bill_due": "overdue"}),
    Tile("supplier_bills_due_soon", "bill.read", WORK, _bills_due_soon, {"bill_status": "open", "bill_due": "soon"}),
    Tile("agency_invoices_overdue", "agency_billing.read", WORK, _agency_invoices_overdue, {"status": "overdue"}),
    Tile("contracts_ending_soon", "supply.write", WORK, _contracts_ending_soon, {"contracts": "ending"}),
    Tile(
        "supplier_bills_to_approve",
        "bill.approve",
        APPROVE,
        _bills_to_approve,
        {"bill_status": "received", "approvable": "me"},
    ),
    Tile(
        "supplier_bills_to_pay",
        "payout.write",
        APPROVE,
        _bills_to_pay,
        {"bill_status": "approved", "approvable": "me"},
    ),
    Tile(
        "agency_payments_to_approve",
        "agency_payment.approve",
        APPROVE,
        _agency_payments_to_approve,
        {"tab": "payments", "approvable": "me"},
    ),
    Tile("stop_sales_to_review", "stop_sale.approve", APPROVE, _stop_sales_to_review, {"review": "me"}),
    Tile(
        "suppliers_to_activate",
        "supplier.activate",
        APPROVE,
        _suppliers_to_activate,
        {"status": "pending", "approvable": "me"},
    ),
    Tile(
        "billing_changes_to_approve",
        "billing_config.approve",
        APPROVE,
        _billing_changes_to_approve,
        {"tab": "settings", "approvable": "me"},
    ),
    Tile(
        "supplier_bank_to_approve",
        "supplier_bank.approve",
        APPROVE,
        _supplier_bank_to_approve,
        {"bank": "pending", "approvable": "me"},
    ),
    Tile(
        "price_versions_to_approve",
        "price_version.approve",
        APPROVE,
        _price_versions_to_approve,
        {"pending_versions": "me"},
    ),
    Tile("addons_to_publish", "addon.approve", APPROVE, _addons_to_publish, {"approvable": "me"}),
    Tile("season_changes_to_approve", "season.approve", APPROVE, _season_changes, {"approvable": "me"}),
    Tile("holiday_events_to_approve", "season.approve", APPROVE, _holiday_events, {"approvable": "me"}),
    Tile("owner_self_approvals", "audit.read", WORK, _owner_self_approvals, {"owner_self_approved": "true"}),
)
CAPABILITIES: tuple[str, ...] = tuple(dict.fromkeys(t.capability for t in TILES))


def tiles_for(staff: Staff) -> list[Tile]:
    """The tiles ``staff`` may work, in display order (by capability, never by person)."""
    return [t for t in TILES if staff.can(t.capability)]


def require_any(staff: Staff) -> None:
    """403 ``forbidden`` for a caller that holds none of the tile capabilities (no band to show)."""
    if not tiles_for(staff):
        raise Forbidden("forbidden", "no My work queue for this role")


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


async def _one(conn: asyncpg.Connection, staff: Staff, tile: Tile) -> dict[str, Any]:
    base: dict[str, Any] = {
        "key": tile.key,
        "capability": tile.capability,
        "group": tile.group,
        "filter": dict(tile.filter),
    }
    try:
        got = await tile.count(conn, staff)
    except (asyncpg.UndefinedTableError, asyncpg.UndefinedColumnError, Unavailable) as exc:
        log.warning("my_work_tile_unavailable tile=%s error=%s", tile.key, type(exc).__name__)
        return {**base, "available": False, "count": None, "oldest_at": None, "earliest_due": None, "days": None}
    return {
        **base,
        "available": True,
        "count": int(got["count"] or 0),
        "oldest_at": _iso(got.get("oldest_at")),
        "earliest_due": _iso(got.get("earliest_due")),
        "days": got.get("days"),
        **({"filter": {**base["filter"], **got["filter"]}} if got.get("filter") else {}),
    }


async def summary(conn: asyncpg.Connection, staff: Staff) -> dict[str, Any]:
    """The caller's tiles (``GET /booking/my-work/summary``)."""
    require_any(staff)
    tiles = [await _one(conn, staff, t) for t in tiles_for(staff)]
    return {"role": staff.role, "tiles": tiles, "generated_at": datetime.now(UTC).isoformat()}


# ── Price setup progress (AA-1190) ───────────────────────────────────────────

PRICE_SETUP_CAPABILITY = "price_version.write"


def may_see_price_setup(staff: Staff) -> bool:
    """The price setup makers (``price_version.write``) who may read the workbench (``pricing.read``)."""
    return staff.can(PRICE_SETUP_CAPABILITY) and staff.can("pricing.read")


def require_price_setup(staff: Staff) -> None:
    if not may_see_price_setup(staff):
        raise Forbidden("forbidden", "the price setup card needs price_version.write and pricing.read")


async def price_setup(conn: asyncpg.Connection, staff: Staff) -> dict[str, Any]:
    """The price setup progress card: the trip plans per next step of the price workbench (its own summary of every
    plan: ``by_next_step``, the same numbers as its filter options), so each count opens
    ``/pricing/workbench?next_step=<step>`` with that many plans. 403 for a caller who is not a price setup maker."""
    require_price_setup(staff)
    rows, facts = await price_workbench.rows(conn, cost_sheet.today())
    summary = price_workbench.summary(rows)
    return {
        "steps": list(price_workbench.NEXT_STEPS),
        "by_next_step": summary["by_next_step"],
        "total": len(rows),
        "degraded": bool(facts.degraded),
        "generated_at": datetime.now(UTC).isoformat(),
    }
