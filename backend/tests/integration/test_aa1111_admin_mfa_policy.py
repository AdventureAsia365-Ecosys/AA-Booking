"""AA-1111 S2 on a real PostgreSQL (bk-061): the administrator MFA switch is a pricing policy parameter with an
approved version 1 = false (today's behaviour), the staff page lists it in section "security" approved by the
administrator group, and app.auth.admin_mfa reads it as off. Invented staff ids only."""

from __future__ import annotations

from datetime import UTC, datetime

import asyncpg
import httpx
import pytest

from app.auth import admin_mfa
from app.pricing import policy_params, policy_values
from tests.integration.test_aa1091_pricing_policy import SALES, _item, admin, api  # noqa: F401 — fixtures by name

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_the_switch_is_seeded_off_and_listed_for_the_administrator(
    api: tuple[httpx.AsyncClient, dict],  # noqa: F811
    admin: asyncpg.Connection,  # noqa: F811
) -> None:
    policy = await policy_values.load(admin, datetime.now(UTC).date())
    assert policy.flag("staff_admin_mfa_required") is False
    assert policy_params.normalise("staff_admin_mfa_required", False, policy.values) is False
    registered = await admin.fetchval("SELECT version AS ok FROM shared.schema_versions WHERE version = 'bk-061'")
    assert registered == "bk-061"
    client, h = api
    listing = await client.get("/policy", headers=h[SALES])
    assert listing.status_code == 200, listing.text
    item = _item(listing.json(), "staff_admin_mfa_required")
    assert item["section"] == "security" and item["approver"] == "admin"
    assert item["current"]["value"] is False and item["current"]["created_by"]["id"] == "seed:bk-061"


async def test_the_check_reads_the_seeded_switch_as_off(admin: asyncpg.Connection) -> None:  # noqa: F811
    class OnePool:
        def acquire(self) -> object:
            conn = admin

            class _Ctx:
                async def __aenter__(self) -> asyncpg.Connection:
                    return conn

                async def __aexit__(self, *exc: object) -> None:
                    return None

            return _Ctx()

    admin_mfa.reset()
    try:
        assert await admin_mfa.required(OnePool()) is False  # type: ignore[arg-type]
    finally:
        admin_mfa.reset()
