"""AA-899: the committed OpenAPI document (docs/api/openapi.json) is current, deterministic and host-free.

The "API contract" workflow diffs this committed file against the base branch with oasdiff, so a stale file would
hide a breaking change: this test is what keeps it current (it runs in the required "Unit Tests" check).
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "export_openapi.py"
_spec = importlib.util.spec_from_file_location("export_openapi", SCRIPT)
assert _spec and _spec.loader
export = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(export)

# Same rule as PUBLIC_PATHS in .github/workflows/api-contract.yml.
PUBLIC_PATHS = re.compile(r"^/booking/(b2b/|customer/|health|ops/)")


def test_committed_document_is_current() -> None:
    problems = export.check(export.render(export.build_spec()))
    assert not problems, (
        "docs/api/openapi.json is stale; run (from backend/) python3 scripts/export_openapi.py\n" + "\n".join(problems)
    )


def test_render_is_stable_and_host_free() -> None:
    spec = export.build_spec()
    text = export.render(spec)
    assert text == export.render(export.build_spec())
    assert text.endswith("}\n")
    doc = json.loads(text)
    assert "servers" not in doc
    assert doc["info"]["version"] == export.UNRELEASED
    assert "do not edit" in doc["info"]["x-generated-by"]


def test_normalize_drops_servers_and_pins_the_version() -> None:
    raw = {"openapi": "3.1.0", "info": {"title": "t", "version": "9.9.9"}, "servers": [{"url": "https://x"}]}
    out = export.normalize(raw)
    assert "servers" not in out and out["info"]["version"] == "unreleased"
    assert raw["info"]["version"] == "9.9.9"  # the input is not mutated


def test_check_reports_a_stale_or_missing_file(tmp_path: Path) -> None:
    path = tmp_path / "openapi.json"
    assert export.check("{}\n", path)  # missing
    path.write_text("{}\n", encoding="utf-8")
    assert export.check("{}\n", path) == []
    assert any(line.startswith("+") for line in export.check('{"a": 1}\n', path))


def test_main_writes_then_checks(tmp_path: Path, capsys) -> None:
    out = tmp_path / "api" / "openapi.json"
    assert export.main(["--output", str(out)]) == 0
    assert export.main(["--check", "--output", str(out)]) == 0
    out.write_text("{}\n", encoding="utf-8")
    assert export.main(["--check", "--output", str(out)]) == 1
    assert "is stale" in capsys.readouterr().out


def test_public_health_bodies_name_every_field() -> None:
    """The breaking-change check can only see a removed field when the response is a model (AA-899 scope)."""
    schemas = export.build_spec()["components"]["schemas"]
    assert set(schemas["Health"]["properties"]) == {"status", "service", "version", "git_sha"}
    assert {"status", "db", "schema_version"} <= set(schemas["Readiness"]["properties"])


def test_every_public_response_names_its_fields() -> None:
    """oasdiff only sees a removed field when the 2xx JSON body is a named model. Booking's public surface is fully
    typed (unlike CIS, there is no allow-list): a public route answering a bare ``dict`` fails here."""
    untyped = []
    for path, ops in export.build_spec()["paths"].items():
        if not PUBLIC_PATHS.match(path):
            continue
        for method, op in ops.items():
            for code, resp in op.get("responses", {}).items():
                if not code.startswith("2") or code == "204":
                    continue
                schema = resp.get("content", {}).get("application/json", {}).get("schema") or {}
                if "$ref" not in schema:
                    untyped.append(f"{method.upper()} {path} {code}")
    assert not untyped, f"public route(s) without a named response model (declare response_model=): {untyped}"
