"""AA-891: the release version (release-please manifest) reaches ``/booking/health``, and a release commit does not
deploy. The workflows are checked as text, like ``test_deploy_preflight_script.py`` (no YAML parser in test deps)."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from app.settings import load_settings

REPO = Path(__file__).resolve().parents[3]
MANIFEST = REPO / ".release-please-manifest.json"
CONFIG = REPO / "release-please-config.json"
WORKFLOWS = REPO / ".github" / "workflows"
READ_MANIFEST = """jq -r '."."' """


def manifest_version() -> str:
    return json.loads(MANIFEST.read_text())["."]


def _push_paths(workflow: str) -> list[str]:
    text = (WORKFLOWS / workflow).read_text()
    block = text[text.index("  push:\n") : text.index("  workflow_dispatch:")]
    return re.findall(r'^\s+- "([^"]+)"$', block, flags=re.MULTILINE)


# ── version on /booking/health ───────────────────────────────────────────────


def test_health_reports_the_manifest_version_baked_as_app_version() -> None:
    version = manifest_version()
    settings = load_settings({"GIT_SHA": "abc1234", "APP_VERSION": version})
    with TestClient(create_app(settings)) as client:
        body = client.get("/booking/health").json()
    assert body["version"] == version
    assert body["git_sha"] == "abc1234"


def test_version_defaults_to_unknown_outside_an_image() -> None:
    assert load_settings({}).app_version == "unknown"


def test_manifest_holds_one_semver_and_matches_pyproject() -> None:
    version = manifest_version()
    assert re.fullmatch(r"\d+\.\d+\.\d+", version)
    pyproject = tomllib.loads((REPO / "backend" / "pyproject.toml").read_text())
    assert pyproject["project"]["version"] == version


def test_dockerfile_and_builds_bake_the_manifest_version() -> None:
    dockerfile = (REPO / "backend" / "Dockerfile").read_text()
    assert "ARG APP_VERSION=unknown" in dockerfile
    assert "APP_VERSION=${APP_VERSION}" in dockerfile
    deploy = (WORKFLOWS / "deploy-dev.yml").read_text()
    assert READ_MANIFEST + ".release-please-manifest.json" in deploy
    assert '--build-arg APP_VERSION="${APP_VERSION}"' in deploy
    assert ".version == $ver" in deploy  # the live smoke test checks it
    ci = (WORKFLOWS / "ci.yml").read_text()
    assert READ_MANIFEST + "../.release-please-manifest.json" in ci
    assert ".version == $ver" in ci


# ── release-please configuration ─────────────────────────────────────────────


def test_release_please_config() -> None:
    config = json.loads(CONFIG.read_text())
    assert config["release-type"] == "simple"
    assert config["bump-minor-pre-major"] is True
    assert config["include-v-in-tag"] is True
    assert config["include-component-in-tag"] is False
    # The squash commit of the release PR must pass the PR title check ("<type>: <description>").
    assert config["pull-request-title-pattern"] == "chore: release ${version}"
    assert set(config["packages"]) == {"."}
    package = config["packages"]["."]
    assert package["changelog-path"] == "CHANGELOG.md"
    assert package["extra-files"] == [
        {"type": "toml", "path": "backend/pyproject.toml", "jsonpath": "$.project.version"}
    ]
    # A one-off release-as left in place would propose the same version on every later release PR.
    assert "release-as" not in package and "release-as" not in config


def test_release_workflow_uses_the_ci_bot_app_token() -> None:
    text = (WORKFLOWS / "release-please.yml").read_text()
    on = text[text.index("\non:\n") : text.index("\npermissions:")]
    assert "push:\n    branches: [main]" in on and "workflow_dispatch:" in on
    assert "pull_request" not in on
    # GITHUB_TOKEN is read-only; the App token does every write.
    assert "\npermissions:\n  contents: read\n\n" in text
    assert re.findall(r"permission-([a-z-]+): (\w+)", text) == [
        ("contents", "write"),
        ("pull-requests", "write"),
        ("issues", "write"),
    ]
    assert "environment: release" in text
    assert "if: github.ref == 'refs/heads/main'" in text
    assert "uses: actions/create-github-app-token@v2" in text
    assert "app-id: ${{ vars.AA_CI_BOT_APP_ID }}" in text
    assert "private-key: ${{ secrets.AA_CI_BOT_PRIVATE_KEY }}" in text
    assert "uses: googleapis/release-please-action@v4" in text
    assert "token: ${{ steps.app-token.outputs.token }}" in text
    assert "GITHUB_TOKEN" not in text.split("\njobs:")[1]


# ── a release commit never deploys ───────────────────────────────────────────


def _deploys(paths: list[str], changed: str) -> bool:
    """GitHub ``paths`` semantics for the patterns used here: the last matching pattern wins, ``!`` excludes."""
    hit = False
    for pattern in paths:
        negated = pattern.startswith("!")
        glob = pattern.removeprefix("!")
        if glob.endswith("/**"):
            match = changed.startswith(glob.removesuffix("**"))
        else:
            match = changed == glob
        if match:
            hit = not negated
    return hit


def test_release_commit_files_trigger_no_deploy() -> None:
    release_files = ["CHANGELOG.md", ".release-please-manifest.json", "backend/pyproject.toml"]
    for workflow in ("deploy-dev.yml", "vercel-deploy.yml"):
        paths = _push_paths(workflow)
        assert paths, workflow
        for changed in release_files:
            assert not _deploys(paths, changed), f"{workflow} deploys on {changed}"
    # Sanity: app code still deploys.
    assert _deploys(_push_paths("deploy-dev.yml"), "backend/app/main.py")
    assert _deploys(_push_paths("vercel-deploy.yml"), "frontend/package.json")
