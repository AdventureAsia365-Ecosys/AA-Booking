"""Suppliers and supplier contracts (bk-008, AA-822; UI spec §6.3, module spec 08; ADR 0005 decisions 1, 2, 8).

Suppliers
    The master record of a company Booking buys from. All commercial. Addressed by UUID or ``SUP-000123``.
    Never deleted: ``deactivate`` / ``activate`` only, and deactivate is refused (409 ``supplier_has_contracts``)
    while a published contract is in force or upcoming. ``destination_id`` is a CIS place checked against
    ``catalog.v_destination`` (``app.catalog``); when it maps to an ISO country the supplier's ``country_code``
    must agree (it is filled in when empty).

Contracts
    Terms of one supplier for a period: validity, the supplier currency, payment terms, cancellation policy.
    ``draft`` (editable) → ``published`` (fixed; by a second person: maker-checker, ADR 0005 d8 — not the last writer
    of the terms, see ``maker``) → ``expired``.
    New terms are a new contract (ADR 0005 d2 "never overwrite"). The DB enforces the same rules (bk-008 CHECK and
    lock trigger); the API answers first with 403 / 409.

``phase`` (computed, UTC day): draft | upcoming | in_force | ended | expired. Every write adds one audit row.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

import asyncpg

from app import audit, catalog, paging
from app.auth.staff import Staff
from app.errors import Conflict, Forbidden, Invalid, NotFound

CATEGORIES = ("hotel", "transport", "guide", "entrance", "activity", "meal")
STATUSES = ("active", "inactive")
SORTS = {"company_name": "lower(s.company_name)", "updated_at": "s.updated_at", "created_at": "s.created_at"}
TEXT_FIELDS = (
    "website",
    "internal_notes",
    "contact_name",
    "contact_title",
    "contact_email",
    "contact_phone",
    "address",
    "region",
    "postal_code",
)
EDITABLE = ("company_name", "categories", *TEXT_FIELDS, "destination_id", "country_code")

CONTRACT_EDITABLE = (
    "title",
    "reference",
    "valid_from",
    "valid_to",
    "currency",
    "payment_terms",
    "cancellation_policy",
    "notes",
)
CONTRACT_REQUIRED = ("title", "valid_from", "currency")
CONTRACT_STATUS_TARGETS = ("published", "expired")

TODAY = "(now() AT TIME ZONE 'UTC')::date"
# Published and not over yet (in force today, or starting later): blocks a deactivation.
LIVE_CONTRACT = f"k.status = 'published' AND (k.valid_to IS NULL OR k.valid_to >= {TODAY})"
IN_FORCE = f"k.status = 'published' AND k.valid_from <= {TODAY} AND (k.valid_to IS NULL OR k.valid_to >= {TODAY})"

SUPPLIER_COLUMNS = f"""
    s.id::text AS id, s.code, s.company_name, s.categories, s.website, s.internal_notes,
    s.contact_name, s.contact_title, s.contact_email, s.contact_phone,
    s.address, s.region, s.postal_code, s.destination_id::text AS destination_id,
    s.country_code::text AS country_code, co.name AS country_name, s.status,
    s.created_by, s.updated_by, s.created_at, s.updated_at,
    (SELECT count(*) FROM booking.supplier_contracts k WHERE k.supplier_id = s.id AND {IN_FORCE})
        AS contracts_in_force
"""  # noqa: S608 — constants only
SUPPLIER_FROM = "booking.suppliers s LEFT JOIN booking.countries co ON co.iso2 = s.country_code"

CONTRACT_COLUMNS = f"""
    k.id::text AS id, k.supplier_id::text AS supplier_id, k.title, k.reference, k.valid_from, k.valid_to,
    k.currency::text AS currency, k.payment_terms, k.cancellation_policy, k.notes, k.status,
    CASE
        WHEN k.status = 'draft' THEN 'draft'
        WHEN k.status = 'expired' THEN 'expired'
        WHEN k.valid_from > {TODAY} THEN 'upcoming'
        WHEN k.valid_to IS NOT NULL AND k.valid_to < {TODAY} THEN 'ended'
        ELSE 'in_force'
    END AS phase,
    k.created_by, cb.name AS created_by_name, k.updated_by, ub.name AS updated_by_name,
    k.published_by, pb.name AS published_by_name, k.published_at,
    k.expired_by, eb.name AS expired_by_name, k.expired_at, k.created_at, k.updated_at
"""
CONTRACT_FROM = """
    booking.supplier_contracts k
    LEFT JOIN booking.staff_users cb ON cb.cis_user_id = k.created_by
    LEFT JOIN booking.staff_users ub ON ub.cis_user_id = k.updated_by
    LEFT JOIN booking.staff_users pb ON pb.cis_user_id = k.published_by
    LEFT JOIN booking.staff_users eb ON eb.cis_user_id = k.expired_by
"""


# ── Helpers ──────────────────────────────────────────────────────────────────


def _supplier_filter(ref: str) -> tuple[str, str]:
    """A supplier is addressed by its UUID or its code (``SUP-000123``)."""
    try:
        return "s.id = $1::uuid", str(uuid.UUID(ref))
    except ValueError:
        return "s.code = $1", ref.strip().upper()


def _uuid(value: str, code: str, what: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise NotFound(code, f"{what} {value} not found") from exc


def _row(row: Any) -> dict[str, Any]:
    out = dict(row)
    if "categories" in out and out["categories"] is not None:
        out["categories"] = list(out["categories"])
    return out


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def normalize(data: dict[str, Any]) -> dict[str, Any]:
    """Trim texts (empty → NULL), lower-case the e-mail, order and de-duplicate categories, upper-case the country."""
    out = dict(data)
    if "company_name" in out:
        out["company_name"] = (out["company_name"] or "").strip()
        if not out["company_name"]:
            raise Invalid("company_name_required", "a supplier needs a company name")
    if "categories" in out:
        given = set(out["categories"] or [])
        unknown = sorted(given - set(CATEGORIES))
        if unknown:
            raise Invalid("unknown_category", f"categories must be among {', '.join(CATEGORIES)}")
        if not given:
            raise Invalid("category_required", "a supplier needs at least one category")
        out["categories"] = [c for c in CATEGORIES if c in given]
    for key in TEXT_FIELDS:
        if key in out:
            out[key] = clean_text(out[key])
    if out.get("contact_email"):
        out["contact_email"] = out["contact_email"].lower()
    if "country_code" in out:
        out["country_code"] = (clean_text(out["country_code"]) or "").upper() or None
    if "destination_id" in out and out["destination_id"] is not None:
        out["destination_id"] = str(out["destination_id"])
    return out


async def check_place(conn: asyncpg.Connection, merged: dict[str, Any], changed: set[str]) -> dict[str, Any]:
    """Validate the country and destination of the record as it will be saved; fill an empty country from the
    destination. Returns the extra columns to write (``country_code`` when derived)."""
    extra: dict[str, Any] = {}
    country = merged.get("country_code")
    if "country_code" in changed and country is not None:
        known = await conn.fetchval("SELECT 1 FROM booking.countries WHERE iso2 = $1 AND active", country)
        if not known:
            raise Invalid("unknown_country", f"country {country} is not in the Booking country list")
    destination_id = merged.get("destination_id")
    if destination_id and ({"destination_id", "country_code"} & changed):
        place = await catalog.require_destination(conn, destination_id)
        if place["country_code"]:
            if country is None:
                extra["country_code"] = place["country_code"]
            elif country != place["country_code"]:
                raise Invalid(
                    "destination_country_mismatch",
                    f"{place['name']} is in {place['country']}, not {country}",
                )
    return extra


async def place_names(conn: asyncpg.Connection, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    places = await catalog.destinations_by_id(conn, [r["destination_id"] for r in rows if r.get("destination_id")])
    for r in rows:
        place = places.get(r.get("destination_id") or "")
        r["destination"] = (
            {"id": place["destination_id"], "name": place["name"], "country": place["country"]} if place else None
        )
    return rows


def _label(supplier: dict[str, Any]) -> str:
    return f"{supplier['code']} {supplier['company_name']}"


# ── Suppliers ────────────────────────────────────────────────────────────────


async def list_suppliers(
    conn: asyncpg.Connection,
    *,
    q: str | None = None,
    status: str | None = None,
    category: str | None = None,
    country: str | None = None,
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
        add("s.status = {p}", status)
    if category:
        if category not in CATEGORIES:
            raise Invalid("unknown_category", f"category must be one of {', '.join(CATEGORIES)}")
        add("s.categories @> ARRAY[{p}]::text[]", category)
    if country:
        add("s.country_code = {p}", country.strip().upper())
    if q and q.strip():
        add(
            "(s.company_name ILIKE {p} OR s.code ILIKE {p} OR s.contact_name ILIKE {p} OR s.contact_email ILIKE {p})",
            paging.contains(q),
        )
    raw = (sort or "company_name").strip()
    column = SORTS.get(raw.lstrip("-+"))
    if column is None:
        raise Invalid("unknown_sort", f"sort must be one of {', '.join(SORTS)} (prefix - for descending)")
    order = f"{column} {'DESC' if raw.startswith('-') else 'ASC'} NULLS LAST, s.id"
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    total = await conn.fetchval(f"SELECT count(*) FROM booking.suppliers s {clause}", *args)  # noqa: S608
    rows = await conn.fetch(
        f"SELECT {SUPPLIER_COLUMNS} FROM {SUPPLIER_FROM} {clause}"  # noqa: S608 — fixed fragments, values bound
        f" ORDER BY {order} LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}",
        *args,
        limit,
        offset,
    )
    items = await place_names(conn, [_row(r) for r in rows])
    return {"items": items, "total": total, "limit": limit, "offset": offset}


async def summary(conn: asyncpg.Connection) -> dict[str, Any]:
    """KPI strip (module spec 08 conflict 8): total, active, inactive, countries, contracts ending within 30 days."""
    row = await conn.fetchrow(
        f"""
        SELECT count(*) AS total,
               count(*) FILTER (WHERE s.status = 'active') AS active,
               count(*) FILTER (WHERE s.status = 'inactive') AS inactive,
               count(DISTINCT s.country_code) AS countries,
               (SELECT count(*) FROM booking.supplier_contracts k
                WHERE {IN_FORCE} AND k.valid_to IS NOT NULL AND k.valid_to < {TODAY} + 30) AS contracts_ending_30d
        FROM booking.suppliers s
        """  # noqa: S608 — constants only
    )
    return dict(row) if row else {"total": 0, "active": 0, "inactive": 0, "countries": 0, "contracts_ending_30d": 0}


async def resolve_supplier(conn: asyncpg.Connection, ref: str, *, lock: bool = False) -> dict[str, Any]:
    where, arg = _supplier_filter(ref)
    row = await conn.fetchrow(
        f"SELECT {SUPPLIER_COLUMNS} FROM {SUPPLIER_FROM} WHERE {where}"  # noqa: S608 — placeholder-only filter
        + (" FOR UPDATE OF s" if lock else ""),
        arg,
    )
    if row is None:
        raise NotFound("supplier_not_found", f"supplier {ref} not found")
    return _row(row)


async def get_supplier(conn: asyncpg.Connection, ref: str) -> dict[str, Any]:
    supplier = (await place_names(conn, [await resolve_supplier(conn, ref)]))[0]
    counts = await conn.fetchrow(
        f"""
        SELECT count(*) AS total,
               count(*) FILTER (WHERE k.status = 'draft') AS drafts,
               count(*) FILTER (WHERE {IN_FORCE}) AS in_force
        FROM booking.supplier_contracts k WHERE k.supplier_id = $1::uuid
        """,  # noqa: S608 — constants only
        supplier["id"],
    )
    supplier["contract_counts"] = dict(counts) if counts else {"total": 0, "drafts": 0, "in_force": 0}
    activity = await conn.fetch(
        """
        SELECT l.id::text AS id, l.created_at AS at, l.actor_ref, st.name AS actor_name, l.action, l.entity_type,
               l.entity_label, l.summary
        FROM booking.audit_logs l LEFT JOIN booking.staff_users st ON st.cis_user_id = l.actor_ref
        WHERE (l.entity_type = 'supplier' AND l.entity_id = $1::uuid)
           OR (l.entity_type = 'supplier_contract' AND l.entity_id IN (
               SELECT k.id FROM booking.supplier_contracts k WHERE k.supplier_id = $1::uuid))
        ORDER BY l.created_at DESC, l.id DESC
        LIMIT 20
        """,
        supplier["id"],
    )
    supplier["recent_activity"] = [
        {
            **{k: r[k] for k in ("id", "at", "action", "entity_type", "entity_label", "summary")},
            "actor": {"id": r["actor_ref"], "name": r["actor_name"]},
        }
        for r in activity
    ]
    return supplier


async def create_supplier(conn: asyncpg.Connection, staff: Staff, data: dict[str, Any]) -> dict[str, Any]:
    values = normalize({k: v for k, v in data.items() if k in EDITABLE})
    if "company_name" not in values:
        raise Invalid("company_name_required", "a supplier needs a company name")
    if "categories" not in values:
        raise Invalid("category_required", "a supplier needs at least one category")
    values.update(await check_place(conn, values, set(values)))
    columns = [k for k in EDITABLE if k in values]
    marks = ", ".join(f"${i + 1}" + ("::uuid" if c == "destination_id" else "") for i, c in enumerate(columns))
    try:
        row = await conn.fetchrow(
            f"INSERT INTO booking.suppliers ({', '.join(columns)}, created_by, updated_by)"  # noqa: S608 — whitelisted
            f" VALUES ({marks}, ${len(columns) + 1}, ${len(columns) + 1}) RETURNING id::text AS id, code",
            *[values[c] for c in columns],
            staff.cis_user_id,
        )
    except asyncpg.UniqueViolationError as exc:
        raise Conflict("supplier_exists", "a supplier with this name already exists in this country") from exc
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="supplier.created",
        entity_type="supplier",
        entity_id=row["id"],
        after={"code": row["code"], **{c: values[c] for c in columns}},
        entity_label=f"{row['code']} {values['company_name']}",
        summary=f"Created supplier {values['company_name']}",
    )
    return {"id": row["id"], "code": row["code"]}


async def update_supplier(conn: asyncpg.Connection, staff: Staff, ref: str, changes: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(changes) - set(EDITABLE))
    if unknown:
        raise Invalid("not_editable", f"not editable: {', '.join(unknown)}")
    if changes.get("company_name", "") is None:
        raise Invalid("company_name_required", "a supplier needs a company name")
    if "categories" in changes and changes["categories"] is None:
        raise Invalid("category_required", "a supplier needs at least one category")
    current = await resolve_supplier(conn, ref, lock=True)
    values = normalize(changes)
    changed = {k: v for k, v in values.items() if current[k] != v}
    if not changed:
        return {**current, "changed": []}
    merged = {**current, **changed}
    for key, value in (await check_place(conn, merged, set(changed))).items():
        # A derived value wins over the requested one (a cleared country is re-filled from the place); when it
        # equals the stored value the column simply stays as it is.
        if current[key] != value:
            changed[key] = value
        else:
            changed.pop(key, None)
    if not changed:
        return {**current, "changed": []}
    sets = ", ".join(
        f"{column} = ${i + 2}" + ("::uuid" if column == "destination_id" else "") for i, column in enumerate(changed)
    )
    try:
        await conn.execute(
            f"UPDATE booking.suppliers SET {sets}, updated_by = ${len(changed) + 2} WHERE id = $1::uuid",  # noqa: S608
            current["id"],
            *changed.values(),
            staff.cis_user_id,
        )
    except asyncpg.UniqueViolationError as exc:
        raise Conflict("supplier_exists", "a supplier with this name already exists in this country") from exc
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="supplier.updated",
        entity_type="supplier",
        entity_id=current["id"],
        before={k: current[k] for k in changed},
        after=changed,
        entity_label=_label(merged),
        summary=f"Updated {', '.join(sorted(changed))}",
    )
    updated = await resolve_supplier(conn, current["id"])
    return {**updated, "changed": sorted(changed)}


async def set_status(
    conn: asyncpg.Connection, staff: Staff, ref: str, target: str, reason: str | None = None
) -> dict[str, Any]:
    """``active`` / ``inactive``. Deactivating is refused while a published contract is in force or upcoming."""
    if target not in STATUSES:
        raise Invalid("unknown_status", f"status must be one of {', '.join(STATUSES)}")
    current = await resolve_supplier(conn, ref, lock=True)
    if current["status"] == target:
        return {**current, "changed": []}
    if target == "inactive":
        live = await conn.fetch(
            f"SELECT k.id::text AS id, k.title FROM booking.supplier_contracts k"  # noqa: S608 — constants
            f" WHERE k.supplier_id = $1::uuid AND {LIVE_CONTRACT} ORDER BY k.valid_from",
            current["id"],
        )
        if live:
            raise Conflict(
                "supplier_has_contracts",
                "a supplier with a published contract in force or upcoming cannot be deactivated; expire it first",
                contracts=[{"id": r["id"], "title": r["title"]} for r in live],
            )
    await conn.execute(
        "UPDATE booking.suppliers SET status = $2, updated_by = $3 WHERE id = $1::uuid",
        current["id"],
        target,
        staff.cis_user_id,
    )
    note = (reason or "").strip() or None
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="supplier.deactivated" if target == "inactive" else "supplier.activated",
        entity_type="supplier",
        entity_id=current["id"],
        before={"status": current["status"]},
        after={"status": target, **({"reason": note} if note else {})},
        entity_label=_label(current),
        summary=("Deactivated" if target == "inactive" else "Activated") + (f": {note}" if note else ""),
    )
    updated = await resolve_supplier(conn, current["id"])
    return {**updated, "changed": ["status"]}


# ── Contracts ────────────────────────────────────────────────────────────────


async def list_contracts(conn: asyncpg.Connection, ref: str) -> dict[str, Any]:
    supplier = await resolve_supplier(conn, ref)
    rows = await conn.fetch(
        f"SELECT {CONTRACT_COLUMNS} FROM {CONTRACT_FROM}"  # noqa: S608 — constants
        " WHERE k.supplier_id = $1::uuid ORDER BY k.valid_from DESC, k.created_at DESC",
        supplier["id"],
    )
    return {"items": [dict(r) for r in rows]}


async def get_contract(conn: asyncpg.Connection, contract_id: str, *, lock: bool = False) -> dict[str, Any]:
    cid = _uuid(contract_id, "contract_not_found", "contract")
    row = await conn.fetchrow(
        f"SELECT {CONTRACT_COLUMNS} FROM {CONTRACT_FROM} WHERE k.id = $1::uuid"  # noqa: S608 — constants
        + (" FOR UPDATE OF k" if lock else ""),
        cid,
    )
    if row is None:
        raise NotFound("contract_not_found", f"contract {contract_id} not found")
    return dict(row)


def _normalize_contract(data: dict[str, Any]) -> dict[str, Any]:
    out = dict(data)
    for key in ("title", "reference", "payment_terms", "cancellation_policy", "notes"):
        if key in out:
            out[key] = clean_text(out[key])
    if "currency" in out and out["currency"] is not None:
        out["currency"] = str(out["currency"]).strip().upper()
    for key in CONTRACT_REQUIRED:
        if key in out and out[key] is None:
            raise Invalid(f"{key}_required", f"a contract needs a {key.replace('_', ' ')}")
    return out


def _check_validity(valid_from: date, valid_to: date | None) -> None:
    if valid_to is not None and valid_to < valid_from:
        raise Invalid("invalid_validity", "valid_to must be on or after valid_from")


def maker(contract: dict[str, Any]) -> str:
    """The maker of a draft: its last editor, or its creator while nobody edited it (bk-008 maker-checker CHECK)."""
    return contract["updated_by"] or contract["created_by"]


def _contract_label(supplier: dict[str, Any], title: str) -> str:
    return f"{supplier['code']} · {title}"


async def create_contract(conn: asyncpg.Connection, staff: Staff, ref: str, data: dict[str, Any]) -> dict[str, Any]:
    supplier = await resolve_supplier(conn, ref, lock=True)
    if supplier["status"] != "active":
        raise Conflict("supplier_inactive", "activate the supplier before adding a contract")
    values = _normalize_contract({k: v for k, v in data.items() if k in CONTRACT_EDITABLE})
    missing = [k for k in CONTRACT_REQUIRED if values.get(k) is None]
    if missing:
        raise Invalid(f"{missing[0]}_required", f"a contract needs: {', '.join(missing)}")
    _check_validity(values["valid_from"], values.get("valid_to"))
    columns = [k for k in CONTRACT_EDITABLE if k in values]
    marks = ", ".join(f"${i + 2}" for i in range(len(columns)))
    cid = await conn.fetchval(
        f"INSERT INTO booking.supplier_contracts (supplier_id, {', '.join(columns)}, created_by, updated_by)"  # noqa: S608
        f" VALUES ($1::uuid, {marks}, ${len(columns) + 2}, ${len(columns) + 2}) RETURNING id::text",
        supplier["id"],
        *[values[c] for c in columns],
        staff.cis_user_id,
    )
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="supplier_contract.created",
        entity_type="supplier_contract",
        entity_id=cid,
        after={"supplier_id": supplier["id"], "status": "draft", **{c: values[c] for c in columns}},
        entity_label=_contract_label(supplier, values["title"]),
        summary=f"Created draft contract {values['title']} ({values['currency']})",
    )
    return await get_contract(conn, cid)


async def update_contract(
    conn: asyncpg.Connection, staff: Staff, contract_id: str, changes: dict[str, Any]
) -> dict[str, Any]:
    """Edit a draft's terms, or move its status (``published`` / ``expired``); not both in one call."""
    status = changes.pop("status", None)
    unknown = sorted(set(changes) - set(CONTRACT_EDITABLE))
    if unknown:
        raise Invalid("not_editable", f"not editable: {', '.join(unknown)}")
    if status is not None and changes:
        raise Invalid("status_with_changes", "change the status on its own, not together with the terms")
    current = await get_contract(conn, contract_id, lock=True)
    # Lock the supplier too: a deactivation running at the same time (which checks for live contracts under the
    # same lock) then either sees this publish or makes it answer 409 supplier_inactive.
    supplier = await resolve_supplier(conn, current["supplier_id"], lock=True)
    if status is not None:
        return await _move(conn, staff, current, supplier, status)
    if current["status"] != "draft":
        raise Conflict("contract_locked", f"a {current['status']} contract is fixed; create a new contract instead")
    values = _normalize_contract(changes)
    changed = {k: v for k, v in values.items() if current[k] != v}
    if not changed:
        return {**current, "changed": []}
    merged = {**current, **changed}
    _check_validity(merged["valid_from"], merged["valid_to"])
    sets = ", ".join(f"{column} = ${i + 2}" for i, column in enumerate(changed))
    await conn.execute(
        f"UPDATE booking.supplier_contracts SET {sets}, updated_by = ${len(changed) + 2}"  # noqa: S608 — whitelisted
        " WHERE id = $1::uuid",
        current["id"],
        *changed.values(),
        staff.cis_user_id,
    )
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="supplier_contract.updated",
        entity_type="supplier_contract",
        entity_id=current["id"],
        before={k: current[k] for k in changed},
        after=changed,
        entity_label=_contract_label(supplier, merged["title"]),
        summary=f"Updated {', '.join(sorted(changed))}",
    )
    return {**(await get_contract(conn, current["id"])), "changed": sorted(changed)}


async def _move(
    conn: asyncpg.Connection, staff: Staff, current: dict[str, Any], supplier: dict[str, Any], status: str
) -> dict[str, Any]:
    if status not in CONTRACT_STATUS_TARGETS:
        raise Invalid("unknown_status", f"status must be one of {', '.join(CONTRACT_STATUS_TARGETS)}")
    if current["status"] == status:
        return {**current, "changed": []}
    if status == "published":
        if current["status"] != "draft":
            raise Conflict("contract_not_draft", f"only a draft can be published (this one is {current['status']})")
        if supplier["status"] != "active":
            raise Conflict("supplier_inactive", "activate the supplier before publishing its contract")
        if staff.cis_user_id == maker(current):
            raise Forbidden(
                "maker_checker",
                "a contract is published by someone other than the person who last wrote its terms",
            )
        sql = "UPDATE booking.supplier_contracts SET status = 'published', published_by = $2, published_at = now()"
    else:
        if current["status"] != "published":
            raise Conflict(
                "contract_not_published", f"only a published contract can expire (this one is {current['status']})"
            )
        sql = "UPDATE booking.supplier_contracts SET status = 'expired', expired_by = $2, expired_at = now()"
    try:
        await conn.execute(f"{sql} WHERE id = $1::uuid", current["id"], staff.cis_user_id)
    except asyncpg.CheckViolationError as exc:  # the DB backstop of the maker-checker rule
        raise Forbidden("maker_checker", "a contract is published by a second person") from exc
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action=f"supplier_contract.{status}",
        entity_type="supplier_contract",
        entity_id=current["id"],
        before={"status": current["status"]},
        after={"status": status},
        entity_label=_contract_label(supplier, current["title"]),
        summary=("Published" if status == "published" else "Expired") + f" contract {current['title']}",
    )
    return {**(await get_contract(conn, current["id"])), "changed": ["status"]}
