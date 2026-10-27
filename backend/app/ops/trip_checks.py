"""Trip checks of a sold trip case (bk-027; AA-682 slice S2, S252 design §3.2, §4, §5, §6).

A **trip check** is one thing to do before departure. When a case enters ``confirmed`` the database seeds three
**standard** checks (trigger ``trip_cases_seed_checks``): ``pre_trip_recheck`` (re-check every booking),
``emergency_contact`` and ``insurance``, due ``start_date - pre_departure_days``. Staff add **custom** checks (title
required). Life: ``open`` → ``done`` | ``not_applicable``, and back to ``open`` (reopen); a move between ``done`` and
``not_applicable`` goes through ``open`` in the same transaction (the guard knows only those edges).

Next to the stored checks a case has two **derived** checks, never stored (``booking.v_trip_check_board``):
``suppliers_confirmed`` (supply state ``booked`` or ``none``, bk-026) and ``payment_verified`` (``None`` until the
invoices of bk-028).

**Emergency contact** (name + phone of a third person, ``pii:emergency_contact``): B2C cases only. On a B2B case the
row stays ``not_applicable`` without contact fields (DM-6.8: the agency keeps traveller data); the database refuses
anything else, this module answers 422 ``contact_not_allowed_b2b`` first. A B2C emergency contact check is ``done``
only with both fields. The fields and the notes are cleared at trip end + ``emergency_contact_keep_days`` by the
``bk_case_lifecycle`` job (:mod:`app.ops.lifecycle`); a cleared check takes no contact again (409 ``check_cleared``).
Audit rows name the fields that changed, never their values.

Permissions: ``ops.read`` / ``ops.write`` (admin, sales, contracting); the customer name in the list needs
``customer.read`` as well. Writes lock the case row (one writer per case at a time) and add one audit row.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

import asyncpg

from app import audit
from app.auth.staff import Staff
from app.errors import Conflict, Invalid, NotFound

STATUSES = ("open", "done", "not_applicable")
CODES = ("pre_trip_recheck", "emergency_contact", "insurance", "custom")
STANDARD_CODES = ("pre_trip_recheck", "emergency_contact", "insurance")
# Cases on the check board (v_trip_check_board): sold and not finished.
BOARD_STATUSES = (
    "confirmed",
    "supplier_confirming",
    "invoice_sent",
    "payment_pending",
    "paid",
    "pre_departure",
    "on_trip",
)
CLOSED_CASE_STATUSES = frozenset({"cancelled", "completed"})
PATCH_FIELDS = (
    "title",
    "status",
    "due_date",
    "assignee_ref",
    "note",
    "emergency_contact_name",
    "emergency_contact_phone",
)
CONTACT_FIELDS = ("emergency_contact_name", "emergency_contact_phone")
# Fields whose values are personal data: the audit row says they changed, not to what.
PRIVATE_FIELDS = frozenset({"note", "emergency_contact_name", "emergency_contact_phone"})
MAX_PAGE = 100
LABELS = {
    "pre_trip_recheck": "Re-check bookings",
    "emergency_contact": "Emergency contact",
    "insurance": "Insurance",
    "custom": "Custom",
}

CHECK_COLUMNS = """
    k.id::text AS id, k.trip_case_id::text AS trip_case_id, tc.code AS trip_case_code, tc.channel,
    tc.status AS trip_case_status, tc.start_date, tc.end_date, k.code, k.title, k.status, k.due_date,
    k.assignee_ref, sa.name AS assignee_name, k.done_by, sd.name AS done_by_name, k.done_at, k.note,
    k.emergency_contact_name, k.emergency_contact_phone, k.cleared_at, k.created_by, k.created_at, k.updated_at,
    cu.name AS customer_name
"""
CHECK_FROM = """
FROM booking.trip_case_checks k
JOIN booking.trip_cases tc ON tc.id = k.trip_case_id
LEFT JOIN booking.customers cu ON cu.id = tc.customer_id
LEFT JOIN booking.staff_users sa ON sa.cis_user_id = k.assignee_ref
LEFT JOIN booking.staff_users sd ON sd.cis_user_id = k.done_by
"""
BOARD_SQL = """
SELECT checks_total, checks_open, checks_done, checks_not_applicable, checks_overdue, next_due_date, supply_state,
       suppliers_confirmed, payment_verified
FROM booking.v_trip_check_board WHERE trip_case_id = $1::uuid
"""


def _uuid(value: Any) -> str | None:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        return None


def _case_filter(ref: str) -> tuple[str, str]:
    ident = _uuid(ref)
    if ident:
        return "tc.id = $1::uuid", ident
    return "tc.code = $1", ref.strip().upper()


async def _case(conn: asyncpg.Connection, ref: str, *, lock: bool = False) -> Any:
    where, arg = _case_filter(ref)
    row = await conn.fetchrow(
        "SELECT tc.id::text AS id, tc.code, tc.status, tc.channel, tc.start_date, tc.end_date"  # noqa: S608
        f" FROM booking.trip_cases tc WHERE {where} AND tc.deleted_at IS NULL" + (" FOR UPDATE" if lock else ""),
        arg,
    )
    if row is None:
        raise NotFound("trip_case_not_found", f"trip case {ref} not found")
    return row


def check_out(row: Any, staff: Staff) -> dict[str, Any]:
    out = dict(row)
    out["trip_case"] = {
        "id": out.pop("trip_case_id"),
        "code": out.pop("trip_case_code"),
        "status": out.pop("trip_case_status"),
        "channel": out.pop("channel"),
        "start_date": out.pop("start_date"),
        "end_date": out.pop("end_date"),
        "customer_name": out.pop("customer_name") if staff.can("customer.read") else None,
    }
    out.pop("customer_name", None)
    out["assignee"] = {"id": out["assignee_ref"], "name": out.pop("assignee_name")} if out["assignee_ref"] else None
    out.pop("assignee_name", None)
    out["done_by"] = {"id": out["done_by"], "name": out.pop("done_by_name")} if out["done_by"] else None
    out.pop("done_by_name", None)
    out["standard"] = out["code"] != "custom"
    out["contact_allowed"] = out["code"] == "emergency_contact" and out["trip_case"]["channel"] != "b2b"
    return out


async def _get(conn: asyncpg.Connection, check_id: str, *, lock: bool = False) -> Any:
    ident = _uuid(check_id)
    if ident is None:
        raise NotFound("trip_check_not_found", f"trip check {check_id} not found")
    row = await conn.fetchrow(
        f"SELECT {CHECK_COLUMNS} {CHECK_FROM} WHERE k.id = $1::uuid AND tc.deleted_at IS NULL"  # noqa: S608
        + (" FOR UPDATE OF k" if lock else ""),
        ident,
    )
    if row is None:
        raise NotFound("trip_check_not_found", f"trip check {check_id} not found")
    return row


def derived_checks(board: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The two checks computed from other facts (never stored): suppliers and payment."""
    supply = (board or {}).get("supply_state")
    return [
        {
            "code": "suppliers_confirmed",
            "title": "Every supplier booking confirmed",
            "status": None if board is None else ("done" if board["suppliers_confirmed"] else "open"),
            "detail": supply,
            "available": board is not None,
        },
        {
            "code": "payment_verified",
            "title": "Paid in full (verified payments)",
            "status": None
            if board is None or board["payment_verified"] is None
            else ("done" if board["payment_verified"] else "open"),
            "detail": None,
            # bk-028 (AA-682 S3): the B2C invoice is paid by verified payments; NULL on B2B (no customer invoice).
            "available": board is not None and board["payment_verified"] is not None,
        },
    ]


# ── Reads ────────────────────────────────────────────────────────────────────


async def list_for_case(conn: asyncpg.Connection, staff: Staff, ref: str) -> dict[str, Any]:
    """The case's checks (standard first, then custom by creation), the board counts and the derived checks."""
    case = await _case(conn, ref)
    rows = await conn.fetch(
        f"SELECT {CHECK_COLUMNS} {CHECK_FROM} WHERE k.trip_case_id = $1::uuid"  # noqa: S608 — constants only
        " ORDER BY (k.code = 'custom'), array_position($2::text[], k.code), k.created_at, k.id",
        case["id"],
        list(STANDARD_CODES),
    )
    board_row = await conn.fetchrow(BOARD_SQL, case["id"])
    board = dict(board_row) if board_row else None
    return {
        "trip_case": {
            "id": case["id"],
            "code": case["code"],
            "status": case["status"],
            "channel": case["channel"],
            "start_date": case["start_date"],
            "end_date": case["end_date"],
        },
        "board": board,
        "items": [check_out(r, staff) for r in rows],
        "derived": derived_checks(board),
        "contact_allowed": case["channel"] != "b2b",
        "writable": case["status"] not in CLOSED_CASE_STATUSES,
    }


async def list_checks(
    conn: asyncpg.Connection,
    staff: Staff,
    *,
    status: str | None = "open",
    due_before: date | None = None,
    assignee: str | None = None,
    code: str | None = None,
    case: str | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Checks across the open sold cases (the /trip-checks page), soonest due first.

    ``status``: open (default), done, not_applicable or all. ``assignee``: a staff key, ``me`` or ``none``.
    ``due_before``: due on or before that date (overdue and due soon). ``q``: case code or check title."""
    if status not in (*STATUSES, "all", None):
        raise Invalid("unknown_status", f"status must be one of {', '.join((*STATUSES, 'all'))}")
    if code is not None and code not in CODES:
        raise Invalid("unknown_code", f"code must be one of {', '.join(CODES)}")
    if not 1 <= limit <= MAX_PAGE or offset < 0:
        raise Invalid("invalid_page", f"limit is 1..{MAX_PAGE} and offset >= 0")
    who = (assignee or "").strip() or None
    if who == "me":
        who = staff.cis_user_id
    text = (q or "").strip() or None
    pattern = None
    if text:
        pattern = "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    where = (
        "tc.deleted_at IS NULL AND tc.status = ANY($1::text[])"
        " AND ($2::text IS NULL OR $2 = 'all' OR k.status = $2)"
        " AND ($3::date IS NULL OR k.due_date <= $3)"
        " AND ($4::text IS NULL OR ($4 = 'none' AND k.assignee_ref IS NULL) OR k.assignee_ref = $4)"
        " AND ($5::text IS NULL OR k.code = $5)"
        " AND ($6::text IS NULL OR tc.code = upper($6) OR tc.id::text = $6)"
        " AND ($7::text IS NULL OR tc.code ILIKE $7 OR k.title ILIKE $7)"
    )
    args = [list(BOARD_STATUSES), status, due_before, who, code, (case or "").strip() or None, pattern]
    total = await conn.fetchval(f"SELECT count(*) {CHECK_FROM} WHERE {where}", *args)  # noqa: S608
    rows = await conn.fetch(
        f"SELECT {CHECK_COLUMNS} {CHECK_FROM} WHERE {where}"  # noqa: S608 — constants only
        " ORDER BY k.due_date NULLS LAST, tc.start_date NULLS LAST, tc.code, k.created_at, k.id"
        " LIMIT $8 OFFSET $9",
        *args,
        limit,
        offset,
    )
    return {"items": [check_out(r, staff) for r in rows], "total": int(total or 0), "limit": limit, "offset": offset}


# ── Writes ───────────────────────────────────────────────────────────────────


def _ensure_writable(case: Any) -> None:
    if case["status"] in CLOSED_CASE_STATUSES:
        raise Conflict("case_closed", f"trip case {case['code']} is {case['status']}: its checks are read-only")
    if case["status"] not in BOARD_STATUSES:
        raise Conflict(
            "case_not_sold", f"trip case {case['code']} is {case['status']}: checks start once the trip is confirmed"
        )


async def _assignable(conn: asyncpg.Connection, staff_key: str | None) -> None:
    if staff_key is None:
        return
    active = await conn.fetchval("SELECT active FROM booking.staff_users WHERE cis_user_id = $1", staff_key)
    if not active:
        raise Invalid("staff_not_assignable", "the staff user does not exist or is not active")


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        return value.strip() or None
    return value


def _audit_view(changes: dict[str, Any]) -> dict[str, Any]:
    return {k: ("(set)" if v is not None else None) if k in PRIVATE_FIELDS else v for k, v in changes.items()}


def _label(case_code: str, row: Any) -> str:
    return f"{case_code} {row['title'] if row['code'] == 'custom' else LABELS[row['code']]}"[:200]


async def create_custom(conn: asyncpg.Connection, staff: Staff, ref: str, data: dict[str, Any]) -> dict[str, Any]:
    """Add a custom check (title required) to a sold, open case."""
    case = await _case(conn, ref, lock=True)
    _ensure_writable(case)
    title = _clean(data.get("title"))
    if not title:
        raise Invalid("title_required", "say what to check")
    assignee = _clean(data.get("assignee_ref"))
    await _assignable(conn, assignee)
    note = _clean(data.get("note"))
    new_id = await conn.fetchval(
        "INSERT INTO booking.trip_case_checks (trip_case_id, code, title, due_date, assignee_ref, note, created_by,"
        " updated_by) VALUES ($1::uuid, 'custom', $2, $3, $4, $5, $6, $6) RETURNING id::text",
        case["id"],
        title[:200],
        data.get("due_date"),
        assignee,
        note,
        staff.cis_user_id,
    )
    row = await _get(conn, new_id)
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="trip_check.created",
        entity_type="trip_case_check",
        entity_id=new_id,
        after=_audit_view({"code": "custom", "due_date": data.get("due_date"), "assignee_ref": assignee, "note": note}),
        entity_label=_label(case["code"], row),
        summary=f"Added check '{title[:120]}' to {case['code']}",
    )
    return check_out(row, staff)


async def update(conn: asyncpg.Connection, staff: Staff, check_id: str, changes: dict[str, Any]) -> dict[str, Any]:
    """Change a check: status (done / not_applicable / open), assignee, due date, note, the title of a custom check,
    and the emergency contact of a B2C case. Returns the check with ``changed`` (field names)."""
    first = await _get(conn, check_id)
    case = await _case(conn, first["trip_case_id"], lock=True)
    row = await _get(conn, first["id"], lock=True)
    _ensure_writable(case)
    wanted = {k: _clean(v) for k, v in changes.items() if k in PATCH_FIELDS}
    if "status" in wanted and wanted["status"] not in STATUSES:
        raise Invalid("unknown_status", f"status must be one of {', '.join(STATUSES)}")
    if "title" in wanted:
        if row["code"] != "custom":
            raise Invalid("title_fixed", "a standard check keeps its title")
        if not wanted["title"]:
            raise Invalid("title_required", "say what to check")
        wanted["title"] = wanted["title"][:200]
    if any(f in wanted for f in CONTACT_FIELDS):
        if row["code"] != "emergency_contact":
            raise Invalid("contact_on_wrong_check", "the emergency contact belongs to the emergency contact check")
        if case["channel"] == "b2b":
            raise Invalid(
                "contact_not_allowed_b2b", "a B2B trip keeps no traveller data in Booking; the agency holds it"
            )
    changed = {k: v for k, v in wanted.items() if row.get(k) != v}
    if row["cleared_at"] is not None and any(changed.get(f) is not None for f in (*CONTACT_FIELDS, "note")):
        raise Conflict("check_cleared", "this trip ended; its emergency contact and notes were cleared for good")
    if not changed:
        return {**check_out(row, staff), "changed": []}
    if "assignee_ref" in changed:
        await _assignable(conn, changed["assignee_ref"])
    target = changed.get("status", row["status"])
    if target == "done" and row["code"] == "emergency_contact":
        name = changed.get("emergency_contact_name", row["emergency_contact_name"])
        phone = changed.get("emergency_contact_phone", row["emergency_contact_phone"])
        if not (name and phone):
            raise Invalid("emergency_contact_required", "enter the emergency contact's name and phone first")
    sets: dict[str, Any] = {k: v for k, v in changed.items() if k != "status"}
    if "status" in changed:
        sets["done_at"] = "now()" if target == "done" else None
        sets["done_by"] = staff.cis_user_id if target == "done" else None
        if row["status"] != "open" and target != "open":
            # done <-> not_applicable is two edges: reopen first (same transaction, one audit row).
            await _write(conn, staff, row, {"status": "open", "done_at": None, "done_by": None})
        sets["status"] = target
    await _write(conn, staff, row, sets)
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="trip_check.updated",
        entity_type="trip_case_check",
        entity_id=row["id"],
        before=_audit_view({k: row[k] for k in changed}),
        after=_audit_view(changed),
        entity_label=_label(case["code"], row),
        summary=_summary(case["code"], row, changed),
    )
    out = await _get(conn, row["id"])
    return {**check_out(out, staff), "changed": sorted(changed)}


def _summary(case_code: str, row: Any, changed: dict[str, Any]) -> str:
    what = row["title"] if row["code"] == "custom" else LABELS[row["code"]]
    if "status" in changed:
        verb = {"done": "Done", "not_applicable": "Not applicable", "open": "Reopened"}[changed["status"]]
        rest = sorted(set(changed) - {"status"})
        return f"{verb}: {what} ({case_code})" + (f"; also {', '.join(rest)}" if rest else "")
    return f"Edited {what} ({case_code}): {', '.join(sorted(changed))}"


async def _write(conn: asyncpg.Connection, staff: Staff, row: Any, sets: dict[str, Any]) -> None:
    parts, args = [], [row["id"], staff.cis_user_id]
    for col, value in sets.items():
        if value == "now()" and col == "done_at":
            parts.append("done_at = now()")
            continue
        args.append(value)
        parts.append(f"{col} = ${len(args)}")
    try:
        await conn.execute(
            f"UPDATE booking.trip_case_checks SET {', '.join(parts)}, updated_by = $2 WHERE id = $1::uuid",  # noqa: S608
            *args,
        )
    except asyncpg.CheckViolationError as exc:  # the guard and the CHECKs are the last word
        raise Conflict("invalid_trip_check", str(exc).split("\n")[0], status=row["status"]) from exc
