"""Unit test isolation: the catalog view feature detection (AA-986 L5) and the trip-plan destinations (AA-996) are
per-process caches; every test starts without them, so a test that scripts a ready view or a tour's places never leaks
into the next one."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from app.catalog import liveness, trip_plans


@pytest.fixture(autouse=True)
def _fresh_catalog_detection() -> Iterator[None]:
    liveness.reset_cache()
    trip_plans.DESTINATIONS_CACHE.clear()
    yield
    liveness.reset_cache()
    trip_plans.DESTINATIONS_CACHE.clear()
