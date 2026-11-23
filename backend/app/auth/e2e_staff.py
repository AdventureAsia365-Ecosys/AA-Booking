"""Dev-only write-capable E2E staff identities (AA-1087, S278, root ADR 0006a A10e).

The synthetic pilot rehearsal proves the staff steps through the browser: a supplier booking confirmed by staff, and an
agency payment recorded by one finance user and approved by another (maker-checker). The A10a E2E user
(:data:`app.auth.staff.E2E_TEST_STAFF_EMAIL`) is read-only, so three more WORKFORCE identities may write, each:

- **Dev only.** The process ``ENVIRONMENT`` must be exactly ``"dev"`` (read per request from the process environment,
  never the ``Settings`` default, which is ``"dev"`` when unset): anywhere else every request of the identity, reads
  included, is 403 ``e2e_not_allowed_here`` before any DB access. The WORKFORCE pre token trigger issues their tokens
  only where ``E2E_DEV_ONLY_STAFF_ENABLED`` is ``"true"`` (AA-CIS-Infra Terraform: dev only).
- **Exact groups.** The token's groups must be exactly the identity's tuple (the trigger already refuses any other
  set; checked again here): else 403 ``e2e_scope``.
- **Its own writes only.** Any method other than GET / HEAD / OPTIONS is refused 403 ``e2e_read_only`` before any DB
  access unless ``(method, route template)`` is in the identity's allow-list (:data:`E2E_WRITERS`). A write with a body
  rule (:data:`BODY_RULES`) must send exactly that body, else 403 ``e2e_read_only`` (AA-1087 S278 l18, root ADR 0006a
  A10e-1: the staff identity may PATCH a departure only to ``{"status": "cancelled"}``).
- **Synthetic agencies only.** An allowed write is then refused 403 ``e2e_synthetic_only`` unless the record belongs to
  an agency that CIS marks synthetic (``catalog.v_tenant_account_v1.is_synthetic``, CIS 401: ``is_canary`` true and not
  the pilot). The record is resolved from the path (:data:`SYNTHETIC_SQL`), before the route runs and before the staff
  row is synced. A record of no agency (a B2C case, a departure-owned supplier booking), an unknown id, a view without
  the column (CIS 401 not applied) or any database error count as real (fail closed). A departure is synthetic only
  when the A10e staff identity created it (the fixture seeder), at least one agency hold has a line on it, and every
  agency with a hold line, an allotment (any state) or a departure fee on it is synthetic
  (:data:`DEPARTURE_SYNTHETIC_SQL`): a catalogue departure staff made, an empty departure, or one any real agency has
  anything on, is real (a cancel ends every open allotment on it).
- **Route-scoped grant (A10e-1).** Departure cancel needs ``trip_plan.write`` (admin, contracting), which the staff
  identity's ``sales`` role lacks. Only after every check above passed (Dev, exact groups, allow-list, exact body,
  synthetic departure) does that one request carry the permission (:attr:`E2EWriter.grants`,
  ``Staff.e2e_grants``); no other request of the identity, and no other identity, ever holds it.

- **The pricing pair (A10e-2, AA-1091 S279).** A maker and a checker run the AA-1091 live check with SYNTHETIC prices:
  the maker is the CIS A10d customer-service address after the owner gave it the Staff page "Pricing team" preset
  (groups exactly ``cis-customer_success`` + ``booking-contracting``), the checker a Dev-only ``booking-contracting``
  address. Their writes touch trip plans, not agencies, so "synthetic" is per plan (:data:`PLAN_SYNTHETIC_EXPR`): no
  agency that CIS does not mark synthetic has a quote line, a hold line, an allotment or a departure fee on the plan.
  Price versions are reachable only when the E2E maker created them (:data:`VERSION_SYNTHETIC_SQL`): the maker's own
  approve or reject is then refused by the service (403 ``maker_checker``), and the checker never decides a real
  person's version. The workbench action body must be ``make_sellable`` / ``make_not_sellable`` on tour keys
  (:data:`BODY_CHECKS`, every key checked); the workbook import is checked inside the route, on every tour key of the
  parsed file, with no Departures row (:func:`check_import`, the guard cannot parse a workbook).

- **Synthetic supply (A10e-3, AA-1091 S280).** Dev has no supplier rates for the tours the live check prices, so the
  pricing maker also loads a SYNTHETIC supplier through the real supply paths: it creates the supplier (body checked:
  the name starts with :data:`SYNTHETIC_SUPPLY_MARK`, no contact or note field), its hotels, room types and services
  (names marked the same way), imports the rate sheet (a draft contract: the maker is its maker), and deactivates them
  at cleanup. AA-1158 (R5): the synthetic supplier starts ``pending``; the checker activates it (the second person:
  the service refuses the maker, 403 ``maker_checker``) before the maker adds hotels, services and rates. The checker
  publishes the draft (the service refuses the maker: 403 ``maker_checker``) and may expire it. A supply record is
  synthetic only when its supplier was created by the E2E pricing maker and still carries the mark
  (:data:`SUPPLIER_SYNTHETIC_SQL` and its hotel / service / contract forms); hotels and services being created are
  checked by the supplier their body names (:data:`BODY_SUPPLIER`). Paths take a UUID or a code (``SUP-`` / ``HTL-`` /
  ``SVC-`` + digits).

- **Synthetic activities (A10e-4, AA-1151 S289).** The maker also imports its synthetic supplier's activity catalogue
  (:data:`ACTIVITY_SHEET_IMPORT`, route check :func:`app.auth.e2e_staff_supply.check_activity_sheet`); the E2E owner
  (A10h) publishes the offers.

- **The custom-role maker (A10g, AA-1155 T5, root spec R4.3).** The Dev-only address :data:`E2E_CUSTOM_MAKER_EMAIL`
  (groups exactly ``cis-custom`` + ``booking-custom``) holds the custom role CIS grants it (``app.auth.custom_roles``),
  so it proves the custom role with a real session: it reads what the role reads, makes a SYNTHETIC supplier (body
  checked as A10e-3; it starts ``pending``, the maker holds no ``supplier.activate``) and deactivates it at cleanup,
  its own supplier only (:attr:`E2EWriter.record_owner`). Its other writes are **approve probes**
  (:attr:`E2EWriter.approve_probes`): any write whose route gate needs an approve-class capability
  (:func:`route_permissions`), and the approvals a service checks (:data:`SERVICE_PROBES`: supplier / hotel / service
  activate, contract publish, stop-sale review). A probe is answered 403 ``forbidden`` from the session's capabilities
  before any record is read (:func:`check_probe`): the answer the route gives, so no real record is ever touched. A
  probe the role could pass (never: a custom role holds no approve-class capability) is refused 403
  ``e2e_read_only`` unless it is also an allow-listed write, which then takes the synthetic-record rule. No
  route-scoped grant (A10g).

Reads keep the permissions of the identity's Booking role (``sales`` / ``finance`` / ``contracting``), like any member
of that group.
Every allowed write runs the route unchanged, so its normal audit row names the identity (``cog:<sub>``) as the actor.

TEMPORARY: removed with the A10 family at go-live (AA-1054): drop this module and its call in ``app.auth.staff``.
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

import asyncpg
from fastapi import HTTPException

if TYPE_CHECKING:
    from collections.abc import Iterable

log = logging.getLogger(__name__)

# The addresses (mailboxes of the E2E test domain; their codes land in the MFA-admin-only prefix of the mailbox).
# Same constants as AA-CIS-Infra workforce.E2E_DEV_ONLY_TEST_STAFF.
E2E_BOOKING_STAFF_EMAIL = "e2e-booking-staff@e2e-mail.lumiguides.it.com"
E2E_FINANCE_MAKER_EMAIL = "e2e-booking-maker@e2e-mail.lumiguides.it.com"
E2E_FINANCE_CHECKER_EMAIL = "e2e-booking-checker@e2e-mail.lumiguides.it.com"

# A10e-2 (AA-1091, S279): the pricing pair. The maker is the CIS A10d address (AA-CIS-App
# api/core/e2e_staff.E2E_CIS_CS_TEST_STAFF_EMAIL) holding the "Pricing team" preset; the checker is Dev-only too. Same
# constants as AA-CIS-Infra workforce.E2E_DEV_ONLY_TEST_STAFF / E2E_DEV_ONLY_EXTRA_GROUP_SETS.
E2E_PRICING_MAKER_EMAIL = "e2e-cis-cs@e2e-mail.lumiguides.it.com"
E2E_PRICING_CHECKER_EMAIL = "e2e-booking-pricing-checker@e2e-mail.lumiguides.it.com"
# A10g (AA-1155, S286): the custom-role maker. Same constant as AA-CIS-Infra workforce.E2E_DEV_ONLY_TEST_STAFF and the
# AA-CIS-App 462 admin_users_email_chk branch.
E2E_CUSTOM_MAKER_EMAIL = "e2e-custom-maker@e2e-mail.lumiguides.it.com"

DEV_ONLY_ENVIRONMENT = "dev"
READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# 403 details (the Playwright specs and the admin UI match on them).
E2E_NOT_ALLOWED_HERE = "e2e_not_allowed_here"
E2E_SCOPE = "e2e_scope"
E2E_READ_ONLY = "e2e_read_only"
E2E_SYNTHETIC_ONLY = "e2e_synthetic_only"

SUPPLIER_REQUEST = ("POST", "/booking/supplier-bookings/{booking_id}/request")
SUPPLIER_CONFIRM = ("POST", "/booking/supplier-bookings/{booking_id}/confirm")
PAYMENT_RECORD = ("POST", "/booking/agency-invoices/{ref}/payments")
PAYMENT_APPROVE = ("POST", "/booking/agency-payments/{payment_id}/approve")
PAYMENT_REJECT = ("POST", "/booking/agency-payments/{payment_id}/reject")
# AA-1087 S278 l18 (root ADR 0006a A10e-1): AA cancels a synthetic departure, so the agency CRM opens its
# aa_cancel_case (crm-13). Body exactly BODY_RULES[DEPARTURE_CANCEL]; trip_plan.write for that request only.
DEPARTURE_CANCEL = ("PATCH", "/booking/catalog/departures/{departure_id}")

# A10e-2 (AA-1091 S279): the AA-1091 pricing steps on synthetic trip plans (root ADR 0006a A10e-2).
PLAN_COMPONENTS = ("PUT", "/booking/catalog/trip-plans/{source_id}/components")
PRICE_IMPORT = ("POST", "/booking/catalog/price-import")
PRICE_WORKBENCH_ACTION = ("POST", "/booking/catalog/price-workbench/actions")
_VERSION = "/booking/catalog/price-versions/{version_id}"
PRICE_VERSION_SAVE = ("PUT", _VERSION)
PRICE_VERSION_SUBMIT = ("POST", f"{_VERSION}/submit")
PRICE_VERSION_APPROVE = ("POST", f"{_VERSION}/approve")
PRICE_VERSION_REJECT = ("POST", f"{_VERSION}/reject")
PRICE_VERSION_WITHDRAW = ("POST", f"{_VERSION}/withdraw")

# A10e-3 (AA-1091 S280): the synthetic supply the live check prices from (root ADR 0006a A10e-3).
SUPPLIER_CREATE = ("POST", "/booking/suppliers")
SUPPLIER_DEACTIVATE = ("POST", "/booking/suppliers/{supplier_ref}/deactivate")
RATE_SHEET_IMPORT = ("POST", "/booking/suppliers/{supplier_ref}/rate-sheets")
# A10e-4 (AA-1151 S289): the activity catalogue of the maker's synthetic supplier (+ e2e_staff_supply).
ACTIVITY_SHEET_IMPORT = ("POST", "/booking/suppliers/{supplier_ref}/activity-sheets")
CONTRACT_STATUS = ("PATCH", "/booking/contracts/{contract_id}")
HOTEL_CREATE = ("POST", "/booking/hotels")
HOTEL_DEACTIVATE = ("POST", "/booking/hotels/{hotel_ref}/deactivate")
ROOM_TYPE_CREATE = ("POST", "/booking/hotels/{hotel_ref}/room-types")
SERVICE_CREATE = ("POST", "/booking/services")
SERVICE_DEACTIVATE = ("POST", "/booking/services/{service_ref}/deactivate")
# A10g (AA-1155 T5): approvals a service checks inside a route gated by a write capability (the approve probes of the
# custom-role maker; app.suppliers.service, app.supply.records, app.supply.restrictions).
SUPPLIER_ACTIVATE = ("POST", "/booking/suppliers/{supplier_ref}/activate")
HOTEL_ACTIVATE = ("POST", "/booking/hotels/{hotel_ref}/activate")
SERVICE_ACTIVATE = ("POST", "/booking/services/{service_ref}/activate")
STOP_SALE_DECISION = ("POST", "/booking/supply/stop-sales/{restriction_id}/decision")
# AA-1158 part 2: approve / reject of a supplier bank account (route gate supply.read, the service checks the approval).
SUPPLIER_BANK_DECISION = ("POST", "/booking/supplier-bank-accounts/{account_id}/decision")
# Every supplier, hotel and service name the maker creates starts with this (checked on the create body, and the
# supplier keeps it: a renamed supplier is no longer synthetic).
SYNTHETIC_SUPPLY_MARK = "SYNTHETIC"

# Allowed writes whose body is fixed: anything else in the body is 403 e2e_read_only (no capacity, rule or status
# change other than the cancel itself).
BODY_RULES: Mapping[tuple[str, str], Mapping[str, Any]] = MappingProxyType(
    {DEPARTURE_CANCEL: MappingProxyType({"status": "cancelled"})}
)
# A10e-2: the only workbench actions the pricing maker sends (switch sellable on and back off; never a provisional
# price or a recompute).
PRICING_WORKBENCH_ACTIONS = frozenset({"make_sellable", "make_not_sellable"})
# The body field of a body-checked write that names the trip plans it touches (each one is checked synthetic).
BODY_TOUR_KEYS = "tour_keys"


def _workbench_body_ok(body: Any) -> bool:
    """Exactly ``{"action": <a PRICING_WORKBENCH_ACTIONS value>, "tour_keys": [<tour key>, ...]}``, at least one key,
    every key a string (the route's own model bounds the count; each key is then checked synthetic)."""
    if not isinstance(body, dict) or set(body) != {"action", BODY_TOUR_KEYS}:
        return False
    keys = body[BODY_TOUR_KEYS]
    return (
        body["action"] in PRICING_WORKBENCH_ACTIONS
        and isinstance(keys, list)
        and len(keys) > 0
        and all(isinstance(k, str) for k in keys)
    )


def _fields_ok(body: Any, required: frozenset[str], allowed: frozenset[str], marked: str | None = None) -> bool:
    """A JSON object holding every ``required`` key and no key outside ``allowed``; ``marked``, when given, names a
    text field that must start with :data:`SYNTHETIC_SUPPLY_MARK`."""
    if not isinstance(body, dict) or not required <= set(body) or not set(body) <= allowed:
        return False
    return marked is None or (isinstance(body[marked], str) and body[marked].strip().startswith(SYNTHETIC_SUPPLY_MARK))


# A10e-3 create bodies: the marked name and the facts the matcher and the cost sheet read; never a contact, address,
# note, link or photo (no personal or free-text data is written by an E2E identity).
_SUPPLIER_FIELDS = frozenset({"company_name", "categories", "country_code", "destination_id"})
_HOTEL_FIELDS = frozenset({"supplier_id", "name", "destination_id", "country_code", "star_rating"})
_SERVICE_FIELDS = frozenset(
    {"supplier_id", "service_type", "title", "unit", "destination_id", "country_code", "source_id"}
)
_ROOM_TYPE_FIELDS = frozenset({"name", "max_occupancy", "bed_types"})
# A contract moves only by its status, sent alone (publish by the checker; expire at cleanup).
CONTRACT_STATUSES = frozenset({"published", "expired"})


def _supplier_body_ok(body: Any) -> bool:
    return _fields_ok(body, frozenset({"company_name", "categories"}), _SUPPLIER_FIELDS, "company_name")


def _hotel_body_ok(body: Any) -> bool:
    return _fields_ok(body, frozenset({"supplier_id", "name"}), _HOTEL_FIELDS, "name")


def _service_body_ok(body: Any) -> bool:
    return _fields_ok(body, frozenset({"supplier_id", "service_type", "title"}), _SERVICE_FIELDS, "title")


def _room_type_body_ok(body: Any) -> bool:
    return _fields_ok(body, frozenset({"name"}), _ROOM_TYPE_FIELDS)


def _contract_body_ok(body: Any) -> bool:
    return isinstance(body, dict) and set(body) == {"status"} and body["status"] in CONTRACT_STATUSES


def _no_body(body: Any) -> bool:
    """A deactivation carries no reason text (empty body or ``{}``)."""
    return body is None or body == {}


# Allowed writes whose body must pass a check (anything else is 403 e2e_read_only before any DB access).
BODY_CHECKS: Mapping[tuple[str, str], Callable[[Any], bool]] = MappingProxyType(
    {
        PRICE_WORKBENCH_ACTION: _workbench_body_ok,
        SUPPLIER_CREATE: _supplier_body_ok,
        HOTEL_CREATE: _hotel_body_ok,
        SERVICE_CREATE: _service_body_ok,
        ROOM_TYPE_CREATE: _room_type_body_ok,
        CONTRACT_STATUS: _contract_body_ok,
        SUPPLIER_DEACTIVATE: _no_body,
        HOTEL_DEACTIVATE: _no_body,
        SERVICE_DEACTIVATE: _no_body,
    }
)


@dataclass(frozen=True)
class E2EWriter:
    """One identity: its exact WORKFORCE groups and the writes (method, route template) it may make."""

    name: str
    groups: frozenset[str]
    writes: frozenset[tuple[str, str]]
    # (method, route template) -> permissions this one request holds on top of the role (A10e-1), granted only after
    # every check passed. Each key is also in ``writes``.
    grants: Mapping[tuple[str, str], frozenset[str]] = field(default_factory=lambda: MappingProxyType({}))
    # A10g: the identity may send approve probes (answered 403 from its capabilities, never run; see check_probe).
    approve_probes: bool = False
    # A10g: the creator of the synthetic supply this identity may touch (its own address); None = the A10e-3 rule
    # (the pricing maker's supply, SYNTHETIC_SQL_ARGS).
    record_owner: str | None = None

    def grants_for(self, method: str, route: str | None) -> frozenset[str]:
        return self.grants.get((method.upper(), route or ""), frozenset())


E2E_WRITERS: Mapping[str, E2EWriter] = MappingProxyType(
    {
        # Supplier ops: send a supplier booking of a synthetic agency's case, then confirm it (booking-sales: ops.write
        # and b2b_desk.write, which an on-request B2B line needs).
        # A10e-1 (S278 l18): it also cancels a synthetic departure (body exactly {"status": "cancelled"}), holding
        # trip_plan.write for that request only.
        E2E_BOOKING_STAFF_EMAIL: E2EWriter(
            "booking_staff",
            frozenset({"booking-sales"}),
            frozenset({SUPPLIER_REQUEST, SUPPLIER_CONFIRM, DEPARTURE_CANCEL}),
            MappingProxyType({DEPARTURE_CANCEL: frozenset({"trip_plan.write"})}),
        ),
        # Finance maker: records a payment on a synthetic agency's invoice. Never approves (not on its list).
        E2E_FINANCE_MAKER_EMAIL: E2EWriter(
            "finance_maker", frozenset({"booking-finance"}), frozenset({PAYMENT_RECORD})
        ),
        # Finance checker: approves or rejects that payment (the DB CHECK and the route refuse its own recordings).
        E2E_FINANCE_CHECKER_EMAIL: E2EWriter(
            "finance_checker", frozenset({"booking-finance"}), frozenset({PAYMENT_APPROVE, PAYMENT_REJECT})
        ),
        # A10e-2 pricing maker (the CIS A10d address with the Pricing team preset): accepts components, imports the
        # synthetic workbook (draft versions + sellable), relabels / submits / withdraws its drafts, switches sellable
        # back off at cleanup. Approve / reject are listed only so the live probe reaches the service, which refuses
        # them 403 maker_checker: VERSION_SYNTHETIC_SQL limits them to versions this maker created.
        E2E_PRICING_MAKER_EMAIL: E2EWriter(
            "pricing_maker",
            frozenset({"cis-customer_success", "booking-contracting"}),
            frozenset(
                {
                    PLAN_COMPONENTS,
                    PRICE_IMPORT,
                    PRICE_WORKBENCH_ACTION,
                    PRICE_VERSION_SAVE,
                    PRICE_VERSION_SUBMIT,
                    PRICE_VERSION_WITHDRAW,
                    PRICE_VERSION_APPROVE,
                    PRICE_VERSION_REJECT,
                    # A10e-3: the synthetic supply (its own supplier only; the contract publish is refused by the
                    # service for the maker of the draft, 403 maker_checker).
                    SUPPLIER_CREATE,
                    SUPPLIER_DEACTIVATE,
                    RATE_SHEET_IMPORT,
                    ACTIVITY_SHEET_IMPORT,  # A10e-4: marked activities, draft offers (published by the E2E owner)
                    CONTRACT_STATUS,
                    HOTEL_CREATE,
                    HOTEL_DEACTIVATE,
                    ROOM_TYPE_CREATE,
                    SERVICE_CREATE,
                    SERVICE_DEACTIVATE,
                }
            ),
        ),
        # A10e-2 pricing checker: approves, rejects or withdraws (published: restores the superseded one) a version
        # the pricing maker made on a synthetic plan. A10e-3: publishes or expires a contract of the maker's synthetic
        # supplier (the second person of the contract maker-checker); AA-1158: activates that synthetic supplier (it
        # starts pending; the second person of the activation).
        E2E_PRICING_CHECKER_EMAIL: E2EWriter(
            "pricing_checker",
            frozenset({"booking-contracting"}),
            frozenset(
                {
                    PRICE_VERSION_APPROVE,
                    PRICE_VERSION_REJECT,
                    PRICE_VERSION_WITHDRAW,
                    CONTRACT_STATUS,
                    SUPPLIER_ACTIVATE,
                }
            ),
        ),
        # A10g (AA-1155 T5): the custom-role maker. It makes and cleans up its own synthetic supplier; every approve
        # probe (route gates and SERVICE_PROBES) is answered 403 from its capabilities before any read.
        E2E_CUSTOM_MAKER_EMAIL: E2EWriter(
            "custom_maker",
            frozenset({"cis-custom", "booking-custom"}),
            frozenset({SUPPLIER_CREATE, SUPPLIER_DEACTIVATE}),
            approve_probes=True,
            record_owner=E2E_CUSTOM_MAKER_EMAIL,
        ),
    }
)

# The record's agency's is_synthetic (one row or none). A record of no agency gives no row.
SUPPLIER_BOOKING_SYNTHETIC_SQL = (
    "SELECT v.is_synthetic FROM booking.supplier_bookings sb"
    " JOIN booking.trip_cases tc ON tc.id = sb.trip_case_id"
    " JOIN catalog.v_tenant_account_v1 v ON v.tenant_id = tc.party_tenant_id"
    " WHERE sb.id = $1::uuid"
)
INVOICE_SYNTHETIC_SQL = (
    "SELECT v.is_synthetic FROM booking.agency_invoice i"
    " JOIN catalog.v_tenant_account_v1 v ON v.tenant_id = i.party_tenant_id"
    " WHERE i.id = $1::uuid"
)
PAYMENT_SYNTHETIC_SQL = (
    "SELECT v.is_synthetic FROM booking.agency_payment p"
    " JOIN booking.agency_invoice i ON i.id = p.invoice_id"
    " JOIN catalog.v_tenant_account_v1 v ON v.tenant_id = i.party_tenant_id"
    " WHERE p.id = $1::uuid"
)
# A departure is synthetic only when (1) the A10e staff identity created it (the seeder, app.e2e_fixtures, writes as
# that identity; $2 = its address), (2) at least one agency hold has a line on it, and (3) every agency tied to it in
# any departure-scoped table is synthetic in CIS: hold lines (any hold status), allotments (any state: a cancel ends
# every open one, bk-046) and departure fees (any status, bk-048). A catalogue departure staff made, an empty one, or
# one any real or unknown agency has anything on is real. Shaped ``SELECT v.is_synthetic`` like the others.
DEPARTURE_SYNTHETIC_SQL = (
    "SELECT v.is_synthetic FROM (SELECT EXISTS (SELECT 1 FROM booking.departures d"
    " JOIN booking.staff_users su ON su.cis_user_id = d.created_by"
    " WHERE d.id = $1::uuid AND lower(su.email) = $2)"
    " AND coalesce(bool_or(x.src = 'hold'), false) AND coalesce(bool_and(t.is_synthetic IS TRUE), false)"
    " AS is_synthetic FROM ("
    "SELECT 'hold' AS src, h.partner_id FROM booking.b2b_hold_lines l"
    " JOIN booking.b2b_holds h ON h.id = l.hold_id WHERE l.departure_id = $1::uuid"
    " UNION ALL SELECT 'allotment', a.partner_id FROM booking.departure_allotments a WHERE a.departure_id = $1::uuid"
    " UNION ALL SELECT 'fee', f.partner_id FROM booking.departure_fees f WHERE f.departure_id = $1::uuid) x"
    " LEFT JOIN booking.b2b_partners p ON p.id = x.partner_id"
    " LEFT JOIN catalog.v_tenant_account_v1 t ON t.tenant_id = p.tenant_id) v"
)
# A10e-2: a trip plan is synthetic when no agency that CIS does not mark synthetic is tied to it: a quote line on the
# plan (component ``tp:<plan id>``, app.b2b.components), or a hold line, an allotment or a departure fee on one of its
# departures. A plan no agency has used is synthetic (prices are catalogue-wide: the live check withdraws them after).
# A missing view column or a DB error fails closed (the caller answers False). ``{plan}`` = the plan id expression.
_PLAN_AGENCIES = (
    "SELECT q.partner_id FROM booking.b2b_quote_lines ql JOIN booking.b2b_quotes q ON q.id = ql.quote_id"
    " WHERE lower(ql.component_id) = 'tp:' || {plan}::text"
    " UNION ALL SELECT h.partner_id FROM booking.b2b_hold_lines l JOIN booking.b2b_holds h ON h.id = l.hold_id"
    " JOIN booking.departures d ON d.id = l.departure_id WHERE d.trip_plan_id = {plan}"
    " UNION ALL SELECT a.partner_id FROM booking.departure_allotments a"
    " JOIN booking.departures d ON d.id = a.departure_id WHERE d.trip_plan_id = {plan}"
    " UNION ALL SELECT f.partner_id FROM booking.departure_fees f"
    " JOIN booking.departures d ON d.id = f.departure_id WHERE d.trip_plan_id = {plan}"
)
PLAN_SYNTHETIC_EXPR = (
    "NOT EXISTS (SELECT 1 FROM (" + _PLAN_AGENCIES + ") x"  # noqa: S608 — constant fragments
    " LEFT JOIN booking.b2b_partners p ON p.id = x.partner_id"
    " LEFT JOIN catalog.v_tenant_account_v1 t ON t.tenant_id = p.tenant_id"
    " WHERE t.is_synthetic IS NOT TRUE)"
)
# One plan by its tour key (the path's source_id, a UUID); no row for an unknown key.
PLAN_SYNTHETIC_SQL = (
    "SELECT " + PLAN_SYNTHETIC_EXPR.format(plan="tp.id") + " AS is_synthetic FROM booking.trip_plans tp"  # noqa: S608 — constant fragments
    " WHERE tp.source_system = 'cis' AND tp.source_id = $1"
)
# Many plans by tour key ($1 text[], $2 = the number of distinct keys): True only when every key is a known plan and
# every plan is synthetic.
PLANS_SYNTHETIC_SQL = (
    "SELECT count(*) = $2::int AND coalesce(bool_and(s.ok), false) AS is_synthetic FROM ("  # noqa: S608 — constant fragments
    "SELECT " + PLAN_SYNTHETIC_EXPR.format(plan="tp.id") + " AS ok FROM booking.trip_plans tp"
    " WHERE tp.source_system = 'cis' AND tp.source_id = ANY($1::text[])) s"
)
# A price list version: the A10e-2 maker created it ($2 = its address; the creator's staff row) and its plan is
# synthetic. So the checker decides only the E2E maker's versions, and the maker's approve / reject reaches only its own
# version, which the service refuses (403 maker_checker).
VERSION_SYNTHETIC_SQL = (
    "SELECT lower(su.email) = $2 AND "  # noqa: S608 — constant fragments
    + PLAN_SYNTHETIC_EXPR.format(plan="v.trip_plan_id")
    + " AS is_synthetic"
    " FROM booking.price_list_versions v LEFT JOIN booking.staff_users su ON su.cis_user_id = v.created_by"
    " WHERE v.id = $1::uuid"
)
# A10e-3: a supplier is synthetic when the A10e-2 pricing maker created it ($2 = its address; the creator's staff row)
# and its name still starts with the mark. Its hotels, services and contracts follow their supplier. By UUID here; the
# ``_BY_CODE`` forms take an ``SUP-`` / ``HTL-`` / ``SVC-`` code (:data:`CODE_SQL`).
_SUPPLIER_BY_MAKER = (
    "SELECT lower(su.email) = $2 AND s.company_name LIKE '" + SYNTHETIC_SUPPLY_MARK + "%' AS is_synthetic"
)
_MAKER_JOIN = " LEFT JOIN booking.staff_users su ON su.cis_user_id = s.created_by"
SUPPLIER_SYNTHETIC_SQL = _SUPPLIER_BY_MAKER + " FROM booking.suppliers s" + _MAKER_JOIN + " WHERE s.id = $1::uuid"
HOTEL_SYNTHETIC_SQL = (
    _SUPPLIER_BY_MAKER
    + " FROM booking.hotels r JOIN booking.suppliers s ON s.id = r.supplier_id"
    + _MAKER_JOIN
    + " WHERE r.id = $1::uuid"
)
SERVICE_SYNTHETIC_SQL = HOTEL_SYNTHETIC_SQL.replace("booking.hotels r", "booking.services r")
CONTRACT_SYNTHETIC_SQL = HOTEL_SYNTHETIC_SQL.replace("booking.hotels r", "booking.supplier_contracts r")
SUPPLIER_BY_CODE_SQL = SUPPLIER_SYNTHETIC_SQL.replace("WHERE s.id = $1::uuid", "WHERE s.code = $1")
HOTEL_BY_CODE_SQL = HOTEL_SYNTHETIC_SQL.replace("WHERE r.id = $1::uuid", "WHERE r.code = $1")
SERVICE_BY_CODE_SQL = SERVICE_SYNTHETIC_SQL.replace("WHERE r.id = $1::uuid", "WHERE r.code = $1")
# A path or body reference that is not a UUID: its code pattern and the SQL that looks it up by code.
CODE_SQL: Mapping[str, tuple[re.Pattern[str], str]] = MappingProxyType(
    {
        SUPPLIER_SYNTHETIC_SQL: (re.compile(r"SUP-[0-9]{6,12}"), SUPPLIER_BY_CODE_SQL),
        HOTEL_SYNTHETIC_SQL: (re.compile(r"HTL-[0-9]{6,12}"), HOTEL_BY_CODE_SQL),
        SERVICE_SYNTHETIC_SQL: (re.compile(r"SVC-[0-9]{6,12}"), SERVICE_BY_CODE_SQL),
    }
)
_SUPPLY_SQL = (SUPPLIER_SYNTHETIC_SQL, HOTEL_SYNTHETIC_SQL, SERVICE_SYNTHETIC_SQL, CONTRACT_SYNTHETIC_SQL)
# Route template -> (path parameter, SQL) for the writes checked by a path record. Every allow-listed write is in
# exactly one of SYNTHETIC_SQL, BODY_SYNTHETIC, BODY_SUPPLIER, CHECKED_IN_ROUTE and NEW_RECORD (a unit test keeps
# the lists equal).
SYNTHETIC_SQL: Mapping[str, tuple[str, str]] = MappingProxyType(
    {
        SUPPLIER_REQUEST[1]: ("booking_id", SUPPLIER_BOOKING_SYNTHETIC_SQL),
        SUPPLIER_CONFIRM[1]: ("booking_id", SUPPLIER_BOOKING_SYNTHETIC_SQL),
        PAYMENT_RECORD[1]: ("ref", INVOICE_SYNTHETIC_SQL),
        PAYMENT_APPROVE[1]: ("payment_id", PAYMENT_SYNTHETIC_SQL),
        PAYMENT_REJECT[1]: ("payment_id", PAYMENT_SYNTHETIC_SQL),
        DEPARTURE_CANCEL[1]: ("departure_id", DEPARTURE_SYNTHETIC_SQL),
        PLAN_COMPONENTS[1]: ("source_id", PLAN_SYNTHETIC_SQL),
        PRICE_VERSION_SAVE[1]: ("version_id", VERSION_SYNTHETIC_SQL),
        PRICE_VERSION_SUBMIT[1]: ("version_id", VERSION_SYNTHETIC_SQL),
        PRICE_VERSION_APPROVE[1]: ("version_id", VERSION_SYNTHETIC_SQL),
        PRICE_VERSION_REJECT[1]: ("version_id", VERSION_SYNTHETIC_SQL),
        PRICE_VERSION_WITHDRAW[1]: ("version_id", VERSION_SYNTHETIC_SQL),
        SUPPLIER_DEACTIVATE[1]: ("supplier_ref", SUPPLIER_SYNTHETIC_SQL),
        SUPPLIER_ACTIVATE[1]: ("supplier_ref", SUPPLIER_SYNTHETIC_SQL),  # AA-1158: the pricing checker
        RATE_SHEET_IMPORT[1]: ("supplier_ref", SUPPLIER_SYNTHETIC_SQL),
        ACTIVITY_SHEET_IMPORT[1]: ("supplier_ref", SUPPLIER_SYNTHETIC_SQL),  # A10e-4 (+ e2e_staff_supply)
        CONTRACT_STATUS[1]: ("contract_id", CONTRACT_SYNTHETIC_SQL),
        HOTEL_DEACTIVATE[1]: ("hotel_ref", HOTEL_SYNTHETIC_SQL),
        ROOM_TYPE_CREATE[1]: ("hotel_ref", HOTEL_SYNTHETIC_SQL),
        SERVICE_DEACTIVATE[1]: ("service_ref", SERVICE_SYNTHETIC_SQL),
    }
)
# Fixed arguments after the record key ($2, ...): the departure rule's creator is the A10e staff identity; a price
# version's creator, and a synthetic supplier's (A10e-3), is the A10e-2 pricing maker.
SYNTHETIC_SQL_ARGS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        DEPARTURE_CANCEL[1]: (E2E_BOOKING_STAFF_EMAIL,),
        **{
            route: (E2E_PRICING_MAKER_EMAIL,)
            for route, (_, sql) in SYNTHETIC_SQL.items()
            if sql is VERSION_SYNTHETIC_SQL or sql in _SUPPLY_SQL
        },
        # A10e-3: the hotel / service creates look up the supplier their body names (BODY_SUPPLIER).
        HOTEL_CREATE[1]: (E2E_PRICING_MAKER_EMAIL,),
        SERVICE_CREATE[1]: (E2E_PRICING_MAKER_EMAIL,),
    }
)
# Writes checked by the plans their JSON body names (every tour key synthetic, one query): route -> body field.
BODY_SYNTHETIC: Mapping[str, str] = MappingProxyType({PRICE_WORKBENCH_ACTION[1]: BODY_TOUR_KEYS})
# A10e-3: creates checked by the supplier their JSON body names (the maker's synthetic supplier): route -> body field.
BODY_SUPPLIER: Mapping[str, str] = MappingProxyType({HOTEL_CREATE[1]: "supplier_id", SERVICE_CREATE[1]: "supplier_id"})
# A10e-3: a create of a record that does not exist yet and names no other record (the synthetic supplier): its body
# check (:data:`BODY_CHECKS`: the marked name, no contact field) is its whole check; the record it makes is then
# synthetic by its creator.
NEW_RECORD: frozenset[str] = frozenset({SUPPLIER_CREATE[1]})
# Writes whose plans only the route knows (a workbook): the route calls check_import on the parsed file before it
# writes or previews anything (app.routers.catalog.post_price_import).
CHECKED_IN_ROUTE: frozenset[str] = frozenset({PRICE_IMPORT[1]})
_SUPPLY_BY_CODE_SQL = (SUPPLIER_BY_CODE_SQL, HOTEL_BY_CODE_SQL, SERVICE_BY_CODE_SQL)
# An invoice ``ref`` that is not a UUID is its number (AAS-2026-000001), as app.agency_billing.invoices resolves it.
INVOICE_BY_NUMBER_SQL = INVOICE_SYNTHETIC_SQL.replace("WHERE i.id = $1::uuid", "WHERE i.number = $1")


# A10g: approvals checked inside a route that a write capability gates, with the body that makes the call an approval
# (None = any body): (method, route) -> (approve capability, body test).
def _status_published(body: Any) -> bool:
    return isinstance(body, dict) and body.get("status") == "published"


def _review_action(body: Any) -> bool:
    return isinstance(body, dict) and body.get("action") in ("approve", "reject")


SERVICE_PROBES: Mapping[tuple[str, str], tuple[str, Callable[[Any], bool] | None]] = MappingProxyType(
    {
        SUPPLIER_ACTIVATE: ("supplier.activate", None),
        HOTEL_ACTIVATE: ("supplier.activate", None),
        SERVICE_ACTIVATE: ("supplier.activate", None),
        CONTRACT_STATUS: ("contract.publish", _status_published),
        STOP_SALE_DECISION: ("stop_sale.approve", _review_action),
        SUPPLIER_BANK_DECISION: ("supplier_bank.approve", _review_action),
    }
)
# The approve class (root spec D4; the same rule as app.auth.custom_roles.is_approve_class, kept here so this TEMPORARY
# module imports nothing of the auth core).
_APPROVE_SUFFIX = re.compile(r"\.approve(_[a-z0-9_]+)?$")
_APPROVE_NAMED = frozenset(
    {
        "supplier.activate",
        "contract.publish",
        "stop_sale.approve",
        "payout.write",
        "payment.verify",
        "billing_config.write",
        "staff.admin",
        "supplier_bank.read",  # bk-067 (AA-1158 part 2): it decrypts full account numbers
    }
)


def _approve_class(permission: str) -> bool:
    return bool(_APPROVE_SUFFIX.search(permission)) or permission in _APPROVE_NAMED


def route_permissions(route: Any) -> frozenset[str]:
    """The permissions the route's gates check (``app.auth.staff.require`` marks each dependency), recursively."""
    found: set[str] = set()
    stack = [getattr(route, "dependant", None)]
    while stack:
        dependant = stack.pop()
        for dep in getattr(dependant, "dependencies", ()) or ():
            permission = getattr(dep.call, "permission", None)
            if isinstance(permission, str):
                found.add(permission)
            stack.append(dep)
    return frozenset(found)


def probe_needs(method: str, route: Any, body: Any = None) -> frozenset[str]:
    """A10g: the approve-class capabilities this write needs (its route gates, plus a :data:`SERVICE_PROBES` approval
    when the body makes it one); empty for a read or a write that approves nothing."""
    verb = method.upper()
    if verb in READ_METHODS:
        return frozenset()
    needs = {p for p in route_permissions(route) if _approve_class(p)}
    probe = SERVICE_PROBES.get((verb, getattr(route, "path", "") or ""))
    if probe is not None and (probe[1] is None or probe[1](body)):
        needs.add(probe[0])
    return frozenset(needs)


def is_probe(writer: E2EWriter, method: str, route: Any) -> bool:
    """A10g, before the body is read: may this write be an approve probe of ``writer``? (A route gate in the approve
    class, or a :data:`SERVICE_PROBES` route; :func:`check_probe` decides with the body and the capabilities.)"""
    if not writer.approve_probes or method.upper() in READ_METHODS:
        return False
    path = getattr(route, "path", "") or ""
    return (method.upper(), path) in SERVICE_PROBES or any(_approve_class(p) for p in route_permissions(route))


def check_probe(writer: E2EWriter, method: str, route: Any, body: Any, capabilities: frozenset[str]) -> None:
    """A10g: answer an approve probe from the session's capabilities, before any record is read: 403 ``forbidden`` (the
    route's own answer) when a needed approval is missing. Any other write of this identity that is not allow-listed
    (a probe the session could pass, a probe route whose body approves nothing) is 403 ``e2e_read_only``: a probe is
    never run. An allow-listed write goes on to the synthetic-record rule. No-op for the other identities and reads."""
    verb = method.upper()
    if not writer.approve_probes or verb in READ_METHODS:
        return
    path = getattr(route, "path", None)
    needs = probe_needs(verb, route, body)
    if needs and not needs <= capabilities:
        log.info("booking_e2e_probe_refused identity=%s route=%s needs=%s", writer.name, path, ",".join(sorted(needs)))
        raise HTTPException(status_code=403, detail="forbidden")
    if (verb, path or "") not in writer.writes:
        log.warning("booking_e2e_writer_refused identity=%s reason=not_allowlisted route=%s", writer.name, path)
        raise HTTPException(status_code=403, detail=E2E_READ_ONLY)


def writer_for(email: Any) -> E2EWriter | None:
    """The identity of this access-token ``email`` claim, or None. Exact after trim + lower-case, ASCII only
    (``str.lower()`` folds some non-ASCII letters to ASCII: a look-alike address never matches)."""
    if not isinstance(email, str) or not email.isascii():
        return None
    return E2E_WRITERS.get(email.strip().lower())


def dev_only_environment() -> bool:
    """True only when the process ``ENVIRONMENT`` is exactly ``"dev"`` (unset, ``"Dev"``, ``"prod"``: False)."""
    return os.environ.get("ENVIRONMENT") == DEV_ONLY_ENVIRONMENT


def check_session(
    writer: E2EWriter, groups: tuple[str, ...], method: str, route: str | None, path: str, *, probe: bool = False
) -> None:
    """The checks that need no database, in order: environment, exact groups, the write allow-list (an A10g approve
    probe, ``probe``, passes it: :func:`check_probe` answers it later). Raises 403."""
    if not dev_only_environment():
        log.warning("booking_e2e_writer_refused identity=%s reason=environment", writer.name)
        raise HTTPException(status_code=403, detail=E2E_NOT_ALLOWED_HERE)
    if frozenset(groups) != writer.groups:
        log.warning("booking_e2e_writer_refused identity=%s reason=groups", writer.name)
        raise HTTPException(status_code=403, detail=E2E_SCOPE)
    verb = method.upper()
    if verb in READ_METHODS:
        return
    if probe:
        return
    if route is None or (verb, route) not in writer.writes:
        log.info(
            "booking_e2e_writer_refused identity=%s reason=not_allowlisted method=%s path=%s", writer.name, verb, path
        )
        raise HTTPException(status_code=403, detail=E2E_READ_ONLY)


def reads_body(method: str, route: str | None) -> bool:
    """True when the guard must read this write's JSON body (a body rule, a body check or plans named in the body)."""
    key = (method.upper(), route or "")
    body_named = key[1] in BODY_SYNTHETIC or key[1] in BODY_SUPPLIER
    probe_body = key in SERVICE_PROBES and SERVICE_PROBES[key][1] is not None  # A10g: the body makes it an approval
    return key in BODY_RULES or key in BODY_CHECKS or probe_body or (method.upper() not in READ_METHODS and body_named)


def check_body(writer: E2EWriter, method: str, route: str | None, body: Any) -> None:
    """For an allowed write with a body rule or check: 403 ``e2e_read_only`` unless the JSON body is exactly the rule
    (:data:`BODY_RULES`) or passes the check (:data:`BODY_CHECKS`). No DB access. ``body`` is the parsed JSON."""
    key = (method.upper(), route or "")
    rule = BODY_RULES.get(key)
    check = BODY_CHECKS.get(key)
    if rule is None and check is None:
        return
    ok = (rule is None or (isinstance(body, dict) and body == dict(rule))) and (check is None or check(body))
    if not ok:
        log.info("booking_e2e_writer_refused identity=%s reason=body route=%s", writer.name, route)
        raise HTTPException(status_code=403, detail=E2E_READ_ONLY)


def _uuid(value: Any) -> str | None:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError):
        return None


async def plans_synthetic(conn: asyncpg.Connection | asyncpg.Pool, keys: Iterable[Any]) -> bool:
    """A10e-2: True only when every tour key is a UUID of a known trip plan and every plan is synthetic
    (:data:`PLANS_SYNTHETIC_SQL`, one query). Fail closed: no key, a key that is not a UUID, an unknown plan, a missing
    view column or any database error answer False."""
    normal = [_uuid(k) if isinstance(k, str) else None for k in keys]
    if not normal or any(k is None for k in normal):
        return False
    distinct = sorted({k for k in normal if k is not None})
    try:
        return (await conn.fetchval(PLANS_SYNTHETIC_SQL, distinct, len(distinct))) is True
    except (asyncpg.PostgresError, OSError) as exc:
        log.warning("booking_e2e_synthetic_unknown route=plans error_class=%s", type(exc).__name__)
        return False


def _lookup(param: str, sql: str, raw: Any) -> tuple[str, str] | None:
    """The SQL and bind value for a record reference: a UUID, an invoice number (``ref``), or an ``SUP-`` / ``HTL-`` /
    ``SVC-`` code where the SQL has a by-code form (:data:`CODE_SQL`); None for anything else (counts as real)."""
    key = _uuid(raw)
    if key is not None:
        return sql, key
    if not isinstance(raw, str) or not 1 <= len(raw) <= 64:
        return None
    if param == "ref":
        return INVOICE_BY_NUMBER_SQL, raw
    code = CODE_SQL.get(sql)
    if code is not None and code[0].fullmatch(raw.strip().upper()):
        return code[1], raw.strip().upper()
    return None


def _sql_args(route: str, sql: str, owner: str | None) -> tuple[str, ...]:
    """The fixed arguments after the record key; an A10g ``owner`` replaces the creator of synthetic supply."""
    if owner is not None and (sql in _SUPPLY_SQL or sql in _SUPPLY_BY_CODE_SQL):
        return (owner,)
    return SYNTHETIC_SQL_ARGS.get(route, ())


async def _fetch_synthetic(
    conn: asyncpg.Connection | asyncpg.Pool, route: str, sql: str, key: str, owner: str | None = None
) -> bool:
    try:
        return (await conn.fetchval(sql, key, *_sql_args(route, sql, owner))) is True
    except (asyncpg.PostgresError, OSError) as exc:
        log.warning("booking_e2e_synthetic_unknown route=%s error_class=%s", route, type(exc).__name__)
        return False


async def is_synthetic_record(
    conn: asyncpg.Connection | asyncpg.Pool,
    route: str,
    params: Mapping[str, Any],
    body: Any = None,
    owner: str | None = None,
) -> bool:
    """True only when the record named by the path (or, for :data:`BODY_SYNTHETIC`, every trip plan the body names;
    for :data:`BODY_SUPPLIER`, the supplier the body names) belongs to an agency CIS marks synthetic, or is a synthetic
    plan / an E2E maker's version of one / the E2E maker's synthetic supply. Fail closed: an unknown route, id or
    record, a record of no agency, a missing view column or any database error answer False."""
    field_name = BODY_SYNTHETIC.get(route)
    if field_name is not None:
        keys = body.get(field_name) if isinstance(body, dict) else None
        return isinstance(keys, list) and await plans_synthetic(conn, keys)
    supplier_field = BODY_SUPPLIER.get(route)
    if supplier_field is not None:
        found = _lookup(
            "supplier_id", SUPPLIER_SYNTHETIC_SQL, body.get(supplier_field) if isinstance(body, dict) else None
        )
        return found is not None and await _fetch_synthetic(conn, route, *found, owner)
    spec = SYNTHETIC_SQL.get(route)
    if spec is None:
        return False
    found = _lookup(spec[0], spec[1], params.get(spec[0]))
    return found is not None and await _fetch_synthetic(conn, route, *found, owner)


async def check_synthetic(
    conn: asyncpg.Connection | asyncpg.Pool,
    writer: E2EWriter,
    method: str,
    route: str | None,
    params: Mapping[str, Any],
    body: Any = None,
) -> None:
    """For an allowed write: 403 ``e2e_synthetic_only`` unless the record's agency (or every plan, or the supplier) is
    synthetic. No-op for reads. A :data:`CHECKED_IN_ROUTE` write passes here: its route calls :func:`check_import`
    before any write. A :data:`NEW_RECORD` write passes too: its body check already ran (:func:`check_body`)."""
    if method.upper() in READ_METHODS:
        return
    if route in CHECKED_IN_ROUTE:
        log.info("booking_e2e_write_checked_in_route identity=%s route=%s", writer.name, route)
        return
    if route in NEW_RECORD:  # A10e-3: the body check (marked name, no contact field) was the whole check
        log.info("booking_e2e_write_allowed identity=%s route=%s new_record=true", writer.name, route)
        return
    if route is None or not await is_synthetic_record(conn, route, params, body, writer.record_owner):
        log.warning("booking_e2e_writer_refused identity=%s reason=not_synthetic route=%s", writer.name, route)
        raise HTTPException(status_code=403, detail=E2E_SYNTHETIC_ONLY)
    log.info("booking_e2e_write_allowed identity=%s route=%s", writer.name, route)


async def check_import(conn: asyncpg.Connection, writer_name: str, sheets: Any) -> None:
    """A10e-2, called by the workbook import route for an E2E session before anything is previewed or written (DRY RUN
    included). ``sheets`` = ``app.catalog.price_import.parse`` of the upload. 403 ``e2e_read_only`` when the workbook
    has a Departures row (not a pricing step); 403 ``e2e_synthetic_only`` unless every tour key of every sheet is a
    synthetic trip plan (:func:`plans_synthetic`: an unknown or malformed key refuses too)."""
    if getattr(sheets, "departures", None):
        log.info("booking_e2e_writer_refused identity=%s reason=departures route=%s", writer_name, PRICE_IMPORT[1])
        raise HTTPException(status_code=403, detail=E2E_READ_ONLY)
    keys = sheets.keys()
    if keys and not await plans_synthetic(conn, keys):
        log.warning(
            "booking_e2e_writer_refused identity=%s reason=not_synthetic route=%s", writer_name, PRICE_IMPORT[1]
        )
        raise HTTPException(status_code=403, detail=E2E_SYNTHETIC_ONLY)
    log.info("booking_e2e_write_allowed identity=%s route=%s tours=%d", writer_name, PRICE_IMPORT[1], len(keys))
