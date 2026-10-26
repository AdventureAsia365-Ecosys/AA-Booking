"""AA-993 V5 — wiring of the public smoke of the staff admin in ``.github/workflows/vercel-deploy.yml``.

The production path runs job ``smoke`` (Playwright project ``smoke-public`` against the production host) after
``deploy``, and job ``result`` is the run's verdict: red on a production run unless deploy and smoke both succeeded,
not gated on previews. The ``result`` step is run here with the ``needs`` JSON GitHub would pass. The workflow is read
as text (no YAML parser in the backend dependencies), like the AA-993 verify tests.
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
WF = REPO / ".github" / "workflows" / "vercel-deploy.yml"
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
    assert f"  PRODUCTION_HOST: {PRODUCTION_HOST}\n" in env + "\n"


def test_smoke_runs_after_a_successful_production_deploy_only() -> None:
    job = _block(_jobs(), "smoke:", 2)
    assert "    needs: deploy\n" in job
    assert "    if: needs.deploy.result == 'success' && needs.deploy.outputs.target == 'production'\n" in job
    assert "ref: ${{ needs.deploy.outputs.sha }}" in job, "smoke must check out the commit that was deployed"
    assert "persist-credentials: false" in job
    assert "npx playwright install --with-deps chromium" in job
    assert 'run: SMOKE_BASE_URL="https://$PRODUCTION_HOST" npm run test:smoke-public\n' in job
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


def test_result_runs_always_and_needs_deploy_and_smoke() -> None:
    job = _block(_jobs(), "result:", 2)
    assert "    if: always()\n" in job
    assert "    needs: [deploy, smoke]\n" in job
    # The dispatch input is only tested for emptiness, never printed.
    script = _run_script(job)
    for line in script.splitlines():
        if "echo" in line:
            assert "ALIAS_ONLY_URL" not in line


def _result(event: str, needs: dict[str, Any], target: str = "", alias_only_url: str = "") -> int:
    script = _run_script(_block(_jobs(), "result:", 2))
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "NEEDS": json.dumps(needs),
        "EVENT": event,
        "INPUT_TARGET": target,
        "ALIAS_ONLY_URL": alias_only_url,
    }
    proc = subprocess.run(  # noqa: S603 — the script is this repo's workflow step
        [BASH or "bash", "-c", script], env=env, capture_output=True, text=True, check=False
    )
    return proc.returncode


def _needs(deploy: str = "success", smoke: str = "success") -> dict[str, Any]:
    return {"deploy": {"result": deploy, "outputs": {}}, "smoke": {"result": smoke, "outputs": {}}}


PRODUCTION_RUNS = [("push", ""), ("workflow_dispatch", "production")]


@needs_tools
@pytest.mark.parametrize(("event", "target"), PRODUCTION_RUNS)
def test_production_is_green_when_deploy_and_smoke_succeeded(event: str, target: str) -> None:
    assert _result(event, _needs(), target) == 0


@needs_tools
@pytest.mark.parametrize(("event", "target"), PRODUCTION_RUNS)
@pytest.mark.parametrize(
    ("deploy", "smoke"),
    [
        ("success", "failure"),
        ("success", "skipped"),
        ("success", "cancelled"),
        ("failure", "skipped"),
        ("cancelled", "skipped"),
        ("skipped", "skipped"),
    ],
)
def test_production_is_red_unless_deploy_and_smoke_succeeded(event: str, target: str, deploy: str, smoke: str) -> None:
    assert _result(event, _needs(deploy, smoke), target) == 1


@needs_tools
def test_production_is_red_when_a_job_result_is_missing() -> None:
    needs = _needs()
    del needs["smoke"]
    assert _result("push", needs) == 1


@needs_tools
@pytest.mark.parametrize(
    ("event", "target", "alias", "deploy"),
    [
        ("pull_request", "", "", "success"),
        ("pull_request", "", "", "failure"),  # the deploy job is red on its own, as before
        ("pull_request", "", "", "skipped"),  # Dependabot / fork / other label
        ("workflow_dispatch", "preview", "", "success"),
        ("workflow_dispatch", "production", "https://aa-booking-admin-x.vercel.app", "skipped"),  # alias-only
    ],
)
def test_previews_and_alias_only_runs_are_not_gated(event: str, target: str, alias: str, deploy: str) -> None:
    assert _result(event, _needs(deploy, "skipped"), target, alias) == 0


def test_playwright_has_the_smoke_public_project_without_a_local_server() -> None:
    cfg = PW_CONFIG.read_text()
    project = cfg[cfg.index('name: "smoke-public"') :]
    assert r"testMatch: /smoke-public\/.*\.spec\.ts$/" in project
    assert "baseURL: SMOKE_BASE_URL" in project
    assert "webServer: SMOKE_BASE_URL ? undefined : [" in cfg, "a deployed URL needs no local next start"
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
