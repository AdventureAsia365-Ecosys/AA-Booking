"""The Deploy Dev job guard script (AA-751 B4, ``.github/scripts/booking-deploy-preflight.sh``) run for real under bash,
with stub ``aws`` and ``curl`` first on PATH. Every branch that must fail closed is pinned here, so an edit that turns
a block into a pass (the ``jq -e`` check, the ``|| CODE=000`` fallback, the task-count skip) fails Unit Tests."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.auth.ci import DEPLOY_GATE_AUDIENCE

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / ".github" / "scripts" / "booking-deploy-preflight.sh"
WORKFLOW = REPO / ".github" / "workflows" / "deploy-dev.yml"

BASH = shutil.which("bash")

# Skipped only off CI (a dev box without jq); on CI a missing tool fails loudly instead of skipping the guard tests.
pytestmark = pytest.mark.skipif(
    (BASH is None or shutil.which("jq") is None) and not os.environ.get("CI"),
    reason="needs bash and jq (present on CI runners)",
)

# Stubs log every call (one line of argv) and answer from STUB_* variables.
AWS_STUB = """#!/usr/bin/env bash
echo "aws $*" >> "$STUB_LOG"
[ -n "${STUB_AWS_ERR:-}" ] && echo "$STUB_AWS_ERR" >&2
printf '%s\\n' "${STUB_AWS_OUT:-}"
exit "${STUB_AWS_EXIT:-0}"
"""

CURL_STUB = """#!/usr/bin/env bash
echo "curl $*" >> "$STUB_LOG"
out=""
while [ $# -gt 0 ]; do
  if [ "$1" = "-o" ]; then out="$2"; shift; fi
  shift
done
if [ -n "$out" ]; then
  printf '%s' "${STUB_GATE_BODY:-}" > "$out"
  printf '%s' "${STUB_GATE_CODE:-200}"
  exit "${STUB_GATE_EXIT:-0}"
fi
printf '%s' "${STUB_MINT_BODY:-}"
exit "${STUB_MINT_EXIT:-0}"
"""

SAFE = {"active": 0, "by_status": {}, "by_kind": {}, "safe_to_deploy": True, "exempt_kinds": ["bk_inquiry_pull"]}
BUSY = {
    "active": 3,
    "by_status": {"queued": 2, "running": 1},
    "by_kind": {"bk_notify_email": 3},
    "safe_to_deploy": False,
    "exempt_kinds": ["bk_inquiry_pull"],
}


@dataclass
class Run:
    code: int
    out: str
    calls: list[str]

    def curl_calls(self) -> list[str]:
        return [c for c in self.calls if c.startswith("curl ")]


def run(
    tmp_path: Path,
    *,
    counts: str = "1\t0\t1",
    aws_exit: int = 0,
    mint: str | None = '{"value": "tok.en.sig"}',
    mint_exit: int = 0,
    gate_body: str = json.dumps(SAFE),
    gate_code: str = "200",
    gate_exit: int = 0,
    override: str = "",
    oidc: bool = True,
) -> Run:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in (("aws", AWS_STUB), ("curl", CURL_STUB)):
        stub = bin_dir / name
        stub.write_text(body)
        stub.chmod(0o755)
    log = tmp_path / "calls.log"
    log.touch()
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "STUB_LOG": str(log),
        "STUB_AWS_OUT": counts,
        "STUB_AWS_EXIT": str(aws_exit),
        "STUB_MINT_BODY": mint or "",
        "STUB_MINT_EXIT": str(mint_exit),
        "STUB_GATE_BODY": gate_body,
        "STUB_GATE_CODE": gate_code,
        "STUB_GATE_EXIT": str(gate_exit),
        "ECS_CLUSTER": "aa-cis-dev-cluster",
        "ECS_SERVICE": "aa-booking-dev-api",
        "BASE_URL": "https://api-booking.test/",
        "OVERRIDE": override,
    }
    if oidc:
        env["ACTIONS_ID_TOKEN_REQUEST_URL"] = "https://oidc.test/token?api-version=2.0"
        env["ACTIONS_ID_TOKEN_REQUEST_TOKEN"] = "runner-request-token"
    # A fixed script from this repo, run by an absolute bash path: no untrusted input.
    proc = subprocess.run(  # noqa: S603
        [str(BASH), str(SCRIPT)], env=env, cwd=tmp_path, capture_output=True, text=True, timeout=30, check=False
    )
    return Run(proc.returncode, proc.stdout + proc.stderr, log.read_text().splitlines())


# ── wiring ───────────────────────────────────────────────────────────────────


def test_workflow_runs_this_script_after_a_checkout() -> None:
    text = WORKFLOW.read_text()
    assert "run: bash .github/scripts/booking-deploy-preflight.sh" in text
    preflight = text[text.index("  preflight:") : text.index("  deploy:\n")]
    assert "actions/checkout@" in preflight
    assert "id-token: write" in text
    assert f"GATE_AUDIENCE: {DEPLOY_GATE_AUDIENCE}" in preflight


# ── task counts ──────────────────────────────────────────────────────────────


def test_nothing_running_pending_or_desired_skips_the_gate(tmp_path: Path) -> None:
    r = run(tmp_path, counts="0\t0\t0")
    assert r.code == 0
    assert "gate skipped" in r.out
    assert r.curl_calls() == []
    (aws,) = r.calls
    assert "describe-services --cluster aa-cis-dev-cluster --services aa-booking-dev-api" in aws
    assert "runningCount,pendingCount,desiredCount" in aws


@pytest.mark.parametrize("counts", ["0\t1\t1", "0\t0\t1", "0\t1\t0"], ids=["pending", "desired", "pending-only"])
def test_a_starting_task_blocks(tmp_path: Path, counts: str) -> None:
    r = run(tmp_path, counts=counts)
    assert r.code == 1
    assert "::error::Deploy blocked: no task is RUNNING yet" in r.out
    assert r.curl_calls() == []


def test_a_starting_task_with_override_passes_with_a_warning(tmp_path: Path) -> None:
    r = run(tmp_path, counts="0\t1\t1", override="hotfix for crash loop")
    assert r.code == 0
    assert "::warning::Deploy guard OVERRIDDEN (no task is RUNNING yet" in r.out
    assert "Reason: hotfix for crash loop" in r.out


@pytest.mark.parametrize(
    ("counts", "aws_exit"),
    [("None", 0), ("", 255), ("1\t0", 0), ("x\t0\t1", 0), ("1\tNone\t1", 0)],
    ids=["service-missing", "aws-error", "short", "non-numeric", "null-pending"],
)
def test_unreadable_task_counts_block(tmp_path: Path, counts: str, aws_exit: int) -> None:
    r = run(tmp_path, counts=counts, aws_exit=aws_exit)
    assert r.code == 1
    assert "cannot read the task counts" in r.out
    assert r.curl_calls() == []


# ── OIDC token ───────────────────────────────────────────────────────────────


def test_no_oidc_request_env_blocks(tmp_path: Path) -> None:
    r = run(tmp_path, oidc=False)
    assert r.code == 1
    assert "no GitHub OIDC token available" in r.out
    assert r.curl_calls() == []


@pytest.mark.parametrize(
    ("mint", "mint_exit"), [("", 22), ('{"other": 1}', 0), ("<html>", 0)], ids=["http-error", "no-value", "not-json"]
)
def test_mint_failure_blocks(tmp_path: Path, mint: str, mint_exit: int) -> None:
    r = run(tmp_path, mint=mint, mint_exit=mint_exit)
    assert r.code == 1
    assert "could not mint the GitHub OIDC token" in r.out
    assert len(r.curl_calls()) == 1


def test_token_is_minted_for_the_gate_audience_masked_and_sent(tmp_path: Path) -> None:
    r = run(tmp_path)
    assert r.code == 0
    mint, gate = r.curl_calls()
    assert f"audience={DEPLOY_GATE_AUDIENCE}" in mint
    assert "Authorization: bearer runner-request-token" in mint
    assert "::add-mask::tok.en.sig" in r.out
    assert "Authorization: Bearer tok.en.sig" in gate
    assert gate.endswith("https://api-booking.test/booking/ops/deploy-gate")


# ── gate answer ──────────────────────────────────────────────────────────────


def test_safe_gate_passes(tmp_path: Path) -> None:
    r = run(tmp_path)
    assert r.code == 0
    assert "safe to deploy" in r.out
    assert "bk_inquiry_pull" in r.out


@pytest.mark.parametrize(
    ("gate_code", "gate_body", "gate_exit"),
    [
        ("404", '{"detail": "Not Found"}', 0),
        ("200", "<html><body>ok</body></html>", 0),
        ("503", '{"detail": "auth_unavailable"}', 0),
        ("401", '{"detail": "invalid_token"}', 0),
        ("000", "", 7),
        ("200", '{"safe_to_deploy": "true", "active": 0}', 0),
        ("200", '{"safe_to_deploy": true}', 0),
        ("200", "", 0),
    ],
    ids=["404", "html-200", "503", "401", "network", "string-bool", "no-active", "empty-200"],
)
def test_unreachable_or_invalid_gate_blocks(tmp_path: Path, gate_code: str, gate_body: str, gate_exit: int) -> None:
    r = run(tmp_path, gate_code=gate_code, gate_body=gate_body, gate_exit=gate_exit)
    assert r.code == 1
    assert "deploy gate unreachable or invalid" in r.out


def test_network_failure_reports_code_000(tmp_path: Path) -> None:
    r = run(tmp_path, gate_code="", gate_body="", gate_exit=7)
    assert r.code == 1
    assert "(HTTP 000)" in r.out


def test_busy_gate_blocks_with_the_counts(tmp_path: Path) -> None:
    r = run(tmp_path, gate_body=json.dumps(BUSY))
    assert r.code == 1
    assert "::error::Deploy blocked: 3 job(s) queued/running (2 queued, 1 running; by kind: bk_notify_email=3)" in r.out


def test_busy_gate_with_override_passes_with_a_warning(tmp_path: Path) -> None:
    r = run(tmp_path, gate_body=json.dumps(BUSY), override="  urgent fix\n")
    assert r.code == 0
    assert "::warning::Deploy guard OVERRIDDEN (3 job(s) queued/running" in r.out
    assert "Reason: urgent fix" in r.out
    assert "::error::" not in r.out


@pytest.mark.parametrize("override", [" ", "\n", " \t \r\n "], ids=["space", "newline", "mixed"])
def test_whitespace_only_override_is_no_override(tmp_path: Path, override: str) -> None:
    r = run(tmp_path, gate_body=json.dumps(BUSY), override=override)
    assert r.code == 1
    assert "::error::Deploy blocked" in r.out


def test_unreachable_gate_with_override_passes(tmp_path: Path) -> None:
    r = run(tmp_path, gate_code="503", gate_body="{}", override="gate down, queue checked by hand")
    assert r.code == 0
    assert "OVERRIDDEN (deploy gate unreachable or invalid (HTTP 503)" in r.out


def test_running_with_a_pending_replacement_still_asks_the_gate(tmp_path: Path) -> None:
    """A rolling deploy in progress (1 running, 1 pending) is judged by the gate, not skipped and not blocked."""
    r = run(tmp_path, counts="1\t1\t1")
    assert r.code == 0
    assert len(r.curl_calls()) == 2
