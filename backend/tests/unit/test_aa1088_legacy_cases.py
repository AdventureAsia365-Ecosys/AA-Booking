"""AA-1088 M13 follow-up (S278 lane l18): the shell of :mod:`app.b2b.legacy_cases` (the plan / apply logic and the
bk-056 lock exception run on PostgreSQL in tests/integration/test_aa1088_legacy_cases.py).
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import pytest

from app.b2b import legacy_cases


def test_legacy_titles_are_the_pre_aa1013_shapes() -> None:
    assert legacy_cases.legacy_version_title("crm-proposal:7c1e", 2) == "B2B crm-proposal:7c1e v2"
    line = {"component_kind": "trip_plan", "component_id": "tp:5b2d", "line_ref": "9a0f"}
    assert legacy_cases.legacy_item_title(line) == "trip_plan tp:5b2d · line 9a0f"


def test_the_report_keeps_counts_and_codes_only() -> None:
    planned = {
        "b2b_cases": 3,
        "to_fix": 1,
        "changes": [
            {
                "case_id": "c-1",
                "code": "TC-000901",
                "version_id": "v-1",
                "end_date": (date(2027, 3, 1), date(2027, 3, 6)),
                "version_title": "Harbour Lights Loop",
                "items": [{"id": "i-1", "position": 1, "title": "Harbour Lights Loop"}],
            }
        ],
    }
    assert legacy_cases._report(planned) == {
        "b2b_cases": 3,
        "to_fix": 1,
        "cases": [
            {
                "code": "TC-000901",
                "end_date": ["2027-03-01", "2027-03-06"],
                "days_added": 5,
                "version_title": True,
                "items": 1,
            }
        ],
    }


class _NoKey:
    async def fetchval(self, *_: Any) -> bool:
        return False


@pytest.mark.asyncio
async def test_apply_refuses_before_bk056() -> None:
    with pytest.raises(RuntimeError, match="bk-056"):
        await legacy_cases.apply(_NoKey())  # type: ignore[arg-type]


def test_main_needs_a_dsn(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert legacy_cases.main([]) == 2
    assert json.loads(capsys.readouterr().out) == {"error": "DATABASE_URL not set"}


class _Conn:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize(("argv", "mode"), [([], "DRY RUN"), (["--apply"], "APPLY")])
def test_main_prints_the_report(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], argv: list[str], mode: str
) -> None:
    conn = _Conn()
    monkeypatch.setenv("DATABASE_URL", "postgresql://booking@db.invalid/booking")

    async def connect(*_: Any, **__: Any) -> _Conn:
        return conn

    async def result(_: Any) -> dict[str, Any]:
        return {"b2b_cases": 0, "to_fix": 0, "changes": []}

    monkeypatch.setattr(legacy_cases.asyncpg, "connect", connect)
    monkeypatch.setattr(legacy_cases, "plan", result)
    monkeypatch.setattr(legacy_cases, "apply", result)
    assert legacy_cases.main(argv) == 0
    assert json.loads(capsys.readouterr().out) == {"mode": mode, "b2b_cases": 0, "to_fix": 0, "cases": []}
    assert conn.closed


def test_main_turns_a_missing_key_into_exit_3(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://booking@db.invalid/booking")

    async def connect(*_: Any, **__: Any) -> _Conn:
        return _Conn()

    async def refuse(_: Any) -> dict[str, Any]:
        raise RuntimeError("bk-056 is not applied: the version locks refuse the fix")

    monkeypatch.setattr(legacy_cases.asyncpg, "connect", connect)
    monkeypatch.setattr(legacy_cases, "apply", refuse)
    assert legacy_cases.main(["--apply"]) == 3
    assert "bk-056" in json.loads(capsys.readouterr().out)["error"]
