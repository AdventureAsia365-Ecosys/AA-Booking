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
  Both must be ``https://`` unless ``ENVIRONMENT`` is ``test`` or ``local``. Both describe the CIS **bridge** token,
  accepted during the Cognito transition only (AA-680 PR-4 removes them).

Cognito WORKFORCE staff sign-in (AA-680 Phase 2, root ADR 0006 §8; ``app.auth.identity_config``). All optional and
**never set on Dev**: the defaults are the Dev pool and clients (code constants, public values). Same names as CIS.

- ``COGNITO_WORKFORCE_ISSUER``: the WORKFORCE pool issuer (``https://cognito-idp.<region>.amazonaws.com/<pool id>``).
- ``COGNITO_BOOKING_STAFF_CLIENT_ID``: the ``booking-staff`` app client (the Booking staff UI's sign-in).
- ``COGNITO_BREAKGLASS_CLIENT_ID``: the ``breakglass`` app client.
- ``COGNITO_BOOKING_STAFF_PREVIEW_CLIENT_ID``: the Preview-only ``booking-staff-preview`` app client (AA-911 D-1).
  Default :data:`~app.auth.identity_config.DEV_BOOKING_STAFF_PREVIEW_CLIENT_ID`; empty = no preview client accepted.
  Its sessions are capped at read-only. It must differ from the two clients above.

Transactional e-mail (AA-811, SES on acc2; Terraform ``accounts/aa365/ses.tf`` grants the task role
``ses:SendEmail`` as ``SES_FROM_ADDRESS`` only):

- ``STAFF_NOTIFY_EMAIL``: inbox that receives the "new inquiry" e-mail (``bk_notify_email``). Empty
  (default) = no e-mail; the job succeeds as ``skipped`` with the reason.
- ``SES_FROM_ADDRESS``: the From address. Empty (default) = no e-mail, same as above.
- ``SES_CONFIGURATION_SET``: SES configuration set (events + metrics). Empty = omitted (the domain
  identity's default set still applies).
- ``SES_REGION``: default ``us-west-1`` (the SES identity's region).
- ``STAFF_ADMIN_URL``: base URL of the staff admin, for links in staff e-mail. Default: the Dev
  custom domain ``https://booking.lumiguides.it.com``. Must be ``https://`` outside ``test``/``local``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from app.auth.identity_config import (
    DEV_BOOKING_STAFF_CLIENT_ID,
    DEV_BOOKING_STAFF_PREVIEW_CLIENT_ID,
    DEV_BREAKGLASS_CLIENT_ID,
    DEV_WORKFORCE_ISSUER,
)

SERVICE_NAME = "aa-booking-api"

SSL_MODES = frozenset({"require", "disable"})
INSECURE_ENVIRONMENTS = frozenset({"test", "local"})
TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_VALUES = frozenset({"0", "false", "no", "off"})

DEFAULT_STAFF_JWT_ISSUER = "https://api-cis.lumiguides.it.com"
DEFAULT_CIS_JWKS_URL = f"{DEFAULT_STAFF_JWT_ISSUER}/auth/.well-known/jwks.json"
DEFAULT_SES_REGION = "us-west-1"
DEFAULT_STAFF_ADMIN_URL = "https://booking.lumiguides.it.com"


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
    staff_notify_email: str = ""
    ses_from_address: str = ""
    ses_configuration_set: str = ""
    ses_region: str = DEFAULT_SES_REGION
    staff_admin_url: str = DEFAULT_STAFF_ADMIN_URL
    cognito_workforce_issuer: str = DEV_WORKFORCE_ISSUER
    cognito_booking_staff_client_id: str = DEV_BOOKING_STAFF_CLIENT_ID
    cognito_breakglass_client_id: str = DEV_BREAKGLASS_CLIENT_ID
    cognito_booking_staff_preview_client_id: str = DEV_BOOKING_STAFF_PREVIEW_CLIENT_ID

    def __post_init__(self) -> None:
        if self.db_ssl not in SSL_MODES:
            raise SettingsError(f"DB_SSL must be one of {sorted(SSL_MODES)}, got {self.db_ssl!r}")
        if self.db_ssl == "disable" and self.environment not in INSECURE_ENVIRONMENTS:
            raise SettingsError(
                f"DB_SSL=disable is only allowed when ENVIRONMENT is one of {sorted(INSECURE_ENVIRONMENTS)}"
            )
        for name, address in (
            ("STAFF_NOTIFY_EMAIL", self.staff_notify_email),
            ("SES_FROM_ADDRESS", self.ses_from_address),
        ):
            if address and (address.count("@") != 1 or any(c.isspace() for c in address)):
                raise SettingsError(f"{name} must be a single e-mail address, got {address!r}")
        for name, value in (
            ("CIS_JWKS_URL", self.cis_jwks_url),
            ("STAFF_JWT_ISSUER", self.staff_jwt_issuer),
            ("STAFF_ADMIN_URL", self.staff_admin_url),
        ):
            if value.startswith("https://"):
                continue
            if value.startswith("http://") and self.environment in INSECURE_ENVIRONMENTS:
                continue
            raise SettingsError(f"{name} must be an https:// URL, got {value!r}")
        issuer = self.cognito_workforce_issuer
        if not issuer.startswith("https://") or issuer.endswith("/"):
            raise SettingsError(
                f"COGNITO_WORKFORCE_ISSUER must be an https:// URL without a trailing slash, got {issuer!r}"
            )
        for name, client_id in (
            ("COGNITO_BOOKING_STAFF_CLIENT_ID", self.cognito_booking_staff_client_id),
            ("COGNITO_BREAKGLASS_CLIENT_ID", self.cognito_breakglass_client_id),
        ):
            if not client_id or not client_id.isalnum():
                raise SettingsError(f"{name} must be a Cognito app client id (letters and digits), got {client_id!r}")
        preview = self.cognito_booking_staff_preview_client_id
        if preview:
            if not preview.isalnum():
                raise SettingsError(
                    "COGNITO_BOOKING_STAFF_PREVIEW_CLIENT_ID must be a Cognito app client id (letters and digits) "
                    f"or empty, got {preview!r}"
                )
            if preview in (self.cognito_booking_staff_client_id, self.cognito_breakglass_client_id):
                # The same id would cap Production sign-ins too, or (worse, after a later fix) un-cap Preview ones.
                raise SettingsError("COGNITO_BOOKING_STAFF_PREVIEW_CLIENT_ID must differ from the production clients")


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
        staff_notify_email=(env.get("STAFF_NOTIFY_EMAIL") or "").strip(),
        ses_from_address=(env.get("SES_FROM_ADDRESS") or "").strip(),
        ses_configuration_set=(env.get("SES_CONFIGURATION_SET") or "").strip(),
        ses_region=(env.get("SES_REGION") or DEFAULT_SES_REGION).strip(),
        staff_admin_url=(env.get("STAFF_ADMIN_URL") or DEFAULT_STAFF_ADMIN_URL).strip().rstrip("/"),
        cognito_workforce_issuer=(env.get("COGNITO_WORKFORCE_ISSUER") or "").strip() or DEV_WORKFORCE_ISSUER,
        cognito_booking_staff_client_id=(env.get("COGNITO_BOOKING_STAFF_CLIENT_ID") or "").strip()
        or DEV_BOOKING_STAFF_CLIENT_ID,
        cognito_breakglass_client_id=(env.get("COGNITO_BREAKGLASS_CLIENT_ID") or "").strip()
        or DEV_BREAKGLASS_CLIENT_ID,
        cognito_booking_staff_preview_client_id=(env.get("COGNITO_BOOKING_STAFF_PREVIEW_CLIENT_ID") or "").strip()
        or DEV_BOOKING_STAFF_PREVIEW_CLIENT_ID,
    )
