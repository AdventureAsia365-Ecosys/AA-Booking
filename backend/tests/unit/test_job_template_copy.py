"""scripts/check_job_template_copy.py: the drift check of the vendored CIS job table template (AA-751)."""

from __future__ import annotations

import importlib.util
import io
import urllib.error
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_job_template_copy.py"
_spec = importlib.util.spec_from_file_location("check_job_template_copy", SCRIPT)
assert _spec and _spec.loader
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)


def _local(tmp_path: Path) -> Path:
    for name in check.COPIES:
        (tmp_path / name).write_bytes(f"-- {name}\nline two\n".encode())
    return tmp_path


def test_the_vendored_files_exist_and_are_listed() -> None:
    assert sorted(p.name for p in check.LOCAL_DIR.iterdir() if p.name in check.COPIES) == sorted(check.COPIES)


def test_byte_copies_pass(tmp_path: Path) -> None:
    local = _local(tmp_path)
    by_source = {src: (local / name).read_bytes() for name, src in check.COPIES.items()}
    out = io.StringIO()
    assert check.run(local, by_source.get, out) == 0
    assert "2 file(s) checked, 0 failed" in out.getvalue()


def test_a_one_byte_difference_fails_with_its_position(tmp_path: Path) -> None:
    local = _local(tmp_path)
    by_source = {src: (local / name).read_bytes() for name, src in check.COPIES.items()}
    by_source["shared/jobs/job_table_shape.py"] = by_source["shared/jobs/job_table_shape.py"].replace(b"two", b"twO")
    out = io.StringIO()
    assert check.run(local, by_source.get, out) == 1
    assert "differs from AdventureAsia365-Ecosys/AA-CIS-App@main:shared/jobs/job_table_shape.py" in out.getvalue()
    assert "(line 2)" in out.getvalue()


def test_missing_upstream_missing_copy_and_fetch_errors_fail(tmp_path: Path) -> None:
    local = _local(tmp_path)
    (local / "job_table_shape.py").unlink()

    def fetch(path: str) -> bytes | None:
        raise check.FetchError("HTTP 500")

    assert "missing; copy" in check.check_copy("job_table_shape.py", local, fetch)
    assert "cannot compare" in check.check_copy("job_table_template.sql", local, fetch)
    assert "not found on" in check.check_copy("job_table_template.sql", local, lambda _p: None)
    assert "prefix" in check.first_difference(b"ab", b"abc")


def test_github_fetch_retries_then_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def boom(req, timeout: float):
        calls.append(req.full_url)
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(check.urllib.request, "urlopen", boom)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    with pytest.raises(check.FetchError, match="network error"):
        check.github_fetch("shared/jobs/job_table_shape.py", attempts=2, sleep=lambda _s: None)
    assert len(calls) == 2 and calls[0].endswith("/contents/shared/jobs/job_table_shape.py?ref=main")
