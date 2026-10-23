"""Staff admin of B2B partners (``booking.b2b_partners``, bk-019, AA-972): list, register, edit, suspend.

A partner row lets an agency's calls through ``booking.b2b.v1``: the CRM sends the agency's CIS tenant id in
``X-AA-Tenant-Id`` and Booking checks that an **active** row exists for it and that the token's client is the row's
``m2m_client_id``. Rows are never deleted (role ``booking`` has no DELETE): ``status = suspended`` stops the agency's
calls at once (403 ``unknown_partner``). Every write adds one ``booking.audit_logs`` row (``entity_type =
'b2b_partner'``). Permission ``b2b.admin`` (Booking admin only).
"""

from __future__ import annotations

from typing import Any

import asyncpg

from app import audit
from app.auth.staff import Staff
from app.errors import Conflict, Invalid, NotFound

EDITABLE = ("agency_name", "status", "billing_currency", "credit_terms_ref", "m2m_client_id")
COLUMNS = """
    id::text AS id, tenant_id::text AS tenant_id, agency_name, status, billing_currency::text AS billing_currency,
    credit_terms_ref, m2m_client_id, created_by, updated_by, created_at, updated_at
"""


async def list_partners(conn: asyncpg.Connection) -> dict[str, Any]:
    rows = await conn.fetch(f"SELECT {COLUMNS} FROM booking.b2b_partners ORDER BY lower(agency_name), id")  # noqa: S608
    return {"items": [dict(r) for r in rows]}


async def get_partner(conn: asyncpg.Connection, partner_id: str, *, lock: bool = False) -> dict[str, Any]:
    row = await conn.fetchrow(
        f"SELECT {COLUMNS} FROM booking.b2b_partners WHERE id = $1::uuid{' FOR UPDATE' if lock else ''}",  # noqa: S608
        partner_id,
    )
    if row is None:
        raise NotFound("partner_not_found", f"B2B partner {partner_id} not found")
    return dict(row)


def _clean(data: dict[str, Any]) -> dict[str, Any]:
    out = dict(data)
    for key in ("agency_name", "credit_terms_ref", "m2m_client_id"):
        if isinstance(out.get(key), str):
            out[key] = out[key].strip() or None
    return out


async def _audit(
    conn: asyncpg.Connection, staff: Staff, action: str, row: dict[str, Any], before: dict | None, after: dict
) -> None:
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action=action,
        entity_type="b2b_partner",
        entity_id=row["id"],
        before=before,
        after=after,
        entity_label=str(row["agency_name"])[:80],
        summary=action.replace("b2b_partner.", "B2B partner ").replace("_", " "),
    )


async def create_partner(conn: asyncpg.Connection, staff: Staff, data: dict[str, Any]) -> dict[str, Any]:
    data = _clean(data)
    try:
        row = await conn.fetchrow(
            f"""
            INSERT INTO booking.b2b_partners
                (tenant_id, agency_name, status, billing_currency, credit_terms_ref, m2m_client_id, created_by,
                 updated_by)
            VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $7)
            RETURNING {COLUMNS}
            """,  # noqa: S608 — constants
            data["tenant_id"],
            data["agency_name"],
            data.get("status") or "active",
            data.get("billing_currency") or "USD",
            data.get("credit_terms_ref"),
            data["m2m_client_id"],
            staff.cis_user_id,
        )
    except asyncpg.UniqueViolationError as exc:
        raise Conflict("tenant_taken", "this tenant already has a B2B partner row") from exc
    except asyncpg.CheckViolationError as exc:
        raise Invalid("invalid_value", exc.constraint_name or "check violation") from exc
    partner = dict(row)
    await _audit(conn, staff, "b2b_partner.created", partner, None, {k: partner[k] for k in ("tenant_id", *EDITABLE)})
    return partner


async def update_partner(
    conn: asyncpg.Connection, staff: Staff, partner_id: str, changes: dict[str, Any]
) -> dict[str, Any]:
    changes = _clean({k: v for k, v in changes.items() if k in EDITABLE})
    before = await get_partner(conn, partner_id, lock=True)
    changed = {k: v for k, v in changes.items() if before[k] != v}
    if not changed:
        return before
    sets = ", ".join(f"{k} = ${i}" for i, k in enumerate(changed, start=2))
    try:
        row = await conn.fetchrow(
            f"UPDATE booking.b2b_partners SET {sets}, updated_by = ${len(changed) + 2}"  # noqa: S608 — keys from EDITABLE
            f" WHERE id = $1::uuid RETURNING {COLUMNS}",
            partner_id,
            *changed.values(),
            staff.cis_user_id,
        )
    except asyncpg.CheckViolationError as exc:
        raise Invalid("invalid_value", exc.constraint_name or "check violation") from exc
    except asyncpg.NotNullViolationError as exc:
        raise Invalid("invalid_value", f"{exc.column_name or 'field'} is required") from exc
    partner = dict(row)
    if set(changed) == {"status"}:
        action = "b2b_partner.suspended" if changed["status"] == "suspended" else "b2b_partner.reactivated"
    else:
        action = "b2b_partner.updated"
    await _audit(conn, staff, action, partner, {k: before[k] for k in changed}, changed)
    return partner
