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
DEPLOY_JOBS = ("config", "schema-gate", "preflight", "deploy", "verify")  # AA-993 M4b: schema-gate

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
if args[:2] == ["ecs", "describe-services"] and os.environ.get("FAKE_AWS_ERR"):
    sys.stderr.write(os.environ["FAKE_AWS_ERR"])
    sys.exit(255)
elif args[:2] == ["ecs", "describe-services"]:
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
def test_aws_error_text_is_scrubbed_before_the_row_and_summary(run_verify: Run) -> None:
    """Actions logs and summaries are public: an AWS error never prints an ARN or an account ID."""
    err = (
        "An error occurred (AccessDeniedException) when calling the DescribeServices operation: User: "
        "arn:aws:sts::123456789012:assumed-role/x/y is not authorized (account 123456789012)"
    )
    r, summary = run_verify(*_good(), FAKE_AWS_ERR=err)
    assert r.returncode == 1
    assert {"ecs-rollout", "ecs-image"} <= _failed_rows(summary), summary
    assert "describe-services failed: An error occurred (AccessDeniedException)" in summary
    for leak in ("123456789012", "arn:aws"):
        assert leak not in r.stdout + r.stderr, leak
        assert leak not in summary, leak


@needs_tools
@pytest.mark.parametrize("image", [IMAGE, "registry.example.invalid/repo:not-a-sha-SENTINEL"], ids=["image", "bad-tag"])
def test_an_invalid_expected_sha_is_red_and_never_printed(run_verify: Run, image: str) -> None:
    r, summary = run_verify(*_good(), EXPECTED_SHA="not-a-sha-SENTINEL", EXPECTED_IMAGE=image)
    assert r.returncode == 1
    assert (
        "| expected-sha | **FAIL** | EXPECTED_SHA must be a full 40-character commit SHA (value not printed)" in summary
    )
    assert "Expected commit `invalid`" in summary
    assert "SENTINEL" not in r.stdout + r.stderr + summary


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
    assert names == [*DEPLOY_JOBS, "rollback", "report", "result"]  # AA-1193: rollback + report (conditional)
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
        # AA-993 M4b: the schema this commit needs is not on Dev; nothing after the gate runs.
        {"schema-gate": "failure", "preflight": "skipped", "deploy": "skipped", "verify": "skipped"},
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


# ── AA-1193: rollback after a red verify, alert, drill ────────────────────────────────────────────────────────────

GUARD = {"booking": "booking-deploy-preflight.sh", "crm": "partner-deploy-preflight.sh"}[APP]
NEVER_IN_ROLLBACK = ("gh workflow", "docker push", "register-task-definition", "git " + "push")


def _step(job: str, name: str) -> str:
    """The lines of the step called ``name`` in a job block (up to the next step or key at its indent)."""
    lines = job.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.strip() == f"- name: {name}")
    indent = len(lines[start]) - len(lines[start].lstrip())
    out = [lines[start]]
    for ln in lines[start + 1 :]:
        if ln.strip() and len(ln) - len(ln.lstrip()) <= indent and not ln.lstrip().startswith("#"):
            break
        out.append(ln)
    return "\n".join(out)


def _jobs() -> str:
    return _block(DEPLOY_WF.read_text(), "jobs:", 0)


def test_aa1193_drill_input_reaches_the_verify() -> None:
    on = _block(DEPLOY_WF.read_text(), "on:", 0)
    drill = _block(_block(on, "workflow_dispatch:", 2), "drill_force_smoke_fail:", 6)
    assert "type: boolean" in drill and "default: false" in drill and "required: false" in drill
    verify = _block(_jobs(), "verify:", 2)
    assert "      drill_force_fail: ${{ inputs.drill_force_smoke_fail == true }}" in verify


def test_aa1193_previous_revision_is_recorded_before_update_service() -> None:
    deploy = _block(_jobs(), "deploy:", 2)
    assert "      previous_td: ${{ steps.before.outputs.previous_td }}" in _block(deploy, "outputs:", 4)
    record = deploy.index("- name: Record the revision before this deploy (rollback target)")
    assert record < deploy.index("- name: Register task definition and update service")
    run = _run_script(_step(deploy, "Record the revision before this deploy (rollback target)"))
    assert 'ecs_smoke_rollback.py previous --cluster "$ECS_CLUSTER" --service "$ECS_SERVICE"' in run
    assert '"previous_tag=${GITHUB_SHA}"' in run, "the drill refuses when Dev already runs this commit"


def test_aa1193_rollback_runs_after_a_failed_verify_behind_the_deploy_gate() -> None:
    job = _block(_jobs(), "rollback:", 2)
    assert "    needs: [deploy, verify]" in job
    cond = " ".join(_block(job, "if: >-", 4).split())
    for part in (
        "always()",
        "github.ref == 'refs/heads/main'",
        "needs.deploy.result == 'success'",
        "needs.verify.result == 'failure'",
    ):
        assert part in cond, part
    assert "    environment: dev" in job
    step = _step(job, "Roll back to the revision before this deploy")
    run = _run_script(step)
    assert f"bash .github/scripts/{GUARD}" in run
    assert run.index(GUARD) < run.index("ecs_api_rollback.py rollback")
    assert '--target "${ECS_SERVICE},,${PREVIOUS_TD}"' in run and "--apply" in run
    assert f'--health-url "${{BASE_URL%/}}{HEALTH}"' in run
    assert "OVERRIDE: ${{ inputs.override_running_jobs }}" in step
    assert "PREVIOUS_TD: ${{ needs.deploy.outputs.previous_td }}" in step
    alert = _step(job, "Alert the ops topic")
    assert "if: always()" in alert and "ecs_api_rollback.py alert" in alert and "--apply" in alert
    assert "FAILED_CHECKS: ${{ needs.verify.outputs.failed_checks }}" in alert
    for forbidden in NEVER_IN_ROLLBACK:
        assert forbidden not in job, f"the rollback must never start a deploy: {forbidden}"
    report = _block(_jobs(), "report:", 2)
    assert "    needs: [deploy, verify, rollback]" in report and "issues: write" in report
    assert "deploy-smoke-red" in report


def _rollback_shell(
    tmp_path: Path, guard_rc: int, guard_msg: str, outcome: str
) -> tuple[subprocess.CompletedProcess[str], str]:
    scripts = tmp_path / ".github" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / GUARD).write_text(f"echo '{guard_msg}'\nexit {guard_rc}\n")
    (scripts / "ecs_api_rollback.py").write_text(
        f"import sys\nsys.stderr.write('called ' + ' '.join(sys.argv[1:]) + '\\n')\nprint({outcome!r})\n"
    )
    out = tmp_path / "out.txt"
    run = _run_script(_step(_block(_jobs(), "rollback:", 2), "Roll back to the revision before this deploy"))
    env = {
        "PATH": os.environ["PATH"],
        "GITHUB_OUTPUT": str(out),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
        "ECS_CLUSTER": "cluster-x",
        "ECS_SERVICE": ECS_SVC,
        "ECR_REPOSITORY": "repo-x",
        "GITHUB_SHA": SHA,
        "PREVIOUS_TD": f"{ECS_SVC}:6",
        "BASE_URL": "http://api.test/",
        "OVERRIDE": "",
    }
    script = run.replace("/tmp/", f"{tmp_path}/")  # noqa: S108 (the step's scratch files go to this test's tmp_path)
    proc = subprocess.run(  # noqa: S603
        [str(BASH), "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc, out.read_text() if out.exists() else ""


@needs_tools
def test_aa1193_a_held_gate_rolls_nothing_back(tmp_path: Path) -> None:
    proc, out = _rollback_shell(tmp_path, 1, "::error::Deploy blocked: a retention job is running", "outcome=unused")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "called" not in proc.stdout
    assert "outcome=not rolled back (held by the deploy gate: a retention job is running)" in out


@needs_tools
def test_aa1193_an_open_gate_runs_the_rollback_with_the_recorded_target(tmp_path: Path) -> None:
    proc, out = _rollback_shell(tmp_path, 0, "safe", f"outcome=rolled back to {ECS_SVC}:6 (/health on {OLD_SHA[:12]})")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"--target {ECS_SVC},,{ECS_SVC}:6" in proc.stdout and f"--deployed-tag {SHA}" in proc.stdout
    assert f"--health-url http://api.test{HEALTH}" in proc.stdout
    assert f"outcome=rolled back to {ECS_SVC}:6" in out


def test_aa1193_verify_workflow_has_the_drill_and_returns_the_failed_checks() -> None:
    text = VERIFY_WF.read_text()
    call = _block(_block(text, "on:", 0), "workflow_call:", 2)
    assert "drill_force_fail:" in call and "value: ${{ jobs.verify.outputs.failed_checks }}" in call
    assert "drill_force_fail" not in _block(_block(text, "on:", 0), "workflow_dispatch:", 2)
    job = _block(text, "verify:", 2)
    assert "      failed_checks: ${{ steps.failed.outputs.checks }}" in job
    drill = _step(job, "Drill — force the post-deploy verify red")
    assert "if: inputs.drill_force_fail" in drill and "exit 1" in drill
    assert job.index("- name: Verify Dev") < job.index("- name: Drill") < job.index("- name: Failing checks")
    assert "if: always()" in _step(job, "Failing checks")


@needs_tools
def test_aa1193_failing_checks_step_keeps_names_only(tmp_path: Path) -> None:
    run = _run_script(_step(_block(VERIFY_WF.read_text(), "verify:", 2), "Failing checks"))
    failed, out = tmp_path / "failed.txt", tmp_path / "out.txt"
    failed.write_text("health\nhealth-ready\nhealth\n$(touch pwned)\ndrill-forced-fail\n")
    env = {"PATH": os.environ["PATH"], "VERIFY_FAILED_FILE": str(failed), "GITHUB_OUTPUT": str(out)}
    proc = subprocess.run([str(BASH), "-c", run], env=env, capture_output=True, text=True, timeout=30)  # noqa: S603
    assert proc.returncode == 0, proc.stderr
    assert out.read_text() == "checks=health,health-ready,drill-forced-fail\n"


@needs_tools
def test_aa1193_failed_check_names_go_to_the_failed_file(run_verify: Run, tmp_path: Path) -> None:
    http, ecs = _good()
    _health_sha_mismatch(http, ecs)
    failed = tmp_path / "failed.txt"
    r, summary = run_verify(http, ecs, VERIFY_FAILED_FILE=str(failed))
    assert r.returncode == 1
    names = failed.read_text().split()
    assert "health" in names and set(names) == _failed_rows(summary)
