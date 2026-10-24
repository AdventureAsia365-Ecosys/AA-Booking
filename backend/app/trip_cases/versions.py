"""Proposal versions of a trip case and their free-text itinerary items (bk-003, bk-005).

- **V1** is pre-filled from ``trip_case_legs`` (the TripPlanner leg chain) ordered by ``day_from``,
  then ``position``. The item title is the inquiry snapshot's first day name for that tour, else
  ``AA tour <source_tour_id>`` (catalog titles arrive with AA-679).
- **Vn** (n > 1) copies the latest live version: title, notes, currency, channel and every item. The total
  is NOT copied: a new version starts unpriced, so it can never be sent with a price set for other items.
- **A price belongs to one item list**: saving items that differ from the stored list clears ``total_price``
  (the version must be priced again before it can be sent).
- **Items keep their ids** (D2): a PUT updates the items it names by ``id``, inserts the ones without an
  id and deletes only the ones left out, so rows that reference an item (bk-004 ``version_item_costs``,
  ``ON DELETE CASCADE``) survive a save.
- **Send** marks the version sent (validity date, sender), moves the case to ``proposal_sent`` and writes a
  customer notification row. No e-mail is sent. From then on the database locks the version's commercial
  fields and its items (bk-005 trigger); staff make a new version instead.
- **Approve** records the customer's approval of a sent version (staff action until the customer app,
  O8): the version is approved (bk-003 lock) and the case moves ``proposal_sent -> confirmed`` (D3).
- **Delete** soft-deletes an unsent, unapproved draft.
- **Net costs** (bk-004 ``version_item_costs``, AA-810): each item carries its supplier net cost lines. ``GET
  …/items`` returns them per item; ``PUT …/items`` replaces an item's lines when the item has ``costs`` (omit it
  to leave them alone). Vn copies the lines with the items. Changing a line clears the version's price.
- **Supply** (bk-010, AA-823): an item may book one hotel room type or one service. When it starts referencing one
  (or references one and has no cost lines) and the PUT gives no ``costs`` for it, the item takes the rate in force as
  its default net cost line (``app.supply.rates.default_cost``; ``supplier_rate_id`` keeps the provenance). The
  response's ``rate_defaults`` lists what was defaulted, or that no rate was in force.
- **CIS content** (AA-835, root ADR 0004a): an item that books a room type or a service carries the description and
  highlights CIS **published** for its hotel / service (``supply.description`` / ``supply.highlights``, read live from
  ``catalog.v_supply_content``); a draft, in review, stale or unpublished entity gives null / [], and so does a
  Booking without CIS 278 (``app.supply.content``).
- **Price** (``POST …/versions/{n}/price``) runs the pricing engine (``app.pricing.service``, the same code as
  quote-preview) on the stored cost lines and saves the total, selling currency, channel and the selling FX rate
  on a draft. A total below the channel floor needs a reason (audited). Send then locks all of it (bk-005).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import asyncpg

from app import audit, paging
from app.auth.staff import Staff
from app.errors import Conflict, Invalid, NotFound, Unavailable
from app.notifications import service as notifications
from app.pricing import engine
from app.pricing import service as pricing
from app.supply import content as supply_content
from app.supply import rates as supply_rates
from app.trip_cases import sql
from app.trip_cases.service import ensure_staff_managed, is_terminal, resolve_case, set_status
from app.trip_cases.sql import leg_title  # noqa: F401 — re-exported (tests, older imports)

MAX_ITEMS = 200
# A PUT parks kept items at position + this offset before renumbering, so the bk-003 unique
# (version, position) constraint never collides mid-save. MAX_ITEMS + offset stays under SMALLINT.
POSITION_PARK = 1000
VALIDITY_MIN_DAYS = 7
VALIDITY_MAX_DAYS = 30
DEFAULT_VALIDITY_DAYS = 14
# Statuses from which "send" may walk the pre-sale chain to proposal_sent (each hop is an allowed edge).
SEND_CHAIN = ("discovery", "building", "proposal_sent")
VERSION_SORTS = {"updated_at": "v.updated_at", "created_at": "v.created_at", "version_number": "v.version_number"}

ITEM_COLUMNS = "position, title, day_from, day_to, source_tour_id, notes, hotel_room_type_id, service_id"
ITEM_SELECT = (
    "position, title, day_from, day_to, source_tour_id, notes,"
    " hotel_room_type_id::text AS hotel_room_type_id, service_id::text AS service_id"
)
MAX_COSTS_PER_ITEM = 20
# trip_case_versions.total_price is NUMERIC(12, 2).
MAX_TOTAL = Decimal("9999999999.99")
# An item's cost lines as JSON text, in entry order (amounts as text: no float round trip).
ITEM_COSTS_JSON = """
    (SELECT coalesce(json_agg(json_build_object(
                'id', c.id::text, 'net_amount', c.net_amount::text, 'net_currency', c.net_currency::text,
                'supplier_note', c.supplier_note, 'supplier_rate_id', c.supplier_rate_id::text)
                ORDER BY c.created_at, c.id), '[]'::json)::text
     FROM booking.version_item_costs c WHERE c.version_item_id = booking.trip_case_version_itinerary.id) AS costs
"""
# The hotel room type or service an item books, as JSON text (null when none). ``entity_type`` / ``entity_id`` name the
# hotel or service whose CIS content the item may carry (AA-835: a room type's content is its hotel's).
ITEM_SUPPLY_JSON = """
    coalesce(
        (SELECT json_build_object('kind', 'room_type', 'id', t.id::text, 'code', h.code,
                    'label', h.name || ' · ' || t.name, 'active', t.status = 'active' AND h.status = 'active',
                    'entity_type', 'hotel', 'entity_id', h.id::text)::text
         FROM booking.hotel_room_types t JOIN booking.hotels h ON h.id = t.hotel_id
         WHERE t.id = booking.trip_case_version_itinerary.hotel_room_type_id),
        (SELECT json_build_object('kind', 'service', 'id', s.id::text, 'code', s.code, 'label', s.title,
                    'active', s.status = 'active', 'entity_type', 'service', 'entity_id', s.id::text)::text
         FROM booking.services s WHERE s.id = booking.trip_case_version_itinerary.service_id)) AS supply
"""


@dataclass(frozen=True)
class Cost:
    net_amount: Decimal
    net_currency: str
    supplier_note: str | None = None
    supplier_rate_id: str | None = None


@dataclass(frozen=True)
class Item:
    title: str
    day_from: int
    day_to: int
    source_tour_id: str | None = None
    notes: str | None = None
    id: str | None = None
    # None = keep the item's stored cost lines; a tuple (even empty) replaces them.
    costs: tuple[Cost, ...] | None = None
    hotel_room_type_id: str | None = None
    service_id: str | None = None

    @property
    def supply(self) -> tuple[str | None, str | None]:
        return (self.hotel_room_type_id, self.service_id)


def prefill_from_legs(legs: list[Any], snapshot: dict[str, Any] | None) -> list[Item]:
    ordered = sorted(legs, key=lambda leg: (leg["day_from"], leg["position"]))
    return [
        Item(
            title=sql.leg_title(snapshot, leg["source_tour_id"]),
            day_from=leg["day_from"],
            day_to=leg["day_to"],
            source_tour_id=leg["source_tour_id"],
        )
        for leg in ordered
    ]


async def _items(conn: asyncpg.Connection, version_id: str, *, content: bool = False) -> list[dict[str, Any]]:
    """The items of a version in order. ``content``: each item that books supply also carries the CIS-published
    description and highlights of its hotel / service (AA-835; responses only, never the write-path comparisons)."""
    rows = await conn.fetch(
        f"SELECT id::text AS id, {ITEM_SELECT}, {ITEM_COSTS_JSON}, {ITEM_SUPPLY_JSON}"  # noqa: S608 — constant
        " FROM booking.trip_case_version_itinerary WHERE trip_case_version_id = $1::uuid ORDER BY position",
        version_id,
    )
    out = []
    for row in rows:
        item = dict(row)
        item["costs"] = sql.loads(item.get("costs"), [])
        item["supply"] = sql.loads(item.get("supply"), None)
        out.append(item)
    if content:
        await supply_content.attach_to_items(conn, out)
    return out


# A cost line is compared by value; ``supplier_rate_id`` is provenance only, so a client that sends the same lines
# back without it keeps the stored lines (and their provenance) and the version keeps its price.
CostKey = tuple[Decimal, str, str | None]


def _cost_key(net_amount: Any, net_currency: str, supplier_note: str | None) -> CostKey:
    return (Decimal(str(net_amount)), net_currency.strip(), supplier_note or None)


def _stored_costs(item: dict[str, Any]) -> list[CostKey]:
    return [_cost_key(c["net_amount"], c["net_currency"], c.get("supplier_note")) for c in item.get("costs") or []]


async def _write_costs(conn: asyncpg.Connection, staff: Staff, item_id: str, costs: tuple[Cost, ...]) -> None:
    await conn.execute("DELETE FROM booking.version_item_costs WHERE version_item_id = $1::uuid", item_id)
    if costs:
        # clock_timestamp() keeps the entry order (now() is the same for every row of the transaction).
        await conn.executemany(
            "INSERT INTO booking.version_item_costs"
            " (version_item_id, net_amount, net_currency, supplier_note, entered_by, supplier_rate_id, created_at)"
            " VALUES ($1::uuid, $2, $3, $4, $5, $6::uuid, clock_timestamp())",
            [
                (item_id, c.net_amount, c.net_currency, c.supplier_note, staff.cis_user_id, c.supplier_rate_id)
                for c in costs
            ],
        )


def _item_rows(items: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    """Stored items as comparable tuples (id + the saved fields, in position order)."""
    return [
        (
            i["id"],
            i["position"],
            i["title"],
            i["day_from"],
            i["day_to"],
            i["source_tour_id"],
            i["notes"],
            i.get("hotel_room_type_id"),
            i.get("service_id"),
        )
        for i in items
    ]


def _given_rows(positioned: list[tuple[int, Item]]) -> list[tuple[Any, ...]]:
    return [
        (it.id, pos, it.title, it.day_from, it.day_to, it.source_tour_id, it.notes, *it.supply)
        for pos, it in positioned
    ]


async def _insert_items(conn: asyncpg.Connection, version_id: str, items: list[tuple[int, Item]]) -> None:
    if not items:
        return
    await conn.executemany(
        "INSERT INTO booking.trip_case_version_itinerary"
        " (trip_case_version_id, position, title, day_from, day_to, source_tour_id, notes, hotel_room_type_id,"
        " service_id) VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8::uuid, $9::uuid)",
        [
            (version_id, pos, it.title, it.day_from, it.day_to, it.source_tour_id, it.notes, *it.supply)
            for pos, it in items
        ],
    )


async def _check_supply(
    conn: asyncpg.Connection, before: list[dict[str, Any]], positioned: list[tuple[int, Item]]
) -> None:
    """Referenced room types / services exist, a newly referenced one is active, and a cost line names only a rate of
    a published (or expired) contract: draft rates are not prices yet."""
    stored = {i["id"]: (i.get("hotel_room_type_id"), i.get("service_id")) for i in before}
    rooms = sorted({it.hotel_room_type_id for _, it in positioned if it.hotel_room_type_id})
    services = sorted({it.service_id for _, it in positioned if it.service_id})
    active: dict[str, bool] = {}
    if rooms:
        for r in await conn.fetch(
            "SELECT t.id::text AS id, t.status = 'active' AND h.status = 'active' AS active"
            " FROM booking.hotel_room_types t JOIN booking.hotels h ON h.id = t.hotel_id WHERE t.id = ANY($1::uuid[])",
            rooms,
        ):
            active[r["id"]] = r["active"]
    if services:
        for r in await conn.fetch(
            "SELECT s.id::text AS id, s.status = 'active' AS active FROM booking.services s"
            " WHERE s.id = ANY($1::uuid[])",
            services,
        ):
            active[r["id"]] = r["active"]
    unknown = sorted((set(rooms) | set(services)) - set(active))
    if unknown:
        raise Invalid("unknown_supply", f"room type or service {unknown[0]} does not exist", supply_ids=unknown)
    for pos, it in positioned:
        ref = it.hotel_room_type_id or it.service_id
        if ref and it.supply != stored.get(it.id or "", (None, None)) and not active[ref]:
            raise Invalid("supply_inactive", f"item {pos} books an inactive room type or service", position=pos)
    rate_ids = sorted({c.supplier_rate_id for _, it in positioned for c in it.costs or () if c.supplier_rate_id})
    if rate_ids:
        known = await conn.fetch(
            "SELECT x.id::text AS id FROM booking.supplier_rates x"
            " JOIN booking.supplier_contracts k ON k.id = x.supplier_contract_id"
            " WHERE x.id = ANY($1::uuid[]) AND k.status <> 'draft'",
            rate_ids,
        )
        bad = sorted(set(rate_ids) - {r["id"] for r in known})
        if bad:
            raise Invalid("unknown_rate", f"rate {bad[0]} is not a published supplier rate", rate_ids=bad)


async def _supply_defaults(
    conn: asyncpg.Connection, case: Any, before: list[dict[str, Any]], positioned: list[tuple[int, Item]]
) -> tuple[list[tuple[int, Item]], list[dict[str, Any]]]:
    """Give an item that books a room type or service, and sends no ``costs``, the net of the rate in force when it
    starts booking it or has no cost line yet. Returns the items and a report per defaulted item."""
    if not any(it.hotel_room_type_id or it.service_id or it.costs for _, it in positioned):
        return positioned, []
    await _check_supply(conn, before, positioned)
    stored = {i["id"]: i for i in before}
    start, pax = case.get("start_date"), case.get("pax")
    out: list[tuple[int, Item]] = []
    report: list[dict[str, Any]] = []
    for pos, it in positioned:
        old = stored.get(it.id or "")
        old_ref = (old.get("hotel_room_type_id"), old.get("service_id")) if old else (None, None)
        books = it.hotel_room_type_id or it.service_id
        if books and it.costs is None and (it.supply != old_ref or not (old and old["costs"])):
            day = start + timedelta(days=it.day_from - 1) if start else datetime.now(UTC).date()
            rate = await supply_rates.rate_in_force(
                conn, room_type_id=it.hotel_room_type_id, service_id=it.service_id, on=day, pax=pax
            )
            if rate is None:
                report.append({"position": pos, "on": day, "rate_id": None})
            else:
                d = supply_rates.default_cost(rate, day_from=it.day_from, day_to=it.day_to, pax=pax)
                it = replace(it, costs=(Cost(d.net_amount, d.net_currency, d.supplier_note, d.supplier_rate_id),))
                report.append(
                    {
                        "position": pos,
                        "on": day,
                        "rate_id": d.supplier_rate_id,
                        "net_amount": str(d.net_amount),
                        "net_currency": d.net_currency,
                    }
                )
        out.append((pos, it))
    return out, report


async def _version(conn: asyncpg.Connection, case_id: str, number: int, *, lock: bool = False) -> Any:
    row = await conn.fetchrow(
        f"SELECT {sql.VERSION_COLUMNS} FROM booking.trip_case_versions v"  # noqa: S608 — constant
        " WHERE v.trip_case_id = $1::uuid AND v.version_number = $2 AND v.deleted_at IS NULL"
        f"{' FOR UPDATE OF v' if lock else ''}",
        case_id,
        number,
    )
    if row is None:
        raise NotFound("version_not_found", f"version {number} not found")
    return row


def _label(case: Any, number: int) -> str:
    return f"{case['code']} V{number}"


async def _version_badges(conn: asyncpg.Connection, case_id: str) -> dict[str, tuple[int, str]]:
    """{version id: (number, derived badge)} of the case's live versions (``sql.VERSION_BADGE``)."""
    rows = await conn.fetch(
        f"SELECT v.id::text AS id, v.version_number, {sql.VERSION_BADGE} AS badge"  # noqa: S608 — constant
        " FROM booking.trip_case_versions AS v WHERE v.trip_case_id = $1::uuid AND v.deleted_at IS NULL",
        case_id,
    )
    return {r["id"]: (r["version_number"], r["badge"]) for r in rows}


async def _audit_superseded(
    conn: asyncpg.Connection,
    staff: Staff,
    case: Any,
    before: dict[str, tuple[int, str]],
    number: int,
    how: str,
) -> None:
    """AA-910: one ``trip_case_version.superseded`` audit row per version that the send / approval of V``number``
    just made superseded. The badge is derived (nothing is written to those versions), so without this row the trail
    showed V2 sent but never that V1 stopped being the offer."""
    after = await _version_badges(conn, case["id"])
    for vid, (n, badge) in sorted(after.items(), key=lambda kv: kv[1][0]):
        old = before.get(vid, (n, None))[1]
        if badge != "superseded" or old == "superseded" or n == number:
            continue
        await audit.record(
            conn,
            actor_type="staff",
            actor_ref=staff.cis_user_id,
            action="trip_case_version.superseded",
            entity_type="trip_case_version",
            entity_id=vid,
            before={"badge": old},
            after={"badge": "superseded", "superseded_by": number, "by": how},
            entity_label=_label(case, n),
            summary=f"V{n} superseded: V{number} was {how}",
        )


def _editable(version: dict[str, Any]) -> None:
    if version["is_approved"]:
        raise Conflict("version_locked", "an approved version cannot change")
    if version["is_sent_to_customer"]:
        raise Conflict("version_sent", "this version was sent to the customer; create a new version")


@dataclass(frozen=True)
class Seed:
    """Items and defaults of a version started from somewhere other than the case itself (a CIS trip plan,
    ``app.catalog.trip_plans``): the version gets these items instead of the legs (V1) or a copy (Vn)."""

    title: str | None
    channel: str | None
    items: list[Item]
    label: str
    source_id: str | None = None


async def create_version(
    conn: asyncpg.Connection, staff: Staff, ref: str, notes: str | None, *, seed: Seed | None = None
) -> dict[str, Any]:
    case = await resolve_case(conn, ref, lock=True)  # the lock serialises version numbering per case
    if is_terminal(case["status"]):
        raise Conflict("trip_case_closed", f"trip case is {case['status']}")
    latest = await conn.fetchrow(
        "SELECT id::text AS id, version_number, title, notes, currency, channel"
        " FROM booking.trip_case_versions WHERE trip_case_id = $1::uuid AND deleted_at IS NULL"
        " ORDER BY version_number DESC LIMIT 1",
        case["id"],
    )
    # Numbers are never reused, even after a draft was deleted.
    number = (
        await conn.fetchval(
            "SELECT coalesce(max(version_number), 0) + 1 FROM booking.trip_case_versions WHERE trip_case_id = $1::uuid",
            case["id"],
        )
        or 1
    )
    if seed is not None:
        copied_from = None
        items = seed.items
        values = (seed.title, notes, None, case["currency"], seed.channel)
    elif latest is None:
        copied_from = None
        legs = await conn.fetch(
            "SELECT position, source_tour_id, day_from, day_to FROM booking.trip_case_legs"
            " WHERE trip_case_id = $1::uuid ORDER BY day_from, position",
            case["id"],
        )
        snapshot = json.loads(case["inquiry_snapshot"]) if case["inquiry_snapshot"] else None
        items = prefill_from_legs(list(legs), snapshot)
        values = (None, notes, None, case["currency"], None)
    else:
        copied_from = latest["version_number"]
        items = []
        values = (
            latest["title"],
            notes if notes is not None else latest["notes"],
            None,  # never copy the total: it was set for the previous version's items (review finding 2)
            latest["currency"],
            latest["channel"],
        )
    version_id = await conn.fetchval(
        "INSERT INTO booking.trip_case_versions"
        " (trip_case_id, version_number, title, notes, total_price, currency, channel, created_by, updated_by)"
        " VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $8) RETURNING id::text",
        case["id"],
        number,
        *values,
        staff.cis_user_id,
    )
    if seed is not None or latest is None:
        await _insert_items(conn, version_id, list(enumerate(items, start=1)))
    else:
        await conn.execute(
            f"INSERT INTO booking.trip_case_version_itinerary (trip_case_version_id, {ITEM_COLUMNS})"  # noqa: S608
            f" SELECT $1::uuid, {ITEM_COLUMNS} FROM booking.trip_case_version_itinerary"
            " WHERE trip_case_version_id = $2::uuid ORDER BY position",
            version_id,
            latest["id"],
        )
        # The net cost lines follow their item (matched by position, which the copy keeps).
        await conn.execute(
            "INSERT INTO booking.version_item_costs"
            " (version_item_id, net_amount, net_currency, supplier_note, entered_by, supplier_rate_id, created_at)"
            " SELECT ni.id, c.net_amount, c.net_currency, c.supplier_note, c.entered_by, c.supplier_rate_id,"
            " clock_timestamp()"
            " FROM booking.trip_case_version_itinerary oi"
            " JOIN booking.trip_case_version_itinerary ni"
            "   ON ni.trip_case_version_id = $1::uuid AND ni.position = oi.position"
            " JOIN booking.version_item_costs c ON c.version_item_id = oi.id"
            " WHERE oi.trip_case_version_id = $2::uuid ORDER BY oi.position, c.created_at, c.id",
            version_id,
            latest["id"],
        )
    version = sql.version_dict(await _version(conn, case["id"], number))
    version["items"] = await _items(conn, version_id, content=True)
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
            **({"trip_plan_source_id": seed.source_id} if seed is not None else {}),
        },
        entity_label=_label(case, number),
        summary=f"Created V{number}"
        + (
            f" from trip plan {seed.label}"
            if seed is not None
            else f" from V{copied_from}"
            if copied_from
            else " from the trip legs"
        ),
    )
    return version


async def get_version(conn: asyncpg.Connection, ref: str, number: int) -> dict[str, Any]:
    case = await resolve_case(conn, ref)
    version = sql.version_dict(await _version(conn, case["id"], number))
    version["trip_case"] = {"id": case["id"], "code": case["code"], "status": case["status"]}
    version["item_count"] = await conn.fetchval(
        "SELECT count(*) FROM booking.trip_case_version_itinerary WHERE trip_case_version_id = $1::uuid",
        version["id"],
    )
    return version


async def get_items(conn: asyncpg.Connection, ref: str, number: int) -> dict[str, Any]:
    case = await resolve_case(conn, ref)
    version = sql.version_dict(await _version(conn, case["id"], number))
    version["items"] = await _items(conn, version["id"], content=True)
    return version


async def update_version(
    conn: asyncpg.Connection, staff: Staff, ref: str, number: int, changes: dict[str, Any]
) -> dict[str, Any]:
    """``title`` / ``notes`` of a draft (409 once sent or approved)."""
    unknown = sorted(set(changes) - {"title", "notes"})
    if unknown:
        raise Invalid("not_editable", f"not editable: {', '.join(unknown)}")
    case = await resolve_case(conn, ref, lock=True)
    version = dict(await _version(conn, case["id"], number, lock=True))
    _editable(version)
    changed = {k: v for k, v in changes.items() if version[k] != v}
    if not changed:
        return {**sql.version_dict(version), "changed": []}
    sets = ", ".join(f"{column} = ${i + 2}" for i, column in enumerate(changed))
    await conn.execute(
        f"UPDATE booking.trip_case_versions SET {sets}, updated_by = ${len(changed) + 2}"  # noqa: S608 — whitelisted
        " WHERE id = $1::uuid",
        version["id"],
        *changed.values(),
        staff.cis_user_id,
    )
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="trip_case_version.updated",
        entity_type="trip_case_version",
        entity_id=version["id"],
        before={k: version[k] for k in changed},
        after=changed,
        entity_label=_label(case, number),
        summary=f"Updated {', '.join(sorted(changed))}",
    )
    return {**sql.version_dict(await _version(conn, case["id"], number)), "changed": sorted(changed)}


async def delete_version(conn: asyncpg.Connection, staff: Staff, ref: str, number: int) -> None:
    """Soft-delete an unsent, unapproved draft. The number is not reused."""
    case = await resolve_case(conn, ref, lock=True)
    version = dict(await _version(conn, case["id"], number, lock=True))
    _editable(version)
    await conn.execute(
        "UPDATE booking.trip_case_versions SET deleted_at = now(), updated_by = $2 WHERE id = $1::uuid",
        version["id"],
        staff.cis_user_id,
    )
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="trip_case_version.deleted",
        entity_type="trip_case_version",
        entity_id=version["id"],
        before={"version_number": number, "title": version["title"], "badge": version["badge"]},
        entity_label=_label(case, number),
        summary=f"Deleted draft V{number}",
    )


async def replace_items(
    conn: asyncpg.Connection, staff: Staff, ref: str, number: int, items: list[Item]
) -> dict[str, Any]:
    """Save the item list in the given order (D2: upsert by id, delete only the items left out)."""
    case = await resolve_case(conn, ref, lock=True)
    if is_terminal(case["status"]):
        raise Conflict("trip_case_closed", f"trip case is {case['status']}")
    version = dict(await _version(conn, case["id"], number, lock=True))
    _editable(version)
    before = await _items(conn, version["id"])
    price_before = version["total_price"]
    existing = {i["id"] for i in before}
    given = [it.id for it in items if it.id]
    if len(given) != len(set(given)):
        raise Invalid("duplicate_item_id", "an item id appears twice")
    foreign = sorted(set(given) - existing)
    if foreign:
        raise Invalid("unknown_item", f"item {foreign[0]} is not on this version", item_ids=foreign)
    removed = sorted(existing - set(given))
    positioned, rate_defaults = await _supply_defaults(conn, case, before, list(enumerate(items, start=1)))
    stored_costs = {i["id"]: _stored_costs(i) for i in before}
    new_costs = {
        pos: [_cost_key(c.net_amount, c.net_currency, c.supplier_note) for c in it.costs]
        for pos, it in positioned
        if it.costs is not None
    }
    # Rewrite an item's lines only when they differ (or the item is new): unchanged lines keep their ids.
    rewrite = {pos for pos, lines in new_costs.items() if lines != stored_costs.get(positioned[pos - 1][1].id, [])}
    changed = bool(rewrite) or _item_rows(before) != _given_rows(positioned)
    try:
        if removed:
            await conn.execute("DELETE FROM booking.trip_case_version_itinerary WHERE id = ANY($1::uuid[])", removed)
        if given:
            await conn.execute(
                "UPDATE booking.trip_case_version_itinerary SET position = position + $2 WHERE id = ANY($1::uuid[])",
                given,
                POSITION_PARK,
            )
            await conn.executemany(
                "UPDATE booking.trip_case_version_itinerary SET position = $2, title = $3, day_from = $4,"
                " day_to = $5, source_tour_id = $6, notes = $7, hotel_room_type_id = $8::uuid, service_id = $9::uuid"
                " WHERE id = $1::uuid",
                [row for row in _given_rows(positioned) if row[0]],
            )
        await _insert_items(conn, version["id"], [(pos, it) for pos, it in positioned if not it.id])
        if rewrite:
            ids_by_position = {
                r["position"]: r["id"]
                for r in await conn.fetch(
                    "SELECT id::text AS id, position FROM booking.trip_case_version_itinerary"
                    " WHERE trip_case_version_id = $1::uuid",
                    version["id"],
                )
            }
            for pos in sorted(rewrite):
                await _write_costs(conn, staff, ids_by_position[pos], positioned[pos - 1][1].costs or ())
        # A price was set for the old items and costs; once they change the version must be priced again.
        await conn.execute(
            "UPDATE booking.trip_case_versions SET updated_at = now(), updated_by = $2,"
            " total_price = CASE WHEN $3 THEN NULL ELSE total_price END,"
            " fx_rate_locked = CASE WHEN $3 THEN NULL ELSE fx_rate_locked END,"
            " fx_as_of = CASE WHEN $3 THEN NULL ELSE fx_as_of END WHERE id = $1::uuid",
            version["id"],
            staff.cis_user_id,
            changed,
        )
    except asyncpg.ObjectNotInPrerequisiteStateError as exc:  # approved / sent meanwhile (DB lock triggers)
        raise Conflict("version_locked", "the version is locked") from exc
    version = sql.version_dict(await _version(conn, case["id"], number))
    version["items"] = await _items(conn, version["id"], content=True)
    version["rate_defaults"] = rate_defaults
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="trip_case_version.items_replaced",
        entity_type="trip_case_version",
        entity_id=version["id"],
        before={"items": before},
        after={
            "items": version["items"],
            "removed": removed,
            "costs_replaced": len(rewrite),
            "rate_defaults": rate_defaults,
            "price_cleared": changed and price_before is not None,
        },
        entity_label=_label(case, number),
        summary=f"Saved {len(items)} item(s), removed {len(removed)}"
        + ("; price cleared" if changed and price_before is not None else ""),
    )
    return version


async def price_version(
    conn: asyncpg.Connection,
    staff: Staff,
    ref: str,
    number: int,
    *,
    channel: engine.Channel,
    currency: str,
    peak: bool,
    override_total: Decimal | None,
    override_reason: str | None,
    today: date | None = None,
) -> dict[str, Any]:
    """Price a draft from its stored net cost lines and save the total (UI spec §6.2 "Apply price")."""
    reason = (override_reason or "").strip() or None
    case = await resolve_case(conn, ref, lock=True)
    if is_terminal(case["status"]):
        raise Conflict("trip_case_closed", f"trip case is {case['status']}")
    version = dict(await _version(conn, case["id"], number, lock=True))
    _editable(version)
    items = await _items(conn, version["id"])
    if not items:
        raise Invalid("version_empty", "add at least one item before pricing")
    bare = [i["id"] for i in items if not i["costs"]]
    if bare:
        raise Invalid(
            "costs_missing",
            "every item needs at least one net cost line (enter 0 for an included item)",
            item_ids=bare,
        )
    lines = [engine.CostLine(Decimal(c["net_amount"]), c["net_currency"].strip()) for i in items for c in i["costs"]]
    try:
        priced = await pricing.price(
            conn,
            channel=channel,
            currency=currency,
            peak=peak,
            lines=lines,
            override_total=override_total,
            on=today or date.today(),
        )
    except pricing.NoRuleInForce as exc:
        raise Invalid("pricing_rule_missing", str(exc), channel=channel.value) from None
    except pricing.MissingFxRates as exc:
        raise Invalid("fx_rate_missing", str(exc), currencies=exc.currencies) from None
    except engine.PricingError as exc:
        raise Invalid("pricing_error", str(exc)) from None
    except pricing.PricingDataUnavailable:
        raise Unavailable("pricing_data_unavailable", "the pricing rules or FX rates could not be read") from None
    quote = priced.quote
    if quote.final_total > MAX_TOTAL:
        raise Invalid("price_too_large", f"a version total is at most {MAX_TOTAL}")
    if quote.below_floor and not reason:
        raise Invalid(
            "override_reason_required",
            "the total is below the channel floor; give a reason",
            effective_margin=str(quote.effective_margin),
            floor_margin=str(quote.floor_margin),
        )
    selling_fx = priced.selling_fx
    fx_rate = selling_fx.rate.rate_per_usd if selling_fx else Decimal(1)
    fx_as_of = selling_fx.as_of if selling_fx else priced.priced_on
    await conn.execute(
        "UPDATE booking.trip_case_versions SET total_price = $2, currency = $3, channel = $4, fx_rate_locked = $5,"
        " fx_as_of = $6, updated_by = $7 WHERE id = $1::uuid",
        version["id"],
        quote.final_total,
        quote.currency,
        quote.channel.value,
        fx_rate,
        fx_as_of,
        staff.cis_user_id,
    )
    label = _label(case, number)
    override = quote.override_total is not None
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="trip_case_version.priced",
        entity_type="trip_case_version",
        entity_id=version["id"],
        before={k: version[k] for k in ("total_price", "currency", "channel")},
        after={
            "total_price": quote.final_total,
            "currency": quote.currency,
            "channel": quote.channel.value,
            "peak": quote.peak,
            "computed_total": quote.total,
            "override_total": quote.override_total,
            "override_reason": reason if override else None,
            "net_total_usd": quote.net_total_usd,
            "effective_margin": quote.effective_margin,
            "floor_margin": quote.floor_margin,
            "below_floor": quote.below_floor,
            "rule_valid_from": priced.rule.valid_from,
            "fx_rate_locked": fx_rate,
            "fx_as_of": fx_as_of,
            "cost_lines": len(lines),
        },
        entity_label=label,
        summary=f"Priced V{number} at {quote.final_total} {quote.currency}"
        + (f" (override below floor: {reason})" if quote.below_floor else " (override)" if override else ""),
    )
    out = sql.version_dict(await _version(conn, case["id"], number))
    return {"quote": pricing.body(priced), "version": out}


async def _validity_days(conn: asyncpg.Connection) -> int:
    raw = await conn.fetchval("SELECT value::text FROM booking.settings WHERE key = 'quote_validity_days'")
    try:
        days = int(json.loads(raw)) if raw else DEFAULT_VALIDITY_DAYS
    except (TypeError, ValueError):
        days = DEFAULT_VALIDITY_DAYS
    return min(max(days, VALIDITY_MIN_DAYS), VALIDITY_MAX_DAYS)


async def send_version(
    conn: asyncpg.Connection,
    staff: Staff,
    ref: str,
    number: int,
    *,
    to: str | None,
    subject: str,
    message: str,
    valid_until: date | None,
    today: date | None = None,
) -> dict[str, Any]:
    case = await resolve_case(conn, ref, lock=True)
    ensure_staff_managed(case)
    if case["status"] not in SEND_CHAIN:
        raise Conflict(
            "case_not_sendable",
            f"a proposal can be sent from {', '.join(SEND_CHAIN)}; the case is {case['status']}",
        )
    version = dict(await _version(conn, case["id"], number, lock=True))
    _editable(version)
    item_count = await conn.fetchval(
        "SELECT count(*) FROM booking.trip_case_version_itinerary WHERE trip_case_version_id = $1::uuid",
        version["id"],
    )
    if not item_count:
        raise Invalid("version_empty", "add at least one item before sending")
    if version["total_price"] is None or version["currency"] is None:
        raise Invalid("price_missing", "price the version (total and currency) before sending")
    customer = await conn.fetchrow(
        "SELECT id::text AS id, code, name, email FROM booking.customers WHERE id = $1::uuid", case["customer_id"]
    )
    if to is not None and (customer["email"] or "").lower() != to.strip().lower():
        raise Invalid("recipient_mismatch", "a proposal is sent to the customer's own e-mail only (Q14)")
    today = today or date.today()
    valid_until = valid_until or today + timedelta(days=await _validity_days(conn))
    if not today + timedelta(days=VALIDITY_MIN_DAYS) <= valid_until <= today + timedelta(days=VALIDITY_MAX_DAYS):
        raise Invalid(
            "invalid_valid_until",
            f"valid_until must be {VALIDITY_MIN_DAYS} to {VALIDITY_MAX_DAYS} days from today",
        )
    badges = await _version_badges(conn, case["id"])
    await conn.execute(
        "UPDATE booking.trip_case_versions SET is_sent_to_customer = true, sent_at = now(), sent_by = $2,"
        " valid_until = $3, updated_by = $2 WHERE id = $1::uuid",
        version["id"],
        staff.cis_user_id,
        valid_until,
    )
    from_status = case["status"]
    hops = list(SEND_CHAIN[SEND_CHAIN.index(from_status) + 1 :])
    current = dict(case)
    for hop in hops:
        await set_status(conn, staff, current, hop, f"V{number} sent")
        current["status"] = hop
    label = _label(case, number)
    notification_id = await notifications.notify_customer(
        conn,
        customer_id=customer["id"],
        type="proposal.sent",
        title=subject,
        message=message,
        entity_type="trip_case_version",
        entity_id=version["id"],
        data={
            "trip_case_code": case["code"],
            "version_number": number,
            "to": customer["email"],
            "valid_until": valid_until,
            "total_price": version["total_price"],
            "currency": version["currency"],
            "email_sent": False,
        },
    )
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="trip_case_version.sent",
        entity_type="trip_case_version",
        entity_id=version["id"],
        before={"is_sent_to_customer": False, "case_status": from_status},
        after={
            "is_sent_to_customer": True,
            "valid_until": valid_until,
            "case_status": "proposal_sent",
            "to": customer["email"],
            "subject": subject,
            "message": message,
            "notification_id": notification_id,
            "total_price": version["total_price"],
            "currency": version["currency"],
        },
        entity_label=label,
        summary=f"Marked V{number} as sent (valid until {valid_until.isoformat()}); no e-mail sent",
    )
    await _audit_superseded(conn, staff, case, badges, number, "sent")
    out = sql.version_dict(await _version(conn, case["id"], number))
    out["trip_case"] = {"id": case["id"], "code": case["code"], "status": "proposal_sent"}
    out["notification_id"] = notification_id
    out["email_sent"] = False
    return out


async def approve_version(
    conn: asyncpg.Connection, staff: Staff, ref: str, number: int, note: str | None
) -> dict[str, Any]:
    """Staff record the customer's approval of a sent version: version approved + case -> confirmed."""
    note = (note or "").strip() or None
    case = await resolve_case(conn, ref, lock=True)
    ensure_staff_managed(case)
    version = dict(await _version(conn, case["id"], number, lock=True))
    if version["is_approved"]:
        raise Conflict("version_already_approved", f"V{number} is already approved")
    if not version["is_sent_to_customer"]:
        raise Conflict("version_not_sent", "only a version sent to the customer can be approved")
    if version["badge"] == "superseded":
        raise Conflict("version_superseded", "a later version was sent; approve that one")
    if case["status"] != "proposal_sent":
        raise Conflict("case_not_awaiting_approval", f"the case is {case['status']}, not proposal_sent")
    badges = await _version_badges(conn, case["id"])
    await conn.execute(
        "UPDATE booking.trip_case_versions SET is_approved = true, approved_at = now(), approved_by = $2,"
        " approval_note = $3, updated_by = $2 WHERE id = $1::uuid",
        version["id"],
        staff.cis_user_id,
        note,
    )
    await set_status(conn, staff, case, "confirmed", f"V{number} approved" + (f": {note}" if note else ""))
    label = _label(case, number)
    await notifications.notify_staff(
        conn,
        type="proposal.approved",
        title=f"{label} approved",
        message=f"{staff.name or staff.cis_user_id} recorded the customer's approval of {label}.",
        entity_type="trip_case",
        entity_id=case["id"],
        data={"code": case["code"], "version_number": number},
    )
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="trip_case_version.approved",
        entity_type="trip_case_version",
        entity_id=version["id"],
        before={"is_approved": False, "case_status": case["status"]},
        after={"is_approved": True, "approval_note": note, "case_status": "confirmed"},
        entity_label=label,
        summary=f"Recorded approval of V{number}; case confirmed",
    )
    await _audit_superseded(conn, staff, case, badges, number, "approved")
    out = sql.version_dict(await _version(conn, case["id"], number))
    out["trip_case"] = {"id": case["id"], "code": case["code"], "status": "confirmed"}
    return out


async def list_versions(
    conn: asyncpg.Connection,
    *,
    q: str | None = None,
    badge: str | None = None,
    assignee: str | None = None,
    updated_from: date | None = None,
    updated_to: date | None = None,
    sort: str | None = None,
    limit: int = 25,
    offset: int = 0,
) -> dict[str, Any]:
    """Cross-case proposal list (``/proposals``). ``assignee`` = the case's open sales owner (id or ``none``)."""
    from app.trip_cases.service import _day_start  # private helper shared with the case list

    where = ["v.deleted_at IS NULL", "tc.deleted_at IS NULL"]
    args: list[Any] = []

    def add(fragment: str, value: Any) -> None:
        args.append(value)
        where.append(fragment.replace("{p}", f"${len(args)}"))

    if badge:
        if badge not in sql.VERSION_BADGES:
            raise Invalid("unknown_badge", f"badge must be one of {', '.join(sql.VERSION_BADGES)}")
        add(f"({sql.VERSION_BADGE}) = {{p}}", badge)
    if q and q.strip():
        add("(tc.code ILIKE {p} OR c.name ILIKE {p} OR c.email ILIKE {p} OR v.title ILIKE {p})", paging.contains(q))
    sales = (
        "(SELECT a.staff_user_id FROM booking.staff_assignments a"
        " WHERE a.trip_case_id = tc.id AND a.role = 'sales' AND a.unassigned_at IS NULL)"
    )
    if assignee == "none":
        where.append(f"{sales} IS NULL")
    elif assignee:
        add(f"{sales} = {{p}}", assignee)
    if updated_from:
        add("v.updated_at >= {p}", _day_start(updated_from))
    if updated_to:
        add("v.updated_at < {p}", _day_start(updated_to + timedelta(days=1)))
    raw = (sort or "-updated_at").strip()
    column = VERSION_SORTS.get(raw.lstrip("-+"))
    if column is None:
        raise Invalid("unknown_sort", f"sort must be one of {', '.join(VERSION_SORTS)} (prefix - for descending)")
    order = f"{column} {'DESC' if raw.startswith('-') else 'ASC'}, v.id"
    clause = " AND ".join(where)
    base = (
        "FROM booking.trip_case_versions v JOIN booking.trip_cases tc ON tc.id = v.trip_case_id"
        " LEFT JOIN booking.customers c ON c.id = tc.customer_id"  # b2b cases have no customer (bk-020)
    )
    total = await conn.fetchval(f"SELECT count(*) {base} WHERE {clause}", *args)  # noqa: S608
    rows = await conn.fetch(
        f"""
        SELECT {sql.VERSION_COLUMNS}, tc.id::text AS trip_case_id, tc.code AS trip_case_code,
               tc.status AS trip_case_status, tc.start_date, c.id::text AS customer_id, c.name AS customer_name,
               us.name AS updated_by_name, {sales} AS sales_id, {sql.LEGS_JSON},
               tc.inquiry_snapshot::text AS inquiry_snapshot
        {base} LEFT JOIN booking.staff_users us ON us.cis_user_id = v.updated_by
        WHERE {clause}
        ORDER BY {order}
        LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}
        """,  # noqa: S608 — fixed fragments, values are bound
        *args,
        limit,
        offset,
    )
    items = []
    for row in rows:
        out = sql.version_dict(row)
        shape = sql.trip_shape(
            sql.loads(out.pop("inquiry_snapshot")), sql.loads(out.pop("legs"), []), row["start_date"]
        )
        out["customer"] = {"id": out.pop("customer_id"), "name": out.pop("customer_name")}
        out["updated_by"] = {"id": row["updated_by"], "name": out.pop("updated_by_name")}
        out.update(
            leg_titles=shape["leg_titles"],
            trip_title=shape["trip_title"],
            duration_days=shape["duration_days"],
            end_date=shape["trip_end_date"],
        )
        items.append(out)
    return {"items": items, "total": total, "limit": limit, "offset": offset}
