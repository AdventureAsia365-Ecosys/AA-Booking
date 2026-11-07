"""AA-1008 H3: the per-PR visual job is safe to be a REQUIRED check under the merge queue.

A required check must report on every pull request and on every merge-queue group (merge_group), under a name that
never changes (the tech lead's branch-protection script and verify-h3.sh pin it). So `.github/workflows/visual.yml`
has no `paths:` on pull_request, a merge_group trigger, the job `pr` keeps its name, is skipped on pull_request (S271:
reported "skipped" = passed) and runs on merge_group (the `deployed` job never does), and its Scope step matches the
old path list exactly (frontend/**, the workflow itself) and, on no match, skips every later step so the job ends
green. The skip is step-level: a job-level skip reports "skipped", which GitHub counts as passed even when the job it
depended on failed. The Scope step's script is run here with bash in a scratch git repository. The workflow is read as
text (no YAML parser in the backend dependencies), like test_vercel_deploy_smoke.py.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
WF = ROOT / ".github" / "workflows" / "visual.yml"
NAME = "Visual (next start, mocked API + kit gallery)"
SCOPED = "steps.scope.outputs.run == 'true'"
BASH = shutil.which("bash")
GIT = shutil.which("git")
needs_git = pytest.mark.skipif(not (BASH and GIT), reason="needs bash and git")


def _text() -> str:
    return WF.read_text()


def _on_block() -> str:
    text = _text()
    return text[text.index("\non:\n") : text.index("\npermissions:\n")]


def _pr_job() -> str:
    text = _text()
    return text[text.index("\n  pr:\n") : text.index("\n  deployed:\n")]


def _steps(job: str) -> list[tuple[str, str]]:
    """(name, block) per step of the job, the checkout first (named "checkout")."""
    body = job[job.index("    steps:\n") :]
    parts = re.split(r"^      - ", body, flags=re.M)[1:]
    out = []
    for part in parts:
        m = re.match(r"name: (.+)", part)
        out.append((m.group(1).strip() if m else "checkout", part))
    return out


def _step(name: str) -> str:
    return next(block for n, block in _steps(_pr_job()) if n == name)


def _run_script(block: str) -> str:
    m = re.compile(r"^        run: \|\n((?:          .*\n|\n)+)", re.M).search(block)
    assert m, "the step has a run block"
    return "\n".join(line[10:] for line in m.group(1).splitlines())


def _if(block: str) -> str:
    m = re.search(r"^        if: (.+)$", block, re.M)
    return m.group(1) if m else ""


# --- triggers, name, conditions -------------------------------------------------------------------------------------


def test_runs_on_every_pr_and_on_the_merge_queue() -> None:
    on = _on_block()
    pr = on[on.index("  pull_request:\n") : on.index("  merge_group:\n")]
    assert "paths" not in pr, "a required check must report on every PR: the Scope step decides"
    assert "branches: [main]" in pr
    assert "  merge_group:\n    types: [checks_requested]\n" in on


def test_the_pr_job_keeps_its_required_name_and_runs_on_merge_group() -> None:
    job = _pr_job()
    assert f"    name: {NAME}\n" in job
    cond = " ".join(job[job.index("    if: >-\n") : job.index("    runs-on:")].split())
    # S271 (CI minutes): skipped on pull_request (reported "skipped" = passed, so the PR can enter the queue); the queue
    # group runs it, so every frontend change is still checked before main.
    assert "'pull_request'" not in cond
    assert "github.event_name == 'merge_group'" in cond
    assert "\n    needs:" not in job, "a job-level dependency would turn a failure into a passing 'skipped'"


def test_the_deployed_job_never_runs_on_pr_or_merge_group() -> None:
    text = _text()
    deployed = text[text.index("\n  deployed:\n") :]
    cond = " ".join(deployed[deployed.index("    if: >-\n") : deployed.index("    runs-on:")].split())
    assert "github.ref == 'refs/heads/main'" in cond
    assert "github.event_name != 'pull_request'" in cond
    assert "github.event_name != 'merge_group'" in cond


def test_each_queue_group_gets_its_own_concurrency_group() -> None:
    m = re.search(r"^concurrency:\n  group: (.+)\n  cancel-in-progress: true$", _text(), re.M)
    assert m
    group = m.group(1)
    # A PR keys on its number; every other event (merge_group has no number and no base_url) on its run_id.
    assert group.startswith("visual-${{ github.event_name == 'pull_request' && github.event.pull_request.number ||")
    assert group.endswith("|| github.run_id }}")


def test_every_step_after_scope_is_scoped() -> None:
    steps = _steps(_pr_job())
    assert steps[0][0] == "checkout" and "fetch-depth: 0" in steps[0][1]
    assert steps[1][0] == "Scope" and "id: scope" in steps[1][1] and not _if(steps[1][1])
    # The job's default working directory is frontend/; the diff names paths from the repository root.
    assert "working-directory: ." in steps[1][1]
    for name, block in steps[2:]:
        assert SCOPED in _if(block), f"{name} must be guarded by the Scope step"
    for name in ("Summary", "Upload screenshots and report"):
        assert _if(_step(name)).startswith("always() && "), name
    # The PR comment went with the per-PR run (S271): nothing in the job writes to a PR any more.
    assert not any("comment" in name.lower() for name, _ in steps)
    assert "pull-requests: write" not in _text()


def test_changed_pages_diffs_a_queue_group_against_its_base() -> None:
    block = _step("Changed pages")
    assert "MERGE_BASE_SHA: ${{ github.event.merge_group.base_sha }}" in block
    script = _run_script(block)
    assert '"$EVENT" = "merge_group"' in script
    assert 'changed-pages.mjs "$MERGE_BASE_SHA" HEAD' in script


# --- the Scope step, run with bash ----------------------------------------------------------------------------------

GIT_CFG = ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false"]


def _g(repo: Path, *args: str) -> str:
    assert GIT
    return subprocess.run(  # noqa: S603
        [GIT, *GIT_CFG, "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _scratch_repo(tmp_path: Path, change: dict[str, str | None], rename: tuple[str, str] | None = None) -> str:
    """main with a few files; HEAD = main + ``change`` (path -> text, None = delete); returns main's sha."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _g(repo, "init", "-q", "-b", "main")
    for f in ("frontend/app/page.tsx", "backend/app/main.py", "migrations/bk-001.sql", "docs/readme.md"):
        (repo / f).parent.mkdir(parents=True, exist_ok=True)
        (repo / f).write_text(f"// {f}\n")
    _g(repo, "add", "-A")
    _g(repo, "commit", "-qm", "base")
    base = _g(repo, "rev-parse", "HEAD")
    _g(repo, "update-ref", "refs/remotes/origin/main", base)
    _g(repo, "checkout", "-qb", "feature")
    for f, text in change.items():
        if text is None:
            (repo / f).unlink()
        else:
            (repo / f).parent.mkdir(parents=True, exist_ok=True)
            (repo / f).write_text(text)
    if rename:
        (repo / rename[1]).parent.mkdir(parents=True, exist_ok=True)
        _g(repo, "mv", rename[0], rename[1])
    _g(repo, "add", "-A")
    _g(repo, "commit", "-qm", "change", "--allow-empty")
    return base


def _run_scope(tmp_path: Path, **env: str) -> tuple[subprocess.CompletedProcess[str], str, str]:
    assert BASH
    out, summary = tmp_path / "out.txt", tmp_path / "summary.md"
    full = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "GITHUB_OUTPUT": str(out),
        "GITHUB_STEP_SUMMARY": str(summary),
        "EVENT": "",
        "BASE_REF": "",
        "GROUP_BASE": "",
        **env,
    }
    # GitHub's default `run` shell is bash -e -o pipefail.
    res = subprocess.run(  # noqa: S603
        [BASH, "--noprofile", "--norc", "-eo", "pipefail", "-c", _run_script(_step("Scope"))],
        cwd=tmp_path / "repo",
        env=full,
        capture_output=True,
        text=True,
        check=False,
    )
    return (
        res,
        out.read_text() if out.exists() else "",
        summary.read_text() if summary.exists() else "",
    )


# The old pull_request `paths:` list: frontend/**, .github/workflows/visual.yml.
SCOPE_CASES: list[tuple[dict[str, str | None], bool]] = [
    ({"frontend/app/page.tsx": "changed\n"}, True),
    ({"frontend/package-lock.json": "{}\n"}, True),
    ({"frontend/app/page.tsx": None}, True),
    ({"frontend/e2e/visual/pages.ts": "x\n"}, True),
    ({".github/workflows/visual.yml": "x\n"}, True),
    ({"backend/app/main.py": "changed\n", "frontend/ui/core/x.tsx": "x\n"}, True),
    ({"backend/app/main.py": "changed\n"}, False),
    ({"migrations/bk-001.sql": "changed\n"}, False),
    ({"docs/readme.md": "changed\n"}, False),
    ({"frontendx/a.ts": "x\n"}, False),
    ({".github/workflows/ci.yml": "x\n"}, False),
    ({".github/workflows/deploy-admin-dev.yml": "x\n"}, False),
    ({".github/workflows/visual.yml.bak": "x\n"}, False),
]


@needs_git
@pytest.mark.parametrize(("change", "want"), SCOPE_CASES)
@pytest.mark.parametrize("event", ["pull_request", "merge_group"])
def test_scope_matches_the_old_path_filter(
    tmp_path: Path, change: dict[str, str | None], want: bool, event: str
) -> None:
    base = _scratch_repo(tmp_path, change)
    env = {"EVENT": event, "BASE_REF": "main"} if event == "pull_request" else {"EVENT": event, "GROUP_BASE": base}
    res, out, summary = _run_scope(tmp_path, **env)
    assert res.returncode == 0, res.stdout + res.stderr
    assert out.strip() == f"run={'true' if want else 'false'}"
    assert ("no frontend change" in summary) is (not want)


@needs_git
def test_scope_counts_a_rename_out_of_the_frontend(tmp_path: Path) -> None:
    _scratch_repo(tmp_path, {}, rename=("frontend/app/page.tsx", "docs/page.tsx"))
    res, out, _ = _run_scope(tmp_path, EVENT="pull_request", BASE_REF="main")
    assert res.returncode == 0 and out.strip() == "run=true"


@needs_git
@pytest.mark.parametrize(
    "env",
    [
        {"EVENT": "workflow_dispatch"},
        {"EVENT": "merge_group", "GROUP_BASE": ""},  # never "...HEAD" (an empty diff would skip silently)
        {"EVENT": "merge_group", "GROUP_BASE": "0" * 40},
        {"EVENT": "pull_request", "BASE_REF": ""},
        {"EVENT": "pull_request", "BASE_REF": "no-such-branch"},
    ],
)
def test_scope_runs_everything_when_it_cannot_decide(tmp_path: Path, env: dict[str, str]) -> None:
    _scratch_repo(tmp_path, {"docs/readme.md": "changed\n"})
    res, out, summary = _run_scope(tmp_path, **env)
    assert res.returncode == 0, res.stdout + res.stderr
    assert out.strip() == "run=true"
    assert "no frontend change" not in summary
    if env["EVENT"] != "workflow_dispatch":
        assert "::warning::" in res.stdout
