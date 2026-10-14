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
