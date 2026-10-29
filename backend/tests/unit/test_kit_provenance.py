"""scripts/check_kit_provenance.py: the vendored UI kit core is what AA-CIS-App main has at the lock's ref (AA-994 S4b,
root ADR 0016 decision 5). Adapted from the AA-Partner test (paths: frontend/ui/core)."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import urllib.error
from collections.abc import Sequence
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_kit_provenance.py"
_spec = importlib.util.spec_from_file_location("check_kit_provenance", SCRIPT)
assert _spec and _spec.loader
prov = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(prov)

REF = "a" * 40
MANIFEST = b'{"files": {"tokens.ts": {"sha256": "x"}}}\n'


def _repo(tmp_path: Path, manifest: bytes = MANIFEST, **lock_overrides: object) -> Path:
    core = tmp_path / "frontend" / "ui" / "core"
    core.mkdir(parents=True)
    (core / "MANIFEST.json").write_bytes(manifest)
    lock = {
        "adr": "root ADR 0016",
        "canonical": {"repo": prov.CANONICAL_REPO, "path": prov.CANONICAL_PATH},
        "ref": REF,
        "profile": "full",
        "dest": "frontend/ui/core",
        "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
    }
    lock.update(lock_overrides)
    (tmp_path / "frontend" / "kit.lock.json").write_text(json.dumps(lock))
    return tmp_path / "frontend" / "kit.lock.json"


def _github(manifest: bytes | None = MANIFEST, status: str | None = "ahead"):
    calls: list[str] = []

    def get(path: str, accept: str) -> bytes | None:
        calls.append(path)
        if "/contents/" in path:
            assert path.endswith(f"/contents/{prov.CANONICAL_PATH}/MANIFEST.json?ref={REF}")
            assert accept == "application/vnd.github.raw"
            return manifest
        assert path.endswith(f"/compare/{REF}...main")
        return None if status is None else json.dumps({"status": status}).encode()

    return get, calls


def test_a_synced_copy_on_main_passes(tmp_path: Path) -> None:
    lock = _repo(tmp_path)
    get, calls = _github()
    assert prov.check_lock(lock, get, tmp_path) == []
    assert len(calls) == 2
    get, _ = _github(status="identical")
    assert prov.check_lock(lock, get, tmp_path) == []


def test_an_edited_copy_with_a_regenerated_manifest_fails(tmp_path: Path) -> None:
    edited = MANIFEST.replace(b'"x"', b'"y"')
    lock = _repo(tmp_path, manifest=edited)  # lock + manifest consistent with each other, not with CIS
    get, _ = _github()
    problems = prov.check_lock(lock, get, tmp_path)
    assert len(problems) == 1 and "differs from AdventureAsia365-Ecosys/AA-CIS-App@aaaaaaaaaaaa" in problems[0]


def test_a_ref_that_is_not_on_cis_main_fails(tmp_path: Path) -> None:
    lock = _repo(tmp_path)
    for status in ("behind", "diverged"):
        get, _ = _github(status=status)
        problems = prov.check_lock(lock, get, tmp_path)
        assert len(problems) == 1 and "is not on AA-CIS-App main" in problems[0], problems
    get, _ = _github(status=None)
    assert "has no commit" in prov.check_lock(lock, get, tmp_path)[0]
    get, _ = _github(manifest=None)
    assert "not found" in prov.check_lock(lock, get, tmp_path)[0]


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"ref": "local"}, "try-out sync"),
        ({"ref": "main"}, "full 40-character commit sha"),
        ({"canonical": {"repo": "someone/AA-CIS-App", "path": prov.CANONICAL_PATH}}, "canonical must be"),
        ({"dest": "../elsewhere"}, "repo-relative folder"),
        ({"dest": "frontend/ui/missing"}, "MANIFEST.json is missing"),
    ],
)
def test_a_bad_lock_fails_before_any_network_call(tmp_path: Path, override: dict, message: str) -> None:
    lock = _repo(tmp_path, **override)
    get, calls = _github()
    problems = prov.check_lock(lock, get, tmp_path)
    assert len(problems) == 1 and message in problems[0], problems
    assert calls == []


def test_a_lock_that_does_not_match_the_copied_manifest_fails(tmp_path: Path) -> None:
    lock = _repo(tmp_path, manifest_sha256="0" * 64)
    get, _ = _github()
    assert "manifest_sha256" in prov.check_lock(lock, get, tmp_path)[0]
    lock.write_text("not json")
    assert "cannot read the lock" in prov.check_lock(lock, get, tmp_path)[0]
    lock.write_text("[]")
    assert "not a JSON object" in prov.check_lock(lock, get, tmp_path)[0]


def test_github_errors_are_reported_not_raised(tmp_path: Path) -> None:
    lock = _repo(tmp_path)

    def down(path: str, accept: str) -> bytes | None:
        raise prov.FetchError("HTTP 503")

    assert "cannot verify against AA-CIS-App: HTTP 503" in prov.check_lock(lock, down, tmp_path)[0]

    def garbled(path: str, accept: str) -> bytes | None:
        return MANIFEST if "/contents/" in path else b"<html>"

    assert "unexpected GitHub compare answer" in prov.check_lock(lock, garbled, tmp_path)[0]


class _Git:
    def __init__(self, diff: str | None, has_base: bool = True, refs: dict[str, str] | None = None) -> None:
        self.diff, self.has_base, self.refs, self.calls = diff, has_base, refs or {}, []

    def __call__(self, args: Sequence[str]) -> str | None:
        self.calls.append(list(args))
        if args[0] == "rev-parse":
            sha = self.refs.get(args[-1].removesuffix("^{commit}"))
            return f"{sha}\n" if sha else None
        if args[0] == "cat-file":
            return "" if self.has_base else None
        if args[0] == "fetch":
            return ""
        return self.diff


def test_main_skips_github_when_the_kit_did_not_change(tmp_path: Path) -> None:
    lock = _repo(tmp_path)
    get, calls = _github()
    git = _Git(diff="")
    out = io.StringIO()
    assert prov.main([str(lock), "--base", "b" * 40], get, git, out, tmp_path) == 0
    assert calls == [] and "kit core unchanged" in out.getvalue()
    watched = ["frontend/kit.lock.json", "frontend/ui/core"]
    assert git.calls[-1] == ["diff", "--name-only", "b" * 40, "HEAD", "--", *watched]


def test_main_resolves_a_ref_name_base_before_the_diff(tmp_path: Path) -> None:
    lock = _repo(tmp_path)
    get, calls = _github()
    git = _Git(diff="", refs={"origin/main": "d" * 40})
    out = io.StringIO()
    assert prov.main([str(lock), "--base", "origin/main"], get, git, out, tmp_path) == 0
    assert calls == [] and "kit core unchanged" in out.getvalue()
    assert ["rev-parse", "--verify", "--quiet", "origin/main^{commit}"] in git.calls
    assert git.calls[-1][:4] == ["diff", "--name-only", "d" * 40, "HEAD"]


def test_main_checks_when_the_kit_changed_or_the_diff_is_unknown(tmp_path: Path) -> None:
    lock = _repo(tmp_path)
    for git, base in (
        (_Git(diff="frontend/ui/core/tokens.ts\n"), "b" * 40),
        (_Git(diff=None, has_base=False), "b" * 40),  # base not fetchable: full check
        (_Git(diff=""), prov.ZERO_SHA),  # push of a new branch
        (_Git(diff=""), ""),  # no base at all
        (_Git(diff=""), "no-such-ref"),  # a ref name git cannot resolve
        (_Git(diff=""), "--output=x"),  # never handed to git as an option
    ):
        get, calls = _github()
        out = io.StringIO()
        argv = [str(lock)] + ([f"--base={base}"] if base else [])
        assert prov.main(argv, get, git, out, tmp_path) == 0, out.getvalue()
        assert len(calls) == 2 and "(on main)" in out.getvalue()
    git = _Git(diff=None, has_base=False)
    prov.main([str(lock), "--base", "c" * 40], _github()[0], git, io.StringIO(), tmp_path)
    assert ["fetch", "--no-tags", "--quiet", "--depth=1", "origin", "c" * 40] in git.calls


def test_main_reports_each_failure_and_annotates_on_actions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lock = _repo(tmp_path)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    get, _ = _github(status="diverged")
    out = io.StringIO()
    assert prov.main([str(lock)], get, _Git(diff=""), out, tmp_path) == 1
    assert "FAIL  frontend/kit.lock.json" in out.getvalue()
    assert "::error file=frontend/kit.lock.json::" in out.getvalue()
    assert "1 lock(s) checked, 1 failed" in out.getvalue()


def test_the_repo_lock_is_a_full_profile_copy_of_cis_core() -> None:
    lock = json.loads((prov.REPO_ROOT / "frontend" / "kit.lock.json").read_text())
    assert lock["canonical"] == {"repo": prov.CANONICAL_REPO, "path": prov.CANONICAL_PATH}
    assert lock["profile"] == "full" and lock["dest"] == "frontend/ui/core"
    assert prov.SHA_RE.match(lock["ref"])
    manifest = (prov.REPO_ROOT / lock["dest"] / "MANIFEST.json").read_bytes()
    assert hashlib.sha256(manifest).hexdigest() == lock["manifest_sha256"]


def test_github_get_retries_then_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def boom(req, timeout: float):
        calls.append(req.full_url)
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(prov.urllib.request, "urlopen", boom)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    with pytest.raises(prov.FetchError, match="network error"):
        prov.github_get("/repos/x/y/compare/a...main", "application/json", attempts=2, sleep=lambda _s: None)
    assert len(calls) == 2 and calls[0] == "https://api.github.com/repos/x/y/compare/a...main"


def test_github_get_treats_404_as_none_and_4xx_as_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def answer(code: int):
        def fake(req, timeout: float):
            raise urllib.error.HTTPError(req.full_url, code, "x", {}, None)

        return fake

    monkeypatch.setattr(prov.urllib.request, "urlopen", answer(404))
    assert prov.github_get("/p", "a") is None
    monkeypatch.setattr(prov.urllib.request, "urlopen", answer(422))
    with pytest.raises(prov.FetchError, match="HTTP 422"):
        prov.github_get("/p", "a")
