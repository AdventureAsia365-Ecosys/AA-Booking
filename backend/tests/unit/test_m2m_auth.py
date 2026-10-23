"""AA-972: M2M settings and token checks (app.auth.m2m) with the real vendored verifier and a local M2M pool."""

from __future__ import annotations

import pytest

from app.auth import m2m
from app.auth.identity_verifier import AuthUnavailable, InvalidToken
from app.settings import Settings, SettingsError, load_settings
from tests.staff_tokens import (
    CRM_CLIENT,
    M2M_ISSUER,
    OTHER_M2M_CLIENT,
    WORKFORCE_ISSUER,
    LocalM2M,
    LocalWorkforce,
)


def test_settings_m2m_default_off_and_env() -> None:
    assert load_settings({}).cognito_m2m_issuer == ""
    assert m2m.build_m2m_verifier(load_settings({})) is None
    s = load_settings({"COGNITO_M2M_ISSUER": f" {M2M_ISSUER} ", "COGNITO_M2M_CRM_CLIENT_ID": CRM_CLIENT})
    assert (s.cognito_m2m_issuer, s.cognito_m2m_crm_client_id) == (M2M_ISSUER, CRM_CLIENT)
    cfg = m2m.m2m_config(s)
    assert cfg is not None and cfg.client_ids == frozenset({CRM_CLIENT}) and cfg.groups_claim == "scope"
    assert cfg.token_use == "access" and cfg.audience_claim == "client_id"


@pytest.mark.parametrize(
    "env",
    [
        {"COGNITO_M2M_ISSUER": M2M_ISSUER},  # client missing
        {"COGNITO_M2M_CRM_CLIENT_ID": CRM_CLIENT},  # issuer missing
        {"COGNITO_M2M_ISSUER": "http://cognito.test/pool", "COGNITO_M2M_CRM_CLIENT_ID": CRM_CLIENT},
        {"COGNITO_M2M_ISSUER": M2M_ISSUER + "/", "COGNITO_M2M_CRM_CLIENT_ID": CRM_CLIENT},
        {"COGNITO_M2M_ISSUER": M2M_ISSUER, "COGNITO_M2M_CRM_CLIENT_ID": "bad id!"},
    ],
)
def test_settings_m2m_refuses_bad_values(env: dict[str, str]) -> None:
    with pytest.raises(SettingsError):
        load_settings(env)


def test_settings_m2m_issuer_must_differ_from_workforce() -> None:
    with pytest.raises(SettingsError):
        Settings(
            database_url=None,
            environment="test",
            cognito_workforce_issuer=WORKFORCE_ISSUER,
            cognito_m2m_issuer=WORKFORCE_ISSUER,
            cognito_m2m_crm_client_id=CRM_CLIENT,
        )


@pytest.mark.asyncio
async def test_valid_token_gives_client_and_scopes() -> None:
    pool = LocalM2M()
    caller = await m2m.verify_caller(pool.verifier(), pool.token("booking.b2b/read  booking.b2b/quote"))
    assert caller.client_id == CRM_CLIENT
    assert caller.scopes == frozenset({m2m.SCOPE_READ, m2m.SCOPE_QUOTE})
    assert caller.has(m2m.SCOPE_QUOTE) and not caller.has(m2m.SCOPE_HOLD)


@pytest.mark.asyncio
async def test_no_scope_claim_means_no_scopes() -> None:
    pool = LocalM2M()
    caller = await m2m.verify_caller(pool.verifier(), pool.token(None))
    assert caller.scopes == frozenset()


@pytest.mark.asyncio
async def test_wrong_client_is_refused_not_invalid() -> None:
    """Pins the vendored verifier's 'client not allowed' message: a validly signed token of another client = 403."""
    pool = LocalM2M()
    with pytest.raises(m2m.CallerRefused):
        await m2m.verify_caller(pool.verifier(), pool.token(client_id=OTHER_M2M_CLIENT))


@pytest.mark.asyncio
async def test_wrong_client_with_bad_signature_is_invalid() -> None:
    """The client check comes after the signature: a forged token of another client stays a 401."""
    pool, forger = LocalM2M(), LocalM2M()
    with pytest.raises(InvalidToken) as exc:
        await m2m.verify_caller(pool.verifier(), pool.token(client_id=OTHER_M2M_CLIENT, key=forger.private_key))
    assert not isinstance(exc.value, m2m.CallerRefused)
    assert str(exc.value) != m2m.CLIENT_NOT_ALLOWED


@pytest.mark.asyncio
async def test_user_token_of_the_pool_is_refused() -> None:
    pool = LocalM2M()
    with pytest.raises(m2m.CallerRefused):
        await m2m.verify_caller(pool.verifier(), pool.token(sub="7f0c-a-person"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"token_use": "id"},
        {"exp_in": -600},
        {"iss": WORKFORCE_ISSUER},
    ],
)
async def test_bad_tokens_are_invalid(kwargs: dict) -> None:
    pool = LocalM2M()
    with pytest.raises(InvalidToken):
        await m2m.verify_caller(pool.verifier(), pool.token(**kwargs))


@pytest.mark.asyncio
async def test_staff_token_is_not_an_m2m_token() -> None:
    pool = LocalM2M()
    staff = LocalWorkforce().token(["booking-admin"])
    with pytest.raises(InvalidToken):
        await m2m.verify_caller(pool.verifier(), staff)


@pytest.mark.asyncio
async def test_jwks_never_loaded_is_unavailable() -> None:
    pool = LocalM2M()
    pool.fail = True
    with pytest.raises(AuthUnavailable):
        await m2m.verify_caller(pool.verifier(), pool.token())
