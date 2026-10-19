"""Checks that the files Booking vendors from AA-CIS-App are byte copies of AA-CIS-App main.

- ``tests/job_template/job_table_template.sql`` and ``job_table_shape.py`` = AA-CIS-App ``shared/jobs/`` (AA-751, X6;
  ADR 0003a decision 2: one DDL template, owned by CIS; every owner's CI compares its job table with it);
- ``app/auth/identity_verifier.py`` = AA-CIS-App ``api/core/identity_verifier.py`` (AA-680 Phase 2; root ADR 0006 §8
  rule 9: the multi-issuer verifier is copied byte-identical into each app repo).

This check fails when a copy differs from the file on AA-CIS-App ``main`` by even one byte, or is not on main yet.
Modelled on AA-CIS-Infra ``db/check_migration_copies.py``. CI job "Vendored CIS copies" (non-required).

Usage (from ``backend/``)::

    python3 scripts/check_vendored_copies.py

Reads the CIS files through the GitHub contents API (the app repos are public). ``GITHUB_TOKEN`` (or ``GH_TOKEN``) is
used when set (higher rate limit); without it, or if GitHub rejects it, the call is anonymous. Rate limits, network
errors and 5xx are retried, then reported as a FAIL (never a traceback). Standard library only: CI needs no install.
"""

from __future__ import annotations

import hashlib
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

ORG = "AdventureAsia365-Ecosys"
REPO = "AA-CIS-App"
BRANCH = "main"
BACKEND_DIR = Path(__file__).resolve().parent.parent
# path under backend/ -> path in AA-CIS-App
COPIES: dict[str, str] = {
    "tests/job_template/job_table_template.sql": "shared/jobs/job_table_template.sql",
    "tests/job_template/job_table_shape.py": "shared/jobs/job_table_shape.py",
    "app/auth/identity_verifier.py": "api/core/identity_verifier.py",
}


class FetchError(RuntimeError):
    """The CIS file could not be read (rate limit, network, GitHub outage); not a 404."""


# Returns the file's bytes on AA-CIS-App main, None when it is not there (HTTP 404); raises FetchError otherwise.
Fetcher = Callable[[str], bytes | None]


def first_difference(a: bytes, b: bytes) -> str:
    for i, (x, y) in enumerate(zip(a, b, strict=False)):
        if x != y:
            line = a[:i].count(b"\n") + 1
            return f"first difference at byte {i} (line {line})"
    return f"one file is a prefix of the other ({len(a)} vs {len(b)} bytes)"


def check_copy(name: str, base_dir: Path, fetch: Fetcher) -> str | None:
    """The problem with one copy, or None when it is a byte copy of CIS main."""
    source = COPIES[name]
    where = f"{ORG}/{REPO}@{BRANCH}:{source}"
    local_path = base_dir / name
    if not local_path.is_file():
        return f"missing; copy {where} here byte for byte"
    local = local_path.read_bytes()
    try:
        upstream = fetch(source)
    except FetchError as exc:
        return f"cannot compare with {where}: {exc}"
    if upstream is None:
        return f"not found on {where}: merge the CIS change first, then re-run this check"
    if upstream != local:
        return (
            f"differs from {where}: {first_difference(local, upstream)}; "
            f"sha256 copy={hashlib.sha256(local).hexdigest()[:12]} cis={hashlib.sha256(upstream).hexdigest()[:12]}. "
            "Copy the CIS file byte for byte (cp, not an editor); never edit the copy here"
        )
    return None


def _token() -> str:
    return (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()


def github_fetch(path: str, attempts: int = 3, sleep: Callable[[float], None] = time.sleep) -> bytes | None:
    url = f"https://api.github.com/repos/{ORG}/{REPO}/contents/{path}?ref={BRANCH}"
    token = _token()
    auths = ([token] if token else []) + [""]
    last = ""
    for attempt in range(attempts):
        for auth in auths:
            headers = {
                "Accept": "application/vnd.github.raw",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "aa-booking-vendored-copy-check",
            }
            if auth:
                headers["Authorization"] = f"Bearer {auth}"
            try:
                req = urllib.request.Request(url, headers=headers)  # noqa: S310 — fixed https URL
                with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 — fixed https URL
                    return resp.read()
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    return None
                last = f"HTTP {exc.code}"
                if exc.code in (401, 403, 429):
                    continue  # token refused or rate limited: try anonymous / next round
                if exc.code < 500:
                    raise FetchError(f"GitHub API {last} for {REPO}:{path}") from None
                break  # 5xx: back off and retry
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last = f"network error {exc}"
                break
        if attempt + 1 < attempts:
            sleep(2 * (attempt + 1))
    hint = "" if token else " (anonymous: 60 requests/hour; set GITHUB_TOKEN or GH_TOKEN)"
    raise FetchError(f"could not read {REPO}:{path} from GitHub: {last}{hint}")


def run(base_dir: Path = BACKEND_DIR, fetch: Fetcher = github_fetch, out: TextIO = sys.stdout) -> int:
    failed = 0
    for name in COPIES:
        problem = check_copy(name, base_dir, fetch)
        rel = f"backend/{name}"
        if problem:
            failed += 1
            print(f"FAIL  {rel}: {problem}", file=out)
            if os.environ.get("GITHUB_ACTIONS") == "true":
                print(f"::error file={rel}::{problem}", file=out)
        else:
            print(f"ok    {rel}", file=out)
    print(f"{len(COPIES)} file(s) checked, {failed} failed", file=out)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run())
