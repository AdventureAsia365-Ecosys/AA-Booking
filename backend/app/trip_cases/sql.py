"""SQL fragments and row shaping shared by the trip case and version modules.

Kept apart so ``service`` (case detail lists versions) and ``versions`` (needs ``resolve_case``) do not
import each other.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

# Derived version badge (S236 UI spec, 04 C3): approved (locked) > superseded (a later version was sent,
# or another version is approved) > sent > draft. Soft-deleted drafts never count.
VERSION_BADGE = """
    CASE
        WHEN v.is_approved THEN 'approved'
        WHEN EXISTS (
            SELECT 1 FROM booking.trip_case_versions o
            WHERE o.trip_case_id = v.trip_case_id AND o.id <> v.id AND o.deleted_at IS NULL
              AND (o.is_approved OR (o.is_sent_to_customer AND o.version_number > v.version_number))
        ) THEN 'superseded'
        WHEN v.is_sent_to_customer THEN 'sent'
        ELSE 'draft'
    END
"""
VERSION_BADGES = ("draft", "sent", "approved", "superseded")

VERSION_COLUMNS = f"""
    v.id::text AS id, v.version_number, v.title, v.notes, v.total_price::text AS total_price, v.currency,
    v.channel, v.valid_until, v.fx_rate_locked::text AS fx_rate_locked, v.fx_as_of,
    v.is_sent_to_customer, v.sent_at, v.sent_by, v.is_approved, v.approved_at, v.approved_by, v.approval_note,
    v.created_by, v.created_at, v.updated_by, v.updated_at,
    {VERSION_BADGE} AS badge
"""

# Legs of a case as JSON text, in day order (the TripPlanner chain).
LEGS_JSON = """
    (SELECT coalesce(json_agg(json_build_object(
                'position', l.position, 'source_tour_id', l.source_tour_id, 'day_from', l.day_from,
                'day_to', l.day_to, 'source_system', l.source_system, 'source_id', l.source_id)
                ORDER BY l.day_from, l.position), '[]'::json)::text
     FROM booking.trip_case_legs l WHERE l.trip_case_id = tc.id) AS legs
"""
COUNTRIES_ARRAY = """
    (SELECT coalesce(array_agg(cc.country_code::text ORDER BY cc.position, cc.country_code), '{}')
     FROM booking.trip_case_countries cc WHERE cc.trip_case_id = tc.id) AS countries
"""


def version_dict(row: Any) -> dict[str, Any]:
    """A version row as the API returns it: the FX lock folded into ``fx_locked``."""
    out = dict(row)
    rate, as_of = out.pop("fx_rate_locked", None), out.pop("fx_as_of", None)
    out["fx_locked"] = {"rate": rate, "as_of": as_of} if rate is not None else None
    return out


def leg_title(snapshot: dict[str, Any] | None, source_tour_id: str) -> str:
    """First named snapshot day of that tour, in day order; a generic title otherwise.

    The CIS catalog title (``catalog.v_tour``) replaces this once the CIS grant lands (Q11, AA-679)."""
    days = (snapshot or {}).get("days") or []
    named = [
        d
        for d in days
        if isinstance(d, dict) and d.get("source_tour_id") == source_tour_id and isinstance(d.get("name"), str)
    ]
    named.sort(key=lambda d: d.get("day") if isinstance(d.get("day"), int) else 0)
    for day in named:
        name = day["name"].strip()
        if name:
            return name[:500]
    return leg_fallback_title(source_tour_id)


def leg_fallback_title(source_tour_id: str) -> str:
    """The title of a leg whose tour has no named snapshot day (``app.trip_cases.cis_readback`` never compares it)."""
    return f"AA tour {source_tour_id}"[:500]


def trip_shape(snapshot: dict[str, Any] | None, legs: list[dict[str, Any]], start: date | None) -> dict[str, Any]:
    """``trip_title`` (first leg, None = custom trip), ``leg_titles``, ``duration_days`` and a derived end date."""
    ordered = sorted(legs, key=lambda leg: (leg["day_from"], leg["position"]))
    titles = [leg_title(snapshot, leg["source_tour_id"]) for leg in ordered]
    duration = max((leg["day_to"] for leg in ordered), default=None)
    return {
        "trip_title": titles[0] if titles else None,
        "leg_titles": titles,
        "leg_count": len(titles),
        "duration_days": duration,
        "trip_end_date": start + timedelta(days=duration - 1) if start and duration else None,
    }


def loads(raw: str | None, default: Any = None) -> Any:
    return json.loads(raw) if raw else default
