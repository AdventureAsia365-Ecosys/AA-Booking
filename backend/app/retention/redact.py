"""AA-825 — what ``bk_retention_inbox_redact`` keeps of an ``inquiry.submitted`` inbox row (DM-6.6). Pure functions.

The payload is **rebuilt from an allowlist**, not scrubbed with a denylist: only keys of the contract (v1 / v1.1 / v1.2,
``backend/app/contracts/``) that carry no personal data survive, and every other key is dropped. This matters most for
``failed`` rows, whose payload by definition did not pass the contract and can hold personal data anywhere (an extra
key, a ``customer`` that is a string, an unknown contact field of a future version). A denylist would leave that data in
place while ``redacted_at`` says the row is clean.

Kept: the envelope ``contract``, ``version``, ``idempotency_key``, ``source``, ``trip_id``, ``handoff_id``,
``submitted_at``; ``customer.customer_id`` (TripPlanner's id, the link an erasure request follows) and the consent
record ``consent`` / ``consent_at`` / ``verified`` (DM-6.5); the itinerary legs, days and custom days with their
contract keys. Set to ``null`` (key kept, so a conforming payload keeps its shape): ``customer.name`` (pii:name),
``customer.email`` / ``customer.phone`` (pii:contact), ``itinerary.custom_days[].note`` (pii:free_text). An allowed key
whose value is an object or an array (the contract only has scalars there) becomes ``null``. A ``customer`` or
``itinerary`` that is not an object, a list item that is not an object, and a payload that is not an object all become
``null``.

Reported paths name what held a value and was removed; dropped keys are reported as ``*`` (``customer.*``,
``itinerary.days[0].*``) so a key name from an untrusted payload never reaches the job result or the audit log.

A ``failed`` row's ``error`` holds the contract validation message, which can quote payload values: it is replaced by
:data:`REDACTED_ERROR`. ``parked`` rows carry fixed reasons written by the intake (no personal data) and keep them.
"""

from __future__ import annotations

from typing import Any

ENVELOPE_KEYS = ("contract", "version", "idempotency_key", "source", "trip_id", "handoff_id", "submitted_at")
CUSTOMER_KEEP = ("customer_id", "consent", "consent_at", "verified")
CUSTOMER_FIELDS = ("name", "email", "phone")
LEG_KEYS = ("position", "source_tour_id", "source_system", "source_id", "day_from", "day_to")
DAY_KEYS = ("day", "component_id", "name", "source_tour_id", "source_day_index", "destination_id", "lat", "lng")
CUSTOM_DAY_KEEP = ("day_id", "kind", "after_component_id")
CUSTOM_DAY_FIELDS = ("note",)
REDACTED_ERROR = "contract validation error (details redacted after 90 days, DM-6.6)"

_SCALARS = (str, int, float, bool)


def _scalar(value: Any, path: str, paths: list[str]) -> Any:
    """``value`` when it is JSON null or a scalar; otherwise ``None`` and ``path`` is reported."""
    if value is None or isinstance(value, _SCALARS):
        return value
    paths.append(path)
    return None


def _pick(src: dict[str, Any], keep: tuple[str, ...], nulls: tuple[str, ...], path: str, paths: list[str]) -> dict:
    """A new object with the scalar values of ``keep`` (when present), every ``nulls`` key set to ``None``, and nothing
    else."""
    out: dict[str, Any] = {}
    for key in keep:
        if key in src:
            out[key] = _scalar(src[key], f"{path}.{key}", paths)
    for key in nulls:
        if src.get(key) is not None:
            paths.append(f"{path}.{key}")
        out[key] = None
    allowed = set(keep) | set(nulls)
    if any(key not in allowed and value is not None for key, value in src.items()):
        paths.append(f"{path}.*")
    return out


def _items(value: Any, keep: tuple[str, ...], nulls: tuple[str, ...], path: str, paths: list[str]) -> list | None:
    if value is None:
        return None
    if not isinstance(value, list):
        paths.append(path)
        return None
    out: list[Any] = []
    for i, item in enumerate(value):
        if isinstance(item, dict):
            out.append(_pick(item, keep, nulls, f"{path}[{i}]", paths))
        else:
            if item is not None:
                paths.append(f"{path}[{i}]")
            out.append(None)
    return out


def redact_payload(payload: Any) -> tuple[Any, list[str]]:
    """Return ``(rebuilt payload, paths that held a value and were removed)``; the input is not modified. Running it on
    its own output changes nothing and reports nothing."""
    paths: list[str] = []
    if not isinstance(payload, dict):
        if payload is not None:
            paths.append("$")
        return None, paths

    out: dict[str, Any] = {}
    for key in ENVELOPE_KEYS:
        if key in payload:
            out[key] = _scalar(payload[key], key, paths)

    if "customer" in payload:
        customer = payload["customer"]
        if isinstance(customer, dict):
            out["customer"] = _pick(customer, CUSTOMER_KEEP, CUSTOMER_FIELDS, "customer", paths)
        else:
            if customer is not None:
                paths.append("customer")
            out["customer"] = None

    if "itinerary" in payload:
        itinerary = payload["itinerary"]
        if isinstance(itinerary, dict):
            rebuilt: dict[str, Any] = {}
            for key, keep, nulls in (
                ("legs", LEG_KEYS, ()),
                ("days", DAY_KEYS, ()),
                ("custom_days", CUSTOM_DAY_KEEP, CUSTOM_DAY_FIELDS),
            ):
                if key in itinerary:
                    rebuilt[key] = _items(itinerary[key], keep, nulls, f"itinerary.{key}", paths)
            if any(k not in ("legs", "days", "custom_days") and v is not None for k, v in itinerary.items()):
                paths.append("itinerary.*")
            out["itinerary"] = rebuilt
        else:
            if itinerary is not None:
                paths.append("itinerary")
            out["itinerary"] = None

    if any(key not in out and value is not None for key, value in payload.items()):
        paths.append("*")
    return out, paths


def redact_error(status: str, error: str | None) -> str | None:
    if status == "failed" and error:
        return REDACTED_ERROR
    return error
