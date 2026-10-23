"""Export the Booking API's OpenAPI document to ``docs/api/openapi.json`` (AA-899).

The committed file is the API reference of this repo and the base of the breaking-change check
(``.github/workflows/api-contract.yml``, oasdiff). It is generated, never edited by hand:

    python3 scripts/export_openapi.py           # (from backend/) rewrite docs/api/openapi.json
    python3 scripts/export_openapi.py --check   # exit 1 when the committed file is stale (CI: Unit Tests)

The document is built from the code alone: ``create_app`` with ``ENVIRONMENT=test`` settings, no database, no network,
no secret (the verifiers only fetch keys on the first request, and the lifespan never runs). It is made deterministic:

- keys sorted, two-space indent, UTF-8, one trailing newline;
- ``servers`` dropped (no host in a public repo, and the host differs per environment);
- ``info.version`` fixed to ``unreleased``: the release workflow stamps the tag into the copy it attaches to the
  GitHub release, so a release PR (which only bumps the version) never makes this file stale;
- ``info.x-generated-by`` names this script (the handbook rule for generated files: "do not edit").

FastAPI and Pydantic are pinned in ``requirements.txt``; a different local version can change the output, so
``--check`` (and the unit test) also print the versions they ran with.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
OUTPUT = REPO_ROOT / "docs" / "api" / "openapi.json"
GENERATED_BY = "backend/scripts/export_openapi.py (AA-899) - generated, do not edit"
UNRELEASED = "unreleased"

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def build_spec() -> dict[str, Any]:
    """The raw ``app.openapi()`` of an app built from code only (no environment variable is read)."""
    from app.main import create_app
    from app.settings import load_settings

    return create_app(load_settings({"ENVIRONMENT": "test"})).openapi()


def normalize(spec: dict[str, Any]) -> dict[str, Any]:
    out = dict(spec)
    out.pop("servers", None)
    info = dict(out.get("info") or {})
    info["version"] = UNRELEASED
    info["x-generated-by"] = GENERATED_BY
    out["info"] = info
    return out


def render(spec: dict[str, Any]) -> str:
    return json.dumps(normalize(spec), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _versions() -> str:
    import fastapi
    import pydantic

    return f"fastapi {fastapi.__version__}, pydantic {pydantic.VERSION}"


def check(expected: str, path: Path = OUTPUT) -> list[str]:
    """Empty when ``path`` holds exactly ``expected``; else a short unified diff (at most 40 lines)."""
    current = path.read_text(encoding="utf-8") if path.is_file() else ""
    if current == expected:
        return []
    diff = difflib.unified_diff(current.splitlines(), expected.splitlines(), "committed", "generated", lineterm="", n=1)
    lines = list(diff)
    return lines[:40] + ([f"... {len(lines) - 40} more diff lines"] if len(lines) > 40 else [])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail when the committed file is stale")
    parser.add_argument("--output", type=Path, default=OUTPUT, help="file to write or check (default: %(default)s)")
    args = parser.parse_args(argv)
    os.environ.setdefault("ENVIRONMENT", "test")
    text = render(build_spec())
    if args.check:
        problems = check(text, args.output)
        if problems:
            print("\n".join(problems))
            print(f"::error::{args.output.name} is stale. Run (from backend/): python3 scripts/export_openapi.py")
            print(f"generated with {_versions()}")
            return 1
        print(f"{args.output} is up to date ({_versions()})")
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(text, encoding="utf-8")
    print(f"wrote {args.output} ({len(json.loads(text)['paths'])} paths, {_versions()})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
