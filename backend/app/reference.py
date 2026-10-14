"""Read-only reference data: Booking-wide settings rows and the ISO country list (bk-005)."""

from __future__ import annotations

import json
from typing import Any

import asyncpg


async def list_settings(conn: asyncpg.Connection) -> dict[str, Any]:
    rows = await conn.fetch(
        "SELECT key, value::text AS value, description, updated_at FROM booking.settings ORDER BY key"
    )
    items = [{**dict(r), "value": json.loads(r["value"])} for r in rows]
    return {"values": {i["key"]: i["value"] for i in items}, "items": items}


async def list_countries(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    rows = await conn.fetch("SELECT iso2::text AS iso2, name FROM booking.countries WHERE active ORDER BY name")
    return [dict(r) for r in rows]
