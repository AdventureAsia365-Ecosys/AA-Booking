"""AA-1087 / AA-1070 crm-13 (S278 lane l18): the CLI shell of the Dev-only fixture seeder (:mod:`app.e2e_fixtures`).

The plan / apply logic runs on PostgreSQL in tests/integration/test_aa1087_e2e_fixtures.py; here: it refuses outside
Dev and without a DSN before connecting, validates the run key, prints a DRY RUN plan without the private template
fields, and turns a refusal into exit 3.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any

import pytest

from app import e2e_fixtures


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
