"""Process configuration, read once from the environment.

Names (set by the ECS task definition in AA-CIS-Infra ``accounts/aa365/booking.tf``):

- ``DATABASE_URL``: DSN for role ``booking`` (Secrets Manager ``booking/dev/database-url``).
  Optional: without it the service starts and ``/booking/health/ready`` answers 503.
- ``DB_SSL``: ``require`` (default) or ``disable``. ``disable`` is only accepted when
  ``ENVIRONMENT`` is ``test`` or ``local`` (CI's Postgres service has no TLS).
- ``ENVIRONMENT``: ``dev`` (default), ``test`` or ``local``.
- ``GIT_SHA``: commit the image was built from (Docker build arg), echoed by ``/booking/health``.
- ``WORKER_ENABLED``: ``true`` / ``false``. Runs the ``booking.job`` worker loop inside the API
  process (Dev, AA-681 slice 2). Default: on when ``ENVIRONMENT`` is ``dev`` and ``DATABASE_URL``
  is set, off otherwise (tests and local runs start it explicitly).
- ``CIS_JWKS_URL``: JWKS of the CIS staff login (ADR 0004 O1, AA-680). Default: the Dev value
  ``https://api-cis.lumiguides.it.com/auth/.well-known/jwks.json``.
- ``STAFF_JWT_ISSUER``: expected ``iss`` of staff JWTs. Default: ``https://api-cis.lumiguides.it.com``.
  Both must be ``https://`` unless ``ENVIRONMENT`` is ``test`` or ``local``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

SERVICE_NAME = "aa-booking-api"

SSL_MODES = frozenset({"require", "disable"})
INSECURE_ENVIRONMENTS = frozenset({"test", "local"})
TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_VALUES = frozenset({"0", "false", "no", "off"})

DEFAULT_STAFF_JWT_ISSUER = "https://api-cis.lumiguides.it.com"
DEFAULT_CIS_JWKS_URL = f"{DEFAULT_STAFF_JWT_ISSUER}/auth/.well-known/jwks.json"


class SettingsError(ValueError):
    """The environment holds an invalid or unsafe value."""


@dataclass(frozen=True)
class Settings:
    database_url: str | None
    db_ssl: str = "require"
    environment: str = "dev"
    git_sha: str = "unknown"
    worker_enabled: bool = False
    cis_jwks_url: str = DEFAULT_CIS_JWKS_URL
    staff_jwt_issuer: str = DEFAULT_STAFF_JWT_ISSUER

    def __post_init__(self) -> None:
        if self.db_ssl not in SSL_MODES:
            raise SettingsError(f"DB_SSL must be one of {sorted(SSL_MODES)}, got {self.db_ssl!r}")
        if self.db_ssl == "disable" and self.environment not in INSECURE_ENVIRONMENTS:
            raise SettingsError(
                f"DB_SSL=disable is only allowed when ENVIRONMENT is one of {sorted(INSECURE_ENVIRONMENTS)}"
            )
        for name, value in (("CIS_JWKS_URL", self.cis_jwks_url), ("STAFF_JWT_ISSUER", self.staff_jwt_issuer)):
            if value.startswith("https://"):
                continue
            if value.startswith("http://") and self.environment in INSECURE_ENVIRONMENTS:
                continue
            raise SettingsError(f"{name} must be an https:// URL, got {value!r}")


def _flag(raw: str | None, default: bool, name: str) -> bool:
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in TRUE_VALUES:
        return True
    if value in FALSE_VALUES:
        return False
    raise SettingsError(f"{name} must be true or false, got {raw!r}")


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    database_url = env.get("DATABASE_URL") or None
    environment = (env.get("ENVIRONMENT") or "dev").strip().lower()
    return Settings(
        database_url=database_url,
        db_ssl=(env.get("DB_SSL") or "require").strip().lower(),
        environment=environment,
        git_sha=(env.get("GIT_SHA") or "unknown").strip(),
        worker_enabled=_flag(
            env.get("WORKER_ENABLED"), environment == "dev" and database_url is not None, "WORKER_ENABLED"
        ),
        cis_jwks_url=(env.get("CIS_JWKS_URL") or DEFAULT_CIS_JWKS_URL).strip(),
        staff_jwt_issuer=(env.get("STAFF_JWT_ISSUER") or DEFAULT_STAFF_JWT_ISSUER).strip(),
    )
