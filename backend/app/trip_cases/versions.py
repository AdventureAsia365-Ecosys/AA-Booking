"""Proposal versions of a trip case and their free-text itinerary items (AA-681 slice 3, bk-003).

- **V1** is pre-filled from ``trip_case_legs`` (the TripPlanner leg chain) ordered by ``day_from``,
  then ``position``. The item title is the inquiry snapshot's first day name for that tour, else
  ``AA tour <source_tour_id>`` (catalog titles arrive with AA-679).
- **Vn** (n > 1) copies the latest version: notes, total, currency and every item.
- An approved version is locked by the database (bk-003 trigger); the API also refuses to change
  the items of a version already sent to the customer — staff make a new version instead.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import asyncpg

from app import audit
from app.auth.staff import Staff
from app.errors import Conflict, NotFound
from app.trip_cases.service import is_terminal, resolve_case

MAX_ITEMS = 200

VERSION_COLUMNS = """
    v.id::text AS id, v.version_number, v.notes, v.total_price::text AS total_price, v.currency,
    v.is_sent_to_customer, v.sent_at, v.is_approved, v.approved_at, v.created_by, v.created_at, v.updated_at
"""
ITEM_COLUMNS = "position, title, day_from, day_to, source_tour_id, notes"


@dataclass(frozen=True)
class Item:
    title: str
    day_from: int
    day_to: int
    source_tour_id: str | None = None
    notes: str | None = None


def leg_title(snapshot: dict[str, Any] | None, source_tour_id: str) -> str:
    """First named snapshot day of that tour, in day order; a generic title otherwise."""
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
    return f"AA tour {source_tour_id}"[:500]


def prefill_from_legs(legs: list[Any], snapshot: dict[str, Any] | None) -> list[Item]:
    ordered = sorted(legs, key=lambda leg: (leg["day_from"], leg["position"]))
    return [
        Item(
            title=leg_title(snapshot, leg["source_tour_id"]),
            day_from=leg["day_from"],
            day_to=leg["day_to"],
            source_tour_id=leg["source_tour_id"],
        )
        for leg in ordered
    ]


async def _items(conn: asyncpg.Connection, version_id: str) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        f"SELECT {ITEM_COLUMNS} FROM booking.trip_case_version_itinerary"  # noqa: S608 — constant
        " WHERE trip_case_version_id = $1::uuid ORDER BY position",
        version_id,
    )
    return [dict(r) for r in rows]


async def _insert_items(conn: asyncpg.Connection, version_id: str, items: list[Item]) -> None:
    if not items:
        return
    await conn.executemany(
        "INSERT INTO booking.trip_case_version_itinerary"
        " (trip_case_version_id, position, title, day_from, day_to, source_tour_id, notes)"
        " VALUES ($1::uuid, $2, $3, $4, $5, $6, $7)",
        [
            (version_id, i, it.title, it.day_from, it.day_to, it.source_tour_id, it.notes)
            for i, it in enumerate(items, start=1)
        ],
    )


async def _version(conn: asyncpg.Connection, case_id: str, number: int, *, lock: bool = False) -> Any:
    row = await conn.fetchrow(
        f"SELECT {VERSION_COLUMNS} FROM booking.trip_case_versions v"  # noqa: S608 — constant
        f" WHERE v.trip_case_id = $1::uuid AND v.version_number = $2{' FOR UPDATE' if lock else ''}",
        case_id,
        number,
    )
    if row is None:
        raise NotFound("version_not_found", f"version {number} not found")
    return row


async def create_version(conn: asyncpg.Connection, staff: Staff, ref: str, notes: str | None) -> dict[str, Any]:
    case = await resolve_case(conn, ref, lock=True)  # the lock serialises version numbering per case
    if is_terminal(case["status"]):
        raise Conflict("trip_case_closed", f"trip case is {case['status']}")
    latest = await conn.fetchrow(
        "SELECT id::text AS id, version_number, notes, total_price, currency FROM booking.trip_case_versions"
        " WHERE trip_case_id = $1::uuid ORDER BY version_number DESC LIMIT 1",
        case["id"],
    )
    if latest is None:
        number, copied_from = 1, None
        legs = await conn.fetch(
            "SELECT position, source_tour_id, day_from, day_to FROM booking.trip_case_legs"
            " WHERE trip_case_id = $1::uuid ORDER BY day_from, position",
            case["id"],
        )
        snapshot = json.loads(case["inquiry_snapshot"]) if case["inquiry_snapshot"] else None
        items = prefill_from_legs(list(legs), snapshot)
        values = (notes, None, case["currency"])
    else:
        number, copied_from = latest["version_number"] + 1, latest["version_number"]
        items = []
        values = (notes if notes is not None else latest["notes"], latest["total_price"], latest["currency"])
    version_id = await conn.fetchval(
        "INSERT INTO booking.trip_case_versions"
        " (trip_case_id, version_number, notes, total_price, currency, created_by)"
        " VALUES ($1::uuid, $2, $3, $4, $5, $6) RETURNING id::text",
        case["id"],
        number,
        *values,
        staff.cis_user_id,
    )
    if latest is None:
        await _insert_items(conn, version_id, items)
    else:
        await conn.execute(
            f"INSERT INTO booking.trip_case_version_itinerary (trip_case_version_id, {ITEM_COLUMNS})"  # noqa: S608
            f" SELECT $1::uuid, {ITEM_COLUMNS} FROM booking.trip_case_version_itinerary"
            " WHERE trip_case_version_id = $2::uuid ORDER BY position",
            version_id,
            latest["id"],
        )
    version = dict(await _version(conn, case["id"], number))
    version["items"] = await _items(conn, version_id)
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="trip_case_version.created",
        entity_type="trip_case_version",
        entity_id=version_id,
        after={
            "trip_case_id": case["id"],
            "version_number": number,
            "copied_from": copied_from,
            "item_count": len(version["items"]),
        },
    )
    return version


async def get_items(conn: asyncpg.Connection, ref: str, number: int) -> dict[str, Any]:
    case = await resolve_case(conn, ref)
    version = dict(await _version(conn, case["id"], number))
    version["items"] = await _items(conn, version["id"])
    return version


async def replace_items(
    conn: asyncpg.Connection, staff: Staff, ref: str, number: int, items: list[Item]
) -> dict[str, Any]:
    case = await resolve_case(conn, ref, lock=True)
    if is_terminal(case["status"]):
        raise Conflict("trip_case_closed", f"trip case is {case['status']}")
    version = dict(await _version(conn, case["id"], number, lock=True))
    if version["is_approved"]:
        raise Conflict("version_locked", "an approved version cannot change")
    if version["is_sent_to_customer"]:
        raise Conflict("version_sent", "this version was sent to the customer; create a new version")
    before = await _items(conn, version["id"])
    try:
        await conn.execute(
            "DELETE FROM booking.trip_case_version_itinerary WHERE trip_case_version_id = $1::uuid", version["id"]
        )
        await _insert_items(conn, version["id"], items)
        await conn.execute(
            "UPDATE booking.trip_case_versions SET updated_at = now() WHERE id = $1::uuid", version["id"]
        )
    except asyncpg.ObjectNotInPrerequisiteStateError as exc:  # approved meanwhile (DB lock trigger)
        raise Conflict("version_locked", "an approved version cannot change") from exc
    version = dict(await _version(conn, case["id"], number))
    version["items"] = await _items(conn, version["id"])
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="trip_case_version.items_replaced",
        entity_type="trip_case_version",
        entity_id=version["id"],
        before={"items": before},
        after={"items": version["items"]},
    )
    return version
