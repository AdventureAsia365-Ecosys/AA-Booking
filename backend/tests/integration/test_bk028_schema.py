"""AA-682 S3 (bk-028) on a real PostgreSQL, as role ``booking`` where the app writes: registration and re-run no-op,
grants (no DELETE on invoices / payments, column UPDATE only, cis_app reads nothing), the DM-6.1 tags in the same file,
and the database rules the API stands on:

- an invoice starts as an empty draft of a B2C case on its approved, priced version; send needs a schedule adding up
  to the total; the money states are derived: a direct ``UPDATE invoices SET status = 'paid'`` is refused;
- payments: recorded against a sent invoice only, maker-checker (verify by the recorder is refused by a CHECK),
  evidence required unless cash, one Idempotency-Key per invoice, void is final, a refund never takes the net paid
  below 0; the invoice settles (sent -> partially_paid -> paid, and back on a void);
- the amendment rule: after send lines are only appended; the schedule must still add up at commit (deferred check);
  a covered schedule row is frozen;
- the case money track: invoice_sent / payment_pending / paid must equal the invoice, never on a B2B case;
  pre_departure for B2C only once paid; reopen refused while an invoice is live;
- two concurrent verifies on one invoice end ``paid`` (the settle locks the invoice row first, critic B4);
- the views (schedule waterfall states, balance) and the check board's payment_verified;
- payment evidence files (ops_files_guard replaced): attached evidence is never removed, a verify re-checks that the
  evidence is an active file and takes it FOR SHARE (a concurrent removal waits, then is refused);
- the schedule guard locks the invoice first: a schedule change waiting on a verify then sees the row covered;
- Python's line amount rounding equals PostgreSQL round() (half away from zero).

The HTTP flows, the case auto-advance and the reminder job are in ``test_bk028_billing_api.py``."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from app.ops import money
from tests.integration import ops

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[3] / "migrations" / "028_invoices_payments.sql"
MAKER, CHECKER = "it-682-maker", "it-682-checker"
BUCKET = "aa-booking-dev-ops-docs-000000000000"


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    await ops.purge_ops(conn)
    await ops.staff(conn, (MAKER, "admin"), (CHECKER, "admin"))
    try:
        yield conn
    finally:
        await ops.purge_ops(conn)
        await conn.close()


@pytest_asyncio.fixture
async def app_conn(admin: asyncpg.Connection, migrated_booking_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_booking_dsn)
    try:
        yield conn
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def sold(admin: asyncpg.Connection) -> dict[str, Any]:
    sup = await ops.supply(admin)
    return await ops.sold_case(admin, sup, tag="bk028")


async def _invoice(conn: asyncpg.Connection, invoice_id: str) -> dict[str, Any]:
    row = await conn.fetchrow("SELECT status, total_amount FROM booking.invoices WHERE id = $1::uuid", invoice_id)
    return dict(row)


async def _case_status(conn: asyncpg.Connection, case_id: str, status: str) -> None:
    await conn.execute("UPDATE booking.trip_cases SET status = $2 WHERE id = $1::uuid", case_id, status)


async def _file(conn: asyncpg.Connection, payment_id: str, case_id: str) -> str:
    fid = str(uuid.uuid4())
    await conn.execute(
        "INSERT INTO booking.ops_files (id, kind, entity_type, entity_id, trip_case_id, s3_bucket, s3_key,"
        " content_type, bytes, sha256, file_name, uploaded_by)"
        " VALUES ($1::uuid, 'payment_evidence', 'payment', $2::uuid, $3::uuid,"
        " $4, $5, 'application/pdf', 10, $6, 'slip.pdf', $7)",
        fid,
        payment_id,
        case_id,
        BUCKET,
        f"ops-docs/payment_evidence/{fid}.pdf",
        uuid.uuid4().hex + uuid.uuid4().hex,
        MAKER,
    )
    return fid


async def test_registered_rerunnable_and_grants(admin: asyncpg.Connection) -> None:
    await admin.execute("SET ROLE bk_owner")
    try:
        await admin.execute(MIGRATION.read_text(encoding="utf-8"))  # a second run is a no-op
    finally:
        await admin.execute("RESET ROLE")
    assert await admin.fetchval("SELECT count(*) FROM shared.schema_versions WHERE version = 'bk-028'") == 1
    rels = (
        "invoices",
        "invoice_items",
        "payment_schedule",
        "payments",
        "v_payment_schedule_status",
        "v_invoice_balance",
    )
    for rel in rels:
        owner = await admin.fetchval(
            f"SELECT relowner::regrole::text FROM pg_class WHERE oid = 'booking.{rel}'::regclass"
        )
        assert owner == "bk_owner", rel
        assert not await admin.fetchval("SELECT has_table_privilege('booking', $1, 'TRUNCATE')", f"booking.{rel}")
        assert not await admin.fetchval("SELECT has_table_privilege('cis_app', $1, 'SELECT')", f"booking.{rel}")
    for rel, can_delete in (
        ("invoices", False),
        ("payments", False),
        ("invoice_items", True),
        ("payment_schedule", True),
        ("v_invoice_balance", False),
    ):
        assert (
            await admin.fetchval("SELECT has_table_privilege('booking', $1, 'DELETE')", f"booking.{rel}") is can_delete
        ), rel
    for table, col, allowed in (
        ("invoices", "status", True),
        ("invoices", "total_amount", True),
        ("invoices", "currency", False),
        ("invoices", "customer_id", False),
        ("payments", "verified_by", True),
        ("payments", "amount", False),
        ("payments", "recorded_by", False),
        ("payments", "kind", False),
    ):
        assert (
            await admin.fetchval("SELECT has_column_privilege('booking', $1, $2, 'UPDATE')", f"booking.{table}", col)
            is allowed
        ), (table, col)
    settings = {r["key"]: r["value"] for r in await admin.fetch("SELECT key, value FROM booking.settings")}
    assert {
        "deposit_due_days",
        "balance_due_days_before_start",
        "payment_due_soon_days",
        "payment_overdue_reminder_days",
        "invoice_issuer",
        "payment_instructions",
    } <= set(settings)
    edges = {
        (r["from_status"], r["to_status"])
        for r in await admin.fetch("SELECT from_status, to_status FROM booking.trip_case_status_transitions")
    }
    assert {
        ("confirmed", "invoice_sent"),
        ("payment_pending", "paid"),
        ("paid", "payment_pending"),
        ("paid", "pre_departure"),
        ("paid", "cancelled"),
    } <= edges
    assert ("on_trip", "paid") not in edges and ("completed", "cancelled") not in edges


async def test_tags_in_the_same_file(admin: asyncpg.Connection) -> None:
    comments = {
        r["col"]: r["c"]
        for r in await admin.fetch(
            "SELECT c.relname || '.' || a.attname AS col, col_description(a.attrelid, a.attnum) AS c"
            " FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid"
            " WHERE c.relnamespace = 'booking'::regnamespace"
            " AND c.relname IN ('invoices', 'invoice_items', 'payments') AND a.attnum > 0"
        )
    }
    assert comments["invoices.bill_to_name"].startswith("pii:name")
    assert comments["invoices.bill_to_email"].startswith("pii:contact")
    assert comments["invoices.bill_to_address"].startswith("pii:address")
    for col in (
        "invoices.notes",
        "invoices.terms",
        "invoices.cancel_reason",
        "invoice_items.description",
        "invoice_items.reason",
        "payments.note",
        "payments.void_reason",
    ):
        assert comments[col].startswith("pii:free_text"), col
    assert comments["payments.reference"].startswith("pii:payment")


async def test_money_states_are_derived(app_conn: asyncpg.Connection, sold: dict[str, Any]) -> None:
    """Draft -> send needs a schedule adding up; the status of a sent invoice follows verified money only."""
    inv = await ops.draft_invoice(app_conn, sold, by=MAKER, schedule=(("deposit", "720", date.today()),))
    assert await _invoice(app_conn, inv["id"]) == {"status": "draft", "total_amount": Decimal("2400.00")}
    with pytest.raises(asyncpg.CheckViolationError, match="adds up to 720.00 but the total is 2400.00"):
        await ops.send_invoice(app_conn, inv["id"], by=MAKER)
    await app_conn.execute(
        "INSERT INTO booking.payment_schedule (invoice_id, seq, kind, amount, due_date)"
        " VALUES ($1::uuid, 2, 'final', 1680, $2)",
        inv["id"],
        ops.START - timedelta(days=30),
    )
    # No payment on a draft.
    with pytest.raises(asyncpg.CheckViolationError, match="against a sent invoice only"):
        await ops.record_payment(app_conn, inv["id"], "720", by=MAKER)
    await ops.send_invoice(app_conn, inv["id"], by=MAKER)
    # Never "paid" without recorded, verified money (Done-when: direct UPDATE as booking -> error).
    for status in ("paid", "partially_paid"):
        with pytest.raises(asyncpg.CheckViolationError, match="does not match its verified payments"):
            await app_conn.execute("UPDATE booking.invoices SET status = $2 WHERE id = $1::uuid", inv["id"], status)
    with pytest.raises(asyncpg.CheckViolationError, match="is not the sum of its lines"):
        await app_conn.execute("UPDATE booking.invoices SET total_amount = 100 WHERE id = $1::uuid", inv["id"])
    with pytest.raises(asyncpg.CheckViolationError, match="snapshot and issue date are fixed"):
        await app_conn.execute("UPDATE booking.invoices SET bill_to_name = 'Other' WHERE id = $1::uuid", inv["id"])
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app_conn.execute("UPDATE booking.invoices SET currency = 'EUR' WHERE id = $1::uuid", inv["id"])
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app_conn.execute("DELETE FROM booking.invoices WHERE id = $1::uuid", inv["id"])

    deposit = await ops.record_payment(app_conn, inv["id"], "720", by=MAKER, method="bank_transfer")
    assert (await _invoice(app_conn, inv["id"]))["status"] == "sent"  # recorded is not money yet
    with pytest.raises(asyncpg.CheckViolationError, match="maker-checker"):
        await ops.verify_payment(app_conn, deposit, by=MAKER)
    with pytest.raises(asyncpg.CheckViolationError, match="needs the evidence file"):
        await ops.verify_payment(app_conn, deposit, by=CHECKER)
    evidence = await _file(app_conn, deposit, sold["id"])
    await app_conn.execute(
        "UPDATE booking.payments SET evidence_file_id = $2::uuid WHERE id = $1::uuid", deposit, evidence
    )
    await ops.verify_payment(app_conn, deposit, by=CHECKER)
    assert (await _invoice(app_conn, inv["id"]))["status"] == "partially_paid"
    # A verified payment changes only by a void; its evidence stays.
    with pytest.raises(asyncpg.CheckViolationError, match="only a void changes it"):
        await app_conn.execute("UPDATE booking.payments SET note = 'x' WHERE id = $1::uuid", deposit)
    with pytest.raises(asyncpg.CheckViolationError, match="evidence of a verified payment"):
        await app_conn.execute(
            "UPDATE booking.ops_files SET status = 'removed', removed_reason = 'x', removed_at = now(),"
            " removed_by = $2 WHERE id = $1::uuid",
            evidence,
            MAKER,
        )
    with pytest.raises(asyncpg.CheckViolationError, match="evidence is fixed"):
        await _file(app_conn, deposit, sold["id"])

    balance = await ops.record_payment(app_conn, inv["id"], "1680", by=MAKER)
    await ops.verify_payment(app_conn, balance, by=CHECKER)  # cash: no evidence needed
    assert (await _invoice(app_conn, inv["id"]))["status"] == "paid"
    # Void the balance: back to partially_paid; void is final.
    await app_conn.execute(
        "UPDATE booking.payments SET voided_at = now(), voided_by = $2, void_reason = 'bounced' WHERE id = $1::uuid",
        balance,
        CHECKER,
    )
    assert (await _invoice(app_conn, inv["id"]))["status"] == "partially_paid"
    with pytest.raises(asyncpg.CheckViolationError, match="voided; it is final"):
        await app_conn.execute("UPDATE booking.payments SET note = 'again' WHERE id = $1::uuid", balance)
    # One Idempotency-Key per invoice.
    with pytest.raises(asyncpg.UniqueViolationError, match="payments_idempotency_uq"):
        await ops.record_payment(app_conn, inv["id"], "5", by=MAKER, tag="same-key")
        await ops.record_payment(app_conn, inv["id"], "6", by=MAKER, tag="same-key")


async def test_refund_and_cancel(app_conn: asyncpg.Connection, sold: dict[str, Any]) -> None:
    inv = await ops.draft_invoice(app_conn, sold, by=MAKER)
    await ops.send_invoice(app_conn, inv["id"], by=MAKER)
    paid = await ops.record_payment(app_conn, inv["id"], "720", by=MAKER)
    await ops.verify_payment(app_conn, paid, by=CHECKER)
    too_much = await ops.record_payment(app_conn, inv["id"], "800", by=MAKER, kind="refund")
    with pytest.raises(asyncpg.CheckViolationError, match="refund cannot take the net paid below zero"):
        await ops.verify_payment(app_conn, too_much, by=CHECKER)
    cancel = "UPDATE booking.invoices SET status = 'cancelled', cancelled_at = now(), cancel_reason = 'trip off'"
    # partially_paid always holds net paid > 0: no edge to cancelled (review S253), refund back to sent first.
    with pytest.raises(asyncpg.CheckViolationError, match="illegal invoice status transition partially_paid -> cancel"):
        await app_conn.execute(cancel + " WHERE id = $1::uuid", inv["id"])
    refund = await ops.record_payment(app_conn, inv["id"], "720", by=MAKER, kind="refund")
    await ops.verify_payment(app_conn, refund, by=CHECKER)
    assert (await _invoice(app_conn, inv["id"]))["status"] == "sent"
    with pytest.raises(asyncpg.CheckViolationError, match="waiting for verification"):
        await app_conn.execute(cancel + " WHERE id = $1::uuid", inv["id"])
    await app_conn.execute(
        "UPDATE booking.payments SET voided_at = now(), voided_by = $2, void_reason = 'typo' WHERE id = $1::uuid",
        too_much,
        CHECKER,
    )
    await app_conn.execute(cancel + " WHERE id = $1::uuid", inv["id"])
    assert (await _invoice(app_conn, inv["id"]))["status"] == "cancelled"
    with pytest.raises(asyncpg.CheckViolationError, match="cancelled; it is final"):
        await app_conn.execute("UPDATE booking.invoices SET notes = 'x' WHERE id = $1::uuid", inv["id"])
    # A new invoice may follow once the old one is cancelled (one live invoice per case).
    again = await ops.draft_invoice(app_conn, sold, by=MAKER, tag="inv2")
    with pytest.raises(asyncpg.UniqueViolationError, match="invoices_live_per_case_uq"):
        await ops.draft_invoice(app_conn, sold, by=MAKER, tag="inv3")
    # A draft is soft-deleted, then fixed.
    await app_conn.execute("UPDATE booking.invoices SET deleted_at = now() WHERE id = $1::uuid", again["id"])
    with pytest.raises(asyncpg.CheckViolationError, match="was deleted"):
        await app_conn.execute(
            "DELETE FROM booking.invoice_items WHERE invoice_id = $1::uuid AND kind = 'version_total'", again["id"]
        )


async def test_void_keeps_the_money_invariants(app_conn: asyncpg.Connection, sold: dict[str, Any]) -> None:
    """Review S253: a void never takes the net paid below 0 and never touches a cancelled invoice's payments."""
    void = "UPDATE booking.payments SET voided_at = now(), voided_by = $2, void_reason = 'x' WHERE id = $1::uuid"
    cancel = "UPDATE booking.invoices SET status = 'cancelled', cancelled_at = now(), cancel_reason = 'trip off'"
    inv = await ops.draft_invoice(app_conn, sold, by=MAKER)
    await ops.send_invoice(app_conn, inv["id"], by=MAKER)
    receipt = await ops.record_payment(app_conn, inv["id"], "720", by=MAKER)
    await ops.verify_payment(app_conn, receipt, by=CHECKER)
    refund = await ops.record_payment(app_conn, inv["id"], "720", by=MAKER, kind="refund")
    await ops.verify_payment(app_conn, refund, by=CHECKER)
    net = "SELECT booking.invoice_net_paid($1::uuid)"
    assert await app_conn.fetchval(net, inv["id"]) == 0
    # Sent invoice, net 0: voiding the verified receipt would leave -720.
    with pytest.raises(asyncpg.CheckViolationError, match="below zero; void the refund first"):
        await app_conn.execute(void, receipt, CHECKER)
    # Void the refund first (net back to 720, partially_paid), then the receipt (net 0, sent); rolled back so the
    # cancelled case below starts again from the verified receipt + refund.
    tx = app_conn.transaction()
    await tx.start()
    await app_conn.execute(void, refund, CHECKER)
    assert (await _invoice(app_conn, inv["id"]))["status"] == "partially_paid"
    await app_conn.execute(void, receipt, CHECKER)
    assert (await _invoice(app_conn, inv["id"]))["status"] == "sent"
    assert await app_conn.fetchval(net, inv["id"]) == 0
    await tx.rollback()

    # Cancelled invoice (net 0 from receipt + refund): its payments are final, either way round.
    await app_conn.execute(cancel + " WHERE id = $1::uuid", inv["id"])
    for payment in (receipt, refund):
        with pytest.raises(asyncpg.CheckViolationError, match="its invoice is cancelled; its payments are final"):
            await app_conn.execute(void, payment, CHECKER)
    assert await app_conn.fetchval(net, inv["id"]) == 0


async def test_lines_and_schedule_after_send(app_conn: asyncpg.Connection, sold: dict[str, Any]) -> None:
    """Draft lines change freely (but the version total line); after send: append only, the schedule still adds up at
    commit, covered rows are frozen, and an amendment can move a paid invoice back to partially_paid."""
    inv = await ops.draft_invoice(
        app_conn,
        sold,
        by=MAKER,
        adjustments=(("Discount", "-100"),),
        schedule=(("deposit", "690", date.today()), ("final", "1610", ops.START)),
    )
    assert (await _invoice(app_conn, inv["id"]))["total_amount"] == Decimal("2300.00")
    with pytest.raises(asyncpg.CheckViolationError, match="version total line is locked"):
        await app_conn.execute(
            "UPDATE booking.invoice_items SET description = 'x' WHERE invoice_id = $1::uuid AND kind = 'version_total'",
            inv["id"],
        )
    with pytest.raises(asyncpg.CheckViolationError, match="carries the approved version total"):
        await app_conn.execute(
            "INSERT INTO booking.invoice_items (invoice_id, position, kind, description, unit_price, amount)"
            " VALUES ($1::uuid, 9, 'version_total', 'x', 1, 1)",
            inv["id"],
        )
    with pytest.raises(asyncpg.CheckViolationError, match="invoice_items_amount_chk"):
        await app_conn.execute(
            "INSERT INTO booking.invoice_items (invoice_id, position, kind, description, quantity, unit_price, amount,"
            " reason) VALUES ($1::uuid, 9, 'adjustment', 'x', 2, 10, 25, 'r')",
            inv["id"],
        )
    await app_conn.execute(
        "DELETE FROM booking.invoice_items WHERE invoice_id = $1::uuid AND kind = 'adjustment'", inv["id"]
    )
    await app_conn.execute(
        "UPDATE booking.payment_schedule SET amount = 1710 WHERE invoice_id = $1::uuid AND seq = 2", inv["id"]
    )
    await ops.send_invoice(app_conn, inv["id"], by=MAKER)
    with pytest.raises(asyncpg.CheckViolationError, match="only appended as adjustments"):
        await app_conn.execute(
            "DELETE FROM booking.invoice_items WHERE invoice_id = $1::uuid AND kind = 'version_total'", inv["id"]
        )
    # An appended line without a schedule change fails at COMMIT (deferred check).
    with pytest.raises(asyncpg.CheckViolationError, match="schedule adds up to 2400.00 but the total is 2500.00"):
        async with app_conn.transaction():
            await app_conn.execute(
                "INSERT INTO booking.invoice_items (invoice_id, position, kind, description, unit_price, amount,"
                " reason) VALUES ($1::uuid, 3, 'adjustment', 'Extra night', 100, 100, 'guest asked')",
                inv["id"],
            )
    # Pay everything, then amend: the invoice drops back to partially_paid.
    for amount in ("690", "1710"):
        pid = await ops.record_payment(app_conn, inv["id"], amount, by=MAKER)
        await ops.verify_payment(app_conn, pid, by=CHECKER)
    assert (await _invoice(app_conn, inv["id"]))["status"] == "paid"
    with pytest.raises(asyncpg.CheckViolationError, match="covered by verified payments"):
        await app_conn.execute(
            "UPDATE booking.payment_schedule SET due_date = due_date + 1 WHERE invoice_id = $1::uuid AND seq = 2",
            inv["id"],
        )
    with pytest.raises(asyncpg.CheckViolationError, match="comes after the rows already paid"):
        async with app_conn.transaction():
            await app_conn.execute(
                "INSERT INTO booking.payment_schedule (invoice_id, seq, kind, amount, due_date)"
                " VALUES ($1::uuid, 3, 'other', 100, $2)",
                inv["id"],
                date.today() - timedelta(days=1),
            )
    async with app_conn.transaction():
        await app_conn.execute(
            "INSERT INTO booking.invoice_items (invoice_id, position, kind, description, unit_price, amount, reason)"
            " VALUES ($1::uuid, 3, 'adjustment', 'Extra night', 100, 100, 'guest asked')",
            inv["id"],
        )
        await app_conn.execute(
            "INSERT INTO booking.payment_schedule (invoice_id, seq, kind, amount, due_date)"
            " VALUES ($1::uuid, 3, 'other', 100, $2)",
            inv["id"],
            ops.START,
        )
    assert await _invoice(app_conn, inv["id"]) == {"status": "partially_paid", "total_amount": Decimal("2500.00")}


async def test_case_money_track(app_conn: asyncpg.Connection, sold: dict[str, Any]) -> None:
    """Case money statuses equal the invoice; B2C pre_departure only once paid; reopen refused while live."""
    with pytest.raises(asyncpg.CheckViolationError, match="no sent invoice"):
        await _case_status(app_conn, sold["id"], "invoice_sent")
    inv = await ops.draft_invoice(app_conn, sold, by=MAKER)
    await ops.send_invoice(app_conn, inv["id"], by=MAKER)
    with pytest.raises(asyncpg.CheckViolationError, match="illegal trip case status transition confirmed -> paid"):
        await _case_status(app_conn, sold["id"], "paid")
    await _case_status(app_conn, sold["id"], "invoice_sent")
    with pytest.raises(asyncpg.CheckViolationError, match="must be what its invoice says"):
        await _case_status(app_conn, sold["id"], "paid")  # the edge exists, the money does not
    with pytest.raises(asyncpg.CheckViolationError, match="once its invoice is cancelled"):
        await _case_status(app_conn, sold["id"], "confirmed")
    pid = await ops.record_payment(app_conn, inv["id"], "720", by=MAKER)
    await ops.verify_payment(app_conn, pid, by=CHECKER)
    await _case_status(app_conn, sold["id"], "payment_pending")
    with pytest.raises(asyncpg.CheckViolationError, match="must be what its invoice says"):
        await _case_status(app_conn, sold["id"], "invoice_sent")  # edge exists, fact does not
    pid = await ops.record_payment(app_conn, inv["id"], "1680", by=MAKER)
    await ops.verify_payment(app_conn, pid, by=CHECKER)
    await _case_status(app_conn, sold["id"], "paid")
    # Supply is still confirming (no supplier booking confirmed): no pre_departure yet.
    with pytest.raises(asyncpg.CheckViolationError, match="every supplier booking confirmed"):
        await _case_status(app_conn, sold["id"], "pre_departure")
    board = await app_conn.fetchrow(
        "SELECT payment_verified FROM booking.v_trip_check_board WHERE trip_case_id = $1::uuid", sold["id"]
    )
    assert board["payment_verified"] is True
    # Cancel the case (staff), then reopen: refused while the invoice is live.
    await _case_status(app_conn, sold["id"], "cancelled")
    with pytest.raises(asyncpg.CheckViolationError, match="cancel its invoice before reopening"):
        await _case_status(app_conn, sold["id"], "new_inquiry")


async def test_b2b_has_no_invoice(app_conn: asyncpg.Connection) -> None:
    case = await ops.b2b_case(app_conn, tag="b2b-028")
    with pytest.raises(asyncpg.CheckViolationError, match="no customer invoice on a B2B trip case"):
        await app_conn.execute(
            "INSERT INTO booking.invoices (trip_case_id, version_id, bill_to_kind, customer_id, currency,"
            " idempotency_key, request_sha256) VALUES ($1::uuid, gen_random_uuid(), 'customer', gen_random_uuid(),"
            " 'USD', 'k-123456', $2)",
            case["id"],
            ops.SHA,
        )
    with pytest.raises(asyncpg.CheckViolationError, match="a B2B case has no money status"):
        await _case_status(app_conn, case["id"], "invoice_sent")
    board = await app_conn.fetchrow(
        "SELECT payment_verified FROM booking.v_trip_check_board WHERE trip_case_id = $1::uuid", case["id"]
    )
    assert board["payment_verified"] is None


async def test_unpriced_version_refused(admin: asyncpg.Connection, app_conn: asyncpg.Connection) -> None:
    sup = await ops.supply(admin, "Echo")
    case = await ops.sold_case(admin, sup, tag="bk028-np")
    await admin.execute("SET ROLE bk_owner")
    try:
        await admin.execute("ALTER TABLE booking.trip_case_versions DISABLE TRIGGER USER")
        await admin.execute(
            "UPDATE booking.trip_case_versions SET total_price = NULL WHERE id = $1::uuid", case["version"]
        )
    finally:
        await admin.execute("ALTER TABLE booking.trip_case_versions ENABLE TRIGGER USER")
        await admin.execute("RESET ROLE")
    with pytest.raises(asyncpg.CheckViolationError, match="has no price"):
        await ops.draft_invoice(app_conn, case, by=MAKER)


async def test_concurrent_verifies_end_paid(
    app_conn: asyncpg.Connection, sold: dict[str, Any], migrated_booking_dsn: str
) -> None:
    """Critic B4: two verifies of one invoice in parallel. payments_guard takes the invoice row FOR UPDATE before
    anything else, so the second verify waits on that lock until the first commits; its settle then sums with the
    first one's committed payment: the invoice ends paid, not partially_paid."""
    inv = await ops.draft_invoice(app_conn, sold, by=MAKER)
    await ops.send_invoice(app_conn, inv["id"], by=MAKER)
    one = await ops.record_payment(app_conn, inv["id"], "720", by=MAKER)
    two = await ops.record_payment(app_conn, inv["id"], "1680", by=MAKER)
    first, second = await asyncpg.connect(migrated_booking_dsn), await asyncpg.connect(migrated_booking_dsn)
    try:
        tx1, tx2 = first.transaction(), second.transaction()
        await tx1.start()
        await tx2.start()
        await ops.verify_payment(first, one, by=CHECKER)  # holds the invoice lock
        blocked = asyncio.create_task(ops.verify_payment(second, two, by=CHECKER))
        await asyncio.sleep(0.3)
        assert not blocked.done()  # waiting for the invoice row
        await tx1.commit()
        await blocked
        await tx2.commit()
    finally:
        await first.close()
        await second.close()
    assert (await _invoice(app_conn, inv["id"]))["status"] == "paid"


REMOVE_FILE = (
    "UPDATE booking.ops_files SET status = 'removed', removed_reason = 'x', removed_at = now(), removed_by = $2"
    " WHERE id = $1::uuid"
)


async def test_evidence_must_stay_active_until_verify(
    admin: asyncpg.Connection, app_conn: asyncpg.Connection, sold: dict[str, Any]
) -> None:
    """Review S253 (blocker): a payment never verifies on removed evidence. The file a non-voided payment points at
    is never removed; if it were (guard bypassed), the verify re-checks it is active; detaching works the normal way."""
    inv = await ops.draft_invoice(app_conn, sold, by=MAKER)
    await ops.send_invoice(app_conn, inv["id"], by=MAKER)
    attach = "UPDATE booking.payments SET evidence_file_id = $2::uuid WHERE id = $1::uuid"

    # 1. Unverified, non-voided payment: its attached evidence is not removed.
    pid = await ops.record_payment(app_conn, inv["id"], "720", by=MAKER, method="bank_transfer")
    first = await _file(app_conn, pid, sold["id"])
    await app_conn.execute(attach, pid, first)
    with pytest.raises(asyncpg.CheckViolationError, match="attached evidence of a payment; detach or replace"):
        await app_conn.execute(REMOVE_FILE, first, MAKER)

    # 2. Removed anyway (guard bypassed, as a pre-fix row or a direct owner edit would be): verify is refused.
    await admin.execute("SET ROLE bk_owner")
    try:
        await admin.execute("ALTER TABLE booking.ops_files DISABLE TRIGGER USER")
        await admin.execute(REMOVE_FILE, first, MAKER)
    finally:
        await admin.execute("ALTER TABLE booking.ops_files ENABLE TRIGGER USER")
        await admin.execute("RESET ROLE")
    with pytest.raises(asyncpg.CheckViolationError, match="is not an active evidence file of payment"):
        await ops.verify_payment(app_conn, pid, by=CHECKER)

    # 3. The normal way: detach, then remove (a replaced file is no longer attached); verify then needs new evidence.
    second = await _file(app_conn, pid, sold["id"])
    await app_conn.execute(attach, pid, second)
    await app_conn.execute(attach, pid, None)
    await app_conn.execute(REMOVE_FILE, second, MAKER)
    with pytest.raises(asyncpg.CheckViolationError, match="needs the evidence file"):
        await ops.verify_payment(app_conn, pid, by=CHECKER)
    third = await _file(app_conn, pid, sold["id"])
    await app_conn.execute(attach, pid, third)
    await ops.verify_payment(app_conn, pid, by=CHECKER)
    assert (await _invoice(app_conn, inv["id"]))["status"] == "partially_paid"

    # 4. A voided payment is final: its attached evidence may go.
    gone = await ops.record_payment(app_conn, inv["id"], "100", by=MAKER, method="bank_transfer")
    slip = await _file(app_conn, gone, sold["id"])
    await app_conn.execute(attach, gone, slip)
    await app_conn.execute(
        "UPDATE booking.payments SET voided_at = now(), voided_by = $2, void_reason = 'typo' WHERE id = $1::uuid",
        gone,
        CHECKER,
    )
    await app_conn.execute(REMOVE_FILE, slip, MAKER)


async def test_concurrent_evidence_removal_waits_for_the_verify(
    app_conn: asyncpg.Connection, sold: dict[str, Any], migrated_booking_dsn: str
) -> None:
    """Review S253: the verify takes its evidence file FOR SHARE. A removal started meanwhile waits for the verify's
    commit, then sees a verified payment and is refused; the reverse order (detach + remove first) leaves the waiting
    verify without evidence."""
    inv = await ops.draft_invoice(app_conn, sold, by=MAKER)
    await ops.send_invoice(app_conn, inv["id"], by=MAKER)
    attach = "UPDATE booking.payments SET evidence_file_id = $2::uuid WHERE id = $1::uuid"
    pid = await ops.record_payment(app_conn, inv["id"], "720", by=MAKER, method="bank_transfer")
    fid = await _file(app_conn, pid, sold["id"])
    await app_conn.execute(attach, pid, fid)
    other = await ops.record_payment(app_conn, inv["id"], "100", by=MAKER, method="bank_transfer")
    other_fid = await _file(app_conn, other, sold["id"])
    await app_conn.execute(attach, other, other_fid)

    first, second = await asyncpg.connect(migrated_booking_dsn), await asyncpg.connect(migrated_booking_dsn)
    try:
        # Verify first: the removal waits on the file row, then is refused.
        tx1, tx2 = first.transaction(), second.transaction()
        await tx1.start()
        await tx2.start()
        await ops.verify_payment(first, pid, by=CHECKER)
        blocked = asyncio.create_task(second.execute(REMOVE_FILE, fid, MAKER))
        await asyncio.sleep(0.3)
        assert not blocked.done()  # waiting for the verify's FOR SHARE lock
        await tx1.commit()
        with pytest.raises(asyncpg.CheckViolationError, match="evidence of a verified payment"):
            await blocked
        await tx2.rollback()

        # Detach + remove first: the verify waits on the invoice row, then finds no evidence.
        tx1, tx2 = first.transaction(), second.transaction()
        await tx1.start()
        await tx2.start()
        await first.execute(attach, other, None)
        await first.execute(REMOVE_FILE, other_fid, MAKER)
        blocked = asyncio.create_task(ops.verify_payment(second, other, by=CHECKER))
        await asyncio.sleep(0.3)
        assert not blocked.done()  # waiting for the payment row the detach changed
        await tx1.commit()
        with pytest.raises(asyncpg.CheckViolationError, match="needs the evidence file"):
            await blocked
        await tx2.rollback()
    finally:
        await first.close()
        await second.close()
    assert (await _invoice(app_conn, inv["id"]))["status"] == "partially_paid"


async def test_schedule_change_waits_for_a_verify_and_then_sees_the_row_covered(
    app_conn: asyncpg.Connection, sold: dict[str, Any], migrated_booking_dsn: str
) -> None:
    """Review S253: payment_schedule_guard locks the invoice row first. A schedule change started while a verify is
    open waits for it, then reads the committed net paid: the row the verify covered stays frozen (without the lock it
    read net 0 and changed a row that was being paid)."""
    inv = await ops.draft_invoice(app_conn, sold, by=MAKER)
    await ops.send_invoice(app_conn, inv["id"], by=MAKER)
    deposit = await ops.record_payment(app_conn, inv["id"], "720", by=MAKER)
    move = "UPDATE booking.payment_schedule SET due_date = due_date + 1 WHERE invoice_id = $1::uuid AND seq = 1"
    first, second = await asyncpg.connect(migrated_booking_dsn), await asyncpg.connect(migrated_booking_dsn)
    try:
        tx1, tx2 = first.transaction(), second.transaction()
        await tx1.start()
        await tx2.start()
        await ops.verify_payment(first, deposit, by=CHECKER)  # holds the invoice row
        blocked = asyncio.create_task(second.execute(move, inv["id"]))
        await asyncio.sleep(0.3)
        assert not blocked.done()  # payment_schedule_guard waits for the invoice row
        await tx1.commit()
        with pytest.raises(asyncpg.CheckViolationError, match="covered by verified payments"):
            await blocked
        await tx2.rollback()
    finally:
        await first.close()
        await second.close()
    # Sequentially too: the covered deposit row stays frozen, the uncovered final row may still move.
    with pytest.raises(asyncpg.CheckViolationError, match="covered by verified payments"):
        await app_conn.execute(move, inv["id"])
    await app_conn.execute(
        "UPDATE booking.payment_schedule SET due_date = due_date + 1 WHERE invoice_id = $1::uuid AND seq = 2",
        inv["id"],
    )


async def test_python_line_amount_matches_postgres_round(app_conn: asyncpg.Connection) -> None:
    """Review S253: invoice_items_amount_chk is amount = round(quantity * unit_price, 2); the API computes the amount
    with money.line_amount (ROUND_HALF_UP = half away from zero, like PostgreSQL numeric round), ties included."""
    pairs = [
        (Decimal(q), Decimal(u))
        for q in ("0.5", "1.5", "2.5", "0.25", "3", "1.01", "9999")
        for u in ("0.01", "-0.01", "0.05", "-0.05", "33.33", "-33.33", "0.03", "-0.07", "12345.67")
    ]
    rows = await app_conn.fetch(
        "SELECT q, u, round(q * u, 2) AS r FROM unnest($1::numeric[], $2::numeric[]) AS t(q, u)",
        [q for q, _ in pairs],
        [u for _, u in pairs],
    )
    mismatches = [(r["q"], r["u"], r["r"]) for r in rows if money.line_amount(r["q"], r["u"]) != r["r"]]
    assert mismatches == []


async def test_views_waterfall_and_balance(app_conn: asyncpg.Connection, sold: dict[str, Any]) -> None:
    today = date.today()
    inv = await ops.draft_invoice(
        app_conn,
        sold,
        by=MAKER,
        schedule=(
            ("deposit", "720", today - timedelta(days=2)),
            ("second", "680", today + timedelta(days=3)),
            ("final", "1000", ops.START),
        ),
    )
    states = await app_conn.fetch(
        "SELECT seq, state FROM booking.v_payment_schedule_status WHERE invoice_id = $1::uuid ORDER BY seq", inv["id"]
    )
    assert [r["state"] for r in states] == ["scheduled", "scheduled", "scheduled"]  # a draft is not due
    await ops.send_invoice(app_conn, inv["id"], by=MAKER)
    pid = await ops.record_payment(app_conn, inv["id"], "1000", by=MAKER)
    waiting = await ops.record_payment(app_conn, inv["id"], "50", by=MAKER, tag="waiting")
    await ops.verify_payment(app_conn, pid, by=CHECKER)
    rows = await app_conn.fetch(
        "SELECT seq, state, covered_amount, remaining_amount, days_overdue FROM booking.v_payment_schedule_status"
        " WHERE invoice_id = $1::uuid ORDER BY seq",
        inv["id"],
    )
    assert [(r["state"], r["covered_amount"], r["remaining_amount"]) for r in rows] == [
        ("paid", Decimal("720.00"), Decimal("0.00")),
        ("due", Decimal("280.00"), Decimal("400.00")),
        ("scheduled", Decimal("0.00"), Decimal("1000.00")),
    ]
    balance = await app_conn.fetchrow("SELECT * FROM booking.v_invoice_balance WHERE invoice_id = $1::uuid", inv["id"])
    assert balance["net_paid"] == Decimal("1000.00") and balance["balance"] == Decimal("1400.00")
    assert balance["unverified_amount"] == Decimal("50.00") and balance["unverified_count"] == 1
    assert balance["next_due_date"] == today + timedelta(days=3) and balance["overdue_amount"] == Decimal("0.00")
    await app_conn.execute(
        "UPDATE booking.payments SET voided_at = now(), voided_by = $2, void_reason = 'x' WHERE id = ANY($1::uuid[])",
        [pid, waiting],
        CHECKER,
    )
    rows = await app_conn.fetch(
        "SELECT state, days_overdue FROM booking.v_payment_schedule_status WHERE invoice_id = $1::uuid ORDER BY seq",
        inv["id"],
    )
    assert (rows[0]["state"], rows[0]["days_overdue"]) == ("overdue", 2)
    balance = await app_conn.fetchrow("SELECT * FROM booking.v_invoice_balance WHERE invoice_id = $1::uuid", inv["id"])
    assert balance["overdue_amount"] == Decimal("720.00")
    assert balance["oldest_overdue_date"] == today - timedelta(days=2)
