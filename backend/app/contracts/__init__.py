"""Vendored cross-app contracts (byte copies of AA-Ecosys ``docs/contracts/*.json``).

The root registry is the source of truth (contract rule 1: the producer owns the file). Copy a new
file here byte-for-byte when a contract version is added; never edit the copies. The SHA-256 of
each copy is pinned in ``tests/unit/test_inquiry_contract.py`` so a drift is a failing test.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

CONTRACTS_DIR = Path(__file__).resolve().parent


@cache
def load(name: str) -> dict[str, Any]:
    """Parsed JSON Schema ``<name>.json`` from this folder, e.g. ``inquiry.submitted.v1.1``."""
    return json.loads((CONTRACTS_DIR / f"{name}.json").read_text(encoding="utf-8"))
