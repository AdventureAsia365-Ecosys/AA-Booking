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

Reads keep the permissions of the identity's Booking role (``sales`` / ``finance``), like any member of that group.
Every allowed write runs the route unchanged, so its normal audit row names the identity (``cog:<sub>``) as the actor.

TEMPORARY: removed with the A10 family at go-live (AA-1054): drop this module and its call in ``app.auth.staff``.
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

import asyncpg
from fastapi import HTTPException

log = logging.getLogger(__name__)

# The addresses (mailboxes of the E2E test domain; their codes land in the MFA-admin-only prefix of the mailbox).
# Same constants as AA-CIS-Infra workforce.E2E_DEV_ONLY_TEST_STAFF.
E2E_BOOKING_STAFF_EMAIL = "e2e-booking-staff@e2e-mail.lumiguides.it.com"
E2E_FINANCE_MAKER_EMAIL = "e2e-booking-maker@e2e-mail.lumiguides.it.com"
E2E_FINANCE_CHECKER_EMAIL = "e2e-booking-checker@e2e-mail.lumiguides.it.com"

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

# Allowed writes whose body is fixed: anything else in the body is 403 e2e_read_only (no capacity, rule or status
# change other than the cancel itself).
BODY_RULES: Mapping[tuple[str, str], Mapping[str, Any]] = MappingProxyType(
    {DEPARTURE_CANCEL: MappingProxyType({"status": "cancelled"})}
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
# Route template -> (path parameter, SQL). Every allow-listed write has one (a unit test keeps the two lists equal).
SYNTHETIC_SQL: Mapping[str, tuple[str, str]] = MappingProxyType(
    {
        SUPPLIER_REQUEST[1]: ("booking_id", SUPPLIER_BOOKING_SYNTHETIC_SQL),
        SUPPLIER_CONFIRM[1]: ("booking_id", SUPPLIER_BOOKING_SYNTHETIC_SQL),
        PAYMENT_RECORD[1]: ("ref", INVOICE_SYNTHETIC_SQL),
        PAYMENT_APPROVE[1]: ("payment_id", PAYMENT_SYNTHETIC_SQL),
        PAYMENT_REJECT[1]: ("payment_id", PAYMENT_SYNTHETIC_SQL),
        DEPARTURE_CANCEL[1]: ("departure_id", DEPARTURE_SYNTHETIC_SQL),
    }
)
# Fixed arguments after the record key ($2, ...): the departure rule's creator is the A10e staff identity.
SYNTHETIC_SQL_ARGS: Mapping[str, tuple[str, ...]] = MappingProxyType({DEPARTURE_CANCEL[1]: (E2E_BOOKING_STAFF_EMAIL,)})
# An invoice ``ref`` that is not a UUID is its number (AAS-2026-000001), as app.agency_billing.invoices resolves it.
INVOICE_BY_NUMBER_SQL = INVOICE_SYNTHETIC_SQL.replace("WHERE i.id = $1::uuid", "WHERE i.number = $1")


def writer_for(email: Any) -> E2EWriter | None:
    """The identity of this access-token ``email`` claim, or None. Exact after trim + lower-case, ASCII only
    (``str.lower()`` folds some non-ASCII letters to ASCII: a look-alike address never matches)."""
    if not isinstance(email, str) or not email.isascii():
        return None
    return E2E_WRITERS.get(email.strip().lower())


def dev_only_environment() -> bool:
    """True only when the process ``ENVIRONMENT`` is exactly ``"dev"`` (unset, ``"Dev"``, ``"prod"``: False)."""
    return os.environ.get("ENVIRONMENT") == DEV_ONLY_ENVIRONMENT


def check_session(writer: E2EWriter, groups: tuple[str, ...], method: str, route: str | None, path: str) -> None:
    """The checks that need no database, in order: environment, exact groups, the write allow-list. Raises 403."""
    if not dev_only_environment():
        log.warning("booking_e2e_writer_refused identity=%s reason=environment", writer.name)
        raise HTTPException(status_code=403, detail=E2E_NOT_ALLOWED_HERE)
    if frozenset(groups) != writer.groups:
        log.warning("booking_e2e_writer_refused identity=%s reason=groups", writer.name)
        raise HTTPException(status_code=403, detail=E2E_SCOPE)
    verb = method.upper()
    if verb in READ_METHODS:
        return
    if route is None or (verb, route) not in writer.writes:
        log.info(
            "booking_e2e_writer_refused identity=%s reason=not_allowlisted method=%s path=%s", writer.name, verb, path
        )
        raise HTTPException(status_code=403, detail=E2E_READ_ONLY)


def check_body(writer: E2EWriter, method: str, route: str | None, body: Any) -> None:
    """For an allowed write with a body rule: 403 ``e2e_read_only`` unless the JSON body is exactly the rule. No DB
    access. ``body`` is the parsed JSON (anything that is not exactly the rule's object is refused)."""
    rule = BODY_RULES.get((method.upper(), route or ""))
    if rule is None:
        return
    if not isinstance(body, dict) or body != dict(rule):
        log.info("booking_e2e_writer_refused identity=%s reason=body route=%s", writer.name, route)
        raise HTTPException(status_code=403, detail=E2E_READ_ONLY)


def _uuid(value: Any) -> str | None:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError):
        return None


async def is_synthetic_record(conn: asyncpg.Connection | asyncpg.Pool, route: str, params: Mapping[str, Any]) -> bool:
    """True only when the record named by the path belongs to an agency CIS marks synthetic. Fail closed: an unknown
    route, id or record, a record of no agency, a missing view column or any database error answer False."""
    spec = SYNTHETIC_SQL.get(route)
    if spec is None:
        return False
    param, sql = spec
    raw = params.get(param)
    key = _uuid(raw)
    if key is None and param == "ref" and isinstance(raw, str) and 1 <= len(raw) <= 64:
        sql, key = INVOICE_BY_NUMBER_SQL, raw
    if key is None:
        return False
    try:
        return (await conn.fetchval(sql, key, *SYNTHETIC_SQL_ARGS.get(route, ()))) is True
    except (asyncpg.PostgresError, OSError) as exc:
        log.warning("booking_e2e_synthetic_unknown route=%s error_class=%s", route, type(exc).__name__)
        return False


async def check_synthetic(
    conn: asyncpg.Connection | asyncpg.Pool,
    writer: E2EWriter,
    method: str,
    route: str | None,
    params: Mapping[str, Any],
) -> None:
    """For an allowed write: 403 ``e2e_synthetic_only`` unless the record's agency is synthetic. No-op for reads."""
    if method.upper() in READ_METHODS:
        return
    if route is None or not await is_synthetic_record(conn, route, params):
        log.warning("booking_e2e_writer_refused identity=%s reason=not_synthetic route=%s", writer.name, route)
        raise HTTPException(status_code=403, detail=E2E_SYNTHETIC_ONLY)
    log.info("booking_e2e_write_allowed identity=%s route=%s", writer.name, route)
