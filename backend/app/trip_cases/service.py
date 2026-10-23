"""Trip case reads and staff writes: list, summary, detail, activity, create, edit, status, assignment.

Plain SQL on asyncpg. Every write runs in the caller's transaction, locks the trip case row first
(``FOR UPDATE``) and writes one ``booking.audit_logs`` row. Status rules live in the database
(bk-003/bk-005 ``trip_case_status_transitions`` + trigger); this module reads the same table so it can
answer with the allowed next statuses instead of a bare trigger error.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import asyncpg

from app import audit, paging
from app.auth.staff import Staff
from app.errors import Conflict, Invalid, NotFound
from app.notifications import service as notifications
from app.trip_cases import sql

STATUSES = (
    "new_inquiry",
    "discovery",
    "building",
    "proposal_sent",
    "confirmed",
    "supplier_confirming",
    "invoice_sent",
    "payment_pending",
    "paid",
    "pre_departure",
    "on_trip",
    "completed",
    "cancelled",
)
TERMINAL_STATUSES = frozenset({"cancelled", "completed"})
# confirmed is reached only by approving a sent version (versions.approve_version), never by a bare move.
APPROVAL_ONLY_STATUSES = frozenset({"confirmed"})
ASSIGNMENT_ROLES = ("sales", "contracting", "ops", "guide")
EDITABLE_FIELDS = ("pax", "pax_adults", "pax_children", "start_date", "currency", "trip_style", "notes")
# Staff may still annotate a B2B case (AA-973): the rest of it is what the partner API sold.
B2B_STAFF_EDITABLE = frozenset({"trip_style", "notes"})
SOURCES = ("tripplanner", "manual", "cis", "crm")  # crm: B2B cases opened by booking.b2b.v1 confirm (AA-973)
INITIAL_STATUSES = ("new_inquiry", "discovery")
SORTS = {"created_at": "tc.created_at", "updated_at": "tc.updated_at", "start_date": "tc.start_date"}
MAX_COUNTRIES = 20

CASE_COLUMNS = """
    tc.id::text AS id, tc.code, tc.status, tc.customer_id::text AS customer_id, tc.trip_style,
    tc.start_date, tc.end_date, tc.pax, tc.pax_adults, tc.pax_children, tc.currency, tc.notes, tc.source_system,
    tc.source_id, tc.channel, tc.party_tenant_id::text AS party_tenant_id, tc.created_at, tc.updated_at
"""
CUSTOMER_COLUMNS = """
    c.code AS customer_code, c.name AS customer_name, c.email AS customer_email, c.phone AS customer_phone,
    c.verified AS customer_verified, c.needs_merge_review AS customer_needs_merge_review
"""
OPEN_ASSIGNEES = """
    (SELECT coalesce(json_agg(json_build_object(
                'cis_user_id', a.staff_user_id, 'role', a.role, 'name', s.name, 'assigned_at', a.assigned_at)
            ORDER BY a.role), '[]'::json)::text
     FROM booking.staff_assignments a JOIN booking.staff_users s ON s.cis_user_id = a.staff_user_id
     WHERE a.trip_case_id = tc.id AND a.unassigned_at IS NULL) AS assignees
"""


def _case_filter(ref: str, placeholder: str = "$1") -> tuple[str, str]:
    """A trip case is addressed by its UUID or its code (``TC-000123``, case-insensitive)."""
    try:
        return f"tc.id = {placeholder}::uuid", str(uuid.UUID(ref))
    except ValueError:
        return f"tc.code = {placeholder}", ref.strip().upper()


def _case_dict(row: Any) -> dict[str, Any]:
    out = dict(row)
    if "assignees" in out:
        out["assignees"] = json.loads(out["assignees"] or "[]")
    if "countries" in out:
        out["countries"] = list(out["countries"] or [])
    return out


def ensure_staff_managed(case: Any) -> None:
    """A B2B case (``channel = 'b2b'``, AA-973) is opened confirmed by the partner API and has no customer: its status
    and its proposals belong to booking.b2b.v1 (the agency cancels with POST /bookings/{id}/cancel, AA-974), so staff
    status moves, sends and approvals are 409.
    A staff cancel here would leave the hold ``confirmed`` with its seats sold and the agency never told."""
    if case.get("channel") == "b2b":
        raise Conflict(
            "b2b_case_managed_by_partner_api",
            "this is a B2B case booked through the partner API; its status and proposals change only there",
        )


def reason_required(from_status: str, to_status: str) -> bool:
    """Cancelling (decline) and reopening a cancelled case both need a reason."""
    return to_status == "cancelled" or from_status == "cancelled"


def _day_start(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=UTC)


async def resolve_case(conn: asyncpg.Connection, ref: str, *, lock: bool = False) -> Any:
    where, arg = _case_filter(ref)
    row = await conn.fetchrow(
        f"SELECT {CASE_COLUMNS}, tc.inquiry_snapshot::text AS inquiry_snapshot FROM booking.trip_cases tc"  # noqa: S608
        f" WHERE {where} AND tc.deleted_at IS NULL{' FOR UPDATE' if lock else ''}",
        arg,
    )
    if row is None:
        raise NotFound("trip_case_not_found", f"trip case {ref} not found")
    return row


def _sort_clause(sort: str | None) -> str:
    raw = (sort or "-created_at").strip()
    desc = raw.startswith("-")
    column = SORTS.get(raw.lstrip("-+"))
    if column is None:
        raise Invalid("unknown_sort", f"sort must be one of {', '.join(SORTS)} (prefix - for descending)")
    return f"{column} {'DESC' if desc else 'ASC'} NULLS LAST, tc.id"


async def list_cases(
    conn: asyncpg.Connection,
    *,
    statuses: list[str] | None = None,
    assignee: str | None = None,
    q: str | None = None,
    source: str | None = None,
    customer_id: str | None = None,
    created_from: date | None = None,
    created_to: date | None = None,
    sort: str | None = None,
    limit: int = 25,
    offset: int = 0,
) -> dict[str, Any]:
    """``assignee``: a CIS user id (open assignment in any role), or ``none`` for unassigned cases.

    ``q`` matches the case code, the customer code, name or e-mail. ``created_to`` is inclusive."""
    where = ["tc.deleted_at IS NULL"]
    args: list[Any] = []

    def add(fragment: str, value: Any) -> None:
        args.append(value)
        where.append(fragment.replace("{p}", f"${len(args)}"))

    if statuses:
        unknown = sorted(set(statuses) - set(STATUSES))
        if unknown:
            raise Invalid("unknown_status", f"unknown status {', '.join(unknown)}")
        add("tc.status = ANY({p}::text[])", statuses)
    if assignee == "none":
        where.append(
            "NOT EXISTS (SELECT 1 FROM booking.staff_assignments a"
            " WHERE a.trip_case_id = tc.id AND a.unassigned_at IS NULL)"
        )
    elif assignee:
        add(
            "EXISTS (SELECT 1 FROM booking.staff_assignments a WHERE a.trip_case_id = tc.id"
            " AND a.unassigned_at IS NULL AND a.staff_user_id = {p})",
            assignee,
        )
    if q and q.strip():
        add("(tc.code ILIKE {p} OR c.code ILIKE {p} OR c.name ILIKE {p} OR c.email ILIKE {p})", paging.contains(q))
    if source:
        if source not in SOURCES:
            raise Invalid("unknown_source", f"source must be one of {', '.join(SOURCES)}")
        add("tc.source_system = {p}", source)
    if customer_id:
        add("tc.customer_id = {p}::uuid", customer_id)
    if created_from:
        add("tc.created_at >= {p}", _day_start(created_from))
    if created_to:
        add("tc.created_at < {p}", _day_start(created_to + timedelta(days=1)))
    order = _sort_clause(sort)
    clause = " AND ".join(where)
    base = "FROM booking.trip_cases tc LEFT JOIN booking.customers c ON c.id = tc.customer_id"  # b2b: no customer
    total = await conn.fetchval(f"SELECT count(*) {base} WHERE {clause}", *args)  # noqa: S608
    rows = await conn.fetch(
        f"""
        SELECT {CASE_COLUMNS}, {CUSTOMER_COLUMNS}, {OPEN_ASSIGNEES}, {sql.LEGS_JSON}, {sql.COUNTRIES_ARRAY},
               tc.inquiry_snapshot::text AS inquiry_snapshot,
               (SELECT max(v.version_number) FROM booking.trip_case_versions v
                WHERE v.trip_case_id = tc.id AND v.deleted_at IS NULL) AS latest_version
        {base}
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
        case = _case_dict(row)
        legs = sql.loads(case.pop("legs", None), [])
        snapshot = sql.loads(case.pop("inquiry_snapshot", None))
        case.update(sql.trip_shape(snapshot, legs, case.get("start_date")))
        items.append(case)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


async def summary(conn: asyncpg.Connection, staff: Staff) -> dict[str, Any]:
    """Dashboard counts. ``mine`` = open cases with an open assignment to the caller; ``proposals_awaiting`` =
    cases in ``proposal_sent`` (a sent version waits for the customer); ``mom_delta`` compares cases opened
    this calendar month (UTC) with the previous month."""
    row = await conn.fetchrow(
        """
        SELECT
            count(*) FILTER (WHERE tc.status NOT IN ('completed', 'cancelled')) AS open,
            count(*) FILTER (WHERE tc.status = 'new_inquiry') AS new_inquiry,
            count(*) FILTER (WHERE tc.status = 'proposal_sent') AS proposals_awaiting,
            count(*) FILTER (WHERE tc.status NOT IN ('completed', 'cancelled') AND EXISTS (
                SELECT 1 FROM booking.staff_assignments a
                WHERE a.trip_case_id = tc.id AND a.unassigned_at IS NULL AND a.staff_user_id = $1)) AS mine,
            count(*) FILTER (WHERE tc.created_at >= date_trunc('month', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')
                AS created_this_month,
            count(*) FILTER (
                WHERE tc.created_at >= (date_trunc('month', now() AT TIME ZONE 'UTC') - interval '1 month')
                                       AT TIME ZONE 'UTC'
                  AND tc.created_at < date_trunc('month', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')
                AS created_last_month
        FROM booking.trip_cases tc WHERE tc.deleted_at IS NULL
        """,
        staff.cis_user_id,
    )
    by_status = await conn.fetch(
        "SELECT status, count(*) AS n FROM booking.trip_cases WHERE deleted_at IS NULL GROUP BY status"
    )
    counts = {r["status"]: r["n"] for r in by_status}
    this_month, last_month = row["created_this_month"], row["created_last_month"]
    return {
        "open": row["open"],
        "new_inquiry": row["new_inquiry"],
        "mine": row["mine"],
        "proposals_awaiting": row["proposals_awaiting"],
        "by_status": {s: counts.get(s, 0) for s in STATUSES},
        "mom_delta": {
            "created_this_month": this_month,
            "created_last_month": last_month,
            "delta": this_month - last_month,
        },
    }


async def allowed_transitions(conn: asyncpg.Connection, status: str) -> list[str]:
    rows = await conn.fetch(
        "SELECT to_status FROM booking.trip_case_status_transitions WHERE from_status = $1 ORDER BY to_status", status
    )
    return [r["to_status"] for r in rows]


async def get_case(conn: asyncpg.Connection, ref: str) -> dict[str, Any]:
    where, arg = _case_filter(ref)
    row = await conn.fetchrow(
        f"""
        SELECT {CASE_COLUMNS}, {CUSTOMER_COLUMNS}, {OPEN_ASSIGNEES}, {sql.COUNTRIES_ARRAY},
               tc.inquiry_snapshot::text AS inquiry_snapshot, tc.inbox_event_id::text AS inbox_event_id
        FROM booking.trip_cases tc LEFT JOIN booking.customers c ON c.id = tc.customer_id
        WHERE {where} AND tc.deleted_at IS NULL
        """,  # noqa: S608 — fixed fragments
        arg,
    )
    if row is None:
        raise NotFound("trip_case_not_found", f"trip case {ref} not found")
    case = _case_dict(row)
    case["inquiry_snapshot"] = json.loads(row["inquiry_snapshot"]) if row["inquiry_snapshot"] else None
    case_id = case["id"]
    legs = await conn.fetch(
        "SELECT position, source_tour_id, day_from, day_to, source_system, source_id FROM booking.trip_case_legs"
        " WHERE trip_case_id = $1::uuid ORDER BY day_from, position",
        case_id,
    )
    versions = await conn.fetch(
        f"""
        SELECT {sql.VERSION_COLUMNS},
               (SELECT count(*) FROM booking.trip_case_version_itinerary i WHERE i.trip_case_version_id = v.id)
                   AS item_count
        FROM booking.trip_case_versions v
        WHERE v.trip_case_id = $1::uuid AND v.deleted_at IS NULL ORDER BY v.version_number
        """,  # noqa: S608 — constant fragment
        case_id,
    )
    history = await conn.fetch(
        "SELECT from_status, to_status, changed_by, note, changed_at FROM booking.trip_case_status_history"
        " WHERE trip_case_id = $1::uuid ORDER BY changed_at, seq",
        case_id,
    )
    assignments = await conn.fetch(
        "SELECT a.id::text AS id, a.staff_user_id, s.name AS staff_name, a.role, a.assigned_by, a.assigned_at,"
        " a.unassigned_at FROM booking.staff_assignments a JOIN booking.staff_users s"
        " ON s.cis_user_id = a.staff_user_id WHERE a.trip_case_id = $1::uuid ORDER BY a.assigned_at",
        case_id,
    )
    case["legs"] = [dict(r) for r in legs]
    case.update(sql.trip_shape(case["inquiry_snapshot"], case["legs"], case.get("start_date")))
    case["versions"] = [sql.version_dict(r) for r in versions]
    case["status_history"] = [dict(r) for r in history]
    case["assignments"] = [dict(r) for r in assignments]
    case["allowed_transitions"] = await allowed_transitions(conn, case["status"])
    return case


async def activity(conn: asyncpg.Connection, ref: str, *, before: str | None = None, limit: int = 50) -> dict[str, Any]:
    """Status history merged with the audit rows of the case and its versions, newest first.

    Audit rows of status changes are left out: the history row already says it (with the trigger's actor)."""
    case = await resolve_case(conn, ref)
    cursor = paging.decode(before)
    rows = await conn.fetch(
        """
        SELECT * FROM (
            SELECT h.id, 'status' AS kind, h.changed_at AS at, h.changed_by AS actor_ref, s.name AS actor_name,
                   'trip_case.status_changed' AS action, NULL::text AS entity_label, h.note AS summary,
                   json_build_object('status', h.from_status)::text AS before,
                   json_build_object('status', h.to_status)::text AS after
            FROM booking.trip_case_status_history h
            LEFT JOIN booking.staff_users s ON s.cis_user_id = h.changed_by
            WHERE h.trip_case_id = $1::uuid
            UNION ALL
            SELECT l.id, 'audit', l.created_at, l.actor_ref, s.name, l.action, l.entity_label, l.summary,
                   l.before::text, l.after::text
            FROM booking.audit_logs l
            LEFT JOIN booking.staff_users s ON s.cis_user_id = l.actor_ref
            WHERE l.action <> 'trip_case.status_changed'
              AND ((l.entity_type = 'trip_case' AND l.entity_id = $1::uuid)
                   OR (l.entity_type = 'trip_case_version' AND l.entity_id IN (
                       SELECT v.id FROM booking.trip_case_versions v WHERE v.trip_case_id = $1::uuid)))
        ) a
        WHERE ($2::timestamptz IS NULL OR (a.at, a.id) < ($2::timestamptz, $3::uuid))
        ORDER BY a.at DESC, a.id DESC
        LIMIT $4
        """,
        case["id"],
        cursor[0] if cursor else None,
        cursor[1] if cursor else None,
        limit,
    )
    items = []
    for r in rows:
        before_v, after_v = sql.loads(r["before"]), sql.loads(r["after"])
        text = r["summary"]
        if r["kind"] == "status":
            arrow = f"{before_v['status'] or 'opened'} -> {after_v['status']}"
            text = f"{arrow}: {r['summary']}" if r["summary"] else arrow
        items.append(
            {
                "id": str(r["id"]),
                "kind": r["kind"],
                "at": r["at"],
                "actor": {"id": r["actor_ref"], "name": r["actor_name"]},
                "action": r["action"],
                "entity_label": r["entity_label"],
                "summary": text,
                "before": before_v,
                "after": after_v,
            }
        )
    return {"items": items, "next_before": paging.next_cursor(items, limit)}


def _plain(row: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    return {k: row[k] for k in keys}


def _pax_consistent(changes: dict[str, Any], before: dict[str, Any]) -> dict[str, Any]:
    """A changed adults / children split recomputes ``pax`` unless the caller sent it too (the DB check
    ``trip_cases_pax_split_chk`` is the last word on a mismatch)."""
    if ("pax_adults" in changes or "pax_children" in changes) and "pax" not in changes:
        adults = changes.get("pax_adults", before.get("pax_adults"))
        children = changes.get("pax_children", before.get("pax_children"))
        if adults is not None:
            return {**changes, "pax": adults + (children or 0)}
    return changes


async def _countries(conn: asyncpg.Connection, case_id: str) -> list[str]:
    rows = await conn.fetch(
        "SELECT country_code::text AS code FROM booking.trip_case_countries WHERE trip_case_id = $1::uuid"
        " ORDER BY position, country_code",
        case_id,
    )
    return [r["code"] for r in rows]


async def _set_countries(conn: asyncpg.Connection, case_id: str, codes: list[str]) -> None:
    unique = list(dict.fromkeys(c.upper() for c in codes))
    if len(unique) > MAX_COUNTRIES:
        raise Invalid("too_many_countries", f"at most {MAX_COUNTRIES} countries")
    known = await conn.fetch(
        "SELECT iso2::text AS iso2 FROM booking.countries WHERE iso2 = ANY($1::text[]) AND active", unique
    )
    missing = sorted(set(unique) - {r["iso2"] for r in known})
    if missing:
        raise Invalid("unknown_country", f"unknown country code {', '.join(missing)}")
    await conn.execute("DELETE FROM booking.trip_case_countries WHERE trip_case_id = $1::uuid", case_id)
    if unique:
        await conn.executemany(
            "INSERT INTO booking.trip_case_countries (trip_case_id, country_code, position) VALUES ($1::uuid, $2, $3)",
            [(case_id, code, i) for i, code in enumerate(unique, start=1)],
        )


async def update_case(conn: asyncpg.Connection, staff: Staff, ref: str, changes: dict[str, Any]) -> dict[str, Any]:
    countries = changes.pop("countries", None) if "countries" in changes else None
    countries_sent = countries is not None
    unknown = sorted(set(changes) - set(EDITABLE_FIELDS))
    if unknown:
        raise Invalid("not_editable", f"not editable: {', '.join(unknown)}")
    row = await resolve_case(conn, ref, lock=True)
    before = _plain(row, EDITABLE_FIELDS)
    changes = _pax_consistent(changes, before)
    changed = {k: v for k, v in changes.items() if before[k] != v}
    if set(changed) - B2B_STAFF_EDITABLE:
        ensure_staff_managed(row)  # B2B: pax / dates / currency are what the hold sold (AA-973)
    before_countries = await _countries(conn, row["id"]) if countries_sent else None
    countries_changed = countries_sent and [c.upper() for c in countries] != before_countries
    if not changed and not countries_changed:
        return {**before, "id": row["id"], "code": row["code"], "changed": []}
    updated: Any = {**before, "id": row["id"], "code": row["code"]}
    if changed:
        sets = ", ".join(f"{column} = ${i + 2}" for i, column in enumerate(changed))
        try:
            updated = await conn.fetchrow(
                f"UPDATE booking.trip_cases SET {sets} WHERE id = $1::uuid"  # noqa: S608 — whitelisted column names
                f" RETURNING id::text AS id, code, {', '.join(EDITABLE_FIELDS)}",
                row["id"],
                *changed.values(),
            )
        except asyncpg.CheckViolationError as exc:
            raise Invalid("invalid_value", exc.constraint_name or "check violation") from exc
    audit_before = {k: before[k] for k in changed}
    audit_after = {k: updated[k] for k in changed}
    if countries_changed:
        await _set_countries(conn, row["id"], countries)
        if not changed:  # only countries moved: bump updated_at for list ordering / last activity
            await conn.execute("UPDATE booking.trip_cases SET updated_at = now() WHERE id = $1::uuid", row["id"])
        audit_before["countries"] = before_countries
        audit_after["countries"] = await _countries(conn, row["id"])
    names = sorted([*changed, *(["countries"] if countries_changed else [])])
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="trip_case.updated",
        entity_type="trip_case",
        entity_id=row["id"],
        before=audit_before,
        after=audit_after,
        entity_label=row["code"],
        summary=f"Updated {', '.join(names)}",
    )
    out = dict(updated)
    if countries_changed:
        out["countries"] = audit_after["countries"]
    return {**out, "changed": names}


async def set_status(conn: asyncpg.Connection, staff: Staff, case: Any, to_status: str, note: str | None) -> None:
    """Move a locked case row along one allowed edge: the trigger writes the history row with this actor/note."""
    await conn.execute(
        "SELECT set_config('booking.actor', $1, true), set_config('booking.status_note', $2, true)",
        staff.cis_user_id,
        note or "",
    )
    try:
        await conn.execute("UPDATE booking.trip_cases SET status = $2 WHERE id = $1::uuid", case["id"], to_status)
    except asyncpg.CheckViolationError as exc:  # the trigger is the last word if the rules changed meanwhile
        raise Conflict("illegal_transition", str(exc)) from exc
    await conn.execute("SELECT set_config('booking.status_note', '', true)")
    await _notify_sales_of_status(conn, staff, case, to_status)


async def _notify_sales_of_status(conn: asyncpg.Connection, staff: Staff, case: Any, to_status: str) -> None:
    owner = await conn.fetchval(
        "SELECT staff_user_id FROM booking.staff_assignments"
        " WHERE trip_case_id = $1::uuid AND role = 'sales' AND unassigned_at IS NULL",
        case["id"],
    )
    if owner and owner != staff.cis_user_id:
        await notifications.notify_staff(
            conn,
            type="trip_case.status_changed",
            title=f"{case['code']} is now {to_status}",
            message=f"{staff.name or staff.cis_user_id} moved {case['code']} from {case['status']} to {to_status}.",
            entity_type="trip_case",
            entity_id=case["id"],
            data={"code": case["code"], "from_status": case["status"], "to_status": to_status},
            recipient_ref=owner,
        )


async def change_status(
    conn: asyncpg.Connection, staff: Staff, ref: str, to_status: str, reason: str | None
) -> dict[str, Any]:
    if to_status not in STATUSES:
        raise Invalid("unknown_status", f"unknown status {to_status}")
    reason = (reason or "").strip() or None
    row = await resolve_case(conn, ref, lock=True)
    ensure_staff_managed(row)
    from_status = row["status"]
    if from_status == to_status:
        raise Conflict("status_unchanged", f"trip case is already {to_status}")
    allowed = await allowed_transitions(conn, from_status)
    if to_status not in allowed:
        raise Conflict(
            "illegal_transition", f"{from_status} -> {to_status} is not allowed", allowed_transitions=allowed
        )
    if to_status in APPROVAL_ONLY_STATUSES:
        raise Conflict(
            "approve_version_first",
            f"{to_status} is reached by approving a sent version (POST .../versions/{{n}}/approve)",
            allowed_transitions=allowed,
        )
    if reason_required(from_status, to_status) and not reason:
        raise Invalid("reason_required", f"a reason is required to move a trip case from {from_status} to {to_status}")
    await set_status(conn, staff, row, to_status, reason)
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="trip_case.status_changed",
        entity_type="trip_case",
        entity_id=row["id"],
        before={"status": from_status},
        after={"status": to_status, "reason": reason},
        entity_label=row["code"],
        summary=f"{from_status} -> {to_status}" + (f": {reason}" if reason else ""),
    )
    return {
        "id": row["id"],
        "code": row["code"],
        "from_status": from_status,
        "status": to_status,
        "reason": reason,
        "allowed_transitions": await allowed_transitions(conn, to_status),
    }


async def assign(
    conn: asyncpg.Connection, staff: Staff, ref: str, cis_user_id: str | None, role: str
) -> dict[str, Any]:
    """Give ``role`` on the case to ``cis_user_id`` (closing the previous holder), or close it when None."""
    if role not in ASSIGNMENT_ROLES:
        raise Invalid("unknown_assignment_role", f"role must be one of {', '.join(ASSIGNMENT_ROLES)}")
    case = await resolve_case(conn, ref, lock=True)
    current = await conn.fetchrow(
        "SELECT id::text AS id, staff_user_id FROM booking.staff_assignments"
        " WHERE trip_case_id = $1::uuid AND role = $2 AND unassigned_at IS NULL FOR UPDATE",
        case["id"],
        role,
    )
    previous = current["staff_user_id"] if current else None
    if cis_user_id is not None:
        target = await conn.fetchrow(
            "SELECT cis_user_id, active FROM booking.staff_users WHERE cis_user_id = $1", cis_user_id
        )
        if target is None or not target["active"]:
            raise Invalid("staff_not_assignable", "the staff user does not exist or is not active")
    if previous == cis_user_id:
        return {"id": case["id"], "code": case["code"], "role": role, "staff_user_id": previous, "changed": False}
    if current is not None:
        await conn.execute(
            "UPDATE booking.staff_assignments SET unassigned_at = now() WHERE id = $1::uuid", current["id"]
        )
    if cis_user_id is not None:
        await conn.execute(
            "INSERT INTO booking.staff_assignments (trip_case_id, staff_user_id, role, assigned_by)"
            " VALUES ($1::uuid, $2, $3, $4)",
            case["id"],
            cis_user_id,
            role,
            staff.cis_user_id,
        )
        if cis_user_id != staff.cis_user_id:
            await notifications.notify_staff(
                conn,
                type="trip_case.assigned",
                title=f"{case['code']} assigned to you ({role})",
                message=f"{staff.name or staff.cis_user_id} assigned {case['code']} to you as {role}.",
                entity_type="trip_case",
                entity_id=case["id"],
                data={"code": case["code"], "role": role},
                recipient_ref=cis_user_id,
            )
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="trip_case.assigned" if cis_user_id else "trip_case.unassigned",
        entity_type="trip_case",
        entity_id=case["id"],
        before={"role": role, "staff_user_id": previous},
        after={"role": role, "staff_user_id": cis_user_id},
        entity_label=case["code"],
        summary=f"{role}: {previous or '-'} -> {cis_user_id or '-'}",
    )
    return {"id": case["id"], "code": case["code"], "role": role, "staff_user_id": cis_user_id, "changed": True}


async def create_case(
    conn: asyncpg.Connection, staff: Staff, data: dict[str, Any], *, source: tuple[str, str | None] = ("manual", None)
) -> dict[str, Any]:
    """A staff-created case for an existing customer or a new manual lead. ``source`` is ``('manual', None)``, or
    ``('cis', <catalog source_id>)`` for a case started from a sellable trip plan (AA-679, ``app.catalog``).

    ``data``: ``customer_id`` xor ``new_customer`` ({name, email?, phone?, address?, contact_consent}),
    ``start_date``, ``pax`` / ``pax_adults`` / ``pax_children``, ``currency`` (default USD), ``trip_style``,
    ``notes``, ``countries`` (ISO-2), ``assignee`` (sales; default the caller when they are sales),
    ``initial_status`` (``new_inquiry`` default, or ``discovery`` for a lead already in conversation)."""
    from app.customers import service as customers  # local: customers imports this module for archive

    initial = data.get("initial_status") or "new_inquiry"
    if initial not in INITIAL_STATUSES:
        raise Invalid("invalid_initial_status", f"initial_status must be one of {', '.join(INITIAL_STATUSES)}")
    if bool(data.get("customer_id")) == bool(data.get("new_customer")):
        raise Invalid("customer_required", "send exactly one of customer_id or new_customer")
    if data.get("customer_id"):
        customer = await customers.resolve_customer(conn, data["customer_id"], lock=True)
        if customer["is_blocked"]:
            raise Conflict("customer_blocked", "the customer is blocked")
        customer_id = customer["id"]
    else:
        customer_id = (await customers.create_lead(conn, staff, data["new_customer"]))["id"]
    pax_fields = _pax_consistent(
        {k: data[k] for k in ("pax", "pax_adults", "pax_children") if data.get(k) is not None}, {}
    )
    await conn.execute("SELECT set_config('booking.actor', $1, true)", staff.cis_user_id)
    try:
        case = await conn.fetchrow(
            """
            INSERT INTO booking.trip_cases
                (customer_id, status, source_system, source_id, start_date, pax, pax_adults, pax_children, currency,
                 trip_style, notes, created_by)
            VALUES ($1::uuid, $2, $11, $12, $3, $4, $5, $6, $7, $8, $9, $10::uuid)
            RETURNING id::text AS id, code, status
            """,
            customer_id,
            initial,
            data.get("start_date"),
            pax_fields.get("pax"),
            pax_fields.get("pax_adults"),
            pax_fields.get("pax_children"),
            data.get("currency") or "USD",
            data.get("trip_style"),
            data.get("notes"),
            audit.as_uuid(staff.cis_user_id),
            source[0],
            source[1],
        )
    except asyncpg.CheckViolationError as exc:
        raise Invalid("invalid_value", exc.constraint_name or "check violation") from exc
    if data.get("countries"):
        await _set_countries(conn, case["id"], data["countries"])
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="trip_case.created",
        entity_type="trip_case",
        entity_id=case["id"],
        after={
            "customer_id": customer_id,
            "status": case["status"],
            "source_system": source[0],
            **({"source_id": source[1]} if source[1] else {}),
            **{k: data.get(k) for k in ("start_date", "currency", "trip_style", "countries")},
            **pax_fields,
        },
        entity_label=case["code"],
        summary=f"Created {case['code']} ({case['status']})",
    )
    assignee = data.get("assignee") or (staff.cis_user_id if staff.role == "sales" else None)
    if assignee:
        await assign(conn, staff, case["id"], assignee, "sales")
    return await get_case(conn, case["id"])


def is_terminal(status: str) -> bool:
    return status in TERMINAL_STATUSES
