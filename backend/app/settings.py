"""Process configuration, read once from the environment.

Names (set by the ECS task definition in AA-CIS-Infra ``accounts/aa365/booking.tf``):

- ``DATABASE_URL``: DSN for role ``booking`` (Secrets Manager ``booking/dev/database-url``).
  Optional: without it the service starts and ``/booking/health/ready`` answers 503.
- ``DB_SSL``: ``require`` (default) or ``disable``. ``disable`` is only accepted when
  ``ENVIRONMENT`` is ``test`` or ``local`` (CI's Postgres service has no TLS).
- ``ENVIRONMENT``: ``dev`` (default), ``test`` or ``local``.
- ``GIT_SHA``: commit the image was built from (Docker build arg), echoed by ``/booking/health``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

SERVICE_NAME = "aa-booking-api"

SSL_MODES = frozenset({"require", "disable"})
INSECURE_ENVIRONMENTS = frozenset({"test", "local"})


class SettingsError(ValueError):
    """The environment holds an invalid or unsafe value."""


@dataclass(frozen=True)
class Settings:
    database_url: str | None
    db_ssl: str = "require"
    environment: str = "dev"
    git_sha: str = "unknown"

    def __post_init__(self) -> None:
        if self.db_ssl not in SSL_MODES:
            raise SettingsError(f"DB_SSL must be one of {sorted(SSL_MODES)}, got {self.db_ssl!r}")
        if self.db_ssl == "disable" and self.environment not in INSECURE_ENVIRONMENTS:
            raise SettingsError(
                f"DB_SSL=disable is only allowed when ENVIRONMENT is one of {sorted(INSECURE_ENVIRONMENTS)}"
            )


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    return Settings(
        database_url=env.get("DATABASE_URL") or None,
        db_ssl=(env.get("DB_SSL") or "require").strip().lower(),
        environment=(env.get("ENVIRONMENT") or "dev").strip().lower(),
        git_sha=(env.get("GIT_SHA") or "unknown").strip(),
    )
