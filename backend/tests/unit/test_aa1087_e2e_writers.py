"""AA-1087 (S278, root ADR 0006a A10e): the Dev-only write-capable E2E staff identities (:mod:`app.auth.e2e_staff`).

Through the real app with local RSA keys + fake JWKS and a scripted pool (no network, no DB): each identity is refused
outside Dev (reads too) and with any other group set before any DB access; every write outside its own allow-list is
403 ``e2e_read_only`` before any DB access; an allow-listed write reaches the route only when the record's agency is
synthetic (else 403 ``e2e_synthetic_only``, before the staff row is synced); reads keep the role's permissions; the
maker never approves and the checker never records; other staff are unaffected. The SQL itself runs on PostgreSQL in
tests/integration/test_aa1087_e2e_writers.py.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

import asyncpg
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.auth import e2e_staff, identity_config
from app.main import create_app
from tests.staff_tokens import BOOKING_STAFF_CLIENT, LocalCis
from tests.unit.fakes import ScriptedConn
from tests.unit.test_aa911_preview_cap import DSN, IDENTITY_SELECT, FakeDatabase, auth, cognito_row

STAFF = "e2e-booking-staff@e2e-mail.lumiguides.it.com"
MAKER = "e2e-booking-maker@e2e-mail.lumiguides.it.com"
CHECKER = "e2e-booking-checker@e2e-mail.lumiguides.it.com"
GROUPS = {STAFF: ["booking-sales"], MAKER: ["booking-finance"], CHECKER: ["booking-finance"]}
ROLE = {STAFF: "sales", MAKER: "finance", CHECKER: "finance"}
RECORD = "6f1d2c3b-4a59-4e8d-9c7b-0a1b2c3d4e5f"
WRITES = {
    STAFF: [
        ("POST", f"/booking/supplier-bookings/{RECORD}/request", None),
        ("POST", f"/booking/supplier-bookings/{RECORD}/confirm", {"confirmation_ref": "RIDGE-77"}),
        ("PATCH", f"/booking/catalog/departures/{RECORD}", {"status": "cancelled"}),
    ],
    MAKER: [
        (
            "POST",
            f"/booking/agency-invoices/{RECORD}/payments",
            {
                "amount": "100.00",
                "method": "bank_transfer",
                "received_on": "2027-01-05",
                "document_ref": "statement line 7",
            },  # fmt: skip
        ),
    ],
    CHECKER: [
        ("POST", f"/booking/agency-payments/{RECORD}/approve", None),
        ("POST", f"/booking/agency-payments/{RECORD}/reject", {"reason": "duplicate line"}),
    ],
}
SYNTHETIC_FRAGMENT = "SELECT v.is_synthetic"


@pytest.fixture(autouse=True)
def dev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "dev")


def _client(cis: LocalCis, conn: ScriptedConn) -> TestClient:
    settings = replace(cis.settings(), database_url=DSN)
    verifier = identity_config.build_staff_verifier(settings, cis.fetch)
    return TestClient(create_app(settings, FakeDatabase(settings, conn), verifier), raise_server_exceptions=False)


def _token(cis: LocalCis, email: str, groups: list[str] | None = None) -> str:
    return cis.workforce.token(GROUPS[email] if groups is None else groups, email=email, client_id=BOOKING_STAFF_CLIENT)


def _row(email: str) -> dict[str, Any]:
    return {**cognito_row(ROLE[email]), "email": email, "name": email.split("@")[0], "cis_role": None}


def _staff_sql(conn: ScriptedConn) -> list[str]:
    """The staff row reads / writes (the departure rule's SQL also joins booking.staff_users: not one of them)."""
    return [sql for sql, _ in conn.calls if "booking.staff_users" in sql and SYNTHETIC_FRAGMENT not in sql]


# ── the module ──────────────────────────────────────────────────────────────────────────────────


def test_the_identities_and_their_writes_are_exactly_the_reviewed_ones() -> None:
    # Guard: a new address or write is a reviewed change of this test (root ADR 0006a A10e).
    assert {email: (w.groups, w.writes) for email, w in e2e_staff.E2E_WRITERS.items()} == {
        STAFF: (
            frozenset({"booking-sales"}),
            frozenset({e2e_staff.SUPPLIER_REQUEST, e2e_staff.SUPPLIER_CONFIRM, e2e_staff.DEPARTURE_CANCEL}),
        ),
        MAKER: (frozenset({"booking-finance"}), frozenset({e2e_staff.PAYMENT_RECORD})),
        CHECKER: (frozenset({"booking-finance"}), frozenset({e2e_staff.PAYMENT_APPROVE, e2e_staff.PAYMENT_REJECT})),
    }
    # The maker and the checker never share a write (maker-checker by identity).
    assert not (e2e_staff.E2E_WRITERS[MAKER].writes & e2e_staff.E2E_WRITERS[CHECKER].writes)
    # A10e-1: the only extra permission any identity ever holds, on exactly one write with a fixed body.
    assert {email: dict(w.grants) for email, w in e2e_staff.E2E_WRITERS.items()} == {
        STAFF: {e2e_staff.DEPARTURE_CANCEL: frozenset({"trip_plan.write"})},
        MAKER: {},
        CHECKER: {},
    }
    assert dict(e2e_staff.BODY_RULES) == {e2e_staff.DEPARTURE_CANCEL: {"status": "cancelled"}}
    for writer in e2e_staff.E2E_WRITERS.values():
        assert set(writer.grants) <= writer.writes
        assert set(writer.grants) <= set(e2e_staff.BODY_RULES)


def test_every_allowed_write_is_a_real_route_with_a_synthetic_check() -> None:
    app = create_app(replace(LocalCis().settings(), database_url=DSN))
    paths = app.openapi()["paths"]
    allowed = {w for writer in e2e_staff.E2E_WRITERS.values() for w in writer.writes}
    for method, route in allowed:
        assert method.lower() in paths.get(route, {}), route
        param, sql = e2e_staff.SYNTHETIC_SQL[route]
        assert "{" + param + "}" in route and "is_synthetic" in sql and "$1" in sql
    assert set(e2e_staff.SYNTHETIC_SQL) == {route for _, route in allowed}


@pytest.mark.parametrize("email", [STAFF, STAFF.upper(), f"  {MAKER} ", CHECKER])
def test_writer_for_matches_the_exact_address(email: str) -> None:
    assert e2e_staff.writer_for(email) is e2e_staff.E2E_WRITERS[email.strip().lower()]


@pytest.mark.parametrize(
    "email",
    [
        None,
        "",
        "e2e-booking@e2e-mail.lumiguides.it.com",
        "e2e-booking-staff@lumiguides.it.com",
        "e2e-booking-staff@e2e-mail.lumiguides.it.com.example",
        "thu@adventure.asia",
        "e2e-booKing-staff@e2e-mail.lumiguides.it.com",
    ],  # fmt: skip
)
def test_writer_for_ignores_other_addresses(email: str | None) -> None:
    assert e2e_staff.writer_for(email) is None


@pytest.mark.parametrize("value", [None, "", "Dev", "dev ", "prod", "test", "local", "staging"])
def test_dev_only_environment_is_an_exact_match(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    if value is None:
        monkeypatch.delenv("ENVIRONMENT", raising=False)
    else:
        monkeypatch.setenv("ENVIRONMENT", value)
    assert e2e_staff.dev_only_environment() is False


class _Boom:
    async def fetchval(self, *_: Any) -> Any:
        raise asyncpg.UndefinedColumnError("column v.is_synthetic does not exist")


@pytest.mark.asyncio
async def test_a_database_error_counts_as_real() -> None:
    assert await e2e_staff.is_synthetic_record(_Boom(), e2e_staff.SUPPLIER_CONFIRM[1], {"booking_id": RECORD}) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route", "params"),
    [
        ("/booking/trip-cases", {}),
        (e2e_staff.SUPPLIER_CONFIRM[1], {"booking_id": "not-a-uuid"}),
        (e2e_staff.PAYMENT_APPROVE[1], {"payment_id": "AAS-2027-000001"}),
        (e2e_staff.PAYMENT_RECORD[1], {"ref": "x" * 65}),
        (e2e_staff.PAYMENT_RECORD[1], {}),
    ],
)
async def test_unknown_routes_and_bad_ids_count_as_real(route: str, params: dict[str, Any]) -> None:
    conn = ScriptedConn().on(SYNTHETIC_FRAGMENT, True)
    assert await e2e_staff.is_synthetic_record(conn, route, params) is False
    assert conn.calls == []


@pytest.mark.asyncio
async def test_an_invoice_number_is_looked_up_by_number() -> None:
    conn = ScriptedConn().on("WHERE i.number = $1", True)
    assert await e2e_staff.is_synthetic_record(conn, e2e_staff.PAYMENT_RECORD[1], {"ref": "AAS-2027-000901"})
    assert conn.sql("WHERE i.number = $1") == [("AAS-2027-000901",)]


# ── the real app ────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("email", [STAFF, MAKER, CHECKER])
@pytest.mark.parametrize("env", [None, "prod", "test", "Dev"])
def test_outside_dev_every_request_is_refused_before_any_db_access(
    monkeypatch: pytest.MonkeyPatch, email: str, env: str | None
) -> None:
    if env is None:
        monkeypatch.delenv("ENVIRONMENT", raising=False)
    else:
        monkeypatch.setenv("ENVIRONMENT", env)
    cis, conn = LocalCis(), ScriptedConn()
    with _client(cis, conn) as c:
        r = c.get("/booking/staff/me", headers=auth(_token(cis, email)))
    assert (r.status_code, r.json()["detail"]) == (403, "e2e_not_allowed_here")
    assert conn.calls == []


@pytest.mark.parametrize(
    ("email", "groups"),
    [
        (STAFF, ["booking-admin"]),
        (STAFF, ["booking-sales", "booking-finance"]),
        (STAFF, ["booking-sales", "cis-admin"]),
        (MAKER, ["booking-sales"]),
        (CHECKER, ["booking-finance", "booking-admin"]),
        (CHECKER, []),
    ],
)
def test_any_other_group_set_is_refused(email: str, groups: list[str]) -> None:
    cis, conn = LocalCis(), ScriptedConn()
    with _client(cis, conn) as c:
        r = c.get("/booking/staff/me", headers=auth(_token(cis, email, groups)))
    assert (r.status_code, r.json()["detail"]) == (403, "e2e_scope")
    assert conn.calls == []


@pytest.mark.parametrize("email", [STAFF, MAKER, CHECKER])
def test_reads_keep_the_role_permissions(email: str) -> None:
    cis = LocalCis()
    conn = ScriptedConn().on(IDENTITY_SELECT, _row(email))
    with _client(cis, conn) as c:
        r = c.get("/booking/staff/me", headers=auth(_token(cis, email)))
    assert r.status_code == 200, r.text
    assert r.json()["role"] == ROLE[email]
    assert conn.sql(SYNTHETIC_FRAGMENT) == []


OTHER_WRITES = [
    ("POST", "/booking/trip-cases", {"title": "x"}),
    ("PATCH", f"/booking/supplier-bookings/{RECORD}", {"notes": "x"}),
    ("POST", f"/booking/supplier-bookings/{RECORD}/decline", {"decline_code": "sold_out"}),
    ("POST", f"/booking/supplier-bookings/{RECORD}/cancel", {"reason": "x"}),
    ("POST", f"/booking/agency-invoices/{RECORD}/issue", None),
    ("POST", f"/booking/agency-invoices/{RECORD}/void", {"reason": "x"}),
    ("POST", "/booking/agency-billing/runs", {}),
    ("PUT", "/booking/billing-terms/default", {}),
    ("POST", "/booking/retention/runs", {}),
    ("PUT", "/booking/staff/me/preferences", {"notify": {}}),
]


@pytest.mark.parametrize("email", [STAFF, MAKER, CHECKER])
@pytest.mark.parametrize(("method", "path", "body"), OTHER_WRITES)
def test_writes_outside_the_allow_list_are_403_before_any_db_access(
    email: str, method: str, path: str, body: Any
) -> None:
    cis, conn = LocalCis(), ScriptedConn()
    with _client(cis, conn) as c:
        r = c.request(method, path, json=body, headers=auth(_token(cis, email)))
    assert (r.status_code, r.json()["detail"]) == (403, "e2e_read_only")
    assert conn.calls == []


@pytest.mark.parametrize(
    ("email", "other"),
    [(MAKER, CHECKER), (CHECKER, MAKER), (STAFF, MAKER), (MAKER, STAFF), (CHECKER, STAFF)],
)
def test_an_identity_never_makes_another_identitys_write(email: str, other: str) -> None:
    cis = LocalCis()
    for method, path, body in WRITES[other]:
        conn = ScriptedConn()
        with _client(cis, conn) as c:
            r = c.request(method, path, json=body, headers=auth(_token(cis, email)))
        assert (r.status_code, r.json()["detail"]) == (403, "e2e_read_only"), path
        assert conn.calls == []


@pytest.mark.parametrize("synthetic", [False, None])
@pytest.mark.parametrize("email", [STAFF, MAKER, CHECKER])
def test_an_allowed_write_on_a_real_agency_is_refused_before_the_staff_sync(email: str, synthetic: Any) -> None:
    cis = LocalCis()
    for method, path, body in WRITES[email]:
        conn = ScriptedConn().on(SYNTHETIC_FRAGMENT, synthetic).on(IDENTITY_SELECT, _row(email))
        with _client(cis, conn) as c:
            r = c.request(method, path, json=body, headers=auth(_token(cis, email)))
        assert (r.status_code, r.json()["detail"]) == (403, "e2e_synthetic_only"), path
        # The departure rule also binds the A10e staff identity's address (the creator of a seeded departure).
        expected = (RECORD, e2e_staff.E2E_BOOKING_STAFF_EMAIL) if "/catalog/departures/" in path else (RECORD,)
        assert [args for args in conn.sql(SYNTHETIC_FRAGMENT)] == [expected]
        assert _staff_sql(conn) == []  # refused before the staff row is read or written
        assert len(conn.calls) == 1


@pytest.mark.parametrize("email", [STAFF, MAKER, CHECKER])
def test_an_allowed_write_on_a_synthetic_agency_reaches_the_route(email: str) -> None:
    cis = LocalCis()
    for method, path, body in WRITES[email]:
        conn = ScriptedConn().on(SYNTHETIC_FRAGMENT, True).on(IDENTITY_SELECT, _row(email))
        with _client(cis, conn) as c:
            r = c.request(method, path, json=body, headers={**auth(_token(cis, email)), "Idempotency-Key": "k" * 12})
        detail = r.json().get("detail") if r.headers.get("content-type", "").startswith("application/json") else None
        assert r.status_code != 422, r.text
        assert not isinstance(detail, str) or not detail.startswith("e2e_"), path
        flat = [sql for sql, _ in conn.calls]
        first_staff = flat.index(_staff_sql(conn)[0])
        assert flat.index(next(s for s in flat if SYNTHETIC_FRAGMENT in s)) < first_staff
        # The route ran past the staff dependency: it read its own record (a route SQL after the staff sync).
        assert len(flat) > first_staff + 1, path


def test_a_real_finance_member_is_unaffected() -> None:
    """Control: a company finance user records a payment without any synthetic check."""
    cis = LocalCis()
    conn = ScriptedConn().on(IDENTITY_SELECT, {**cognito_row("finance"), "cis_role": None})
    with _client(cis, conn) as c:
        token = cis.workforce.token(["booking-finance"], client_id=BOOKING_STAFF_CLIENT)
        method, path, body = WRITES[MAKER][0]
        r = c.request(method, path, json=body, headers={**auth(token), "Idempotency-Key": "k" * 12})
    assert r.status_code != 403 or r.json().get("detail") not in {"e2e_read_only", "e2e_synthetic_only"}
    assert conn.sql(SYNTHETIC_FRAGMENT) == []


def test_the_a10a_user_is_still_read_only() -> None:
    cis, conn = LocalCis(), ScriptedConn()
    with _client(cis, conn) as c:
        token = cis.workforce.token(["booking-admin"], email="e2e-booking@e2e-mail.lumiguides.it.com")
        method, path, body = WRITES[CHECKER][0]
        r = c.request(method, path, json=body, headers=auth(token))
    assert (r.status_code, r.json()["detail"]) == (403, "e2e_read_only")
    assert conn.calls == []


def test_uuid_helper() -> None:
    assert e2e_staff._uuid(uuid.UUID(RECORD)) == RECORD
    assert e2e_staff._uuid("nope") is None


def test_the_writer_smoke_is_agent_only() -> None:
    """The Playwright writer smoke writes on Dev and reads codes only the MFA admin role can read: no workflow may set
    its switch, and the spec skips without it (root ADR 0006a A10e)."""
    root = Path(__file__).resolve().parents[3]
    for wf in sorted((root / ".github" / "workflows").glob("*.y*ml")):
        assert "BOOKING_E2E_WRITERS" not in wf.read_text(encoding="utf-8"), wf.name
    spec = (root / "frontend" / "e2e" / "e2e-writers" / "writes.spec.ts").read_text(encoding="utf-8")
    assert 'process.env.BOOKING_E2E_WRITERS === "1"' in spec
    assert spec.count("test.skip(!ON") == 5
    for email in (STAFF, MAKER, CHECKER):
        assert f'"{email}"' in spec
    assert 'MAILBOX_ADMIN_PREFIX = "inbound-admin/"' in spec


# ── A10e-1 (S278 l18): departure cancel on a synthetic departure ─────────────────────────────────


DEPARTURE = f"/booking/catalog/departures/{RECORD}"


@pytest.mark.parametrize(
    "body",
    [
        None,
        {},
        {"status": "guaranteed"},
        {"status": "cancelled", "capacity": 0},
        {"capacity": 4},
        {"status": "Cancelled"},
        ["status", "cancelled"],
    ],
)
def test_a_departure_patch_other_than_the_cancel_is_refused_before_any_db_access(body: Any) -> None:
    cis, conn = LocalCis(), ScriptedConn()
    with _client(cis, conn) as c:
        r = c.request("PATCH", DEPARTURE, json=body, headers=auth(_token(cis, STAFF)))
    assert (r.status_code, r.json()["detail"]) == (403, "e2e_read_only")
    assert conn.calls == []


def test_a_departure_cancel_that_is_not_json_is_refused() -> None:
    """FastAPI refuses a body that is not JSON (422) before any dependency; nothing is read or written either way."""
    cis, conn = LocalCis(), ScriptedConn()
    with _client(cis, conn) as c:
        r = c.request(
            "PATCH",
            DEPARTURE,
            content=b"status=cancelled",
            headers={**auth(_token(cis, STAFF)), "content-type": "application/json"},
        )
    assert r.status_code in (403, 422)
    assert conn.calls == []


@pytest.mark.parametrize("synthetic", [False, None])
def test_a_departure_cancel_on_a_real_departure_is_refused(synthetic: Any) -> None:
    """A departure with no agency hold, or one a real agency held seats on, answers false (or no row): refused before
    the staff row sync and before the route reads or changes the departure."""
    cis = LocalCis()
    conn = ScriptedConn().on(SYNTHETIC_FRAGMENT, synthetic).on(IDENTITY_SELECT, _row(STAFF))
    with _client(cis, conn) as c:
        r = c.request("PATCH", DEPARTURE, json={"status": "cancelled"}, headers=auth(_token(cis, STAFF)))
    assert (r.status_code, r.json()["detail"]) == (403, "e2e_synthetic_only")
    assert conn.sql(SYNTHETIC_FRAGMENT) == [(RECORD, e2e_staff.E2E_BOOKING_STAFF_EMAIL)]
    assert len(conn.calls) == 1  # the route never locked, read or changed the departure


def test_the_cancel_grant_reaches_only_that_request() -> None:
    """On a synthetic departure the cancel passes the route's trip_plan.write check (the route then looks up the
    departure); the same identity's other writes and reads never hold it."""
    cis = LocalCis()
    conn = ScriptedConn().on(SYNTHETIC_FRAGMENT, True).on(IDENTITY_SELECT, _row(STAFF))
    with _client(cis, conn) as c:
        r = c.request("PATCH", DEPARTURE, json={"status": "cancelled"}, headers=auth(_token(cis, STAFF)))
        me = c.get("/booking/staff/me", headers=auth(_token(cis, STAFF)))
    assert r.status_code == 404, r.text  # departure_not_found from the route: past the permission check
    assert any("booking.departures" in sql and "FOR UPDATE" in sql for sql, _ in conn.calls)
    assert "trip_plan.write" not in me.json()["permissions"]


def test_a_real_sales_member_still_cannot_patch_a_departure() -> None:
    cis = LocalCis()
    conn = ScriptedConn().on(IDENTITY_SELECT, {**cognito_row("sales"), "cis_role": None})
    with _client(cis, conn) as c:
        token = cis.workforce.token(["booking-sales"], client_id=BOOKING_STAFF_CLIENT)
        r = c.request("PATCH", DEPARTURE, json={"status": "cancelled"}, headers=auth(token))
    assert (r.status_code, r.json()["detail"]) == (403, "forbidden")
    assert conn.sql(SYNTHETIC_FRAGMENT) == []


@pytest.mark.parametrize(("body", "ok"), [({"status": "cancelled"}, True), ({"status": "cancelled", "x": 1}, False)])
def test_check_body_rule(body: Any, ok: bool) -> None:
    writer = e2e_staff.E2E_WRITERS[STAFF]
    if ok:
        e2e_staff.check_body(writer, "PATCH", e2e_staff.DEPARTURE_CANCEL[1], body)
    else:
        with pytest.raises(HTTPException):
            e2e_staff.check_body(writer, "PATCH", e2e_staff.DEPARTURE_CANCEL[1], body)
    e2e_staff.check_body(writer, "POST", e2e_staff.SUPPLIER_CONFIRM[1], {"anything": 1})  # no rule: no check


def test_grants_for_is_exact() -> None:
    writer = e2e_staff.E2E_WRITERS[STAFF]
    assert writer.grants_for("patch", e2e_staff.DEPARTURE_CANCEL[1]) == frozenset({"trip_plan.write"})
    assert writer.grants_for("POST", e2e_staff.DEPARTURE_CANCEL[1]) == frozenset()
    assert writer.grants_for("PATCH", None) == frozenset()
    assert e2e_staff.E2E_WRITERS[MAKER].grants_for("PATCH", e2e_staff.DEPARTURE_CANCEL[1]) == frozenset()
