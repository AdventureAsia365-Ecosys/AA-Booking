"""Unit test isolation: the catalog view feature detection (AA-986 L5) and the trip-plan destinations (AA-996) are
per-process caches; every test starts without them, so a test that scripts a ready view or a tour's places never leaks
into the next one."""

from __future__ import annotations

import os as _os
from collections.abc import Iterator

import pytest

from app.catalog import liveness, trip_plans

# S272 incident: under a git hook git exports GIT_DIR / GIT_WORK_TREE / GIT_INDEX_FILE; a test that runs git in tmp_path
# with the inherited env rewrites the real clone. No unit test may inherit them.
for _name in (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
):
    _os.environ.pop(_name, None)


@pytest.fixture(autouse=True)
def _fresh_catalog_detection() -> Iterator[None]:
    liveness.reset_cache()
    trip_plans.DESTINATIONS_CACHE.clear()
    yield
    liveness.reset_cache()
    trip_plans.DESTINATIONS_CACHE.clear()
