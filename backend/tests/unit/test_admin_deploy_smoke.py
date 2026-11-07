"""AA-993 V5 — wiring of the public smoke of the staff admin in ``.github/workflows/deploy-admin-dev.yml``.

Moved from the retired ``vercel-deploy.yml`` (AA-911 Vercel retirement, S272). Job ``smoke`` (Playwright project
``smoke-public`` against the production host) runs after a successful ``verify``, and job ``result`` is the run's
verdict: red unless config, deploy, verify and smoke succeeded; verify and smoke may skip only when the service runs
no task (desired count 0). The ``result`` step is run here with the ``needs`` JSON GitHub would pass. The workflow is
read as text (no YAML parser in the backend dependencies), like the AA-993 verify tests.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[3]
WORKFLOWS = REPO / ".github" / "workflows"
WF = WORKFLOWS / "deploy-admin-dev.yml"
FRONTEND = REPO / "frontend"
PW_CONFIG = FRONTEND / "playwright.config.ts"
SPEC = FRONTEND / "e2e" / "smoke-public" / "login.spec.ts"
PRODUCTION_HOST = "booking.lumiguides.it.com"

BASH = shutil.which("bash")
# Skipped only off CI (a dev box without jq); on CI a missing tool fails loudly instead of skipping.
needs_tools = pytest.mark.skipif(
    (BASH is None or shutil.which("jq") is None) and not os.environ.get("CI"),
    reason="needs bash and jq (present on CI runners)",
)


def _block(text: str, header: str, indent: int) -> str:
    """The lines of ``header`` (a key at ``indent`` spaces) up to the next key at the same or a lower indent."""
    lines = text.splitlines()
    start = lines.index(" " * indent + header)
    out = [lines[start]]
    for line in lines[start + 1 :]:
        if line.strip() and not line.startswith(" " * (indent + 1)) and not line.lstrip().startswith("#"):
            break
        out.append(line)
    while not out[-1].strip() or out[-1].lstrip().startswith("#"):  # the comment above the next key belongs to it
        out.pop()
    return "\n".join(out)


def _run_script(job: str) -> str:
    """The ``run: |`` script of the last step of a job, dedented."""
    lines = job.splitlines()
    i = max(n for n, ln in enumerate(lines) if ln.strip() == "run: |")
    indent = len(lines[i]) - len(lines[i].lstrip()) + 2
    body = []
    for ln in lines[i + 1 :]:
        if ln.strip() and not ln.startswith(" " * indent):
            break
        body.append(ln[indent:])
    return "\n".join(body)


def _jobs() -> str:
    return _block(WF.read_text(), "jobs:", 0)


def test_production_host_is_the_public_custom_domain() -> None:
    env = _block(WF.read_text(), "env:", 0)
    assert f"  PUBLIC_HOST: {PRODUCTION_HOST}\n" in env + "\n"


def test_vercel_is_retired() -> None:
    """AA-911 (S272): no Vercel deploy, config, header script or Preview host is left in the workflows."""
    assert not (WORKFLOWS / "vercel-deploy.yml").exists()
    assert not (FRONTEND / "vercel.json").exists()
    assert not (REPO / ".github" / "scripts" / "check_security_headers.sh").exists()
    for wf in WORKFLOWS.glob("*.y*ml"):
        text = wf.read_text()
        assert "VERCEL_TOKEN" not in text and "VERCEL_PROJECT_ID" not in text, wf.name
        assert "booking-preview.lumiguides.it.com" not in text, wf.name


def test_smoke_runs_after_a_successful_verify() -> None:
    job = _block(_jobs(), "smoke:", 2)
    assert "    needs: verify\n" in job
    assert "    if: needs.verify.result == 'success'\n" in job
    # A deploy runs on the pushed or dispatched ref, so the default checkout (github.sha) is the deployed commit; no
    # ref override and no npm cache (CodeQL actions/cache-poisoning, #103 review).
    assert "\n          ref:" not in job
    assert 'cache: "npm"' not in job
    assert "persist-credentials: false" in job
    assert "npx playwright install --with-deps chromium" in job
    assert 'run: SMOKE_BASE_URL="https://$PUBLIC_HOST" npm run test:smoke-public\n' in job
    # The screenshot artifact is uploaded even when the smoke fails.
    upload = job[job.index("- name: Upload the screenshot") :]
    assert "if: always()" in upload
    assert re.search(r"uses: actions/upload-artifact@[0-9a-f]{40} # v", upload), "pin the action to a SHA"
    assert "path: frontend/e2e/smoke-public/results/" in upload


def test_smoke_and_result_use_no_secret() -> None:
    jobs = _jobs()
    for name in ("smoke:", "result:"):
        job = _block(jobs, name, 2)
        assert "secrets." not in job, f"{name} the public smoke needs no secret"
        assert "contents: write" not in job and "pull-requests: write" not in job


def test_result_runs_on_every_deploy_and_needs_verify_and_smoke() -> None:
    job = _block(_jobs(), "result:", 2)
    assert "    if: always() && github.event_name != 'pull_request'\n" in job
    assert "    needs: [config, deploy, verify, smoke]\n" in job


def test_visual_runs_after_the_smoke_on_main_only() -> None:
    job = _block(_jobs(), "visual-production:", 2)
    assert "    needs: [verify, smoke]\n" in job
    assert "github.ref == 'refs/heads/main'" in job
    assert "    uses: ./.github/workflows/visual.yml\n" in job
    assert f"      base_url: https://{PRODUCTION_HOST}\n" in job


def _result(needs: dict[str, Any], running: str = "true") -> int:
    script = _run_script(_block(_jobs(), "result:", 2))
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "NEEDS": json.dumps(needs),
        "RUNNING": running,
    }
    proc = subprocess.run(  # noqa: S603 — the script is this repo's workflow step
        [BASH or "bash", "-c", script], env=env, capture_output=True, text=True, check=False
    )
    return proc.returncode


def _needs(deploy: str = "success", verify: str = "success", smoke: str = "success") -> dict[str, Any]:
    return {
        "config": {"result": "success", "outputs": {}},
        "deploy": {"result": deploy, "outputs": {}},
        "verify": {"result": verify, "outputs": {}},
        "smoke": {"result": smoke, "outputs": {}},
    }


@needs_tools
def test_green_when_deploy_verify_and_smoke_succeeded() -> None:
    assert _result(_needs()) == 0


@needs_tools
@pytest.mark.parametrize(
    ("deploy", "verify", "smoke"),
    [
        ("success", "success", "failure"),
        ("success", "success", "skipped"),
        ("success", "success", "cancelled"),
        ("success", "failure", "skipped"),
        ("failure", "skipped", "skipped"),
        ("cancelled", "skipped", "skipped"),
    ],
)
def test_red_unless_verify_and_smoke_succeeded_on_a_running_service(deploy: str, verify: str, smoke: str) -> None:
    assert _result(_needs(deploy, verify, smoke)) == 1


@needs_tools
def test_desired_count_zero_may_skip_verify_and_smoke() -> None:
    assert _result(_needs("success", "skipped", "skipped"), running="false") == 0
    # ... but never a failed smoke or a failed deploy.
    assert _result(_needs("success", "success", "failure"), running="false") == 1
    assert _result(_needs("failure", "skipped", "skipped"), running="false") == 1


def test_playwright_has_the_smoke_public_project_without_a_local_server() -> None:
    cfg = PW_CONFIG.read_text()
    project = cfg[cfg.index('name: "smoke-public"') :]
    assert r"testMatch: /smoke-public\/.*\.spec\.ts$/" in project
    assert "baseURL: SMOKE_BASE_URL" in project
    server_line = "webServer: SMOKE_BASE_URL || VISUAL_BASE_URL ? undefined : ["
    assert server_line in cfg, "a deployed URL (smoke or visual) needs no local next start"
    scripts = json.loads((FRONTEND / "package.json").read_text())["scripts"]
    assert "--project=smoke-public" in scripts["test:smoke-public"]
    assert f"https://{PRODUCTION_HOST}" in scripts["test:smoke-public"]


def test_smoke_spec_checks_status_render_errors_and_headers() -> None:
    spec = SPEC.read_text()
    assert 'page.goto("/login"' in spec
    assert ".toBe(200)" in spec
    assert 'page.on("console"' in spec and 'page.on("pageerror"' in spec
    assert 'page.on("requestfailed"' in spec and 'page.on("response"' in spec
    assert 'from "../../next.config"' in spec, "expected header values come from next.config.ts"
    for header in (
        "content-security-policy",
        "strict-transport-security",
        "x-frame-options",
        "x-content-type-options",
        "referrer-policy",
        "permissions-policy",
    ):
        assert f'"{header}"' in spec
    assert "page.screenshot(" in spec
    # Signed out only: no credential of any kind in the public smoke.
    for word in ("password", "fill(", "storageState", "bk_access", "Bearer"):
        assert word not in spec
