"""Guard: AA-Booking makes no AI calls (AA-642 L6, root ADR 0009). A future AI feature goes through the shared AI core
gateway (route, budget, breaker, one ledger row per call), never a direct provider call. Same rule as AA-CIS-App
``tests/unit/test_aa685_no_raw_model_calls.py``, AA-TripPlanner-Web and AA-Partner.

Python under ``backend/app`` and ``backend/scripts`` is parsed with ``ast``, so comments and docstrings that name these
methods do not count. What counts as a model call:

- a Bedrock runtime or batch method, called or passed uncalled (``asyncio.to_thread(client.converse, ...)``):
  invoke_model(_with_response_stream), converse(_stream), apply_guardrail, create_model_invocation_job;
- a vendor SDK call: ``<x>.completions`` / ``.messages`` / ``.responses`` / ``.embeddings`` ``.create/.stream/.parse``;
- importing the vendor SDK (``openai``, ``anthropic``);
- a vendor endpoint in a string: api.anthropic.com, api.openai.com, bedrock-runtime (an httpx / requests URL, or a
  boto3 "bedrock-runtime" client).

The staff frontend must not import a vendor AI SDK or name a vendor endpoint either. ALLOWED is empty: the first AI
feature adds its gateway module there with a reason, in the PR that brings the gateway.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
PY_ROOTS = ("backend/app", "backend/scripts")
RAW_BEDROCK_METHODS = frozenset(
    {
        "invoke_model",
        "invoke_model_with_response_stream",
        "converse",
        "converse_stream",
        "apply_guardrail",
        "create_model_invocation_job",
    }
)
SDK_RESOURCES = frozenset({"completions", "messages", "responses", "embeddings"})
SDK_VERBS = frozenset({"create", "stream", "parse"})
VENDOR_MODULES = frozenset({"openai", "anthropic"})
VENDOR_HOSTS = ("api.anthropic.com", "api.openai.com", "bedrock-runtime")

# path -> reason. Empty: Booking has no AI call. Ratcheted: an entry whose file has no hit fails.
ALLOWED: dict[str, str] = {}

FRONTEND = REPO / "frontend"
FRONTEND_SUFFIXES = frozenset({".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"})
FRONTEND_SKIP = frozenset({"node_modules", ".next", "out", "coverage", "test-results", "playwright-report"})
FRONTEND_BYPASS = re.compile(
    r"""(?:from\s+|require\(\s*|import\(\s*)['"](?:openai|@anthropic-ai/[\w-]+|@aws-sdk/client-bedrock[\w-]*"""
    r"""|@ai-sdk/[\w-]+)['"]|api\.anthropic\.com|api\.openai\.com|bedrock-runtime"""
)


def _docstring_nodes(tree: ast.AST) -> set[int]:
    docs: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docs.add(id(first.value))
    return docs


def find_bypasses(source: str) -> list[tuple[int, str]]:
    """(line, what) for every gateway bypass in one Python module's source."""
    tree = ast.parse(source)
    docs = _docstring_nodes(tree)
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            hits.extend((node.lineno, f"import {a.name}") for a in node.names if a.name.split(".")[0] in VENDOR_MODULES)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and (node.module or "").split(".")[0] in VENDOR_MODULES:
                hits.append((node.lineno, f"import {node.module}"))
        elif isinstance(node, ast.Attribute):
            if node.attr in RAW_BEDROCK_METHODS:
                hits.append((node.lineno, f".{node.attr}"))
            elif node.attr in SDK_VERBS and isinstance(node.value, ast.Attribute) and node.value.attr in SDK_RESOURCES:
                hits.append((node.lineno, f".{node.value.attr}.{node.attr}"))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            hits.extend((node.lineno, f"host {host}") for host in VENDOR_HOSTS if host in node.value)
    return hits


def _scan() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for root in PY_ROOTS:
        for path in sorted((REPO / root).rglob("*.py")):
            hits = find_bypasses(path.read_text(encoding="utf-8"))
            if hits:
                found[path.relative_to(REPO).as_posix()] = [f"{line} {what}" for line, what in hits]
    return found


def test_no_model_call_in_booking() -> None:
    bad = [f"{rel}:{hit}" for rel, hits in _scan().items() if rel not in ALLOWED for hit in hits]
    assert not bad, (
        "Booking makes no AI calls; a new one goes through the shared AI core gateway (root ADR 0009), "
        "then its gateway module goes into ALLOWED with a reason:\n" + "\n".join(bad)
    )


def test_allowlist_entries_still_match() -> None:
    found = _scan()
    stale = [rel for rel in ALLOWED if rel not in found]
    assert not stale, f"stale ALLOWED entries (file gone or no model call left, remove them): {stale}"


def test_frontend_never_calls_a_model() -> None:
    bad = []
    for path in sorted(FRONTEND.rglob("*")):
        rel = path.relative_to(REPO)
        if path.suffix not in FRONTEND_SUFFIXES or FRONTEND_SKIP.intersection(rel.parts) or not path.is_file():
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if FRONTEND_BYPASS.search(line):
                bad.append(f"{rel}:{n} {line.strip()[:120]}")
    assert not bad, "The staff frontend must not call a model:\n" + "\n".join(bad)


# The detectors themselves, on invented snippets.
_BYPASSES = {
    "rt.converse(modelId=m, messages=[])": ".converse",
    "asyncio.to_thread(rt.converse, modelId=m, messages=[])": ".converse",
    "rt.converse_stream(modelId=m)": ".converse_stream",
    "rt.invoke_model(body=b, modelId=m)": ".invoke_model",
    "rt.apply_guardrail(guardrailIdentifier=g, source='INPUT', content=[])": ".apply_guardrail",
    "bedrock.create_model_invocation_job(jobName=j)": ".create_model_invocation_job",
    "client.chat.completions.create(model=m, messages=[])": ".completions.create",
    "client.messages.create(model=m, max_tokens=5, messages=[])": ".messages.create",
    "client.responses.create(model=m, input='hi')": ".responses.create",
    "client.embeddings.create(model=m, input=['hi'])": ".embeddings.create",
    "import openai": "import openai",
    "from anthropic import Anthropic": "import anthropic",
    "httpx.post('https://api.anthropic.com/v1/messages', json={})": "host api.anthropic.com",
    "requests.post(f'https://api.openai.com/v1/{path}', json={})": "host api.openai.com",
    "boto3.client('bedrock-runtime', region_name='us-west-1')": "host bedrock-runtime",
}


def test_detector_catches_each_bypass_pattern() -> None:
    for snippet, expected in _BYPASSES.items():
        assert [what for _, what in find_bypasses(snippet)] == [expected], snippet


def test_detector_ignores_docstrings_comments_and_unrelated_calls() -> None:
    source = '''
"""Calls api.openai.com through the gateway; never rt.converse() here."""
def f(db, inbox):
    """bedrock-runtime is only named in this docstring."""
    # client.messages.create(...) in a comment
    inbox.messages.send("x")
    db.create(table="t")
    return "converse"
'''
    assert find_bypasses(source) == []


def test_frontend_detector() -> None:
    assert FRONTEND_BYPASS.search('import OpenAI from "openai";')
    assert FRONTEND_BYPASS.search("import Anthropic from '@anthropic-ai/sdk';")
    assert FRONTEND_BYPASS.search('const { BedrockRuntimeClient } = require("@aws-sdk/client-bedrock-runtime");')
    assert FRONTEND_BYPASS.search('fetch("https://api.openai.com/v1/responses")')
    assert not FRONTEND_BYPASS.search('fetch("/api/booking/trips")')
    assert not FRONTEND_BYPASS.search("Payments run on our own gateway, no Amazon Bedrock call.")
