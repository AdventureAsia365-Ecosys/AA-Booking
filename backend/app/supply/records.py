"""Hotels, hotel room types and services: the supply facts Booking owns (bk-010, AA-823; ADR 0004a decision 1).

A hotel or a service belongs to one supplier (bk-008) and sits at a CIS place: ``destination_id`` is a
``catalog.v_destination`` id checked on every write (``app.suppliers.service.check_place``: no FK across schemas),
and ``country_code`` follows it. Records are addressed by UUID or by code (``HTL-000123`` / ``SVC-000123``).

- **Facts only.** No customer-facing description, highlights or photo (ADR 0004a d2, O7, Q17): CIS writes that
  content later from these facts. Names and titles are internal working names.
- **Supplier.** Set on create, never moved afterwards (rates of the record live under that supplier's contracts). The
  supplier must be active and carry the matching category (hotel; transport for a transfer, otherwise the service type).
- **Never deleted.** ``deactivate`` / ``activate`` only; a record of an inactive supplier cannot be activated.
- **Room types** (hotels only): name, max occupancy, bed types, internal notes, status. Rates hang off a room type.
- A service's pricing ``unit`` cannot change once it has rates (the rates were entered per that unit).
- **Read-back** (AA-835 S4): the detail carries ``content``, the customer content CIS published for the record
  (``app.supply.content``; ``None`` until CIS serves ``catalog.v_supply_content``). Supplier photos are
  ``app.supply.media``; their audit rows show in the record's recent activity.

Hotels and services share one implementation, parametrised by :class:`Kind`. Every write adds one audit row.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

import asyncpg

from app import audit, paging
from app.auth.staff import Staff
from app.errors import Conflict, Invalid, NotFound
from app.suppliers import service as suppliers
from app.suppliers.service import clean_text
from app.supply import content

HOTEL_RATES = (
    suppliers.RATE_ROOM_TYPE + " IN (SELECT t.id FROM booking.hotel_room_types t WHERE t.hotel_id = $1::uuid)"  # noqa: S608 — constants
)
SERVICE_RATES = suppliers.RATE_SERVICE + " = $1::uuid"

STATUSES = ("active", "inactive")
SERVICE_TYPES = ("activity", "transfer", "guide", "entrance", "meal")
SERVICE_UNITS = ("person", "group", "vehicle", "day")
# The bk-008 supplier category a record needs.
SERVICE_CATEGORY = {
    "activity": "activity",
    "transfer": "transport",
    "guide": "guide",
    "entrance": "entrance",
    "meal": "meal",
}

TODAY = "(now() AT TIME ZONE 'UTC')::date"
# A rate is in force on a day when its contract is published and both validities cover the day.
RATE_IN_FORCE_ON = (
    "k.status = 'published' AND k.valid_from <= {day} AND (k.valid_to IS NULL OR k.valid_to >= {day})"
    " AND x.valid_from <= {day} AND (x.valid_to IS NULL OR x.valid_to >= {day})"
)
RATE_IN_FORCE_TODAY = RATE_IN_FORCE_ON.format(day=TODAY)


@dataclass(frozen=True)
class Kind:
    entity: str  # audit entity_type and error prefix: hotel | service
    table: str
    prefix: str  # human code prefix
    name_field: str
    columns: str  # kind-specific SELECT columns over alias r
    editable: tuple[str, ...]
    text_fields: tuple[str, ...]
    list_fields: tuple[str, ...]
    search: str  # ILIKE condition, {p} = the pattern
    sorts: dict[str, str]


HOTEL = Kind(
    entity="hotel",
    table="booking.hotels",
    prefix="HTL-",
    name_field="name",
    columns=f"""
        r.name, r.hotel_style, r.star_rating, r.address, r.latitude, r.longitude, r.check_in_time, r.check_out_time,
        r.amenities, r.links::text AS links,
        (SELECT count(*) FROM booking.hotel_room_types t WHERE t.hotel_id = r.id AND t.status = 'active')
            AS room_type_count,
        (SELECT count(*) FROM booking.supplier_rates x
            JOIN booking.hotel_room_types t ON t.id = x.hotel_room_type_id
            JOIN booking.supplier_contracts k ON k.id = x.supplier_contract_id
         WHERE t.hotel_id = r.id AND {RATE_IN_FORCE_TODAY}) AS rates_in_force
    """,  # noqa: S608 — constants only
    editable=(
        "name",
        "hotel_style",
        "star_rating",
        "address",
        "destination_id",
        "country_code",
        "latitude",
        "longitude",
        "check_in_time",
        "check_out_time",
        "amenities",
        "links",
        "internal_notes",
    ),
    text_fields=("hotel_style", "address", "internal_notes"),
    list_fields=("amenities",),
    search="(r.name ILIKE {p} OR r.code ILIKE {p} OR r.address ILIKE {p} OR sp.company_name ILIKE {p})",
    sorts={
        "name": "lower(r.name)",
        "star_rating": "r.star_rating",
        "updated_at": "r.updated_at",
        "created_at": "r.created_at",
    },
)

SERVICE = Kind(
    entity="service",
    table="booking.services",
    prefix="SVC-",
    name_field="title",
    columns=f"""
        r.service_type, r.title, r.unit, r.source_system, r.source_id, r.max_group_size, r.duration_minutes,
        r.inclusions,
        (SELECT count(*) FROM booking.supplier_rates x
            JOIN booking.supplier_contracts k ON k.id = x.supplier_contract_id
         WHERE x.service_id = r.id AND {RATE_IN_FORCE_TODAY}) AS rates_in_force
    """,  # noqa: S608 — constants only
    editable=(
        "service_type",
        "title",
        "unit",
        "destination_id",
        "country_code",
        "source_id",
        "max_group_size",
        "duration_minutes",
        "inclusions",
        "internal_notes",
    ),
    text_fields=("internal_notes", "source_id"),
    list_fields=("inclusions",),
    search="(r.title ILIKE {p} OR r.code ILIKE {p} OR sp.company_name ILIKE {p})",
    sorts={
        "title": "lower(r.title)",
        "service_type": "r.service_type",
        "updated_at": "r.updated_at",
        "created_at": "r.created_at",
    },
)

KINDS = {"hotel": HOTEL, "service": SERVICE}

COMMON_COLUMNS = """
    r.id::text AS id, r.code, r.supplier_id::text AS supplier_id, sp.code AS supplier_code,
    sp.company_name AS supplier_name, sp.status AS supplier_status, r.destination_id::text AS destination_id,
    r.country_code::text AS country_code, co.name AS country_name, r.internal_notes, r.status,
    r.created_by, r.updated_by, r.created_at, r.updated_at
"""


def _from(kind: Kind) -> str:
    return (
        f"{kind.table} r JOIN booking.suppliers sp ON sp.id = r.supplier_id"
        " LEFT JOIN booking.countries co ON co.iso2 = r.country_code"
    )


def _select(kind: Kind) -> str:
    return f"SELECT {COMMON_COLUMNS}, {kind.columns} FROM {_from(kind)}"  # noqa: S608 — constants only


ROOM_TYPE_COLUMNS = f"""
    t.id::text AS id, t.hotel_id::text AS hotel_id, t.name, t.max_occupancy, t.bed_types, t.internal_notes, t.status,
    t.created_by, t.updated_by, t.created_at, t.updated_at,
    (SELECT count(*) FROM booking.supplier_rates x JOIN booking.supplier_contracts k ON k.id = x.supplier_contract_id
     WHERE x.hotel_room_type_id = t.id AND {RATE_IN_FORCE_TODAY}) AS rates_in_force
"""  # noqa: S608 — constants only
ROOM_TYPE_EDITABLE = ("name", "max_occupancy", "bed_types", "internal_notes", "status")


# ── Helpers ──────────────────────────────────────────────────────────────────


def ref_filter(kind: Kind, ref: str) -> tuple[str, str]:
    """A record is addressed by its UUID or its code (``HTL-000123`` / ``SVC-000123``)."""
    try:
        return "r.id = $1::uuid", str(uuid.UUID(ref))
    except ValueError:
        return "r.code = $1", ref.strip().upper()


def as_uuid(value: str, code: str, what: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise NotFound(code, f"{what} {value} not found") from exc


def _row(row: Any) -> dict[str, Any]:
    out = dict(row)
    for key in ("amenities", "inclusions", "bed_types"):
        if out.get(key) is not None:
            out[key] = list(out[key])
    if "links" in out:
        out["links"] = json.loads(out["links"]) if out["links"] else []
    return out


def _text_list(values: Any) -> list[str]:
    """Trim, drop empties and duplicates (case-insensitive), keep the order."""
    out: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        text = clean_text(value)
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text)
    return out


def normalize(kind: Kind, data: dict[str, Any]) -> dict[str, Any]:
    """Trim texts (empty → NULL), clean lists, upper-case the country; ``source_id`` brings ``source_system``."""
    out = dict(data)
    if kind.name_field in out:
        out[kind.name_field] = clean_text(out[kind.name_field])
        if not out[kind.name_field]:
            raise Invalid(f"{kind.name_field}_required", f"a {kind.entity} needs a {kind.name_field}")
    for key in kind.text_fields:
        if key in out:
            out[key] = clean_text(out[key])
    for key in kind.list_fields:
        if key in out:
            out[key] = _text_list(out[key])
    if "links" in out:
        out["links"] = [
            {"label": clean_text(link.get("label")) or clean_text(link.get("url")), "url": clean_text(link.get("url"))}
            for link in out["links"] or []
            if clean_text(link.get("url"))
        ]
    if "country_code" in out:
        out["country_code"] = (clean_text(out["country_code"]) or "").upper() or None
    if out.get("destination_id") is not None:
        out["destination_id"] = str(out["destination_id"])
    for key in ("service_type", "unit"):
        if key in out and out[key] is None:
            raise Invalid(f"{key}_required", f"a service needs a {key.replace('_', ' ')}")
    return out


def _check_geo(merged: dict[str, Any]) -> None:
    if (merged.get("latitude") is None) != (merged.get("longitude") is None):
        raise Invalid("geo_pair", "give both latitude and longitude, or neither")


def _db_values(values: dict[str, Any]) -> dict[str, Any]:
    """Python values → bind values: links as JSON text, ``source_system`` follows ``source_id``."""
    out = dict(values)
    if "links" in out:
        out["links"] = json.dumps(out["links"])
    if "source_id" in out:
        out["source_system"] = "cis" if out["source_id"] else None
    return out


def _cast(column: str) -> str:
    return {"destination_id": "::uuid", "links": "::jsonb"}.get(column, "")


def label(kind: Kind, record: dict[str, Any]) -> str:
    return f"{record['code']} {record[kind.name_field]}"


async def _supplier_for(conn: asyncpg.Connection, kind: Kind, ref: str, service_type: str | None) -> dict[str, Any]:
    supplier = await suppliers.resolve_supplier(conn, ref)
    if supplier["status"] != "active":
        raise Conflict("supplier_inactive", f"supplier {supplier['code']} is inactive")
    needed = "hotel" if kind is HOTEL else SERVICE_CATEGORY.get(service_type or "", "")
    if needed not in supplier["categories"]:
        raise Invalid(
            "supplier_category_missing",
            f"supplier {supplier['code']} does not have the {needed} category; add it on the supplier first",
        )
    return supplier


def _exists(kind: Kind) -> Conflict:
    where = "for this supplier at this place"
    return Conflict(f"{kind.entity}_exists", f"a {kind.entity} with this {kind.name_field} already exists {where}")


# ── Records ──────────────────────────────────────────────────────────────────


async def list_records(
    conn: asyncpg.Connection,
    kind: Kind,
    *,
    q: str | None = None,
    status: str | None = None,
    supplier: str | None = None,
    country: str | None = None,
    destination_id: str | None = None,
    service_type: str | None = None,
    star_rating: int | None = None,
    sort: str | None = None,
    limit: int = 25,
    offset: int = 0,
) -> dict[str, Any]:
    where: list[str] = []
    args: list[Any] = []

    def add(fragment: str, value: Any) -> None:
        args.append(value)
        where.append(fragment.replace("{p}", f"${len(args)}"))

    if status:
        if status not in STATUSES:
            raise Invalid("unknown_status", f"status must be one of {', '.join(STATUSES)}")
        add("r.status = {p}", status)
    if supplier:
        try:
            add("r.supplier_id = {p}::uuid", str(uuid.UUID(supplier)))
        except ValueError:
            add("sp.code = {p}", supplier.strip().upper())
    if country:
        add("r.country_code = {p}", country.strip().upper())
    if destination_id:
        add("r.destination_id = {p}::uuid", as_uuid(destination_id, "unknown_destination", "destination"))
    if service_type and kind is SERVICE:
        if service_type not in SERVICE_TYPES:
            raise Invalid("unknown_type", f"type must be one of {', '.join(SERVICE_TYPES)}")
        add("r.service_type = {p}", service_type)
    if star_rating is not None and kind is HOTEL:
        add("r.star_rating = {p}", star_rating)
    if q and q.strip():
        add(kind.search, paging.contains(q))
    raw = (sort or kind.name_field).strip()
    column = kind.sorts.get(raw.lstrip("-+"))
    if column is None:
        raise Invalid("unknown_sort", f"sort must be one of {', '.join(kind.sorts)} (prefix - for descending)")
    order = f"{column} {'DESC' if raw.startswith('-') else 'ASC'} NULLS LAST, r.id"
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    total = await conn.fetchval(f"SELECT count(*) FROM {_from(kind)} {clause}", *args)  # noqa: S608 — fixed fragments
    rows = await conn.fetch(
        f"{_select(kind)} {clause} ORDER BY {order} LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}",  # noqa: S608
        *args,
        limit,
        offset,
    )
    items = await suppliers.place_names(conn, [_row(r) for r in rows])
    return {"items": items, "total": total, "limit": limit, "offset": offset}


async def resolve(conn: asyncpg.Connection, kind: Kind, ref: str, *, lock: bool = False) -> dict[str, Any]:
    where, arg = ref_filter(kind, ref)
    row = await conn.fetchrow(f"{_select(kind)} WHERE {where}" + (" FOR UPDATE OF r" if lock else ""), arg)
    if row is None:
        raise NotFound(f"{kind.entity}_not_found", f"{kind.entity} {ref} not found")
    return _row(row)


async def get_record(conn: asyncpg.Connection, kind: Kind, ref: str) -> dict[str, Any]:
    record = (await suppliers.place_names(conn, [await resolve(conn, kind, ref)]))[0]
    if kind is HOTEL:
        record["room_types"] = await list_room_types(conn, record["id"])
    # AA-910: the rates that price this hotel's room types (or this service) show here too, deleted ones included.
    rates = HOTEL_RATES if kind is HOTEL else SERVICE_RATES
    activity = await conn.fetch(
        f"""
        SELECT l.id::text AS id, l.created_at AS at, l.actor_ref, st.name AS actor_name, l.action, l.entity_type,
               l.entity_label, l.summary
        FROM booking.audit_logs l LEFT JOIN booking.staff_users st ON st.cis_user_id = l.actor_ref
        WHERE (l.entity_type = $2 AND l.entity_id = $1::uuid)
           OR (l.entity_type = 'hotel_room_type' AND l.entity_id IN (
               SELECT t.id FROM booking.hotel_room_types t WHERE t.hotel_id = $1::uuid))
           OR (l.entity_type = 'supplier_rate' AND {rates})
           OR (l.entity_type = 'entity_media' AND l.entity_id IN (
               SELECT m.id FROM booking.entity_media m WHERE m.entity_type = $2 AND m.entity_id = $1::uuid))
        ORDER BY l.created_at DESC, l.id DESC
        LIMIT 20
        """,  # noqa: S608 — constants only
        record["id"],
        kind.entity,
    )
    record["recent_activity"] = [
        {
            **{k: r[k] for k in ("id", "at", "action", "entity_type", "entity_label", "summary")},
            "actor": {"id": r["actor_ref"], "name": r["actor_name"]},
        }
        for r in activity
    ]
    record["content"] = await content.read_content(conn, kind.entity, record["id"])
    return record


async def create_record(conn: asyncpg.Connection, kind: Kind, staff: Staff, data: dict[str, Any]) -> dict[str, Any]:
    values = normalize(kind, {k: v for k, v in data.items() if k in kind.editable})
    if not values.get(kind.name_field):
        raise Invalid(f"{kind.name_field}_required", f"a {kind.entity} needs a {kind.name_field}")
    if kind is SERVICE and not values.get("service_type"):
        raise Invalid("service_type_required", "a service needs a type")
    supplier = await _supplier_for(conn, kind, str(data.get("supplier_id") or ""), values.get("service_type"))
    _check_geo(values)
    values.update(await suppliers.check_place(conn, values, set(values)))
    db = _db_values(values)
    columns = list(db)
    marks = ", ".join(f"${i + 3}{_cast(c)}" for i, c in enumerate(columns))
    try:
        row = await conn.fetchrow(
            f"INSERT INTO {kind.table} (supplier_id, created_by, updated_by, {', '.join(columns)})"  # noqa: S608
            f" VALUES ($1::uuid, $2, $2, {marks}) RETURNING id::text AS id, code",
            supplier["id"],
            staff.cis_user_id,
            *db.values(),
        )
    except asyncpg.UniqueViolationError as exc:
        raise _exists(kind) from exc
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action=f"{kind.entity}.created",
        entity_type=kind.entity,
        entity_id=row["id"],
        after={"code": row["code"], "supplier_id": supplier["id"], **values},
        entity_label=f"{row['code']} {values[kind.name_field]}",
        summary=f"Created {kind.entity} {values[kind.name_field]} ({supplier['code']})",
    )
    return await get_record(conn, kind, row["id"])


async def update_record(
    conn: asyncpg.Connection, kind: Kind, staff: Staff, ref: str, changes: dict[str, Any]
) -> dict[str, Any]:
    unknown = sorted(set(changes) - set(kind.editable))
    if unknown:
        raise Invalid("not_editable", f"not editable: {', '.join(unknown)}")
    current = await resolve(conn, kind, ref, lock=True)
    values = normalize(kind, changes)
    changed = {k: v for k, v in values.items() if current[k] != v}
    if not changed:
        return {**current, "changed": []}
    merged = {**current, **changed}
    _check_geo(merged)
    if "service_type" in changed:
        await _supplier_for(conn, kind, current["supplier_id"], merged["service_type"])
    if "unit" in changed:
        has_rates = await conn.fetchval(
            "SELECT 1 FROM booking.supplier_rates WHERE service_id = $1::uuid LIMIT 1", current["id"]
        )
        if has_rates:
            raise Conflict("service_has_rates", "the unit of a service with rates cannot change; add a new service")
    for key, value in (await suppliers.check_place(conn, merged, set(changed))).items():
        # A derived country wins over the requested one; equal to the stored value → nothing to write.
        if current[key] != value:
            changed[key] = value
        else:
            changed.pop(key, None)
    if not changed:
        return {**current, "changed": []}
    db = _db_values(changed)
    sets = ", ".join(f"{column} = ${i + 2}{_cast(column)}" for i, column in enumerate(db))
    try:
        await conn.execute(
            f"UPDATE {kind.table} SET {sets}, updated_by = ${len(db) + 2} WHERE id = $1::uuid",  # noqa: S608
            current["id"],
            *db.values(),
            staff.cis_user_id,
        )
    except asyncpg.UniqueViolationError as exc:
        raise _exists(kind) from exc
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action=f"{kind.entity}.updated",
        entity_type=kind.entity,
        entity_id=current["id"],
        before={k: current[k] for k in changed},
        after=changed,
        entity_label=label(kind, merged),
        summary=f"Updated {', '.join(sorted(changed))}",
    )
    return {**(await get_record(conn, kind, current["id"])), "changed": sorted(changed)}


async def set_status(
    conn: asyncpg.Connection, kind: Kind, staff: Staff, ref: str, target: str, reason: str | None = None
) -> dict[str, Any]:
    if target not in STATUSES:
        raise Invalid("unknown_status", f"status must be one of {', '.join(STATUSES)}")
    current = await resolve(conn, kind, ref, lock=True)
    if current["status"] == target:
        return {**current, "changed": []}
    if target == "active" and current["supplier_status"] != "active":
        raise Conflict("supplier_inactive", f"activate supplier {current['supplier_code']} first")
    await conn.execute(
        f"UPDATE {kind.table} SET status = $2, updated_by = $3 WHERE id = $1::uuid",  # noqa: S608 — constant table
        current["id"],
        target,
        staff.cis_user_id,
    )
    note = clean_text(reason)
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action=f"{kind.entity}.{'deactivated' if target == 'inactive' else 'activated'}",
        entity_type=kind.entity,
        entity_id=current["id"],
        before={"status": current["status"]},
        after={"status": target, **({"reason": note} if note else {})},
        entity_label=label(kind, current),
        summary=("Deactivated" if target == "inactive" else "Activated") + (f": {note}" if note else ""),
    )
    return {**(await get_record(conn, kind, current["id"])), "changed": ["status"]}


# ── Room types ───────────────────────────────────────────────────────────────


def _room_row(row: Any) -> dict[str, Any]:
    out = dict(row)
    out["bed_types"] = list(out.get("bed_types") or [])
    return out


async def list_room_types(conn: asyncpg.Connection, hotel_id: str) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        f"SELECT {ROOM_TYPE_COLUMNS} FROM booking.hotel_room_types t"  # noqa: S608 — constant
        " WHERE t.hotel_id = $1::uuid ORDER BY t.status, lower(t.name), t.id",
        hotel_id,
    )
    return [_room_row(r) for r in rows]


async def get_room_type(conn: asyncpg.Connection, room_type_id: str, *, lock: bool = False) -> dict[str, Any]:
    rid = as_uuid(room_type_id, "room_type_not_found", "room type")
    row = await conn.fetchrow(
        f"SELECT {ROOM_TYPE_COLUMNS} FROM booking.hotel_room_types t WHERE t.id = $1::uuid"  # noqa: S608
        + (" FOR UPDATE OF t" if lock else ""),
        rid,
    )
    if row is None:
        raise NotFound("room_type_not_found", f"room type {room_type_id} not found")
    return _room_row(row)


def _normalize_room(data: dict[str, Any]) -> dict[str, Any]:
    out = dict(data)
    if "name" in out:
        out["name"] = clean_text(out["name"])
        if not out["name"]:
            raise Invalid("name_required", "a room type needs a name")
    if "internal_notes" in out:
        out["internal_notes"] = clean_text(out["internal_notes"])
    if "bed_types" in out:
        out["bed_types"] = _text_list(out["bed_types"])
    if "status" in out and out["status"] not in STATUSES:
        raise Invalid("unknown_status", f"status must be one of {', '.join(STATUSES)}")
    return out


async def create_room_type(
    conn: asyncpg.Connection, staff: Staff, hotel_ref: str, data: dict[str, Any]
) -> dict[str, Any]:
    hotel = await resolve(conn, HOTEL, hotel_ref, lock=True)
    if hotel["status"] != "active":
        raise Conflict("hotel_inactive", f"activate hotel {hotel['code']} before adding room types")
    values = _normalize_room({k: v for k, v in data.items() if k in ROOM_TYPE_EDITABLE and k != "status"})
    if not values.get("name"):
        raise Invalid("name_required", "a room type needs a name")
    columns = list(values)
    marks = ", ".join(f"${i + 3}" for i in range(len(columns)))
    try:
        rid = await conn.fetchval(
            f"INSERT INTO booking.hotel_room_types (hotel_id, created_by, updated_by, {', '.join(columns)})"  # noqa: S608
            f" VALUES ($1::uuid, $2, $2, {marks}) RETURNING id::text",
            hotel["id"],
            staff.cis_user_id,
            *values.values(),
        )
    except asyncpg.UniqueViolationError as exc:
        raise Conflict("room_type_exists", "this hotel already has a room type with this name") from exc
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="hotel_room_type.created",
        entity_type="hotel_room_type",
        entity_id=rid,
        after={"hotel_id": hotel["id"], **values},
        entity_label=f"{hotel['code']} · {values['name']}",
        summary=f"Added room type {values['name']}",
    )
    return await get_room_type(conn, rid)


async def update_room_type(
    conn: asyncpg.Connection, staff: Staff, room_type_id: str, changes: dict[str, Any]
) -> dict[str, Any]:
    unknown = sorted(set(changes) - set(ROOM_TYPE_EDITABLE))
    if unknown:
        raise Invalid("not_editable", f"not editable: {', '.join(unknown)}")
    current = await get_room_type(conn, room_type_id, lock=True)
    hotel = await resolve(conn, HOTEL, current["hotel_id"])
    values = _normalize_room(changes)
    changed = {k: v for k, v in values.items() if current[k] != v}
    if not changed:
        return {**current, "changed": []}
    if changed.get("status") == "active" and hotel["status"] != "active":
        raise Conflict("hotel_inactive", f"activate hotel {hotel['code']} first")
    sets = ", ".join(f"{column} = ${i + 2}" for i, column in enumerate(changed))
    try:
        await conn.execute(
            f"UPDATE booking.hotel_room_types SET {sets}, updated_by = ${len(changed) + 2} WHERE id = $1::uuid",  # noqa: S608
            current["id"],
            *changed.values(),
            staff.cis_user_id,
        )
    except asyncpg.UniqueViolationError as exc:
        raise Conflict("room_type_exists", "this hotel already has a room type with this name") from exc
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="hotel_room_type.updated",
        entity_type="hotel_room_type",
        entity_id=current["id"],
        before={k: current[k] for k in changed},
        after=changed,
        entity_label=f"{hotel['code']} · {changed.get('name', current['name'])}",
        summary=f"Updated {', '.join(sorted(changed))}",
    )
    return {**(await get_room_type(conn, current["id"])), "changed": sorted(changed)}


# ── Picker for version items ─────────────────────────────────────────────────

OPTION_KINDS = ("room_type", "service")
MAX_OPTIONS = 50


async def search_options(
    conn: asyncpg.Connection,
    *,
    q: str | None = None,
    kind: str | None = None,
    supplier_id: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Active room types (of active hotels) and active services a version item can book (or a rate can price),
    optionally of one supplier."""
    if kind is not None and kind not in OPTION_KINDS:
        raise Invalid("unknown_kind", f"kind must be one of {', '.join(OPTION_KINDS)}")
    supplier = as_uuid(supplier_id, "supplier_not_found", "supplier") if supplier_id else None
    pattern = paging.contains(q) if q and q.strip() else None
    rows = await conn.fetch(
        """
        SELECT * FROM (
            SELECT 'room_type' AS kind, t.id::text AS id, h.code, h.name || ' · ' || t.name AS label,
                   sp.company_name AS supplier_name, h.destination_id::text AS destination_id,
                   h.country_code::text AS country_code, 'room_night' AS unit
            FROM booking.hotel_room_types t
            JOIN booking.hotels h ON h.id = t.hotel_id
            JOIN booking.suppliers sp ON sp.id = h.supplier_id
            WHERE t.status = 'active' AND h.status = 'active'
              AND ($1::text IS NULL OR h.name ILIKE $1 OR t.name ILIKE $1 OR h.code ILIKE $1)
              AND ($4::uuid IS NULL OR h.supplier_id = $4::uuid)
            UNION ALL
            SELECT 'service', s.id::text, s.code, s.title, sp.company_name, s.destination_id::text,
                   s.country_code::text, s.unit
            FROM booking.services s JOIN booking.suppliers sp ON sp.id = s.supplier_id
            WHERE s.status = 'active' AND ($1::text IS NULL OR s.title ILIKE $1 OR s.code ILIKE $1)
              AND ($4::uuid IS NULL OR s.supplier_id = $4::uuid)
        ) o
        WHERE ($2::text IS NULL OR o.kind = $2)
        ORDER BY lower(o.label), o.id
        LIMIT $3
        """,
        pattern,
        kind,
        min(limit, MAX_OPTIONS),
        supplier,
    )
    return {"items": [dict(r) for r in rows]}
