"""AA-1175 / AA-1158 (S290, root ADR 0006a A10e-6): the Dev-only E2E pricing checker decides the E2E owner's
group-size band changes, the pricing maker enters a bill of its synthetic supplier on a synthetic agency's trip case,
and the checker approves those bills (:mod:`app.auth.e2e_staff`, :mod:`app.auth.e2e_staff_finance`,
:mod:`app.auth.e2e_staff_bodies`).

Through the real app with local RSA keys + fake JWKS and a scripted pool (no network, no DB):

- the approvals carry no body, the rejection a marked reason only, the bill a marked bill number, a trip case, no
  departure, no notes and plain lines: else 403 ``e2e_read_only`` before any DB access;
- every neighbour write stays 403 ``e2e_read_only`` before any DB access: the policy propose / submit / withdraw /
  discard, the maker's decisions, the checker's bill entry, bill edit / cancel / payouts, the other E2E writers;
- the decision reaches the route only for a band change the E2E owner made, the bill only for the maker's synthetic
  supplier and a synthetic agency's case, the bill approval only for the maker's own synthetic bill: else 403
  ``e2e_synthetic_only`` before the staff row sync.

The SQL itself runs on PostgreSQL in tests/integration/test_aa1175_e2e_policy_bills.py. All names are invented.
"""

from __future__ import annotations

from typing import Any

import asyncpg
import pytest
from fastapi import HTTPException

from app.auth import e2e_owner, e2e_staff, e2e_staff_finance
from app.pricing import pax_bands
from tests.staff_tokens import LocalCis
from tests.unit.fakes import ScriptedConn
from tests.unit.test_aa911_preview_cap import IDENTITY_SELECT, auth
from tests.unit.test_aa1091_e2e_pricing_pair import CHECKER, MAKER, _client, _detail, _row, _staff_calls, _token

VERSION = "2d3e4f5a-6b7c-4d8e-9f0a-1b2c3d4e5f6a"
SUPPLIER = "3e4f5a6b-7c8d-4e9f-8a1b-2c3d4e5f6a7b"
CASE = "4f5a6b7c-8d9e-4f0a-9b2c-3d4e5f6a7b8c"
BOOKING = "5a6b7c8d-9e0f-4a1b-8c3d-4e5f6a7b8c9d"
BILL = "6b7c8d9e-0f1a-4b2c-9d4e-5f6a7b8c9d0e"
POLICY_GUARD = "FROM booking.pricing_policy pp LEFT JOIN"
SUPPLY_GUARD = "s.company_name LIKE 'SYNTHETIC%' AS is_synthetic"
CASE_GUARD = "FROM booking.trip_cases tc JOIN catalog.v_tenant_account_v1"
BILL_GUARD = "FROM booking.supplier_bills b JOIN booking.suppliers s"
GUARDS = (POLICY_GUARD, SUPPLY_GUARD, CASE_GUARD, BILL_GUARD)

BILL_BODY = {
    "supplier_id": SUPPLIER,
    "trip_case": "TC-000123",
    "bill_number": "SYNTHETIC-RIVERBEND-1",
    "currency": "USD",
    "amount": "60.00",
    "issue_date": "2031-03-01",
    "due_date": "2031-03-15",
    "notes": None,
    "lines": [{"supplier_booking_id": BOOKING, "amount": "60.00"}],
}
REJECT_BODY = {"reason": "SYNTHETIC band check"}
IDEM = {"Idempotency-Key": "synthetic-bill-check-0001"}
APPROVE_POLICY = ("POST", f"/booking/pricing/policy/{VERSION}/approve", None)
REJECT_POLICY = ("POST", f"/booking/pricing/policy/{VERSION}/reject", REJECT_BODY)
ENTER_BILL = ("POST", "/booking/supplier-bills", BILL_BODY)
APPROVE_BILL = ("POST", f"/booking/supplier-bills/{BILL}/approve", None)


@pytest.fixture(autouse=True)
def dev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "dev")


def _guard_calls(conn: ScriptedConn) -> list[str]:
    return [sql for sql, _ in conn.calls if any(g in sql for g in GUARDS)]


# ── the module ──────────────────────────────────────────────────────────────────────────────────


def test_the_writes_are_split_between_the_pair_and_held_by_no_one_else() -> None:
    maker, checker = e2e_staff.E2E_WRITERS[MAKER], e2e_staff.E2E_WRITERS[CHECKER]
    new = {e2e_staff.POLICY_APPROVE, e2e_staff.POLICY_REJECT, e2e_staff.BILL_CREATE, e2e_staff.BILL_APPROVE}
    assert maker.writes & new == {e2e_staff.BILL_CREATE}
    assert checker.writes & new == {e2e_staff.POLICY_APPROVE, e2e_staff.POLICY_REJECT, e2e_staff.BILL_APPROVE}
    for email, writer in e2e_staff.E2E_WRITERS.items():
        if email not in (MAKER, CHECKER):
            assert not (writer.writes & new), email
    # No route-scoped grant: contracting already holds bill.write, bill.approve and pricing_policy.approve.
    assert not checker.grants and set(maker.grants) == {e2e_staff.DESK_ENTRY}
    assert e2e_staff.FINANCE_CHECKED == {route for _, route in new}
    # The only parameter decided is the group-size bands, and only the A10h owner's changes.
    assert e2e_staff_finance.POLICY_PARAM == pax_bands.KEY
    assert e2e_owner.E2E_OWNER_EMAIL == "e2e-owner@e2e-mail.lumiguides.it.com"


@pytest.mark.parametrize(
    ("write", "body", "ok"),
    [
        (e2e_staff.POLICY_APPROVE, None, True),
        (e2e_staff.POLICY_APPROVE, {}, True),
        (e2e_staff.POLICY_APPROVE, {"reason": "SYNTHETIC ok"}, False),
        (e2e_staff.POLICY_REJECT, REJECT_BODY, True),
        (e2e_staff.POLICY_REJECT, {"reason": "bands look wrong"}, False),
        (e2e_staff.POLICY_REJECT, {}, False),
        (e2e_staff.POLICY_REJECT, {**REJECT_BODY, "value": [[1, None]]}, False),
        (e2e_staff.BILL_APPROVE, None, True),
        (e2e_staff.BILL_APPROVE, {"note": "x"}, False),
        (e2e_staff.BILL_CREATE, BILL_BODY, True),
        (e2e_staff.BILL_CREATE, {k: v for k, v in BILL_BODY.items() if k != "notes"}, True),
        (e2e_staff.BILL_CREATE, {**BILL_BODY, "trip_case": CASE}, True),
        (e2e_staff.BILL_CREATE, {**BILL_BODY, "bill_number": "RB-2031-17"}, False),
        (e2e_staff.BILL_CREATE, {**BILL_BODY, "notes": "call the river lodge"}, False),
        (e2e_staff.BILL_CREATE, {**BILL_BODY, "departure": CASE}, False),
        (e2e_staff.BILL_CREATE, {**BILL_BODY, "trip_case": None}, False),
        (e2e_staff.BILL_CREATE, {**BILL_BODY, "trip_case": " "}, False),
        (e2e_staff.BILL_CREATE, {k: v for k, v in BILL_BODY.items() if k != "trip_case"}, False),
        (e2e_staff.BILL_CREATE, {**BILL_BODY, "lines": []}, False),
        (e2e_staff.BILL_CREATE, {**BILL_BODY, "lines": [{"supplier_booking_id": BOOKING}]}, False),
        (e2e_staff.BILL_CREATE, {**BILL_BODY, "lines": [{**BILL_BODY["lines"][0], "memo": "x"}]}, False),
        (e2e_staff.BILL_CREATE, [BILL_BODY], False),
    ],
)
def test_the_body_checks(write: tuple[str, str], body: Any, ok: bool) -> None:
    writer = e2e_staff.E2E_WRITERS[CHECKER if write != e2e_staff.BILL_CREATE else MAKER]
    if ok:
        e2e_staff.check_body(writer, *write, body)
    else:
        with pytest.raises(HTTPException) as err:
            e2e_staff.check_body(writer, *write, body)
        assert err.value.detail == "e2e_read_only"


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [True, False, None])
async def test_a_policy_decision_is_one_query_on_the_owner_and_the_key(answer: Any) -> None:
    conn = ScriptedConn().on(POLICY_GUARD, answer)
    for route in (e2e_staff.POLICY_APPROVE[1], e2e_staff.POLICY_REJECT[1]):
        assert await e2e_staff.is_synthetic_record(conn, route, {"version_id": VERSION}) is (answer is True)
    assert conn.sql(POLICY_GUARD) == [(VERSION, e2e_owner.E2E_OWNER_EMAIL, "price_pax_bands")] * 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("supplier", "case", "expected"), [(True, True, True), (False, True, False), (True, False, False)]
)
async def test_a_bill_needs_the_synthetic_supplier_and_a_synthetic_agency_case(
    supplier: bool, case: bool, expected: bool
) -> None:
    conn = ScriptedConn().on(SUPPLY_GUARD, supplier).on(CASE_GUARD, case)
    route = e2e_staff.BILL_CREATE[1]
    assert await e2e_staff.is_synthetic_record(conn, route, {}, BILL_BODY) is expected
    assert conn.sql(SUPPLY_GUARD) == [(SUPPLIER, MAKER)]
    assert conn.sql(CASE_GUARD) == ([("TC-000123",)] if supplier else [])  # by code, upper-cased


@pytest.mark.asyncio
@pytest.mark.parametrize(("ref", "bound"), [(BILL, BILL), ("bil-000042", "BIL-000042")])
async def test_a_bill_approval_is_one_query_on_the_maker_s_bill(ref: str, bound: str) -> None:
    conn = ScriptedConn().on(BILL_GUARD, True)
    assert await e2e_staff.is_synthetic_record(conn, e2e_staff.BILL_APPROVE[1], {"bill": ref}) is True
    assert conn.sql(BILL_GUARD) == [(bound, MAKER)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route", "params", "body"),
    [
        (e2e_staff.POLICY_APPROVE[1], {"version_id": "price_pax_bands"}, None),
        (e2e_staff.POLICY_REJECT[1], {}, REJECT_BODY),
        (e2e_staff.BILL_CREATE[1], {}, {**BILL_BODY, "supplier_id": "Riverbend Lodges"}),
        (e2e_staff.BILL_CREATE[1], {}, {**BILL_BODY, "trip_case": "Lotus booking 12"}),
        (e2e_staff.BILL_CREATE[1], {}, None),
        (e2e_staff.BILL_APPROVE[1], {"bill": "SPO-000042"}, None),
        (e2e_staff.BILL_APPROVE[1], {}, None),
    ],
)
async def test_a_bad_reference_counts_as_real_without_a_query(route: str, params: dict[str, Any], body: Any) -> None:
    conn = ScriptedConn().on(POLICY_GUARD, True).on(SUPPLY_GUARD, True).on(CASE_GUARD, True).on(BILL_GUARD, True)
    assert await e2e_staff.is_synthetic_record(conn, route, params, body) is False
    assert conn.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route", "params", "body"),
    [
        (e2e_staff.POLICY_APPROVE[1], {"version_id": VERSION}, None),
        (e2e_staff.BILL_CREATE[1], {}, BILL_BODY),
        (e2e_staff.BILL_APPROVE[1], {"bill": BILL}, None),
    ],
)
async def test_a_database_error_counts_as_real(route: str, params: dict[str, Any], body: Any) -> None:
    class Broken(ScriptedConn):
        async def fetchval(self, sql: str, *args: Any, **kw: Any) -> Any:
            raise asyncpg.PostgresError("down")

    assert await e2e_staff.is_synthetic_record(Broken(), route, params, body) is False


# ── the real app ────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("email", "request_"),
    [
        # Neighbour policy writes: the checker only approves or rejects; the maker never decides.
        (CHECKER, ("POST", "/booking/pricing/policy", {"key": "price_pax_bands", "value": [[1, None]]})),
        (CHECKER, ("POST", f"/booking/pricing/policy/{VERSION}/submit", None)),
        (CHECKER, ("POST", f"/booking/pricing/policy/{VERSION}/withdraw", None)),
        (CHECKER, ("POST", f"/booking/pricing/policy/{VERSION}/discard", REJECT_BODY)),
        (CHECKER, ("POST", f"/booking/pricing/policy/{VERSION}/reject", {"reason": "bands look wrong"})),
        (CHECKER, ("POST", f"/booking/pricing/rules/{VERSION}/approve", None)),
        (MAKER, APPROVE_POLICY),
        (MAKER, REJECT_POLICY),
        (MAKER, ("POST", "/booking/pricing/policy", {"key": "price_pax_bands", "value": [[1, None]]})),
        # Neighbour bill writes: the checker never enters, the maker never approves; nobody edits, cancels or pays.
        (CHECKER, ENTER_BILL),
        (MAKER, APPROVE_BILL),
        (MAKER, ("POST", "/booking/supplier-bills", {**BILL_BODY, "bill_number": "RB-2031-17"})),
        (MAKER, ("POST", "/booking/supplier-bills", {**BILL_BODY, "notes": "call the river lodge"})),
        (MAKER, ("PATCH", f"/booking/supplier-bills/{BILL}", {"amount": "70.00"})),
        (MAKER, ("POST", f"/booking/supplier-bills/{BILL}/cancel", {"reason": "SYNTHETIC"})),
        (CHECKER, ("POST", f"/booking/supplier-bills/{BILL}/cancel", {"reason": "SYNTHETIC"})),
        (CHECKER, ("POST", f"/booking/supplier-bills/{BILL}/payouts", {"amount": "60.00"})),
        (MAKER, ("POST", f"/booking/supplier-bills/{BILL}/payouts", {"amount": "60.00"})),
        (CHECKER, ("POST", f"/booking/supplier-bills/{BILL}/approve", {"note": "x"})),
    ],
)
def test_neighbour_writes_are_403_before_any_db_access(email: str, request_: tuple[str, str, Any]) -> None:
    method, path, body = request_
    cis, conn = LocalCis(), ScriptedConn()
    with _client(cis, conn) as c:
        r = c.request(method, path, json=body, headers={**auth(_token(cis, email)), **IDEM})
    assert (r.status_code, r.json()["detail"]) == (403, "e2e_read_only"), path
    assert conn.calls == []


@pytest.mark.parametrize("synthetic", [False, None])
@pytest.mark.parametrize(
    ("email", "request_", "guard"),
    [
        (CHECKER, APPROVE_POLICY, POLICY_GUARD),
        (CHECKER, REJECT_POLICY, POLICY_GUARD),
        (MAKER, ENTER_BILL, SUPPLY_GUARD),
        (CHECKER, APPROVE_BILL, BILL_GUARD),
    ],
)
def test_real_records_are_refused_before_the_staff_sync(
    email: str, request_: tuple[str, str, Any], guard: str, synthetic: Any
) -> None:
    method, path, body = request_
    cis = LocalCis()
    conn = ScriptedConn().on(guard, synthetic).on(IDENTITY_SELECT, _row(email))
    with _client(cis, conn) as c:
        r = c.request(method, path, json=body, headers={**auth(_token(cis, email)), **IDEM})
    assert (r.status_code, r.json()["detail"]) == (403, "e2e_synthetic_only"), path
    assert _staff_calls(conn) == [] and len(conn.calls) == 1


def test_a_bill_on_a_real_agency_case_is_refused_before_the_staff_sync() -> None:
    method, path, body = ENTER_BILL
    cis = LocalCis()
    conn = ScriptedConn().on(SUPPLY_GUARD, True).on(CASE_GUARD, False).on(IDENTITY_SELECT, _row(MAKER))
    with _client(cis, conn) as c:
        r = c.request(method, path, json=body, headers={**auth(_token(cis, MAKER)), **IDEM})
    assert (r.status_code, r.json()["detail"]) == (403, "e2e_synthetic_only")
    assert _staff_calls(conn) == [] and len(_guard_calls(conn)) == 2


@pytest.mark.parametrize(
    ("email", "request_"),
    [(CHECKER, APPROVE_POLICY), (CHECKER, REJECT_POLICY), (MAKER, ENTER_BILL), (CHECKER, APPROVE_BILL)],
)
def test_synthetic_writes_reach_the_route(email: str, request_: tuple[str, str, Any]) -> None:
    method, path, body = request_
    cis = LocalCis()
    conn = ScriptedConn()
    for guard in GUARDS:
        conn.on(guard, True)
    conn.on(IDENTITY_SELECT, _row(email))
    with _client(cis, conn) as c:
        r = c.request(method, path, json=body, headers={**auth(_token(cis, email)), **IDEM})
    detail = _detail(r)
    assert r.status_code != 422, r.text
    assert detail != "forbidden", r.text  # the role's own gate (bill.write / bill.approve / pricing_policy.read) passed
    assert not isinstance(detail, str) or not detail.startswith("e2e_"), r.text
    flat = [sql for sql, _ in conn.calls]
    assert len(flat) > flat.index(_staff_calls(conn)[0]) + 1, path  # the route ran past the staff dependency


@pytest.mark.parametrize("request_", [APPROVE_POLICY, ENTER_BILL, APPROVE_BILL])
def test_the_finance_pair_and_the_staff_writer_still_cannot(request_: tuple[str, str, Any]) -> None:
    # The A10e finance maker / checker and the supplier-ops writer hold none of these writes.
    from tests.unit.test_aa1087_e2e_writers import CHECKER as FIN_CHECKER
    from tests.unit.test_aa1087_e2e_writers import MAKER as FIN_MAKER
    from tests.unit.test_aa1087_e2e_writers import STAFF
    from tests.unit.test_aa1087_e2e_writers import _token as writer_token

    method, path, body = request_
    for email in (FIN_MAKER, FIN_CHECKER, STAFF):
        cis, conn = LocalCis(), ScriptedConn()
        with _client(cis, conn) as c:
            r = c.request(method, path, json=body, headers={**auth(writer_token(cis, email)), **IDEM})
        assert (r.status_code, r.json()["detail"]) == (403, "e2e_read_only"), email
        assert conn.calls == []
