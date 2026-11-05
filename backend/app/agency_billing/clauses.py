"""Partner terms clauses (AA-1037 R4 extras G6, bk-044 ``booking.partner_terms_clause``): AA's terms to agencies as
data rows, never legal text in code.

- **Edit** (``billing_config.write``: admin, finance): ``PUT /booking/partner-terms-clauses/{code}`` creates the
  clause or replaces its wording; every edit makes it a ``draft`` again (version + 1) and clears the approval.
- **Approve** (``terms_clause.approve``: admin, the business owner's role): the version the approver read
  (``version`` in the body; another version is 409 ``clause_changed``) by a person who is not its last editor (403
  ``maker_checker``; a CHECK backs it). The starting row (``price_error``, bk-044) is written by the migration, so the
  first approval by the business owner passes.
- Only **approved** clauses reach agencies (``booking.b2b.v1`` ``GET /terms``, :func:`approved`); drafts show in the
  staff admin with a draft badge. Every change writes one ``booking.audit_logs`` row.
"""

from __future__ import annotations

from typing import Any

import asyncpg

from app import audit
from app.errors import Conflict, Forbidden, NotFound

COLUMNS = (
    "id::text AS id, code, position, heading AS title, body, free_cancel_days, status, version, created_at, updated_at,"
    " updated_by, approved_by, approved_at"
)
EDIT_FIELDS = ("title", "body", "free_cancel_days", "position")


async def list_clauses(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    rows = await conn.fetch(f"SELECT {COLUMNS} FROM booking.partner_terms_clause ORDER BY position, code")  # noqa: S608
    return [dict(r) for r in rows]


async def approved(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    """The approved clauses, in display order (what agencies see)."""
    rows = await conn.fetch(
        "SELECT code, heading AS title, body, free_cancel_days, version, approved_at FROM booking.partner_terms_clause"
        " WHERE status = 'approved' ORDER BY position, code"
    )
    return [dict(r) for r in rows]


def _snapshot(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    return {k: row[k] for k in ("title", "body", "free_cancel_days", "position", "status", "version")}


async def put_clause(conn: asyncpg.Connection, code: str, body: dict[str, Any], actor: str) -> dict[str, Any]:
    """Create or reword a clause; it is a draft (version + 1) until a second person approves it."""
    async with conn.transaction():
        before = await conn.fetchrow(
            f"SELECT {COLUMNS} FROM booking.partner_terms_clause WHERE code = $1 FOR UPDATE",  # noqa: S608
            code,
        )
        values = [body.get(k) for k in EDIT_FIELDS]
        if values[3] is None:
            values[3] = before["position"] if before is not None else 100
        if before is None:
            row = await conn.fetchrow(
                "INSERT INTO booking.partner_terms_clause (code, heading, body, free_cancel_days, position, updated_by)"  # noqa: S608
                f" VALUES ($1, $2, $3, $4, $5, $6) RETURNING {COLUMNS}",
                code,
                *values,
                actor,
            )
        else:
            row = await conn.fetchrow(
                "UPDATE booking.partner_terms_clause SET heading = $2, body = $3, free_cancel_days = $4, position = $5,"  # noqa: S608
                " status = 'draft', approved_by = NULL, approved_at = NULL, version = version + 1, updated_by = $6,"
                f" updated_at = now() WHERE code = $1 RETURNING {COLUMNS}",
                code,
                *values,
                actor,
            )
        out = dict(row)
        await audit.record(
            conn,
            actor_type="staff",
            actor_ref=actor,
            action="terms_clause.created" if before is None else "terms_clause.updated",
            entity_type="terms_clause",
            entity_id=out["id"],
            before=_snapshot(before),
            after=_snapshot(row),
            entity_label=f"clause {code} v{out['version']}",
            summary=f"Terms clause {code} v{out['version']} saved as a draft (waits for approval)",
        )
    return out


async def approve_clause(conn: asyncpg.Connection, code: str, version: int, actor: str) -> dict[str, Any]:
    """Approve the draft version the approver read. 404 unknown, 409 ``clause_changed`` (another version) or
    ``clause_approved`` (nothing to approve), 403 ``maker_checker`` (the approver is its last editor)."""
    async with conn.transaction():
        before = await conn.fetchrow(
            f"SELECT {COLUMNS} FROM booking.partner_terms_clause WHERE code = $1 FOR UPDATE",  # noqa: S608
            code,
        )
        if before is None:
            raise NotFound("clause_not_found", "no terms clause with this code")
        if before["version"] != version:
            raise Conflict("clause_changed", f"the clause is at version {before['version']}; read it again")
        if before["status"] == "approved":
            raise Conflict("clause_approved", "this version is already approved")
        if before["updated_by"] == actor:
            raise Forbidden("maker_checker", "a second person approves a clause, never its last editor")
        row = await conn.fetchrow(
            "UPDATE booking.partner_terms_clause SET status = 'approved', approved_by = $2, approved_at = now()"  # noqa: S608
            f" WHERE code = $1 RETURNING {COLUMNS}",
            code,
            actor,
        )
        out = dict(row)
        await audit.record(
            conn,
            actor_type="staff",
            actor_ref=actor,
            action="terms_clause.approved",
            entity_type="terms_clause",
            entity_id=out["id"],
            before={"status": "draft", "version": version},
            after={"status": "approved", "version": version, "edited_by": before["updated_by"]},
            entity_label=f"clause {code} v{version}",
            summary=f"Approved terms clause {code} v{version}; agencies now see it",
        )
    return out
