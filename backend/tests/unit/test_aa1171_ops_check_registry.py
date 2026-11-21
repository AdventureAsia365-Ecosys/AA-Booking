"""AA-1171 (S287 alerts catalogue P0, decision D1) — GET /booking/ops/checks, the job check registry the CIS alerts
catalogue reads, and the two job check thresholds as pricing policy parameters (bk-063).

Auth: a WORKFORCE token of the booking-staff client (any booking-* group) or, on this route only, of the CIS admin
client (group cis-admin); 401 without a token or with another client, 403 for another group. The body: both checks
with the values in force, their policy keys and unit, the last alert per check. Local RSA keys, scripted DB."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.auth import identity_config
from app.main import create_app
from app.pricing import policy_params
from app.routers import ops_checks as route
from tests.staff_tokens import CIS_ADMIN_CLIENT, LocalCis
from tests.unit.fakes import POLICY_FRAGMENT, ScriptedConn, policy_rows
from tests.unit.test_aa911_preview_cap import DSN, FakeDatabase, auth

PATH = "/booking/ops/checks"


def _client(cis: LocalCis, conn: ScriptedConn, *, cis_admin: bool = True) -> TestClient:
    settings = replace(
        cis.settings(), database_url=DSN, cognito_cis_admin_client_id=CIS_ADMIN_CLIENT if cis_admin else ""
    )
    verifier = identity_config.build_staff_verifier(settings, cis.fetch)
    reader = identity_config.build_ops_reader_verifier(settings, cis.fetch)
    return TestClient(create_app(settings, FakeDatabase(settings, conn), verifier, ops_reader_verifier=reader))


def _conn(**policy: str) -> ScriptedConn:
    last = datetime(2026, 11, 20, 3, 0, tzinfo=UTC)
    return (
        ScriptedConn()
        .on(POLICY_FRAGMENT, lambda *_: policy_rows(**policy))
        .on("split_part(idempotency_key", [{"check_key": "jobs_stuck", "last_at": last}])
    )


def test_the_parameters_are_ops_section_and_administrator_approved() -> None:
    for key, kind in (("ops_jobs_stuck_after_s", "integer"), ("ops_jobs_overdue_factor", "decimal")):
        p = policy_params.PARAMS[key]
        assert (p.type, p.group, p.section, p.used_by) == (kind, "admin", "ops", "app.jobs.metrics")
    assert "ops" in policy_params.SECTIONS
    assert policy_params.normalise("ops_jobs_stuck_after_s", 900, {}) == 900


def test_cis_admin_token_reads_the_registry_with_the_values_in_force() -> None:
    cis = LocalCis()
    token = cis.workforce.token(["cis-admin"], client_id=CIS_ADMIN_CLIENT)
    with _client(cis, _conn(ops_jobs_stuck_after_s="1200")) as c:
        r = c.get(PATH, headers=auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["app"] == "booking" and body["version"] == 1
    checks = {c["key"]: c for c in body["checks"]}
    assert set(checks) == {"jobs_stuck", "jobs_overdue"}
    stuck = checks["jobs_stuck"]["thresholds"][0]
    assert (stuck["setting"], stuck["value"], stuck["unit"], stuck["editable_via"]) == (
        "ops_jobs_stuck_after_s",
        1200,
        "seconds",
        "settings",
    )
    assert checks["jobs_overdue"]["thresholds"][0]["value"] == "2"
    assert checks["jobs_stuck"]["last_alert_at"].startswith("2026-11-20")
    assert checks["jobs_overdue"]["last_alert_at"] is None


def test_booking_staff_token_reads_too() -> None:
    cis = LocalCis()
    with _client(cis, _conn()) as c:
        assert c.get(PATH, headers=auth(cis.workforce.token(["booking-sales"]))).status_code == 200


def test_refusals() -> None:
    cis = LocalCis()
    with _client(cis, _conn()) as c:
        assert c.get(PATH).status_code == 401
        other_group = cis.workforce.token(["cis-content"], client_id=CIS_ADMIN_CLIENT)
        assert c.get(PATH, headers=auth(other_group)).status_code == 403
    with _client(cis, _conn(), cis_admin=False) as c:  # the CIS client is not accepted when unset
        token = cis.workforce.token(["cis-admin"], client_id=CIS_ADMIN_CLIENT)
        assert c.get(PATH, headers=auth(token)).status_code == 401


def test_the_cis_client_opens_no_staff_route() -> None:
    cis = LocalCis()
    token = cis.workforce.token(["cis-admin", "booking-admin"], client_id=CIS_ADMIN_CLIENT)
    with _client(cis, _conn()) as c:
        assert c.get("/booking/staff/me", headers=auth(token)).status_code == 401


def test_no_value_in_force_is_503() -> None:
    cis = LocalCis()
    rows = [r for r in policy_rows() if r["param_key"] != "ops_jobs_overdue_factor"]
    conn = ScriptedConn().on(POLICY_FRAGMENT, lambda *_: rows)
    token = cis.workforce.token(["cis-admin"], client_id=CIS_ADMIN_CLIENT)
    with _client(cis, conn) as c:
        r = c.get(PATH, headers=auth(token))
    assert r.status_code == 503 and r.json()["detail"] == "policy_unavailable"


def test_registry_body_is_pure() -> None:
    now = datetime(2026, 11, 21, tzinfo=UTC)
    body = route.registry_body(
        {"ops_jobs_stuck_after_s": 900, "ops_jobs_overdue_factor": "2"}, {"jobs_overdue": now - timedelta(hours=1)}, now
    )
    assert body["as_of"] == now.isoformat() and len(body["checks"]) == 2
    assert body["checks"][1]["last_alert_at"] == (now - timedelta(hours=1)).isoformat()
