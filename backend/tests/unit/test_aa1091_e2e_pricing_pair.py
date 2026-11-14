"""AA-1091 (S279, root ADR 0006a A10e-2): the Dev-only E2E pricing pair (:mod:`app.auth.e2e_staff`).

The maker is the CIS A10d address holding the "Pricing team" preset (groups exactly ``cis-customer_success`` +
``booking-contracting``); the checker a Dev-only ``booking-contracting`` address. Through the real app with local RSA
keys + fake JWKS and a scripted pool (no network, no DB): exact groups and Dev only; every write outside each one's
allow-list is 403 ``e2e_read_only`` before any DB access; the workbench action body is ``make_sellable`` /
``make_not_sellable`` on tour keys only; components, versions and workbench writes reach the route only on a synthetic
plan (or an E2E maker's version of one), else 403 ``e2e_synthetic_only`` before the staff row sync; the workbook import
checks every tour inside the route and refuses a new departure; a real contracting member is unaffected. The SQL itself
runs on PostgreSQL in tests/integration/test_aa1091_e2e_pricing_pair.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

import asyncpg
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.auth import e2e_staff, identity_config
from app.catalog import price_import
from app.main import create_app
from tests.staff_tokens import BOOKING_STAFF_CLIENT, LocalCis
from tests.unit.fakes import ScriptedConn
from tests.unit.test_aa911_preview_cap import DSN, IDENTITY_SELECT, FakeDatabase, auth, cognito_row

MAKER = "e2e-cis-cs@e2e-mail.lumiguides.it.com"
CHECKER = "e2e-booking-pricing-checker@e2e-mail.lumiguides.it.com"
GROUPS = {MAKER: ["cis-customer_success", "booking-contracting"], CHECKER: ["booking-contracting"]}
TOUR = "3c0a9f21-7d4e-4b6a-9e15-2f8d7c6b5a41"
TOUR_2 = "8e7d6c5b-4a39-4281-9f0e-1d2c3b4a5968"
VERSION = "5b4a3928-1706-4f5e-8d9c-0b1a2c3d4e5f"
PLAN_ONE = "AS is_synthetic FROM booking.trip_plans tp WHERE tp.source_system = 'cis' AND tp.source_id = $1"
PLANS_MANY = "SELECT count(*) = $2::int AND coalesce(bool_and(s.ok), false)"
VERSION_SQL = "FROM booking.price_list_versions v LEFT JOIN booking.staff_users su"
GUARD_SQL = (PLAN_ONE, PLANS_MANY, VERSION_SQL)
COMPONENTS = ("PUT", f"/booking/catalog/trip-plans/{TOUR}/components", {"items": []})
WORKBENCH = ("POST", "/booking/catalog/price-workbench/actions", {"action": "make_sellable", "tour_keys": [TOUR]})
SAVE = (
    "PUT",
    f"/booking/catalog/price-versions/{VERSION}",
    {"label": "Synthetic", "travel_from": "2027-01-01", "travel_to": "2027-12-31", "items": []},
)
SUBMIT = ("POST", f"/booking/catalog/price-versions/{VERSION}/submit", {"reason": "synthetic rehearsal"})
APPROVE = ("POST", f"/booking/catalog/price-versions/{VERSION}/approve", {"diff_sha256": "a" * 64})
REJECT = ("POST", f"/booking/catalog/price-versions/{VERSION}/reject", {"reason": "synthetic rehearsal"})
WITHDRAW = ("POST", f"/booking/catalog/price-versions/{VERSION}/withdraw", {"reason": "synthetic rehearsal"})
VERSION_WRITES = {MAKER: [SAVE, SUBMIT, WITHDRAW, APPROVE, REJECT], CHECKER: [APPROVE, REJECT, WITHDRAW]}


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
    return {**cognito_row("contracting"), "email": email, "name": email.split("@")[0], "cis_role": None}


def _guard_calls(conn: ScriptedConn) -> list[tuple[str, tuple[Any, ...]]]:
    return [(sql, args) for sql, args in conn.calls if any(f in sql for f in GUARD_SQL)]


def _staff_calls(conn: ScriptedConn) -> list[str]:
    return [sql for sql, _ in conn.calls if IDENTITY_SELECT in sql]


def _detail(r: Any) -> Any:
    return r.json().get("detail") if r.headers.get("content-type", "").startswith("application/json") else None


# ── the module ──────────────────────────────────────────────────────────────────────────────────


def test_the_pair_shares_only_the_decisions_and_holds_no_grant() -> None:
    maker, checker = e2e_staff.E2E_WRITERS[MAKER], e2e_staff.E2E_WRITERS[CHECKER]
    assert maker.writes & checker.writes == {
        e2e_staff.PRICE_VERSION_APPROVE,
        e2e_staff.PRICE_VERSION_REJECT,
        e2e_staff.PRICE_VERSION_WITHDRAW,
        e2e_staff.CONTRACT_STATUS,  # A10e-3: the service refuses the draft's maker (403 maker_checker)
    }
    assert not maker.grants and not checker.grants
    # Every version route is held to the E2E maker's own versions (the address is bound as $2).
    for route, (param, sql) in e2e_staff.SYNTHETIC_SQL.items():
        if sql is e2e_staff.VERSION_SYNTHETIC_SQL:
            assert param == "version_id"
            assert e2e_staff.SYNTHETIC_SQL_ARGS[route] == (MAKER,)
    assert e2e_staff.E2E_PRICING_MAKER_EMAIL == MAKER and e2e_staff.E2E_PRICING_CHECKER_EMAIL == CHECKER
    assert e2e_staff.CHECKED_IN_ROUTE == {e2e_staff.PRICE_IMPORT[1]}
    assert dict(e2e_staff.BODY_SYNTHETIC) == {e2e_staff.PRICE_WORKBENCH_ACTION[1]: "tour_keys"}


@pytest.mark.parametrize(
    ("body", "ok"),
    [
        ({"action": "make_sellable", "tour_keys": [TOUR]}, True),
        ({"action": "make_not_sellable", "tour_keys": [TOUR, TOUR_2]}, True),
        ({"action": "recompute", "tour_keys": [TOUR]}, False),
        ({"action": "apply_provisional", "tour_keys": [TOUR]}, False),
        ({"action": "clear_provisional", "tour_keys": [TOUR]}, False),
        ({"action": "make_sellable", "tour_keys": []}, False),
        ({"action": "make_sellable", "tour_keys": TOUR}, False),
        ({"action": "make_sellable", "tour_keys": [7]}, False),
        ({"action": "make_sellable", "tour_keys": [TOUR], "note": "x"}, False),
        ({"action": "make_sellable"}, False),
        (None, False),
        ([TOUR], False),
    ],
)
def test_the_workbench_body_check(body: Any, ok: bool) -> None:
    writer = e2e_staff.E2E_WRITERS[MAKER]
    if ok:
        e2e_staff.check_body(writer, "POST", e2e_staff.PRICE_WORKBENCH_ACTION[1], body)
    else:
        with pytest.raises(HTTPException) as err:
            e2e_staff.check_body(writer, "POST", e2e_staff.PRICE_WORKBENCH_ACTION[1], body)
        assert err.value.detail == "e2e_read_only"


@pytest.mark.asyncio
@pytest.mark.parametrize("keys", [[], ["not-a-uuid"], [TOUR, "x"], [TOUR, None]])
async def test_plans_synthetic_fails_closed_without_a_query(keys: list[Any]) -> None:
    conn = ScriptedConn().on(PLANS_MANY, True)
    assert await e2e_staff.plans_synthetic(conn, keys) is False
    assert conn.calls == []


@pytest.mark.asyncio
async def test_plans_synthetic_counts_distinct_normalised_keys() -> None:
    conn = ScriptedConn().on(PLANS_MANY, True)
    assert await e2e_staff.plans_synthetic(conn, [TOUR_2, TOUR.upper(), TOUR]) is True
    assert conn.sql(PLANS_MANY) == [(sorted([TOUR, TOUR_2]), 2)]


class _Boom:
    async def fetchval(self, *_: Any) -> Any:
        raise asyncpg.UndefinedColumnError("column t.is_synthetic does not exist")


@pytest.mark.asyncio
async def test_a_database_error_counts_as_real() -> None:
    assert await e2e_staff.plans_synthetic(_Boom(), [TOUR]) is False
    assert await e2e_staff.is_synthetic_record(_Boom(), e2e_staff.PLAN_COMPONENTS[1], {"source_id": TOUR}) is False
    assert (
        await e2e_staff.is_synthetic_record(_Boom(), e2e_staff.PRICE_VERSION_APPROVE[1], {"version_id": VERSION})
        is False
    )


@dataclass
class _Sheets:
    """Stands in for ``app.catalog.price_import.Sheets``: the tour keys of every sheet and the Departures rows."""

    tour_keys: list[str]
    departures: list[dict[str, Any]] = field(default_factory=list)

    def keys(self) -> list[str]:
        return list(self.tour_keys)


@pytest.mark.asyncio
async def test_check_import_refuses_a_departures_row_before_any_query() -> None:
    conn = ScriptedConn().on(PLANS_MANY, True)
    with pytest.raises(HTTPException) as err:
        await e2e_staff.check_import(conn, "pricing_maker", _Sheets([TOUR], [{"tour_key": TOUR}]))
    assert (err.value.status_code, err.value.detail) == (403, "e2e_read_only")
    assert conn.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [False, None])
async def test_check_import_refuses_a_real_plan(answer: Any) -> None:
    conn = ScriptedConn().on(PLANS_MANY, answer)
    with pytest.raises(HTTPException) as err:
        await e2e_staff.check_import(conn, "pricing_maker", _Sheets([TOUR, TOUR_2]))
    assert (err.value.status_code, err.value.detail) == (403, "e2e_synthetic_only")
    assert conn.sql(PLANS_MANY) == [(sorted([TOUR, TOUR_2]), 2)]


@pytest.mark.asyncio
async def test_check_import_refuses_a_malformed_key_without_a_query() -> None:
    conn = ScriptedConn().on(PLANS_MANY, True)
    with pytest.raises(HTTPException) as err:
        await e2e_staff.check_import(conn, "pricing_maker", _Sheets([TOUR, "Hollow Ridge"]))
    assert err.value.detail == "e2e_synthetic_only" and conn.calls == []


@pytest.mark.asyncio
async def test_check_import_passes_synthetic_plans() -> None:
    conn = ScriptedConn().on(PLANS_MANY, True)
    await e2e_staff.check_import(conn, "pricing_maker", _Sheets([TOUR]))
    await e2e_staff.check_import(conn, "pricing_maker", _Sheets([]))  # no tour in the file: nothing to write, no query
    assert conn.sql(PLANS_MANY) == [([TOUR], 1)]


# ── the real app ────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("email", [MAKER, CHECKER])
@pytest.mark.parametrize("env", [None, "prod", "Dev"])
def test_outside_dev_every_request_is_refused(monkeypatch: pytest.MonkeyPatch, email: str, env: str | None) -> None:
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
        (MAKER, ["cis-customer_success"]),  # A10d without the preset: Booking refuses it as a pricing writer
        (MAKER, ["cis-customer_success", "booking-admin"]),
        (MAKER, ["cis-customer_success", "booking-contracting", "booking-sales"]),
        (MAKER, ["booking-contracting"]),
        (CHECKER, ["booking-contracting", "cis-customer_success"]),
        (CHECKER, ["booking-admin"]),
        (CHECKER, []),
    ],
)
def test_any_other_group_set_is_refused(email: str, groups: list[str]) -> None:
    cis, conn = LocalCis(), ScriptedConn()
    with _client(cis, conn) as c:
        r = c.get("/booking/staff/me", headers=auth(_token(cis, email, groups)))
    assert (r.status_code, r.json()["detail"]) == (403, "e2e_scope")
    assert conn.calls == []


@pytest.mark.parametrize("email", [MAKER, CHECKER])
def test_reads_keep_the_contracting_permissions(email: str) -> None:
    cis = LocalCis()
    conn = ScriptedConn().on(IDENTITY_SELECT, _row(email))
    with _client(cis, conn) as c:
        r = c.get("/booking/staff/me", headers=auth(_token(cis, email)))
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "contracting"
    assert {"price_version.write", "price_version.approve", "trip_plan.write"} <= set(r.json()["permissions"])
    assert _guard_calls(conn) == []


OTHER_WRITES = [
    ("POST", f"/booking/catalog/trip-plans/{TOUR}/price-versions", {}),
    ("PUT", f"/booking/catalog/trip-plans/{TOUR}/commercial", {}),
    ("PUT", f"/booking/catalog/trip-plans/{TOUR}/prices", {"items": []}),
    ("POST", f"/booking/catalog/trip-plans/{TOUR}/prices/recompute", None),
    ("POST", f"/booking/catalog/trip-plans/{TOUR}/departures", {"start_date": "2027-03-01", "capacity": 4}),
    ("PUT", f"/booking/catalog/trip-plans/{TOUR}/season-calendar", {"season_calendar_id": None, "reason": "x"}),
    ("POST", "/booking/pricing/season-calendars", {}),
    ("PATCH", f"/booking/catalog/departures/{TOUR}", {"status": "cancelled"}),
    ("POST", f"/booking/agency-payments/{TOUR}/approve", None),
    ("POST", "/booking/trip-cases", {"title": "x"}),
    ("PUT", "/booking/staff/me/preferences", {"notify": {}}),
]


@pytest.mark.parametrize("email", [MAKER, CHECKER])
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
    ("method", "path", "body"),
    [COMPONENTS, WORKBENCH, SAVE, SUBMIT, ("POST", "/booking/catalog/price-import", {"data_base64": "UEsDBA=="})],
)
def test_the_checker_never_makes_the_makers_writes(method: str, path: str, body: Any) -> None:
    cis, conn = LocalCis(), ScriptedConn()
    with _client(cis, conn) as c:
        r = c.request(method, path, json=body, headers=auth(_token(cis, CHECKER)))
    assert (r.status_code, r.json()["detail"]) == (403, "e2e_read_only")
    assert conn.calls == []


@pytest.mark.parametrize(
    "body",
    [
        {"action": "recompute", "tour_keys": [TOUR]},
        {"action": "apply_provisional", "tour_keys": [TOUR]},
        {"action": "make_sellable", "tour_keys": [TOUR], "extra": 1},
    ],
)
def test_a_workbench_action_other_than_the_sellable_switch_is_refused_before_any_db_access(body: Any) -> None:
    cis, conn = LocalCis(), ScriptedConn()
    with _client(cis, conn) as c:
        r = c.post("/booking/catalog/price-workbench/actions", json=body, headers=auth(_token(cis, MAKER)))
    assert (r.status_code, r.json()["detail"]) == (403, "e2e_read_only")
    assert conn.calls == []


@pytest.mark.parametrize("synthetic", [False, None])
def test_maker_plan_writes_on_a_real_plan_are_refused_before_the_staff_sync(synthetic: Any) -> None:
    cis = LocalCis()
    for method, path, body in (COMPONENTS, WORKBENCH):
        conn = ScriptedConn().on(PLAN_ONE, synthetic).on(PLANS_MANY, synthetic).on(IDENTITY_SELECT, _row(MAKER))
        with _client(cis, conn) as c:
            r = c.request(method, path, json=body, headers=auth(_token(cis, MAKER)))
        assert (r.status_code, r.json()["detail"]) == (403, "e2e_synthetic_only"), path
        assert [args for _, args in _guard_calls(conn)] == [(TOUR,) if path.endswith("/components") else ([TOUR], 1)]
        assert _staff_calls(conn) == [] and len(conn.calls) == 1


@pytest.mark.parametrize("email", [MAKER, CHECKER])
@pytest.mark.parametrize("synthetic", [False, None])
def test_version_writes_on_another_persons_or_a_real_plans_version_are_refused(email: str, synthetic: Any) -> None:
    cis = LocalCis()
    for method, path, body in VERSION_WRITES[email]:
        conn = ScriptedConn().on(VERSION_SQL, synthetic).on(IDENTITY_SELECT, _row(email))
        with _client(cis, conn) as c:
            r = c.request(method, path, json=body, headers=auth(_token(cis, email)))
        assert (r.status_code, r.json()["detail"]) == (403, "e2e_synthetic_only"), path
        # The version must be the E2E maker's, whoever asks (the checker decides only the maker's versions).
        assert [args for _, args in _guard_calls(conn)] == [(VERSION, MAKER)]
        assert _staff_calls(conn) == [] and len(conn.calls) == 1


@pytest.mark.parametrize("email", [MAKER, CHECKER])
def test_allowed_writes_on_synthetic_records_reach_the_route(email: str) -> None:
    cis = LocalCis()
    writes = VERSION_WRITES[email] + ([COMPONENTS, WORKBENCH] if email == MAKER else [])
    for method, path, body in writes:
        conn = (
            ScriptedConn()
            .on(VERSION_SQL, True)
            .on(PLAN_ONE, True)
            .on(PLANS_MANY, True)
            .on(IDENTITY_SELECT, _row(email))
        )
        with _client(cis, conn) as c:
            r = c.request(method, path, json=body, headers=auth(_token(cis, email)))
        detail = _detail(r)
        assert r.status_code != 422, r.text
        assert not isinstance(detail, str) or not detail.startswith("e2e_"), path
        flat = [sql for sql, _ in conn.calls]
        first_staff = flat.index(_staff_calls(conn)[0])
        assert flat.index(_guard_calls(conn)[0][0]) < first_staff
        assert len(flat) > first_staff + 1, path  # the route ran past the staff dependency


# ── the workbook import: checked inside the route ─────────────────────────────────────────────────


def _fake_import(seen: dict[str, Any]) -> Any:
    async def fake(conn: Any, staff: Any, data: bytes, *, dry_run: bool) -> dict[str, Any]:
        seen["applied"] = staff.e2e_writer
        return {"dry_run": dry_run, "tours": [], "totals": {}}

    return fake


@pytest.mark.parametrize(
    ("synthetic", "sheets", "expected"),
    [
        (True, _Sheets([TOUR]), 200),
        (False, _Sheets([TOUR]), "e2e_synthetic_only"),
        (True, _Sheets([TOUR], [{"tour_key": TOUR}]), "e2e_read_only"),
    ],
)
def test_the_import_route_checks_every_tour_for_the_e2e_maker(
    monkeypatch: pytest.MonkeyPatch, synthetic: bool, sheets: _Sheets, expected: Any
) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(price_import, "parse", lambda data: sheets)
    monkeypatch.setattr(price_import, "import_workbook", _fake_import(seen))
    cis = LocalCis()
    conn = ScriptedConn().on(PLANS_MANY, synthetic).on(IDENTITY_SELECT, _row(MAKER))
    with _client(cis, conn) as c:
        r = c.post(
            "/booking/catalog/price-import?dry_run=true",
            json={"data_base64": "UEsDBA=="},
            headers=auth(_token(cis, MAKER)),
        )
    if expected == 200:
        assert r.status_code == 200, r.text
        assert seen["applied"] == "pricing_maker"
    else:
        assert (r.status_code, r.json()["detail"]) == (403, expected)
        assert "applied" not in seen  # refused before the preview or any write


def test_a_real_contracting_member_is_unaffected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Control: a company contracting user imports without any E2E check (the file is parsed once, by the import)."""
    seen: dict[str, Any] = {}

    def no_parse(data: bytes) -> Any:
        raise AssertionError("the route parsed the file for a non-E2E session")

    monkeypatch.setattr(price_import, "parse", no_parse)
    monkeypatch.setattr(price_import, "import_workbook", _fake_import(seen))
    cis = LocalCis()
    conn = ScriptedConn().on(IDENTITY_SELECT, {**cognito_row("contracting"), "cis_role": None})
    token = cis.workforce.token(["booking-contracting"], client_id=BOOKING_STAFF_CLIENT)
    with _client(cis, conn) as c:
        r = c.post("/booking/catalog/price-import", json={"data_base64": "UEsDBA=="}, headers=auth(token))
        method, path, _ = WORKBENCH
        c.request(method, path, json={"action": "recompute", "tour_keys": [TOUR]}, headers=auth(token))
    assert r.status_code == 200, r.text
    assert seen["applied"] is None
    assert _guard_calls(conn) == []
