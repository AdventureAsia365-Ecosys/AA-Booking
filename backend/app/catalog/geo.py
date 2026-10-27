"""Catalog → Countries and Destinations: CIS places read live plus Booking's own state (AA-996, bk-029).

- **Content is CIS's** (ADR 0002, O7): a destination is a row of ``catalog.v_destination`` (name, country name,
  coordinates, cover, photo count) and its photos come from ``catalog.v_destination_photo`` when CIS serves it
  (feature-detected: without it the photo list is empty). A country is derived: the ISO reference row
  (``booking.countries``) that a CIS place or tour names (``v_destination.country`` / ``v_tour.country`` are country
  names, mapped through ``booking.countries.name``). Nothing is copied into Booking.
- **Booking state** (bk-029): ``booking.destination_profiles`` and ``booking.country_profiles`` hold the status
  (``active`` | ``inactive``), area (countries), internal notes and the deactivation reason. No row = active.
  Deactivate / Activate is the soft replacement of the Figma "Delete / Unpublish" dialogs: nothing is ever deleted.
- **Booking facts** shown next to the content: active suppliers, hotels and services at the place / in the country
  (``booking.suppliers`` / ``hotels`` / ``services``), and how many CIS tours name the country.
- Reads need ``catalog.read`` (every staff role); a status / notes change needs ``catalog.admin`` (admin only, UI spec
  §3 "A edit status"). Every change writes one audit row.
- A catalog that cannot be read is 503 ``catalog_unavailable`` (``trip_plans.catalog_reads``); an unknown place is
  404 on reads and 404 on writes (the id is checked against the view before a profile row is written).
"""

from __future__ import annotations

import logging
from typing import Any

import asyncpg

from app import audit, paging
from app.auth.staff import Staff
from app.catalog.destinations import CATALOG_ERRORS
from app.catalog.trip_plans import catalog_reads
from app.errors import Invalid, NotFound

log = logging.getLogger(__name__)

STATUSES = ("active", "inactive")
MAX_PAGE = 100
MAX_PHOTOS = 40
MAX_COUNTRY_DESTINATIONS = 200
MAX_COUNTRY_TOURS = 50
DEST_SORTS = {"name": "lower(d.name)", "country": "lower(d.country)", "photos": "d.photo_count"}

# Active supply records per place / country (Booking facts, not content).
SUPPLY_AT_PLACE = """
    (SELECT count(*) FROM booking.suppliers s WHERE s.destination_id = d.destination_id AND s.status = 'active')
        AS active_suppliers,
    (SELECT count(*) FROM booking.hotels h WHERE h.destination_id = d.destination_id AND h.status = 'active')
        AS active_hotels,
    (SELECT count(*) FROM booking.services v WHERE v.destination_id = d.destination_id AND v.status = 'active')
        AS active_services
"""

DEST_COLUMNS = """
    d.destination_id::text AS destination_id, d.name, d.country, c.iso2::text AS country_code,
    d.lat::float8 AS lat, d.lng::float8 AS lng, d.cover_photo_path, d.cover_image_url, d.photo_count,
    coalesce(p.status, 'active') AS status, p.status_reason, p.internal_notes, p.updated_at AS profile_updated_at,
    p.updated_by AS profile_updated_by, ub.name AS profile_updated_by_name
"""
DEST_FROM = """
    FROM catalog.v_destination d
    LEFT JOIN booking.countries c ON lower(c.name) = lower(d.country)
    LEFT JOIN booking.destination_profiles p ON p.destination_id = d.destination_id
    LEFT JOIN booking.staff_users ub ON ub.cis_user_id = p.updated_by
"""


def _status(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    v = value.strip().lower()
    if v not in STATUSES:
        raise Invalid("invalid_status", "status must be active or inactive")
    return v


# ── Destinations ────────────────────────────────────────────────────────────


async def list_destinations(
    conn: asyncpg.Connection,
    *,
    q: str | None = None,
    country: str | None = None,
    status: str | None = None,
    sort: str | None = None,
    limit: int = 25,
    offset: int = 0,
) -> dict[str, Any]:
    """CIS places with their Booking status. ``q`` = part of the name, ``country`` = ISO-2, ``status`` = active /
    inactive. ``summary`` (total, active, inactive, countries) counts the whole catalog, not the filtered page."""
    where: list[str] = []
    args: list[Any] = []

    def add(fragment: str, value: Any) -> None:
        args.append(value)
        where.append(fragment.replace("{p}", f"${len(args)}"))

    if q and q.strip():
        add("d.name ILIKE {p}", paging.contains(q))
    if country and country.strip():
        add("c.iso2 = {p}", country.strip().upper())
    wanted = _status(status)
    if wanted:
        add("coalesce(p.status, 'active') = {p}", wanted)
    raw = (sort or "name").strip()
    column = DEST_SORTS.get(raw.lstrip("-+"))
    if column is None:
        raise Invalid("unknown_sort", f"sort must be one of {', '.join(DEST_SORTS)} (prefix - for descending)")
    order = f"{column} {'DESC' if raw.startswith('-') else 'ASC'} NULLS LAST, d.destination_id"
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    limit = max(1, min(limit, MAX_PAGE))
    async with catalog_reads():
        total = await conn.fetchval(f"SELECT count(*) {DEST_FROM} {clause}", *args)  # noqa: S608 — fixed fragments
        rows = await conn.fetch(
            f"SELECT {DEST_COLUMNS} {DEST_FROM} {clause}"  # noqa: S608 — fixed fragments, values are bound
            f" ORDER BY {order} LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}",
            *args,
            limit,
            offset,
        )
        summary = await conn.fetchrow(
            """
            SELECT count(*) AS total,
                   count(*) FILTER (WHERE coalesce(p.status, 'active') = 'active') AS active,
                   count(*) FILTER (WHERE p.status = 'inactive') AS inactive,
                   count(DISTINCT lower(d.country)) AS countries
            FROM catalog.v_destination d
            LEFT JOIN booking.destination_profiles p ON p.destination_id = d.destination_id
            """
        )
    return {
        "items": [dict(r) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
        "summary": dict(summary) if summary else {"total": 0, "active": 0, "inactive": 0, "countries": 0},
    }


async def _destination_photos(conn: asyncpg.Connection, destination_id: str) -> list[dict[str, Any]]:
    """Photos of a place from ``catalog.v_destination_photo`` (CIS 225), or ``[]`` when CIS does not serve it."""
    try:
        async with conn.transaction():
            rows = await conn.fetch(
                "SELECT photo_id::text AS photo_id, path, position FROM catalog.v_destination_photo"
                " WHERE destination_id = $1::uuid ORDER BY position NULLS LAST, photo_id LIMIT $2",
                destination_id,
                MAX_PHOTOS,
            )
    except (*CATALOG_ERRORS, asyncpg.UndefinedColumnError) as exc:
        log.warning("catalog.v_destination_photo unreadable, photos omitted: %s", type(exc).__name__)
        return []
    return [dict(r) for r in rows]


async def get_destination(conn: asyncpg.Connection, destination_id: str) -> dict[str, Any]:
    async with catalog_reads():
        row = await conn.fetchrow(
            f"SELECT {DEST_COLUMNS}, {SUPPLY_AT_PLACE} {DEST_FROM} WHERE d.destination_id = $1::uuid",  # noqa: S608
            destination_id,
        )
    if row is None:
        raise NotFound("destination_not_found", f"destination {destination_id} is not a CIS catalog place")
    return {**dict(row), "photos": await _destination_photos(conn, destination_id)}


async def update_destination(
    conn: asyncpg.Connection, staff: Staff, destination_id: str, patch: dict[str, Any]
) -> dict[str, Any]:
    """PATCH semantics on the Booking state of one CIS place (``status``, ``status_reason``, ``internal_notes``).
    Activating clears the reason. One audit row when something changed."""
    await _lock(conn, "booking.destination_profiles", destination_id)
    place = await get_destination(conn, destination_id)  # 404 for an id that is not a catalog place
    before = {k: place[k] for k in ("status", "status_reason", "internal_notes")}
    after = _apply(before, patch)
    changed = sorted(k for k in after if after[k] != before[k])
    if not changed:
        return {**place, "changed": []}
    await conn.execute(
        """
        INSERT INTO booking.destination_profiles (destination_id, status, status_reason, internal_notes, updated_by)
        VALUES ($1::uuid, $2, $3, $4, $5)
        ON CONFLICT (destination_id) DO UPDATE
            SET status = EXCLUDED.status, status_reason = EXCLUDED.status_reason,
                internal_notes = EXCLUDED.internal_notes, updated_by = EXCLUDED.updated_by
        """,
        destination_id,
        after["status"],
        after["status_reason"],
        after["internal_notes"],
        staff.cis_user_id,
    )
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action=_action("destination", before, after),
        entity_type="destination_profile",
        entity_id=destination_id,
        before={k: before[k] for k in changed},
        after={k: after[k] for k in changed},
        entity_label=place["name"][:80] if place["name"] else destination_id,
        summary=_summary(place["name"] or destination_id, before, after, changed),
    )
    return {**await get_destination(conn, destination_id), "changed": changed}


# ── Countries ───────────────────────────────────────────────────────────────

COUNTRY_CTE = """
WITH dest AS (
    SELECT c.iso2, count(*) AS destinations, coalesce(sum(d.photo_count), 0) AS photos
    FROM catalog.v_destination d JOIN booking.countries c ON lower(c.name) = lower(d.country)
    GROUP BY c.iso2
), tours AS (
    SELECT c.iso2, count(*) AS tours
    FROM catalog.v_tour t JOIN booking.countries c ON lower(c.name) = lower(t.country)
    GROUP BY c.iso2
), supply AS (
    SELECT country_code AS iso2, count(*) AS active_suppliers FROM booking.suppliers
    WHERE status = 'active' AND country_code IS NOT NULL GROUP BY country_code
)
"""
COUNTRY_COLUMNS = """
    c.iso2::text AS country_code, c.name, coalesce(dest.destinations, 0) AS destinations,
    coalesce(dest.photos, 0)::bigint AS photos, coalesce(tours.tours, 0) AS tours,
    coalesce(supply.active_suppliers, 0) AS active_suppliers,
    coalesce(p.status, 'active') AS status, p.area, p.status_reason, p.internal_notes,
    p.updated_at AS profile_updated_at, p.updated_by AS profile_updated_by, ub.name AS profile_updated_by_name
"""
COUNTRY_FROM = """
    FROM booking.countries c
    LEFT JOIN dest ON dest.iso2 = c.iso2
    LEFT JOIN tours ON tours.iso2 = c.iso2
    LEFT JOIN supply ON supply.iso2 = c.iso2
    LEFT JOIN booking.country_profiles p ON p.country_code = c.iso2
    LEFT JOIN booking.staff_users ub ON ub.cis_user_id = p.updated_by
"""
# A country is listed when CIS has a place or a tour there, or Booking already keeps state for it.
COUNTRY_LISTED = "(dest.iso2 IS NOT NULL OR tours.iso2 IS NOT NULL OR p.country_code IS NOT NULL)"


async def list_countries(
    conn: asyncpg.Connection, *, q: str | None = None, status: str | None = None, area: str | None = None
) -> dict[str, Any]:
    """Countries of the CIS catalog with their Booking state (a short list: no paging). ``unmapped`` names the CIS
    country names that have no ISO row in ``booking.countries`` (their places cannot be grouped by country)."""
    where = [COUNTRY_LISTED]
    args: list[Any] = []
    if q and q.strip():
        args.append(paging.contains(q))
        term = q.strip()
        if len(term) == 2:  # "VN" or "vn" is also an ISO code; a longer word ("Myanmar") is a name only
            args.append(term.upper())
            where.append(f"(c.name ILIKE ${len(args) - 1} OR c.iso2 = ${len(args)})")
        else:
            where.append(f"c.name ILIKE ${len(args)}")
    wanted = _status(status)
    if wanted:
        args.append(wanted)
        where.append(f"coalesce(p.status, 'active') = ${len(args)}")
    if area and area.strip():
        args.append(area.strip())
        where.append(f"lower(p.area) = lower(${len(args)})")
    async with catalog_reads():
        rows = await conn.fetch(
            f"{COUNTRY_CTE} SELECT {COUNTRY_COLUMNS} {COUNTRY_FROM}"  # noqa: S608 — fixed fragments, values bound
            f" WHERE {' AND '.join(where)} ORDER BY c.name",
            *args,
        )
        unmapped = await conn.fetch(
            """
            SELECT d.country AS name, count(*) AS destinations FROM catalog.v_destination d
            WHERE d.country IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM booking.countries c WHERE lower(c.name) = lower(d.country))
            GROUP BY d.country ORDER BY d.country LIMIT 50
            """
        )
    items = [dict(r) for r in rows]
    areas = sorted({i["area"] for i in items if i["area"]})
    return {"items": items, "total": len(items), "areas": areas, "unmapped": [dict(r) for r in unmapped]}


async def _country_row(conn: asyncpg.Connection, iso: str) -> dict[str, Any]:
    async with catalog_reads():
        row = await conn.fetchrow(
            f"{COUNTRY_CTE} SELECT {COUNTRY_COLUMNS} {COUNTRY_FROM} WHERE c.iso2 = $1",  # noqa: S608
            iso,
        )
    if row is None:
        raise NotFound("country_not_found", f"country {iso} is not in the ISO reference list")
    return dict(row)


async def get_country(conn: asyncpg.Connection, iso: str) -> dict[str, Any]:
    """One country: its Booking state, the CIS places in it (with their Booking status) and the CIS tours naming it."""
    iso = iso.strip().upper()
    country = await _country_row(conn, iso)
    async with catalog_reads():
        destinations = await conn.fetch(
            f"SELECT {DEST_COLUMNS} {DEST_FROM} WHERE c.iso2 = $1"  # noqa: S608 — fixed fragments
            " ORDER BY lower(d.name), d.destination_id LIMIT $2",
            iso,
            MAX_COUNTRY_DESTINATIONS,
        )
        tours = await conn.fetch(
            """
            SELECT t.source_id, t.title, t.sku, t.duration_days, t.cover_photo_path,
                   coalesce(tp.sellable, false) AS sellable
            FROM catalog.v_tour t
            LEFT JOIN booking.trip_plans tp ON tp.source_system = t.source_system AND tp.source_id = t.source_id
            WHERE lower(t.country) = lower($1)
            ORDER BY t.title, t.source_id LIMIT $2
            """,
            country["name"],
            MAX_COUNTRY_TOURS,
        )
    return {
        **country,
        "destination_items": [dict(r) for r in destinations],
        "tour_items": [dict(r) for r in tours],
    }


async def update_country(conn: asyncpg.Connection, staff: Staff, iso: str, patch: dict[str, Any]) -> dict[str, Any]:
    """PATCH semantics on a country's Booking state (``status``, ``status_reason``, ``area``, ``internal_notes``).

    "Add country": a country that is not listed yet (no CIS place, no CIS tour, no Booking state) gets its profile row
    even when the body keeps every default, so it becomes listed; that write is audited as ``country.added``."""
    iso = iso.strip().upper()
    await _lock(conn, "booking.country_profiles", iso)
    country = await _country_row(conn, iso)
    keys = ("status", "status_reason", "area", "internal_notes")
    before = {k: country[k] for k in keys}
    after = _apply(before, patch)
    changed = sorted(k for k in after if after[k] != before[k])
    # profile_updated_at is NOT NULL on a stored row, so None = no Booking state yet.
    added = country["profile_updated_at"] is None and not country["destinations"] and not country["tours"]
    if not changed and not added:
        return {**await get_country(conn, iso), "changed": []}
    profile_id = await conn.fetchval(
        """
        INSERT INTO booking.country_profiles (country_code, status, status_reason, area, internal_notes, updated_by)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (country_code) DO UPDATE
            SET status = EXCLUDED.status, status_reason = EXCLUDED.status_reason, area = EXCLUDED.area,
                internal_notes = EXCLUDED.internal_notes, updated_by = EXCLUDED.updated_by
        RETURNING id::text
        """,
        iso,
        after["status"],
        after["status_reason"],
        after["area"],
        after["internal_notes"],
        staff.cis_user_id,
    )
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="country.added" if added else _action("country", before, after),
        entity_type="country_profile",
        entity_id=profile_id,
        before=None if added else {k: before[k] for k in changed},
        after=after if added else {k: after[k] for k in changed},
        entity_label=f"{country['name']} ({iso})",
        summary=f"{country['name']} added to the Booking country list"
        if added
        else _summary(country["name"], before, after, changed),
    )
    return {**await get_country(conn, iso), "changed": changed}


# ── Shared ──────────────────────────────────────────────────────────────────


async def _lock(conn: asyncpg.Connection, table: str, key: str) -> None:
    """Serialize read-merge-write of one profile row until the caller's transaction ends (the routes run inside
    ``conn.transaction()``). An advisory lock, not ``SELECT … FOR UPDATE``, because the first write of a key has no row
    to lock yet. Two-key form: (hash of the table, hash of the key), apart from the one-key outbox lock."""
    await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1), hashtext($2))", table, key)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _apply(before: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """The new state: fields absent from ``patch`` keep their value; ``status = active`` clears the reason."""
    after = dict(before)
    for key in before:
        if key in patch:
            after[key] = (_status(patch[key]) or "active") if key == "status" else _text(patch[key])
    if after["status"] == "active":
        after["status_reason"] = None
    return after


def _action(kind: str, before: dict[str, Any], after: dict[str, Any]) -> str:
    if before["status"] != after["status"]:
        return f"{kind}.{'deactivated' if after['status'] == 'inactive' else 'activated'}"
    return f"{kind}.updated"


def _summary(name: str, before: dict[str, Any], after: dict[str, Any], changed: list[str]) -> str:
    if "status" in changed:
        verb = "deactivated" if after["status"] == "inactive" else "activated"
        reason = f": {after['status_reason']}" if after.get("status_reason") else ""
        return f"{name} {verb} in Booking{reason}"
    return f"{name}: {', '.join(changed)} updated"
