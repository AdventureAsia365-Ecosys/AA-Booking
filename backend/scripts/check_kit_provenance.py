"""Provenance of the vendored UI kit core (AA-994 S4b, root ADR 0016 decision 5, plan guard G13).

Copied from AA-Partner ``api/scripts/check_kit_provenance.py`` (AA-994 S2a); only this docstring differs.

``frontend/ui/core/`` is a byte copy of AA-CIS-App ``frontend/app/_kit/core/``, written
by the root ``tools/kit/kit-sync.sh``, which records the CIS commit in ``kit.lock.json`` (``ref``) together with the
sha256 of the copied ``MANIFEST.json``. The integrity check (``core/check-kit-copy.mjs``, offline) proves that the copy
matches its own manifest. This check proves where that manifest came from:

1. the lock names the canonical source (``AdventureAsia365-Ecosys/AA-CIS-App``, ``frontend/app/_kit/core``) and a full
   commit sha (``"ref": "local"``, a ``--from-local`` try-out, is refused by design);
2. the copied ``MANIFEST.json`` has the lock's ``manifest_sha256``;
3. it is byte-identical to ``frontend/app/_kit/core/MANIFEST.json`` of AA-CIS-App at ``ref``;
4. ``ref`` is on AA-CIS-App ``main`` (GitHub compare ``ref...main`` is ``ahead`` or ``identical``).

Together with integrity, every file of the copy equals CIS at a commit CIS has merged: an edited copy with a
regenerated manifest fails (3), and a copy of an unmerged CIS branch fails (4).

The check always runs (Frontend Build; no path filter, so it also reports on ``merge_group``) but only reads
GitHub when the change touches the kit: with ``--base <sha>`` it diffs ``<sha>..HEAD`` over the lock and the core
folder and passes at once when neither changed. A ref name (``--base origin/main``, for local runs) is resolved to its
commit first. No base (or a base git cannot read) means a full check.

Usage (any working directory; lock paths are relative to it)::

    python3 backend/scripts/check_kit_provenance.py frontend/kit.lock.json [--base <sha|ref>]

Reads AA-CIS-App through the GitHub REST API (public repo). ``GITHUB_TOKEN`` (or ``GH_TOKEN``) only lifts the rate
limit; without it, or if GitHub refuses it, calls are anonymous. Network errors, rate limits and 5xx are retried, then
reported as a FAIL. Standard library only. Exit codes: 0 fine, 1 a lock failed, 2 usage error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TextIO

CANONICAL_REPO = "AdventureAsia365-Ecosys/AA-CIS-App"
CANONICAL_PATH = "frontend/app/_kit/core"
MAIN = "main"
REPO_ROOT = Path(__file__).resolve().parents[2]
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
ZERO_SHA = "0" * 40
FIX = (
    "re-sync with the root tools/kit/kit-sync.sh from AA-CIS-App main (never edit core/, MANIFEST.json or "
    "kit.lock.json by hand)"
)


class FetchError(RuntimeError):
    """GitHub could not be read (rate limit, network, outage); not a 404."""


# (url path under api.github.com, Accept header) -> body bytes, or None on HTTP 404; raises FetchError otherwise.
Getter = Callable[[str, str], bytes | None]
# git arguments -> stdout, or None when git failed.
GitRunner = Callable[[Sequence[str]], str | None]


def _token() -> str:
    return (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()


def github_get(path: str, accept: str, attempts: int = 3, sleep: Callable[[float], None] = time.sleep) -> bytes | None:
    url = f"https://api.github.com{path}"
    token = _token()
    auths = ([token] if token else []) + [""]
    last = ""
    for attempt in range(attempts):
        for auth in auths:
            headers = {
                "Accept": accept,
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "aa-partner-kit-provenance",
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
                    raise FetchError(f"GitHub API {last} for {path}") from None
                break  # 5xx: back off and retry
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last = f"network error {exc}"
                break
        if attempt + 1 < attempts:
            sleep(2 * (attempt + 1))
    hint = "" if token else " (anonymous: 60 requests/hour; set GITHUB_TOKEN or GH_TOKEN)"
    raise FetchError(f"could not read {path} from GitHub: {last}{hint}")


def run_git(args: Sequence[str]) -> str | None:
    git = shutil.which("git")
    if git is None:
        return None
    try:
        done = subprocess.run(  # noqa: S603 — the git binary on PATH, arguments built here
            [git, "-C", str(REPO_ROOT), *args], capture_output=True, text=True, timeout=120, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout if done.returncode == 0 else None


def kit_changed(paths: Sequence[str], base: str, git: GitRunner = run_git) -> bool:
    """True when <base>..HEAD touches any of `paths` (repo-relative), or when the diff cannot be computed.

    CI passes a sha; a local run may pass a ref name (``origin/main``), which is resolved to its commit first."""
    if not base or base == ZERO_SHA or base.startswith("-"):
        return True
    if not SHA_RE.match(base):
        resolved = git(["rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"])
        if resolved is None or not SHA_RE.match(resolved.strip()):
            return True
        base = resolved.strip()
    if git(["cat-file", "-e", f"{base}^{{commit}}"]) is None:
        git(["fetch", "--no-tags", "--quiet", "--depth=1", "origin", base])
    out = git(["diff", "--name-only", base, "HEAD", "--", *paths])
    return out is None or bool(out.strip())


def check_lock(lock_path: Path, get: Getter, root: Path = REPO_ROOT) -> list[str]:
    """The provenance problems of one kit.lock.json (empty when it is fine)."""
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"cannot read the lock: {exc}"]
    if not isinstance(lock, dict):
        return ["the lock is not a JSON object"]
    canonical = lock.get("canonical") or {}
    expected = {"repo": CANONICAL_REPO, "path": CANONICAL_PATH}
    if not isinstance(canonical, dict) or {k: canonical.get(k) for k in expected} != expected:
        return [f"canonical must be {{repo: {CANONICAL_REPO}, path: {CANONICAL_PATH}}}, got {canonical!r}; {FIX}"]
    ref = lock.get("ref")
    if ref == "local":
        return [f'"ref": "local" is a try-out sync (kit-sync.sh --from-local) and never merges; {FIX}']
    if not isinstance(ref, str) or not SHA_RE.match(ref):
        return [f"ref must be a full 40-character commit sha of AA-CIS-App, got {ref!r}; {FIX}"]
    dest = lock.get("dest")
    if not isinstance(dest, str) or not dest or dest.startswith("/") or ".." in Path(dest).parts:
        return [f"dest must be a repo-relative folder, got {dest!r}; {FIX}"]
    manifest_path = root / dest / "MANIFEST.json"
    try:
        local = manifest_path.read_bytes()
    except OSError:
        return [f"{dest}/MANIFEST.json is missing; {FIX}"]
    problems: list[str] = []
    if hashlib.sha256(local).hexdigest() != lock.get("manifest_sha256"):
        problems.append(f"{dest}/MANIFEST.json does not have the lock's manifest_sha256; {FIX}")
    repo = f"/repos/{CANONICAL_REPO}"
    where = f"{CANONICAL_REPO}@{ref[:12]}:{CANONICAL_PATH}/MANIFEST.json"
    try:
        upstream = get(f"{repo}/contents/{CANONICAL_PATH}/MANIFEST.json?ref={ref}", "application/vnd.github.raw")
        if upstream is None:
            problems.append(f"{where} not found (unknown commit, or no kit core there); {FIX}")
        elif upstream != local:
            problems.append(f"{dest}/MANIFEST.json differs from {where}: the copy is not what CIS has at ref; {FIX}")
        compare = get(f"{repo}/compare/{urllib.parse.quote(ref)}...{MAIN}", "application/vnd.github+json")
        if compare is None:
            problems.append(f"AA-CIS-App has no commit {ref}; {FIX}")
        else:
            status = json.loads(compare).get("status")
            if status not in ("ahead", "identical"):
                problems.append(
                    f"ref {ref[:12]} is not on AA-CIS-App {MAIN} (compare status {status!r}): merge the CIS kit change "
                    f"first, then sync from main"
                )
    except FetchError as exc:
        problems.append(f"cannot verify against AA-CIS-App: {exc}")
    except ValueError as exc:
        problems.append(f"unexpected GitHub compare answer: {exc}")
    return problems


def main(
    argv: Sequence[str] | None = None,
    get: Getter = github_get,
    git: GitRunner = run_git,
    out: TextIO = sys.stdout,
    root: Path = REPO_ROOT,
) -> int:
    parser = argparse.ArgumentParser(description="Provenance of the vendored UI kit core (root ADR 0016).")
    parser.add_argument("locks", nargs="+", type=Path, help="kit.lock.json files")
    parser.add_argument("--base", default="", help="diff base commit (sha or ref); unchanged kit = pass without GitHub")
    args = parser.parse_args(argv)
    failed = 0
    for lock_path in args.locks:
        rel = os.path.relpath(lock_path.resolve(), root.resolve())
        try:
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
            dest = lock.get("dest") if isinstance(lock, dict) else None
        except (OSError, ValueError):
            dest = None
        watched = [rel] + ([dest] if isinstance(dest, str) and dest else [])
        if args.base and not kit_changed(watched, args.base, git):
            print(f"ok    {rel}: kit core unchanged since {args.base[:12]}; provenance not re-checked", file=out)
            continue
        problems = check_lock(lock_path, get, root)
        if problems:
            failed += 1
            for problem in problems:
                print(f"FAIL  {rel}: {problem}", file=out)
                if os.environ.get("GITHUB_ACTIONS") == "true":
                    print(f"::error file={rel}::{problem}", file=out)
        else:
            print(f"ok    {rel}: MANIFEST.json = {CANONICAL_REPO}@{lock_path_ref(lock_path)} (on {MAIN})", file=out)
    print(f"{len(args.locks)} lock(s) checked, {failed} failed", file=out)
    return 1 if failed else 0


def lock_path_ref(lock_path: Path) -> str:
    return str(json.loads(lock_path.read_text(encoding="utf-8")).get("ref", ""))[:12]


if __name__ == "__main__":
    sys.exit(main())
