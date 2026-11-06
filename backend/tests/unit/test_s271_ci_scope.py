"""S271 (CI minutes, AA-889 merge queue): the pull-request scope of ci.yml and the events each job runs on.

- .github/scripts/ci_scope.py (same file as AA-CIS-App's and AA-Partner's): every output is true for any event but
  pull_request, when the diff cannot be read or is empty, and when the PR changes .github/; `tests` is false only for a
  docs-only PR; an area is true when a changed path is one of its files or under one of its folders.
- .github/workflows/ci.yml: no `push` run (the merge queue already tested the exact tree that lands on main), and the
  job conditions, evaluated for each event: merge_group and workflow_dispatch run every job; a pull request runs the
  fast jobs and the heavy ones only in their area; a failed scope (empty outputs) runs every gated job (fail closed).
  The workflow is read as text (no YAML parser in the backend dependencies), like test_release_version.py.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / ".github" / "scripts" / "ci_scope.py"
CI = REPO / ".github" / "workflows" / "ci.yml"
SCOPE_JOB = "lint"
AREAS = {
    "integration": ["migrations/", "backend/tests/integration/"],
    "docker": ["backend/Dockerfile", "backend/.dockerignore", "backend/requirements.txt", ".trivyignore"],
    "frontend": ["frontend/", "backend/scripts/check_kit_provenance.py"],
}
JOBS = [
    "lint",
    "security-audit",
    "test-unit",
    "test-integration",
    "docker-build",
    "vendored-copies",
    "migration-preflight",
    "frontend-build",
]
REQUIRED = ["Lint", "Security Audit", "Unit Tests", "Integration Tests", "Docker Build Check", "Frontend Build"]
HEAVY = ["test-integration", "docker-build"]

_spec = importlib.util.spec_from_file_location("ci_scope", SCRIPT)
assert _spec is not None and _spec.loader is not None
ci_scope = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ci_scope)


# --- ci_scope.py --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("event", ["merge_group", "workflow_dispatch", "push", ""])
def test_every_output_is_true_outside_pull_requests(event: str) -> None:
    out, _ = ci_scope.decide(event, ["docs/a.md"], AREAS)
    assert all(out.values()) and set(out) == {"tests", *AREAS}


@pytest.mark.parametrize("files", [None, [], ["docs/a.md", ".github/workflows/ci.yml"], [".github/scripts/x.py"]])
def test_a_pull_request_runs_everything_when_unsure_or_ci_changes(files: list[str] | None) -> None:
    out, _ = ci_scope.decide("pull_request", files, AREAS)
    assert all(out.values()), out


@pytest.mark.parametrize(
    ("files", "on"),
    [
        (["docs/guide.md", "README.md", "frontend/README.md"], set()),
        (["backend/app/main.py"], {"tests"}),
        (["migrations/050_x.sql"], {"tests", "integration"}),
        (["backend/tests/integration/test_x.py"], {"tests", "integration"}),
        (["backend/requirements.txt"], {"tests", "docker"}),
        (["frontend/app/page.tsx"], {"tests", "frontend"}),
        (["backend/scripts/check_kit_provenance.py"], {"tests", "frontend"}),
        (["backend/requirements-dev.txt", "backend/Dockerfile.old", "frontendx/a.ts", "migrations_tool.py"], {"tests"}),
    ],
)
def test_pull_request_outputs(files: list[str], on: set[str]) -> None:
    out, _ = ci_scope.decide("pull_request", files, AREAS)
    assert {k for k, v in out.items() if v} == on


@pytest.mark.parametrize("spec", ["frontend", "=x/", "tests=docs/", "bad name=x/", "frontend="])
def test_area_spec_refusals(spec: str) -> None:
    with pytest.raises(ci_scope.argparse.ArgumentTypeError, match="--area must be"):
        ci_scope.parse_area(spec)


GIT_CFG = ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false"]


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
@pytest.mark.parametrize(("change", "want"), [("docs/new.md", ["tests=false", "frontend=false"]),
                                             ("frontend/app/page.tsx", ["tests=true", "frontend=true"])])  # fmt: skip
def test_the_cli_writes_github_output_from_the_merge_commit_diff(tmp_path: Path, change: str, want: list[str]) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> None:
        subprocess.run(["git", *GIT_CFG, "-C", str(repo), *args], check=True, capture_output=True)  # noqa: S603, S607

    git("init", "-q")
    (repo / "README.md").write_text("x\n")
    git("add", "-A")
    git("commit", "-qm", "base")
    (repo / change).parent.mkdir(parents=True, exist_ok=True)
    (repo / change).write_text("y\n")
    git("add", "-A")
    git("commit", "-qm", "change")  # HEAD^1 = the base, as the first parent of GitHub's merge commit
    out = tmp_path / "out.txt"
    res = subprocess.run(  # noqa: S603 (our own script, fixed arguments)
        [sys.executable, str(SCRIPT), "--event", "pull_request", "--area", "frontend=frontend/"],
        cwd=repo, env={**os.environ, "GITHUB_OUTPUT": str(out)}, capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert res.returncode == 0, res.stderr
    assert out.read_text().splitlines() == want


def test_the_cli_runs_everything_when_git_fails(tmp_path: Path) -> None:
    out = tmp_path / "out.txt"
    res = subprocess.run(  # noqa: S603 (our own script, fixed arguments)
        [sys.executable, str(SCRIPT), "--event", "pull_request", "--base", "no-such-rev",
         "--area", "frontend=frontend/"],
        cwd=tmp_path, env={**os.environ, "GITHUB_OUTPUT": str(out)}, capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert res.returncode == 0 and "::warning::" in res.stdout
    assert out.read_text().splitlines() == ["tests=true", "frontend=true"]


# --- ci.yml: triggers and job conditions (read as text) -------------------------------------------------------------


def _text() -> str:
    return CI.read_text(encoding="utf-8")


def _job(name: str) -> str:
    body = _text().split(f"\n  {name}:\n", 1)[1]
    return re.split(r"\n  [a-z][a-z-]*:\n", body, maxsplit=1)[0]


def _if(job: str) -> str | None:
    m = re.search(r"^    if: (.+)$", job, re.M)
    return m.group(1) if m else None


def _eval(cond: str | None, event: str, outputs: dict[str, str]) -> bool:
    """Evaluate the small expression subset ci.yml uses (==, !=, &&, ||, !cancelled(), parentheses)."""
    if cond is None:
        return True
    expr = cond.strip()
    if expr.startswith("${{"):
        expr = expr[3:-2]
    expr = expr.replace("!cancelled()", "True").replace("&&", " and ").replace("||", " or ")
    expr = expr.replace("github.event_name", repr(event))
    expr = re.sub(rf"needs\.{SCOPE_JOB}\.outputs\.(\w+)", lambda m: repr(outputs.get(m.group(1), "")), expr)
    assert re.fullmatch(r"[\sA-Za-z_'()!=]*", expr.replace("True", "")), expr
    return bool(eval(expr, {"__builtins__": {}}, {}))  # noqa: S307 - literals and operators only (asserted above)


def _runs(event: str, outputs: dict[str, str]) -> dict[str, bool]:
    return {j: _eval(_if(_job(j)), event, outputs) for j in JOBS}


def _scope(event: str, files: list[str] | None) -> dict[str, str]:
    out, _ = ci_scope.decide(event, files, AREAS)
    return {k: "true" if v else "false" for k, v in out.items()}


def test_the_job_list_is_complete() -> None:
    assert re.findall(r"^  ([a-z][a-z-]*):\n", _text().split("\njobs:\n", 1)[1], re.M) == JOBS


def test_no_push_run_the_merge_queue_already_tested_main() -> None:
    on = _text().split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
    triggers = re.findall(r"^  ([a-z_]+):", on, re.M)
    assert triggers == ["pull_request", "merge_group", "workflow_dispatch"]
    assert "paths" not in on


@pytest.mark.parametrize("event", ["merge_group", "workflow_dispatch"])
def test_the_queue_and_a_dispatch_run_every_job(event: str) -> None:
    runs = _runs(event, _scope(event, None))
    off = {"migration-preflight"} if event == "workflow_dispatch" else set()
    assert {j for j, r in runs.items() if not r} == off


def test_a_failed_scope_runs_every_gated_job_on_a_pull_request() -> None:
    assert all(_runs("pull_request", {}).values())


def test_a_backend_pull_request_runs_the_fast_jobs_only() -> None:
    runs = _runs("pull_request", _scope("pull_request", ["backend/app/main.py"]))
    assert {j for j, r in runs.items() if not r} == {*HEAVY, "frontend-build"}


def test_a_migration_pull_request_also_runs_the_integration_tests() -> None:
    runs = _runs("pull_request", _scope("pull_request", ["migrations/050_x.sql"]))
    assert runs["test-integration"] and not runs["docker-build"]


def test_a_docs_only_pull_request_runs_no_test_job() -> None:
    runs = _runs("pull_request", _scope("pull_request", ["docs/a.md"]))
    assert {j for j, r in runs.items() if r} == {"lint", "security-audit", "vendored-copies", "migration-preflight"}


def test_heavy_jobs_never_run_on_a_plain_pull_request() -> None:
    for files in (["backend/app/main.py"], ["frontend/app/page.tsx"], ["docs/a.md"]):
        runs = _runs("pull_request", _scope("pull_request", files))
        assert not any(runs[j] for j in HEAVY), files


def test_every_gate_is_fail_closed_and_names_a_declared_output() -> None:
    lint = _job(SCOPE_JOB)
    for name in ("tests", *AREAS):
        assert f"      {name}: ${{{{ steps.scope.outputs.{name} }}}}\n" in lint, name
    scope = lint.split("- name: Scope (what this pull request needs)\n", 1)[1].split("\n      - ", 1)[0]
    assert "id: scope" in scope and "continue-on-error: true" in scope and "ci_scope.py" in scope
    for name, prefixes in AREAS.items():
        assert f"--area {name}={','.join(prefixes)}" in scope, name
    for j in JOBS:
        cond = _if(_job(j)) or ""
        for out in re.findall(rf"needs\.{SCOPE_JOB}\.outputs\.(\w+)", cond):
            assert out in {"tests", *AREAS}, (j, out)
            assert f"needs.{SCOPE_JOB}.outputs.{out} != 'false'" in cond, (j, "must test != 'false'")
            assert cond.startswith("${{ !cancelled() && "), j
            assert re.search(rf"^    needs: (\[.*\b{SCOPE_JOB}\b.*\]|{SCOPE_JOB})$", _job(j), re.M), j


def test_required_checks_keep_their_job_names() -> None:
    names = set(re.findall(r"^    name: (.+)$", _text(), re.M))
    assert set(REQUIRED) <= names


def test_frontend_build_skips_only_next_build_on_a_pull_request() -> None:
    fe = _job("frontend-build")
    build = fe.split("- name: Build (next build)\n", 1)[1].split("\n      - ", 1)[0]
    assert "if: github.event_name != 'pull_request'" in build
    for step in ("- name: Typecheck (tsc --noEmit)\n", "- name: Unit tests (node --test)\n"):
        assert "if:" not in fe.split(step, 1)[1].split("\n      - ", 1)[0], step


def test_the_vendored_copies_check_stays_a_job_of_its_own() -> None:
    """It compares with CIS main, which a PR here does not control: inside the required Lint a CIS change would block
    every PR of this repo. It runs on every event (a few seconds)."""
    job = _job("vendored-copies")
    assert "    name: Vendored CIS copies\n" in job and _if(job) is None and "\n    needs:" not in job
    assert "python3 scripts/check_vendored_copies.py" not in _job("lint")
