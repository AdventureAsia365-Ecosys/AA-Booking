"""The hotel property master (AA-1153 part 1, bk-069; S285 advisory r2 hotels T8, T10, T11).

AA has no contract with a hotel: a DMC (AA's supplier) sells the hotel to AA under an AA supplier contract, and the
same hotel may come from several DMCs. One ``booking.hotels`` row is one real hotel; the DMCs that sell it are
``booking.hotel_supplier_links`` rows (``hotels.supplier_id`` = the supplier that first listed it, always linked).

- **Duplicates** (:func:`check_duplicates`, on create and on a change of the name, place or position): the cleaned
  name (:func:`clean_name`: lower case, no accents or punctuation, without :data:`COMMON_WORDS`), its similarity
  (``difflib``, 0..100) and the distance (haversine, metres) against the hotels of the same place, country or nearby.
  **Block** (409 ``hotel_duplicate`` with the candidates): similarity >= ``hotel_match_block_pct`` and closer than
  ``hotel_match_block_m`` (or, without a position on one side, at the same place). **Ask** (409
  ``hotel_possible_duplicate``): similarity >= ``hotel_match_warn_pct`` at the same place or closer than
  ``hotel_match_warn_m``; the write goes through with ``not_duplicate_reason`` (10..300 characters, audited). The four
  thresholds are pricing policy parameters (section hotels), never literals.
- **DMC links** (:func:`add_link`, :func:`update_link`): a supplier with the hotel category, its own code for the hotel
  (unique per supplier), at most one preferred DMC (its price first when DMCs tie on the board), active / inactive
  (never deleted: past contracts keep their hotel; an inactive link prices nothing new, bk-069 guards).
- **Room type aliases** (:func:`add_alias`): the name a DMC uses for an AA room type; one alias names one room type of
  a hotel per supplier (the rate-sheet importer matches it after the AA name).
- **Facts confirmation** (:func:`confirm_facts`): source (``dmc_sheet`` with the DMC, ``hotel_website``,
  ``site_visit``) and day; due again ``hotel_facts_reconfirm_days`` later (:func:`reconfirm_due`; the hotel list
  filter ``reconfirm=due``). None of these columns is a ``supply.hotel.v1`` fact: CIS sees no new version.
- **External codes** (:func:`clean_codes`): ``{"<system>": "<code>"}`` (a DMC's code, a GIATA id later), at most
  :data:`MAX_CODES`.

Permissions: reads ``supply.read``, writes ``supply.write`` (the routes). Every write adds one audit row on the hotel.
"""

from __future__ import annotations

import math
import re
import unicodedata
from datetime import date, timedelta
from decimal import Decimal
from difflib import SequenceMatcher
from typing import Any

import asyncpg

from app import audit
from app.auth.staff import Staff
from app.errors import Conflict, Invalid, NotFound
from app.pricing import policy_values
from app.pricing.policy_params_hotels import (
    FACTS_RECONFIRM_DAYS,
    MATCH_BLOCK_M,
    MATCH_BLOCK_PCT,
    MATCH_KEYS,
    MATCH_WARN_M,
    MATCH_WARN_PCT,
)
from app.suppliers.service import clean_text

# Words that do not tell two hotels apart (a cleaned name drops them); not a threshold.
COMMON_WORDS = frozenset({"hotel", "hotels", "resort", "resorts", "spa", "boutique", "the", "and", "by"})
FACTS_SOURCES = ("dmc_sheet", "hotel_website", "site_visit")
CODE_KEY = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
MAX_CODES = 20  # technical-constant: the bk-069 CHECK keeps the object small (2000 characters)
MAX_CANDIDATES = 5  # technical-constant: how many candidates an answer lists (display size)
SCAN_LIMIT = 5000  # technical-constant: the hotels compared at most (same place, country or nearby)
METRES_PER_DEGREE = 111_320.0
EARTH_RADIUS_M = 6_371_000.0
REASON_MIN, REASON_MAX = 10, 300


# ── Pure helpers ─────────────────────────────────────────────────────────────


def clean_name(name: str | None) -> str:
    """Lower case, accents and punctuation removed, common words dropped ("La Siesta Classic Hotel – Ma May" ->
    "la siesta classic ma may")."""
    text = unicodedata.normalize("NFKD", (name or "").replace("đ", "d").replace("Đ", "D"))
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    words = re.sub(r"[^a-z0-9]+", " ", text).split()
    return " ".join(w for w in words if w not in COMMON_WORDS)


def similarity(a: str, b: str) -> int:
    """0..100: how alike two cleaned names are."""
    if not a or not b:
        return 0
    if a == b:
        return 100
    return round(SequenceMatcher(None, a, b).ratio() * 100)


def distance_m(a: tuple[Any, Any] | None, b: tuple[Any, Any] | None) -> float | None:
    """Haversine distance in metres between two (latitude, longitude) points; None when one is missing."""
    if a is None or b is None or None in a or None in b:
        return None
    lat1, lon1, lat2, lon2 = (math.radians(float(v)) for v in (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


def verdict(sim: int, metres: float | None, same_place: bool, limits: dict[str, int]) -> str | None:
    """``block``, ``warn`` or None for one candidate (the rules in the module docstring)."""
    near_block = metres <= limits[MATCH_BLOCK_M] if metres is not None else same_place
    if sim >= limits[MATCH_BLOCK_PCT] and near_block:
        return "block"
    near_warn = same_place or (metres is not None and metres <= limits[MATCH_WARN_M])
    if sim >= limits[MATCH_WARN_PCT] and near_warn:
        return "warn"
    return None


def clean_codes(value: Any) -> dict[str, str]:
    """External codes as ``{"<system>": "<code>"}``: system keys lower snake case, codes 1..64 characters."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise Invalid("invalid_external_codes", "external codes are an object of system: code")
    out: dict[str, str] = {}
    for key, code in value.items():
        system = str(key).strip().lower()
        text = clean_text(code)
        if not CODE_KEY.match(system):
            raise Invalid("invalid_external_codes", f"{key!r}: a system is 2-40 lower-case letters, digits or _")
        if text is None:
            continue
        if len(text) > 64:
            raise Invalid("invalid_external_codes", f"{system}: a code is at most 64 characters")
        out[system] = text
    if len(out) > MAX_CODES:
        raise Invalid("invalid_external_codes", f"at most {MAX_CODES} external codes")
    return dict(sorted(out.items()))


def reconfirm_due(confirmed_on: date | None, days: int) -> date | None:
    return confirmed_on + timedelta(days=days) if confirmed_on else None


# ── Duplicates ───────────────────────────────────────────────────────────────

CANDIDATES_SQL = """
SELECT h.id::text AS id, h.code, h.name, h.destination_id::text AS destination_id, h.latitude, h.longitude, h.status
FROM booking.hotels h
WHERE ($1::uuid IS NULL OR h.id <> $1::uuid)
  AND (($2::uuid IS NOT NULL AND h.destination_id = $2::uuid)
       OR ($3::text IS NOT NULL AND h.country_code = $3)
       OR ($4::numeric IS NOT NULL AND h.latitude BETWEEN $4::numeric - $6::numeric AND $4::numeric + $6::numeric
           AND h.longitude BETWEEN $5::numeric - $7::numeric AND $5::numeric + $7::numeric))
LIMIT $8
"""


async def limits(conn: asyncpg.Connection) -> dict[str, int]:
    """The four duplicate thresholds in force today (503 ``policy_unavailable`` without one)."""
    policy = await policy_values.require_staff(conn, await policy_values.today_utc(conn), *MATCH_KEYS)
    return {key: policy.integer(key) for key in MATCH_KEYS}


async def candidates(
    conn: asyncpg.Connection, values: dict[str, Any], *, exclude_id: str | None = None
) -> list[dict[str, Any]]:
    """The hotels a new or changed hotel may duplicate, worst first: ``{id, code, name, similarity, distance_m,
    same_place, verdict}``."""
    name = clean_name(values.get("name"))
    if not name:
        return []
    found = await limits(conn)
    lat, lon = values.get("latitude"), values.get("longitude")
    span = found[MATCH_WARN_M] / METRES_PER_DEGREE
    lon_span = span / max(math.cos(math.radians(float(lat))), 0.01) if lat is not None else None
    as_num = lambda v: Decimal(str(v)) if v is not None else None  # noqa: E731
    rows = await conn.fetch(
        CANDIDATES_SQL,
        exclude_id,
        values.get("destination_id"),
        values.get("country_code"),
        as_num(lat),
        as_num(lon),
        as_num(round(span, 6)),
        as_num(round(lon_span, 6)) if lon_span is not None else None,
        SCAN_LIMIT,
    )
    out = []
    for row in rows:
        sim = similarity(name, clean_name(row["name"]))
        metres = distance_m((lat, lon), (row["latitude"], row["longitude"]))
        same = values.get("destination_id") is not None and row["destination_id"] == str(values["destination_id"])
        kind = verdict(sim, metres, same, found)
        if kind:
            out.append(
                {
                    "id": row["id"],
                    "code": row["code"],
                    "name": row["name"],
                    "status": row["status"],
                    "similarity": sim,
                    "distance_m": round(metres) if metres is not None else None,
                    "same_place": same,
                    "verdict": kind,
                }
            )
    out.sort(key=lambda c: (c["verdict"] != "block", -c["similarity"], c["distance_m"] or 0))
    return out[:MAX_CANDIDATES]


async def check_duplicates(
    conn: asyncpg.Connection, values: dict[str, Any], *, exclude_id: str | None = None, reason: str | None = None
) -> dict[str, Any] | None:
    """Refuse a duplicate (409 ``hotel_duplicate``), ask about a possible one (409 ``hotel_possible_duplicate`` unless
    ``reason``); returns what the audit row records when a reason let a possible duplicate through, else None."""
    found = await candidates(conn, values, exclude_id=exclude_id)
    blocks = [c for c in found if c["verdict"] == "block"]
    if blocks:
        raise Conflict(
            "hotel_duplicate",
            f"this looks like {blocks[0]['code']} {blocks[0]['name']}: use that hotel and link this supplier to it",
            candidates=found,
        )
    if not found:
        return None
    text = clean_text(reason)
    if text is None:
        raise Conflict(
            "hotel_possible_duplicate",
            "a hotel with a similar name is close by; check it, or say why this is a different hotel",
            candidates=found,
        )
    if not REASON_MIN <= len(text) <= REASON_MAX:
        raise Invalid("invalid_reason", f"the reason is {REASON_MIN}-{REASON_MAX} characters")
    return {"not_duplicate_of": [c["code"] for c in found], "reason": text}


# ── DMC links ────────────────────────────────────────────────────────────────

LINKS_SQL = """
SELECT l.supplier_id::text AS supplier_id, s.code AS supplier_code, s.company_name AS supplier_name,
       s.status AS supplier_status, l.supplier_hotel_code, l.is_preferred, l.status, l.created_by, l.created_at,
       l.updated_at, (l.supplier_id = h.supplier_id) AS first_listed,
       (SELECT count(*) FROM booking.supplier_contracts k WHERE k.supplier_id = l.supplier_id
         AND k.status = 'published' AND EXISTS (SELECT 1 FROM booking.supplier_rates x
           JOIN booking.hotel_room_types t ON t.id = x.hotel_room_type_id
          WHERE x.supplier_contract_id = k.id AND t.hotel_id = h.id)) AS published_contracts
FROM booking.hotel_supplier_links l
JOIN booking.hotels h ON h.id = l.hotel_id
JOIN booking.suppliers s ON s.id = l.supplier_id
WHERE l.hotel_id = $1::uuid
ORDER BY l.is_preferred DESC, l.status, lower(s.company_name)
"""


async def list_links(conn: asyncpg.Connection, hotel_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in await conn.fetch(LINKS_SQL, hotel_id)]


async def _hotel(conn: asyncpg.Connection, ref: str) -> dict[str, Any]:
    from app.supply import records  # noqa: PLC0415 — records imports this module

    return await records.resolve(conn, records.HOTEL, ref, lock=True)


async def _audit(conn: asyncpg.Connection, staff: Staff, hotel: dict[str, Any], action: str, summary: str, **kw: Any):
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action=f"hotel.{action}",
        entity_type="hotel",
        entity_id=hotel["id"],
        entity_label=f"{hotel['code']} {hotel['name']}",
        summary=summary,
        **kw,
    )


def _code(value: Any) -> str | None:
    text = clean_text(value)
    if text is not None and len(text) > 64:
        raise Invalid("invalid_supplier_hotel_code", "the supplier's hotel code is at most 64 characters")
    return text


async def _clear_preferred(conn: asyncpg.Connection, hotel_id: str, keep: str) -> None:
    await conn.execute(
        "UPDATE booking.hotel_supplier_links SET is_preferred = false"
        " WHERE hotel_id = $1::uuid AND supplier_id <> $2::uuid AND is_preferred",
        hotel_id,
        keep,
    )


async def add_link(conn: asyncpg.Connection, staff: Staff, ref: str, data: dict[str, Any]) -> list[dict[str, Any]]:
    """Link one more DMC to the hotel (409 ``link_exists`` when it is already linked: edit it instead)."""
    from app.suppliers import service as suppliers  # noqa: PLC0415

    hotel = await _hotel(conn, ref)
    supplier = await suppliers.resolve_supplier(conn, str(data.get("supplier_id") or ""))
    if supplier["status"] != "active":
        raise Conflict("supplier_inactive", f"supplier {supplier['code']} is inactive")
    if "hotel" not in supplier["categories"]:
        raise Invalid("supplier_category_missing", f"supplier {supplier['code']} does not have the hotel category")
    code, preferred = _code(data.get("supplier_hotel_code")), bool(data.get("is_preferred"))
    if preferred:
        await _clear_preferred(conn, hotel["id"], supplier["id"])
    try:
        inserted = await conn.fetchval(
            "INSERT INTO booking.hotel_supplier_links (hotel_id, supplier_id, supplier_hotel_code, is_preferred,"
            " created_by, updated_by) VALUES ($1::uuid, $2::uuid, $3, $4, $5, $5) ON CONFLICT DO NOTHING RETURNING 1",
            hotel["id"],
            supplier["id"],
            code,
            preferred,
            staff.cis_user_id,
        )
    except asyncpg.UniqueViolationError as exc:
        raise Conflict("supplier_hotel_code_taken", f"{supplier['code']} already uses code {code} for a hotel") from exc
    if not inserted:
        raise Conflict("link_exists", f"{supplier['code']} already sells this hotel; edit its link instead")
    await _audit(
        conn,
        staff,
        hotel,
        "supplier_linked",
        f"Linked {supplier['code']} {supplier['company_name']}" + (f" (code {code})" if code else ""),
        after={"supplier_id": supplier["id"], "supplier_hotel_code": code, "is_preferred": preferred},
    )
    return await list_links(conn, hotel["id"])


async def update_link(
    conn: asyncpg.Connection, staff: Staff, ref: str, supplier_id: str, changes: dict[str, Any]
) -> list[dict[str, Any]]:
    """Change a DMC's code for the hotel, make it the preferred DMC (or not), or set the link active / inactive."""
    hotel = await _hotel(conn, ref)
    row = await conn.fetchrow(
        "SELECT supplier_hotel_code, is_preferred, status FROM booking.hotel_supplier_links"
        " WHERE hotel_id = $1::uuid AND supplier_id = $2::uuid FOR UPDATE",
        hotel["id"],
        supplier_id,
    )
    if row is None:
        raise NotFound("link_not_found", "this supplier does not sell this hotel")
    values: dict[str, Any] = {}
    if "supplier_hotel_code" in changes:
        values["supplier_hotel_code"] = _code(changes["supplier_hotel_code"])
    if "status" in changes:
        if changes["status"] not in ("active", "inactive"):
            raise Invalid("unknown_status", "status is active or inactive")
        values["status"] = changes["status"]
        if changes["status"] == "inactive":
            values["is_preferred"] = False
    if changes.get("is_preferred") is not None and values.get("status", row["status"]) == "active":
        values["is_preferred"] = bool(changes["is_preferred"])
    elif changes.get("is_preferred"):
        raise Conflict("link_inactive", "an inactive supplier link cannot be the preferred one")
    changed = {k: v for k, v in values.items() if row[k] != v}
    if not changed:
        return await list_links(conn, hotel["id"])
    if changed.get("is_preferred"):
        await _clear_preferred(conn, hotel["id"], supplier_id)
    sets = ", ".join(f"{k} = ${i + 4}" for i, k in enumerate(changed))
    try:
        await conn.execute(
            f"UPDATE booking.hotel_supplier_links SET {sets}, updated_by = $3"  # noqa: S608 — whitelisted columns
            " WHERE hotel_id = $1::uuid AND supplier_id = $2::uuid",
            hotel["id"],
            supplier_id,
            staff.cis_user_id,
            *changed.values(),
        )
    except asyncpg.UniqueViolationError as exc:
        raise Conflict("supplier_hotel_code_taken", "this supplier already uses that code for another hotel") from exc
    await _audit(
        conn,
        staff,
        hotel,
        "supplier_link_updated",
        f"Updated the supplier link: {', '.join(sorted(changed))}",
        before={k: row[k] for k in changed},
        after={"supplier_id": supplier_id, **changed},
    )
    return await list_links(conn, hotel["id"])


# ── Room type aliases ────────────────────────────────────────────────────────

ALIASES_SQL = """
SELECT a.id::text AS id, a.room_type_id::text AS room_type_id, a.supplier_id::text AS supplier_id,
       s.code AS supplier_code, a.alias, a.created_by, a.created_at
FROM booking.hotel_room_type_aliases a LEFT JOIN booking.suppliers s ON s.id = a.supplier_id
WHERE a.room_type_id = ANY($1::uuid[])
ORDER BY lower(a.alias), a.id
"""


async def aliases_by_room(conn: asyncpg.Connection, room_type_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {rid: [] for rid in room_type_ids}
    if room_type_ids:
        for row in await conn.fetch(ALIASES_SQL, room_type_ids):
            out[row["room_type_id"]].append(dict(row))
    return out


async def add_alias(
    conn: asyncpg.Connection, staff: Staff, room_type_id: str, alias: str, supplier_id: str | None
) -> list[dict[str, Any]]:
    """Another name of a room type, for one DMC or any; refused when another room type of the hotel already has it
    for the same DMC (409 ``alias_taken``) or it is the name of another room type of the hotel."""
    from app.supply import records  # noqa: PLC0415

    room = await records.get_room_type(conn, room_type_id, lock=True)
    text = clean_text(alias)
    if text is None or len(text) > 120:
        raise Invalid("invalid_alias", "an alias is 1-120 characters")
    taken = await conn.fetchval(
        "SELECT t.name FROM booking.hotel_room_types t WHERE t.hotel_id = $1::uuid AND t.id <> $2::uuid"
        " AND (lower(btrim(t.name)) = lower($3) OR EXISTS (SELECT 1 FROM booking.hotel_room_type_aliases a"
        " WHERE a.room_type_id = t.id AND lower(btrim(a.alias)) = lower($3)"
        " AND (a.supplier_id IS NULL OR $4::uuid IS NULL OR a.supplier_id = $4::uuid))) LIMIT 1",
        room["hotel_id"],
        room["id"],
        text,
        supplier_id,
    )
    if taken:
        raise Conflict("alias_taken", f"'{text}' already names room type {taken} of this hotel")
    try:
        await conn.execute(
            "INSERT INTO booking.hotel_room_type_aliases (room_type_id, supplier_id, alias, created_by)"
            " VALUES ($1::uuid, $2::uuid, $3, $4)",
            room["id"],
            supplier_id,
            text,
            staff.cis_user_id,
        )
    except asyncpg.UniqueViolationError as exc:
        raise Conflict("alias_exists", f"'{text}' is already an alias of this room type") from exc
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="hotel_room_type.alias_added",
        entity_type="hotel_room_type",
        entity_id=room["id"],
        after={"alias": text, "supplier_id": supplier_id},
        entity_label=room["name"],
        summary=f"Added the alias '{text}'",
    )
    return (await aliases_by_room(conn, [room["id"]]))[room["id"]]


async def remove_alias(conn: asyncpg.Connection, staff: Staff, alias_id: str) -> None:
    row = await conn.fetchrow(
        "DELETE FROM booking.hotel_room_type_aliases WHERE id = $1::uuid"
        " RETURNING room_type_id::text AS room_type_id, alias, supplier_id::text AS supplier_id",
        alias_id,
    )
    if row is None:
        raise NotFound("alias_not_found", "alias not found")
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="hotel_room_type.alias_removed",
        entity_type="hotel_room_type",
        entity_id=row["room_type_id"],
        before={"alias": row["alias"], "supplier_id": row["supplier_id"]},
        summary=f"Removed the alias '{row['alias']}'",
    )


# ── Facts confirmation ───────────────────────────────────────────────────────


async def reconfirm_days(conn: asyncpg.Connection) -> int:
    policy = await policy_values.require_staff(conn, await policy_values.today_utc(conn), FACTS_RECONFIRM_DAYS)
    return policy.integer(FACTS_RECONFIRM_DAYS)


async def confirm_facts(conn: asyncpg.Connection, staff: Staff, ref: str, data: dict[str, Any]) -> dict[str, Any]:
    """Record that the facts were confirmed today (or on ``confirmed_on``, never in the future) from a source."""
    hotel = await _hotel(conn, ref)
    source = data.get("source")
    if source not in FACTS_SOURCES:
        raise Invalid("unknown_facts_source", f"source is one of {', '.join(FACTS_SOURCES)}")
    today = await policy_values.today_utc(conn)
    on = data.get("confirmed_on") or today
    if on > today:
        raise Invalid("invalid_confirmed_on", "a confirmation is today or in the past")
    supplier = str(data["supplier_id"]) if data.get("supplier_id") else None
    if (supplier is not None) != (source == "dmc_sheet"):
        raise Invalid("facts_supplier", "a DMC sheet names its supplier; the other sources name none")
    if supplier and not await conn.fetchval(
        "SELECT 1 FROM booking.hotel_supplier_links WHERE hotel_id = $1::uuid AND supplier_id = $2::uuid",
        hotel["id"],
        supplier,
    ):
        raise Invalid("facts_supplier", "the DMC of the sheet sells this hotel (link it first)")
    await conn.execute(
        "UPDATE booking.hotels SET facts_source = $2, facts_source_supplier_id = $3::uuid, facts_confirmed_on = $4,"
        " facts_confirmed_by = $5, updated_by = $5 WHERE id = $1::uuid",
        hotel["id"],
        source,
        supplier,
        on,
        staff.cis_user_id,
    )
    await _audit(
        conn,
        staff,
        hotel,
        "facts_confirmed",
        f"Confirmed the facts ({source.replace('_', ' ')}, {on.isoformat()})",
        after={"facts_source": source, "facts_source_supplier_id": supplier, "facts_confirmed_on": on.isoformat()},
    )
    return {"facts_source": source, "facts_source_supplier_id": supplier, "facts_confirmed_on": on}
