"""``inquiry.submitted`` (TripPlanner → AA-Booking): validation and the normalised inquiry.

Contract rule 2: a consumer accepts every minor of its major. Booking accepts version 1 and 1.1:

- ``version: 1.1`` must satisfy ``inquiry.submitted.v1.1``;
- ``version: 1`` must satisfy ``inquiry.submitted.v1`` **or** ``v1.1``. TripPlanner (PR #69) already
  sends the v1.1 shape (``customer.verified``, nullable ``customer_id``) while still labelling it
  ``1``; v1.1 is additive, so that payload is accepted rather than lost. A v1 payload without
  ``verified`` is read as unverified.

Anything else (unknown version, wrong shape) is a :class:`ContractError` — the inbox row is marked
``failed`` with the reason, never silently dropped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator

from app.contracts import load

CONTRACT = "inquiry.submitted"
SOURCE = "tripplanner"
SUPPORTED_VERSIONS = ("1", "1.1")


class ContractError(ValueError):
    """The payload does not satisfy any accepted version of the contract."""


def _validator(name: str) -> Draft202012Validator:
    return Draft202012Validator(load(name))


def _errors(validator: Draft202012Validator, payload: Any) -> list[str]:
    found = sorted(validator.iter_errors(payload), key=lambda e: list(e.absolute_path))
    return [f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}" for e in found[:5]]


def version_label(payload: Any) -> str | None:
    """'1' or '1.1' (the JSON number as a label), or None when absent / not a number."""
    if not isinstance(payload, dict):
        return None
    v = payload.get("version")
    if isinstance(v, bool) or not isinstance(v, int | float):
        return None
    return "1" if v == 1 else str(v)


def validate(payload: Any) -> str:
    """Return the accepted version label, or raise ContractError with the first few reasons."""
    label = version_label(payload)
    if label not in SUPPORTED_VERSIONS:
        raise ContractError(
            f"unsupported {CONTRACT} version {payload.get('version') if isinstance(payload, dict) else None!r}"
        )
    if label == "1.1":
        errors = _errors(_validator("inquiry.submitted.v1.1"), payload)
    else:
        errors = _errors(_validator("inquiry.submitted.v1"), payload)
        if errors and not _errors(_validator("inquiry.submitted.v1.1"), payload):
            errors = []  # v1.1 shape labelled 1 (TripPlanner PR #69): additive, accept
    if errors:
        raise ContractError(f"{CONTRACT} v{label} invalid: " + "; ".join(errors))
    return label


@dataclass(frozen=True)
class Contact:
    name: str
    email: str | None
    phone: str | None
    consent: bool
    consent_at: str | None
    verified: bool
    external_ref: str | None  # tripplanner.customers.id, when TripPlanner linked one


@dataclass(frozen=True)
class Leg:
    position: int
    source_tour_id: str
    day_from: int
    day_to: int


@dataclass(frozen=True)
class Inquiry:
    version: str
    idempotency_key: str
    trip_id: str
    handoff_id: str
    submitted_at: str
    contact: Contact | None
    legs: tuple[Leg, ...]
    snapshot: dict[str, Any] = field(default_factory=dict)


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def parse(payload: dict[str, Any]) -> Inquiry:
    """Validate, then normalise into an :class:`Inquiry` (email lower-cased, blanks to None)."""
    version = validate(payload)
    raw = payload["customer"]
    contact = None
    if raw is not None:
        email = _clean(raw.get("email"))
        contact = Contact(
            name=_clean(raw.get("name")) or "",
            email=email.lower() if email else None,
            phone=_clean(raw.get("phone")),
            consent=bool(raw.get("consent")),
            consent_at=raw.get("consent_at"),
            verified=raw.get("verified") is True,
            external_ref=_clean(raw.get("customer_id")),
        )
    itinerary = payload["itinerary"]
    legs = tuple(
        Leg(int(leg["position"]), str(leg["source_tour_id"]), int(leg["day_from"]), int(leg["day_to"]))
        for leg in sorted(itinerary["legs"], key=lambda leg: leg["position"])
    )
    return Inquiry(
        version=version,
        idempotency_key=payload["idempotency_key"],
        trip_id=payload["trip_id"],
        handoff_id=payload["handoff_id"],
        submitted_at=payload["submitted_at"],
        contact=contact,
        legs=legs,
        snapshot={"days": itinerary["days"], "custom_days": itinerary["custom_days"], "contract_version": version},
    )
