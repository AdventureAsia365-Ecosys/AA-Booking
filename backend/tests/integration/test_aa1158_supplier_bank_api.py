"""AA-1158 part 2 (bk-067) over HTTP on a real PostgreSQL, as role ``booking`` — the Done-when flows:

- a bank account entered by the content + ops custom role stays ``pending``; that role sees it masked, cannot read the
  details, approve or reject it;
- a payout to that supplier is refused while it is pending (409 ``bank_account_pending``), then for the lock after
  the approval (409 ``bank_payout_locked``; time moved on by ageing the row as the owner), then allowed;
- the stored details are ciphertext: neither the number nor the holder appears in the row (checked by counts);
- finance reads the details (audited, the audit row holds only the last 4); the tech lead never approves his own
  entry (403), the business owner does, flagged;
- reject / withdraw rules, one pending change per supplier, the profile fields;
- review S289: a contact change of the supplier by the person who entered the account (or by the approver) within
  ``supplier_bank_contact_change_days`` blocks the approval (409 ``contacts_recently_changed``); anyone else's change
  is listed on the approval card; the profile choices are admin-edited data (a retired code stays valid where held).

KMS is a fake key provider (AES-GCM with a fixed test master key): the database and the API are real. All names,
numbers and codes are invented."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.auth import custom_roles
from app.db import Database
from app.main import create_app
from app.settings import Settings
from app.suppliers import bank_crypto
from tests.integration import ops
from tests.staff_tokens import LocalCis

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

OWNER_SUB, LEAD_SUB, HOLDER_SUB = (f"d1158000-0000-4000-8000-00000000000{n}" for n in (1, 2, 3))
OWNER, LEAD = f"cog:{OWNER_SUB}", f"cog:{LEAD_SUB}"
MAKER, FIN, CHECKER = "it-1158p2-maker", "it-1158p2-fin", "it-1158p2-checker"
TOTP = ["pwd", "otp", "mfa"]
NUMBER, HOLDER = "VN12 3456 7890 1234", "Kestrel Lodges Invented Co"
ACCOUNT = {
    "account_holder": HOLDER,
    "account_number": NUMBER,
    "bank_name": "Harbourline Bank",
    "bank_country": "VN",
    "currency": "USD",
    "swift_bic": "HRBLVNVX",
    "reason": "The supplier sent new bank details by letter",
}
CALLBACK = "Called the accounts desk on the phone number in the signed contract; Ms Lantern confirmed"


class FakeKms:
    """GenerateDataKey / Decrypt stand-in: wraps the data key with a fixed master key, bound to the context."""

    master = AESGCM(b"\x07" * 32)

    def __init__(self) -> None:
        self.contexts: list[dict[str, str]] = []

    async def generate(self, context: dict[str, str]) -> bank_crypto.DataKey:
        self.contexts.append(context)
        key, nonce = os.urandom(32), os.urandom(12)
        wrapped = nonce + self.master.encrypt(nonce, key, repr(sorted(context.items())).encode())
        return bank_crypto.DataKey(plaintext=key, wrapped=wrapped, key_id="arn:test:kms:key/it-1158p2")

    async def unwrap(self, wrapped: bytes, context: dict[str, str]) -> bytes:
        self.contexts.append(context)
        return self.master.decrypt(wrapped[:12], wrapped[12:], repr(sorted(context.items())).encode())


async def _purge(admin: asyncpg.Connection) -> None:
    await ops.purge_ops(admin)
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE bk_owner")
        await admin.execute("DELETE FROM booking.audit_logs WHERE action LIKE 'supplier_bank_account.%'")
        await admin.execute("DELETE FROM booking.audit_logs WHERE action LIKE 'supplier_profile_option.%'")
        await admin.execute("DELETE FROM booking.supplier_profile_option WHERE created_by LIKE 'cog:d1158000-%'")
        await admin.execute("DELETE FROM booking.suppliers WHERE company_name LIKE 'it-1158p2 %'")
    await admin.execute("DELETE FROM shared.staff_booking_grant_stub WHERE cognito_sub LIKE 'd1158000-%'")
    custom_roles.forget()


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    await _purge(conn)
    await ops.staff(conn, (MAKER, "contracting"), (FIN, "finance"), (CHECKER, "admin"))
    for sub, business_owner in ((OWNER_SUB, True), (LEAD_SUB, False)):
        await conn.execute(
            "INSERT INTO shared.staff_booking_grant_stub (cognito_sub, booking_role_code, is_owner, is_business_owner)"
            " VALUES ($1, NULL, true, $2)",
            sub,
            business_owner,
        )
    await conn.execute(
        "INSERT INTO shared.staff_booking_grant_stub (cognito_sub, booking_role_code) VALUES ($1, $2)",
        HOLDER_SUB,
        custom_roles.SEED_CODE,
    )
    try:
        yield conn
    finally:
        await _purge(conn)
        await conn.close()


@pytest_asyncio.fixture
async def http(migrated_booking_dsn: str) -> AsyncIterator[tuple[httpx.AsyncClient, LocalCis, FakeKms]]:
    settings = Settings(database_url=migrated_booking_dsn, db_ssl="disable", environment="test")
    db, cis, kms = Database(settings), LocalCis(), FakeKms()
    app = create_app(settings, db, cis.verifier(), bank_key=kms)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://booking/booking") as c:
            yield c, cis, kms
    finally:
        await db.close()


def admin_of(cis: LocalCis, sub: str) -> dict[str, str]:
    return {"Authorization": "Bearer " + cis.workforce.token(["booking-admin", "cis-admin"], sub=sub, amr=TOTP)}


def custom(cis: LocalCis) -> dict[str, str]:
    return {"Authorization": "Bearer " + cis.workforce.token(["cis-custom", "booking-custom"], sub=HOLDER_SUB)}


def finance(cis: LocalCis) -> dict[str, str]:
    return {
        "Authorization": "Bearer "
        + cis.workforce.token(["booking-finance"], sub="d1158000-0000-4000-8000-0000000000f1")
    }


async def _ok(r: httpx.Response, status: int = 200) -> Any:
    assert r.status_code == status, r.text
    return r.json()


def _refused(r: httpx.Response, status: int, detail: str) -> None:
    assert r.status_code == status, r.text
    assert r.json()["detail"] == detail, r.text


async def _age(admin: asyncpg.Connection, supplier: str, hours: int) -> None:
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE bk_owner")
        await admin.execute(
            "UPDATE booking.supplier_bank_account SET entered_at = entered_at - make_interval(hours => $2),"
            " approved_at = approved_at - make_interval(hours => $2) WHERE supplier_id = $1::uuid",
            supplier,
            hours,
        )


async def test_entry_stays_pending_payouts_wait_for_approval_and_the_lock(
    http: tuple[httpx.AsyncClient, LocalCis, FakeKms], admin: asyncpg.Connection
) -> None:
    c, cis, kms = http
    lead, holder = admin_of(cis, LEAD_SUB), custom(cis)
    sup = await ops.supply(admin, "it-1158p2 Kestrel")
    sid = sup["supplier"]
    case = await ops.sold_case(admin, sup, tag="aa1158p2")
    booking = await ops.confirmed_booking(admin, case, sid, net="100.00")
    bill = await ops.bill(admin, sid, by=MAKER, amount="100.00", number="IT1158P2-1", lines=((booking, "100"),))
    await ops.approve_bill(admin, bill["id"], by=CHECKER)

    # The content + ops role enters the account: pending, masked for it, no read / approve.
    entered = await _ok(await c.post(f"/suppliers/{sid}/bank-accounts", json=ACCOUNT, headers=holder), 201)
    assert entered["status"] == "pending" and entered["account_masked"] == "•••• 1234"
    assert entered["actions"]["withdraw"] is True and entered["actions"]["reveal"] is False
    listed = await _ok(await c.get(f"/suppliers/{sid}/bank-accounts", headers=holder))
    assert listed["payout_lock"]["state"] == "pending" and listed["can_read_details"] is False
    assert NUMBER.replace(" ", "") not in str(listed) and HOLDER not in str(listed)
    _refused(await c.post(f"/supplier-bank-accounts/{entered['id']}/reveal", headers=holder), 403, "forbidden")
    decision = {"action": "approve", "note": CALLBACK}
    _refused(await c.post(f"/supplier-bank-accounts/{entered['id']}/decision", json=decision, headers=holder),
             403, "forbidden")  # fmt: skip
    _refused(await c.post(f"/suppliers/{sid}/bank-accounts", json=ACCOUNT, headers=lead), 409, "bank_change_pending")
    assert kms.contexts[-1] == {"purpose": "supplier_bank_account", "supplier_id": sid}
    # My work: the approver's tile equals the supplier list filter; the enterer has no approval tile.
    tile = {t["key"]: t for t in (await _ok(await c.get("/my-work/summary", headers=lead)))["tiles"]}
    assert tile["supplier_bank_to_approve"]["count"] == 1
    approvable = await _ok(await c.get("/suppliers", params={"bank": "pending", "approvable": "me"}, headers=lead))
    assert [r["id"] for r in approvable["items"]] == [sid]
    mine = await _ok(await c.get("/my-work/summary", headers=holder))
    assert "supplier_bank_to_approve" not in {t["key"] for t in mine["tiles"]}

    # The stored row is ciphertext (counts only, the bytes never printed).
    leaks = await admin.fetchval(
        "SELECT count(*) AS n FROM booking.supplier_bank_account WHERE supplier_id = $1::uuid"
        " AND (position(convert_to($2, 'UTF8') IN ciphertext) > 0"
        " OR position(convert_to($3, 'UTF8') IN ciphertext) > 0)",
        sid,
        NUMBER.replace(" ", ""),
        HOLDER,
    )
    assert leaks == 0
    assert (
        await admin.fetchval(
            "SELECT count(*) AS n FROM booking.supplier_bank_account WHERE supplier_id = $1::uuid"
            " AND account_last4 = '1234' AND octet_length(ciphertext) > 40"
            " AND kms_key_id = 'arn:test:kms:key/it-1158p2'",
            sid,
        )
        == 1
    )

    # No payout while it waits.
    pay = {"amount": "50.00", "method": "bank_transfer", "paid_on": "2026-11-20", "reference": "IT1158P2-A"}

    async def pay_as(key: str) -> httpx.Response:
        return await c.post(f"/supplier-bills/{bill['id']}/payouts", json=pay, headers={**lead, "Idempotency-Key": key})

    bill_detail = await _ok(await c.get(f"/supplier-bills/{bill['id']}", headers=lead))
    assert bill_detail["payout_lock"]["state"] == "pending" and bill_detail["actions"]["pay"] is False
    _refused(await pay_as("it1158-p2a"), 409, "bank_account_pending")

    # A short call-back note is refused; the tech lead approves after a real one.
    short = {"action": "approve", "note": "ok"}
    r = await c.post(f"/supplier-bank-accounts/{entered['id']}/decision", json=short, headers=lead)
    assert r.status_code == 422 and r.json()["detail"] == "callback_note_required", r.text
    approved = await _ok(await c.post(f"/supplier-bank-accounts/{entered['id']}/decision", json=decision, headers=lead))
    assert approved["status"] == "approved" and approved["owner_self_approved"] is False

    # Locked for the policy hours (24 by default) after the approval.
    r = await pay_as("it1158-p2b")
    assert r.status_code == 409 and r.json()["detail"] == "bank_payout_locked" and r.json()["until"], r.text
    await _age(admin, sid, 23)
    _refused(await pay_as("it1158-p2c"), 409, "bank_payout_locked")
    await _age(admin, sid, 2)
    await _ok(await pay_as("it1158-p2d"), 201)

    # Finance reads the details; the read is audited with the last 4 only.
    fin = finance(cis)
    details = await _ok(await c.post(f"/supplier-bank-accounts/{entered['id']}/reveal", headers=fin))
    assert details["account_number"] == NUMBER.replace(" ", "") and details["account_holder"] == HOLDER
    audit = await admin.fetch(
        "SELECT action, after::text AS after FROM booking.audit_logs WHERE entity_id = $1::uuid"
        " AND action LIKE 'supplier_bank_account.%' ORDER BY created_at, id",
        sid,
    )
    assert [r["action"] for r in audit] == [
        "supplier_bank_account.entered",
        "supplier_bank_account.approved",
        "supplier_bank_account.revealed",
    ]
    assert not any(NUMBER.replace(" ", "") in r["after"] or HOLDER in r["after"] for r in audit)


async def test_own_entries_reject_withdraw_and_supersede(
    http: tuple[httpx.AsyncClient, LocalCis, FakeKms], admin: asyncpg.Connection
) -> None:
    c, cis, _ = http
    owner, lead = admin_of(cis, OWNER_SUB), admin_of(cis, LEAD_SUB)
    sid = (await ops.supply(admin, "it-1158p2 Ridgeway"))["supplier"]
    decision = {"action": "approve", "note": CALLBACK}

    # The tech lead never approves (nor rejects) his own entry.
    mine = await _ok(await c.post(f"/suppliers/{sid}/bank-accounts", json=ACCOUNT, headers=lead), 201)
    _refused(await c.post(f"/supplier-bank-accounts/{mine['id']}/decision", json=decision, headers=lead),
             403, "maker_checker")  # fmt: skip
    reject = {"action": "reject", "note": "The number does not match the call-back"}
    _refused(await c.post(f"/supplier-bank-accounts/{mine['id']}/decision", json=reject, headers=lead),
             403, "maker_checker")  # fmt: skip
    _refused(await c.post(f"/supplier-bank-accounts/{mine['id']}/decision", json={"action": "withdraw", "note": "x"},
                          headers=owner), 403, "forbidden")  # fmt: skip
    rejected = await _ok(await c.post(f"/supplier-bank-accounts/{mine['id']}/decision", json=reject, headers=owner))
    assert rejected["status"] == "rejected"

    # The business owner approves her own entry, flagged; a later approved one supersedes it.
    own = await _ok(await c.post(f"/suppliers/{sid}/bank-accounts", json=ACCOUNT, headers=owner), 201)
    flagged = await _ok(await c.post(f"/supplier-bank-accounts/{own['id']}/decision", json=decision, headers=owner))
    assert flagged["status"] == "approved" and flagged["owner_self_approved"] is True
    second = await _ok(
        await c.post(f"/suppliers/{sid}/bank-accounts", json={**ACCOUNT, "account_number": "VN99 0000 0000 5678"},
                     headers=lead), 201)  # fmt: skip
    withdraw = {"action": "withdraw", "note": "Typed the wrong account"}
    withdrawn = await _ok(await c.post(f"/supplier-bank-accounts/{second['id']}/decision", json=withdraw, headers=lead))
    assert withdrawn["status"] == "withdrawn"
    third = await _ok(
        await c.post(f"/suppliers/{sid}/bank-accounts", json={**ACCOUNT, "account_number": "VN99 0000 0000 9012"},
                     headers=lead), 201)  # fmt: skip
    await _ok(await c.post(f"/supplier-bank-accounts/{third['id']}/decision", json=decision, headers=owner))
    items = (await _ok(await c.get(f"/suppliers/{sid}/bank-accounts", headers=lead)))["items"]
    assert sorted((r["account_masked"], r["status"]) for r in items) == [
        ("•••• 1234", "rejected"),
        ("•••• 1234", "superseded"),
        ("•••• 5678", "withdrawn"),
        ("•••• 9012", "approved"),
    ]
    assert (
        await admin.fetchval(
            "SELECT count(*) AS n FROM booking.audit_logs WHERE entity_id = $1::uuid"
            " AND action = 'supplier_bank_account.approved' AND (after->>'owner_self_approved') = 'true'",
            sid,
        )
        == 1
    )


async def test_entry_without_the_key_is_unavailable(admin: asyncpg.Connection, migrated_booking_dsn: str) -> None:
    settings = Settings(database_url=migrated_booking_dsn, db_ssl="disable", environment="test")
    db, cis = Database(settings), LocalCis()
    app = create_app(settings, db, cis.verifier())
    sid = (await ops.supply(admin, "it-1158p2 Nokey"))["supplier"]
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://booking/booking") as c:
            r = await c.post(f"/suppliers/{sid}/bank-accounts", json=ACCOUNT, headers=admin_of(cis, LEAD_SUB))
            assert r.status_code == 503 and r.json()["detail"] == "bank_crypto_unavailable", r.text
            listed = await _ok(await c.get(f"/suppliers/{sid}/bank-accounts", headers=admin_of(cis, LEAD_SUB)))
            assert listed["items"] == [] and listed["payout_lock"]["state"] == "none"
    finally:
        await db.close()


async def test_profile_fields(http: tuple[httpx.AsyncClient, LocalCis, FakeKms], admin: asyncpg.Connection) -> None:
    c, cis, _ = http
    lead = admin_of(cis, LEAD_SUB)
    sid = (await ops.supply(admin, f"it-1158p2 Profile {uuid.uuid4().hex[:4]}"))["supplier"]
    body = {
        "legal_entity": "  Kestrel Lodges Invented Co., Ltd ",
        "tax_code": "0312345678",
        "role_contacts": [{"role": "accounts", "name": "Mai Lantern", "email": "Accounts@Example.TEST"}],
        "payment_terms": {"basis": "deposit_balance", "deposit_percent": 30, "due_days": 14, "method": "bank_transfer"},
    }
    out = await _ok(await c.patch(f"/suppliers/{sid}", json=body, headers=lead))
    assert out["legal_entity"] == "Kestrel Lodges Invented Co., Ltd"
    assert out["role_contacts"] == [
        {"role": "accounts", "name": "Mai Lantern", "email": "accounts@example.test", "phone": None}
    ]
    assert out["payment_terms"]["deposit_percent"] == "30" and out["payment_terms"]["basis"] == "deposit_balance"
    got = await _ok(await c.get(f"/suppliers/{sid}", headers=lead))
    assert got["tax_code"] == "0312345678" and got["role_contacts"][0]["role"] == "accounts"
    bad = {"payment_terms": {"basis": "prepayment", "deposit_percent": 30}}
    r = await c.patch(f"/suppliers/{sid}", json=bad, headers=lead)
    assert r.status_code == 422 and r.json()["detail"] == "invalid_payment_terms", r.text
    r = await c.patch(f"/suppliers/{sid}", json={"role_contacts": [{"role": "accounts"}]}, headers=lead)
    assert r.status_code == 422 and r.json()["detail"] == "invalid_role_contact", r.text
    cleared = await _ok(await c.patch(f"/suppliers/{sid}", json={"payment_terms": None, "role_contacts": []},
                                      headers=lead))  # fmt: skip
    assert cleared["payment_terms"] is None and cleared["role_contacts"] == []


async def _enter_and_approve(
    c: httpx.AsyncClient, sid: str, enterer: dict[str, str], approver: dict[str, str]
) -> tuple[dict[str, Any], httpx.Response]:
    entered = await _ok(await c.post(f"/suppliers/{sid}/bank-accounts", json=ACCOUNT, headers=enterer), 201)
    decision = {"action": "approve", "note": CALLBACK}
    return entered, await c.post(f"/supplier-bank-accounts/{entered['id']}/decision", json=decision, headers=approver)


async def test_a_contact_change_before_the_call_back(
    http: tuple[httpx.AsyncClient, LocalCis, FakeKms], admin: asyncpg.Connection
) -> None:
    c, cis, _ = http
    lead, owner, holder = admin_of(cis, LEAD_SUB), admin_of(cis, OWNER_SUB), custom(cis)
    planted = {"contact_phone": "+84 000 999 111", "role_contacts": [{"role": "accounts", "phone": "+84 000 999 111"}]}

    # One person changes the live supplier's contacts, then enters a bank account: the approval is refused.
    sid = (await ops.supply(admin, "it-1158p2 Planted"))["supplier"]
    await _ok(await c.patch(f"/suppliers/{sid}", json=planted, headers=holder))
    entered = await _ok(await c.post(f"/suppliers/{sid}/bank-accounts", json=ACCOUNT, headers=holder), 201)
    card = await _ok(await c.get(f"/suppliers/{sid}/bank-accounts", headers=lead))
    assert card["contact_changes"]["days"] == 30
    assert [(i["by"]["id"], i["fields"]) for i in card["contact_changes"]["items"]] == [
        (f"cog:{HOLDER_SUB}", ["contact_phone", "role_contacts"])
    ]
    assert card["items"][0]["approve_blocked"] == "contacts_recently_changed"
    assert card["items"][0]["actions"]["approve"] is False and card["items"][0]["actions"]["reject"] is True
    decision = {"action": "approve", "note": CALLBACK}
    r = await c.post(f"/supplier-bank-accounts/{entered['id']}/decision", json=decision, headers=lead)
    assert r.status_code == 409 and r.json()["detail"] == "contacts_recently_changed" and r.json()["until"], r.text
    _refused(await c.post(f"/supplier-bank-accounts/{entered['id']}/decision", json=decision, headers=owner),
             409, "contacts_recently_changed")  # fmt: skip
    reject = {"action": "reject", "note": "Contacts changed by the same person; checking with the contract"}
    assert (await _ok(await c.post(f"/supplier-bank-accounts/{entered['id']}/decision", json=reject, headers=lead)))[
        "status"
    ] == "rejected"

    # The approver changed the contacts: refused too.
    sid = (await ops.supply(admin, "it-1158p2 Approver"))["supplier"]
    await _ok(await c.patch(f"/suppliers/{sid}", json=planted, headers=lead))
    _, r = await _enter_and_approve(c, sid, holder, lead)
    _refused(r, 409, "contacts_recently_changed")

    # Someone else's change is shown on the card, and the call-back approval goes through.
    sid = (await ops.supply(admin, "it-1158p2 Third"))["supplier"]
    await _ok(await c.patch(f"/suppliers/{sid}", json={"contact_email": "desk@third.example.test"}, headers=owner))
    _, r = await _enter_and_approve(c, sid, holder, lead)
    assert (await _ok(r))["status"] == "approved"
    card = await _ok(await c.get(f"/suppliers/{sid}/bank-accounts", headers=lead))
    assert card["contact_changes"] == {"days": None, "items": []}  # nothing pending: nothing to decide


async def test_profile_choices_are_admin_edited_data(
    http: tuple[httpx.AsyncClient, LocalCis, FakeKms], admin: asyncpg.Connection
) -> None:
    c, cis, _ = http
    lead, holder = admin_of(cis, LEAD_SUB), custom(cis)
    kinds = (await _ok(await c.get("/supplier-profile-options", headers=holder)))["kinds"]
    assert [o["code"] for o in kinds["terms_method"]] == ["bank_transfer", "card", "cash", "other"]
    new = {"kind": "contact_role", "code": "night_desk", "label_en": "Night desk", "label_vi": "Trực đêm"}
    _refused(await c.post("/supplier-profile-options", json=new, headers=holder), 403, "forbidden")
    added = await _ok(await c.post("/supplier-profile-options", json=new, headers=lead), 201)
    assert added["active"] is True and added["position"] == 70
    _refused(await c.post("/supplier-profile-options", json=new, headers=lead), 409, "option_exists")

    sid = (await ops.supply(admin, f"it-1158p2 Choices {uuid.uuid4().hex[:4]}"))["supplier"]
    night = {"role_contacts": [{"role": "night_desk", "name": "Hoa Lantern"}]}
    await _ok(await c.patch(f"/suppliers/{sid}", json=night, headers=lead))
    retired = await _ok(await c.patch("/supplier-profile-options/contact_role/night_desk", json={"active": False},
                                      headers=lead))  # fmt: skip
    assert retired["active"] is False
    # The supplier that holds the retired role keeps it through an edit; nobody adds it anew.
    kept = {"role_contacts": [{"role": "night_desk", "name": "Hoa Lantern", "phone": "+84 000 222"}]}
    await _ok(await c.patch(f"/suppliers/{sid}", json=kept, headers=lead))
    other = (await ops.supply(admin, f"it-1158p2 Choices {uuid.uuid4().hex[:4]}"))["supplier"]
    _refused(await c.patch(f"/suppliers/{other}", json=night, headers=lead), 422, "invalid_role_contact")
    audit = await admin.fetch(
        "SELECT action FROM booking.audit_logs WHERE entity_type = 'supplier_profile_option' AND entity_id = $1::uuid"
        " ORDER BY created_at, id",
        added["id"],
    )
    assert [r["action"] for r in audit] == ["supplier_profile_option.created", "supplier_profile_option.updated"]
