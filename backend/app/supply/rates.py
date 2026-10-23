"""Supplier net rates (bk-010, AA-823; ADR 0005 decisions 2 and 8) and the net cost a version item takes from them.

A rate prices one hotel room type (per room night) or one service (per the service unit) under a supplier contract
(bk-008): season label, validity inside the contract's, meal plan (rooms), pax band, net amount in the contract
currency.

- **Written under a draft contract only.** Once a second person publishes the contract its rates are fixed ("never
  overwrite", ADR 0005 d2); new prices = a new contract. The DB trigger ``supplier_rate_guard`` enforces the same.
- **A rate write makes the writer the contract's maker** (``updated_by``), so the maker-checker rule of bk-008 also
  covers the rates: whoever adds or changes a rate cannot publish that contract.
- **In force** on a day = contract published and both validities cover the day. When several rates match (seasons,
  meal plans, pax bands), the newest ``valid_from`` wins, then a rate with a pax band, then the lowest net.
- **Default net of a version item** (:func:`default_cost`): the rate in force on the item's first day (trip start +
  day_from - 1; today, UTC, when the case has no start date) for the case's pax, times a quantity — nights
  (``max(day_to - day_from, 1)``, one room) for a room, pax for a per-person service, days for a per-day service, one
  otherwise. Staff edit the line afterwards like any other; it keeps ``supplier_rate_id`` as provenance.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import asyncpg

from app import audit
from app.auth.staff import Staff
from app.errors import Conflict, Invalid, NotFound
from app.suppliers import service as suppliers
from app.suppliers.service import clean_text
from app.supply.records import RATE_IN_FORCE_ON, RATE_IN_FORCE_TODAY, as_uuid

MEAL_PLANS = ("RO", "BB", "HB", "FB", "AI")
EDITABLE = ("season", "valid_from", "valid_to", "meal_plan", "pax_min", "pax_max", "net_amount", "notes")
UNIT_LABELS = {"room_night": "night", "person": "pax", "group": "group", "vehicle": "vehicle", "day": "day"}
MAX_NOTE = 200

RATE_COLUMNS = f"""
    x.id::text AS id, x.supplier_contract_id::text AS contract_id, k.title AS contract_title,
    k.status AS contract_status, k.supplier_id::text AS supplier_id,
    x.hotel_room_type_id::text AS hotel_room_type_id, t.name AS room_type_name,
    h.id::text AS hotel_id, h.code AS hotel_code, h.name AS hotel_name,
    x.service_id::text AS service_id, s.code AS service_code, s.title AS service_title,
    x.season, x.valid_from, x.valid_to, x.unit, x.meal_plan, x.pax_min, x.pax_max,
    x.net_amount::text AS net_amount, x.currency::text AS currency, x.notes,
    ({RATE_IN_FORCE_TODAY}) AS in_force,
    x.created_by, x.updated_by, x.created_at, x.updated_at
"""
RATE_FROM = """
    booking.supplier_rates x
    JOIN booking.supplier_contracts k ON k.id = x.supplier_contract_id
    LEFT JOIN booking.hotel_room_types t ON t.id = x.hotel_room_type_id
    LEFT JOIN booking.hotels h ON h.id = t.hotel_id
    LEFT JOIN booking.services s ON s.id = x.service_id
"""
RATE_ORDER = "x.valid_from DESC, (x.pax_min IS NOT NULL OR x.pax_max IS NOT NULL) DESC, x.net_amount ASC, x.id"


def target_label(rate: dict[str, Any]) -> str:
    if rate.get("hotel_room_type_id"):
        return f"{rate['hotel_code']} {rate['hotel_name']} · {rate['room_type_name']}"
    return f"{rate['service_code']} {rate['service_title']}"


# ── Reads ────────────────────────────────────────────────────────────────────


async def list_rates(
    conn: asyncpg.Connection,
    *,
    contract_id: str | None = None,
    hotel_id: str | None = None,
    service_id: str | None = None,
) -> dict[str, Any]:
    """Rates of one contract, of every room type of one hotel, or of one service (newest validity first)."""
    if contract_id:
        where, arg = "x.supplier_contract_id = $1::uuid", contract_id
    elif hotel_id:
        where, arg = "t.hotel_id = $1::uuid", hotel_id
    else:
        where, arg = "x.service_id = $1::uuid", service_id
    rows = await conn.fetch(
        f"SELECT {RATE_COLUMNS} FROM {RATE_FROM} WHERE {where}"  # noqa: S608 — constants
        " ORDER BY k.valid_from DESC, t.name NULLS LAST, x.valid_from, x.meal_plan NULLS FIRST, x.pax_min NULLS FIRST",
        arg,
    )
    return {"items": [dict(r) for r in rows]}


async def get_rate(conn: asyncpg.Connection, rate_id: str) -> dict[str, Any]:
    rid = as_uuid(rate_id, "rate_not_found", "rate")
    row = await conn.fetchrow(f"SELECT {RATE_COLUMNS} FROM {RATE_FROM} WHERE x.id = $1::uuid", rid)  # noqa: S608
    if row is None:
        raise NotFound("rate_not_found", f"rate {rate_id} not found")
    return dict(row)


async def rate_in_force(
    conn: asyncpg.Connection,
    *,
    room_type_id: str | None = None,
    service_id: str | None = None,
    on: date,
    pax: int | None = None,
) -> dict[str, Any] | None:
    """The rate in force for a room type or service on ``on`` for ``pax`` travellers, or None."""
    row = await conn.fetchrow(
        f"SELECT {RATE_COLUMNS} FROM {RATE_FROM}"  # noqa: S608 — constants
        " WHERE (x.hotel_room_type_id = $1::uuid OR x.service_id = $2::uuid)"
        f" AND {RATE_IN_FORCE_ON.format(day='$3::date')}"
        " AND ($4::int IS NULL OR ((x.pax_min IS NULL OR x.pax_min <= $4) AND (x.pax_max IS NULL OR x.pax_max >= $4)))"
        f" ORDER BY {RATE_ORDER} LIMIT 1",
        room_type_id,
        service_id,
        on,
        pax,
    )
    return dict(row) if row else None


@dataclass(frozen=True)
class RateAsk:
    """One rate lookup: a room type or a service, a day, a pax count (None = any band)."""

    room_type_id: str | None
    service_id: str | None
    on: date
    pax: int | None


# The same choice as rate_in_force (in force on the day, pax band, RATE_ORDER), for many asks in one round trip
# (AA-972 review: a B2B quote must not run one query per night or component).
RATES_MANY_SQL = f"""
SELECT a.n, r.id, r.unit, r.net_amount, r.currency
FROM unnest($1::uuid[], $2::uuid[], $3::date[], $4::int[]) WITH ORDINALITY
     AS a(room_type_id, service_id, on_day, pax, n)
LEFT JOIN LATERAL (
    SELECT x.id::text AS id, x.unit, x.net_amount::text AS net_amount, x.currency::text AS currency
    FROM booking.supplier_rates x JOIN booking.supplier_contracts k ON k.id = x.supplier_contract_id
    WHERE (x.hotel_room_type_id = a.room_type_id OR x.service_id = a.service_id)
      AND {RATE_IN_FORCE_ON.format(day="a.on_day")}
      AND (a.pax IS NULL OR ((x.pax_min IS NULL OR x.pax_min <= a.pax) AND (x.pax_max IS NULL OR x.pax_max >= a.pax)))
    ORDER BY {RATE_ORDER} LIMIT 1
) r ON true
ORDER BY a.n
"""  # noqa: S608 — constants only


async def rates_in_force_many(conn: asyncpg.Connection, asks: list[RateAsk]) -> list[dict[str, Any] | None]:
    """The rate in force for every ask (same order; None where none), in one query. Duplicate asks are looked up once.

    Each found rate has ``id``, ``unit``, ``net_amount`` (text) and ``currency`` (stripped).
    """
    unique = list(dict.fromkeys(asks))
    if not unique:
        return []
    rows = await conn.fetch(
        RATES_MANY_SQL,
        [a.room_type_id for a in unique],
        [a.service_id for a in unique],
        [a.on for a in unique],
        [a.pax for a in unique],
    )
    found: dict[int, dict[str, Any]] = {}
    for row in rows:
        if row["id"] is not None:
            found[int(row["n"])] = {
                "id": row["id"],
                "unit": row["unit"],
                "net_amount": row["net_amount"],
                "currency": str(row["currency"]).strip(),
            }
    by_ask = {ask: found.get(i) for i, ask in enumerate(unique, start=1)}
    return [by_ask[a] for a in asks]


# ── Default net of a version item ────────────────────────────────────────────


@dataclass(frozen=True)
class DefaultCost:
    net_amount: Decimal
    net_currency: str
    supplier_note: str
    supplier_rate_id: str


def quantity(unit: str, *, day_from: int, day_to: int, pax: int | None) -> int:
    if unit == "room_night":
        return max(day_to - day_from, 1)
    if unit == "person":
        return max(pax or 1, 1)
    if unit == "day":
        return day_to - day_from + 1
    return 1


def default_cost(rate: dict[str, Any], *, day_from: int, day_to: int, pax: int | None) -> DefaultCost:
    qty = quantity(rate["unit"], day_from=day_from, day_to=day_to, pax=pax)
    unit_net = Decimal(str(rate["net_amount"]))
    amount = (unit_net * qty).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    extras = " ".join(p for p in (rate.get("meal_plan"), rate.get("season")) if p)
    unit = UNIT_LABELS.get(rate["unit"], rate["unit"])
    note = f"{target_label(rate)}{f' {extras}' if extras else ''}: {unit_net} {rate['currency']} x {qty} {unit}"
    return DefaultCost(amount, rate["currency"], note[:MAX_NOTE], rate["id"])


# ── Writes (draft contracts only) ────────────────────────────────────────────


async def _draft_contract(conn: asyncpg.Connection, contract_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    contract = await suppliers.get_contract(conn, contract_id, lock=True)
    if contract["status"] != "draft":
        raise Conflict(
            "contract_locked", f"rates of a {contract['status']} contract are fixed; create a new contract instead"
        )
    supplier = await suppliers.resolve_supplier(conn, contract["supplier_id"])
    return contract, supplier


def _normalize(data: dict[str, Any]) -> dict[str, Any]:
    out = dict(data)
    for key in ("season", "notes"):
        if key in out:
            out[key] = clean_text(out[key])
    if out.get("meal_plan") is not None:
        out["meal_plan"] = str(out["meal_plan"]).strip().upper() or None
    if "net_amount" in out and out["net_amount"] is None:
        raise Invalid("net_amount_required", "a rate needs a net amount")
    if "valid_from" in out and out["valid_from"] is None:
        raise Invalid("valid_from_required", "a rate needs a start date")
    return out


def _check(merged: dict[str, Any], contract: dict[str, Any]) -> None:
    if merged.get("meal_plan") is not None:
        if not merged.get("hotel_room_type_id"):
            raise Invalid("meal_plan_rooms_only", "a meal plan applies to room rates only")
        if merged["meal_plan"] not in MEAL_PLANS:
            raise Invalid("unknown_meal_plan", f"meal plan must be one of {', '.join(MEAL_PLANS)}")
    if merged.get("valid_to") is not None and merged["valid_to"] < merged["valid_from"]:
        raise Invalid("invalid_validity", "valid_to must be on or after valid_from")
    k_from, k_to = contract["valid_from"], contract["valid_to"]
    if merged["valid_from"] < k_from or (
        k_to is not None and (merged["valid_to"] is None or merged["valid_to"] > k_to)
    ):
        raise Invalid(
            "rate_outside_contract",
            f"a rate stays inside its contract validity ({k_from} to {k_to or 'open-ended'})",
        )
    lo, hi = merged.get("pax_min"), merged.get("pax_max")
    if lo is not None and hi is not None and hi < lo:
        raise Invalid("invalid_pax_band", "pax_max must be at least pax_min")


async def _touch_contract(conn: asyncpg.Connection, staff: Staff, contract_id: str) -> None:
    """The rate writer becomes the draft's maker (bk-008 maker-checker covers the rates too)."""
    await conn.execute(
        "UPDATE booking.supplier_contracts SET updated_by = $2 WHERE id = $1::uuid", contract_id, staff.cis_user_id
    )


async def _audit(
    conn: asyncpg.Connection,
    staff: Staff,
    action: str,
    rate: dict[str, Any],
    supplier: dict[str, Any],
    contract: dict[str, Any],
    *,
    before: Any = None,
    after: Any = None,
    summary: str,
) -> None:
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action=f"supplier_rate.{action}",
        entity_type="supplier_rate",
        entity_id=rate["id"],
        before=before,
        after=after,
        entity_label=f"{supplier['code']} · {contract['title']} · {target_label(rate)}",
        summary=summary,
    )


async def create_rate(conn: asyncpg.Connection, staff: Staff, contract_id: str, data: dict[str, Any]) -> dict[str, Any]:
    contract, supplier = await _draft_contract(conn, contract_id)
    room_type_id, service_id = data.get("hotel_room_type_id"), data.get("service_id")
    if (room_type_id is None) == (service_id is None):
        raise Invalid("rate_target", "a rate prices exactly one room type or one service")
    if room_type_id:
        target = await conn.fetchrow(
            "SELECT h.supplier_id::text AS supplier_id, 'room_night' AS unit FROM booking.hotel_room_types t"
            " JOIN booking.hotels h ON h.id = t.hotel_id WHERE t.id = $1::uuid",
            str(room_type_id),
        )
    else:
        target = await conn.fetchrow(
            "SELECT s.supplier_id::text AS supplier_id, s.unit FROM booking.services s WHERE s.id = $1::uuid",
            str(service_id),
        )
    if target is None:
        raise Invalid("unknown_rate_target", "the room type or service does not exist")
    if target["supplier_id"] != contract["supplier_id"]:
        raise Invalid("rate_supplier_mismatch", "a rate prices a room type or service of the contract's own supplier")
    # An omitted (or null) start takes the contract's; an omitted end too (null = open-ended, if the contract is).
    values = _normalize({k: v for k, v in data.items() if k in EDITABLE and not (k == "valid_from" and v is None)})
    if values.get("net_amount") is None:
        raise Invalid("net_amount_required", "a rate needs a net amount")
    values.setdefault("valid_from", contract["valid_from"])
    if "valid_to" not in values:
        values["valid_to"] = contract["valid_to"]
    _check({**values, "hotel_room_type_id": room_type_id}, contract)
    columns = list(values)
    marks = ", ".join(f"${i + 7}" for i in range(len(columns)))
    try:
        rid = await conn.fetchval(
            "INSERT INTO booking.supplier_rates (supplier_contract_id, hotel_room_type_id, service_id, unit, currency,"  # noqa: S608
            f" created_by, updated_by, {', '.join(columns)})"
            f" VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5, $6, $6, {marks}) RETURNING id::text",
            contract["id"],
            str(room_type_id) if room_type_id else None,
            str(service_id) if service_id else None,
            target["unit"],
            contract["currency"],
            staff.cis_user_id,
            *values.values(),
        )
    except asyncpg.CheckViolationError as exc:  # the DB backstop (guard trigger and CHECKs)
        raise Invalid("invalid_rate", str(exc)) from exc
    await _touch_contract(conn, staff, contract["id"])
    rate = await get_rate(conn, rid)
    await _audit(
        conn,
        staff,
        "created",
        rate,
        supplier,
        contract,
        after={k: rate[k] for k in ("hotel_room_type_id", "service_id", "unit", "currency", *EDITABLE)},
        summary=f"Added rate {rate['net_amount']} {rate['currency']} per {UNIT_LABELS[rate['unit']]}",
    )
    return rate


async def update_rate(conn: asyncpg.Connection, staff: Staff, rate_id: str, changes: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(changes) - set(EDITABLE))
    if unknown:
        raise Invalid("not_editable", f"not editable: {', '.join(unknown)}")
    current = await get_rate(conn, rate_id)
    contract, supplier = await _draft_contract(conn, current["contract_id"])
    values = _normalize(changes)
    if "net_amount" in values:
        values["net_amount"] = Decimal(str(values["net_amount"]))
        stored = Decimal(current["net_amount"])
        if values["net_amount"] == stored:
            values.pop("net_amount")
    changed = {k: v for k, v in values.items() if k == "net_amount" or current[k] != v}
    if not changed:
        return {**current, "changed": []}
    _check({**current, **changed}, contract)
    sets = ", ".join(f"{column} = ${i + 2}" for i, column in enumerate(changed))
    try:
        await conn.execute(
            f"UPDATE booking.supplier_rates SET {sets}, updated_by = ${len(changed) + 2} WHERE id = $1::uuid",  # noqa: S608
            current["id"],
            *changed.values(),
            staff.cis_user_id,
        )
    except asyncpg.CheckViolationError as exc:
        raise Invalid("invalid_rate", str(exc)) from exc
    await _touch_contract(conn, staff, contract["id"])
    await _audit(
        conn,
        staff,
        "updated",
        current,
        supplier,
        contract,
        before={k: current[k] for k in changed},
        after=changed,
        summary=f"Updated rate {', '.join(sorted(changed))}",
    )
    return {**(await get_rate(conn, current["id"])), "changed": sorted(changed)}


async def delete_rate(conn: asyncpg.Connection, staff: Staff, rate_id: str) -> None:
    current = await get_rate(conn, rate_id)
    contract, supplier = await _draft_contract(conn, current["contract_id"])
    try:
        await conn.execute("DELETE FROM booking.supplier_rates WHERE id = $1::uuid", current["id"])
    except asyncpg.ForeignKeyViolationError as exc:  # a cost line names it (only possible for a published rate)
        raise Conflict("rate_in_use", "a proposal cost line uses this rate") from exc
    await _touch_contract(conn, staff, contract["id"])
    await _audit(
        conn,
        staff,
        "deleted",
        current,
        supplier,
        contract,
        before={k: current[k] for k in ("hotel_room_type_id", "service_id", "unit", "currency", *EDITABLE)},
        summary=f"Removed rate {current['net_amount']} {current['currency']}",
    )
