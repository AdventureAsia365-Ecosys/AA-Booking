"""Components suggested from the CIS itinerary (AA-1091 item 2): for each day of a tour, the hotel room type of the
night and the services the day's text asks for, matched to Booking's existing ``HTL-`` / ``SVC-`` items with an
explained score. A read only: nothing is applied here. Staff accept, edit or reject each suggestion in the staff app,
which then saves the plan's whole set with the existing ``PUT …/components`` (``app.b2b.plan_components``: same
checks, one audit row). Rules, no LLM (root ADR 0009 decision 13: Booking makes no AI call).

**What is read** (role ``booking``, contract views only, never CIS internals):

- ``catalog.v_tour`` by the catalog key: the CIS ``tour_id``, title, country and days;
- ``catalog.v_tour_day`` **by ``tour_id`` only** (a parse-backed view, the AA-996 perf guard): day title, text, places;
- ``catalog.v_tour_day_route`` by ``tour_id`` (CIS 253, feature-detected: without it, or when it fails, the night's
  place is the day's last place and the suggestion says so in ``degraded``): the overnight place and its CIS
  destination id;
- Booking's own active hotels with their active room types and active services, of the tour's country
  (``booking.countries`` by name) or at one of the tour's overnight destinations, plus services made from this tour
  in CIS (``services.source_id``); at most :data:`MAX_CANDIDATES` rows each.

**Needs per day**: a hotel for every night (days 1 .. days - 1; a one-day tour has none); a service per type the day's
text names (:data:`KEYWORDS`: transfer, guide, entrance, activity, and a meal for lunch or dinner; breakfast is in the
room rate, so it is never a need). A need already met by a component of the plan on that day is ``covered``.

**Score** (0..1, every point with its reason, so staff can see why): hotel = named in the day's text (0.6) or most of
its name words in it (0.3-0.5), at the night's CIS destination (0.3), the night's place in its name or address (0.2),
in the tour's country (0.05); service = made from this tour in CIS (0.5), its title words in the day's text (up to
0.4), at one of the day's destinations (0.3), in the tour's country (0.05). A candidate under :data:`MIN_SCORE` is
never suggested (a guide needs only the country: :data:`MIN_GUIDE_SCORE`); the best is ``suggestion``, the next ones
``alternatives`` (at most :data:`ALTERNATIVES`). The room type of a suggested hotel is its double or twin (the default
room mix is doubles), else the first by name; every active room type is listed for an edit. A need with no candidate
is listed in ``unmatched`` so staff can create the supplier, hotel or service.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import asyncpg

from app.b2b import plan_components
from app.catalog import liveness
from app.catalog.destinations import CATALOG_ERRORS
from app.errors import NotFound

MAX_CANDIDATES = 3000
ALTERNATIVES = 3
MIN_SCORE = 0.2
MIN_GUIDE_SCORE = 0.05
HOTEL = "hotel"
# The words of a day's text that ask for a service of a type (lower case, accents removed; whole words or phrases).
KEYWORDS: dict[str, tuple[str, ...]] = {
    "transfer": ("transfer", "transfers", "pick up", "pickup", "pick-up", "drop off", "drive", "airport", "by car"),
    "guide": ("guide", "guided", "tour leader", "escorted"),
    "entrance": ("entrance", "entrance fee", "ticket", "tickets", "admission"),
    "activity": (
        "cruise",
        "trek",
        "trekking",
        "hike",
        "hiking",
        "kayak",
        "kayaking",
        "cycling",
        "bike ride",
        "boat trip",
        "rafting",
        "cooking class",
        "snorkelling",
        "snorkeling",
        "show",
    ),
    "meal": ("lunch", "dinner"),
}
SERVICE_TYPES: tuple[str, ...] = tuple(KEYWORDS)
# Words that say what kind of place or service it is, not which one (never count as a name match).
GENERIC = frozenset(
    """hotel hotels resort resorts lodge inn the and spa boutique homestay guesthouse guest house villa villas camp
    by de la le of at in a an retreat hostel suites suite residence private shared transfer transfers guide guided tour
    tours service services day days half full english speaking local ticket tickets entrance fee fees lunch dinner
    breakfast meal meals with from to for per person group car van bus airport return way one standard deluxe
    superior room rooms""".split()
)
ROOM_PREFERRED = ("double", "twin")

TOUR_SQL = """
SELECT tour_id::text AS tour_id, title, country, duration_days FROM catalog.v_tour
WHERE source_system = 'cis' AND source_id = $1 LIMIT 1
"""
# Keyed by tour_id only (parse-backed view, tests/catalog_perf.py).
DAYS_SQL = """
SELECT d.day_number, d.title, d.body, d.places FROM catalog.v_tour_day d
WHERE d.tour_id = $1::uuid ORDER BY d.day_number
"""
ROUTE_VIEW = liveness.View(
    "catalog", "v_tour_day_route", ("tour_id", "day_index", "overnight_place", "overnight_destination_id")
)
ROUTE_SQL = """
SELECT r.day_index, r.overnight_place, r.overnight_destination_id::text AS destination_id
FROM catalog.v_tour_day_route r WHERE r.tour_id = $1::uuid ORDER BY r.day_index
"""
COUNTRY_SQL = "SELECT iso2::text FROM booking.countries WHERE lower(name) = lower($1) LIMIT 1"
HOTELS_SQL = """
SELECT h.code, h.name, h.address, h.destination_id::text AS destination_id, h.country_code::text AS country_code,
       su.code AS supplier_code, rt.id::text AS room_type_id, rt.name AS room_type
FROM booking.hotels h
JOIN booking.hotel_room_types rt ON rt.hotel_id = h.id AND rt.status = 'active'
JOIN booking.suppliers su ON su.id = h.supplier_id
WHERE h.status = 'active' AND (h.country_code = $1 OR h.destination_id = ANY($2::uuid[]))
ORDER BY h.code, rt.name
LIMIT $3
"""
SERVICES_SQL = """
SELECT s.id::text AS service_id, s.code, s.title, s.service_type, s.unit, s.destination_id::text AS destination_id,
       s.country_code::text AS country_code, s.source_id, su.code AS supplier_code
FROM booking.services s JOIN booking.suppliers su ON su.id = s.supplier_id
WHERE s.status = 'active'
  AND (s.country_code = $1 OR s.destination_id = ANY($2::uuid[]) OR (s.source_system = 'cis' AND s.source_id = $3))
ORDER BY s.code
LIMIT $4
"""


# ── Text ─────────────────────────────────────────────────────────────────────


def norm(text: str | None) -> str:
    """Lower case, accents removed, every run of non-letters one space, padded (``" ha long bay "``)."""
    plain = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii").lower()
    return " " + " ".join(re.findall(r"[a-z0-9]+", plain)) + " "


def words(text: str | None) -> list[str]:
    return [w for w in norm(text).split() if w not in GENERIC and len(w) > 1]


def has_phrase(haystack: str, phrase: str) -> bool:
    """``phrase`` (normalised) as whole words in an already normalised ``haystack``."""
    needle = norm(phrase).strip()
    return bool(needle) and f" {needle} " in haystack


def keywords_in(text: str, service_type: str) -> list[str]:
    return [k for k in KEYWORDS[service_type] if has_phrase(text, k)]


# ── Pure matching ────────────────────────────────────────────────────────────


@dataclass
class Day:
    day_no: int
    title: str | None = None
    body: str | None = None
    places: list[str] = field(default_factory=list)
    overnight_place: str | None = None
    destination_id: str | None = None

    @property
    def text(self) -> str:
        return norm(f"{self.title or ''} {self.body or ''}")

    @property
    def night_place(self) -> str | None:
        return self.overnight_place or (self.places[-1] if self.places else None)


@dataclass
class Hotel:
    code: str
    name: str
    address: str | None
    destination_id: str | None
    country_code: str | None
    supplier_code: str | None
    room_types: list[tuple[str, str]] = field(default_factory=list)  # (id, name)

    def default_room(self) -> tuple[str, str]:
        for wanted in ROOM_PREFERRED:
            for rid, name in self.room_types:
                if wanted in norm(name):
                    return rid, name
        return self.room_types[0]


def _scored(score: float, reasons: list[str]) -> tuple[float, str]:
    value = round(min(score, 1.0), 2)
    return value, "high" if value >= 0.6 else "medium" if value >= 0.3 else "low"


def score_hotel(hotel: Hotel, day: Day, *, iso: str | None, country: str | None) -> tuple[float, list[str]]:
    text = day.text
    reasons: list[str] = []
    score = 0.0
    name_words = words(hotel.name)
    if has_phrase(text, hotel.name):
        score += 0.6
        reasons.append("named in the day's text")
    elif name_words:
        found = [w for w in name_words if f" {w} " in text]
        if found and len(found) == len(name_words):
            score += 0.5
            reasons.append(f"its name words are in the day's text ({', '.join(found)})")
        elif len(found) >= 2 and len(found) * 2 >= len(name_words):
            score += 0.3
            reasons.append(f"some name words are in the day's text ({', '.join(found)})")
    if day.destination_id and hotel.destination_id == day.destination_id:
        score += 0.3
        reasons.append(f"at the night's place in CIS ({day.night_place or 'same destination'})")
    place = day.night_place
    if place and words(place):
        where = norm(f"{hotel.name} {hotel.address or ''}")
        if all(f" {w} " in where for w in words(place)):
            score += 0.2
            reasons.append(f"'{place}' is in its name or address")
    if iso and hotel.country_code == iso:
        score += 0.05
        reasons.append(f"in {country or iso}")
    return score, reasons


def score_service(
    service: Mapping[str, Any], day: Day, destinations: set[str], *, tour_key: str, iso: str | None, country: str | None
) -> tuple[float, list[str]]:
    text = day.text
    reasons: list[str] = []
    score = 0.0
    if service.get("source_id") and service["source_id"] == tour_key:
        score += 0.5
        reasons.append("made from this tour in CIS")
    title_words = words(service["title"])
    found = [w for w in title_words if f" {w} " in text]
    if found:
        score += 0.4 * len(found) / len(title_words)
        reasons.append(f"title words in the day's text ({', '.join(found)})")
    if service.get("destination_id") and service["destination_id"] in destinations:
        score += 0.3
        reasons.append("at one of the day's places in CIS")
    if iso and service.get("country_code") == iso:
        score += 0.05
        reasons.append(f"in {country or iso}")
    return score, reasons


def hotel_candidate(hotel: Hotel, score: float, reasons: list[str]) -> dict[str, Any]:
    room_id, room_name = hotel.default_room()
    value, confidence = _scored(score, reasons)
    return {
        "kind": plan_components.KIND_ROOM,
        "hotel_room_type_id": room_id,
        "service_id": None,
        "code": hotel.code,
        "label": f"{hotel.code} {hotel.name} · {room_name}",
        "room_type": room_name,
        "room_types": [{"id": rid, "name": name} for rid, name in hotel.room_types],
        "supplier_code": hotel.supplier_code,
        "score": value,
        "confidence": confidence,
        "reasons": reasons + [f"room type {room_name}: the default double or twin"],
    }


def service_candidate(service: Mapping[str, Any], score: float, reasons: list[str]) -> dict[str, Any]:
    value, confidence = _scored(score, reasons)
    return {
        "kind": service["service_type"],
        "hotel_room_type_id": None,
        "service_id": service["service_id"],
        "code": service["code"],
        "label": f"{service['code']} {service['title']}",
        "room_type": None,
        "room_types": [],
        "supplier_code": service["supplier_code"],
        "score": value,
        "confidence": confidence,
        "reasons": reasons,
    }


def _ranked(cands: Iterable[tuple[float, list[str], Any]], floor: float) -> list[tuple[float, list[str], Any]]:
    kept = [c for c in cands if c[0] >= floor]
    return sorted(kept, key=lambda c: (-c[0], str(getattr(c[2], "code", None) or c[2].get("code"))))


def suggest(
    *,
    tour_key: str,
    duration_days: int | None,
    days: Sequence[Day],
    hotels: Sequence[Hotel],
    services: Sequence[Mapping[str, Any]],
    current: Sequence[Mapping[str, Any]],
    iso: str | None,
    country: str | None,
) -> dict[str, Any]:
    """The suggestions of every day (see the module doc). ``current`` = the plan's components (``list_components``)."""
    by_day = {d.day_no: d for d in days}
    total = max([duration_days or 0, *by_day]) if (duration_days or by_day) else 0
    have: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for c in current:
        have.setdefault((int(c["day_no"]), HOTEL if c["kind"] == plan_components.KIND_ROOM else c["kind"]), []).append(
            dict(c)
        )
    out_days: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    counts = {"suggested": 0, "covered": 0, "unmatched": 0}
    for n in range(1, total + 1):
        day = by_day.get(n, Day(n))
        prev = by_day.get(n - 1)
        destinations = {d for d in (day.destination_id, prev.destination_id if prev else None) if d}
        needs: list[dict[str, Any]] = []
        if n < total:
            needs.append({"kind": HOTEL, "keywords": [], "place": day.night_place})
        for stype in SERVICE_TYPES:
            found = keywords_in(day.text, stype)
            if found:
                needs.append({"kind": stype, "keywords": found, "place": day.night_place})
        day_needs: list[dict[str, Any]] = []
        for need in needs:
            kind = need["kind"]
            covered = have.get((n, kind), [])
            if covered:
                counts["covered"] += 1
                day_needs.append(
                    {**need, "status": "covered", "current": covered, "suggestion": None, "alternatives": []}
                )
                continue
            if kind == HOTEL:
                ranked = _ranked(
                    ((*score_hotel(h, day, iso=iso, country=country), h) for h in hotels if h.room_types), MIN_SCORE
                )
                cands = [hotel_candidate(h, s, r) for s, r, h in ranked[: ALTERNATIVES + 1]]
            else:
                floor = MIN_GUIDE_SCORE if kind == "guide" else MIN_SCORE
                ranked = _ranked(
                    (
                        (*score_service(s, day, destinations, tour_key=tour_key, iso=iso, country=country), s)
                        for s in services
                        if s["service_type"] == kind
                    ),
                    floor,
                )
                cands = [service_candidate(s, sc, r) for sc, r, s in ranked[: ALTERNATIVES + 1]]
            if not cands:
                counts["unmatched"] += 1
                why = (
                    f"no active hotel matches the night at {need['place']}"
                    if kind == HOTEL and need["place"]
                    else "no active hotel matches this night"
                    if kind == HOTEL
                    else f"no active {kind} service matches ({', '.join(need['keywords'])})"
                )
                item = {**need, "day_no": n, "why": why}
                unmatched.append(item)
                day_needs.append({**need, "status": "unmatched", "current": [], "suggestion": None, "alternatives": []})
                continue
            counts["suggested"] += 1
            day_needs.append(
                {**need, "status": "suggested", "current": [], "suggestion": cands[0], "alternatives": cands[1:]}
            )
        out_days.append(
            {
                "day_no": n,
                "title": day.title,
                "places": day.places,
                "overnight_place": day.night_place if n < total else None,
                "needs": day_needs,
            }
        )
    return {"days": out_days, "unmatched": unmatched, **counts}


# ── Database part ────────────────────────────────────────────────────────────


async def _soft(conn: asyncpg.Connection, sql: str, *args: Any) -> list[Any] | None:
    """A catalog read in its own savepoint: None when it fails (the page degrades, never a 500)."""
    try:
        async with conn.transaction():
            return list(await conn.fetch(sql, *args))
    except CATALOG_ERRORS:
        return None


async def build(conn: asyncpg.Connection, source_id: str) -> dict[str, Any]:
    """The suggestions for one trip plan (404 when the plan or its CIS tour is unknown)."""
    current = (await plan_components.list_components(conn, source_id))["items"]
    tours = await _soft(conn, TOUR_SQL, source_id)
    if tours is None:
        return {"tour_key": source_id, "days": [], "unmatched": [], "degraded": ["catalog"], **_zero()}
    if not tours:
        raise NotFound("tour_not_in_catalog", "this tour is not in the CIS catalog")
    tour = tours[0]
    degraded: list[str] = []
    day_rows = await _soft(conn, DAYS_SQL, tour["tour_id"])
    if day_rows is None:
        degraded.append("days")
        day_rows = []
    days = {
        int(r["day_number"]): Day(int(r["day_number"]), r["title"], r["body"], [p for p in (r["places"] or []) if p])
        for r in day_rows
    }
    route = await _soft(conn, ROUTE_SQL, tour["tour_id"]) if await liveness.view_ready(conn, ROUTE_VIEW) else None
    if route is None:
        degraded.append("route")
    for r in route or []:
        day = days.setdefault(int(r["day_index"]), Day(int(r["day_index"])))
        day.overnight_place = r["overnight_place"] or day.overnight_place
        day.destination_id = r["destination_id"]
    iso = await conn.fetchval(COUNTRY_SQL, tour["country"]) if tour["country"] else None
    dest_ids = sorted({d.destination_id for d in days.values() if d.destination_id})
    hotels: dict[str, Hotel] = {}
    for r in await conn.fetch(HOTELS_SQL, iso, dest_ids, MAX_CANDIDATES):
        hotel = hotels.setdefault(
            r["code"],
            Hotel(r["code"], r["name"], r["address"], r["destination_id"], r["country_code"], r["supplier_code"]),
        )
        hotel.room_types.append((r["room_type_id"], r["room_type"]))
    services = [dict(r) for r in await conn.fetch(SERVICES_SQL, iso, dest_ids, source_id, MAX_CANDIDATES)]
    out = suggest(
        tour_key=source_id,
        duration_days=tour["duration_days"],
        days=list(days.values()),
        hotels=list(hotels.values()),
        services=services,
        current=current,
        iso=iso,
        country=tour["country"],
    )
    return {
        "tour_key": source_id,
        "title": tour["title"],
        "country": tour["country"],
        "duration_days": tour["duration_days"],
        "candidates": {"hotels": len(hotels), "services": len(services)},
        **out,
        "degraded": degraded,
    }


def _zero() -> dict[str, int]:
    return {"suggested": 0, "covered": 0, "unmatched": 0}
