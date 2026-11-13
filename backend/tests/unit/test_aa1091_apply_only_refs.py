"""A renumbered migration leaves no stale ``apply_migrations(..., only=[N])`` behind (AA-1091, S279).

``.github/scripts/migration_refs.py`` refuses a stale ``NNN_name`` token, but an integration test re-runs one file by
its bare number (``only=[54]``), which no name search reaches: when bk-053 / bk-054 became bk-054 / bk-055, the
``"bk-053"`` strings moved and ``only=[53]`` stayed, so the test of the price version data step re-ran bk-053 (another
file) and found no version. The rule here: every integer literal in an ``only=[...]`` list of a ``tests/integration``
file names an existing migration, and the same file says which one it means (``bk-0NN`` in its docstring or a
comment). A renumber that rewrites the ``bk-0NN`` text and forgets the integer then fails at once, without PostgreSQL.
Names (``only=[BK018]``) and ranges are not checked."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]
INTEGRATION = BACKEND / "tests" / "integration"
MIGRATIONS = BACKEND.parent / "migrations"


def only_literals(source: str) -> list[tuple[int, int]]:
    """``(line, number)`` for every integer literal in an ``only=[...]`` keyword of an ``apply_migrations`` call."""
    found: list[tuple[int, int]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
        if name != "apply_migrations":
            continue
        for kw in node.keywords:
            if kw.arg == "only" and isinstance(kw.value, ast.List | ast.Tuple | ast.Set):
                found += [
                    (e.lineno, e.value) for e in kw.value.elts if isinstance(e, ast.Constant) and type(e.value) is int
                ]
    return found


def stale_refs(source: str, numbers: set[int]) -> list[str]:
    """The ``only=[N]`` literals of ``source`` that name no migration file, or a bk- number the file never mentions."""
    bad = []
    for line, n in only_literals(source):
        if n not in numbers:
            bad.append(f"line {line}: only=[{n}] names no migration file")
        elif f"bk-{n:03d}" not in source:
            bad.append(f"line {line}: only=[{n}] but the file never says bk-{n:03d} (a renumber left behind?)")
    return bad


def test_the_rule_on_examples() -> None:
    numbers = {53, 54, 55}
    ok = '"""bk-054 data step."""\nawait apply_migrations(admin, only=[54])\n'
    assert stale_refs(ok, numbers) == []
    stale = '"""bk-054 data step."""\nawait apply_migrations(admin, only=[53])\n'
    assert stale_refs(stale, numbers) == ["line 2: only=[53] but the file never says bk-053 (a renumber left behind?)"]
    missing = '"""bk-099."""\nawait apply_migrations(admin, only=[99])\n'
    assert stale_refs(missing, numbers) == ["line 2: only=[99] names no migration file"]
    # Names, ranges and other calls are out of scope.
    other = (
        "await apply_migrations(admin, only=[BK018])\nawait apply_migrations(admin, only=range(1, 9))\nf(only=[7])\n"
    )
    assert stale_refs(other, numbers) == []


@pytest.mark.parametrize("path", sorted(INTEGRATION.glob("test_*.py")), ids=lambda p: p.name)
def test_integration_files_name_the_migration_they_rerun(path: Path) -> None:
    numbers = {int(p.name[:3]) for p in MIGRATIONS.glob("[0-9][0-9][0-9]_*.sql")}
    assert numbers, f"no migration file under {MIGRATIONS}"
    assert stale_refs(path.read_text(encoding="utf-8"), numbers) == []
