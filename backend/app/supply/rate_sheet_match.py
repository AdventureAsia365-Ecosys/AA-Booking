"""How a DMC rate sheet finds the supplier's hotels and room types (AA-1153 part 1, bk-069; split out of
:mod:`app.supply.rate_sheet`, file size limit): every hotel linked (active) to the supplier, by the DMC's own code
for it first, then the AA code; a room type by its name, then the supplier's own alias, then an any-DMC alias."""

from __future__ import annotations

from difflib import get_close_matches
from typing import Any

import asyncpg


def _key(text: Any) -> str:
    return " ".join(str(text or "").split()).lower()


# AA-1153 (bk-069): the room types of every hotel linked (active) to the supplier, with the DMC's code for the hotel.
ROOM_TYPES_SQL = """
SELECT upper(h.code) AS hotel_code, upper(btrim(l.supplier_hotel_code)) AS dmc_code, h.id::text AS hotel_id,
       t.name, lower(regexp_replace(btrim(t.name), '\\s+', ' ', 'g')) AS room_type,
       t.id::text AS id, t.status, h.status AS hotel_status
FROM booking.hotel_supplier_links l
JOIN booking.hotels h ON h.id = l.hotel_id
JOIN booking.hotel_room_types t ON t.hotel_id = h.id
WHERE l.supplier_id = $1::uuid AND l.status = 'active'
"""
HOTELS_SQL = """
SELECT upper(h.code) AS hotel_code, upper(btrim(l.supplier_hotel_code)) AS dmc_code, h.id::text AS hotel_id
FROM booking.hotel_supplier_links l JOIN booking.hotels h ON h.id = l.hotel_id
WHERE l.supplier_id = $1::uuid AND l.status = 'active'
"""
ALIASES_SQL = """
SELECT t.hotel_id::text AS hotel_id, a.room_type_id::text AS id,
       lower(regexp_replace(btrim(a.alias), '\\s+', ' ', 'g')) AS alias, (a.supplier_id IS NOT NULL) AS own
FROM booking.hotel_room_type_aliases a
JOIN booking.hotel_room_types t ON t.id = a.room_type_id
JOIN booking.hotel_supplier_links l ON l.hotel_id = t.hotel_id AND l.supplier_id = $1::uuid AND l.status = 'active'
WHERE a.supplier_id IS NULL OR a.supplier_id = $1::uuid
"""


class Rooms:
    """The supplier's hotels and room types for the match (AA-1153): a hotel by the DMC's code, then the AA code; a
    room type by its name, then the supplier's alias, then an any-DMC alias."""

    def __init__(self, rows: list[Any], hotels: list[Any], aliases: list[Any]) -> None:
        self.hotel: dict[str, str] = {}
        for h in hotels:
            self.hotel.setdefault(h["hotel_code"], h["hotel_id"])
        for h in hotels:  # the DMC's code wins over an AA code that happens to read the same
            if h["dmc_code"]:
                self.hotel[h["dmc_code"]] = h["hotel_id"]
        self.room = {(r["hotel_id"], r["room_type"]): r for r in rows}
        self.names: dict[str, list[str]] = {}
        for r in rows:
            self.names.setdefault(r["hotel_id"], []).append(r["name"])
        by_id = {r["id"]: r for r in rows}
        self.alias: dict[tuple[str, str], Any] = {}
        for a in sorted(aliases, key=lambda a: not a["own"]):
            self.alias.setdefault((a["hotel_id"], a["alias"]), by_id.get(a["id"]))

    def find(self, code: str | None, room_type: str | None) -> tuple[str | None, Any]:
        hotel = self.hotel.get(code or "")
        if hotel is None:
            return None, None
        key = (hotel, _key(room_type))
        return hotel, self.room.get(key) or self.alias.get(key)

    def closest(self, hotel: str, room_type: str | None) -> list[str]:
        return get_close_matches(room_type or "", self.names.get(hotel, []), n=3, cutoff=0)

    def target(self, code: str | None, room_type: str | None) -> tuple[dict[str, Any], tuple[str, str, str] | None]:
        """The room type of a hotel-sheet row, or why not (column, code, message)."""
        hotel, found = self.find(code, room_type)
        if hotel is None:
            why = f"no hotel {code} sold by this supplier (link the supplier to the hotel first)"
            return {}, ("Hotel code", "unknown_item", why)
        if found is None:
            near = ", ".join(self.closest(hotel, room_type))
            hint = f" (closest: {near}; add an alias or a room type)" if near else ""
            return {}, ("Room type", "unknown_item", f"no room type '{room_type}' at hotel {code}{hint}")
        if found["status"] != "active" or found["hotel_status"] != "active":
            return {}, ("Room type", "inactive_item", "the room type or hotel is inactive")
        return {"hotel_room_type_id": found["id"], "unit": "room_night"}, None


async def load(conn: asyncpg.Connection, supplier_id: str) -> Rooms:
    """The supplier's linked hotels, their room types and the aliases it may use (three reads)."""
    return Rooms(
        list(await conn.fetch(ROOM_TYPES_SQL, supplier_id)),
        list(await conn.fetch(HOTELS_SQL, supplier_id)),
        list(await conn.fetch(ALIASES_SQL, supplier_id)),
    )
