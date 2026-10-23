"""Staff edit of the bookable components of a trip plan (``booking.trip_plan_components``, bk-019, AA-972).

A component is a Booking supply item (a hotel room type or a service, bk-010) on a day of a sellable plan; the set is
what ``GET /booking/b2b/v1/products`` lists and what a ``tp:`` quote line prices. ``PUT`` replaces the whole set of a
plan in one transaction (delete + insert; role ``booking`` has SELECT, INSERT, DELETE), touches the plan's
``updated_at`` (the products feed's change signal, also when the set is emptied) and writes one audit row when it
changed. The plan must already have its commercial row (``PUT …/commercial``); every item must exist and be active.
Ids only: no content is copied (O7).
"""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg

from app import audit
from app.auth.staff import Staff
from app.b2b.components import KIND_ROOM, PREFIX_ROOM, PREFIX_SERVICE, component_id
from app.errors import Invalid, NotFound

MAX_COMPONENTS = 200

LIST_SQL = """
SELECT c.day_no, c.hotel_room_type_id::text AS room_type_id, c.service_id::text AS service_id,
       rt.name AS room_type_name, h.code AS hotel_code, h.name AS hotel_name, rt.status AS room_type_status,
       s.code AS service_code, s.title AS service_title, s.service_type, s.status AS service_status
FROM booking.trip_plan_components c
LEFT JOIN booking.hotel_room_types rt ON rt.id = c.hotel_room_type_id
LEFT JOIN booking.hotels h ON h.id = rt.hotel_id
LEFT JOIN booking.services s ON s.id = c.service_id
WHERE c.trip_plan_id = $1::uuid
ORDER BY c.day_no, coalesce(c.hotel_room_type_id, c.service_id)
"""


async def _plan_id(conn: asyncpg.Connection, source_id: str, *, lock: bool = False) -> str:
    sql = "SELECT id::text FROM booking.trip_plans WHERE source_system = 'cis' AND source_id = $1"
    plan = await conn.fetchval(sql + (" FOR UPDATE" if lock else ""), source_id)
    if plan is None:
        raise NotFound("trip_plan_not_found", "save the commercial row of this tour first")
    return plan


def _shape(row: Any) -> dict[str, Any]:
    if row["room_type_id"]:
        return {
            "component_id": component_id(PREFIX_ROOM, row["room_type_id"]),
            "kind": KIND_ROOM,
            "day_no": row["day_no"],
            "hotel_room_type_id": row["room_type_id"],
            "service_id": None,
            "label": f"{row['hotel_code']} {row['hotel_name']} · {row['room_type_name']}",
            "active": row["room_type_status"] == "active",
        }
    return {
        "component_id": component_id(PREFIX_SERVICE, row["service_id"]),
        "kind": row["service_type"],
        "day_no": row["day_no"],
        "hotel_room_type_id": None,
        "service_id": row["service_id"],
        "label": f"{row['service_code']} {row['service_title']}",
        "active": row["service_status"] == "active",
    }


async def list_components(conn: asyncpg.Connection, source_id: str) -> dict[str, Any]:
    plan_id = await _plan_id(conn, source_id)
    rows = await conn.fetch(LIST_SQL, plan_id)
    return {"trip_plan_id": plan_id, "source_id": source_id, "items": [_shape(r) for r in rows]}


def _key(item: dict[str, Any]) -> tuple[int, str, str]:
    return (int(item["day_no"]), item.get("hotel_room_type_id") or "", item.get("service_id") or "")


async def _check_items(conn: asyncpg.Connection, items: list[dict[str, Any]]) -> None:
    rooms = sorted({i["hotel_room_type_id"] for i in items if i.get("hotel_room_type_id")})
    services = sorted({i["service_id"] for i in items if i.get("service_id")})
    ok_rooms = {
        r["id"]
        for r in await conn.fetch(
            "SELECT rt.id::text AS id FROM booking.hotel_room_types rt JOIN booking.hotels h ON h.id = rt.hotel_id"
            " WHERE rt.id = ANY($1::uuid[]) AND rt.status = 'active' AND h.status = 'active'",
            rooms,
        )
    }
    ok_services = {
        r["id"]
        for r in await conn.fetch(
            "SELECT id::text AS id FROM booking.services WHERE id = ANY($1::uuid[]) AND status = 'active'", services
        )
    }
    bad = [f"room type {r}" for r in rooms if r not in ok_rooms]
    bad += [f"service {s}" for s in services if s not in ok_services]
    if bad:
        raise Invalid("unknown_component", f"unknown or inactive: {', '.join(bad)}")


async def replace_components(
    conn: asyncpg.Connection, staff: Staff, source_id: str, items: list[dict[str, Any]]
) -> dict[str, Any]:
    if len(items) > MAX_COMPONENTS:
        raise Invalid("too_many_components", f"at most {MAX_COMPONENTS} components per plan")
    norm: list[dict[str, Any]] = []
    for item in items:
        room, service = item.get("hotel_room_type_id"), item.get("service_id")
        if bool(room) == bool(service):
            raise Invalid("invalid_component", "each component names exactly one of hotel_room_type_id, service_id")
        norm.append(
            {
                "day_no": int(item["day_no"]),
                "hotel_room_type_id": str(uuid.UUID(str(room))) if room else None,
                "service_id": str(uuid.UUID(str(service))) if service else None,
            }
        )
    if len({_key(i) for i in norm}) != len(norm):
        raise Invalid("duplicate_component", "the same item appears twice on one day")
    plan_id = await _plan_id(conn, source_id, lock=True)
    await _check_items(conn, norm)
    before = sorted(
        _key(dict(r))
        for r in await conn.fetch(
            "SELECT day_no, hotel_room_type_id::text AS hotel_room_type_id, service_id::text AS service_id"
            " FROM booking.trip_plan_components WHERE trip_plan_id = $1::uuid",
            plan_id,
        )
    )
    after = sorted(_key(i) for i in norm)
    if before != after:
        await conn.execute("DELETE FROM booking.trip_plan_components WHERE trip_plan_id = $1::uuid", plan_id)
        await conn.executemany(
            "INSERT INTO booking.trip_plan_components"
            " (trip_plan_id, day_no, hotel_room_type_id, service_id, created_by)"
            " VALUES ($1::uuid, $2, $3::uuid, $4::uuid, $5)",
            [(plan_id, i["day_no"], i["hotel_room_type_id"], i["service_id"], staff.cis_user_id) for i in norm],
        )
        # The plan's updated_at is the products feed's change signal (an emptied set leaves no component row).
        await conn.execute("UPDATE booking.trip_plans SET updated_at = now() WHERE id = $1::uuid", plan_id)
        await audit.record(
            conn,
            actor_type="staff",
            actor_ref=staff.cis_user_id,
            action="trip_plan.components_replaced",
            entity_type="trip_plan",
            entity_id=plan_id,
            before={"count": len(before)},
            after={"count": len(after), "source_id": source_id},
            entity_label=source_id[:80],
            summary=f"Bookable components: {len(before)} -> {len(after)}",
        )
    return await list_components(conn, source_id)
