"""Supplier bank accounts (AA-1158 part 2, bk-067; tech lead S285 A1, Q5, Q10).

Where AA sends a supplier's money is the main payout fraud path, so a bank account is never a plain field:

- **Enter** (``supplier_bank.write``: admin, contracting, finance and the content + operations role): a new account
  starts ``pending`` (one pending per supplier, 409 ``bank_change_pending``). The details (holder, account number /
  IBAN, SWIFT / BIC, bank code, bank address) are encrypted at once (:mod:`app.suppliers.bank_crypto`, a KMS envelope
  only the Booking task role can open); the row keeps the bank name, country, currency and the last 4 in clear.
- **Approve** (``supplier_bank.approve``: admin) after a **call-back**: the approver calls the supplier on a number
  AA already knew (not one given with the change) and writes who confirmed it (``callback_note``, required). Never the
  person who entered it (403 ``maker_checker``), except the business owner, flagged ``owner_self_approved``
  (:mod:`app.auth.self_approval`; the database checks the CIS marker again). The previous approved account becomes
  ``superseded`` in the same transaction. **Reject** (``supplier_bank.approve``, not the person who entered it, a
  reason) or **withdraw** (the person who entered it, a reason).
- **Payout lock**: no payout to the supplier while one of its accounts is pending, nor for
  ``supplier_bank_payout_lock_hours`` (Pricing > Policy, 24 by default) after the newest approval: 409
  ``bank_account_pending`` / ``bank_payout_locked`` (:func:`check_payout`, called by the payout checks); the database
  refuses the same (``booking.supplier_payouts_bank_lock``). A supplier with no account on file is not locked.
- **Read**: every reader of the supplier sees the masked list (bank, ``•••• 1234``, who and when); the full details
  only with ``supplier_bank.read`` (admin, finance), one KMS Decrypt per request, every read audited
  (``supplier_bank_account.revealed``, never the details themselves).

Every step writes one audit row on the supplier (it shows in the supplier's activity); the audit never holds a detail
beyond the last 4. Lock order: the supplier row, then the account (a payout share-locks the supplier).
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta
from typing import Any

import asyncpg

from app import audit
from app.auth import self_approval
from app.auth.staff import Staff
from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.pricing import policy_values
from app.pricing.policy_params_security import BANK_LOCK_HOURS
from app.suppliers import bank_crypto

ACTIONS = ("approve", "reject", "withdraw")
MASK = "••••"
CALLBACK_MIN = 10  # technical-constant: a call-back note says who confirmed on which number, never a word or two
ACCOUNT_RE = re.compile(r"^[A-Z0-9]{4,34}$")
BIC_RE = re.compile(r"^[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?$")
COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
CURRENCY_RE = re.compile(r"^[A-Z]{3}$")

COLUMNS = """
    a.id::text AS id, a.supplier_id::text AS supplier_id, a.status, a.bank_name, a.bank_country::text AS bank_country,
    a.currency::text AS currency, a.account_last4, a.reason, a.entered_by, en.name AS entered_by_name, a.entered_at,
    a.approved_by, ap.name AS approved_by_name, a.approved_at, a.callback_note, a.owner_self_approved,
    a.closed_by, cl.name AS closed_by_name, a.closed_at, a.close_reason
"""
FROM = """
    booking.supplier_bank_account a
    LEFT JOIN booking.staff_users en ON en.cis_user_id = a.entered_by
    LEFT JOIN booking.staff_users ap ON ap.cis_user_id = a.approved_by
    LEFT JOIN booking.staff_users cl ON cl.cis_user_id = a.closed_by
"""
ACCOUNT_SQL = (
    "SELECT id::text AS id, supplier_id::text AS supplier_id, status, entered_by, account_last4, bank_name"
    " FROM booking.supplier_bank_account WHERE id = $1::uuid"
)
LOCK_SQL = """
SELECT coalesce(bool_or(a.status = 'pending'), false) AS pending,
       max(a.approved_at) FILTER (WHERE a.status = 'approved') AS last_approved, now() AS now
  FROM booking.supplier_bank_account a
 WHERE a.supplier_id = $1::uuid
"""


# ── Input ────────────────────────────────────────────────────────────────────


def _text(body: dict[str, Any], key: str, limit: int, *, required: bool = False) -> str | None:
    value = body.get(key)
    text = str(value).strip() if value is not None else ""
    if not text:
        if required:
            raise Invalid(f"{key}_required", f"{key.replace('_', ' ')} is required")
        return None
    if len(text) > limit:
        raise Invalid(f"{key}_too_long", f"{key.replace('_', ' ')} is at most {limit} characters")
    return text


def _code(body: dict[str, Any], key: str, pattern: re.Pattern[str], *, required: bool = False) -> str | None:
    raw = _text(body, key, 64, required=required)
    if raw is None:
        return None
    value = re.sub(r"[\s-]", "", raw).upper()
    if not pattern.fullmatch(value):
        raise Invalid(f"invalid_{key}", f"{key.replace('_', ' ')} is not valid")
    return value


def clean(body: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """(the encrypted details, the clear columns) of an entry; 422 on a bad field. The details never reach a log."""
    details = {
        "account_holder": _text(body, "account_holder", 140, required=True),
        "account_number": _code(body, "account_number", ACCOUNT_RE, required=True),
        "swift_bic": _code(body, "swift_bic", BIC_RE),
        "bank_code": _text(body, "bank_code", 34),
        "bank_address": _text(body, "bank_address", 300),
    }
    clear = {
        "bank_name": _text(body, "bank_name", 120, required=True),
        "bank_country": _code(body, "bank_country", COUNTRY_RE),
        "currency": _code(body, "currency", CURRENCY_RE, required=True),
        "reason": _text(body, "reason", 300, required=True),
    }
    clear["account_last4"] = details["account_number"][-4:]
    return details, clear


# ── Rows ─────────────────────────────────────────────────────────────────────


def _person(ref: str | None, name: str | None) -> dict[str, Any] | None:
    return {"id": ref, "name": name or ref} if ref else None


def masked(last4: str) -> str:
    return f"{MASK} {last4}"


def row_out(row: Any, staff: Staff) -> dict[str, Any]:
    out = dict(row)
    entered_by = out["entered_by"]
    for who in ("entered_by", "approved_by", "closed_by"):
        out[who] = _person(out[who], out.pop(f"{who}_name"))
    out["account_masked"] = masked(out["account_last4"])
    pending, mine = out["status"] == "pending", staff.cis_user_id == entered_by
    out["actions"] = {
        "reveal": staff.can("supplier_bank.read") and out["status"] != "withdrawn",
        "approve": pending and staff.can("supplier_bank.approve"),  # the write re-checks the maker rule
        "approve_self": pending and mine and staff.can("supplier_bank.approve"),
        "reject": pending and staff.can("supplier_bank.approve") and not mine,
        "withdraw": pending and mine and staff.can("supplier_bank.write"),
    }
    return out


async def _supplier(conn: asyncpg.Connection, ref: str, *, lock: bool = False) -> dict[str, Any]:
    try:
        where, arg = "id = $1::uuid", str(uuid.UUID(ref))
    except ValueError:
        where, arg = "code = $1", ref.strip().upper()
    row = await conn.fetchrow(
        f"SELECT id::text AS id, code, company_name FROM booking.suppliers WHERE {where}"  # noqa: S608 — fixed text
        + (" FOR UPDATE" if lock else ""),
        arg,
    )
    if row is None:
        raise NotFound("supplier_not_found", f"supplier {ref} not found")
    return dict(row)


async def _account(conn: asyncpg.Connection, account_id: str, *, lock: bool = False) -> dict[str, Any]:
    try:
        key = str(uuid.UUID(account_id))
    except ValueError as exc:
        raise NotFound("bank_account_not_found", "no supplier bank account with this id") from exc
    sql = ACCOUNT_SQL + (" FOR UPDATE" if lock else "")
    row = await conn.fetchrow(sql, key)
    if row is None:
        raise NotFound("bank_account_not_found", "no supplier bank account with this id")
    return dict(row)


async def _audit(
    conn: asyncpg.Connection, staff: Staff, supplier: dict[str, Any], action: str, summary: str, after: dict[str, Any]
) -> None:
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action=f"supplier_bank_account.{action}",
        entity_type="supplier",
        entity_id=supplier["id"],
        after=after,
        entity_label=f"{supplier['code']} {supplier['company_name']}"[:80],
        summary=summary,
    )


# ── The payout lock ──────────────────────────────────────────────────────────


async def payout_lock(conn: asyncpg.Connection, supplier_id: str, *, now: datetime | None = None) -> dict[str, Any]:
    """``state``: ``none`` (no account on file), ``pending`` (a change waits), ``locked`` (approved less than the policy
    hours ago; ``until``) or ``open``. ``now`` overrides the database clock (tests)."""
    row = await conn.fetchrow(LOCK_SQL, supplier_id)
    if row is None:  # an aggregate always answers one row; a stub connection may not
        return {"state": "none", "until": None}
    if row["pending"]:
        return {"state": "pending", "until": None}
    if row["last_approved"] is None:
        return {"state": "none", "until": None}
    today = await policy_values.today_utc(conn)
    hours = (await policy_values.require_staff(conn, today, BANK_LOCK_HOURS)).integer(BANK_LOCK_HOURS)
    until = row["last_approved"] + timedelta(hours=hours)
    current = now or row["now"]
    return {"state": "locked" if current < until else "open", "until": until, "hours": hours}


async def check_payout(conn: asyncpg.Connection, supplier_id: str, *, now: datetime | None = None) -> None:
    """Refuse a payout to ``supplier_id`` while a bank change is pending or within the lock (409)."""
    lock = await payout_lock(conn, supplier_id, now=now)
    if lock["state"] == "pending":
        raise Conflict(
            "bank_account_pending",
            "this supplier's bank account change waits for approval: no payout until it is decided",
        )
    if lock["state"] == "locked":
        raise Conflict(
            "bank_payout_locked",
            f"this supplier's bank account was approved less than {lock['hours']} hours ago: payouts open at"
            f" {lock['until'].isoformat()}",
            until=lock["until"].isoformat(),
        )


# ── Approval queue (My work, the supplier list filter) ──────────────────────

# The supplier ({p} = the caller) has a pending bank account the caller did not enter (one pending per supplier).
PENDING_NOT_MINE = (
    "EXISTS (SELECT 1 FROM booking.supplier_bank_account a WHERE a.supplier_id = s.id AND a.status = 'pending'"
    " AND a.entered_by <> {p})"
)
PENDING_ANY = (
    "EXISTS (SELECT 1 FROM booking.supplier_bank_account a WHERE a.supplier_id = s.id AND a.status = 'pending')"
)


async def approvable_filter(conn: asyncpg.Connection, staff: Staff, where: list[str], add: Any) -> None:
    """``bank=pending&approvable=me``: suppliers whose pending bank account the caller may approve now (not entered by
    them unless they may self-approve; none without ``supplier_bank.approve``)."""
    if not staff.can("supplier_bank.approve"):
        where.append("false")
    elif await self_approval.may_self_approve(conn, staff):
        where.append(PENDING_ANY)
    else:
        add(PENDING_NOT_MINE, staff.cis_user_id)


async def queue(conn: asyncpg.Connection, staff: Staff) -> dict[str, Any]:
    """AA-1158 part 2 My work: the pending bank accounts ``staff`` may approve now (the list filter above) and the
    oldest."""
    args: list[Any] = []
    where = "a.status = 'pending'"
    if not staff.can("supplier_bank.approve"):
        where += " AND false"
    elif not await self_approval.may_self_approve(conn, staff):
        args.append(staff.cis_user_id)
        where += " AND a.entered_by <> $1"
    row = await conn.fetchrow(
        f"SELECT count(*)::int AS count, min(a.entered_at) AS oldest_at FROM booking.supplier_bank_account a"  # noqa: S608
        f" WHERE {where}",
        *args,
    )
    return dict(row) if row else {"count": 0, "oldest_at": None}


# ── Reads ────────────────────────────────────────────────────────────────────


async def list_for(conn: asyncpg.Connection, staff: Staff, supplier_ref: str) -> dict[str, Any]:
    """The supplier's accounts, newest first, masked; the payout lock; what the caller may do."""
    supplier = await _supplier(conn, supplier_ref)
    rows = await conn.fetch(
        f"SELECT {COLUMNS} FROM {FROM} WHERE a.supplier_id = $1::uuid ORDER BY a.entered_at DESC, a.id",  # noqa: S608
        supplier["id"],
    )
    lock = await payout_lock(conn, supplier["id"])
    return {
        "supplier": supplier,
        "items": [row_out(r, staff) for r in rows],
        "payout_lock": {**lock, "until": lock["until"].isoformat() if lock["until"] else None},
        "actions": {"enter": staff.can("supplier_bank.write") and not any(r["status"] == "pending" for r in rows)},
        "can_read_details": staff.can("supplier_bank.read"),
    }


async def reveal(
    conn: asyncpg.Connection, staff: Staff, provider: bank_crypto.KeyProvider | None, account_id: str
) -> dict[str, Any]:
    """The full details of one account (``supplier_bank.read``), audited."""
    if not staff.can("supplier_bank.read"):
        raise Forbidden("forbidden", "reading a supplier's bank details needs supplier_bank.read")
    key_provider = bank_crypto.require(provider)
    row = await conn.fetchrow(
        "SELECT id::text AS id, supplier_id::text AS supplier_id, account_last4, ciphertext, wrapped_key"
        " FROM booking.supplier_bank_account WHERE id = $1::uuid",
        (await _account(conn, account_id))["id"],
    )
    try:
        details = await bank_crypto.open_sealed(
            key_provider, row["supplier_id"], row["id"], row["ciphertext"], row["wrapped_key"]
        )
    except bank_crypto.BankCryptoError as exc:
        raise Conflict("bank_details_unreadable", "the bank details could not be decrypted; enter them again") from exc
    supplier = await _supplier(conn, row["supplier_id"])
    await _audit(conn, staff, supplier, "revealed", f"Viewed bank account {masked(row['account_last4'])}",
                 {"bank_account_id": row["id"], "account_last4": row["account_last4"]})  # fmt: skip
    return {"id": row["id"], **details}


# ── Writes ───────────────────────────────────────────────────────────────────


async def enter(
    conn: asyncpg.Connection,
    staff: Staff,
    provider: bank_crypto.KeyProvider | None,
    supplier_ref: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    """A new account for the supplier, pending until a second person approves it after a call-back."""
    if not staff.can("supplier_bank.write"):
        raise Forbidden("forbidden", "entering a supplier bank account needs supplier_bank.write")
    details, clear = clean(body)
    key_provider = bank_crypto.require(provider)
    supplier = await _supplier(conn, supplier_ref, lock=True)
    if await conn.fetchval(
        "SELECT 1 FROM booking.supplier_bank_account WHERE supplier_id = $1::uuid AND status = 'pending'",
        supplier["id"],
    ):
        raise Conflict("bank_change_pending", "this supplier already has a bank account change waiting for approval")
    row_id = str(uuid.uuid4())
    try:
        sealed = await bank_crypto.seal(key_provider, supplier["id"], row_id, details)
    except bank_crypto.BankCryptoError as exc:
        raise Conflict("bank_crypto_failed", "the bank details could not be encrypted; try again") from exc
    try:
        await conn.execute(
            "INSERT INTO booking.supplier_bank_account (id, supplier_id, bank_name, bank_country, currency,"
            " account_last4, ciphertext, wrapped_key, kms_key_id, enc_alg, reason, entered_by)"
            " VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)",
            row_id,
            supplier["id"],
            clear["bank_name"],
            clear["bank_country"],
            clear["currency"],
            clear["account_last4"],
            sealed.ciphertext,
            sealed.wrapped_key,
            sealed.key_id,
            bank_crypto.ALG,
            clear["reason"],
            staff.cis_user_id,
        )
    except asyncpg.UniqueViolationError as exc:
        raise Conflict("bank_change_pending", "this supplier already has a bank account change waiting") from exc
    await _audit(
        conn,
        staff,
        supplier,
        "entered",
        f"Entered bank account {masked(clear['account_last4'])} at {clear['bank_name']} (pending approval)",
        {"bank_account_id": row_id, **{k: clear[k] for k in ("bank_name", "currency", "account_last4", "reason")}},
    )
    return await _one(conn, staff, row_id)


async def _one(conn: asyncpg.Connection, staff: Staff, account_id: str) -> dict[str, Any]:
    row = await conn.fetchrow(f"SELECT {COLUMNS} FROM {FROM} WHERE a.id = $1::uuid", account_id)  # noqa: S608
    return row_out(row, staff)


def _note(text: str | None, code: str, minimum: int = 1, limit: int = 300) -> str:
    value = (text or "").strip()
    if not minimum <= len(value) <= limit:
        raise Invalid(code, f"write {minimum} to {limit} characters")
    return value


async def decide(
    conn: asyncpg.Connection, staff: Staff, account_id: str, action: str, note: str | None = None
) -> dict[str, Any]:
    """``approve`` (``supplier_bank.approve`` + a call-back note), ``reject`` (``supplier_bank.approve``, not the
    person who entered it) or ``withdraw`` (the person who entered it). One transaction."""
    if action not in ACTIONS:
        raise Invalid("unknown_action", f"action must be one of {', '.join(ACTIONS)}")
    needs = "supplier_bank.write" if action == "withdraw" else "supplier_bank.approve"
    if not staff.can(needs):
        raise Forbidden("forbidden", f"this decision needs {needs}")
    async with conn.transaction():
        first = await _account(conn, account_id)
        supplier = await _supplier(conn, first["supplier_id"], lock=True)  # the supplier first, as a payout does
        account = await _account(conn, account_id, lock=True)
        if account["status"] != "pending":
            raise Conflict("bank_account_decided", f"this bank account is already {account['status']}")
        mine = account["entered_by"] == staff.cis_user_id
        if action == "withdraw":
            await _close(conn, staff, supplier, account, "withdrawn", _note(note, "reason_required"), mine=mine)
        elif action == "reject":
            await _close(conn, staff, supplier, account, "rejected", _note(note, "reason_required"), mine=mine)
        else:
            await _approve(conn, staff, supplier, account, _note(note, "callback_note_required", CALLBACK_MIN, 500))
    return await _one(conn, staff, account["id"])


async def _close(
    conn: asyncpg.Connection,
    staff: Staff,
    supplier: dict[str, Any],
    account: dict[str, Any],
    status: str,
    reason: str,
    *,
    mine: bool,
) -> None:
    if status == "withdrawn" and not mine:
        raise Forbidden("forbidden", "only the person who entered the bank account withdraws it")
    if status == "rejected" and mine:
        raise Forbidden("maker_checker", "you entered this bank account: withdraw it, another person rejects it")
    await conn.execute(
        f"UPDATE booking.supplier_bank_account SET status = '{status}', closed_by = $2, closed_at = now(),"  # noqa: S608
        " close_reason = $3 WHERE id = $1::uuid",
        account["id"],
        staff.cis_user_id,
        reason,
    )
    await _audit(conn, staff, supplier, status, f"Bank account {masked(account['account_last4'])} {status}",
                 {"bank_account_id": account["id"], "status": status, "reason": reason})  # fmt: skip


async def _approve(
    conn: asyncpg.Connection, staff: Staff, supplier: dict[str, Any], account: dict[str, Any], callback: str
) -> None:
    flagged = await self_approval.check(
        conn,
        staff,
        [account["entered_by"]],
        message="you entered this bank account: another person approves it after a call-back",
    )
    old = await conn.fetchval(
        "UPDATE booking.supplier_bank_account SET status = 'superseded', closed_by = $2, closed_at = now()"
        " WHERE supplier_id = $1::uuid AND status = 'approved' RETURNING account_last4",
        supplier["id"],
        staff.cis_user_id,
    )
    try:
        await conn.execute(
            "UPDATE booking.supplier_bank_account SET status = 'approved', approved_by = $2, approved_at = now(),"
            " callback_note = $3, owner_self_approved = $4 WHERE id = $1::uuid",
            account["id"],
            staff.cis_user_id,
            callback,
            flagged,
        )
    except asyncpg.CheckViolationError as exc:  # the DB backstop of the maker-checker rule and the owner marker
        raise Forbidden("maker_checker", "a supplier bank account is approved by a second person") from exc
    lock = await payout_lock(conn, supplier["id"])
    await _audit(
        conn,
        staff,
        supplier,
        "approved",
        f"Approved bank account {masked(account['account_last4'])} after a call-back"
        + (f" (replaces {masked(old)})" if old else "")
        + (" (business-owner self-approval)" if flagged else ""),
        {
            "bank_account_id": account["id"],
            "callback_note": callback,
            "superseded_last4": old,
            "payouts_open_at": lock["until"].isoformat() if lock.get("until") else None,
        }
        | self_approval.flag(flagged),
    )
