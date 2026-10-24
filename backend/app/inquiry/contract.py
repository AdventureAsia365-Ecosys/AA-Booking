"""``inquiry.submitted`` (TripPlanner → AA-Booking): validation and the normalised inquiry.

Contract rule 2: a consumer accepts every minor of its major. Booking accepts version 1, 1.1, 1.2, 1.3 and 1.4:

- ``version: 1.4`` must satisfy ``inquiry.submitted.v1.4`` (AA-908, root ADR 0006 Phase 4: ``customer.identity`` is
  the verified sign-in identity ``{iss, sub}`` of a traveller signed in on the Cognito EXTERNAL pool, or null for a
  guest; ``verified`` is true only together with an identity, and then ``email`` is the token's address);
- ``version: 1.3`` must satisfy ``inquiry.submitted.v1.3`` (AA-986 L6, root ADR 0015 decision 5: every leg and day
  also says ``available`` + ``reason`` + ``reason_detail``; stops whose tour AA no longer serves come after the served
  ones with ``available: false``, so staff see why such a leg cannot be matched; the snapshot keeps the leg flags
  under ``legs``);

- ``version: 1.2`` must satisfy ``inquiry.submitted.v1.2`` (AA-830: each leg also carries the catalog tour key
  ``source_system`` + ``source_id``, both null when TripPlanner could not resolve it);
- ``version: 1.1`` must satisfy ``inquiry.submitted.v1.1``;
- ``version: 1`` must satisfy ``inquiry.submitted.v1`` **or** ``v1.1``. TripPlanner (PR #69) already
  sends the v1.1 shape (``customer.verified``, nullable ``customer_id``) while still labelling it
  ``1``; v1.1 is additive, so that payload is accepted rather than lost. A v1 payload without
  ``verified`` is read as unverified. Legs of a v1 / v1.1 payload carry no catalog key: intake resolves
  ``source_tour_id`` against ``catalog.v_tour`` (``app.inquiry.leg_keys``).

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
SUPPORTED_VERSIONS = ("1", "1.1", "1.2", "1.3", "1.4")


class ContractError(ValueError):
    """The payload does not satisfy any accepted version of the contract."""


def _validator(name: str) -> Draft202012Validator:
    return Draft202012Validator(load(name))


def _errors(validator: Draft202012Validator, payload: Any) -> list[str]:
    found = sorted(validator.iter_errors(payload), key=lambda e: list(e.absolute_path))
    return [f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}" for e in found[:5]]


def version_label(payload: Any) -> str | None:
    """'1', '1.1', '1.2', '1.3' or '1.4' (the JSON number as a label), or None when absent / not a number."""
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
    if label in ("1.1", "1.2", "1.3", "1.4"):
        errors = _errors(_validator(f"inquiry.submitted.v{label}"), payload)
    else:
        errors = _errors(_validator("inquiry.submitted.v1"), payload)
        if errors and not _errors(_validator("inquiry.submitted.v1.1"), payload):
            errors = []  # v1.1 shape labelled 1 (TripPlanner PR #69): additive, accept
    if errors:
        raise ContractError(f"{CONTRACT} v{label} invalid: " + "; ".join(errors))
    return label


@dataclass(frozen=True)
class Identity:
    """v1.4: the traveller's verified sign-in identity (Cognito EXTERNAL access token); key on iss AND sub."""

    iss: str
    sub: str


@dataclass(frozen=True)
class Contact:
    name: str
    email: str | None
    phone: str | None
    consent: bool
    consent_at: str | None
    verified: bool
    external_ref: str | None  # tripplanner.customers.id, when TripPlanner linked one
    # v1.4 (AA-908): set for a signed-in traveller, None for a guest and for v1 - v1.3. ``verified`` is true only
    # together with it in a v1.4 payload (schema).
    identity: Identity | None = None

    @property
    def signed_in(self) -> bool:
        """A verified, signed-in traveller with an email: the only contact intake links to a verified customer."""
        return self.identity is not None and self.verified and bool(self.email)


@dataclass(frozen=True)
class Leg:
    position: int
    source_tour_id: str
    day_from: int
    day_to: int
    # Catalog tour key (contract catalog.tour.v1). Set from a v1.2 payload; None for v1 / v1.1 or when TripPlanner
    # could not resolve it (intake then resolves source_tour_id itself).
    source_system: str | None = None
    source_id: str | None = None
    # AA-986 L6 (v1.3): does AA still serve this leg's tour? True for v1 / v1.1 / v1.2 (no flag sent). reason:
    # retired | paused | stop_removed | not_served (None when available); reason_detail: the CIS lifecycle reason.
    available: bool = True
    reason: str | None = None
    reason_detail: str | None = None


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
        ident = raw.get("identity")
        contact = Contact(
            name=_clean(raw.get("name")) or "",
            email=email.lower() if email else None,
            phone=_clean(raw.get("phone")),
            consent=bool(raw.get("consent")),
            consent_at=raw.get("consent_at"),
            verified=raw.get("verified") is True,
            external_ref=_clean(raw.get("customer_id")),
            # Only a v1.4 payload can carry it (the customer object of v1 - v1.3 is closed); checked again here.
            identity=Identity(str(ident["iss"]), str(ident["sub"]))
            if version == "1.4" and isinstance(ident, dict)
            else None,
        )
    itinerary = payload["itinerary"]
    legs = tuple(
        Leg(
            int(leg["position"]),
            str(leg["source_tour_id"]),
            int(leg["day_from"]),
            int(leg["day_to"]),
            _clean(leg.get("source_system")),
            _clean(leg.get("source_id")),
            leg.get("available") is not False,
            _clean(leg.get("reason")),
            _clean(leg.get("reason_detail")),
        )
        for leg in sorted(itinerary["legs"], key=lambda leg: leg["position"])
    )
    snapshot: dict[str, Any] = {
        "days": itinerary["days"],
        "custom_days": itinerary["custom_days"],
        "contract_version": version,
    }
    if version in ("1.3", "1.4"):
        # AA-986 L6 review: the leg-level flags are kept too (trip_case_legs has no column for them until slice L5),
        # so staff and the "new inquiry" e-mail can tell which leg AA no longer serves, by position.
        snapshot["legs"] = [
            {
                "position": leg.position,
                "source_tour_id": leg.source_tour_id,
                "available": leg.available,
                "reason": leg.reason,
                "reason_detail": leg.reason_detail,
            }
            for leg in legs
        ]
    return Inquiry(
        version=version,
        idempotency_key=payload["idempotency_key"],
        trip_id=payload["trip_id"],
        handoff_id=payload["handoff_id"],
        submitted_at=payload["submitted_at"],
        contact=contact,
        legs=legs,
        snapshot=snapshot,
    )
