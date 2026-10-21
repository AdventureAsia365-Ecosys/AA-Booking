from __future__ import annotations

import pytest

from app.settings import Settings, SettingsError, load_settings


def test_defaults_require_ssl_and_allow_missing_database_url() -> None:
    s = load_settings({})
    assert s == Settings(database_url=None, db_ssl="require", environment="dev", git_sha="unknown")


def test_reads_environment() -> None:
    s = load_settings(
        {"DATABASE_URL": "postgresql://u@h/db", "DB_SSL": "Disable", "ENVIRONMENT": "TEST", "GIT_SHA": "abc123"}
    )
    assert (s.database_url, s.db_ssl, s.environment, s.git_sha) == ("postgresql://u@h/db", "disable", "test", "abc123")


def test_empty_database_url_means_not_configured() -> None:
    assert load_settings({"DATABASE_URL": ""}).database_url is None


@pytest.mark.parametrize("environment", ["dev", "staging", "prod", ""])
def test_ssl_cannot_be_disabled_outside_test_or_local(environment: str) -> None:
    with pytest.raises(SettingsError):
        load_settings({"DB_SSL": "disable", "ENVIRONMENT": environment})


@pytest.mark.parametrize("environment", ["test", "local"])
def test_ssl_can_be_disabled_in_test_and_local(environment: str) -> None:
    assert load_settings({"DB_SSL": "disable", "ENVIRONMENT": environment}).db_ssl == "disable"


def test_unknown_ssl_mode_is_rejected() -> None:
    with pytest.raises(SettingsError):
        load_settings({"DB_SSL": "prefer"})


def test_worker_defaults_on_only_for_dev_with_a_database() -> None:
    assert load_settings({"DATABASE_URL": "postgresql://u@h/db"}).worker_enabled is True
    assert load_settings({}).worker_enabled is False
    assert load_settings({"DATABASE_URL": "postgresql://u@h/db", "ENVIRONMENT": "test"}).worker_enabled is False


@pytest.mark.parametrize(("raw", "expected"), [("true", True), ("ON", True), ("0", False), ("off", False)])
def test_worker_flag_is_explicit_when_set(raw: str, expected: bool) -> None:
    assert load_settings({"WORKER_ENABLED": raw, "ENVIRONMENT": "local"}).worker_enabled is expected


def test_worker_flag_rejects_garbage() -> None:
    with pytest.raises(SettingsError):
        load_settings({"WORKER_ENABLED": "maybe"})


def test_retired_bridge_settings_are_ignored() -> None:
    """AA-911: CIS_JWKS_URL / STAFF_JWT_ISSUER described the retired CIS bridge; a value left in the environment
    (even an unsafe one) is not read and does not fail start-up."""
    s = load_settings({"CIS_JWKS_URL": "http://cis.example/jwks", "STAFF_JWT_ISSUER": "ftp://cis.example"})
    assert not hasattr(s, "cis_jwks_url") and not hasattr(s, "staff_jwt_issuer")


def test_email_settings_default_to_no_email() -> None:
    s = load_settings({})
    assert (s.staff_notify_email, s.ses_from_address, s.ses_configuration_set) == ("", "", "")
    assert s.ses_region == "us-west-1"
    assert s.staff_admin_url == "https://booking.lumiguides.it.com"


def test_reads_email_settings() -> None:
    s = load_settings(
        {
            "STAFF_NOTIFY_EMAIL": " staff@example.test ",
            "SES_FROM_ADDRESS": "no-reply@lumiguides.it.com",
            "SES_CONFIGURATION_SET": "aa-email-dev-transactional",
            "SES_REGION": "us-east-1",
            "STAFF_ADMIN_URL": "https://admin.example.test/",
        }
    )
    assert s.staff_notify_email == "staff@example.test"
    assert s.ses_from_address == "no-reply@lumiguides.it.com"
    assert s.ses_configuration_set == "aa-email-dev-transactional"
    assert s.ses_region == "us-east-1"
    assert s.staff_admin_url == "https://admin.example.test"


@pytest.mark.parametrize(
    "env",
    [
        {"STAFF_NOTIFY_EMAIL": "a@b.test, c@d.test"},
        {"STAFF_NOTIFY_EMAIL": "not-an-address"},
        {"SES_FROM_ADDRESS": "a@@b.test"},
        {"STAFF_ADMIN_URL": "http://admin.example.test"},
    ],
)
def test_email_settings_are_validated(env: dict) -> None:
    with pytest.raises(SettingsError):
        load_settings(env)
