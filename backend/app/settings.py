"""Process configuration, read once from the environment.

Names (set by the ECS task definition in AA-CIS-Infra ``accounts/aa365/booking.tf``):

- ``DATABASE_URL``: DSN for role ``booking`` (Secrets Manager ``booking/dev/database-url``).
  Optional: without it the service starts and ``/booking/health/ready`` answers 503.
- ``DB_SSL``: ``require`` (default) or ``disable``. ``disable`` is only accepted when
  ``ENVIRONMENT`` is ``test`` or ``local`` (CI's Postgres service has no TLS).
- ``ENVIRONMENT``: ``dev`` (default), ``test`` or ``local``.
- ``GIT_SHA``: commit the image was built from (Docker build arg), echoed by ``/booking/health``.
- ``APP_VERSION``: release version the image was built at (Docker build arg read from ``.release-please-manifest.json``
  by the deploy, AA-891), echoed by ``/booking/health`` next to ``git_sha``.
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

Cognito M2M (AA-972, root ADR 0006b decision 7; ``app.auth.m2m``): service-to-service tokens of the ``aa-m2m-<env>``
pool for the B2B API ``/booking/b2b/v1`` (contract booking.b2b.v1). Set by AA-CIS-Infra ``booking.tf`` from the pool
and the AA-Partner (CRM) app client; both empty (default) = every B2B route answers 503 ``unavailable`` (fail closed).

- ``COGNITO_EXTERNAL_ISSUER``: the EXTERNAL (traveller) pool issuer (AA-908). The inquiry intake accepts a contract v1.4
  ``customer.identity`` only from this issuer; any other one fails the hand-off. Default
  :data:`~app.auth.identity_config.DEV_EXTERNAL_ISSUER`. Never set on Dev. It must differ from the WORKFORCE and M2M
  issuers. Since AA-682 S5 it is also the issuer of the customer routes ``/booking/customer/v1``
  (``app.auth.customer``).
- ``COGNITO_TRIPPLANNER_CLIENT_ID`` / ``COGNITO_BOOKING_CUSTOMER_CLIENT_ID``: the two EXTERNAL clients the customer
  routes accept (AA-682 S5). Defaults :data:`~app.auth.identity_config.DEV_TRIPPLANNER_CLIENT_ID` /
  :data:`~app.auth.identity_config.DEV_BOOKING_CUSTOMER_CLIENT_ID`; never set on Dev. Letters and digits, and never a
  staff or M2M client.

- ``COGNITO_M2M_ISSUER``: the M2M pool issuer (``https://cognito-idp.<region>.amazonaws.com/<pool id>``).
- ``COGNITO_M2M_CRM_CLIENT_ID``: the AA-Partner (CRM) client-credentials app client. Required with the issuer.

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

Supplier photos (AA-835 S4, ``app.supply.media``; Terraform ``accounts/aa365/booking_media.tf`` creates the bucket and
grants the task role ``s3:PutObject`` / ``s3:GetObject`` on ``supply-media/*``):

- ``SUPPLY_MEDIA_BUCKET``: the Booking supply-media bucket (``aa-booking-<env>-supply-media-<account>``, the bk-023
  naming CHECK). Empty (default) = uploads answer 503 ``media_storage_unavailable`` and listed photos carry no URL.
- ``SUPPLY_MEDIA_REGION``: default ``us-west-1`` (the bucket's region; presigned URLs are signed for it).

Operations documents (AA-682 S1, ``app.ops.files``; AA-CIS-Infra ``booking_ops_docs.tf`` creates the private ops-docs
bucket, SSE-S3, objects reachable by the task role only, and grants it ``s3:PutObject`` / ``s3:GetObject`` on
``ops-docs/*``):

- ``OPS_DOCS_BUCKET``: the Booking ops-docs bucket (``aa-booking-<env>-ops-docs-<account>``, the bk-026 naming CHECK).
  Empty (default) = uploads answer 503 ``ops_storage_unavailable`` and downloads are unavailable.
- ``OPS_DOCS_REGION``: default ``us-west-1``.

Error tracking (AA-871, ``app.observability.sentry``):

- ``SENTRY_DSN``: the Sentry project DSN (task definition secret). Empty (default) = Sentry off. Must be ``https://``.
  Never logged.
- ``SENTRY_REQUIRED``: ``true`` makes start-up fail when ``SENTRY_DSN`` is empty (the missing-DSN guard). Default off,
  so a deploy before the secret exists still starts; Infra turns it on together with the secret.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from app.auth.identity_config import (
    DEV_BOOKING_CUSTOMER_CLIENT_ID,
    DEV_BOOKING_STAFF_CLIENT_ID,
    DEV_BOOKING_STAFF_PREVIEW_CLIENT_ID,
    DEV_BREAKGLASS_CLIENT_ID,
    DEV_EXTERNAL_ISSUER,
    DEV_TRIPPLANNER_CLIENT_ID,
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
DEFAULT_SUPPLY_MEDIA_REGION = "us-west-1"
# The bk-023 CHECK on booking.entity_media.s3_bucket: a wrong name fails at start-up, not on the first upload.
SUPPLY_MEDIA_BUCKET_RE = re.compile(r"^aa-booking-[a-z0-9]+-supply-media-[0-9]{12}$")
# The bk-026 CHECK on booking.ops_files.s3_bucket.
OPS_DOCS_BUCKET_RE = re.compile(r"^aa-booking-[a-z0-9]+-ops-docs-[0-9]{12}$")


class SettingsError(ValueError):
    """The environment holds an invalid or unsafe value."""


@dataclass(frozen=True)
class Settings:
    database_url: str | None
    db_ssl: str = "require"
    environment: str = "dev"
    git_sha: str = "unknown"
    app_version: str = "unknown"
    worker_enabled: bool = False
    # AA-993 G4 proof: JOB_TEST_KINDS registers the Dev-only test kind bk_noop_sleep (app.jobs.noop_sleep). Like the
    # CIS env of the same name, any value other than 1/true/yes/on means off (never a start-up error).
    job_test_kinds: bool = False
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
    cognito_m2m_issuer: str = ""
    cognito_m2m_crm_client_id: str = ""
    supply_media_bucket: str = ""
    supply_media_region: str = DEFAULT_SUPPLY_MEDIA_REGION
    ops_docs_bucket: str = ""
    ops_docs_region: str = DEFAULT_SUPPLY_MEDIA_REGION
    cognito_external_issuer: str = DEV_EXTERNAL_ISSUER
    cognito_tripplanner_client_id: str = DEV_TRIPPLANNER_CLIENT_ID
    cognito_booking_customer_client_id: str = DEV_BOOKING_CUSTOMER_CLIENT_ID
    sentry_dsn: str = field(default="", repr=False)
    sentry_required: bool = False

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
        m2m_issuer, m2m_client = self.cognito_m2m_issuer, self.cognito_m2m_crm_client_id
        if m2m_issuer and (not m2m_issuer.startswith("https://") or m2m_issuer.endswith("/")):
            raise SettingsError(
                f"COGNITO_M2M_ISSUER must be an https:// URL without a trailing slash, got {m2m_issuer!r}"
            )
        if m2m_client and not m2m_client.isalnum():
            raise SettingsError(
                f"COGNITO_M2M_CRM_CLIENT_ID must be a Cognito app client id (letters and digits), got {m2m_client!r}"
            )
        if bool(m2m_issuer) != bool(m2m_client):
            raise SettingsError("COGNITO_M2M_ISSUER and COGNITO_M2M_CRM_CLIENT_ID are set together or not at all")
        if m2m_issuer and m2m_issuer == self.cognito_workforce_issuer:
            # One verifier per route class: a staff token must never satisfy the M2M check (or the reverse).
            raise SettingsError("COGNITO_M2M_ISSUER must differ from COGNITO_WORKFORCE_ISSUER")
        if self.supply_media_bucket and not SUPPLY_MEDIA_BUCKET_RE.fullmatch(self.supply_media_bucket):
            raise SettingsError(
                "SUPPLY_MEDIA_BUCKET must name the supply-media bucket (aa-booking-<env>-supply-media-<account>), "
                f"got {self.supply_media_bucket!r}"
            )
        if not re.fullmatch(r"[a-z]{2}(-[a-z]+)+-[0-9]", self.supply_media_region):
            raise SettingsError(f"SUPPLY_MEDIA_REGION must be an AWS region, got {self.supply_media_region!r}")
        if self.ops_docs_bucket and not OPS_DOCS_BUCKET_RE.fullmatch(self.ops_docs_bucket):
            raise SettingsError(
                "OPS_DOCS_BUCKET must name the ops-docs bucket (aa-booking-<env>-ops-docs-<account>), "
                f"got {self.ops_docs_bucket!r}"
            )
        if not re.fullmatch(r"[a-z]{2}(-[a-z]+)+-[0-9]", self.ops_docs_region):
            raise SettingsError(f"OPS_DOCS_REGION must be an AWS region, got {self.ops_docs_region!r}")
        external = self.cognito_external_issuer
        if not external.startswith("https://") or external.endswith("/") or len(external) > 300:
            raise SettingsError(
                "COGNITO_EXTERNAL_ISSUER must be an https:// URL without a trailing slash, at most 300 characters,"
                f" got {external!r}"
            )
        if external in (self.cognito_workforce_issuer, m2m_issuer):
            # A staff or service identity must never pass as a signed-in traveller.
            raise SettingsError(
                "COGNITO_EXTERNAL_ISSUER must differ from COGNITO_WORKFORCE_ISSUER and COGNITO_M2M_ISSUER"
            )
        customer_clients = (
            ("COGNITO_TRIPPLANNER_CLIENT_ID", self.cognito_tripplanner_client_id),
            ("COGNITO_BOOKING_CUSTOMER_CLIENT_ID", self.cognito_booking_customer_client_id),
        )
        for name, client_id in customer_clients:
            if not client_id or not client_id.isalnum():
                raise SettingsError(f"{name} must be a Cognito app client id (letters and digits), got {client_id!r}")
            if client_id in (
                self.cognito_booking_staff_client_id,
                self.cognito_breakglass_client_id,
                self.cognito_booking_staff_preview_client_id,
                m2m_client,
            ):
                # One client, one route class: a traveller client never doubles as a staff or service client.
                raise SettingsError(f"{name} must differ from the staff and M2M clients")
        if self.sentry_dsn and not self.sentry_dsn.startswith("https://"):
            raise SettingsError("SENTRY_DSN must be an https:// URL")  # the value is never echoed
        if self.sentry_required and not self.sentry_dsn:
            raise SettingsError("SENTRY_REQUIRED is on but SENTRY_DSN is empty")


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
        app_version=(env.get("APP_VERSION") or "unknown").strip(),
        worker_enabled=_flag(
            env.get("WORKER_ENABLED"), environment == "dev" and database_url is not None, "WORKER_ENABLED"
        ),
        job_test_kinds=(env.get("JOB_TEST_KINDS") or "").strip().lower() in TRUE_VALUES,
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
        cognito_m2m_issuer=(env.get("COGNITO_M2M_ISSUER") or "").strip(),
        cognito_m2m_crm_client_id=(env.get("COGNITO_M2M_CRM_CLIENT_ID") or "").strip(),
        supply_media_bucket=(env.get("SUPPLY_MEDIA_BUCKET") or "").strip(),
        supply_media_region=(env.get("SUPPLY_MEDIA_REGION") or DEFAULT_SUPPLY_MEDIA_REGION).strip(),
        ops_docs_bucket=(env.get("OPS_DOCS_BUCKET") or "").strip(),
        ops_docs_region=(env.get("OPS_DOCS_REGION") or DEFAULT_SUPPLY_MEDIA_REGION).strip(),
        cognito_external_issuer=(env.get("COGNITO_EXTERNAL_ISSUER") or "").strip() or DEV_EXTERNAL_ISSUER,
        cognito_tripplanner_client_id=(env.get("COGNITO_TRIPPLANNER_CLIENT_ID") or "").strip()
        or DEV_TRIPPLANNER_CLIENT_ID,
        cognito_booking_customer_client_id=(env.get("COGNITO_BOOKING_CUSTOMER_CLIENT_ID") or "").strip()
        or DEV_BOOKING_CUSTOMER_CLIENT_ID,
        sentry_dsn=(env.get("SENTRY_DSN") or "").strip(),
        sentry_required=_flag(env.get("SENTRY_REQUIRED"), False, "SENTRY_REQUIRED"),
    )
