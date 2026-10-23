"""Unit test isolation: the catalog view feature detection (AA-986 L5) is a per-process cache; every test starts
without it, so a test that scripts a ready view never leaks into the next one."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from app.catalog import liveness


@pytest.fixture(autouse=True)
def _fresh_catalog_detection() -> Iterator[None]:
    liveness.reset_cache()
    yield
    liveness.reset_cache()
