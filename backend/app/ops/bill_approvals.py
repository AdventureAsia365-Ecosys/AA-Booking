"""Who may approve a supplier bill and who may record its payout (AA-1158 part 1, R3 / Q4 / Q5; split out of
:mod:`app.ops.supplier_bills`: file size limit).

- **Approve**: never the person who entered the bill or last changed its money facts (403 ``maker_checker``), except
  the business owner, flagged ``owner_self_approved`` (:mod:`app.auth.self_approval`).
- **Payout**: never the bill's creator or facts editor (bk-030, the business owner included); never its approver,
  except the business owner, flagged ``same_approver_payer`` (Q4). The checks are one list (:func:`payer_checks`).
- **Bank lock** (AA-1158 part 2, bk-067): no payout while the supplier's bank account change is pending, nor within
  ``supplier_bank_payout_lock_hours`` of its approval (:func:`app.suppliers.bank_accounts.check_payout`, 409; the
  database refuses it too). The bill detail carries ``payout_lock`` so the drawer explains it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import asyncpg

from app.auth import self_approval
from app.auth.staff import Staff
from app.errors import Forbidden, Unavailable
from app.suppliers import bank_accounts


@dataclass(frozen=True)
class OwnerPaths:
    """AA-1158: what the caller may do as the business owner (:mod:`app.auth.self_approval`), read once per request."""

    approve: bool = False  # approve a bill she entered or last changed (flagged owner_self_approved)
    pay: bool = False  # record the payout of a bill she approved (flagged same_approver_payer)


async def owner_paths(conn: asyncpg.Connection, staff: Staff) -> OwnerPaths:
    if not await self_approval.is_business_owner(conn, staff):
        return OwnerPaths()
    return OwnerPaths(
        approve=await self_approval.switch_on(conn, self_approval.APPROVE),
        pay=await self_approval.switch_on(conn, self_approval.PAYOUT),
    )


async def payer_checks(conn: asyncpg.Connection, staff: Staff, bill: Any) -> bool:
    """Who may record a payout of ``bill`` (AA-1158 R3 / Q4) and whether the supplier's bank lock allows one now
    (part 2). Returns ``same_approver_payer``: True when the business owner records the payout of a bill she
    approved."""
    if bill["created_by"] == staff.cis_user_id:
        raise Forbidden("maker_checker", f"you entered {bill['code']}: another admin records its payout")
    if bill["facts_edited_by"] == staff.cis_user_id:
        raise Forbidden(
            "maker_checker", f"you last changed the amounts of {bill['code']}: another admin records its payout"
        )
    same_approver = await self_approval.check(
        conn,
        staff,
        [bill["approved_by"]],
        kind=self_approval.PAYOUT,
        message=f"you approved {bill['code']}: another admin records its payout",
    )
    await bank_accounts.check_payout(conn, bill["supplier_id"])  # AA-1158 part 2: the bank change lock (409)
    return same_approver


async def approver(conn: asyncpg.Connection, staff: Staff, row: Any) -> bool:
    """The ``owner_self_approved`` value of approving ``row`` (403 ``maker_checker`` for another maker)."""
    entered = row["created_by"] == staff.cis_user_id
    return await self_approval.check(
        conn,
        staff,
        [row["created_by"], row["facts_edited_by"]],
        message=f"you {'entered' if entered else 'last changed the amounts of'} {row['code']}:"
        " another person approves it",
    )


async def with_payout_lock(conn: asyncpg.Connection, out: dict[str, Any], supplier_id: str) -> dict[str, Any]:
    """The bill detail with the supplier's bank lock (``payout_lock``); a pending or locked supplier takes ``pay``
    away (``pay_blocker``). A lock that cannot be read (no policy value) reads ``unknown`` and blocks too."""
    try:
        lock = await bank_accounts.payout_lock(conn, supplier_id)
    except Unavailable:
        lock = {"state": "unknown", "until": None}
    out["payout_lock"] = {**lock, "until": lock["until"].isoformat() if lock.get("until") else None}
    if lock["state"] in ("pending", "locked", "unknown"):
        out["actions"] = {**out["actions"], "pay": False, "pay_flagged": False}
        out["actions"]["pay_blocker"] = f"bank_{lock['state']}"
    return out
