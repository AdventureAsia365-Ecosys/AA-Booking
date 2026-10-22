"""PII and secret redaction shared by the JSON log renderer and Sentry (AA-871, data standards DM-6.4).

Two layers:

- **by key**: a dict value under a key that names personal data or a credential (``email``, ``phone``, ``name``,
  ``password``, ``token``, ``authorization``, ...) is replaced whole by :data:`FILTERED`;
- **by pattern**: inside any string, e-mail addresses, ``+``-prefixed phone numbers, Bearer tokens, JWTs, key-like
  query parameters (``?api_key=…``, ``token=…``) and Postgres error details such as ``Key (email)=(a@b.c)`` are
  redacted.

Free-text names cannot be found by a pattern; the rule stays "ids only in log lines". This module is the safety net.
Bare digit runs are not treated as phone numbers on purpose (ids, counts and timestamps would be lost).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

FILTERED = "[Filtered]"

# Whole-key matches (lower-cased, '-' folded to '_'). Kept explicit so ids such as `request_id` or `job_id` survive.
_SENSITIVE_EXACT = frozenset(
    {
        "name",
        "first_name",
        "last_name",
        "full_name",
        "given_name",
        "family_name",
        "customer_name",
        "contact_name",
        "display_name",
        "email",
        "e_mail",
        "email_address",
        "phone",
        "phone_number",
        "mobile",
        "telephone",
        "address",
        "street",
        "passport",
        "dob",
        "date_of_birth",
        "otp",
        "code_hash",
        "ip",
        "ip_address",
        "cookie",
        "cookies",
        "set_cookie",
        "authorization",
        "proxy_authorization",
        "x_admin_secret",
        "x_api_key",
        "x_tripplanner_key",
        "x_tripplanner_guest",
        "dsn",
        "database_url",
    }
)
# Substring matches: any key containing one of these is a credential.
_SENSITIVE_PARTS = ("password", "passwd", "secret", "token", "api_key", "apikey", "private_key", "credential")

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<![\w+])\+\d[\d\s().-]{6,18}\d")
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*")
# AA-712 (CIS sentry_init): key-like query parameters, plus key=value pairs in log text.
_SECRET_PARAM = re.compile(
    r"(?i)\b((?:api_?key|apikey|access_token|refresh_token|id_token|token|key|password|secret|code)=)[^&\s'\"]+"
)
# Postgres unique-violation detail: Key (email)=(a@b.c) already exists.
_PG_DETAIL = re.compile(r"(?i)\(((?:[a-z_]*(?:email|phone|name|address)[a-z_]*)(?:,\s*[a-z_]+)*)\)=\([^)]*\)")


def is_sensitive_key(key: object) -> bool:
    k = str(key).strip().lower().replace("-", "_")
    return k in _SENSITIVE_EXACT or any(part in k for part in _SENSITIVE_PARTS)


def scrub_text(text: str) -> str:
    """Redact personal data and credentials inside one string."""
    if not text:
        return text
    text = _JWT.sub("[jwt]", text)
    text = _BEARER.sub(lambda m: f"{m.group(1)} {FILTERED}", text)
    text = _SECRET_PARAM.sub(lambda m: f"{m.group(1)}***", text)
    text = _PG_DETAIL.sub(lambda m: f"({m.group(1)})=({FILTERED})", text)
    text = _EMAIL.sub("[email]", text)
    return _PHONE.sub("[phone]", text)


def scrub(value: Any, *, keep: frozenset[str] = frozenset(), _depth: int = 0) -> Any:
    """Return a redacted copy of ``value`` (dicts, lists, tuples and strings; other types unchanged).

    Keys in ``keep`` are copied as they are (fields this code writes itself, e.g. ``request_id``)."""
    if _depth > 12:
        return value
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, Mapping):
        out = {}
        for k, v in value.items():
            if k in keep:
                out[k] = v
            elif is_sensitive_key(k):
                out[k] = FILTERED
            else:
                out[k] = scrub(v, _depth=_depth + 1)
        return out
    if isinstance(value, list):
        return [scrub(v, _depth=_depth + 1) for v in value]
    if isinstance(value, tuple):
        return tuple(scrub(v, _depth=_depth + 1) for v in value)
    return value
