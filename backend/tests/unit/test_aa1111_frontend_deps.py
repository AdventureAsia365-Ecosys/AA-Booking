"""AA-1111 (S294): the staff admin's (frontend/) dependency floor and its npm audit gate.

1. The lockfile resolves every ``zod`` to 4.6.6 or later (GHSA-mrhx-c7xc-m3w5, high; dev only, through
   eslint-config-next).
2. ``next`` stays an exact pin at 16.4.6 or later, ``@sentry/nextjs`` an exact 11.x pin (the CIS admin and the CRM web
   run the same pair).
3. CI's "Security Audit" job runs ``npm audit`` in frontend/ through ``.github/scripts/npm_audit_gate.py`` with the
   allowlist, so a new high or critical advisory fails the pull request; the gate's own tests run in Workflow Lint.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
FE = REPO / "frontend"


def _version(text: str) -> tuple[int, ...]:
    match = re.match(r"\d+(?:\.\d+)*", text)
    assert match, text
    return tuple(int(p) for p in match.group(0).split("."))


def test_lockfile_zod_is_patched():
    packages = json.loads((FE / "package-lock.json").read_text(encoding="utf-8"))["packages"]
    zod = [(path, meta["version"]) for path, meta in packages.items() if path.endswith("node_modules/zod")]
    assert zod
    assert all(_version(v) >= (4, 6, 6) for _, v in zod), zod


def test_next_and_sentry_pins():
    deps = json.loads((FE / "package.json").read_text(encoding="utf-8"))["dependencies"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", deps["next"])
    assert _version(deps["next"]) >= (16, 4, 6)
    assert re.fullmatch(r"11\.\d+\.\d+", deps["@sentry/nextjs"])


def test_security_audit_runs_the_npm_audit_gate():
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    job = ci.split("\n  security-audit:\n", 1)[1].split("\n  # ", 1)[0]
    step = job.split("- name: npm audit (staff admin, failing on high)\n", 1)[1]
    assert "working-directory: frontend" in step
    assert "npm audit --json > ../npm-audit.json || true" in step
    gate = "python3 ../.github/scripts/npm_audit_gate.py ../.github/npm-audit-allow.json < ../npm-audit.json"
    assert gate in step
    allow = json.loads((REPO / ".github" / "npm-audit-allow.json").read_text(encoding="utf-8"))
    assert isinstance(allow.get("allow"), list)
    lint = (REPO / ".github" / "workflows" / "workflow-lint.yml").read_text(encoding="utf-8")
    assert "-p 'test_npm_audit_gate.py'" in lint
