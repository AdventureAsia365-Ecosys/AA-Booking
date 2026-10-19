"""Booking staff issuers (AA-680 Phase 2): WORKFORCE x {booking-staff, breakglass} + the CIS bridge, env overrides,
issuer kinds and the lifespan JWKS prefetch. No network: local issuers from tests/staff_tokens.py."""

from __future__ import annotations

import asyncio
import logging

import pytest
from fastapi.testclient import TestClient

from app.auth import identity_config
from app.main import create_app
from app.settings import Settings, SettingsError, load_settings
from tests.staff_tokens import ISSUER, WORKFORCE_ISSUER, LocalCis


def test_defaults_are_the_dev_pool_clients_and_the_cis_bridge() -> None:
    settings = load_settings({})
    workforce, bridge = identity_config.booking_staff_issuers(settings)
    assert workforce.name == identity_config.WORKFORCE
    assert workforce.issuer == identity_config.DEV_WORKFORCE_ISSUER
    assert workforce.jwks_url == f"{identity_config.DEV_WORKFORCE_ISSUER}/.well-known/jwks.json"
    assert workforce.client_ids == frozenset(
        {identity_config.DEV_BOOKING_STAFF_CLIENT_ID, identity_config.DEV_BREAKGLASS_CLIENT_ID}
    )
    assert (workforce.audience_claim, workforce.token_use, workforce.groups_claim) == (
        "client_id",
        "access",
        "cognito:groups",
    )
    assert bridge.name == identity_config.BRIDGE
    assert bridge.issuer == "https://api-cis.lumiguides.it.com"
    assert bridge.jwks_url == "https://api-cis.lumiguides.it.com/auth/.well-known/jwks.json"
    assert bridge.client_ids == frozenset({"booking"})
    assert (bridge.audience_claim, bridge.token_use, bridge.groups_claim) == ("aud", None, "cis_role")


def test_cis_admin_client_is_not_accepted_by_default() -> None:
    (workforce, _) = identity_config.booking_staff_issuers(load_settings({}))
    assert "3oqkumijh04cp81k3nlnub6qtv" not in workforce.client_ids


def test_env_overrides_and_blank_values() -> None:
    settings = load_settings(
        {
            "COGNITO_WORKFORCE_ISSUER": f" {WORKFORCE_ISSUER} ",
            "COGNITO_BOOKING_STAFF_CLIENT_ID": "abc123",
            "COGNITO_BREAKGLASS_CLIENT_ID": "   ",
        }
    )
    workforce, _ = identity_config.booking_staff_issuers(settings)
    assert workforce.issuer == WORKFORCE_ISSUER
    assert workforce.client_ids == frozenset({"abc123", identity_config.DEV_BREAKGLASS_CLIENT_ID})


@pytest.mark.parametrize(
    "env",
    [
        {"COGNITO_WORKFORCE_ISSUER": "http://cognito.test/pool"},
        {"COGNITO_WORKFORCE_ISSUER": f"{WORKFORCE_ISSUER}/"},
        {"COGNITO_BOOKING_STAFF_CLIENT_ID": "not a client id"},
        {"COGNITO_BREAKGLASS_CLIENT_ID": "x/y"},
    ],
)
def test_bad_overrides_are_refused(env: dict[str, str]) -> None:
    with pytest.raises(SettingsError):
        load_settings(env)


def test_bridge_can_be_switched_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(identity_config, "BRIDGE_ENABLED", False)
    (only,) = identity_config.booking_staff_issuers(load_settings({}))
    assert only.name == identity_config.WORKFORCE


def test_issuer_kind() -> None:
    verifier = LocalCis().verifier()
    assert identity_config.issuer_kind(verifier, WORKFORCE_ISSUER) == identity_config.WORKFORCE
    assert identity_config.issuer_kind(verifier, ISSUER) == identity_config.BRIDGE
    assert identity_config.issuer_kind(verifier, "https://evil.test") is None


@pytest.mark.asyncio
async def test_prefetch_loads_both_issuers_and_never_raises(caplog: pytest.LogCaptureFixture) -> None:
    cis = LocalCis()
    with caplog.at_level(logging.INFO, logger="app.auth.identity_config"):
        assert await identity_config.prefetch_staff_jwks(cis.verifier()) == {WORKFORCE_ISSUER: 1, ISSUER: 1}
    assert caplog.text.count("identity_jwks_prefetched") == 2
    cis.fail_next = True
    assert await identity_config.prefetch_staff_jwks(cis.verifier()) == {WORKFORCE_ISSUER: 0, ISSUER: 0}

    class Broken:
        async def prefetch(self) -> dict[str, int]:
            raise RuntimeError("boom")

    assert await identity_config.prefetch_staff_jwks(Broken()) == {}  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_prefetch_task_is_off_in_test_and_runs_elsewhere() -> None:
    cis = LocalCis()
    assert identity_config.start_staff_jwks_prefetch(cis.verifier(), Settings(None, environment="test")) is None
    await identity_config.stop_staff_jwks_prefetch(None)
    task = identity_config.start_staff_jwks_prefetch(cis.verifier(), Settings(None, environment="local"))
    assert task is not None
    assert await task == {WORKFORCE_ISSUER: 1, ISSUER: 1}
    await identity_config.stop_staff_jwks_prefetch(task)


@pytest.mark.asyncio
async def test_stop_cancels_a_prefetch_still_running() -> None:
    gate = asyncio.Event()

    class Slow:
        async def prefetch(self) -> dict[str, int]:
            await gate.wait()
            return {}

    task = identity_config.start_staff_jwks_prefetch(Slow(), Settings(None, environment="local"))  # type: ignore[arg-type]
    assert task is not None
    await asyncio.sleep(0)
    await identity_config.stop_staff_jwks_prefetch(task)
    assert task.cancelled()


def test_app_lifespan_prefetches_outside_test() -> None:
    cis = LocalCis()
    settings = Settings(None, environment="local")
    with TestClient(create_app(settings, verifier=cis.verifier())) as c:
        assert c.get("/booking/health").status_code == 200
    assert cis.fetches == 2  # one JWKS per issuer, in the background at start-up
