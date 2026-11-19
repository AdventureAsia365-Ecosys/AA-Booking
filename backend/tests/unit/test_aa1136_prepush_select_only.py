"""AA-1136 (agent change hooks, app-repo slice): ``scripts/pre-push.sh --select-only``.

It prints the unit test files fast mode would pick for the changes since the merge base, committed or not, as
``unit backend/tests/unit/<file>`` lines, and exits 0. It runs no tool and writes nothing (S284 hooks advisory 4.3: a
pre-push step never writes files). Run here on a throw-away repository with a copy of the script."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "pre-push.sh"
GIT = shutil.which("git")
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(GIT is None or BASH is None, reason="needs git and bash")


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env |= {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid"}
    return env | {"GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603 (fixed git argv in a temp repo)
        [GIT, *args], cwd=cwd, env=_env(), check=True, capture_output=True, text=True
    ).stdout


def _select(repo: Path, base: str) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 (the repo's own script in a temp repo)
        [BASH, "scripts/pre-push.sh", "--select-only"],
        cwd=repo,
        env=_env() | {"AA_PREPUSH_BASE": base},
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "scripts").mkdir()
    shutil.copy(SCRIPT, tmp_path / "scripts" / "pre-push.sh")
    (tmp_path / "backend" / "app").mkdir(parents=True)
    (tmp_path / "backend" / "tests" / "unit").mkdir(parents=True)
    for name in ("lantern", "harbor", "orchard", "meadow"):
        (tmp_path / "backend" / "app" / f"{name}.py").write_text("")
        (tmp_path / "backend" / "tests" / "unit" / f"test_{name}.py").write_text("")
    (tmp_path / ".gitignore").write_text("scratch_*.py\n")
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    _git(tmp_path, "switch", "-qc", "feat/lane")
    return tmp_path


def test_select_only_lists_committed_staged_unstaged_and_untracked_changes(repo: Path) -> None:
    (repo / "backend" / "app" / "lantern.py").write_text("x = 1\n")
    _git(repo, "commit", "-qam", "committed")
    (repo / "backend" / "app" / "harbor.py").write_text("y = 1\n")
    _git(repo, "add", "backend/app/harbor.py")
    (repo / "backend" / "app" / "orchard.py").write_text("z = 1\n")
    (repo / "backend" / "app" / "scratch_ignored.py").write_text("")
    status_before = _git(repo, "status", "--porcelain", "--untracked-files=all")

    p = _select(repo, "main")

    assert p.returncode == 0, p.stderr
    assert p.stdout.splitlines() == [
        "unit backend/tests/unit/test_harbor.py",
        "unit backend/tests/unit/test_lantern.py",
        "unit backend/tests/unit/test_orchard.py",
    ]
    assert _git(repo, "status", "--porcelain", "--untracked-files=all") == status_before


def test_select_only_with_a_missing_base_is_a_note_and_exit_0(repo: Path) -> None:
    p = _select(repo, "no-such-ref")
    assert p.returncode == 0
    assert p.stdout.strip() == "note no-such-ref not found (git fetch origin?): nothing selected"
