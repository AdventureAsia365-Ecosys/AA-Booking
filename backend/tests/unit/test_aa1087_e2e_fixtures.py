"""AA-1087 / AA-1070 crm-13 (S278 lane l18): the CLI shell of the Dev-only fixture seeder (:mod:`app.e2e_fixtures`).

The plan / apply logic runs on PostgreSQL in tests/integration/test_aa1087_e2e_fixtures.py; here: it refuses outside
Dev and without a DSN before connecting, validates the run key, prints a DRY RUN plan without the private template
fields, and turns a refusal into exit 3.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

import asyncpg
import pytest

from app import e2e_fixtures
from tests.unit.fakes import ScriptedConn


class _Pool:
    def __init__(self) -> None:
        self.closed = False

    def acquire(self) -> _Pool:
        return self

    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize("env", [None, "prod", "test", "Dev"])
def test_refused_outside_dev_before_connecting(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], env: str | None
) -> None:
    if env is None:
        monkeypatch.delenv("ENVIRONMENT", raising=False)
    else:
        monkeypatch.setenv("ENVIRONMENT", env)

    async def boom(*_: Any, **__: Any) -> None:
        raise AssertionError("must not connect")

    monkeypatch.setattr(e2e_fixtures.asyncpg, "create_pool", boom)
    assert e2e_fixtures.main(["--apply"]) == 2
    assert "Dev-only" in json.loads(capsys.readouterr().out)["error"]


def test_refused_without_a_dsn(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert e2e_fixtures.main([]) == 2
    assert json.loads(capsys.readouterr().out) == {"error": "DATABASE_URL not set"}


@pytest.mark.parametrize("run", ["ab", "Bad-Run", "has space", "x" * 40, "-lead"])
def test_a_bad_run_key_is_refused(run: str) -> None:
    with pytest.raises(SystemExit):
        e2e_fixtures.main(["--run", run])


def test_keys_and_default_run() -> None:
    at = datetime(2026, 11, 12, 7, 5, tzinfo=UTC)
    assert e2e_fixtures.default_run(at) == at.strftime("%Y%m%d%H%M")
    assert e2e_fixtures.RUN_RE.match(e2e_fixtures.default_run(at))
    assert e2e_fixtures.keys("s278a") == {
        "agency_ref": "E2E-s278a",
        "desk_idem": "e2e-fixture-s278a",
        "supplier_note": "E2E fixture (synthetic) run s278a",
        "invoice_idem": "e2e-fixture:s278a:invoice",
    }


def _wire(monkeypatch: pytest.MonkeyPatch, pool: _Pool) -> None:
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv("DATABASE_URL", "postgresql://booking@db.invalid/booking")

    async def create_pool(*_: Any, **__: Any) -> _Pool:
        return pool

    monkeypatch.setattr(e2e_fixtures.asyncpg, "create_pool", create_pool)


def test_dry_run_prints_the_plan_and_never_applies(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pool = _Pool()
    _wire(monkeypatch, pool)
    seen: dict[str, Any] = {}

    async def plan(_: Any, *, run: str, agency_tenant: str | None, today: date) -> dict[str, Any]:
        seen.update(run=run, agency_tenant=agency_tenant)
        return {"run": run, "departure": {"state": "create"}, "_template": {"secret": 1}, "_supplier_id": "x"}

    async def apply(*_: Any, **__: Any) -> None:
        raise AssertionError("DRY RUN applied")

    monkeypatch.setattr(e2e_fixtures, "plan", plan)
    monkeypatch.setattr(e2e_fixtures, "apply", apply)
    assert e2e_fixtures.main(["--run", "s278a", "--agency-tenant", "t-1"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"mode": "DRY RUN", "plan": {"run": "s278a", "departure": {"state": "create"}}}
    assert seen == {"run": "s278a", "agency_tenant": "t-1"}
    assert pool.closed


def test_the_crm_e2e_agency_is_the_default_target() -> None:
    # The CRM seed's sen-vang agency (AA-Partner seed, AA-CIS-App services/tenant_account/e2e_fixtures.py, same UUIDv5).
    assert e2e_fixtures.CRM_E2E_AGENCY_TENANT == "bab16039-a064-517f-a99f-172b729ad3c4"


@pytest.mark.parametrize(
    ("argv", "target"),
    [
        ([], "bab16039-a064-517f-a99f-172b729ad3c4"),
        (["--agency-tenant", "t-2"], "t-2"),
        (["--any-synthetic"], None),
    ],
)
def test_the_agency_target_options(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], argv: list[str], target: str | None
) -> None:
    _wire(monkeypatch, _Pool())
    seen: dict[str, Any] = {}

    async def plan(_: Any, *, run: str, agency_tenant: str | None, today: date) -> dict[str, Any]:
        seen["agency_tenant"] = agency_tenant
        return {"run": run}

    monkeypatch.setattr(e2e_fixtures, "plan", plan)
    assert e2e_fixtures.main(["--run", "s278t", *argv]) == 0
    capsys.readouterr()
    assert seen == {"agency_tenant": target}


def test_agency_tenant_and_any_synthetic_exclude_each_other() -> None:
    with pytest.raises(SystemExit):
        e2e_fixtures.main(["--any-synthetic", "--agency-tenant", "t-2"])


def test_apply_prints_the_fixtures(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    pool = _Pool()
    _wire(monkeypatch, pool)

    async def plan(*_: Any, **__: Any) -> dict[str, Any]:
        return {"run": "s278b"}

    async def apply(_: Any, planned: dict[str, Any], *, today: date) -> dict[str, Any]:
        return {"run": planned["run"], "departure_id": "d-1"}

    monkeypatch.setattr(e2e_fixtures, "plan", plan)
    monkeypatch.setattr(e2e_fixtures, "apply", apply)
    assert e2e_fixtures.main(["--apply", "--run", "s278b"]) == 0
    assert json.loads(capsys.readouterr().out) == {"mode": "APPLY", "fixtures": {"run": "s278b", "departure_id": "d-1"}}


def test_a_refusal_is_exit_3(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    pool = _Pool()
    _wire(monkeypatch, pool)

    async def plan(*_: Any, **__: Any) -> dict[str, Any]:
        raise e2e_fixtures.Refused("tenant x is not synthetic in CIS")

    monkeypatch.setattr(e2e_fixtures, "plan", plan)
    assert e2e_fixtures.main(["--apply"]) == 3
    assert json.loads(capsys.readouterr().out) == {"error": "refused: tenant x is not synthetic in CIS"}
    assert pool.closed


# ── plan / apply against a scripted connection (the services are stubbed; PostgreSQL: the integration test) ──────

STAFF_ROW = {
    "cis_user_id": "cog:e2e-staff",
    "email": "e2e-booking-staff@e2e-mail.lumiguides.it.com",
    "name": "E2E staff",
    "role": "sales",
    "active": True,
    "cis_role": None,
    "created_at": None,
    "updated_at": None,
    "last_seen_at": None,
}
TEMPLATE = {
    "partner_id": "p-1",
    "tenant_id": "t-1",
    "agency_name": "Copperleaf Journeys",
    "billing": "USD",
    "currency": "USD",
    "component_id": "tp:plan-1",
    "plan_id": "plan-1",
    "source_id": "src-1",
    "plan_supplier_id": None,
}
TODAY = date(2027, 1, 10)


def _conn(**over: Any) -> ScriptedConn:
    conn = ScriptedConn()
    conn.on("FROM booking.staff_users WHERE lower(email)", over.get("staff", STAFF_ROW))
    conn.on("ORDER BY h.confirmed_at DESC", over.get("template", TEMPLATE))
    conn.on("FROM booking.b2b_desk_bookings d", over.get("desk"))
    conn.on("NOT EXISTS (SELECT 1 FROM booking.b2b_hold_lines l WHERE l.departure_id = d.id)", over.get("leftover"))
    conn.on("SELECT start_date FROM booking.departures", over.get("taken", []))
    conn.on("FROM booking.suppliers WHERE status = 'active'", over.get("supplier", {"id": "s-1", "code": "SUP-1"}))
    conn.on("FROM booking.supplier_bookings WHERE trip_case_id", over.get("sb"))
    conn.on("FROM booking.agency_invoice WHERE idempotency_key", over.get("invoice"))
    conn.on("AND period_start = $2", over.get("period_taken", False))
    conn.on("INSERT INTO booking.agency_invoice", "inv-1")
    return conn


@pytest.mark.asyncio
async def test_plan_of_a_new_run() -> None:
    planned = await e2e_fixtures.plan(_conn(taken=[{"start_date": TODAY + timedelta(days=60)}]), run="s278a",
                                      agency_tenant=None, today=TODAY)  # fmt: skip
    assert planned["departure"] == {"start_date": (TODAY + timedelta(days=61)).isoformat(), "state": "create"}
    assert planned["desk_booking"] == {"state": "create", "agency_ref": "E2E-s278a"}
    assert planned["supplier_booking"] == {"state": "create", "supplier_code": "SUP-1"}
    assert planned["invoice"] == {"state": "create", "currency": "USD"}
    assert planned["agency"]["tenant_id"] == "t-1" and planned["actor"] == "cog:e2e-staff"


@pytest.mark.asyncio
async def test_plan_reuses_a_leftover_departure_and_an_existing_run() -> None:
    leftover = {"id": "d-0", "start_date": TODAY + timedelta(days=70)}
    planned = await e2e_fixtures.plan(_conn(leftover=leftover, invoice={"id": "i", "status": "draft", "number": None}),
                                      run="s278a", agency_tenant=None, today=TODAY)  # fmt: skip
    assert planned["departure"]["state"] == "reuse" and planned["departure"]["departure_id"] == "d-0"
    assert planned["invoice"]["state"] == "issue"
    desk = {"desk_id": "k", "status": "booked", "trip_case_id": "c-1", "trip_case_code": "TC-1", "departure_id": "d-1"}
    again = await e2e_fixtures.plan(_conn(desk=desk, sb={"id": "sb", "code": "SB-1", "status": "pending"}),
                                    run="s278a", agency_tenant=None, today=TODAY)  # fmt: skip
    assert again["departure"] == {"departure_id": "d-1", "state": "exists"}
    assert again["supplier_booking"]["state"] == "exists" and again["desk_booking"]["state"] == "exists"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("over", "agency", "match"),
    [
        ({"staff": None}, None, "sign in once"),
        ({"staff": {**STAFF_ROW, "active": False}}, None, "sign in once"),
        ({"template": None}, None, "no confirmed booking"),
        ({"supplier": None}, None, "no active supplier"),
        ({"taken": [{"start_date": TODAY + timedelta(days=60 + n)} for n in range(120)]}, None, "no free departure"),
    ],
)
async def test_plan_refusals(over: dict[str, Any], agency: str | None, match: str) -> None:
    with pytest.raises(e2e_fixtures.Refused, match=match):
        await e2e_fixtures.plan(_conn(**over), run="s278a", agency_tenant=agency, today=TODAY)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agency_row", "match"),
    [
        (None, "no B2B partner"),
        ({"partner_id": "p", "status": "active", "synthetic": False}, "not synthetic"),
        ({"partner_id": "p", "status": "suspended", "synthetic": True}, "suspended"),
    ],
)
async def test_an_agency_tenant_must_be_an_active_synthetic_partner(agency_row: Any, match: str) -> None:
    conn = _conn().on("FROM booking.b2b_partners p LEFT JOIN", agency_row)
    with pytest.raises(e2e_fixtures.Refused, match=match):
        await e2e_fixtures.plan(conn, run="s278a", agency_tenant="t-9", today=TODAY)


@pytest.mark.asyncio
async def test_a_missing_view_column_is_refused() -> None:
    conn = _conn().on("ORDER BY h.confirmed_at DESC", asyncpg.UndefinedColumnError("is_synthetic"))
    with pytest.raises(e2e_fixtures.Refused, match="CIS 401"):
        await e2e_fixtures.plan(conn, run="s278a", agency_tenant=None, today=TODAY)


@pytest.mark.asyncio
async def test_apply_runs_the_services_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    desk_rows: list[Any] = [None]
    booked = {
        "desk_id": "k",
        "status": "booked",
        "trip_case_id": "c-1",
        "trip_case_code": "TC-1",
        "departure_id": "d-1",
        "departure_start": TODAY + timedelta(days=60),
    }
    conn = _conn(period_taken=False)
    conn.on("FROM booking.b2b_desk_bookings d", lambda *_: desk_rows[-1])
    seen: list[str] = []

    async def create_departure(_c: Any, staff: Any, source_id: str, data: dict[str, Any]) -> dict[str, Any]:
        seen.append(f"departure {source_id} {data['start_date']} by {staff.cis_user_id}")
        return {"id": "d-1", "start_date": data["start_date"]}

    async def enter(_p: Any, _s: Any, **kw: Any) -> dict[str, Any]:
        seen.append(f"desk {kw['agency_ref']} {kw['lines'][0].departure_id}")
        desk_rows.append(booked)
        return {}

    async def create_sb(_c: Any, _s: Any, case_id: str, data: dict[str, Any]) -> dict[str, Any]:
        seen.append(f"supplier booking on {case_id} by {data['supplier_id']}")
        return {"id": "sb-1", "code": "SB-1", "status": "pending"}

    async def get(_c: Any, _ref: str) -> dict[str, Any]:
        return {"lines": []}

    async def add_line(_c: Any, ref: str, body: dict[str, Any], _a: str) -> dict[str, Any]:
        seen.append(f"line on {ref} {body['unit_price']}")
        return {}

    async def issue(_c: Any, ref: str, _a: str) -> dict[str, Any]:
        seen.append(f"issue {ref}")
        return {"id": ref, "status": "issued", "number": "AAT-2027-000001"}

    monkeypatch.setattr(e2e_fixtures.departures, "create_departure", create_departure)
    monkeypatch.setattr(e2e_fixtures.desk_bookings, "enter", enter)
    monkeypatch.setattr(e2e_fixtures.supplier_bookings, "create", create_sb)
    monkeypatch.setattr(e2e_fixtures.invoices, "get", get)
    monkeypatch.setattr(e2e_fixtures.invoices, "add_line", add_line)
    monkeypatch.setattr(e2e_fixtures.invoices, "issue", issue)
    planned = await e2e_fixtures.plan(conn, run="s278a", agency_tenant=None, today=TODAY)
    made = await e2e_fixtures.apply(conn, planned, today=TODAY)  # type: ignore[arg-type]
    assert seen == [
        f"departure src-1 {TODAY + timedelta(days=60)} by cog:e2e-staff",
        "desk E2E-s278a d-1",
        "supplier booking on c-1 by s-1",
        "line on inv-1 25.00",
        "issue inv-1",
    ]
    assert made["departure_id"] == "d-1" and made["trip_case"] == {"id": "c-1", "code": "TC-1"}
    assert made["departure_start"] == (TODAY + timedelta(days=60)).isoformat()
    assert made["tour_component_id"].startswith("tp:")
    assert made["supplier_booking"]["status"] == "pending"
    assert made["invoice"] == {"id": "inv-1", "number": "AAT-2027-000001", "status": "issued"}


@pytest.mark.asyncio
async def test_apply_stops_when_the_booking_did_not_confirm(monkeypatch: pytest.MonkeyPatch) -> None:
    waiting = {"desk_id": "k", "status": "awaiting_approval", "trip_case_id": None, "trip_case_code": None,
               "departure_id": "d-1"}  # fmt: skip
    leftover = {"id": "d-0", "start_date": TODAY + timedelta(days=70)}
    conn = _conn(leftover=leftover)
    rows: list[Any] = [None]
    conn.on("FROM booking.b2b_desk_bookings d", lambda *_: rows[-1])

    async def enter(*_: Any, **__: Any) -> dict[str, Any]:
        rows.append(waiting)
        return {}

    monkeypatch.setattr(e2e_fixtures.desk_bookings, "enter", enter)
    planned = await e2e_fixtures.plan(conn, run="s278a", agency_tenant=None, today=TODAY)
    with pytest.raises(e2e_fixtures.Refused, match="did not confirm"):
        await e2e_fixtures.apply(conn, planned, today=TODAY)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_free_period_skips_taken_starts_and_gives_up() -> None:
    taken = {TODAY, TODAY - timedelta(days=1)}
    conn = ScriptedConn().on("AND period_start = $2", lambda _t, day, _c: day in taken)
    assert await e2e_fixtures._free_period(conn, "t-1", "USD", TODAY) == TODAY - timedelta(days=2)  # type: ignore[arg-type]
    full = ScriptedConn().on("AND period_start = $2", True)
    with pytest.raises(e2e_fixtures.Refused, match="period start"):
        await e2e_fixtures._free_period(full, "t-1", "USD", TODAY)  # type: ignore[arg-type]
