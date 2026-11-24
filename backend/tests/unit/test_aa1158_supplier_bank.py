"""AA-1158 part 2 — supplier bank accounts and profile fields, without a database or AWS (the DB rules are
tests/integration/test_bk067_schema.py, the HTTP flows tests/integration/test_aa1158_supplier_bank_api.py):

- envelope encryption: a round trip; a ciphertext moved to another row or supplier never opens; the KMS context is
  exactly {purpose, supplier_id}; the boto3 provider sends the key, the spec and the context and never leaks the
  request in its error; no key configured = 503;
- input checks and masking; what each caller may do on a row;
- the payout lock with a clock override (pending, none, locked, open) and its 409 answers;
- the decisions' permission and maker rules before any write;
- the profile fields (contacts by role, payment terms): shapes here, the codes against the admin-edited choices in
  tests/unit/test_aa1158_profile_options.py;
- a contact change by the person who entered the account or by the approver blocks the approval (review S289);
- fix pass 2 (review S289): a 422 on the entry / decision body never echoes a submitted value (nor logs it); the full
  details only of a pending or approved row; no Approve for the person who entered a row unless business owner;
- tech lead S289: only the business owner approves, the tech lead (platform owner marker) as the fallback; any other
  administrator, a capped session and a view-as session get no Approve and 403 ``bank_approver_not_allowed``
  (tests/unit/test_aa1158_bank_approver.py; the tests here stand every approver in as the tech lead).

KMS is a fake key provider; all names and numbers are invented."""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi.testclient import TestClient

from app.auth import self_approval
from app.auth.staff import Staff, current_staff, get_pool
from app.errors import Conflict, Forbidden, Invalid, NotFound, Unavailable
from app.main import create_app
from app.settings import Settings
from app.suppliers import bank_accounts, bank_crypto, profile
from tests.unit.fakes import ScriptedConn, policy_rows

SUPPLIER = "5a000000-0000-4000-8000-0000000011b2"
ROW = "5b000000-0000-4000-8000-0000000011b2"
OTHER = "5b000000-0000-4000-8000-0000000011b3"
ENTERER = Staff("it-1158p2-maker", None, "Maker", "contracting", True, None)
CHECKER = Staff("it-1158p2-checker", None, "Checker", "admin", True, None)
FIN = Staff("it-1158p2-fin", None, "Fin", "finance", True, None)
SALES = Staff("it-1158p2-sales", None, "Sales", "sales", True, None)
NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
GOOD = {
    "account_holder": "Kestrel Lodges Invented Co",
    "account_number": "vn12 3456-7890 1234",
    "bank_name": "Harbourline Bank",
    "currency": "usd",
    "bank_country": "vn",
    "swift_bic": "hrbl vn vx",
    "reason": "New details by letter",
}


class FakeKms:
    master = AESGCM(b"\x05" * 32)

    def __init__(self) -> None:
        self.contexts: list[dict[str, str]] = []

    async def generate(self, context: dict[str, str]) -> bank_crypto.DataKey:
        self.contexts.append(dict(context))
        key, nonce = os.urandom(32), os.urandom(12)
        wrapped = nonce + self.master.encrypt(nonce, key, json.dumps(context, sort_keys=True).encode())
        return bank_crypto.DataKey(plaintext=key, wrapped=wrapped, key_id="arn:test:key/1")

    async def unwrap(self, wrapped: bytes, context: dict[str, str]) -> bytes:
        self.contexts.append(dict(context))
        try:
            return self.master.decrypt(wrapped[:12], wrapped[12:], json.dumps(context, sort_keys=True).encode())
        except Exception as exc:  # noqa: BLE001 — like KMS: a context mismatch is a refusal
            raise bank_crypto.BankCryptoError("KMS Decrypt failed") from exc


@pytest.fixture(autouse=True)
def tech_lead_approves(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every holder of supplier_bank.approve stands in as the tech lead (the approver rule has its own tests below)."""
    asked: list[str] = []

    async def fake(_conn: Any, staff: Staff) -> str | None:
        asked.append(staff.cis_user_id)
        return bank_accounts.TECH_LEAD if staff.can("supplier_bank.approve") else None

    monkeypatch.setattr(bank_accounts, "approver_kind", fake)
    return asked


# ── Envelope encryption ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_seal_and_open_round_trip_bound_to_row_and_supplier() -> None:
    kms = FakeKms()
    details = {"account_number": "VN1234", "account_holder": "Kestrel"}
    sealed = await bank_crypto.seal(kms, SUPPLIER, ROW, details)
    assert b"VN1234" not in sealed.ciphertext and sealed.key_id == "arn:test:key/1"
    assert kms.contexts == [{"purpose": "supplier_bank_account", "supplier_id": SUPPLIER}]
    assert await bank_crypto.open_sealed(kms, SUPPLIER, ROW, sealed.ciphertext, sealed.wrapped_key) == details
    with pytest.raises(bank_crypto.BankCryptoError):  # copied onto another row
        await bank_crypto.open_sealed(kms, SUPPLIER, OTHER, sealed.ciphertext, sealed.wrapped_key)
    with pytest.raises(bank_crypto.BankCryptoError):  # another supplier's context: KMS refuses the unwrap
        await bank_crypto.open_sealed(kms, OTHER, ROW, sealed.ciphertext, sealed.wrapped_key)


@pytest.mark.asyncio
async def test_a_short_data_key_is_refused() -> None:
    class Short(FakeKms):
        async def generate(self, context: dict[str, str]) -> bank_crypto.DataKey:
            return bank_crypto.DataKey(plaintext=b"\x00" * 16, wrapped=b"w" * 20, key_id="k")

    with pytest.raises(bank_crypto.BankCryptoError):
        await bank_crypto.seal(Short(), SUPPLIER, ROW, {})


class StubKmsClient:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def generate_data_key(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("generate", kw))
        if self.fail:
            raise RuntimeError(f"secret request detail {kw}")
        return {"Plaintext": b"\x01" * 32, "CiphertextBlob": b"wrapped", "KeyId": "arn:aws:kms:us-west-1:x:key/k"}

    def decrypt(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("decrypt", kw))
        if self.fail:
            raise RuntimeError(f"secret request detail {kw}")
        return {"Plaintext": b"\x01" * 32}


@pytest.mark.asyncio
async def test_the_kms_provider_sends_the_key_spec_and_context(caplog: pytest.LogCaptureFixture) -> None:
    provider = bank_crypto.KmsKeyProvider("alias/aa-booking-dev-supplier-bank", "us-west-1")
    stub = StubKmsClient()
    provider._client = stub
    ctx = bank_crypto.context(SUPPLIER)
    key = await provider.generate(ctx)
    assert key.plaintext == b"\x01" * 32 and key.wrapped == b"wrapped"
    assert await provider.unwrap(b"wrapped", ctx) == b"\x01" * 32
    assert stub.calls[0] == (
        "generate",
        {"KeyId": "alias/aa-booking-dev-supplier-bank", "KeySpec": "AES_256", "EncryptionContext": ctx},
    )
    assert stub.calls[1][1]["EncryptionContext"] == ctx
    provider._client = StubKmsClient(fail=True)
    with pytest.raises(bank_crypto.BankCryptoError) as err:
        await provider.generate(ctx)
    assert "secret" not in str(err.value) and err.value.__cause__ is None
    with pytest.raises(bank_crypto.BankCryptoError):
        await provider.unwrap(b"wrapped", ctx)
    assert "secret" not in caplog.text and "supplier_bank_kms_failed step=generate" in caplog.text


def test_provider_from_settings() -> None:
    assert bank_crypto.provider_from_settings(Settings(database_url=None)) is None
    with pytest.raises(Unavailable):
        bank_crypto.require(None)
    made = bank_crypto.provider_from_settings(
        Settings(database_url=None, supplier_bank_kms_key_id="alias/aa-booking-dev-supplier-bank")
    )
    assert isinstance(made, bank_crypto.KmsKeyProvider) and made.region == "us-west-1"


@pytest.mark.parametrize("value", ["not-an-alias", "alias/", "arn:aws:s3:::bucket"])
def test_settings_refuse_a_bad_key_id(value: str) -> None:
    with pytest.raises(ValueError, match="SUPPLIER_BANK_KMS_KEY_ID"):
        Settings(database_url=None, supplier_bank_kms_key_id=value)


# ── Input and rows ───────────────────────────────────────────────────────────


def test_clean_normalises_and_keeps_the_details_apart() -> None:
    details, clear = bank_accounts.clean(GOOD)
    assert details == {
        "account_holder": "Kestrel Lodges Invented Co",
        "account_number": "VN12345678901234",
        "swift_bic": "HRBLVNVX",
        "bank_code": None,
        "bank_address": None,
    }
    assert clear == {
        "bank_name": "Harbourline Bank",
        "bank_country": "VN",
        "currency": "USD",
        "reason": "New details by letter",
        "account_last4": "1234",
    }


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"account_number": "12"}, "invalid_account_number"),
        ({"account_number": "VN12-34*56"}, "invalid_account_number"),
        ({"swift_bic": "HRB"}, "invalid_swift_bic"),
        ({"currency": "US"}, "invalid_currency"),
        ({"bank_country": "VNM"}, "invalid_bank_country"),
        ({"reason": "  "}, "reason_required"),
        ({"account_holder": "x" * 141}, "account_holder_too_long"),
        ({"bank_name": None}, "bank_name_required"),
    ],
)
def test_clean_refuses(change: dict[str, Any], code: str) -> None:
    with pytest.raises(Invalid) as err:
        bank_accounts.clean({**GOOD, **change})
    assert err.value.code == code


def _row(**over: Any) -> dict[str, Any]:
    return {
        "id": ROW,
        "supplier_id": SUPPLIER,
        "status": "pending",
        "bank_name": "Harbourline Bank",
        "bank_country": "VN",
        "currency": "USD",
        "account_last4": "1234",
        "reason": "r",
        "entered_by": ENTERER.cis_user_id,
        "entered_by_name": "Maker",
        "entered_at": NOW,
        "approved_by": None,
        "approved_by_name": None,
        "approved_at": None,
        "callback_note": None,
        "owner_self_approved": False,
        "closed_by": None,
        "closed_by_name": None,
        "closed_at": None,
        "close_reason": None,
        **over,
    }


def test_row_actions_by_caller() -> None:
    own = bank_accounts.row_out(_row(), ENTERER)
    assert own["account_masked"] == "•••• 1234" and own["entered_by"] == {"id": ENTERER.cis_user_id, "name": "Maker"}
    assert own["actions"] == {
        "reveal": False,
        "approve": False,
        "approve_self": False,
        "reject": False,
        "withdraw": True,
    }
    checker = bank_accounts.row_out(_row(), CHECKER, approver_ok=True)["actions"]
    assert checker["approve"] and checker["reject"] and checker["reveal"] and not checker["withdraw"]
    assert bank_accounts.row_out(_row(), FIN)["actions"]["reveal"] is True
    assert bank_accounts.row_out(_row(), SALES)["actions"] == {k: False for k in own["actions"]}
    done = bank_accounts.row_out(_row(status="approved"), CHECKER, approver_ok=True)["actions"]
    assert not done["approve"] and not done["reject"]


def test_a_recent_contact_change_blocks_the_approve_action() -> None:
    by_enterer = bank_accounts.row_out(_row(), CHECKER, frozenset({ENTERER.cis_user_id}), approver_ok=True)
    assert by_enterer["approve_blocked"] == "contacts_recently_changed"
    assert not by_enterer["actions"]["approve"] and by_enterer["actions"]["reject"]
    by_me = bank_accounts.row_out(_row(), CHECKER, frozenset({CHECKER.cis_user_id}), approver_ok=True)
    assert not by_me["actions"]["approve"]
    someone_else = bank_accounts.row_out(_row(), CHECKER, frozenset({"it-1158p2-third"}), approver_ok=True)
    assert someone_else["actions"]["approve"] and someone_else["approve_blocked"] is None
    decided = bank_accounts.row_out(_row(status="approved"), CHECKER, frozenset({ENTERER.cis_user_id}))
    assert decided["approve_blocked"] is None


ADMIN_ENTERER = Staff("it-1158p2-admin", None, "Admin", "admin", True, None)


def test_no_approve_for_the_person_who_entered_unless_business_owner() -> None:
    row = _row(entered_by=ADMIN_ENTERER.cis_user_id)
    plain = bank_accounts.row_out(row, ADMIN_ENTERER)["actions"]
    assert not plain["approve"] and not plain["approve_self"] and not plain["reject"] and plain["withdraw"]
    owner = bank_accounts.row_out(row, ADMIN_ENTERER, self_ok=True, approver_ok=True)["actions"]
    assert owner["approve"] and owner["approve_self"]
    # A recent contact change still blocks the business owner's own approval.
    blocked = bank_accounts.row_out(row, ADMIN_ENTERER, frozenset({ADMIN_ENTERER.cis_user_id}), self_ok=True)
    assert not blocked["actions"]["approve"]


@pytest.mark.parametrize(
    ("status", "shown"),
    [("pending", True), ("approved", True), ("withdrawn", False), ("rejected", False), ("superseded", False)],
)
def test_reveal_action_only_while_the_row_may_be_paid_to(status: str, shown: bool) -> None:
    assert bank_accounts.row_out(_row(status=status), FIN)["actions"]["reveal"] is shown


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [False, True])
async def test_list_reads_the_owner_marker_only_for_an_own_pending_row(
    monkeypatch: pytest.MonkeyPatch, owner: bool
) -> None:
    asked: list[str] = []

    async def fake_may_self_approve(_conn: Any, staff: Staff, kind: str = "approve") -> bool:
        asked.append(staff.cis_user_id)
        return owner

    monkeypatch.setattr(self_approval, "may_self_approve", fake_may_self_approve)
    conn = (
        ScriptedConn()
        .on("FROM booking.suppliers WHERE", {"id": SUPPLIER, "code": "SUP-001158", "company_name": "Kestrel"})
        .on("ORDER BY a.entered_at DESC", [_row(entered_by=ADMIN_ENTERER.cis_user_id)])
        .on(
            "FROM booking.supplier_bank_account a WHERE a.supplier_id",
            {"pending": True, "last_approved": None, "now": NOW},
        )  # fmt: skip
    )
    out = await bank_accounts.list_for(conn, ADMIN_ENTERER, SUPPLIER)
    assert out["items"][0]["actions"]["approve"] is owner and asked == [ADMIN_ENTERER.cis_user_id]
    asked.clear()
    await bank_accounts.list_for(conn, CHECKER, SUPPLIER)  # not their row: no marker read
    assert asked == []


# ── The payout lock (clock override) ─────────────────────────────────────────


def lock_conn(pending: bool, last: datetime | None, hours: str = "24") -> ScriptedConn:
    row = {"pending": pending, "last_approved": last, "now": NOW}
    return (
        ScriptedConn()
        .on("FROM booking.supplier_bank_account a WHERE a.supplier_id", row)
        .on("SELECT (now() AT TIME ZONE 'UTC')::date", NOW.date())
        .on("FROM booking.pricing_policy p", policy_rows(supplier_bank_payout_lock_hours=hours))
    )


@pytest.mark.asyncio
async def test_payout_lock_states() -> None:
    assert (await bank_accounts.payout_lock(lock_conn(False, None), SUPPLIER))["state"] == "none"
    assert (await bank_accounts.payout_lock(lock_conn(True, NOW), SUPPLIER))["state"] == "pending"
    locked = await bank_accounts.payout_lock(lock_conn(False, NOW - timedelta(hours=23)), SUPPLIER)
    assert locked == {"state": "locked", "until": NOW + timedelta(hours=1), "hours": 24}
    assert (await bank_accounts.payout_lock(lock_conn(False, NOW - timedelta(hours=25)), SUPPLIER))["state"] == "open"
    # The clock override: the same approval, seen 2 hours later.
    later = await bank_accounts.payout_lock(lock_conn(False, NOW - timedelta(hours=23)), SUPPLIER,
                                            now=NOW + timedelta(hours=2))  # fmt: skip
    assert later["state"] == "open"
    # The policy value decides (48 h).
    assert (await bank_accounts.payout_lock(lock_conn(False, NOW - timedelta(hours=30), "48"), SUPPLIER))[
        "state"
    ] == "locked"
    assert (await bank_accounts.payout_lock(ScriptedConn(), SUPPLIER))["state"] == "none"


@pytest.mark.asyncio
async def test_check_payout_answers() -> None:
    with pytest.raises(Conflict) as err:
        await bank_accounts.check_payout(lock_conn(True, None), SUPPLIER)
    assert err.value.code == "bank_account_pending"
    with pytest.raises(Conflict) as err:
        await bank_accounts.check_payout(lock_conn(False, NOW - timedelta(hours=1)), SUPPLIER)
    assert (
        err.value.code == "bank_payout_locked" and err.value.extra["until"] == (NOW + timedelta(hours=23)).isoformat()
    )
    await bank_accounts.check_payout(lock_conn(False, NOW - timedelta(hours=1)), SUPPLIER, now=NOW + timedelta(days=1))
    no_value = lock_conn(False, NOW).on("FROM booking.pricing_policy p", [])
    with pytest.raises(Unavailable):
        await bank_accounts.check_payout(no_value, SUPPLIER)


# ── Decisions and entry: rules before any write ──────────────────────────────


def decide_conn(status: str = "pending", entered_by: str = ENTERER.cis_user_id) -> ScriptedConn:
    account = {"id": ROW, "supplier_id": SUPPLIER, "status": status, "entered_by": entered_by,
               "account_last4": "1234", "bank_name": "Harbourline Bank"}  # fmt: skip
    supplier = {"id": SUPPLIER, "code": "SUP-001158", "company_name": "Kestrel"}
    return (
        ScriptedConn()
        .on("FROM booking.supplier_bank_account WHERE id = $1::uuid", account)
        .on("FROM booking.suppliers WHERE", supplier)
        .on(f"SELECT {' '.join(bank_accounts.COLUMNS.split())}", _row(status=status, entered_by=entered_by))
    )


def writes(conn: ScriptedConn) -> list[tuple[Any, ...]]:
    return conn.sql("UPDATE booking.supplier_bank_account")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("staff", "action", "note", "error", "code"),
    [
        (SALES, "approve", "x" * 20, Forbidden, "forbidden"),
        (ENTERER, "approve", "x" * 20, Forbidden, "forbidden"),  # contracting enters, never approves
        (CHECKER, "approve", "too short", Invalid, "callback_note_required"),
        (CHECKER, "withdraw", "mine?", Forbidden, "forbidden"),
        (CHECKER, "explode", None, Invalid, "unknown_action"),
        (CHECKER, "reject", "", Invalid, "reason_required"),
    ],
)
async def test_decisions_refused_before_any_write(
    staff: Staff, action: str, note: str | None, error: type[Exception], code: str
) -> None:
    conn = decide_conn()
    with pytest.raises(error) as err:
        await bank_accounts.decide(conn, staff, ROW, action, note)
    assert err.value.code == code
    assert writes(conn) == []


@pytest.mark.asyncio
async def test_reject_by_the_enterer_and_a_decided_row() -> None:
    admin_enterer = Staff("it-1158p2-admin", None, "Admin", "admin", True, None)
    conn = decide_conn(entered_by=admin_enterer.cis_user_id)
    with pytest.raises(Forbidden) as err:
        await bank_accounts.decide(conn, admin_enterer, ROW, "reject", "wrong number")
    assert err.value.code == "maker_checker" and writes(conn) == []
    with pytest.raises(Conflict) as err:
        await bank_accounts.decide(decide_conn(status="approved"), CHECKER, ROW, "reject", "late")
    assert err.value.code == "bank_account_decided"
    with pytest.raises(NotFound):
        await bank_accounts.decide(ScriptedConn(), CHECKER, "not-a-uuid", "reject", "x")


@pytest.mark.asyncio
async def test_withdraw_and_reject_write_once() -> None:
    conn = decide_conn()
    out = await bank_accounts.decide(conn, ENTERER, ROW, "withdraw", "typo")
    assert out["id"] == ROW
    assert len(writes(conn)) == 1 and writes(conn)[0][1:] == (ENTERER.cis_user_id, "typo")
    assert conn.sql("INSERT INTO booking.audit_logs")[0][3] == "supplier_bank_account.withdrawn"
    conn = decide_conn()
    await bank_accounts.decide(conn, CHECKER, ROW, "reject", "number does not match")
    assert conn.sql("INSERT INTO booking.audit_logs")[0][3] == "supplier_bank_account.rejected"


@pytest.mark.asyncio
async def test_approve_supersedes_then_approves_with_the_callback() -> None:
    callback = "Called the accounts desk on the contract number"
    lock_row = {"pending": False, "last_approved": NOW, "now": NOW}
    conn = (
        decide_conn()
        .on("SET status = 'superseded'", "9999")
        .on("FROM booking.supplier_bank_account a WHERE a.supplier_id", lock_row)
    )
    await bank_accounts.decide(conn, CHECKER, ROW, "approve", callback)
    updates = writes(conn)
    assert len(updates) == 2  # superseded, then approved
    assert updates[1] == (ROW, CHECKER.cis_user_id, callback, False)
    audit_after = json.loads(conn.sql("INSERT INTO booking.audit_logs")[0][7])
    assert audit_after["superseded_last4"] == "9999" and "owner_self_approved" not in audit_after
    assert audit_after["payouts_open_at"] == (NOW + timedelta(hours=24)).isoformat()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changer", "who"),
    [(ENTERER.cis_user_id, "the person who entered"), (CHECKER.cis_user_id, "you changed")],
)
async def test_approve_refused_after_a_contact_change(changer: str, who: str) -> None:
    changed = NOW - timedelta(days=3)
    conn = decide_conn().on("max(l.created_at) AS at", [{"actor_ref": changer, "at": changed}])
    with pytest.raises(Conflict) as err:
        await bank_accounts.decide(conn, CHECKER, ROW, "approve", "Called the desk on the contract number")
    assert err.value.code == "contacts_recently_changed" and who in err.value.message
    assert err.value.extra["until"] == (changed + timedelta(days=30)).isoformat()
    assert writes(conn) == []
    days, fields, people = conn.sql("max(l.created_at) AS at")[0][1:]
    assert days == 30 and set(fields) == set(bank_accounts.CONTACT_FIELDS)
    assert people == [ENTERER.cis_user_id, CHECKER.cis_user_id]


@pytest.mark.asyncio
async def test_list_shows_the_contact_changes_of_the_window() -> None:
    pending = _row()
    change = {"at": NOW, "actor_ref": "it-1158p2-third", "actor_name": "Third", "fields": ["contact_phone"]}
    conn = (
        ScriptedConn()
        .on("FROM booking.suppliers WHERE", {"id": SUPPLIER, "code": "SUP-001158", "company_name": "Kestrel"})
        .on("ORDER BY a.entered_at DESC", [pending])
        .on(
            "FROM booking.supplier_bank_account a WHERE a.supplier_id",
            {"pending": True, "last_approved": None, "now": NOW},
        )  # fmt: skip
        .on("LIMIT $4", [change])
        .on("max(l.created_at) AS at", [{"actor_ref": ENTERER.cis_user_id, "at": NOW}])
    )
    out = await bank_accounts.list_for(conn, CHECKER, SUPPLIER)
    assert out["contact_changes"] == {
        "days": 30,
        "items": [{"at": NOW, "by": {"id": "it-1158p2-third", "name": "Third"}, "fields": ["contact_phone"]}],
    }
    assert out["items"][0]["approve_blocked"] == "contacts_recently_changed"
    assert not out["items"][0]["actions"]["approve"] and not out["actions"]["enter"]
    none_pending = (
        ScriptedConn()
        .on("FROM booking.suppliers WHERE", {"id": SUPPLIER, "code": "SUP-001158", "company_name": "Kestrel"})
        .on("ORDER BY a.entered_at DESC", [_row(status="approved")])
    )
    out = await bank_accounts.list_for(none_pending, CHECKER, SUPPLIER)
    assert out["contact_changes"] == {"days": None, "items": []}
    assert none_pending.sql("FROM booking.audit_logs") == []


@pytest.mark.asyncio
async def test_enter_needs_the_right_and_the_key() -> None:
    with pytest.raises(Forbidden):
        await bank_accounts.enter(ScriptedConn(), SALES, FakeKms(), SUPPLIER, GOOD)
    with pytest.raises(Unavailable):
        await bank_accounts.enter(ScriptedConn(), ENTERER, None, SUPPLIER, GOOD)
    pending = decide_conn().on("AND status = 'pending'", 1)
    with pytest.raises(Conflict) as err:
        await bank_accounts.enter(pending, ENTERER, FakeKms(), SUPPLIER, GOOD)
    assert err.value.code == "bank_change_pending"


@pytest.mark.asyncio
async def test_enter_writes_ciphertext_and_audits_only_the_last4() -> None:
    kms = FakeKms()
    conn = decide_conn()
    out = await bank_accounts.enter(conn, ENTERER, kms, SUPPLIER, GOOD)
    assert out["account_masked"] == "•••• 1234"
    (insert,) = conn.sql("INSERT INTO booking.supplier_bank_account")
    row_id, supplier_id, *clear, ciphertext, wrapped, key_id, alg, reason, by = insert
    assert supplier_id == SUPPLIER and clear == ["Harbourline Bank", "VN", "USD", "1234"]
    assert b"VN12345678901234" not in ciphertext and key_id == "arn:test:key/1" and alg == "AES-256-GCM"
    assert by == ENTERER.cis_user_id and reason == "New details by letter"
    opened = await bank_crypto.open_sealed(kms, SUPPLIER, row_id, ciphertext, wrapped)
    assert opened["account_number"] == "VN12345678901234"
    audit_text = json.dumps([list(map(str, a)) for a in conn.sql("INSERT INTO booking.audit_logs")])
    assert "VN12345678901234" not in audit_text and "Kestrel Lodges" not in audit_text


@pytest.mark.asyncio
async def test_reveal_needs_the_read_right_and_audits() -> None:
    kms = FakeKms()
    sealed = await bank_crypto.seal(kms, SUPPLIER, ROW, {"account_number": "VN1234"})
    conn = decide_conn().on(
        "SELECT id::text AS id, supplier_id::text AS supplier_id, account_last4, ciphertext",
        {"id": ROW, "supplier_id": SUPPLIER, "account_last4": "1234", "ciphertext": sealed.ciphertext,
         "wrapped_key": sealed.wrapped_key},
    )  # fmt: skip
    with pytest.raises(Forbidden):
        await bank_accounts.reveal(conn, ENTERER, kms, ROW)
    assert await bank_accounts.reveal(conn, FIN, kms, ROW) == {"id": ROW, "account_number": "VN1234"}
    assert conn.sql("INSERT INTO booking.audit_logs")[0][3] == "supplier_bank_account.revealed"
    broken = decide_conn().on(
        "SELECT id::text AS id, supplier_id::text AS supplier_id, account_last4, ciphertext",
        {"id": OTHER, "supplier_id": SUPPLIER, "account_last4": "1234", "ciphertext": sealed.ciphertext,
         "wrapped_key": sealed.wrapped_key},
    )  # fmt: skip
    with pytest.raises(Conflict) as err:
        await bank_accounts.reveal(broken, FIN, kms, ROW)
    assert err.value.code == "bank_details_unreadable"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["withdrawn", "rejected", "superseded"])
async def test_reveal_refuses_a_closed_row(status: str) -> None:
    kms = FakeKms()
    conn = decide_conn(status=status)
    with pytest.raises(Conflict) as err:
        await bank_accounts.reveal(conn, FIN, kms, ROW)
    assert err.value.code == "bank_account_closed" and status in err.value.message
    assert kms.contexts == []  # no KMS Decrypt
    assert conn.sql("ciphertext") == [] and conn.sql("INSERT INTO booking.audit_logs") == []


# ── A 422 never echoes bank details (fix pass 2) ─────────────────────────────


PLANTED_NUMBER = "VN7766554433221100998877665544332211009988776655"  # doubled below: over the 64 limit
PLANTED_EXTRA = "planted-extra-field-value"


def _bank_client() -> TestClient:
    app = create_app(Settings(database_url=None, environment="test"), bank_key=FakeKms())
    app.dependency_overrides[current_staff] = lambda: CHECKER
    app.dependency_overrides[get_pool] = lambda: ScriptedConn()
    return TestClient(app)


def _assert_redacted(response: Any, *secrets: str) -> list[dict[str, Any]]:
    assert response.status_code == 422, response.text
    for secret in secrets:
        assert secret not in response.text
    detail = response.json()["detail"]
    assert detail and all(set(e) == {"loc", "type", "msg"} and e["msg"] == e["type"] for e in detail)
    return detail


@pytest.mark.parametrize(
    ("change", "field", "kind"),
    [
        ({"account_number": PLANTED_NUMBER * 2}, "account_number", "string_too_long"),
        ({"currency": "U$D-PLANTED"}, "currency", "string_pattern_mismatch"),
        ({"iban_full": PLANTED_EXTRA}, "iban_full", "extra_forbidden"),
        ({"account_holder": ""}, "account_holder", "string_too_short"),
    ],
)
def test_a_bad_entry_answers_field_names_and_types_only(
    change: dict[str, Any], field: str, kind: str, caplog: pytest.LogCaptureFixture
) -> None:
    body = {**GOOD, **change}
    caplog.set_level(logging.DEBUG)
    response = _bank_client().post(f"/booking/suppliers/{SUPPLIER}/bank-accounts", json=body)
    secrets = [str(v) for v in change.values() if v] + [GOOD["account_holder"], "3456-7890"]
    detail = _assert_redacted(response, *secrets)
    assert {"loc": ["body", field], "type": kind, "msg": kind} in detail
    logged = "\n".join(r.getMessage() for r in caplog.records)
    for secret in secrets:
        assert secret not in logged


def test_a_bad_decision_body_answers_field_names_and_types_only() -> None:
    note = "Called Mr Invented on +84 999 888 777 " * 20
    response = _bank_client().post(
        f"/booking/supplier-bank-accounts/{ROW}/decision", json={"action": "approve", "note": note, "x": "y-planted"}
    )
    detail = _assert_redacted(response, "+84 999 888 777", "y-planted")
    assert {tuple(e["loc"]) for e in detail} == {("body", "note"), ("body", "x")}


def test_a_body_that_is_not_json_is_redacted_too() -> None:
    response = _bank_client().post(
        f"/booking/suppliers/{SUPPLIER}/bank-accounts",
        content=b'{"account_number": "' + PLANTED_NUMBER.encode() + b'"',
        headers={"content-type": "application/json"},
    )
    _assert_redacted(response, PLANTED_NUMBER)


# ── Profile fields ───────────────────────────────────────────────────────────


def test_profile_normalize() -> None:
    out = profile.normalize(
        {
            "legal_entity": "  Kestrel Co ",
            "tax_code": "",
            "role_contacts": [{"role": "accounts", "email": " A@B.TEST "}],
            "payment_terms": {"basis": "deposit_balance", "deposit_percent": 30.0, "due_days": 7},
        }
    )
    assert out == {
        "legal_entity": "Kestrel Co",
        "tax_code": None,
        "role_contacts": [{"role": "accounts", "name": None, "email": "a@b.test", "phone": None}],
        "payment_terms": {"basis": "deposit_balance", "deposit_percent": "30", "due_days": 7, "method": None,
                          "note": None},
    }  # fmt: skip
    assert profile.normalize({"role_contacts": None, "payment_terms": None}) == {
        "role_contacts": [],
        "payment_terms": None,
    }


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"role_contacts": [{"role": "Head Chef", "name": "x"}]}, "invalid_role_contact"),
        ({"role_contacts": [{"role": "sales"}]}, "invalid_role_contact"),
        ({"role_contacts": [{"role": "sales", "email": "nope"}]}, "invalid_role_contact"),
        ({"role_contacts": [{"role": "sales", "name": "x"}] * 13}, "invalid_role_contacts"),
        ({"role_contacts": "x"}, "invalid_role_contacts"),
        ({"payment_terms": {"basis": "Barter!"}}, "invalid_payment_terms"),
        ({"payment_terms": {"basis": "prepayment", "deposit_percent": 20}}, "invalid_payment_terms"),
        ({"payment_terms": {"basis": "deposit_balance"}}, "invalid_payment_terms"),
        ({"payment_terms": {"basis": "deposit_balance", "deposit_percent": "abc"}}, "invalid_payment_terms"),
        ({"payment_terms": {"basis": "deposit_balance", "deposit_percent": 0}}, "invalid_payment_terms"),
        ({"payment_terms": {"basis": "after_service", "due_days": 400}}, "invalid_payment_terms"),
        ({"payment_terms": {"basis": "after_service", "due_days": True}}, "invalid_payment_terms"),
        ({"payment_terms": {"basis": "after_service", "method": "bar ter"}}, "invalid_payment_terms"),
        ({"legal_entity": "x" * 301}, "legal_entity_too_long"),
    ],
)
def test_profile_refuses(body: dict[str, Any], code: str) -> None:
    with pytest.raises(Invalid) as err:
        profile.normalize(dict(body))
    assert err.value.code == code


def test_profile_db_round_trip() -> None:
    assert profile.cast("destination_id") == "::uuid" and profile.cast("role_contacts") == "::jsonb"
    assert profile.cast("tax_code") == ""
    assert profile.db_value("payment_terms", None) is None
    assert profile.db_value("role_contacts", []) == "[]" and profile.db_value("tax_code", "1") == "1"
    assert profile.decode({"role_contacts": "[]", "payment_terms": '{"basis": "on_arrival"}'}) == {
        "role_contacts": [],
        "payment_terms": {"basis": "on_arrival"},
    }
