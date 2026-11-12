"""AA-1088 M13 follow-up (S278 lane l18): the shell of :mod:`app.b2b.legacy_cases` (the plan / apply logic and the
bk-052 lock exception run on PostgreSQL in tests/integration/test_aa1088_legacy_cases.py).
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import pytest

from app.b2b import legacy_cases
from tests.unit.fakes import ScriptedConn


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


HASH = "0" * 64


@pytest.mark.asyncio
async def test_apply_refuses_before_bk052() -> None:
    with pytest.raises(RuntimeError, match="bk-052"):
        await legacy_cases.apply(_NoKey(), HASH)  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["", "abc", "Z" * 64, "0" * 63])
async def test_apply_needs_a_plan_hash(bad: str) -> None:
    with pytest.raises(legacy_cases.PlanChanged, match="plan_hash"):
        await legacy_cases.apply(_NoKey(), bad)  # type: ignore[arg-type]


def test_the_plan_hash_is_canonical() -> None:
    a = [{"code": "TC-1", "end_date": (date(2027, 3, 1), date(2027, 3, 6)), "case_id": "c"}]
    b = [{"case_id": "c", "end_date": (date(2027, 3, 1), date(2027, 3, 6)), "code": "TC-1"}]
    assert legacy_cases.plan_hash(a) == legacy_cases.plan_hash(b)
    assert legacy_cases.HASH_RE.match(legacy_cases.plan_hash(a))
    assert legacy_cases.plan_hash(a) != legacy_cases.plan_hash([{**a[0], "end_date": (date(2027, 3, 1), START_LATER)}])
    assert legacy_cases.plan_hash([]) != legacy_cases.plan_hash(a)


START_LATER = date(2027, 3, 7)


def test_main_apply_needs_the_expected_plan(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://booking@db.invalid/booking")
    assert legacy_cases.main(["--apply"]) == 2
    assert "--expect-plan" in json.loads(capsys.readouterr().out)["error"]


def test_main_needs_a_dsn(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert legacy_cases.main([]) == 2
    assert json.loads(capsys.readouterr().out) == {"error": "DATABASE_URL not set"}


class _Conn:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize(("argv", "mode"), [([], "DRY RUN"), (["--apply", "--expect-plan", HASH], "APPLY")])
def test_main_prints_the_report(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], argv: list[str], mode: str
) -> None:
    conn = _Conn()
    monkeypatch.setenv("DATABASE_URL", "postgresql://booking@db.invalid/booking")

    async def connect(*_: Any, **__: Any) -> _Conn:
        return conn

    async def result(_: Any, *__: Any) -> dict[str, Any]:
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

    async def refuse(_: Any, *__: Any) -> dict[str, Any]:
        raise RuntimeError("bk-052 is not applied: the version locks refuse the fix")

    monkeypatch.setattr(legacy_cases.asyncpg, "connect", connect)
    monkeypatch.setattr(legacy_cases, "apply", refuse)
    assert legacy_cases.main(["--apply", "--expect-plan", HASH]) == 3
    assert "bk-052" in json.loads(capsys.readouterr().out)["error"]


# ── plan / apply against a scripted connection (PostgreSQL and the bk-052 locks: the integration test) ─────────────

START = date(2027, 3, 1)
CASE = {
    "case_id": "c-1",
    "code": "TC-000901",
    "start_date": START,
    "end_date": START,
    "quote_id": "q-1",
    "proposal_ref": "crm-proposal:7c1e",
    "proposal_version": 1,
    "version_id": "v-1",
    "version_title": "B2B crm-proposal:7c1e v1",
    "sent": False,
}
LINE = {
    "line_no": 1,
    "line_ref": "9a0f",
    "component_id": "tp:5b2d0000-0000-4000-8000-000000000001",
    "component_kind": "trip_plan",
    "service_date": START,
    "nights": None,
}
ITEM = {"id": "i-1", "position": 1, "title": "trip_plan tp:5b2d0000-0000-4000-8000-000000000001 · line 9a0f",
        "day_from": 1, "day_to": 1}  # fmt: skip


def _scripted(case: dict[str, Any] = CASE, item: dict[str, Any] = ITEM, key: bool = True) -> ScriptedConn:
    conn = ScriptedConn()
    conn.on("FROM booking.trip_cases tc JOIN booking.b2b_holds h", [case])
    conn.on("FROM booking.b2b_quote_lines WHERE quote_id", [LINE])
    conn.on("FROM booking.trip_case_version_itinerary WHERE trip_case_version_id", [item])
    conn.on("FROM booking.v_schema_versions", key)
    return conn


@pytest.fixture
def catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    async def plan_days(_c: Any, ids: list[str]) -> tuple[dict[str, int], dict[str, str]]:
        return {i: 6 for i in ids}, {i: "src-1" for i in ids}

    async def line_titles(_c: Any, lines: list[Any], _s: dict[str, str]) -> dict[int, str]:
        return {r["line_no"]: "Harbour Lights Loop" for r in lines}

    monkeypatch.setattr(legacy_cases.plan_span, "plan_days", plan_days)
    monkeypatch.setattr(legacy_cases.holds, "line_titles", line_titles)


@pytest.mark.asyncio
async def test_plan_finds_the_three_fixes(catalog: None) -> None:
    planned = await legacy_cases.plan(_scripted())
    assert (planned["to_fix"], planned["end_dates"], planned["version_titles"]) == (1, 1, 1)
    change = planned["changes"][0]
    assert change["end_date"] == (START, date(2027, 3, 6))
    assert change["version_title"] == "Harbour Lights Loop"
    assert change["items"] == [{"id": "i-1", "position": 1, "title": "Harbour Lights Loop", "day_to": 6}]


@pytest.mark.asyncio
async def test_plan_leaves_fixed_extended_and_sent_cases(catalog: None) -> None:
    fixed = {**CASE, "end_date": date(2027, 3, 9), "version_title": "Harbour Lights Loop"}
    item = {**ITEM, "title": "Harbour Lights Loop", "day_to": 6}
    assert (await legacy_cases.plan(_scripted(fixed, item)))["to_fix"] == 0
    sent = await legacy_cases.plan(_scripted({**CASE, "sent": True}))
    assert (sent["to_fix"], sent["skipped_sent"]) == (0, 1)


@pytest.mark.asyncio
async def test_a_case_without_quote_lines_is_skipped(catalog: None) -> None:
    conn = _scripted().on("FROM booking.b2b_quote_lines WHERE quote_id", [])
    assert (await legacy_cases.plan(conn))["to_fix"] == 0


@pytest.mark.asyncio
async def test_apply_writes_each_fix_with_the_setting_and_an_audit_row(catalog: None) -> None:
    digest = (await legacy_cases.plan(_scripted()))["plan_hash"]
    conn = _scripted()
    await legacy_cases.apply(conn, digest)  # type: ignore[arg-type]
    assert conn.sql("SELECT set_config($1, $2, true)") == [("booking.legacy_display_fix", "aa1088")]
    assert conn.sql("UPDATE booking.trip_cases SET end_date") == [("c-1", date(2027, 3, 6), START)]
    assert conn.sql("UPDATE booking.trip_case_versions SET title") == [("v-1", "Harbour Lights Loop")]
    assert conn.sql("UPDATE booking.trip_case_version_itinerary") == [("i-1", "Harbour Lights Loop", 6)]
    assert len(conn.sql("INSERT INTO booking.audit_logs")) == 1


@pytest.mark.asyncio
async def test_apply_writes_nothing_when_the_plan_changed_since_the_dry_run(catalog: None) -> None:
    reviewed = (await legacy_cases.plan(_scripted()))["plan_hash"]
    # A second case appeared (or a tour length changed) after the DRY RUN: same command, different plan.
    conn = _scripted()
    conn.on(
        "FROM booking.trip_cases tc JOIN booking.b2b_holds h", [CASE, {**CASE, "case_id": "c-2", "code": "TC-000902"}]
    )
    with pytest.raises(legacy_cases.PlanChanged, match="plan changed"):
        await legacy_cases.apply(conn, reviewed)  # type: ignore[arg-type]
    assert conn.sql("SELECT set_config($1, $2, true)") == []
    assert conn.sql("INSERT INTO booking.audit_logs") == []
