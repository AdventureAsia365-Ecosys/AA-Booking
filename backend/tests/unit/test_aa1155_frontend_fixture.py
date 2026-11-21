"""AA-1155 T7 (root spec R4.5): the staff admin's page rules are tested against the backend capability matrix.

``frontend/lib/rbac.permissions.fixture.json`` holds each built-in role's capabilities (``app.auth.staff.PERMISSIONS``)
and the seeded custom role's (``app.auth.custom_roles.SEED_CAPABILITIES``); ``frontend/lib/rbacPermissions.test.mjs``
checks every page rule with them. This test fails when the matrix and the fixture drift. Rewrite the fixture (from
``backend/``): ``python -m tests.unit.test_aa1155_frontend_fixture``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.auth import custom_roles
from app.auth.staff import STAFF_ROLES, role_permissions

FIXTURE = Path(__file__).resolve().parents[3] / "frontend" / "lib" / "rbac.permissions.fixture.json"
COMMENT = (
    "AA-1155 T7: the backend capability matrix (backend/app/auth/staff.py PERMISSIONS) per built-in role, and the"
    " seeded custom role, for the frontend page-rule tests. Generated;"
    " backend/tests/unit/test_aa1155_frontend_fixture.py fails when it drifts"
    " (rewrite: python -m tests.unit.test_aa1155_frontend_fixture from backend/)."
)


def expected() -> dict[str, Any]:
    return {
        "_comment": COMMENT,
        "builtin": {role: sorted(role_permissions(role)) for role in STAFF_ROLES},
        "custom": {custom_roles.SEED_CODE: sorted(custom_roles.SEED_CAPABILITIES)},
    }


def render() -> str:
    return json.dumps(expected(), indent=2) + "\n"


def test_the_frontend_fixture_is_the_backend_matrix() -> None:
    assert FIXTURE.read_text(encoding="utf-8") == render(), (
        "frontend/lib/rbac.permissions.fixture.json is stale; run (from backend/) "
        "python -m tests.unit.test_aa1155_frontend_fixture"
    )


if __name__ == "__main__":
    FIXTURE.write_text(render(), encoding="utf-8")
    print(f"wrote {FIXTURE}")
