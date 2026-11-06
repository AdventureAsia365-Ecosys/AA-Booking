"""AA-1024 l22 (S271 lessons guard G6): the pre-push hook refuses a push while a rebase is in progress.

A push in the middle of a rebase sends only the commits replayed so far (lesson S257, CIS #1144). The hook
(``.githooks/pre-push``, installed by ``scripts/install-hooks.sh``) is run here in a throw-away repository."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
HOOK = REPO / ".githooks" / "pre-push"
GIT = shutil.which("git")
BASH = shutil.which("bash")


def _git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid"}
    env |= {"GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    return subprocess.run(  # noqa: S603 (fixed git argv in a temp repo)
        [GIT, *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


def _push_hook(repo: Path) -> subprocess.CompletedProcess:
    line = f"refs/heads/main {_git(repo, 'rev-parse', 'HEAD')} refs/heads/main {'0' * 40}\n"
    return subprocess.run(  # noqa: S603 (the repo's own hook in a temp repo)
        [BASH, ".githooks/pre-push", "origin", "x"], cwd=repo, input=line, capture_output=True, text=True, timeout=60
    )


@pytest.mark.skipif(GIT is None or BASH is None, reason="needs git and bash")
def test_hook_refuses_a_push_mid_rebase(tmp_path: Path) -> None:
    (tmp_path / ".githooks").mkdir()
    shutil.copy(HOOK, tmp_path / ".githooks" / "pre-push")
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    rebase_dir = Path(_git(tmp_path, "rev-parse", "--path-format=absolute", "--git-path", "rebase-merge"))
    rebase_dir.mkdir()
    p = _push_hook(tmp_path)
    assert p.returncode == 1 and "a rebase is in progress" in p.stderr
    rebase_dir.rmdir()
    # Without the rebase state the hook goes on to scripts/pre-push.sh (missing in this throw-away repo).
    p = _push_hook(tmp_path)
    assert "a rebase is in progress" not in p.stderr
