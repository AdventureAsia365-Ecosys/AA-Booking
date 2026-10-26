"""AA-993 V2 (ADR 0016 D3) — the Booking post-deploy verify is red/green and a skipped verify fails Deploy Dev.

- ``.github/scripts/post_deploy_verify.sh booking`` runs for real against fixture JSON: ``curl`` and ``aws`` on PATH are
  stubs that answer from the fixture (no network, no AWS). All good exits 0; a wrong git_sha or version, a FAILED or
  unfinished rollout, desired count 0, a second deployment, an old image, a missing service, ``/booking/health/ready``
  503 or on an older schema each exit 1. The real ``.github/verify/booking.rows`` is used.
- The workflows are checked as text (no YAML parser in the test deps, like ``test_deploy_preflight_script.py``):
  the verify runs after ``deploy`` with its own concurrency group (never ``deploy-dev``); ``config`` fails instead of
  skipping when ``AWS_ROLE_ARN`` is unset; the ``result`` job runs always, needs every job, and its step (run here
  with the ``needs`` JSON GitHub would pass) is red when any job did not succeed.
- The required-schema extraction of the workflow gives the newest key of ``REQUIRED_SCHEMA_VERSIONS`` in ``app/db.py``.
"""

from __future__ import annotations

import ast
import copy
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[3]
APP = "booking"
SCRIPT = REPO / ".github" / "scripts" / "post_deploy_verify.sh"
ROWS = REPO / ".github" / "verify" / f"{APP}.rows"
VERIFY_WF = REPO / ".github" / "workflows" / "post-deploy-verify.yml"
DEPLOY_WF = REPO / ".github" / "workflows" / "deploy-dev.yml"
DB_PY = REPO / "backend" / "app" / "db.py"
PREFIX = "bk"
HEALTH, READY = "/booking/health", "/booking/health/ready"
SERVICE_NAME = "aa-booking-api"
ECS_SVC = "aa-booking-dev-api"
DEPLOY_JOBS = ("config", "preflight", "deploy", "verify")

BASH = shutil.which("bash")
# Skipped only off CI (a dev box without jq); on CI a missing tool fails loudly instead of skipping the verify tests.
needs_tools = pytest.mark.skipif(
    (BASH is None or shutil.which("jq") is None) and not os.environ.get("CI"),
    reason="needs bash and jq (present on CI runners)",
)

SHA = "3f2a9c1e7b4d6a0f5e8c2b1d9a7f4e6c0b3d5a2e"
OLD_SHA = "9e1b7c3a5d2f4e6a8c0b1d3f5a7e9c2b4d6f8a0c"
VERSION = "0.7.0"
REQUIRED = f"{PREFIX}-026"
IMAGE = f"registry.example.invalid/{ECS_SVC}:{SHA}"

FAKE_CURL = r"""
import json, os, sys
from urllib.parse import urlparse
args = sys.argv[1:]
out = fmt = url = None
i = 0
while i < len(args):
    a = args[i]
    if a in ("-o", "-w", "-H", "--max-time", "-X"):
        if a == "-o":
            out = args[i + 1]
        elif a == "-w":
            fmt = args[i + 1]
        i += 2
        continue
    if not a.startswith("-"):
        url = a
    i += 1
route = json.load(open(os.environ["FAKE_HTTP"])).get(urlparse(url).path)
status, body = (404, {"detail": "not found"}) if route is None else (route["status"], route["body"])
if out:
    with open(out, "w") as f:
        json.dump(body, f)
if fmt:
    sys.stdout.write(str(status))
"""

FAKE_AWS = r"""
import json, os, sys
args = sys.argv[1:]
def opt(name):
    return args[args.index(name) + 1]
ecs = json.load(open(os.environ["FAKE_ECS"]))
if args[:2] == ["ecs", "describe-services"]:
    svc = ecs["services"].get(opt("--services"))
    print(json.dumps({"services": [svc]} if svc else {"services": [], "failures": [{"reason": "MISSING"}]}))
elif args[:2] == ["ecs", "describe-task-definition"]:
    td = ecs["taskDefinitions"].get(opt("--task-definition"))
    if td is None:
        sys.stderr.write("task definition not found")
        sys.exit(255)
    print(json.dumps({"taskDefinition": td}))
else:
    sys.stderr.write("unexpected aws call: " + " ".join(args))
    sys.exit(2)
"""


def _service(td: str, rollout: str = "COMPLETED", desired: int = 1, running: int = 1, deployments: int = 1) -> dict:
    deps = [{"status": "PRIMARY", "taskDefinition": td, "rolloutState": rollout}]
    deps += [{"status": "ACTIVE", "taskDefinition": td + "-old", "rolloutState": "COMPLETED"}] * (deployments - 1)
    return {
        "serviceName": ECS_SVC,
        "status": "ACTIVE",
        "desiredCount": desired,
        "runningCount": running,
        "taskDefinition": td,
        "deployments": deps,
    }


def _good() -> tuple[dict, dict]:
    """A Dev that runs SHA: (http routes, ecs state)."""
    http = {
        HEALTH: {"status": 200, "body": {"status": "ok", "service": SERVICE_NAME, "version": VERSION, "git_sha": SHA}},
        # The DB is ahead of the code (another migration applied first): still green.
        READY: {"status": 200, "body": {"status": "ok", "db": "ok", "schema_version": f"{PREFIX}-027"}},
    }
    ecs = {
        "services": {ECS_SVC: _service("td-api:2")},
        "taskDefinitions": {"td-api:2": {"containerDefinitions": [{"image": IMAGE}]}},
    }
    return http, ecs


def _stub(bin_dir: Path, name: str, source: str) -> None:
    path = bin_dir / name
    path.write_text(f"#!{sys.executable}\n{source}")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


Run = Callable[..., tuple[subprocess.CompletedProcess[str], str]]


@pytest.fixture
def run_verify(tmp_path: Path) -> Run:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _stub(bin_dir, "curl", FAKE_CURL)
    _stub(bin_dir, "aws", FAKE_AWS)

    def run(http: dict, ecs: dict, *args: str, **env_over: str) -> tuple[subprocess.CompletedProcess[str], str]:
        (tmp_path / "http.json").write_text(json.dumps(http))
        (tmp_path / "ecs.json").write_text(json.dumps(ecs))
        summary = tmp_path / "summary.md"
        summary.write_text("")
        env = {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_HTTP": str(tmp_path / "http.json"),
            "FAKE_ECS": str(tmp_path / "ecs.json"),
            "EXPECTED_SHA": SHA,
            "EXPECTED_VERSION": VERSION,
            "REQUIRED_SCHEMA": REQUIRED,
            "EXPECTED_IMAGE": IMAGE,
            "ECS_CLUSTER": "aa-cis-dev-cluster",
            "ECS_SERVICE": ECS_SVC,
            "AWS_REGION": "us-west-1",
            "BASE": "http://verify.test/",
            "GITHUB_STEP_SUMMARY": str(summary),
            "VERIFY_TRIES": "2",
            "VERIFY_RETRY_DELAY": "0",
            "VERIFY_INITIAL_WAIT": "0",
        }
        env.update(env_over)
        proc = subprocess.run(  # noqa: S603
            [str(BASH), str(SCRIPT), *(args or (APP,))], env=env, capture_output=True, text=True, timeout=120
        )
        return proc, summary.read_text()

    return run


def _failed_rows(summary: str) -> set[str]:
    return {line.split("|")[1].strip() for line in summary.splitlines() if "| **FAIL** |" in line}


@needs_tools
def test_all_good_exits_zero_and_prints_the_table(run_verify: Run) -> None:
    r, summary = run_verify(*_good())
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"### {APP} post-deploy verify: PASS" in summary
    for row in ("inputs", "ecs-rollout", "ecs-image", "health", "health-ready"):
        assert f"| {row} | PASS |" in summary, (row, summary)
    assert "**FAIL**" not in summary
    assert "registry.example.invalid" not in summary + r.stdout, "the registry names the AWS account: never printed"


def _health_sha_mismatch(http: dict, ecs: dict) -> None:
    http[HEALTH]["body"]["git_sha"] = OLD_SHA


def _health_version_mismatch(http: dict, ecs: dict) -> None:
    http[HEALTH]["body"]["version"] = "0.6.9"


def _health_500(http: dict, ecs: dict) -> None:
    http[HEALTH] = {"status": 500, "body": {"detail": "boom"}}


def _rollout_failed(http: dict, ecs: dict) -> None:
    ecs["services"][ECS_SVC]["deployments"][0]["rolloutState"] = "FAILED"


def _rollout_in_progress(http: dict, ecs: dict) -> None:
    ecs["services"][ECS_SVC]["deployments"][0]["rolloutState"] = "IN_PROGRESS"


def _ready_503(http: dict, ecs: dict) -> None:
    http[READY] = {
        "status": 503,
        "body": {
            "status": "not_ready",
            "db": "ok",
            "schema_version": f"{PREFIX}-024",
            "required_schema_version": REQUIRED,
            "missing_schema_versions": [REQUIRED],
        },
    }


def _schema_behind_but_200(http: dict, ecs: dict) -> None:
    http[READY]["body"]["schema_version"] = f"{PREFIX}-025"  # the running code is older than the deployed commit


def _schema_wrong_prefix(http: dict, ecs: dict) -> None:
    http[READY]["body"]["schema_version"] = "999"


def _schema_null(http: dict, ecs: dict) -> None:
    http[READY]["body"]["schema_version"] = None


def _desired_zero(http: dict, ecs: dict) -> None:
    ecs["services"][ECS_SVC] = _service("td-api:2", desired=0, running=0)


def _running_short(http: dict, ecs: dict) -> None:
    ecs["services"][ECS_SVC] = _service("td-api:2", desired=2, running=1)


def _old_deployment_left(http: dict, ecs: dict) -> None:
    ecs["services"][ECS_SVC] = _service("td-api:2", deployments=2)


def _service_missing(http: dict, ecs: dict) -> None:
    del ecs["services"][ECS_SVC]


def _service_inactive(http: dict, ecs: dict) -> None:
    ecs["services"][ECS_SVC]["status"] = "INACTIVE"


def _old_image(http: dict, ecs: dict) -> None:
    ecs["taskDefinitions"]["td-api:2"]["containerDefinitions"][0]["image"] = IMAGE.replace(SHA, OLD_SHA)


@needs_tools
@pytest.mark.parametrize(
    ("breaker", "red_rows"),
    [
        (_health_sha_mismatch, {"health"}),
        (_health_version_mismatch, {"health"}),
        (_health_500, {"health"}),
        (_rollout_failed, {"ecs-rollout"}),
        (_rollout_in_progress, {"ecs-rollout"}),
        (_ready_503, {"health-ready"}),
        (_schema_behind_but_200, {"health-ready"}),
        (_schema_wrong_prefix, {"health-ready"}),
        (_schema_null, {"health-ready"}),
        (_desired_zero, {"ecs-rollout"}),
        (_running_short, {"ecs-rollout"}),
        (_old_deployment_left, {"ecs-rollout"}),
        (_service_missing, {"ecs-rollout", "ecs-image"}),
        (_service_inactive, {"ecs-rollout"}),
        (_old_image, {"ecs-image"}),
    ],
)
def test_each_failure_is_red(run_verify: Run, breaker: Callable[[dict, dict], None], red_rows: set[str]) -> None:
    http, ecs = copy.deepcopy(_good())
    breaker(http, ecs)
    r, summary = run_verify(http, ecs)
    assert r.returncode == 1, r.stdout + r.stderr
    assert f"### {APP} post-deploy verify: FAIL" in summary
    assert _failed_rows(summary) == red_rows, summary
    assert "::error::verify [" in r.stdout


@needs_tools
def test_desired_zero_says_no_scale_up(run_verify: Run) -> None:
    http, ecs = copy.deepcopy(_good())
    _desired_zero(http, ecs)
    r, _ = run_verify(http, ecs)
    assert "desired count 0 (scaling Dev up is a separate approved step)" in r.stdout


@needs_tools
def test_stale_expected_sha_is_red_on_the_sha_rows(run_verify: Run) -> None:
    """Live proof 2 in miniature: Dev runs SHA, the dispatch expects OLD_SHA."""
    r, summary = run_verify(*_good(), EXPECTED_SHA=OLD_SHA, EXPECTED_IMAGE=IMAGE.replace(SHA, OLD_SHA))
    assert r.returncode == 1
    assert _failed_rows(summary) == {"health", "ecs-image"}, summary


@needs_tools
def test_a_newer_required_schema_is_red(run_verify: Run) -> None:
    r, summary = run_verify(*_good(), REQUIRED_SCHEMA=f"{PREFIX}-028")
    assert r.returncode == 1
    assert _failed_rows(summary) == {"health-ready"}, summary


@needs_tools
@pytest.mark.parametrize(
    "over",
    [
        {"EXPECTED_SHA": ""},
        {"EXPECTED_SHA": SHA[:12]},
        {"REQUIRED_SCHEMA": ""},
        {"REQUIRED_SCHEMA": "26"},
        {"EXPECTED_VERSION": ""},
        {"EXPECTED_IMAGE": ""},
        {"EXPECTED_IMAGE": IMAGE.replace(SHA, OLD_SHA)},
        {"EXPECTED_IMAGE": f"registry.example.invalid/{ECS_SVC}:dev-{SHA}"},
        {"ECS_CLUSTER": ""},
        {"ECS_SERVICE": ""},
        {"AWS_REGION": ""},
        {"BASE": ""},
    ],
)
def test_bad_inputs_are_red_and_nothing_is_reported_green(run_verify: Run, over: dict[str, str]) -> None:
    r, summary = run_verify(*_good(), **over)
    assert r.returncode == 1, r.stdout
    assert "| checks | **FAIL** | not run" in summary
    assert "| health | PASS |" not in summary


@needs_tools
@pytest.mark.parametrize("args", [(), ("../x",)], ids=["no-app", "bad-app"])
def test_the_app_argument_is_required(tmp_path: Path, args: tuple[str, ...]) -> None:
    proc = subprocess.run(  # noqa: S603
        [str(BASH), str(SCRIPT), *args], env={"PATH": os.environ["PATH"]}, capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 2
    assert "usage: post_deploy_verify.sh <app>" in proc.stdout


@needs_tools
def test_an_empty_rows_file_is_red(run_verify: Run, tmp_path: Path) -> None:
    rows = tmp_path / "empty.rows"
    rows.write_text("# nothing\n")
    r, summary = run_verify(*_good(), ROWS_FILE=str(rows))
    assert r.returncode == 1
    assert _failed_rows(summary) == {"rows"}


@needs_tools
def test_a_non_get_or_non_public_row_is_red(run_verify: Run, tmp_path: Path) -> None:
    rows = tmp_path / "bad.rows"
    rows.write_text(f"post|public|POST {HEALTH}|200|\nadmin|admin|GET {HEALTH}|200|\n")
    r, summary = run_verify(*_good(), ROWS_FILE=str(rows))
    assert r.returncode == 1
    assert _failed_rows(summary) == {"post", "admin"}


def test_rows_file_is_get_only_and_public() -> None:
    rows = [ln for ln in ROWS.read_text().splitlines() if ln.strip() and not ln.startswith("#")]
    names = set()
    for ln in rows:
        name, auth, req, want, _expr = ln.split("|", 4)
        method, path = req.split(" ", 1)
        assert auth == "public" and method == "GET" and path.startswith("/booking/"), ln
        assert want.isdigit(), ln
        assert name not in names, f"duplicate row name {name}"
        names.add(name)
    assert {"health", "health-ready"} <= names


# ── the required schema the workflow reads from the deployed commit ──────────────────────────────────────────────


def _required_from_source() -> str:
    tree = ast.parse(DB_PY.read_text())
    for node in tree.body:
        if isinstance(node, ast.AnnAssign | ast.Assign):
            targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
            if any(isinstance(t, ast.Name) and t.id == "REQUIRED_SCHEMA_VERSIONS" for t in targets):
                keys = ast.literal_eval(node.value)
                return max(keys, key=lambda k: int(k.split("-")[1]))
    raise AssertionError("REQUIRED_SCHEMA_VERSIONS not found")


@needs_tools
def test_the_workflow_extracts_the_newest_required_schema_key() -> None:
    text = VERIFY_WF.read_text()
    fn = re.search(r"^\s*(required_schema\(\) \{.*\})\s*$", text, re.M)
    assert fn, "required_schema() must stay a one-line function"
    proc = subprocess.run(  # noqa: S603
        [str(BASH), "-c", f"set -euo pipefail; {fn.group(1)}; required_schema < {DB_PY}"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == _required_from_source()
    assert re.fullmatch(rf"{PREFIX}-\d+", proc.stdout.strip())
    assert f'git show "${{EXPECTED_SHA}}:{DB_PY.relative_to(REPO)}" | required_schema' in text


# ── workflow wiring (text) ─────────────────────────────────────────────────────────────────────────────────────


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


def test_verify_workflow_is_callable_and_dispatchable_with_expected_sha() -> None:
    text = VERIFY_WF.read_text()
    on = _block(text, "on:", 0)
    call, dispatch = _block(on, "workflow_call:", 2), _block(on, "workflow_dispatch:", 2)
    for trigger in (call, dispatch):
        assert re.search(r"expected_sha:\n.*\n\s+required: true", trigger)
    assert "from_deploy:" in call and "from_deploy" not in dispatch
    assert "merge_group" not in on and "push:" not in on and "pull_request" not in on
    assert not re.search(r"^concurrency:", text, re.M), "workflow-level concurrency would be shared with every caller"
    job = _block(text, "verify:", 2)
    assert "    environment: dev\n" in job
    group = re.search(r"^\s+group: (.+)$", job, re.M)
    assert group
    assert f"'post-deploy-verify-{APP}'" in group.group(1)
    assert "inputs.from_deploy" in group.group(1) and "github.run_id" in group.group(1)
    assert "'deploy-dev'" not in group.group(1)
    assert "cancel-in-progress: false" in job
    assert f"bash .github/scripts/post_deploy_verify.sh {APP}\n" in text
    # The expected image is what the build pushes: <registry>/<repo>:<sha>, no prefix on the tag.
    assert 'EXPECTED_IMAGE="${REGISTRY}/${ECR_REPOSITORY}:${EXPECTED_SHA}"' in text
    assert "REGISTRY: ${{ steps.ecr.outputs.registry }}" in text
    assert "secrets." not in text, "Booking/Partner deploy configuration is variables only"


def test_deploy_dev_calls_verify_after_deploy_and_result_needs_every_job() -> None:
    text = DEPLOY_WF.read_text()
    jobs = _block(text, "jobs:", 0)
    names = re.findall(r"^  ([a-z][a-z-]*):$", jobs, re.M)
    assert names == [*DEPLOY_JOBS, "result"]
    verify = _block(jobs, "verify:", 2)
    assert "    uses: ./.github/workflows/post-deploy-verify.yml" in verify
    assert "    needs: deploy\n" in verify
    assert "      expected_sha: ${{ github.sha }}" in verify and "      from_deploy: true" in verify
    result = _block(jobs, "result:", 2)
    assert "    if: always()" in result
    assert f"    needs: [{', '.join(DEPLOY_JOBS)}]" in result
    # No job is conditional any more: a skip always means something did not run (and `result` says so).
    for name in DEPLOY_JOBS:
        assert not re.search(r"^    if:", _block(jobs, f"{name}:", 2), re.M), name
    # The post-deploy steps left the deploy job; the smoke can no longer skip green at desired count 0.
    deploy = _block(jobs, "deploy:", 2)
    assert "Verify the running image" not in deploy and "Smoke test" not in deploy
    assert "smoke skipped" not in text and "DESIRED_COUNT" not in text
    assert deploy.rstrip().endswith('aws ecs wait services-stable --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE"')


@needs_tools
@pytest.mark.parametrize(("role", "code"), [("", 1), ("arn-from-the-dev-environment", 0)], ids=["unset", "set"])
def test_config_fails_when_aws_role_arn_is_unset(role: str, code: int) -> None:
    config = _block(_block(DEPLOY_WF.read_text(), "jobs:", 0), "config:", 2)
    assert not re.search(r"^    outputs:", config, re.M) and "GITHUB_OUTPUT" not in config
    proc = subprocess.run(  # noqa: S603
        [str(BASH), "-c", _run_script(config)],
        env={"PATH": os.environ["PATH"], "ROLE_ARN": role},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == code, proc.stdout
    if code:
        assert "::error::Variable AWS_ROLE_ARN is not set" in proc.stdout


def test_deploy_dev_keeps_its_trigger_guard_and_concurrency() -> None:
    text = DEPLOY_WF.read_text()
    on = _block(text, "on:", 0)
    assert "  push:\n    branches: [main]" in on and "override_running_jobs:" in on
    assert "merge_group" not in on
    assert "concurrency:\n  group: deploy-dev\n  cancel-in-progress: false" in text


def _result_step(needs: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    script = _run_script(_block(_block(DEPLOY_WF.read_text(), "jobs:", 0), "result:", 2))
    env = {"PATH": os.environ["PATH"], "NEEDS": json.dumps(needs), "EVENT": "push"}
    return subprocess.run(  # noqa: S603
        [str(BASH), "-c", script], env=env, capture_output=True, text=True, timeout=60
    )


def _needs(**over: str) -> dict[str, Any]:
    base = {j: {"result": "success", "outputs": {}} for j in DEPLOY_JOBS}
    for job, res in over.items():
        base[job]["result"] = res
    return base


@needs_tools
def test_result_is_green_only_when_every_job_succeeded() -> None:
    r = _result_step(_needs())
    assert r.returncode == 0, r.stdout + r.stderr
    assert "deployed and verified" in r.stdout


@needs_tools
@pytest.mark.parametrize(
    "over",
    [
        {"verify": "skipped"},
        {"verify": "failure"},
        {"verify": "cancelled"},
        {"deploy": "failure", "verify": "skipped"},
        {"preflight": "failure", "deploy": "skipped", "verify": "skipped"},
        # AWS_ROLE_ARN unset on a push: config fails, everything after it skips.
        {"config": "failure", "preflight": "skipped", "deploy": "skipped", "verify": "skipped"},
        {"config": "skipped", "preflight": "skipped", "deploy": "skipped", "verify": "skipped"},
    ],
)
def test_result_is_red_when_any_job_did_not_succeed(over: dict[str, str]) -> None:
    r = _result_step(_needs(**over))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "::error::Deploy Dev (push) is not verified" in r.stdout


@needs_tools
def test_result_is_red_when_a_job_result_is_missing() -> None:
    needs = _needs()
    del needs["verify"]
    assert _result_step(needs).returncode == 1
