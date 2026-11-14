"""S280 (bk-058): reject and withdraw of a pending pricing rule, FX rate or cancellation policy, with a scripted
connection: the routes (permission, body), ``app.pricing.policy.close_row`` (maker-checker, one closure per row,
never an approved row) and the closed row's fields. Real SQL, CHECKs and triggers: tests/integration/test_bk058_*.py.
"""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.auth.staff import current_staff, get_pool
from app.b2b import cancellation
from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.main import create_app
from app.pricing import policy
from app.settings import Settings
from tests.unit.fakes import ScriptedConn
from tests.unit.test_pricing_policy import ADMIN, CONTRACTING, FX_ROW, ROW_ID, RULE_ROW, SALES, TODAY

CLOSE = "INSERT INTO booking.policy_row_closures"
AUDIT = "INSERT INTO booking.audit_logs"


def _client(staff: Any, conn: ScriptedConn | None = None) -> TestClient:
    app = create_app(Settings(database_url=None, environment="test"))
    app.dependency_overrides[current_staff] = lambda: staff
    app.dependency_overrides[get_pool] = lambda: conn or ScriptedConn()
    return TestClient(app)


def _policy_row(created_by: str = "u-5", approval: Any = None, closure: Any = None) -> dict[str, Any]:
    return {"created_by": created_by, "day": TODAY, "approval_id": approval, "closure_id": closure}


def _closed(row: dict[str, Any], event: str, actor: str, name: str, reason: str | None) -> dict[str, Any]:
    return {
        **row,
        "status": event,
        "closed_by": actor,
        "closed_by_name": name,
        "closed_at": row["created_at"],
        "close_reason": reason,
    }


def _conn(policy_row: Any, after: dict[str, Any] | None = None, insert: Any = "OK") -> ScriptedConn:
    conn = ScriptedConn().on("a.id AS approval_id", policy_row).on(CLOSE, insert)
    return conn.on("WITH in_force AS", after) if after else conn


# ── Routes ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", ["rules", "fx-rates", "cancellation-policies"])
def test_routes_need_pricing_write_and_a_reason_to_reject(path: str) -> None:
    base = f"/booking/pricing/{path}/{ROW_ID}"
    assert _client(SALES).post(f"{base}/reject", json={"reason": "wrong"}).status_code == 403
    assert _client(SALES).post(f"{base}/withdraw").status_code == 403
    for body in ({}, {"reason": ""}, {"reason": "x" * 301}, {"reason": "ok", "extra": 1}):
        assert _client(ADMIN).post(f"{base}/reject", json=body).status_code == 422


def test_reject_route_closes_a_rule_with_the_reason() -> None:
    conn = _conn(_policy_row(), _closed(RULE_ROW, "rejected", "a-1", "Admin", "Margin too low"))
    r = _client(ADMIN, conn).post(f"/booking/pricing/rules/{ROW_ID}/reject", json={"reason": "  Margin too low "})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "rejected" and out["closed_by"] == {"id": "a-1", "name": "Admin"}
    assert out["close_reason"] == "Margin too low"
    assert out["can_approve"] is False and out["can_reject"] is False and out["can_withdraw"] is False
    (args,) = conn.sql(CLOSE)
    assert args == ("pricing_rule", ROW_ID, "rejected", "u-5", "a-1", "Margin too low")
    audit = conn.sql(AUDIT)[0]
    assert audit[3] == "pricing_rule.rejected" and "Margin too low" in audit[9]


def test_withdraw_route_closes_an_fx_rate_of_its_maker() -> None:
    conn = _conn(_policy_row(), _closed(FX_ROW, "withdrawn", "u-5", "Contracts", None))
    r = _client(CONTRACTING, conn).post(f"/booking/pricing/fx-rates/{ROW_ID}/withdraw")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "withdrawn"
    (args,) = conn.sql(CLOSE)
    assert args == ("fx_rate", ROW_ID, "withdrawn", "u-5", "u-5", None)
    assert conn.sql(AUDIT)[0][3] == "fx_rate.withdrawn"


def test_pending_row_says_who_may_reject_or_withdraw() -> None:
    conn = ScriptedConn().on("FROM booking.pricing_rules p", [RULE_ROW])
    maker = _client(CONTRACTING, conn).get("/booking/pricing/rules").json()["items"][0]
    assert (maker["can_reject"], maker["can_withdraw"]) == (False, True)
    other = _client(ADMIN, conn).get("/booking/pricing/rules").json()["items"][0]
    assert (other["can_reject"], other["can_withdraw"]) == (True, False)
    reader = _client(SALES, conn).get("/booking/pricing/rules").json()["items"][0]
    assert (reader["can_reject"], reader["can_withdraw"]) == (False, False)


# ── Service: close_row ───────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("staff", "event", "reason", "policy_row", "insert", "error", "code"),
    [
        (ADMIN, "rejected", None, _policy_row(), "OK", Invalid, "reason_required"),
        (ADMIN, "rejected", "  ", _policy_row(), "OK", Invalid, "reason_required"),
        (ADMIN, "rejected", "x" * 301, _policy_row(), "OK", Invalid, "reason_required"),
        (ADMIN, "rejected", "why", None, "OK", NotFound, "fx_rate_not_found"),
        (ADMIN, "rejected", "why", _policy_row(approval=uuid.uuid4()), "OK", Conflict, "already_approved"),
        (ADMIN, "rejected", "why", _policy_row(closure=uuid.uuid4()), "OK", Conflict, "already_decided"),
        (CONTRACTING, "rejected", "why", _policy_row(), "OK", Forbidden, "own_row"),
        (ADMIN, "withdrawn", None, _policy_row(), "OK", Forbidden, "not_maker"),
        (ADMIN, "rejected", "why", _policy_row(), asyncpg.UniqueViolationError("race"), Conflict, "already_decided"),
        (ADMIN, "rejected", "why", _policy_row(), asyncpg.CheckViolationError("trigger"), Conflict, "already_decided"),
    ],
)
async def test_close_refusals(
    staff: Any, event: str, reason: str | None, policy_row: Any, insert: Any, error: type, code: str
) -> None:
    conn = _conn(policy_row, insert=insert)
    with pytest.raises(error) as exc:
        await policy.close_fx_rate(conn, staff, ROW_ID, event=event, reason=reason, today=TODAY)
    assert exc.value.code == code
    assert conn.sql(AUDIT) == []


@pytest.mark.asyncio
async def test_close_takes_the_row_lock_before_reading() -> None:
    conn = _conn(_policy_row(), _closed(FX_ROW, "withdrawn", "u-5", "Contracts", None))
    await policy.close_fx_rate(conn, CONTRACTING, ROW_ID, event="withdrawn", reason=None, today=TODAY)
    sqls = [sql for sql, _ in conn.calls]
    lock = next(i for i, s in enumerate(sqls) if "pg_advisory_xact_lock" in s)
    read = next(i for i, s in enumerate(sqls) if "a.id AS approval_id" in s)
    assert lock < read


@pytest.mark.asyncio
async def test_a_closed_row_is_never_approved() -> None:
    conn = ScriptedConn().on("a.id AS approval_id", _policy_row(closure=uuid.uuid4()))
    with pytest.raises(Conflict) as exc:
        await policy.approve_rule(conn, ADMIN, ROW_ID, today=TODAY)
    assert exc.value.code == "already_decided"
    assert conn.sql("INSERT INTO booking.policy_approvals") == []


@pytest.mark.asyncio
async def test_cancellation_policy_reject_records_its_label() -> None:
    from tests.unit.test_b2b_commercial import ROW as CANCEL_ROW

    after = _closed(CANCEL_ROW, "rejected", "a-1", "Admin", "Tier gap")
    conn = _conn(_policy_row(), after)
    out = await cancellation.close_policy(conn, ADMIN, ROW_ID, event="rejected", reason="Tier gap", today=TODAY)
    assert out["status"] == "rejected" and out["close_reason"] == "Tier gap"
    (args,) = conn.sql(CLOSE)
    assert args[:3] == ("cancellation_policy", ROW_ID, "rejected")
    audit = conn.sql(AUDIT)[0]
    assert audit[3] == "cancellation_policy.rejected" and "BT-7D" in audit[9]
