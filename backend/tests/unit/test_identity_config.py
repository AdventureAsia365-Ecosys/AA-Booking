"""Booking staff issuers (AA-680 Phase 2, AA-911): WORKFORCE x {booking-staff, breakglass} only, env overrides,
issuer kinds and the lifespan JWKS prefetch. No network: local issuers from tests/staff_tokens.py."""

from __future__ import annotations

import asyncio
import logging

import pytest
from fastapi.testclient import TestClient

from app.auth import identity_config
from app.main import create_app
from app.settings import Settings, SettingsError, load_settings
from tests.staff_tokens import BRIDGE_ISSUER, WORKFORCE_ISSUER, LocalStaff


def test_defaults_are_the_dev_pool_and_clients_and_nothing_else() -> None:
    settings = load_settings({})
    (workforce,) = identity_config.booking_staff_issuers(settings)
    assert workforce.name == identity_config.WORKFORCE
    assert workforce.issuer == identity_config.DEV_WORKFORCE_ISSUER
    assert workforce.jwks_url == f"{identity_config.DEV_WORKFORCE_ISSUER}/.well-known/jwks.json"
    # AA-911 D-1: plus the Preview-only booking-staff-preview client once its id is filled in (empty until then).
    preview = {identity_config.DEV_BOOKING_STAFF_PREVIEW_CLIENT_ID} - {""}
    assert workforce.client_ids == frozenset(
        {identity_config.DEV_BOOKING_STAFF_CLIENT_ID, identity_config.DEV_BREAKGLASS_CLIENT_ID} | preview
    )
    assert (workforce.audience_claim, workforce.token_use, workforce.groups_claim) == (
        "client_id",
        "access",
        "cognito:groups",
    )


def test_the_cis_bridge_issuer_is_gone() -> None:
    """AA-911: the issuer set is exactly {WORKFORCE}; the bridge constants and config builder no longer exist."""
    issuers = identity_config.booking_staff_issuers(load_settings({}))
    assert {cfg.name for cfg in issuers} == {identity_config.WORKFORCE}
    assert all(cfg.issuer != BRIDGE_ISSUER for cfg in issuers)
    for name in ("BRIDGE", "BRIDGE_ENABLED", "BRIDGE_AUDIENCE", "bridge_config"):
        assert not hasattr(identity_config, name)


def test_cis_admin_client_is_not_accepted_by_default() -> None:
    (workforce,) = identity_config.booking_staff_issuers(load_settings({}))
    assert "3oqkumijh04cp81k3nlnub6qtv" not in workforce.client_ids


def test_env_overrides_and_blank_values() -> None:
    settings = load_settings(
        {
            "COGNITO_WORKFORCE_ISSUER": f" {WORKFORCE_ISSUER} ",
            "COGNITO_BOOKING_STAFF_CLIENT_ID": "abc123",
            "COGNITO_BREAKGLASS_CLIENT_ID": "   ",
        }
    )
    (workforce,) = identity_config.booking_staff_issuers(settings)
    assert workforce.issuer == WORKFORCE_ISSUER
    preview = {identity_config.DEV_BOOKING_STAFF_PREVIEW_CLIENT_ID} - {""}
    assert workforce.client_ids == frozenset({"abc123", identity_config.DEV_BREAKGLASS_CLIENT_ID} | preview)


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


def test_issuer_kind() -> None:
    verifier = LocalStaff().verifier()
    assert identity_config.issuer_kind(verifier, WORKFORCE_ISSUER) == identity_config.WORKFORCE
    assert identity_config.issuer_kind(verifier, BRIDGE_ISSUER) is None
    assert identity_config.issuer_kind(verifier, "https://evil.test") is None


@pytest.mark.asyncio
async def test_prefetch_loads_the_workforce_keys_and_never_raises(caplog: pytest.LogCaptureFixture) -> None:
    staff = LocalStaff()
    with caplog.at_level(logging.INFO, logger="app.auth.identity_config"):
        assert await identity_config.prefetch_staff_jwks(staff.verifier()) == {WORKFORCE_ISSUER: 1}
    assert caplog.text.count("identity_jwks_prefetched") == 1
    staff.fail_next = True
    assert await identity_config.prefetch_staff_jwks(staff.verifier()) == {WORKFORCE_ISSUER: 0}

    class Broken:
        async def prefetch(self) -> dict[str, int]:
            raise RuntimeError("boom")

    assert await identity_config.prefetch_staff_jwks(Broken()) == {}  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_prefetch_task_is_off_in_test_and_runs_elsewhere() -> None:
    staff = LocalStaff()
    assert identity_config.start_staff_jwks_prefetch(staff.verifier(), Settings(None, environment="test")) is None
    await identity_config.stop_staff_jwks_prefetch(None)
    task = identity_config.start_staff_jwks_prefetch(staff.verifier(), Settings(None, environment="local"))
    assert task is not None
    assert await task == {WORKFORCE_ISSUER: 1}
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
    staff = LocalStaff()
    settings = Settings(None, environment="local")
    with TestClient(create_app(settings, verifier=staff.verifier())) as c:
        assert c.get("/booking/health").status_code == 200
    assert staff.fetched == [f"{WORKFORCE_ISSUER}/.well-known/jwks.json"]  # WORKFORCE only, at start-up
