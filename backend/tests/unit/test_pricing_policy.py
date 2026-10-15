"""AA-810 maker-checker (bk-007): the /booking/pricing/rules and /fx-rates routes and ``app.pricing.policy`` branches,
with a scripted connection. Real SQL, grants and the trigger: tests/integration/test_bk007_*.py."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal as D
from typing import Any

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.auth.staff import Staff, current_staff, get_pool
from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.main import create_app
from app.pricing import policy
from app.settings import Settings
from tests.unit.fakes import ScriptedConn

ADMIN = Staff("a-1", None, "Admin", "admin", True, "admin")
CONTRACTING = Staff("u-5", None, "Contracts", "contracting", True, "reviewer")
SALES = Staff("u-1", None, "Sales One", "sales", True, "reviewer")
CONTENT = Staff("u-9", None, "Writer", "content_creator", True, "content")
TODAY = date(2026, 10, 14)
ROW_ID = "11111111-1111-4111-8111-111111111111"

RULE_ROW = {
    "id": ROW_ID,
    "channel": "b2b",
    "target_margin": D("0.1600"),
    "floor_margin": D("0.1000"),
    "peak_uplift_pts": D("0"),
    "card_fee_pct": D("0.0300"),
    "valid_from": TODAY,
    "note": None,
    "created_at": datetime(2026, 10, 14, tzinfo=UTC),
    "created_by": "u-5",
    "created_by_name": "Contracts",
    "approved_by": None,
    "approved_by_name": None,
    "approved_at": None,
    "status": "pending",
}
FX_ROW = {
    "id": ROW_ID,
    "currency": "EUR",
    "rate_per_usd": D("0.92"),
    "buffer_pct": D("0.02"),
    "as_of": TODAY,
    "source": "manual-dev",
    "created_at": datetime(2026, 10, 14, tzinfo=UTC),
    "created_by": "u-5",
    "created_by_name": "Contracts",
    "approved_by": None,
    "approved_by_name": None,
    "approved_at": None,
    "status": "pending",
}
RULE_BODY = {"channel": "b2b", "target_margin": "0.16", "floor_margin": "0.10", "valid_from": "2026-10-20"}
FX_BODY = {"currency": "EUR", "rate_per_usd": "0.92", "buffer_pct": "0.02", "as_of": "2026-10-14", "source": "ECB"}


def _client(staff: Staff, conn: ScriptedConn | None = None) -> TestClient:
    app = create_app(Settings(database_url=None, environment="test"))
    app.dependency_overrides[current_staff] = lambda: staff
    app.dependency_overrides[get_pool] = lambda: conn or ScriptedConn()
    return TestClient(app)


def _approved(row: dict[str, Any], status: str = "in_force") -> dict[str, Any]:
    return {
        **row,
        "status": status,
        "approved_by": "a-1",
        "approved_by_name": "Admin",
        "approved_at": row["created_at"],
    }


# ── Routes: permissions and body validation ──────────────────────────────────


def test_reads_need_pricing_read_and_writes_pricing_write() -> None:
    conn = ScriptedConn().on("FROM booking.pricing_rules p", [RULE_ROW]).on("FROM booking.fx_rates p", [FX_ROW])
    r = _client(SALES, conn).get("/booking/pricing/rules?channel=b2b&limit=10")
    assert r.status_code == 200, r.text
    assert r.json()["items"][0]["can_approve"] is False and r.json()["items"][0]["created_by"]["id"] == "u-5"
    today, channel, row_id, limit = conn.sql("FROM booking.pricing_rules p")[0]
    assert (channel, row_id, limit) == ("b2b", None, 10) and isinstance(today, date)

    r = _client(ADMIN, conn).get("/booking/pricing/fx-rates?currency=EUR")
    assert r.status_code == 200 and r.json()["items"][0]["currency"] == "EUR"
    assert r.json()["items"][0]["can_approve"] is True  # admin did not create it
    assert _client(CONTRACTING, conn).get("/booking/pricing/fx-rates").json()["items"][0]["can_approve"] is False

    for path in ("/booking/pricing/rules", "/booking/pricing/fx-rates"):
        assert _client(CONTENT).get(path).status_code == 403
        assert _client(SALES).post(path, json=RULE_BODY if "rules" in path else FX_BODY).status_code == 403
        assert _client(SALES).post(f"{path}/{ROW_ID}/approve").status_code == 403
    assert _client(ADMIN).get("/booking/pricing/fx-rates?currency=eur").status_code == 422


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("rules", {**RULE_BODY, "channel": "retail"}),
        ("rules", {**RULE_BODY, "target_margin": "1"}),
        ("rules", {**RULE_BODY, "card_fee_pct": "0.00001"}),  # 4 decimals at most (NUMERIC(5,4))
        ("rules", {**RULE_BODY, "extra": 1}),
        ("fx-rates", {**FX_BODY, "rate_per_usd": "0"}),
        ("fx-rates", {**FX_BODY, "currency": "eu"}),
        ("fx-rates", {**FX_BODY, "source": ""}),
        ("fx-rates", {**FX_BODY, "buffer_pct": "-0.01"}),
    ],
)
def test_body_validation(path: str, body: dict[str, Any]) -> None:
    assert _client(ADMIN).post(f"/booking/pricing/{path}", json=body).status_code == 422


def test_create_and_approve_routes_map_to_the_service(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, Any]] = []

    def record(name: str):
        async def fake(conn: Any, staff: Staff, arg: Any, *, today: date) -> dict[str, Any]:
            calls.append((name, arg))
            return {"id": ROW_ID}

        return fake

    for name in ("create_rule", "create_fx_rate", "approve_rule", "approve_fx_rate"):
        monkeypatch.setattr(policy, name, record(name))
    c = _client(CONTRACTING)
    assert c.post("/booking/pricing/rules", json=RULE_BODY).status_code == 201
    assert c.post("/booking/pricing/fx-rates", json=FX_BODY).status_code == 201
    assert c.post(f"/booking/pricing/rules/{ROW_ID}/approve").status_code == 200
    assert c.post(f"/booking/pricing/fx-rates/{ROW_ID}/approve").status_code == 200
    assert c.post("/booking/pricing/rules/not-a-uuid/approve").status_code == 422
    assert [name for name, _ in calls] == ["create_rule", "create_fx_rate", "approve_rule", "approve_fx_rate"]
    rule = calls[0][1]
    assert rule["peak_uplift_pts"] == D("0") and rule["card_fee_pct"] == D("0") and rule["note"] is None
    assert calls[1][1]["rate_per_usd"] == D("0.92") and calls[2][1] == ROW_ID


# ── Service: create ──────────────────────────────────────────────────────────


def _rule_data(**overrides: Any) -> dict[str, Any]:
    data = {
        "channel": "b2b",
        "target_margin": D("0.16"),
        "floor_margin": D("0.10"),
        "peak_uplift_pts": D("0"),
        "card_fee_pct": D("0.03"),
        "valid_from": date(2026, 10, 20),
        "note": "  ",
    }
    data.update(overrides)
    return data


def _fx_data(**overrides: Any) -> dict[str, Any]:
    data = {"currency": "EUR", "rate_per_usd": D("0.92"), "buffer_pct": D("0.02"), "as_of": TODAY, "source": " ECB "}
    data.update(overrides)
    return data


@pytest.mark.asyncio
async def test_create_rule_writes_pending_row_and_audit() -> None:
    conn = ScriptedConn().on("INSERT INTO booking.pricing_rules", ROW_ID).on("FROM booking.pricing_rules p", RULE_ROW)
    row = await policy.create_rule(conn, CONTRACTING, _rule_data(), today=TODAY)
    assert row["status"] == "pending" and row["can_approve"] is False
    insert = conn.sql("INSERT INTO booking.pricing_rules")[0]
    assert insert[0] == "b2b" and insert[6] is None and insert[7] == "u-5"  # blank note -> NULL; maker recorded
    audit = conn.sql("INSERT INTO booking.audit_logs")[0]
    assert audit[3] == "pricing_rule.created" and "target 16%" in audit[9] and "pending approval" in audit[9]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data", "answer", "error", "code"),
    [
        (_rule_data(floor_margin=D("0.20")), ROW_ID, Invalid, "invalid_rule"),
        (_rule_data(), asyncpg.UniqueViolationError("dup"), Conflict, "rule_exists"),
        (_rule_data(), asyncpg.CheckViolationError("chk"), Invalid, "invalid_rule"),
    ],
)
async def test_create_rule_refusals(data: dict[str, Any], answer: Any, error: type, code: str) -> None:
    conn = ScriptedConn().on("INSERT INTO booking.pricing_rules", answer)
    with pytest.raises(error) as exc:
        await policy.create_rule(conn, CONTRACTING, data, today=TODAY)
    assert exc.value.code == code
    assert conn.sql("INSERT INTO booking.audit_logs") == []


@pytest.mark.asyncio
async def test_create_fx_rate_writes_pending_row_and_audit() -> None:
    conn = ScriptedConn().on("INSERT INTO booking.fx_rates", ROW_ID).on("FROM booking.fx_rates p", FX_ROW)
    row = await policy.create_fx_rate(conn, CONTRACTING, _fx_data(), today=TODAY)
    assert row["currency"] == "EUR" and row["status"] == "pending"
    assert conn.sql("INSERT INTO booking.fx_rates")[0][4] == "ECB"  # source trimmed
    assert conn.sql("INSERT INTO booking.audit_logs")[0][3] == "fx_rate.created"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data", "answer", "error", "code"),
    [
        (_fx_data(currency="USD"), ROW_ID, Invalid, "usd_is_base"),
        (_fx_data(source="   "), ROW_ID, Invalid, "source_required"),
        (_fx_data(), asyncpg.UniqueViolationError("dup"), Conflict, "fx_rate_exists"),
        (_fx_data(), asyncpg.CheckViolationError("chk"), Invalid, "invalid_fx_rate"),
    ],
)
async def test_create_fx_rate_refusals(data: dict[str, Any], answer: Any, error: type, code: str) -> None:
    conn = ScriptedConn().on("INSERT INTO booking.fx_rates", answer)
    with pytest.raises(error) as exc:
        await policy.create_fx_rate(conn, CONTRACTING, data, today=TODAY)
    assert exc.value.code == code


@pytest.mark.asyncio
async def test_created_row_vanishing_is_not_found() -> None:
    conn = ScriptedConn().on("INSERT INTO booking.fx_rates", ROW_ID).on("FROM booking.fx_rates p", None)
    with pytest.raises(NotFound):
        await policy.create_fx_rate(conn, CONTRACTING, _fx_data(), today=TODAY)


# ── Service: approve (maker-checker) ─────────────────────────────────────────


def _approve_conn(policy_row: Any, insert: Any = "OK") -> ScriptedConn:
    return (
        ScriptedConn()
        .on("SELECT p.created_by, a.id AS approval_id", policy_row)
        .on("INSERT INTO booking.policy_approvals", insert)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["rule", "fx"])
async def test_approve_by_second_person(kind: str) -> None:
    row = RULE_ROW if kind == "rule" else FX_ROW
    conn = _approve_conn({"created_by": "u-5", "approval_id": None}).on("WITH in_force AS", _approved(row))
    approve = policy.approve_rule if kind == "rule" else policy.approve_fx_rate
    out = await approve(conn, ADMIN, ROW_ID, today=TODAY)
    assert out["status"] == "in_force" and out["approved_by"] == {"id": "a-1", "name": "Admin"}
    assert out["can_approve"] is False
    (insert,) = conn.sql("INSERT INTO booking.policy_approvals")
    assert insert == (ROW_ID, "a-1")
    audit = conn.sql("INSERT INTO booking.audit_logs")[0]
    assert audit[3] == ("pricing_rule.approved" if kind == "rule" else "fx_rate.approved")
    assert "in force" in audit[9]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("policy_row", "insert", "error", "code"),
    [
        (None, "OK", NotFound, "fx_rate_not_found"),
        ({"created_by": "u-5", "approval_id": uuid.uuid4()}, "OK", Conflict, "already_approved"),
        ({"created_by": "a-1", "approval_id": None}, "OK", Forbidden, "own_row"),
        ({"created_by": None, "approval_id": None}, asyncpg.UniqueViolationError("race"), Conflict, "already_approved"),
        ({"created_by": None, "approval_id": None}, asyncpg.CheckViolationError("trigger"), Forbidden, "own_row"),
    ],
)
async def test_approve_refusals(policy_row: Any, insert: Any, error: type, code: str) -> None:
    conn = _approve_conn(policy_row, insert)
    with pytest.raises(error) as exc:
        await policy.approve_fx_rate(conn, ADMIN, ROW_ID, today=TODAY)
    assert exc.value.code == code
    assert conn.sql("INSERT INTO booking.audit_logs") == []


@pytest.mark.asyncio
async def test_approve_unknown_rule() -> None:
    with pytest.raises(NotFound) as exc:
        await policy.approve_rule(_approve_conn(None), ADMIN, ROW_ID, today=TODAY)
    assert exc.value.code == "rule_not_found"


@pytest.mark.asyncio
async def test_list_caps_the_limit() -> None:
    conn = ScriptedConn().on("FROM booking.pricing_rules p", [])
    assert await policy.list_rules(conn, ADMIN, today=TODAY, limit=10_000) == []
    assert conn.sql("FROM booking.pricing_rules p")[0][3] == policy.MAX_ROWS
