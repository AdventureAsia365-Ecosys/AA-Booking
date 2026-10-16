"""AA-825 — what ``bk_retention_inbox_redact`` removes from an ``inquiry.submitted`` inbox row (DM-6.6). Pure functions.

Personal data in the contract (v1 / v1.1 / v1.2, ``backend/app/contracts/``): ``customer.name`` (pii:name),
``customer.email`` and ``customer.phone`` (pii:contact), ``itinerary.custom_days[].note`` (pii:free_text, a traveller's
own words). They are set to ``null``; the keys stay so the payload keeps its shape for any reader. Kept on purpose:
``customer.customer_id`` (TripPlanner's id, no personal data by itself and the link an erasure request follows),
``consent`` / ``consent_at`` / ``verified`` (the consent record, DM-6.5), the idempotency key, ids, dates and the
itinerary (places, tours, days).

A ``failed`` row's ``error`` holds the contract validation message, which can quote payload values: it is replaced by
:data:`REDACTED_ERROR`. ``parked`` rows carry fixed reasons written by the intake (no personal data) and keep them.
"""

from __future__ import annotations

import copy
from typing import Any

CUSTOMER_FIELDS = ("name", "email", "phone")
REDACTED_ERROR = "contract validation error (details redacted after 90 days, DM-6.6)"


def redact_payload(payload: Any) -> tuple[Any, list[str]]:
    """Return ``(redacted copy, paths that held a value)``; the input is not modified. A payload that is not an object
    comes back unchanged with no paths."""
    if not isinstance(payload, dict):
        return payload, []
    out = copy.deepcopy(payload)
    paths: list[str] = []
    customer = out.get("customer")
    if isinstance(customer, dict):
        for field in CUSTOMER_FIELDS:
            if customer.get(field) is not None:
                paths.append(f"customer.{field}")
            if field in customer:
                customer[field] = None
    itinerary = out.get("itinerary")
    custom_days = itinerary.get("custom_days") if isinstance(itinerary, dict) else None
    if isinstance(custom_days, list):
        for i, day in enumerate(custom_days):
            if isinstance(day, dict) and "note" in day:
                if day["note"] is not None:
                    paths.append(f"itinerary.custom_days[{i}].note")
                day["note"] = None
    return out, paths


def redact_error(status: str, error: str | None) -> str | None:
    if status == "failed" and error:
        return REDACTED_ERROR
    return error
