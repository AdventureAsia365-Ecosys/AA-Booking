"""inquiry.submitted sample payloads, shaped like TripPlanner backend/assembly/handoff.py builds them.

Contract rule 7: consumer tests parse samples of every accepted version. ``v1_1_as_tripplanner_sends``
is what TripPlanner main emits today (PR #69): the v1.1 shape still labelled ``version: 1``.
"""

from __future__ import annotations

import copy
import uuid
from typing import Any


def _itinerary() -> dict[str, Any]:
    tour_a, tour_b = str(uuid.uuid4()), str(uuid.uuid4())
    return {
        "legs": [
            {"position": 2, "source_tour_id": tour_b, "day_from": 1, "day_to": 3},
            {"position": 1, "source_tour_id": tour_a, "day_from": 2, "day_to": 4},
        ],
        "days": [
            {
                "day": 1,
                "component_id": "c-1",
                "name": "Hanoi Old Quarter",
                "source_tour_id": tour_a,
                "source_day_index": 2,
                "destination_id": "d-1",
                "lat": 21.03,
                "lng": 105.85,
            }
        ],
        "custom_days": [{"day_id": "x-1", "kind": "free_day", "after_component_id": "c-1", "note": None}],
    }


def _envelope(version: float | int, customer: dict[str, Any] | None) -> dict[str, Any]:
    trip_id = str(uuid.uuid4())
    handoff_id = str(uuid.uuid4())
    return {
        "contract": "inquiry.submitted",
        "version": version,
        "idempotency_key": f"{trip_id}:{handoff_id}",
        "source": "tripplanner",
        "trip_id": trip_id,
        "handoff_id": handoff_id,
        "submitted_at": "2026-10-14T03:00:00+00:00",
        "customer": customer,
        "itinerary": _itinerary(),
    }


def v1(email: str | None = None) -> dict[str, Any]:
    """Strict v1: customer_id always set, no ``verified``."""
    return _envelope(
        1,
        {
            "customer_id": str(uuid.uuid4()),
            "name": "Linh Tran",
            "phone": "+84900000001",
            "email": email or f"{uuid.uuid4().hex[:10]}@example.test",
            "consent": True,
            "consent_at": "2026-10-14T02:59:00+00:00",
        },
    )


def v1_1(email: str | None = None, *, verified: bool = False, customer_id: str | None = None) -> dict[str, Any]:
    return _envelope(
        1.1,
        {
            "customer_id": customer_id,
            "name": "Minh Nguyen",
            "phone": None,
            "email": email or f"{uuid.uuid4().hex[:10]}@example.test",
            "consent": True,
            "consent_at": "2026-10-14T02:59:00Z",
            "verified": verified,
        },
    )


def v1_1_as_tripplanner_sends(email: str | None = None) -> dict[str, Any]:
    payload = v1_1(email)
    payload["version"] = 1
    return payload


def legacy_guest() -> dict[str, Any]:
    """v1 guest hand-off: customer is null (deprecated by O6)."""
    return _envelope(1, None)


def with_changes(payload: dict[str, Any], **changes: Any) -> dict[str, Any]:
    out = copy.deepcopy(payload)
    out.update(changes)
    return out
