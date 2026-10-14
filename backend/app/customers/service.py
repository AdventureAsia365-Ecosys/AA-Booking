"""Customers on the staff side (S236 UI spec §4.12): list / detail over ``booking.v_customer_summary``
(bk-005), manual leads, edit, block, archive.

Rules carried over from intake (O6, contract v1.1): a staff-entered contact is an **unverified lead**
(``verified = false``, ``source = 'manual'``) and is never merged into an existing customer; when its
e-mail or phone matches a live customer it is flagged ``needs_merge_review`` with ``merge_candidate_id``.
E-mail is editable only while the customer is unverified (Q14). There is no hard delete (02 #7): archive
sets ``deleted_at`` and is refused once any case went past ``new_inquiry``; its open new inquiries are
cancelled with the reason "customer archived".
"""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg

from app import audit, paging
from app.auth.staff import Staff
from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.trip_cases import service as trip_cases

STATUSES = ("lead", "active", "past")
SOURCES = ("manual", "otp", "tripplanner_lead")
SORTS = {"last_activity_at": "s.last_activity_at", "created_at": "s.created_at", "name": "lower(s.name)"}
EDITABLE = ("name", "email", "phone", "address", "is_blocked")
ARCHIVE_REASON = "customer archived"

CUSTOMER_COLUMNS = "id::text AS id, code, name, email, phone, address, verified, is_blocked, needs_merge_review, source"
SUMMARY_COLUMNS = """
    s.id::text AS id, s.code, s.name, s.email, s.phone, s.address, s.verified, s.is_blocked,
    s.needs_merge_review, s.merge_candidate_id::text AS merge_candidate_id, s.source, s.contact_consent,
    s.contact_consent_at, s.case_count, s.open_case_count, s.last_activity_at, s.status, s.created_at, s.updated_at
"""


def _customer_filter(ref: str) -> tuple[str, str]:
    """A customer is addressed by its UUID or its code (``CUS-000123``)."""
    try:
        return "id = $1::uuid", str(uuid.UUID(ref))
    except ValueError:
        return "code = $1", ref.strip().upper()


async def resolve_customer(conn: asyncpg.Connection, ref: str, *, lock: bool = False) -> Any:
    where, arg = _customer_filter(ref)
    row = await conn.fetchrow(
        f"SELECT {CUSTOMER_COLUMNS} FROM booking.customers"  # noqa: S608 — constant, placeholder-only filter
        f" WHERE {where} AND deleted_at IS NULL{' FOR UPDATE' if lock else ''}",
        arg,
    )
    if row is None:
        raise NotFound("customer_not_found", f"customer {ref} not found")
    return row


async def list_customers(
    conn: asyncpg.Connection,
    *,
    q: str | None = None,
    status: str | None = None,
    blocked: bool | None = None,
    merge_review: bool | None = None,
    source: str | None = None,
    sort: str | None = None,
    limit: int = 25,
    offset: int = 0,
) -> dict[str, Any]:
    """``status`` = lead | active | past, or ``blocked`` (the Blocked chip; same as ``blocked=true``)."""
    where: list[str] = []
    args: list[Any] = []

    def add(fragment: str, value: Any) -> None:
        args.append(value)
        where.append(fragment.replace("{p}", f"${len(args)}"))

    if status == "blocked":
        blocked = True
    elif status:
        if status not in STATUSES:
            raise Invalid("unknown_status", f"status must be one of {', '.join((*STATUSES, 'blocked'))}")
        add("s.status = {p}", status)
    if blocked is not None:
        add("s.is_blocked = {p}", blocked)
    if merge_review is not None:
        add("s.needs_merge_review = {p}", merge_review)
    if source:
        if source not in SOURCES:
            raise Invalid("unknown_source", f"source must be one of {', '.join(SOURCES)}")
        add("s.source = {p}", source)
    if q and q.strip():
        add("(s.name ILIKE {p} OR s.email ILIKE {p} OR s.phone ILIKE {p} OR s.code ILIKE {p})", paging.contains(q))
    raw = (sort or "-last_activity_at").strip()
    column = SORTS.get(raw.lstrip("-+"))
    if column is None:
        raise Invalid("unknown_sort", f"sort must be one of {', '.join(SORTS)} (prefix - for descending)")
    order = f"{column} {'DESC' if raw.startswith('-') else 'ASC'} NULLS LAST, s.id"
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    total = await conn.fetchval(f"SELECT count(*) FROM booking.v_customer_summary s {clause}", *args)  # noqa: S608
    rows = await conn.fetch(
        f"SELECT {SUMMARY_COLUMNS} FROM booking.v_customer_summary s {clause}"  # noqa: S608 — fixed fragments
        f" ORDER BY {order} LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}",
        *args,
        limit,
        offset,
    )
    return {"items": [dict(r) for r in rows], "total": total, "limit": limit, "offset": offset}


async def get_customer(conn: asyncpg.Connection, ref: str) -> dict[str, Any]:
    where, arg = _customer_filter(ref)
    row = await conn.fetchrow(
        f"SELECT {SUMMARY_COLUMNS} FROM booking.v_customer_summary s WHERE s.{where}",  # noqa: S608
        arg,
    )
    if row is None:
        raise NotFound("customer_not_found", f"customer {ref} not found")
    customer = dict(row)
    candidate = None
    if customer["merge_candidate_id"]:
        candidate = await conn.fetchrow(
            "SELECT id::text AS id, code, name, verified, deleted_at IS NOT NULL AS archived"
            " FROM booking.customers WHERE id = $1::uuid",
            customer["merge_candidate_id"],
        )
    customer["merge_candidate"] = dict(candidate) if candidate else None
    cases = await trip_cases.list_cases(conn, customer_id=customer["id"], limit=100)
    customer["cases"] = cases["items"]
    customer["inquiries"] = [
        {k: c[k] for k in ("id", "code", "status", "created_at", "trip_title", "leg_titles", "duration_days")}
        for c in cases["items"]
        if c["source_system"] == "tripplanner"
    ]
    activity = await conn.fetch(
        """
        SELECT l.id::text AS id, l.created_at AS at, l.actor_ref, s.name AS actor_name, l.action, l.entity_type,
               l.entity_label, l.summary
        FROM booking.audit_logs l LEFT JOIN booking.staff_users s ON s.cis_user_id = l.actor_ref
        WHERE (l.entity_type = 'customer' AND l.entity_id = $1::uuid)
           OR (l.entity_type = 'trip_case' AND l.entity_id IN (
               SELECT tc.id FROM booking.trip_cases tc WHERE tc.customer_id = $1::uuid))
        ORDER BY l.created_at DESC, l.id DESC
        LIMIT 20
        """,
        customer["id"],
    )
    customer["recent_activity"] = [
        {
            **{k: r[k] for k in ("id", "at", "action", "entity_type", "entity_label", "summary")},
            "actor": {"id": r["actor_ref"], "name": r["actor_name"]},
        }
        for r in activity
    ]
    return customer


def _clean_contact(data: dict[str, Any]) -> tuple[str | None, str | None]:
    email = (data.get("email") or "").strip().lower() or None
    phone = (data.get("phone") or "").strip() or None
    return email, phone


async def create_lead(conn: asyncpg.Connection, staff: Staff, data: dict[str, Any]) -> dict[str, Any]:
    """A manual lead: name, e-mail or phone (one required), address, consent. Never merged (O6)."""
    name = (data.get("name") or "").strip()
    if not name:
        raise Invalid("name_required", "a customer needs a name")
    email, phone = _clean_contact(data)
    if not email and not phone:
        raise Invalid("contact_required", "a customer needs an e-mail or a phone number")
    candidate = await conn.fetchval(
        """
        SELECT id::text FROM booking.customers
        WHERE deleted_at IS NULL
          AND (($1::text IS NOT NULL AND lower(email) = $1) OR ($2::text IS NOT NULL AND phone = $2))
        ORDER BY verified DESC, created_at
        LIMIT 1
        """,
        email,
        phone,
    )
    consent = bool(data.get("contact_consent"))
    row = await conn.fetchrow(
        """
        INSERT INTO booking.customers
            (name, email, phone, address, verified, source, needs_merge_review, merge_candidate_id,
             contact_consent, contact_consent_at)
        VALUES ($1, $2, $3, $4, false, 'manual', $5, $6::uuid, $7, CASE WHEN $7 THEN now() END)
        RETURNING id::text AS id, code
        """,
        name,
        email,
        phone,
        (data.get("address") or "").strip() or None,
        candidate is not None,
        candidate,
        consent,
    )
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="customer.created",
        entity_type="customer",
        entity_id=row["id"],
        after={
            "code": row["code"],
            "name": name,
            "email": email,
            "phone": phone,
            "source": "manual",
            "contact_consent": consent,
            "merge_candidate_id": candidate,
        },
        entity_label=row["code"],
        summary=f"Created manual lead {name}" + (" (needs merge review)" if candidate else ""),
    )
    return {"id": row["id"], "code": row["code"], "needs_merge_review": candidate is not None}


async def update_customer(conn: asyncpg.Connection, staff: Staff, ref: str, changes: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(changes) - set(EDITABLE))
    if unknown:
        raise Invalid("not_editable", f"not editable: {', '.join(unknown)}")
    if "is_blocked" in changes and not staff.can("customer.admin"):
        raise Forbidden("forbidden", "only an admin can block or unblock a customer")
    current = dict(await resolve_customer(conn, ref, lock=True))
    if "name" in changes:
        changes["name"] = (changes["name"] or "").strip()
        if not changes["name"]:
            raise Invalid("name_required", "a customer needs a name")
    for key in ("email", "phone", "address"):
        if key in changes:
            value = (changes[key] or "").strip() or None
            changes[key] = value.lower() if key == "email" and value else value
    if "email" in changes and changes["email"] != current["email"] and current["verified"]:
        raise Conflict("email_locked", "a verified customer's e-mail changes only through their own sign-in (OTP)")
    changed = {k: v for k, v in changes.items() if current[k] != v}
    if not changed:
        return {**current, "changed": []}
    email = changed.get("email", current["email"])
    phone = changed.get("phone", current["phone"])
    if not email and not phone:
        raise Invalid("contact_required", "a customer needs an e-mail or a phone number")
    sets = ", ".join(f"{column} = ${i + 2}" for i, column in enumerate(changed))
    try:
        await conn.execute(
            f"UPDATE booking.customers SET {sets} WHERE id = $1::uuid",  # noqa: S608 — whitelisted column names
            current["id"],
            *changed.values(),
        )
    except asyncpg.UniqueViolationError as exc:
        raise Conflict("contact_taken", "another verified customer already uses this e-mail or phone") from exc
    action = "customer.updated"
    if set(changed) == {"is_blocked"}:
        action = "customer.blocked" if changed["is_blocked"] else "customer.unblocked"
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action=action,
        entity_type="customer",
        entity_id=current["id"],
        before={k: current[k] for k in changed},
        after=changed,
        entity_label=current["code"],
        summary=f"Updated {', '.join(sorted(changed))}",
    )
    updated = dict(await resolve_customer(conn, current["id"]))
    return {**updated, "changed": sorted(changed)}


async def archive_customer(conn: asyncpg.Connection, staff: Staff, ref: str) -> None:
    customer = await resolve_customer(conn, ref, lock=True)
    cases = await conn.fetch(
        "SELECT id::text AS id, code, status FROM booking.trip_cases"
        " WHERE customer_id = $1::uuid AND deleted_at IS NULL ORDER BY created_at FOR UPDATE",
        customer["id"],
    )
    blocking = [c["code"] for c in cases if c["status"] not in ("new_inquiry", "cancelled")]
    if blocking:
        raise Conflict(
            "customer_has_cases",
            "a customer with a case past new_inquiry cannot be archived; block them instead",
            trip_cases=blocking,
        )
    cancelled = []
    for case in cases:
        if case["status"] == "new_inquiry":
            await trip_cases.set_status(conn, staff, case, "cancelled", ARCHIVE_REASON)
            cancelled.append(case["code"])
    await conn.execute("UPDATE booking.customers SET deleted_at = now() WHERE id = $1::uuid", customer["id"])
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="customer.archived",
        entity_type="customer",
        entity_id=customer["id"],
        before={"code": customer["code"], "name": customer["name"]},
        after={"archived": True, "cancelled_cases": cancelled},
        entity_label=customer["code"],
        summary="Archived" + (f"; cancelled {', '.join(cancelled)}" if cancelled else ""),
    )
