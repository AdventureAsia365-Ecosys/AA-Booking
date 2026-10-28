"""Guard: no string the API can send to a client names an internal issue ID (AA-994, rule R2).

Every module under ``app/`` is parsed and each string constant is checked, except docstrings: error details,
OpenAPI descriptions, labels and CLI help all reach a reader outside the code. Comments are not in the syntax tree,
so issue IDs stay allowed there (and in docstrings, tests and commit text). A hit: reword the string and keep the
issue reference in a comment above it.
"""

import ast
import re
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"
ISSUE_ID = re.compile(r"\bAA-\d+\b")
# Byte copy of an AA-CIS-App file (scripts/check_vendored_copies.py): never edited here.
VENDORED = {"auth/identity_verifier.py"}


def _docstring_nodes(tree: ast.AST) -> set[int]:
    docs: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docs.add(id(first.value))
    return docs


def find_issue_ids(source: str, name: str) -> list[str]:
    tree = ast.parse(source, filename=name)
    docs = _docstring_nodes(tree)
    hits = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docs
        and ISSUE_ID.search(node.value)
    ]
    hits.sort(key=lambda n: (n.lineno, n.col_offset))
    return [f"{name}:{node.lineno}: {node.value[:120]}" for node in hits]


def test_scanner_skips_comments_and_docstrings() -> None:
    sample = (
        '"""Module AA-1."""\n# AA-2\ndef f():\n    """AA-3"""\n    raise ValueError("see AA-4")\nX = f"wave {1} AA-5"\n'
    )
    hits = [h.split(": ", 1)[1] for h in find_issue_ids(sample, "sample.py")]
    assert hits == ["see AA-4", " AA-5"]


def test_no_client_visible_string_names_an_issue_id() -> None:
    files = [p for p in sorted(APP.rglob("*.py")) if p.relative_to(APP).as_posix() not in VENDORED]
    assert len(files) > 20
    hits = [hit for p in files for hit in find_issue_ids(p.read_text(encoding="utf-8"), p.relative_to(APP).as_posix())]
    assert hits == [], "issue IDs in client-visible strings:\n" + "\n".join(hits)
