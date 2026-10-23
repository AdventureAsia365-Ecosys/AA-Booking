"""inquiry.submitted sample payloads, shaped like TripPlanner backend/assembly/handoff.py builds them.

Contract rule 7: consumer tests parse samples of every accepted version. ``v1_1_as_tripplanner_sends``
is what TripPlanner emitted before AA-805 (PR #69): the v1.1 shape still labelled ``version: 1``; ``v1_2`` is
what it emitted from AA-830 on; ``v1_3`` what it emits from AA-986 L6 on.
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


def v1_2(email: str | None = None, *, verified: bool = False, keys: list[str | None] | None = None) -> dict[str, Any]:
    """v1.2 (AA-830): v1.1 plus the catalog tour key per leg. ``keys`` gives each leg's source_id in ``position``
    order (None = TripPlanner could not resolve it: both fields null); default: a fresh source_id per leg."""
    payload = v1_1(email, verified=verified)
    payload["version"] = 1.2
    legs = payload["itinerary"]["legs"]
    keys = keys if keys is not None else [f"src-{uuid.uuid4()}" for _ in legs]
    by_position = dict(zip(sorted(leg["position"] for leg in legs), keys, strict=True))
    payload["itinerary"]["legs"] = [
        {
            "position": leg["position"],
            "source_tour_id": leg["source_tour_id"],
            "source_system": "cis" if by_position[leg["position"]] else None,
            "source_id": by_position[leg["position"]],
            "day_from": leg["day_from"],
            "day_to": leg["day_to"],
        }
        for leg in legs
    ]
    return payload


def v1_3(email: str | None = None, *, unavailable: str | None = None) -> dict[str, Any]:
    """v1.3 (AA-986 L6): v1.2 plus ``available`` / ``reason`` / ``reason_detail`` on every leg and day. With
    ``unavailable`` (a reason), one more leg and day of a tour AA no longer serves follow the served ones."""
    payload = v1_2(email)
    payload["version"] = 1.3
    served = {"available": True, "reason": None, "reason_detail": None}
    itinerary = payload["itinerary"]
    itinerary["legs"] = [leg | served for leg in itinerary["legs"]]
    itinerary["days"] = [day | served for day in itinerary["days"]]
    if unavailable:
        gone, flag = (
            f"src-{uuid.uuid4()}",
            {"available": False, "reason": unavailable, "reason_detail": "source_deleted"},
        )
        itinerary["legs"].append(
            {
                "position": len(itinerary["legs"]) + 1,
                "source_tour_id": gone,
                "source_system": "cis",
                "source_id": gone,
                "day_from": 3,
                "day_to": 4,
            }
            | flag
        )
        itinerary["days"].append(
            {
                "day": 2,
                "component_id": "c-gone",
                "name": "Hue Citadel",
                "source_tour_id": gone,
                "source_day_index": 3,
                "destination_id": None,
                "lat": 16.47,
                "lng": 107.58,
            }
            | flag
        )
    return payload


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
