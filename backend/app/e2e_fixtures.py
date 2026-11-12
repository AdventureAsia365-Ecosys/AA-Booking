"""Dev-only fixtures for the live E2E write checks (AA-1087 A10e, AA-1070 crm-13; S278 lane l18). DRY RUN by default.

The S278 live verify could not prove the Dev-only E2E write identities (root ADR 0006a A10e) because Dev held no record
they may act on: no pending supplier booking of a synthetic agency's B2B case, no issued invoice of a synthetic agency,
and no departure booked only by synthetic agencies to cancel. This module makes them, per **run**, through Booking's
own services (the same code staff and the agency API use), never by hand-written rows:

1. **A synthetic departure**: a new fixed departure (``app.b2b.departures.create_departure``) of a tour a synthetic
   agency has booked before (the template: its latest confirmed hold with a trip plan line on a departure), on the first
   free date from :data:`FIRST_DAY_OFFSET` days ahead.
2. **A booking of that agency on it**: a staff-entered agency booking (``app.b2b.desk_bookings.enter``: quote, hold,
   confirm, at the agency's own price; agency reference ``E2E-<run>``). Its B2B trip case is the agency's, so the
   departure is synthetic (A10e-1: every agency holding seats on it is synthetic) and its cancel reaches that agency's
   feed (``departure.cancelled``).
3. **A pending supplier booking** on that trip case (``app.ops.supplier_bookings.create``, an extra item of the
   template plan's supplier, else the first active supplier): what the staff identity sends and confirms.
4. **An issued agency invoice** of that agency (a ``settlement`` draft with one adjustment line, issued by
   ``app.agency_billing.invoices``): what the finance maker records a payment on and the checker approves.

Rules:

- **Dev only.** The process ``ENVIRONMENT`` must be exactly ``dev`` (:func:`app.auth.e2e_staff.dev_only_environment`),
  else exit 2 before connecting.
- **Synthetic agencies only.** The agency must be active and synthetic in CIS (``catalog.v_tenant_account_v1
  .is_synthetic``, CIS 401); ``--agency-tenant`` narrows the choice, a real one is refused (exit 3).
- **Actor.** Rows are written as the A10e staff identity (``e2e-booking-staff@``, its ``booking.staff_users`` row from
  its first sign-in), so the audit log names it; without that row the run stops (exit 3).
- **Idempotent per run** (``--run``, default the UTC minute ``YYYYMMDDHHMM``): the desk booking's Idempotency-Key, the
  supplier booking's note and the invoice's idempotency key carry the run, so a second ``--apply`` of the same run
  returns what the first made. A departure left empty by a failed run is reused.
- **DRY RUN first:** without ``--apply`` it prints the plan (agency, tour, date, what exists already) and writes
  nothing.

Run in the Booking API container (``DATABASE_URL`` = role ``booking``) through ECS Exec on ``aa-booking-dev-api``::

    python -m app.e2e_fixtures                    # DRY RUN: the plan
    python -m app.e2e_fixtures --apply            # make (or return) this run's fixtures
    python -m app.e2e_fixtures --apply --run s278a --agency-tenant <uuid>

Output: one JSON object (ids, codes, counts; agency name and tour key; no traveller or contact data). The Playwright
project ``e2e-writers`` reads it through ``BOOKING_E2E_FIXTURES`` (frontend/e2e/e2e-writers/writes.spec.ts).

TEMPORARY like A10e: removed with the A10 family at go-live (AA-1054).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import asyncpg

from app.agency_billing import invoices
from app.auth import e2e_staff
from app.auth.staff import STAFF_COLUMNS, Staff
from app.b2b import departures, desk_bookings, quotes
from app.b2b.components import LineAsk
from app.ops import supplier_bookings

log = logging.getLogger(__name__)

RUN_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,30}$")
FIRST_DAY_OFFSET = 60  # days ahead of today: past every sales cut-off and decision window of a fresh departure
MAX_DATE_TRIES = 120
MAX_PERIOD_TRIES = 60
CAPACITY = 12
ADULTS = 2
INVOICE_AMOUNT = Decimal("25.00")
MARK = "E2E fixture (synthetic)"

STAFF_SQL = f"SELECT {STAFF_COLUMNS} FROM booking.staff_users WHERE lower(email) = $1"  # noqa: S608 — fixed columns
# The template: a synthetic agency's latest confirmed booking of a trip plan line on a departure ($1 = agency tenant or
# NULL for any synthetic agency).
TEMPLATE_SQL = """
SELECT p.id::text AS partner_id, p.tenant_id::text AS tenant_id, p.agency_name, p.billing_currency::text AS billing,
       q.currency::text AS currency, ql.component_id, tp.id::text AS plan_id, tp.source_id,
       tp.supplier_id::text AS plan_supplier_id
FROM booking.b2b_holds h
JOIN booking.b2b_partners p ON p.id = h.partner_id
JOIN catalog.v_tenant_account_v1 v ON v.tenant_id = p.tenant_id
JOIN booking.b2b_quotes q ON q.id = h.quote_id
JOIN booking.b2b_quote_lines ql ON ql.quote_id = q.id
JOIN booking.trip_plans tp ON ql.component_id = 'tp:' || tp.id::text
WHERE h.status = 'confirmed' AND p.status = 'active' AND v.is_synthetic IS TRUE AND ql.departure_id IS NOT NULL
  AND tp.sellable AND ($1::uuid IS NULL OR p.tenant_id = $1::uuid)
ORDER BY h.confirmed_at DESC NULLS LAST
LIMIT 1
"""
AGENCY_SQL = """
SELECT p.id::text AS partner_id, p.status, coalesce(v.is_synthetic IS TRUE, false) AS synthetic
FROM booking.b2b_partners p LEFT JOIN catalog.v_tenant_account_v1 v ON v.tenant_id = p.tenant_id
WHERE p.tenant_id = $1::uuid
"""
# This run's desk booking (agency reference E2E-<run>), with its case and departure.
DESK_SQL = """
SELECT d.id::text AS desk_id, d.status, d.trip_case_id::text AS trip_case_id, tc.code AS trip_case_code,
       (SELECT l.departure_id::text FROM booking.b2b_hold_lines l WHERE l.hold_id = d.hold_id
         AND l.departure_id IS NOT NULL ORDER BY l.line_no LIMIT 1) AS departure_id
FROM booking.b2b_desk_bookings d LEFT JOIN booking.trip_cases tc ON tc.id = d.trip_case_id
WHERE d.idempotency_key = $1
"""
# A departure of the plan this actor created that no hold ever used (a failed run's leftover), else none.
EMPTY_DEPARTURE_SQL = """
SELECT d.id::text AS id, d.start_date FROM booking.departures d
WHERE d.trip_plan_id = $1::uuid AND d.created_by = $2 AND d.status = 'scheduled' AND d.start_date >= $3
  AND NOT EXISTS (SELECT 1 FROM booking.b2b_hold_lines l WHERE l.departure_id = d.id)
ORDER BY d.start_date LIMIT 1
"""
TAKEN_DATES_SQL = "SELECT start_date FROM booking.departures WHERE trip_plan_id = $1::uuid AND start_date >= $2"
SUPPLIER_SQL = """
SELECT id::text AS id, code FROM booking.suppliers WHERE status = 'active'
ORDER BY (id = $1::uuid) DESC NULLS LAST, created_at, id LIMIT 1
"""
SUPPLIER_BOOKING_SQL = """
SELECT id::text AS id, code, status FROM booking.supplier_bookings
WHERE trip_case_id = $1::uuid AND notes = $2 ORDER BY created_at DESC LIMIT 1
"""
INVOICE_BY_KEY_SQL = (
    "SELECT id::text AS id, number, status FROM booking.agency_invoice WHERE idempotency_key = $1 AND status <> 'void'"
)
PERIOD_TAKEN_SQL = """
SELECT EXISTS (SELECT 1 FROM booking.agency_invoice WHERE party_tenant_id = $1::uuid AND invoice_kind = 'settlement'
               AND period_start = $2 AND currency = $3 AND status <> 'void')
"""
INSERT_DRAFT_SQL = """
INSERT INTO booking.agency_invoice (party_tenant_id, invoice_kind, period_start, period_end, currency, tax_rate, source,
                                    created_by, idempotency_key, staff_note)
VALUES ($1::uuid, 'settlement', $2, $3, $4, 0, 'staff', $5, $6, $7) RETURNING id::text
"""


class Refused(Exception):
    """The run cannot go ahead (exit 3): a reason a person can act on."""


def default_run(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y%m%d%H%M")


def keys(run: str) -> dict[str, str]:
    """Every idempotency key and marker of one run."""
    return {
        "agency_ref": f"E2E-{run}",
        "desk_idem": f"e2e-fixture-{run}",
        "supplier_note": f"{MARK} run {run}",
        "invoice_idem": f"e2e-fixture:{run}:invoice",
    }


async def _actor(conn: asyncpg.Connection) -> Staff:
    row = await conn.fetchrow(STAFF_SQL, e2e_staff.E2E_BOOKING_STAFF_EMAIL)
    if row is None or not row["active"]:
        raise Refused("no active staff row for the A10e staff identity: sign in once as e2e-booking-staff first")
    return Staff.from_row(row)


async def _template(conn: asyncpg.Connection, agency_tenant: str | None) -> dict[str, Any]:
    if agency_tenant is not None:
        agency = await conn.fetchrow(AGENCY_SQL, agency_tenant)
        if agency is None:
            raise Refused(f"no B2B partner for tenant {agency_tenant}")
        if not agency["synthetic"]:
            raise Refused(f"tenant {agency_tenant} is not synthetic in CIS (catalog.v_tenant_account_v1.is_synthetic)")
        if agency["status"] != "active":
            raise Refused(f"the B2B partner of tenant {agency_tenant} is {agency['status']}")
    try:
        row = await conn.fetchrow(TEMPLATE_SQL, agency_tenant)
    except asyncpg.UndefinedColumnError as exc:
        raise Refused("catalog.v_tenant_account_v1 has no is_synthetic column (CIS 401 not applied)") from exc
    if row is None:
        raise Refused(
            "no confirmed booking of a synthetic agency on a departure to copy the tour from: book one tour for the "
            "agency first (CRM Book now or the B2B desk)"
        )
    return dict(row)


async def _free_date(conn: asyncpg.Connection, plan_id: str, today: date) -> date:
    first = today + timedelta(days=FIRST_DAY_OFFSET)
    taken = {r["start_date"] for r in await conn.fetch(TAKEN_DATES_SQL, plan_id, first)}
    for n in range(MAX_DATE_TRIES):
        day = first + timedelta(days=n)
        if day not in taken:
            return day
    raise Refused(f"no free departure date in the {MAX_DATE_TRIES} days from {first}")


async def _free_period(conn: asyncpg.Connection, tenant_id: str, currency: str, today: date) -> date:
    for n in range(MAX_PERIOD_TRIES):
        day = today - timedelta(days=n)
        if not await conn.fetchval(PERIOD_TAKEN_SQL, tenant_id, day, currency):
            return day
    raise Refused(f"every settlement period start of the last {MAX_PERIOD_TRIES} days is taken for this agency")


async def plan(conn: asyncpg.Connection, *, run: str, agency_tenant: str | None, today: date) -> dict[str, Any]:
    """What ``--apply`` would make or reuse (read-only)."""
    k = keys(run)
    actor = await _actor(conn)
    tpl = await _template(conn, agency_tenant)
    desk = await conn.fetchrow(DESK_SQL, k["desk_idem"])
    leftover = await conn.fetchrow(EMPTY_DEPARTURE_SQL, tpl["plan_id"], actor.cis_user_id, today)
    if desk is not None and desk["departure_id"]:
        dep: dict[str, Any] = {"departure_id": desk["departure_id"], "state": "exists"}
    elif leftover is not None:
        dep = {"departure_id": leftover["id"], "start_date": leftover["start_date"].isoformat(), "state": "reuse"}
    else:
        dep = {"start_date": (await _free_date(conn, tpl["plan_id"], today)).isoformat(), "state": "create"}
    sb = None
    if desk is not None and desk["trip_case_id"]:
        sb = await conn.fetchrow(SUPPLIER_BOOKING_SQL, desk["trip_case_id"], k["supplier_note"])
    supplier = await conn.fetchrow(SUPPLIER_SQL, tpl["plan_supplier_id"])
    if supplier is None:
        raise Refused("no active supplier for the supplier booking")
    inv = await conn.fetchrow(INVOICE_BY_KEY_SQL, k["invoice_idem"])
    return {
        "run": run,
        "agency": {"tenant_id": tpl["tenant_id"], "partner_id": tpl["partner_id"], "name": tpl["agency_name"]},
        "tour": {"source_id": tpl["source_id"], "component_id": tpl["component_id"], "currency": tpl["currency"]},
        "actor": actor.cis_user_id,
        "departure": dep,
        "desk_booking": {"state": "exists" if desk is not None else "create", "agency_ref": k["agency_ref"]},
        "supplier_booking": {"state": "exists" if sb is not None else "create", "supplier_code": supplier["code"]},
        "invoice": {
            "state": "create" if inv is None else ("exists" if inv["status"] != "draft" else "issue"),
            "currency": tpl["billing"],
        },
        "_template": tpl,
        "_supplier_id": supplier["id"],
    }


async def apply(pool: asyncpg.Pool, planned: dict[str, Any], *, today: date) -> dict[str, Any]:
    """Make (or return) the run's four fixtures. Each step commits on its own, through the services."""
    run, tpl, k = planned["run"], planned["_template"], keys(planned["run"])
    async with pool.acquire() as conn:
        actor = await _actor(conn)
    out: dict[str, Any] = {"run": run, "agency": planned["agency"], "tour_source_id": tpl["source_id"]}

    # 1 + 2. Departure and the agency's booking on it.
    async with pool.acquire() as conn:
        desk = await conn.fetchrow(DESK_SQL, k["desk_idem"])
    if desk is None:
        dep = planned["departure"]
        if dep["state"] == "create":
            async with pool.acquire() as conn, conn.transaction():
                created = await departures.create_departure(
                    conn,
                    actor,
                    tpl["source_id"],
                    {"start_date": date.fromisoformat(dep["start_date"]), "capacity": CAPACITY, "auto_confirm": True},
                )
            departure_id, start = str(created["id"]), date.fromisoformat(str(created["start_date"]))
        else:
            departure_id, start = dep["departure_id"], date.fromisoformat(dep["start_date"])
        line = quotes.AskLine(
            ask=LineAsk(
                line_ref="tour",
                component_id=tpl["component_id"],
                service_date=start,
                nights=None,
                adult=ADULTS,
                child=0,
                infant=0,
            ),
            rooms=[],
            departure_id=departure_id,
        )
        await desk_bookings.enter(
            pool,
            actor,
            partner_id=tpl["partner_id"],
            agency_ref=k["agency_ref"],
            currency=tpl["currency"],
            lines=[line],
            idempotency_key=k["desk_idem"],
        )
        async with pool.acquire() as conn:
            desk = await conn.fetchrow(DESK_SQL, k["desk_idem"])
    if desk is None or desk["status"] != "booked" or not desk["trip_case_id"]:
        raise Refused(f"the agency booking of run {run} did not confirm (status {desk and desk['status']})")
    out["departure_id"] = desk["departure_id"]
    out["trip_case"] = {"id": desk["trip_case_id"], "code": desk["trip_case_code"]}

    # 3. The pending supplier booking on that case.
    async with pool.acquire() as conn, conn.transaction():
        sb = await conn.fetchrow(SUPPLIER_BOOKING_SQL, desk["trip_case_id"], k["supplier_note"])
        if sb is None:
            made = await supplier_bookings.create(
                conn,
                actor,
                desk["trip_case_id"],
                {
                    "supplier_id": planned["_supplier_id"],
                    "item_description": f"{MARK}: airport transfer, run {run}",
                    "service_date": None,
                    "pax": ADULTS,
                    "notes": k["supplier_note"],
                },
            )
            sb = {"id": made["id"], "code": made["code"], "status": made["status"]}
    out["supplier_booking"] = {"id": sb["id"], "code": sb["code"], "status": sb["status"]}

    # 4. The issued agency invoice.
    tenant, currency = tpl["tenant_id"], tpl["billing"]
    async with pool.acquire() as conn:
        inv = await conn.fetchrow(INVOICE_BY_KEY_SQL, k["invoice_idem"])
        if inv is None:
            async with conn.transaction():
                start = await _free_period(conn, tenant, currency, today)
                invoice_id = await conn.fetchval(
                    INSERT_DRAFT_SQL,
                    tenant,
                    start,
                    start + timedelta(days=1),
                    currency,
                    actor.cis_user_id,
                    k["invoice_idem"],
                    f"{MARK} run {run}",
                )
            inv = {"id": invoice_id, "status": "draft", "number": None}
        if inv["status"] == "draft":
            draft = await invoices.get(conn, inv["id"])
            if not draft.get("lines"):
                await invoices.add_line(
                    conn,
                    inv["id"],
                    {"description": f"{MARK}: adjustment, run {run}", "unit_price": INVOICE_AMOUNT, "quantity": 1},
                    actor.cis_user_id,
                )
            issued = await invoices.issue(conn, inv["id"], actor.cis_user_id)
            inv = {"id": issued["id"], "status": issued["status"], "number": issued["number"]}
    out["invoice"] = {"id": inv["id"], "number": inv["number"], "status": inv["status"]}
    return out


def _public(planned: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in planned.items() if not k.startswith("_")}


async def _main(args: argparse.Namespace) -> int:
    if not e2e_staff.dev_only_environment():
        print(json.dumps({"error": "refused: ENVIRONMENT is not exactly 'dev' (Dev-only fixtures)"}))
        return 2
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print(json.dumps({"error": "DATABASE_URL not set"}))
        return 2
    pool = await asyncpg.create_pool(
        dsn,
        ssl=os.environ.get("DB_SSL", "require"),
        min_size=1,
        max_size=2,
        timeout=10,
        server_settings={"application_name": "booking-e2e-fixtures"},
    )
    today = datetime.now(UTC).date()
    try:
        async with pool.acquire() as conn:
            planned = await plan(conn, run=args.run, agency_tenant=args.agency_tenant, today=today)
        if not args.apply:
            print(json.dumps({"mode": "DRY RUN", "plan": _public(planned)}, indent=1, default=str))
            return 0
        made = await apply(pool, planned, today=today)
    except Refused as exc:
        print(json.dumps({"error": f"refused: {exc}"}))
        return 3
    finally:
        await pool.close()
    print(json.dumps({"mode": "APPLY", "fixtures": made}, indent=1, default=str))
    return 0


def _run_arg(value: str) -> str:
    if not RUN_RE.match(value):
        raise argparse.ArgumentTypeError("a run is 3-31 lower-case letters, digits or hyphens")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dev-only fixtures for the Booking E2E write identities (A10e).")
    parser.add_argument("--apply", action="store_true", help="make the fixtures (default: DRY RUN, plan only)")
    parser.add_argument("--run", type=_run_arg, default=default_run(), help="run key (default: UTC YYYYMMDDHHMM)")
    parser.add_argument("--agency-tenant", default=None, help="the synthetic agency's CIS tenant id (default: any)")
    return asyncio.run(_main(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
