"""Pending changes of a billing entity's bank details and default flag (AA-1158 part 1, bk-064).

``payment_instructions`` is the bank text printed on every agency invoice, and the default entity is the issuer of
every new invoice. One person with ``billing_config.write`` used to change both alone. Now a change of either is a
``booking.billing_entity_change`` row in ``pending``:

- requested by a holder of ``billing_config.write`` (:func:`request`; editing the entity with those fields calls it);
- approved or rejected by a second person with ``billing_config.approve`` (admin, finance; approve class, so a custom
  role never holds it), never the requester (403 ``maker_checker``), except the business owner, whose approval is
  flagged ``owner_self_approved`` (:mod:`app.auth.self_approval`; the database checks the marker too);
- withdrawn by the requester.

The approval applies the change in the same transaction: the old default loses the flag first (one default, partial
unique index), then the entity takes the new text and / or the flag. Until then invoices keep reading the old row
(:func:`app.agency_billing.config.default_entity` is unchanged). One pending change per entity (a partial unique
index): a second request is 409 ``change_pending``. Every step writes one audit row.
"""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg

from app import audit
from app.auth import self_approval
from app.auth.staff import Staff
from app.errors import Conflict, Forbidden, Invalid, NotFound

CHANGE_COLUMNS = (
    "c.id::text AS id, c.billing_entity_id::text AS billing_entity_id, c.set_payment_instructions,"
    " c.payment_instructions, c.make_default, c.reason, c.status, c.requested_by, c.requested_at, c.decided_by,"
    " c.decided_at, c.reject_reason, c.owner_self_approved"
)
ACTIONS = ("approve", "reject", "withdraw")
DEFAULT_REASON = "Requested on the billing settings page"


def _plain(row: Any) -> dict[str, Any]:
    return {k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in dict(row).items()}


def _reason(text: str | None, *, default: str | None = None) -> str:
    value = (text or "").strip() or (default or "")
    if not 1 <= len(value) <= 300:
        raise Invalid("reason_required", "give a reason of 1 to 300 characters")
    return value


async def _audit(conn: asyncpg.Connection, actor: str, action: str, change: dict[str, Any], label: str,
                 after: dict[str, Any]) -> None:  # fmt: skip
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=actor,
        action=action,
        entity_type="billing_entity",
        entity_id=change["billing_entity_id"],
        after={"change_id": change["id"], **after},
        entity_label=label[:80],
        summary=action.replace("billing_entity.", "Billing entity ").replace("_", " ")
        + (" (business-owner self-approval)" if after.get("owner_self_approved") else ""),
    )


async def request(
    conn: asyncpg.Connection,
    actor: str,
    entity_id: str,
    *,
    set_payment_instructions: bool,
    payment_instructions: str | None,
    make_default: bool,
    reason: str | None = None,
) -> dict[str, Any]:
    """Ask for a change of the bank text and / or the default flag (the caller holds ``billing_config.write``)."""
    if not set_payment_instructions and not make_default:
        raise Invalid("nothing_to_change", "ask for new payment details, the default flag, or both")
    text = (payment_instructions or "").strip() or None
    if text is not None and len(text) > 2000:
        raise Invalid("payment_instructions_too_long", "payment details are at most 2000 characters")
    entity = await conn.fetchrow(
        "SELECT id::text AS id, code, active, is_default, payment_instructions FROM booking.billing_entity"
        " WHERE id = $1::uuid FOR UPDATE",
        entity_id,
    )
    if entity is None:
        raise NotFound("billing_entity_not_found", "no billing entity with this id")
    if make_default and not entity["active"]:
        raise Invalid("default_must_be_active", "the default billing entity must be active")
    if make_default and entity["is_default"]:
        make_default = False
    if set_payment_instructions and text == entity["payment_instructions"]:
        set_payment_instructions, text = False, None
    if not set_payment_instructions and not make_default:
        raise Invalid("nothing_to_change", "the entity already has these payment details and default flag")
    try:
        row = await conn.fetchrow(
            "INSERT INTO booking.billing_entity_change AS c (billing_entity_id, set_payment_instructions,"  # noqa: S608
            " payment_instructions, make_default, reason, requested_by)"
            f" VALUES ($1::uuid, $2, $3, $4, $5, $6) RETURNING {CHANGE_COLUMNS}",
            entity_id,
            set_payment_instructions,
            text,
            make_default,
            _reason(reason, default=DEFAULT_REASON),
            actor,
        )
    except asyncpg.UniqueViolationError as exc:
        raise Conflict("change_pending", "this billing entity already has a pending change; decide it first") from exc
    change = _plain(row)
    await _audit(
        conn,
        actor,
        "billing_entity.change_requested",
        change,
        entity["code"],
        {
            "payment_instructions": "(set)" if set_payment_instructions else None,
            "make_default": make_default,
            "reason": change["reason"],
        },
    )
    return change


async def decide(
    conn: asyncpg.Connection, staff: Staff, change_id: str, action: str, reason: str | None = None
) -> dict[str, Any]:
    """``approve`` / ``reject`` (``billing_config.approve``, a second person or the business owner flagged) or
    ``withdraw`` (the requester). The approval applies the change to the entity in the same transaction."""
    if action not in ACTIONS:
        raise Invalid("unknown_action", f"action must be one of {', '.join(ACTIONS)}")
    if action != "withdraw" and not staff.can("billing_config.approve"):
        raise Forbidden("forbidden", "deciding a billing entity change needs billing_config.approve")
    async with conn.transaction():
        row = await conn.fetchrow(
            f"SELECT {CHANGE_COLUMNS} FROM booking.billing_entity_change c WHERE c.id = $1::uuid FOR UPDATE",  # noqa: S608
            change_id,
        )
        if row is None:
            raise NotFound("change_not_found", "no billing entity change with this id")
        change = _plain(row)
        if change["status"] != "pending":
            raise Conflict("change_decided", f"the change is already {change['status']}")
        entity = await conn.fetchrow(
            "SELECT id::text AS id, code, active FROM booking.billing_entity WHERE id = $1::uuid FOR UPDATE",
            change["billing_entity_id"],
        )
        status, columns, args, flagged = await _decision(conn, staff, change, entity, action, reason)
        try:
            decided = _plain(
                await conn.fetchrow(
                    f"UPDATE booking.billing_entity_change SET status = '{status}', decided_by = $2,"  # noqa: S608
                    f" decided_at = now(){columns} WHERE id = $1::uuid RETURNING {CHANGE_COLUMNS.replace('c.', '')}",
                    change_id,
                    staff.cis_user_id,
                    *args,
                )
            )
        except asyncpg.CheckViolationError as exc:  # the DB backstop of the maker-checker rule (bk-064)
            raise Forbidden("maker_checker", "a billing entity change is decided by a second person") from exc
        if action == "approve":
            await _apply(conn, staff.cis_user_id, change)
        after = {"status": status, "reason": decided["reject_reason"]} | self_approval.flag(flagged)
        await _audit(conn, staff.cis_user_id, f"billing_entity.change_{status}", change, entity["code"], after)
    return decided


async def _decision(
    conn: asyncpg.Connection, staff: Staff, change: dict[str, Any], entity: Any, action: str, reason: str | None
) -> tuple[str, str, tuple[Any, ...], bool]:
    """Who may take ``action`` on ``change``: (new status, extra SET text, its arguments from $3, the flag)."""
    mine = staff.cis_user_id == change["requested_by"]
    if action == "withdraw":
        if not mine:
            raise Forbidden("forbidden", "only the person who asked for the change withdraws it")
        return "withdrawn", "", (), False
    if action == "reject":
        if mine:
            raise Forbidden("maker_checker", "the person who asked for the change cannot reject it; withdraw it")
        return "rejected", ", reject_reason = $3", (_reason(reason),), False
    flagged = await self_approval.check(
        conn,
        staff,
        [change["requested_by"]],
        message="a billing entity change is approved by someone other than the person who asked for it",
    )
    if change["make_default"] and not entity["active"]:
        raise Conflict("default_must_be_active", "the entity was deactivated; reject the change")
    return "approved", ", owner_self_approved = $3", (flagged,), flagged


async def _apply(conn: asyncpg.Connection, actor: str, change: dict[str, Any]) -> None:
    if change["make_default"]:
        await conn.execute(
            "UPDATE booking.billing_entity SET is_default = false, updated_by = $2 WHERE is_default AND id <> $1::uuid",
            change["billing_entity_id"],
            actor,
        )
    await conn.execute(
        "UPDATE booking.billing_entity SET payment_instructions = CASE WHEN $2 THEN $3 ELSE payment_instructions END,"
        " is_default = is_default OR $4, updated_by = $5 WHERE id = $1::uuid",
        change["billing_entity_id"],
        change["set_payment_instructions"],
        change["payment_instructions"],
        change["make_default"],
        actor,
    )


async def pending_for(conn: asyncpg.Connection, entity_ids: list[str]) -> dict[str, dict[str, Any]]:
    """The pending change of each entity (none: absent), with the requester's name."""
    if not entity_ids:
        return {}
    rows = await conn.fetch(
        f"SELECT {CHANGE_COLUMNS}, s.name AS requested_by_name FROM booking.billing_entity_change c"  # noqa: S608
        " LEFT JOIN booking.staff_users s ON s.cis_user_id = c.requested_by"
        " WHERE c.status = 'pending' AND c.billing_entity_id = ANY($1::uuid[])",
        entity_ids,
    )
    return {r["billing_entity_id"]: _plain(r) for r in rows}


async def queue(conn: asyncpg.Connection, staff: Staff) -> dict[str, Any]:
    """AA-1158 My work: the pending changes ``staff`` may decide now (not requested by them, unless the business owner
    may self-approve) and the oldest."""
    args: list[Any] = []
    where = "c.status = 'pending'"
    if not staff.can("billing_config.approve"):
        where += " AND false"
    elif not await self_approval.may_self_approve(conn, staff):
        args.append(staff.cis_user_id)
        where += " AND c.requested_by <> $1"
    row = await conn.fetchrow(
        f"SELECT count(*)::int AS count, min(c.requested_at) AS oldest_at"  # noqa: S608
        f" FROM booking.billing_entity_change c WHERE {where}",
        *args,
    )
    return dict(row)
