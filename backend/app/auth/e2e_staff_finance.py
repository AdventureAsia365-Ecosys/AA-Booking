"""Pricing policy decisions and supplier bills of the Dev-only E2E pricing pair (root ADR 0006a A10e-6, S290).

Beside :mod:`app.auth.e2e_staff` (the identities, their route allow-list and the synthetic-path SQL), like
:mod:`app.auth.e2e_staff_supply`. Same logger, same refusal codes. The record checks of the writes in
:data:`app.auth.e2e_staff.FINANCE_CHECKED` (:func:`check_record`), each run after the body check and before the staff
row sync or the route:

- **Policy decision (checker, AA-1175):** POST ``/booking/pricing/policy/{version_id}/approve`` or ``/reject`` only for
  a version of :data:`POLICY_PARAM` (the group-size bands) that the A10h E2E owner proposed (:data:`POLICY_SQL`). Never
  another parameter, never a real person's change. The second-person rule stays the service's (``policy_admin``: the
  table CHECK wants an actor other than the maker); the checker's ``contracting`` role holds the parameter's approver
  permission ``pricing_policy.approve``.
- **Bill entry (maker, AA-1158):** POST ``/booking/supplier-bills`` only when the supplier of the body is the maker's
  synthetic supplier (the A10e-3 rule) and the trip case of the body is a case of an agency CIS marks synthetic
  (``catalog.v_tenant_account_v1.is_synthetic``, the A10b-1 rule; by UUID or ``TC-`` code). The lines need no check of
  their own: the bk-045 trigger ``supplier_bill_items_guard`` refuses a line whose supplier booking is of another
  supplier or another trip case than the bill.
- **Bill approval (checker, AA-1158):** POST ``/booking/supplier-bills/{bill}/approve`` only for a bill that the E2E
  pricing maker entered and last edited (``created_by`` and ``facts_edited_by``), of the maker's synthetic supplier,
  on a synthetic agency's trip case (:data:`BILL_SQL`, by UUID or ``BIL-`` code). The service still refuses the bill's
  maker (403 ``maker_checker``); the checker never approves a real person's bill.

Fail closed: an unknown or malformed id, a missing row or column, or a database error answer False (403
``e2e_synthetic_only``).

TEMPORARY: removed with the A10 family at go-live (AA-1054), with :mod:`app.auth.e2e_staff`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

import asyncpg

from app.auth.e2e_owner import E2E_OWNER_EMAIL
from app.auth.e2e_staff import (
    BILL_APPROVE,
    BILL_CREATE,
    E2E_PRICING_MAKER_EMAIL,
    POLICY_APPROVE,
    POLICY_REJECT,
    SUPPLIER_SYNTHETIC_SQL,
    SYNTHETIC_SUPPLY_MARK,
    _lookup,
    _uuid,
)
from app.auth.e2e_staff_supply import _fetch

# The one pricing policy parameter the checker decides (app.pricing.pax_bands.KEY; a unit test keeps them equal). Bound
# as $3, never written into the SQL text.
POLICY_PARAM = "price_pax_bands"

# $1 the version, $2 the E2E owner's address, $3 POLICY_PARAM: True only for that parameter's version the owner made.
POLICY_SQL = (
    "SELECT coalesce(pp.param_key = $3 AND lower(su.email) = $2, false) AS is_synthetic"
    " FROM booking.pricing_policy pp LEFT JOIN booking.staff_users su ON su.cis_user_id = pp.created_by"
    " WHERE pp.id = $1::uuid"
)

# A trip case of an agency CIS marks synthetic (a B2C case has no agency: no row).
_CASE = (
    "SELECT v.is_synthetic IS TRUE AS is_synthetic FROM booking.trip_cases tc"
    " JOIN catalog.v_tenant_account_v1 v ON v.tenant_id = tc.party_tenant_id"
    " WHERE tc.deleted_at IS NULL AND "
)
CASE_SQL = _CASE + "tc.id = $1::uuid"
CASE_BY_CODE_SQL = _CASE + "tc.code = $1"
CASE_CODE = re.compile(r"TC-[0-9]{6,12}")

# $1 the bill, $2 the E2E pricing maker's address: entered and last edited by the maker, of the maker's synthetic
# supplier, on a synthetic agency's trip case (a bill of a departure or of no case has no agency: False).
_BILL = (
    "SELECT coalesce(lower(cu.email) = $2 AND b.facts_edited_by = b.created_by AND lower(su.email) = $2"  # noqa: S608 — constant fragments
    " AND s.company_name LIKE '" + SYNTHETIC_SUPPLY_MARK + "%' AND v.is_synthetic IS TRUE, false) AS is_synthetic"
    " FROM booking.supplier_bills b JOIN booking.suppliers s ON s.id = b.supplier_id"
    " LEFT JOIN booking.staff_users su ON su.cis_user_id = s.created_by"
    " LEFT JOIN booking.staff_users cu ON cu.cis_user_id = b.created_by"
    " LEFT JOIN booking.trip_cases tc ON tc.id = b.trip_case_id"
    " LEFT JOIN catalog.v_tenant_account_v1 v ON v.tenant_id = tc.party_tenant_id"
    " WHERE "
)
BILL_SQL = _BILL + "b.id = $1::uuid"
BILL_BY_CODE_SQL = _BILL + "b.code = $1"
BILL_CODE = re.compile(r"BIL-[0-9]{6,12}")


def _by_id_or_code(raw: Any, by_id: str, by_code: str, code: re.Pattern[str]) -> tuple[str, str] | None:
    """The SQL and bind value for a UUID or a code; None for anything else (counts as real)."""
    key = _uuid(raw)
    if key is not None:
        return by_id, key
    if isinstance(raw, str) and code.fullmatch(raw.strip().upper()):
        return by_code, raw.strip().upper()
    return None


async def _policy(conn: asyncpg.Connection | asyncpg.Pool, route: str, params: Mapping[str, Any]) -> bool:
    key = _uuid(params.get("version_id"))
    return key is not None and await _fetch(conn, route, POLICY_SQL, key, E2E_OWNER_EMAIL, POLICY_PARAM)


async def _bill_create(conn: asyncpg.Connection | asyncpg.Pool, body: Any) -> bool:
    if not isinstance(body, dict):
        return False
    supplier = _lookup("supplier_id", SUPPLIER_SYNTHETIC_SQL, body.get("supplier_id"))
    case = _by_id_or_code(body.get("trip_case"), CASE_SQL, CASE_BY_CODE_SQL, CASE_CODE)
    if supplier is None or case is None:
        return False
    route = BILL_CREATE[1]
    return await _fetch(conn, route, *supplier, E2E_PRICING_MAKER_EMAIL) and await _fetch(conn, route, *case)


async def _bill_approve(conn: asyncpg.Connection | asyncpg.Pool, params: Mapping[str, Any]) -> bool:
    found = _by_id_or_code(params.get("bill"), BILL_SQL, BILL_BY_CODE_SQL, BILL_CODE)
    return found is not None and await _fetch(conn, BILL_APPROVE[1], *found, E2E_PRICING_MAKER_EMAIL)


async def check_record(
    conn: asyncpg.Connection | asyncpg.Pool, route: str, params: Mapping[str, Any], body: Any
) -> bool:
    """A10e-6: True only when the write of :data:`app.auth.e2e_staff.FINANCE_CHECKED` touches synthetic records only."""
    if route in (POLICY_APPROVE[1], POLICY_REJECT[1]):
        return await _policy(conn, route, params)
    if route == BILL_CREATE[1]:
        return await _bill_create(conn, body)
    if route == BILL_APPROVE[1]:
        return await _bill_approve(conn, params)
    return False
