"""S294 (nightly Image Scan red on a Debian package fixed after the last deploy): every API image build reads the base
tag and the Debian archive fresh, so the next commit's image carries the fixed package.

The Dockerfile upgrades the installed Debian packages in its own layer; the Deploy Dev and CI builds pull the base tag
(``--pull``) and never reuse a layer cache (``--cache-from`` / buildx cache), so that layer is never a stale copy.
The workflows are checked as text, like ``test_release_version.py`` (no YAML parser in test deps)."""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
WORKFLOWS = REPO / ".github" / "workflows"
CACHE_FLAGS = ("--cache-from", "--cache-to", "cache-from:", "cache-to:", "type=gha", "docker/build-push-action")


def _build_commands(text: str) -> list[str]:
    """Each `docker build` command, joined over its backslash-continued lines."""
    commands = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.search(r"\bdocker build\b", line):
            command = line
            j = i
            while command.rstrip().endswith("\\") and j + 1 < len(lines):
                j += 1
                command = command.rstrip()[:-1] + " " + lines[j].strip()
            commands.append(command)
    return commands


def test_dockerfile_upgrades_the_installed_debian_packages() -> None:
    dockerfile = (REPO / "backend" / "Dockerfile").read_text()
    assert re.search(r"^RUN apt-get update && apt-get upgrade -y\b", dockerfile, re.M)


def test_api_image_builds_pull_the_base_and_use_no_layer_cache() -> None:
    for name in ("deploy-dev.yml", "ci.yml"):
        text = (WORKFLOWS / name).read_text()
        commands = [c for c in _build_commands(text) if "aa-booking-api" in c or '"$IMAGE" backend' in c]
        assert commands, f"{name}: no API image docker build found"
        for command in commands:
            assert "--pull" in command, f"{name}: API image build without --pull: {command.strip()}"
        for flag in CACHE_FLAGS:
            assert flag not in text, f"{name}: a layer cache ({flag}) can serve a stale apt upgrade layer"
