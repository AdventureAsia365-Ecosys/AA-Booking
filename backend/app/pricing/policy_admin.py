"""Pricing > Policy: the staff side of the pricing policy parameters (root ADR 0005b d15; AA-1091, bk-050).

A change of a parameter is a new **version** (``booking.pricing_policy``, append-only), never an edit. Its life is a
list of steps (``booking.pricing_policy_events``, append-only):

- ``draft``: written by its maker, no step yet; it prices nothing. Saving another draft of the same parameter
  withdraws the maker's previous draft (a draft is never edited in place);
- ``pending``: submitted by its maker (step ``submitted``); read-only, it prices nothing;
- ``published``: approved by a second person (step ``approved``, CHECK actor <> maker in the table); from its
  ``effective_from`` it is the value in force (``in_force``), before that ``scheduled``, after a newer one takes over
  ``superseded`` (derived, never stored);
- ``rejected``: refused by a second person, with a reason; ``withdrawn``: taken back by its maker (draft or pending).
  S280: another maker's **draft** can be discarded by a member of the parameter's approver group, with a reason (step
  ``rejected``, audit action ``pricing_policy.discarded``), so a forgotten draft never locks a parameter.

At most one open (draft or pending) version per parameter. The approver must hold the permission of the parameter's
group (:data:`app.pricing.policy_params.APPROVE_PERMISSION`: the pricing lead for prices, finance for FX, tax,
surcharges and payment terms, the administrator for data retention windows, S279) and never be its maker (403
``own_row``, and the table CHECK). A version takes effect on or after the day it is proposed and approved: approving one
whose ``effective_from`` has passed is 409 ``effective_date_passed`` (the maker proposes it again), so a change never
applies backwards. Every write is audited (``booking.audit_logs``, entity ``pricing_policy``).
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import asyncpg

from app import audit
from app.auth.staff import Staff
from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.pricing import policy_guides, policy_hints, policy_params, policy_values

MAX_REASON = 300

VERSIONS_SQL = """
SELECT p.id::text AS id, p.param_key, p.version_no, p.value::text AS value, p.unit, p.effective_from, p.reason,
       p.replaces_id::text AS replaces_id, p.diff::text AS diff, p.created_by, cs.name AS created_by_name,
       p.created_at,
       sub.created_at AS submitted_at,
       ap.actor AS approved_by, aps.name AS approved_by_name, ap.created_at AS approved_at,
       rj.actor AS rejected_by, rjs.name AS rejected_by_name, rj.created_at AS rejected_at,
       rj.reason AS reject_reason, wd.created_at AS withdrawn_at
FROM booking.pricing_policy p
LEFT JOIN booking.pricing_policy_events sub ON sub.policy_id = p.id AND sub.event = 'submitted'
LEFT JOIN booking.pricing_policy_events ap ON ap.policy_id = p.id AND ap.event = 'approved'
LEFT JOIN booking.pricing_policy_events rj ON rj.policy_id = p.id AND rj.event = 'rejected'
LEFT JOIN booking.pricing_policy_events wd ON wd.policy_id = p.id AND wd.event = 'withdrawn'
LEFT JOIN booking.staff_users cs ON cs.cis_user_id = p.created_by
LEFT JOIN booking.staff_users aps ON aps.cis_user_id = ap.actor
LEFT JOIN booking.staff_users rjs ON rjs.cis_user_id = rj.actor
WHERE ($1::text IS NULL OR p.param_key = $1) AND ($2::uuid IS NULL OR p.id = $2::uuid)
ORDER BY p.param_key, p.version_no DESC
"""

LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtext('booking.pricing_policy:' || $1))"
EVENT_SQL = (
    "INSERT INTO booking.pricing_policy_events (policy_id, maker, event, actor, reason)"
    " VALUES ($1::uuid, $2, $3, $4, $5)"
)


def _person(ref: str | None, name: str | None) -> dict[str, Any] | None:
    return None if ref is None else {"id": ref, "name": name}


def _base_status(row: Any) -> str:
    if row["withdrawn_at"] is not None:
        return "withdrawn"
    if row["rejected_at"] is not None:
        return "rejected"
    if row["approved_at"] is not None:
        return "published"
    if row["submitted_at"] is not None:
        return "pending"
    return "draft"


def _statuses(rows: list[Any], today: date) -> dict[str, str]:
    """Status per version id; published versions split into in_force / scheduled / superseded by ``today`` (the same
    ordering as :data:`app.pricing.policy_values.IN_FORCE_SQL`)."""
    out: dict[str, str] = {}
    live: dict[str, Any] = {}
    for r in rows:
        status = _base_status(r)
        out[r["id"]] = status
        if status == "published" and r["effective_from"] <= today:
            best = live.get(r["param_key"])
            rank = (r["effective_from"], r["approved_at"], r["version_no"])
            if best is None or rank > (best["effective_from"], best["approved_at"], best["version_no"]):
                live[r["param_key"]] = r
    for r in rows:
        if out[r["id"]] != "published":
            continue
        if r["effective_from"] > today:
            out[r["id"]] = "scheduled"
        else:
            out[r["id"]] = "in_force" if live.get(r["param_key"]) is r else "superseded"
    return out


def _can_approve(staff: Staff, param: policy_params.Param, row: Any) -> bool:
    return staff.can(policy_params.APPROVE_PERMISSION[param.group]) and row["created_by"] != staff.cis_user_id


def _version(row: Any, status: str, staff: Staff) -> dict[str, Any]:
    param = policy_params.PARAMS.get(row["param_key"])
    mine = row["created_by"] == staff.cis_user_id
    return {
        "id": row["id"],
        "key": row["param_key"],
        "version_no": row["version_no"],
        "value": policy_values.decode(row["value"]),
        "unit": row["unit"],
        "effective_from": row["effective_from"],
        "reason": row["reason"],
        "replaces_id": row["replaces_id"],
        "diff": policy_values.decode(row["diff"]) if row["diff"] else None,
        "status": status,
        "created_by": _person(row["created_by"], row["created_by_name"]),
        "created_at": row["created_at"],
        "submitted_at": row["submitted_at"],
        "approved_by": _person(row["approved_by"], row["approved_by_name"]),
        "approved_at": row["approved_at"],
        "rejected_by": _person(row["rejected_by"], row["rejected_by_name"]),
        "rejected_at": row["rejected_at"],
        "reject_reason": row["reject_reason"],
        "withdrawn_at": row["withdrawn_at"],
        # What the API would allow this caller (the UI only shows the buttons).
        "can_submit": status == "draft" and mine and staff.can("pricing_policy.write"),
        "can_withdraw": status in ("draft", "pending") and mine and staff.can("pricing_policy.write"),
        "can_approve": status == "pending" and param is not None and _can_approve(staff, param, row),
        # S280: another maker's draft blocks the parameter for everyone; a member of its approver group discards it.
        "can_discard": status == "draft" and param is not None and _can_approve(staff, param, row),
    }


async def _versions(
    conn: asyncpg.Connection, staff: Staff, today: date, *, key: str | None = None, version_id: str | None = None
) -> list[dict[str, Any]]:
    rows = list(await conn.fetch(VERSIONS_SQL, key, version_id))
    if version_id is not None and rows:
        # The status of one version depends on the other versions of its parameter.
        rows = list(await conn.fetch(VERSIONS_SQL, rows[0]["param_key"], None))
        statuses = _statuses(rows, today)
        return [_version(r, statuses[r["id"]], staff) for r in rows if r["id"] == version_id]
    statuses = _statuses(rows, today)
    return [_version(r, statuses[r["id"]], staff) for r in rows]


async def list_params(conn: asyncpg.Connection, staff: Staff, *, today: date) -> dict[str, Any]:
    """Every parameter: its definition, the version in force, the scheduled and open ones, and its history."""
    by_key: dict[str, list[dict[str, Any]]] = {}
    for v in await _versions(conn, staff, today):
        by_key.setdefault(v["key"], []).append(v)
    items = []
    for key, param in policy_params.PARAMS.items():
        versions = by_key.get(key, [])
        current = next((v for v in versions if v["status"] == "in_force"), None)
        items.append(
            {
                **policy_params.describe(param),
                # S280: the cross-parameter bound and the Vietnamese "not enforced yet" reason (policy_hints).
                **policy_hints.hints(param),
                # AA-1096 l23: the operator's guide (what / raise / lower / when / example, EN + VI).
                "guide": policy_guides.guide(key),
                "current": current,
                # In the order they take effect (the history is by version number, newest first).
                "scheduled": sorted(
                    (v for v in versions if v["status"] == "scheduled"),
                    key=lambda v: (v["effective_from"], v["version_no"]),
                ),
                "open": next((v for v in versions if v["status"] in ("draft", "pending")), None),
                "history": versions,
                "can_approve_group": staff.can(policy_params.APPROVE_PERMISSION[param.group]),
            }
        )
    pending = sum(1 for i in items if i["open"] and i["open"]["status"] == "pending")
    return {"items": items, "today": today, "pending": pending}


async def get_version(conn: asyncpg.Connection, staff: Staff, version_id: str, *, today: date) -> dict[str, Any]:
    found = await _versions(conn, staff, today, version_id=version_id)
    if not found:
        raise NotFound("policy_version_not_found", "no pricing policy version with this id")
    return found[0]


def _reason(text: str | None, code: str = "reason_required") -> str:
    reason = (text or "").strip()
    if not reason:
        raise Invalid(code, "a reason is required")
    if len(reason) > MAX_REASON:
        raise Invalid(code, f"the reason is at most {MAX_REASON} characters")
    return reason


def _label(key: str, version_no: int) -> str:
    return f"{key} v{version_no}"


def _show(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, sort_keys=True)


async def propose(
    conn: asyncpg.Connection,
    staff: Staff,
    *,
    key: str,
    value: Any,
    effective_from: date,
    reason: str | None,
    submit: bool,
    today: date,
) -> dict[str, Any]:
    """A new version of ``key`` (a draft, or pending when ``submit``). Call inside a transaction."""
    param = policy_params.PARAMS.get(key)
    if param is None:
        raise NotFound("policy_param_not_found", f"no pricing policy parameter {key}")
    why = _reason(reason)
    if effective_from < today:
        raise Invalid("effective_in_past", "a change takes effect today or later, never backwards")
    await conn.execute(LOCK_SQL, key)
    in_force = await policy_values.load(conn, effective_from)
    try:
        stored = policy_params.normalise(key, value, in_force.values)
    except policy_params.PolicyValueError as exc:
        raise Invalid("invalid_value", str(exc)) from None
    old = in_force.values.get(key)
    if old == stored:
        raise Invalid("no_change", f"{key} is already {_show(stored)} on {effective_from}")
    versions = await _versions(conn, staff, today, key=key)
    for v in versions:
        if v["status"] not in ("draft", "pending"):
            continue
        if v["status"] == "draft" and (v["created_by"] or {}).get("id") == staff.cis_user_id:
            # A draft is never edited in place: the maker's new draft replaces it.
            await conn.execute(EVENT_SQL, v["id"], staff.cis_user_id, "withdrawn", staff.cis_user_id, None)
            continue
        raise Conflict(
            "open_version",
            f"{key} already has a {v['status']} change (v{v['version_no']}); approve, reject or withdraw it first",
            open_status=v["status"],
            open_by=(v["created_by"] or {}).get("name"),
        )
    latest = versions[0] if versions else None
    version_no = (latest["version_no"] + 1) if latest else 1
    diff = {"from": old, "to": stored}
    try:
        version_id = await conn.fetchval(
            """
            INSERT INTO booking.pricing_policy
                (param_key, version_no, value, unit, effective_from, reason, replaces_id, diff, created_by)
            VALUES ($1, $2, $3::jsonb, $4, $5, $6, $7::uuid, $8::jsonb, $9)
            RETURNING id::text
            """,
            key,
            version_no,
            json.dumps(stored),
            param.unit,
            effective_from,
            why,
            latest["id"] if latest else None,
            json.dumps(diff),
            staff.cis_user_id,
        )
    except asyncpg.UniqueViolationError:  # a concurrent version of the same key (the lock makes this rare)
        raise Conflict("open_version", f"{key} changed meanwhile; reload and try again") from None
    if submit:
        await conn.execute(EVENT_SQL, version_id, staff.cis_user_id, "submitted", staff.cis_user_id, None)
    row = await get_version(conn, staff, version_id, today=today)
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="pricing_policy.submitted" if submit else "pricing_policy.drafted",
        entity_type="pricing_policy",
        entity_id=version_id,
        before={"value": old},
        after={"value": stored, "effective_from": effective_from.isoformat(), "reason": why},
        entity_label=_label(key, version_no),
        summary=f"{'Proposed' if submit else 'Drafted'} {param.label}: {_show(old)} -> {_show(stored)}"
        f" from {effective_from} ({'pending approval' if submit else 'draft'})",
    )
    return row


async def _version_for_step(conn: asyncpg.Connection, staff: Staff, version_id: str, today: date) -> dict[str, Any]:
    key = await conn.fetchval("SELECT param_key FROM booking.pricing_policy WHERE id = $1::uuid", version_id)
    if key is None:
        raise NotFound("policy_version_not_found", "no pricing policy version with this id")
    await conn.execute(LOCK_SQL, key)
    return await get_version(conn, staff, version_id, today=today)


async def _step(
    conn: asyncpg.Connection,
    staff: Staff,
    v: dict[str, Any],
    event: str,
    *,
    reason: str | None,
    action: str,
    summary: str,
    today: date,
) -> dict[str, Any]:
    maker = (v["created_by"] or {}).get("id")
    try:
        await conn.execute(EVENT_SQL, v["id"], maker, event, staff.cis_user_id, reason)
    except asyncpg.UniqueViolationError:
        raise Conflict("already_decided", "this version was decided meanwhile; reload") from None
    except asyncpg.CheckViolationError:  # the table's maker-checker CHECK, same rule as the checks above
        raise Forbidden("own_row", "maker-checker: a second person must decide a version you made") from None
    row = await get_version(conn, staff, v["id"], today=today)
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action=action,
        entity_type="pricing_policy",
        entity_id=v["id"],
        before={"status": v["status"]},
        after={"status": row["status"], "created_by": maker, **({"reason": reason} if reason else {})},
        entity_label=_label(v["key"], v["version_no"]),
        summary=summary,
    )
    return row


async def submit(conn: asyncpg.Connection, staff: Staff, version_id: str, *, today: date) -> dict[str, Any]:
    v = await _version_for_step(conn, staff, version_id, today)
    if (v["created_by"] or {}).get("id") != staff.cis_user_id:
        raise Forbidden("not_maker", "only the maker submits a draft")
    if v["status"] != "draft":
        raise Conflict("not_draft", f"this version is {v['status']}, not a draft")
    if v["effective_from"] < today:
        raise Conflict("effective_date_passed", "the effective day has passed; save a new draft with a later day")
    return await _step(
        conn, staff, v, "submitted", reason=None, action="pricing_policy.submitted",
        summary=f"Submitted {_label(v['key'], v['version_no'])} for approval", today=today,
    )  # fmt: skip


async def withdraw(conn: asyncpg.Connection, staff: Staff, version_id: str, *, today: date) -> dict[str, Any]:
    v = await _version_for_step(conn, staff, version_id, today)
    if (v["created_by"] or {}).get("id") != staff.cis_user_id:
        raise Forbidden("not_maker", "only the maker withdraws a draft or a pending change")
    if v["status"] not in ("draft", "pending"):
        raise Conflict("already_decided", f"this version is {v['status']}")
    return await _step(
        conn, staff, v, "withdrawn", reason=None, action="pricing_policy.withdrawn",
        summary=f"Withdrew {_label(v['key'], v['version_no'])}", today=today,
    )  # fmt: skip


def _checker(staff: Staff, v: dict[str, Any]) -> policy_params.Param:
    param = policy_params.PARAMS.get(v["key"])
    if param is None:
        raise Conflict("unknown_param", f"{v['key']} is no longer a known parameter")
    if not staff.can(policy_params.APPROVE_PERMISSION[param.group]):
        who = {policy_params.FINANCE: "finance", policy_params.ADMIN: "the administrator"}.get(
            param.group, "the pricing lead"
        )
        raise Forbidden("approver_group", f"a change of {param.label} is approved by {who}")
    if (v["created_by"] or {}).get("id") == staff.cis_user_id:
        raise Forbidden("own_row", "maker-checker: a second person must decide a version you made")
    if v["status"] != "pending":
        raise Conflict("not_pending", f"this version is {v['status']}, not pending")
    return param


async def approve(conn: asyncpg.Connection, staff: Staff, version_id: str, *, today: date) -> dict[str, Any]:
    v = await _version_for_step(conn, staff, version_id, today)
    param = _checker(staff, v)
    if v["effective_from"] < today:
        raise Conflict(
            "effective_date_passed",
            "the effective day has passed: approving now would apply it backwards; the maker proposes it again",
        )
    in_force = await policy_values.load(conn, v["effective_from"])
    try:  # the other values may have changed since the proposal (cross-parameter bounds)
        policy_params.normalise(v["key"], v["value"], in_force.values)
    except policy_params.PolicyValueError as exc:
        raise Conflict("no_longer_valid", str(exc)) from None
    return await _step(
        conn, staff, v, "approved", reason=None, action="pricing_policy.approved",
        summary=f"Approved {param.label}: {_show(in_force.values.get(v['key']))} -> {_show(v['value'])}"
        f" from {v['effective_from']}",
        today=today,
    )  # fmt: skip


async def reject(
    conn: asyncpg.Connection, staff: Staff, version_id: str, *, reason: str | None, today: date
) -> dict[str, Any]:
    why = _reason(reason)
    v = await _version_for_step(conn, staff, version_id, today)
    param = _checker(staff, v)
    return await _step(
        conn, staff, v, "rejected", reason=why, action="pricing_policy.rejected",
        summary=f"Rejected {param.label} v{v['version_no']}: {why}", today=today,
    )  # fmt: skip


async def discard(
    conn: asyncpg.Connection, staff: Staff, version_id: str, *, reason: str | None, today: date
) -> dict[str, Any]:
    """S280: another maker's draft, discarded by a member of the parameter's approver group, with a reason (step
    ``rejected``: the table CHECK wants a second person and a reason, which this is). A draft never expires, so without
    this a maker who never comes back would lock the parameter for everyone (``open_version``)."""
    why = _reason(reason)
    v = await _version_for_step(conn, staff, version_id, today)
    param = policy_params.PARAMS.get(v["key"])
    if param is None:
        raise Conflict("unknown_param", f"{v['key']} is no longer a known parameter")
    if v["status"] != "draft":
        raise Conflict("not_draft", f"this version is {v['status']}, not a draft")
    if (v["created_by"] or {}).get("id") == staff.cis_user_id:
        raise Forbidden("own_row", "withdraw your own draft instead")
    if not staff.can(policy_params.APPROVE_PERMISSION[param.group]):
        raise Forbidden("approver_group", f"a draft of {param.label} is discarded by its approver group")
    return await _step(
        conn, staff, v, "rejected", reason=why, action="pricing_policy.discarded",
        summary=f"Discarded the draft {_label(v['key'], v['version_no'])}: {why}", today=today,
    )  # fmt: skip
