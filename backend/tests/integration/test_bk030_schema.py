"""AA-682 S4 (bk-030) on a real PostgreSQL, as role ``booking`` where the app writes: registration and re-run no-op,
grants (no DELETE on bills / payouts, lines insert + delete only, column UPDATE only, cis_app reads nothing), the
DM-6.1 tags in the same file, and the database rules the API stands on:

- a bill starts received; its lines name CONFIRMED bookings of the bill's supplier (and case); facts, lines and
  document change only while received; approve needs another person than the creator and than the last editor of
  the money facts or lines (``facts_edited_by``, maker-checker), >= 1 line and lines = amount;
- paid is derived: a direct ``UPDATE supplier_bills SET status = 'paid'`` is refused; payouts go on an approved bill,
  in its currency, by someone other than the bill's creator and its facts_edited_by, never above the amount; they
  settle the bill (paid when they reach the amount, back to approved on a void); a bill with a live payout is not
  cancelled; cancelled is final;
- one Idempotency-Key per bill for payouts; one bill number per supplier among bills not cancelled;
- files (ops_files_guard replaced): bill documents only while received and never on a bill touching a B2B case (and a
  bill with a document takes no B2B line), the document of an approved bill stays; payout evidence allowed on B2B,
  the evidence of a live payout stays (replaced, never removed);
- two concurrent payouts of the full amount: one wins, the other is refused (the bill row is locked first, critic B4);
- the view v_supplier_bill_balance.

The HTTP flows and the reminder are in ``test_bk030_supplier_bills_api.py``."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from tests.integration import ops
from tests.integration.conftest import apply_migrations

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[3] / "migrations" / "030_supplier_bills_payouts.sql"
MAKER, CHECKER, PAYER = "it-682-maker", "it-682-checker", "it-682-payer"
EDITOR = "it-682-editor"
BUCKET = "aa-booking-dev-ops-docs-000000000000"


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    await ops.purge_ops(conn)
    await ops.staff(conn, (MAKER, "contracting"), (CHECKER, "contracting"), (PAYER, "admin"), (EDITOR, "admin"))
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
async def world(admin: asyncpg.Connection) -> dict[str, Any]:
    """A sold B2C case with two confirmed bookings of supplier A, and one confirmed booking of supplier B."""
    sup = await ops.supply(admin, "Alpha")
    other = await ops.supply(admin, "Beta")
    case = await ops.sold_case(admin, sup, tag="bk030")
    room = await ops.confirmed_booking(admin, case, sup["supplier"], item=case["room_item"])
    kayak = await ops.confirmed_booking(admin, case, sup["supplier"], item=case["service_item"], net="80.00")
    foreign = await ops.confirmed_booking(admin, case, other["supplier"])
    return {"case": case, "sup": sup, "other": other, "room": room, "kayak": kayak, "foreign": foreign}


async def _status(conn: asyncpg.Connection, bill_id: str) -> tuple[str, Any]:
    row = await conn.fetchrow("SELECT status, paid_at FROM booking.supplier_bills WHERE id = $1::uuid", bill_id)
    return row["status"], row["paid_at"]


async def _file(conn: asyncpg.Connection, kind: str, entity_type: str, entity_id: str, case_id: str | None) -> str:
    fid = str(uuid.uuid4())
    await conn.execute(
        "INSERT INTO booking.ops_files (id, kind, entity_type, entity_id, trip_case_id, s3_bucket, s3_key,"
        " content_type, bytes, sha256, file_name, uploaded_by)"
        " VALUES ($1::uuid, $2, $3, $4::uuid, $5::uuid, $6, $7, 'application/pdf', 10, $8, 'bill.pdf', $9)",
        fid,
        kind,
        entity_type,
        entity_id,
        case_id,
        BUCKET,
        f"ops-docs/{kind}/{fid}.pdf",
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
    await apply_migrations(admin, only=range(31, 1000))
    assert await admin.fetchval("SELECT count(*) FROM shared.schema_versions WHERE version = 'bk-030'") == 1
    for rel in ("supplier_bills", "supplier_bill_items", "supplier_payouts", "v_supplier_bill_balance"):
        owner = await admin.fetchval(
            f"SELECT relowner::regrole::text FROM pg_class WHERE oid = 'booking.{rel}'::regclass"
        )
        assert owner == "bk_owner", rel
        assert not await admin.fetchval("SELECT has_table_privilege('booking', $1, 'TRUNCATE')", f"booking.{rel}")
        assert not await admin.fetchval("SELECT has_table_privilege('cis_app', $1, 'SELECT')", f"booking.{rel}")
    for rel, can_delete in (
        ("supplier_bills", False),
        ("supplier_payouts", False),
        ("supplier_bill_items", True),
        ("v_supplier_bill_balance", False),
    ):
        assert (
            await admin.fetchval("SELECT has_table_privilege('booking', $1, 'DELETE')", f"booking.{rel}") is can_delete
        ), rel
    assert not await admin.fetchval("SELECT has_table_privilege('booking', 'booking.supplier_bill_items', 'UPDATE')")
    for table, col, allowed in (
        ("supplier_bills", "status", True),
        ("supplier_bills", "amount", True),  # while received (guard)
        ("supplier_bills", "created_by", False),
        ("supplier_bills", "facts_edited_by", True),  # only to the editor of a received bill (guard)
        ("supplier_bills", "idempotency_key", False),
        ("supplier_payouts", "voided_at", True),
        ("supplier_payouts", "evidence_file_id", True),
        ("supplier_payouts", "amount", False),
        ("supplier_payouts", "recorded_by", False),
        ("supplier_payouts", "reference", False),
    ):
        assert (
            await admin.fetchval("SELECT has_column_privilege('booking', $1, $2, 'UPDATE')", f"booking.{table}", col)
            is allowed
        ), (table, col)
    edges = {
        (r["from_status"], r["to_status"])
        for r in await admin.fetch(
            "SELECT from_status, to_status FROM booking.ops_transitions WHERE entity = 'supplier_bill'"
        )
    }
    assert edges == {
        ("received", "approved"),
        ("received", "cancelled"),
        ("approved", "paid"),
        ("approved", "cancelled"),
        ("paid", "approved"),
    }
    assert await admin.fetchval("SELECT value FROM booking.settings WHERE key = 'supplier_bill_due_soon_days'") == "3"


async def test_tags_in_the_same_file(admin: asyncpg.Connection) -> None:
    comments = {
        r["col"]: r["c"]
        for r in await admin.fetch(
            "SELECT c.relname || '.' || a.attname AS col, col_description(a.attrelid, a.attnum) AS c"
            " FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid"
            " WHERE c.relnamespace = 'booking'::regnamespace"
            " AND c.relname IN ('supplier_bills', 'supplier_bill_items', 'supplier_payouts') AND a.attnum > 0"
        )
    }
    for col in (
        "supplier_bills.amount",
        "supplier_bills.bill_number",
        "supplier_bill_items.amount",
        "supplier_payouts.amount",
        "supplier_payouts.reference",
    ):
        assert comments[col].startswith("conf:commercial"), col
    for col in ("supplier_bills.notes", "supplier_bills.cancel_reason", "supplier_payouts.note"):
        assert comments[col].startswith("pii:free_text"), col
    assert "conf:commercial" in comments["supplier_bills.notes"]
    assert comments["supplier_payouts.void_reason"].startswith("pii:free_text")


async def test_lines_name_confirmed_bookings_of_the_supplier(
    app_conn: asyncpg.Connection, admin: asyncpg.Connection, world: dict[str, Any]
) -> None:
    sup, case = world["sup"]["supplier"], world["case"]
    b = await ops.bill(app_conn, sup, by=MAKER, case_id=case["id"])
    add = (
        "INSERT INTO booking.supplier_bill_items (bill_id, supplier_booking_id, amount, created_by)"
        " VALUES ($1::uuid, $2::uuid, $3::numeric, $4)"
    )
    with pytest.raises(asyncpg.CheckViolationError, match="another supplier"):
        await app_conn.execute(add, b["id"], world["foreign"], "10", MAKER)
    pending = await admin.fetchval(
        "INSERT INTO booking.supplier_bookings (trip_case_id, supplier_id, item_description)"
        " VALUES ($1::uuid, $2::uuid, 'Extra') RETURNING id::text",
        case["id"],
        sup,
    )
    with pytest.raises(asyncpg.CheckViolationError, match="is pending"):
        await app_conn.execute(add, b["id"], pending, "10", MAKER)
    other_case = await ops.sold_case(admin, world["sup"], tag="bk030-other")
    elsewhere = await ops.confirmed_booking(admin, other_case, sup)
    with pytest.raises(asyncpg.CheckViolationError, match="another trip case"):
        await app_conn.execute(add, b["id"], elsewhere, "10", MAKER)
    await app_conn.execute(add, b["id"], world["room"], "300", MAKER)
    with pytest.raises(asyncpg.CheckViolationError, match="never updated"):
        await admin.execute(
            "UPDATE booking.supplier_bill_items SET amount = 1 WHERE bill_id = $1::uuid", b["id"]
        )  # as the owner: role booking has no UPDATE grant at all
    # The supplier stays while lines exist.
    with pytest.raises(asyncpg.CheckViolationError, match="remove its lines"):
        await app_conn.execute(
            "UPDATE booking.supplier_bills SET supplier_id = $2::uuid WHERE id = $1::uuid",
            b["id"],
            world["other"]["supplier"],
        )
    # A statement bill (no case) takes lines of several cases of the supplier.
    statement = await ops.bill(app_conn, sup, by=MAKER, number="STMT-1")
    await app_conn.execute(add, statement["id"], world["room"], "300", MAKER)
    await app_conn.execute(add, statement["id"], elsewhere, "50", MAKER)


async def test_approve_is_maker_checker_and_checks_the_lines(
    app_conn: asyncpg.Connection, world: dict[str, Any]
) -> None:
    sup = world["sup"]["supplier"]
    empty = await ops.bill(app_conn, sup, by=MAKER, number="EMPTY-1")
    with pytest.raises(asyncpg.CheckViolationError, match="at least one confirmed"):
        await ops.approve_bill(app_conn, empty["id"], by=CHECKER)
    b = await ops.bill(app_conn, sup, by=MAKER, amount="380.00", lines=((world["room"], "300"), (world["kayak"], "70")))
    with pytest.raises(asyncpg.CheckViolationError, match="maker-checker|maker_checker"):
        await ops.approve_bill(app_conn, b["id"], by=MAKER)
    with pytest.raises(asyncpg.CheckViolationError, match="add up to 370.00 but the bill is 380.00"):
        await ops.approve_bill(app_conn, b["id"], by=CHECKER)
    # Received = the working draft: fix the amount, then approve.
    await app_conn.execute("UPDATE booking.supplier_bills SET amount = 370 WHERE id = $1::uuid", b["id"])
    await ops.approve_bill(app_conn, b["id"], by=CHECKER)
    assert (await _status(app_conn, b["id"]))[0] == "approved"
    with pytest.raises(asyncpg.CheckViolationError, match="lines are fixed"):
        await app_conn.execute(
            "DELETE FROM booking.supplier_bill_items WHERE bill_id = $1::uuid AND supplier_booking_id = $2::uuid",
            b["id"],
            world["kayak"],
        )
    with pytest.raises(asyncpg.CheckViolationError, match="change only while it is received"):
        await app_conn.execute("UPDATE booking.supplier_bills SET due_date = due_date + 1 WHERE id = $1::uuid", b["id"])
    with pytest.raises(asyncpg.CheckViolationError, match="approval is fixed"):
        await app_conn.execute("UPDATE booking.supplier_bills SET approved_by = $2 WHERE id = $1::uuid", b["id"], PAYER)
    # Notes stay editable on a live bill.
    await app_conn.execute("UPDATE booking.supplier_bills SET notes = 'checked' WHERE id = $1::uuid", b["id"])


async def test_maker_checker_follows_the_last_editor_of_the_money_facts(
    app_conn: asyncpg.Connection, world: dict[str, Any]
) -> None:
    """Review S4: B changes A's amount and lines, then B approves → refused; B never pays it either."""
    sup = world["sup"]["supplier"]
    b = await ops.bill(app_conn, sup, by=MAKER, amount="370.00", lines=((world["room"], "300"), (world["kayak"], "70")))
    edited_by = "SELECT facts_edited_by FROM booking.supplier_bills WHERE id = $1::uuid"
    assert await app_conn.fetchval(edited_by, b["id"]) == MAKER  # set at insert, whatever the app sends
    # Notes and the document are not money facts.
    await app_conn.execute(
        "UPDATE booking.supplier_bills SET notes = 'seen', updated_by = $2 WHERE id = $1::uuid", b["id"], CHECKER
    )
    assert await app_conn.fetchval(edited_by, b["id"]) == MAKER
    # A line added by someone who is not the bill's last editor is refused; the app marks the editor first.
    add = (
        "INSERT INTO booking.supplier_bill_items (bill_id, supplier_booking_id, amount, created_by)"
        " VALUES ($1::uuid, $2::uuid, $3::numeric, $4)"
    )
    await app_conn.execute("DELETE FROM booking.supplier_bill_items WHERE bill_id = $1::uuid", b["id"])
    with pytest.raises(asyncpg.CheckViolationError, match="added by the person editing"):
        await app_conn.execute(add, b["id"], world["room"], "5000", CHECKER)
    with pytest.raises(asyncpg.CheckViolationError, match="added by the person editing"):
        await app_conn.execute(add, b["id"], world["room"], "5000", None)
    # Naming someone else than the person editing (updated_by) is refused.
    with pytest.raises(asyncpg.CheckViolationError, match="facts_edited_by names the person editing"):
        await app_conn.execute(
            "UPDATE booking.supplier_bills SET facts_edited_by = $2, updated_by = $3 WHERE id = $1::uuid",
            b["id"],
            EDITOR,
            CHECKER,
        )
    await app_conn.execute(
        "UPDATE booking.supplier_bills SET facts_edited_by = $2, updated_by = $2 WHERE id = $1::uuid", b["id"], CHECKER
    )
    await app_conn.execute(add, b["id"], world["room"], "5000", CHECKER)
    # A fact change records its author (updated_by), whatever facts_edited_by the statement sends.
    await app_conn.execute(
        "UPDATE booking.supplier_bills SET amount = 5000, facts_edited_by = $3, updated_by = $2 WHERE id = $1::uuid",
        b["id"],
        CHECKER,
        MAKER,
    )
    assert await app_conn.fetchval(edited_by, b["id"]) == CHECKER
    with pytest.raises(asyncpg.CheckViolationError, match="last changed its amounts"):
        await ops.approve_bill(app_conn, b["id"], by=CHECKER)
    check = await app_conn.fetchval(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'supplier_bills_maker_checker_chk'"
    )
    assert "approved_by <> facts_edited_by" in check  # the CHECK holds it too, past the guard
    # A third person approves; the editor of the amounts does not pay it.
    await ops.approve_bill(app_conn, b["id"], by=PAYER)
    with pytest.raises(asyncpg.CheckViolationError, match="set its amounts does not pay it"):
        await ops.payout(app_conn, b["id"], "100", by=CHECKER)
    with pytest.raises(asyncpg.CheckViolationError, match="facts_edited_by names the person editing"):
        await app_conn.execute(
            "UPDATE booking.supplier_bills SET facts_edited_by = $2, updated_by = $2 WHERE id = $1::uuid",
            b["id"],
            EDITOR,
        )
    await ops.payout(app_conn, b["id"], "100", by=EDITOR)


async def test_paid_is_derived_from_payouts(
    app_conn: asyncpg.Connection, admin: asyncpg.Connection, world: dict[str, Any]
) -> None:
    sup = world["sup"]["supplier"]
    b = await ops.bill(app_conn, sup, by=MAKER, amount="380.00", lines=((world["room"], "300"), (world["kayak"], "80")))
    with pytest.raises(asyncpg.CheckViolationError, match="approved bill"):
        await ops.payout(app_conn, b["id"], "100", by=PAYER)  # received: not yet
    await ops.approve_bill(app_conn, b["id"], by=CHECKER)
    with pytest.raises(asyncpg.CheckViolationError, match="paid needs payouts"):
        await app_conn.execute(
            "UPDATE booking.supplier_bills SET status = 'paid', paid_at = now() WHERE id = $1::uuid", b["id"]
        )
    with pytest.raises(asyncpg.CheckViolationError, match="does not pay it"):
        await ops.payout(app_conn, b["id"], "100", by=MAKER)  # the bill's creator
    with pytest.raises(asyncpg.CheckViolationError, match="exceed"):
        await ops.payout(app_conn, b["id"], "380.01", by=PAYER)
    with pytest.raises(asyncpg.CheckViolationError, match="bill currency"):
        await app_conn.execute(
            "INSERT INTO booking.supplier_payouts (bill_id, amount, currency, method, paid_on, idempotency_key,"
            " request_sha256, recorded_by) VALUES ($1::uuid, 10, 'EUR', 'cash', current_date, 'it-682-eur', $2, $3)",
            b["id"],
            ops.SHA,
            PAYER,
        )
    first = await ops.payout(app_conn, b["id"], "100", by=PAYER, tag="p1")
    assert await _status(app_conn, b["id"]) == ("approved", None)
    with pytest.raises(asyncpg.UniqueViolationError):
        await ops.payout(app_conn, b["id"], "1", by=PAYER, tag="p1")  # same Idempotency-Key on this bill
    await ops.payout(app_conn, b["id"], "280", by=PAYER, tag="p2")
    status, paid_at = await _status(app_conn, b["id"])
    assert status == "paid" and paid_at is not None
    balance = await app_conn.fetchrow(
        "SELECT paid_amount, balance, live_payouts, line_count, lines_amount FROM booking.v_supplier_bill_balance"
        " WHERE bill_id = $1::uuid",
        b["id"],
    )
    assert dict(balance) == {
        "paid_amount": Decimal("380.00"),
        "balance": Decimal("0.00"),
        "live_payouts": 2,
        "line_count": 2,
        "lines_amount": Decimal("380.00"),
    }
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app_conn.execute("UPDATE booking.supplier_payouts SET amount = 1 WHERE id = $1::uuid", first)
    with pytest.raises(asyncpg.CheckViolationError, match="money facts never change"):
        await admin.execute("UPDATE booking.supplier_payouts SET amount = 1 WHERE id = $1::uuid", first)
    with pytest.raises(asyncpg.CheckViolationError, match="illegal supplier_bill status transition paid -> cancelled"):
        await app_conn.execute(
            "UPDATE booking.supplier_bills SET status = 'cancelled', cancelled_at = now(), cancelled_by = $2,"
            " cancel_reason = 'wrong' WHERE id = $1::uuid",
            b["id"],
            CHECKER,
        )
    await app_conn.execute(
        "UPDATE booking.supplier_payouts SET voided_at = now(), voided_by = $2, void_reason = 'bank returned it'"
        " WHERE id = $1::uuid",
        first,
        PAYER,
    )
    assert await _status(app_conn, b["id"]) == ("approved", None)
    with pytest.raises(asyncpg.CheckViolationError, match="voided; it is final"):
        await app_conn.execute("UPDATE booking.supplier_payouts SET void_reason = 'again' WHERE id = $1::uuid", first)
    with pytest.raises(asyncpg.CheckViolationError, match="live payout"):
        await app_conn.execute(
            "UPDATE booking.supplier_bills SET status = 'cancelled', cancelled_at = now(), cancelled_by = $2,"
            " cancel_reason = 'wrong' WHERE id = $1::uuid",
            b["id"],
            CHECKER,
        )


async def test_cancel_is_final_and_numbers_are_unique_per_supplier(
    app_conn: asyncpg.Connection, world: dict[str, Any]
) -> None:
    sup = world["sup"]["supplier"]
    b = await ops.bill(app_conn, sup, by=MAKER, number="INV-77", tag="n1")
    with pytest.raises(asyncpg.UniqueViolationError):
        await ops.bill(app_conn, sup, by=MAKER, number="inv-77", tag="n2")  # case-insensitive
    await ops.bill(app_conn, world["other"]["supplier"], by=MAKER, number="INV-77", tag="n3")  # another supplier
    with pytest.raises(asyncpg.CheckViolationError):
        await app_conn.execute(
            "UPDATE booking.supplier_bills SET status = 'cancelled', cancelled_at = now() WHERE id = $1::uuid", b["id"]
        )  # no reason
    await app_conn.execute(
        "UPDATE booking.supplier_bills SET status = 'cancelled', cancelled_at = now(), cancelled_by = $2,"
        " cancel_reason = 'entered twice' WHERE id = $1::uuid",
        b["id"],
        MAKER,
    )
    with pytest.raises(asyncpg.CheckViolationError, match="cancelled; it is final"):
        await app_conn.execute("UPDATE booking.supplier_bills SET notes = 'x' WHERE id = $1::uuid", b["id"])
    await ops.bill(app_conn, sup, by=MAKER, number="INV-77", tag="n4")  # the number is free again
    with pytest.raises(asyncpg.CheckViolationError, match="starts received"):
        await app_conn.execute(
            "INSERT INTO booking.supplier_bills (supplier_id, bill_number, currency, amount, issue_date, due_date,"
            " status, approved_by, approved_at, idempotency_key, request_sha256, created_by)"
            " VALUES ($1::uuid, 'X', 'USD', 1, current_date, current_date, 'approved', $2, now(), 'it-682-x-key', $3,"
            " $4)",
            sup,
            CHECKER,
            ops.SHA,
            MAKER,
        )


async def test_bill_documents_and_payout_evidence(
    app_conn: asyncpg.Connection, admin: asyncpg.Connection, world: dict[str, Any]
) -> None:
    sup, case = world["sup"]["supplier"], world["case"]
    b = await ops.bill(app_conn, sup, by=MAKER, case_id=case["id"], amount="300", lines=((world["room"], "300"),))
    with pytest.raises(asyncpg.CheckViolationError, match="trip_case_id must be"):
        await _file(app_conn, "supplier_bill", "supplier_bill", b["id"], None)
    doc = await _file(app_conn, "supplier_bill", "supplier_bill", b["id"], case["id"])
    await app_conn.execute(
        "UPDATE booking.supplier_bills SET document_file_id = $2::uuid WHERE id = $1::uuid", b["id"], doc
    )
    remove = (
        "UPDATE booking.ops_files SET status = 'removed', removed_reason = 'wrong', removed_at = now(),"
        " removed_by = $2 WHERE id = $1::uuid"
    )
    with pytest.raises(asyncpg.CheckViolationError, match="detach it"):
        await app_conn.execute(remove, doc, MAKER)
    await ops.approve_bill(app_conn, b["id"], by=CHECKER)
    with pytest.raises(asyncpg.CheckViolationError, match="approved supplier bill; it stays"):
        await app_conn.execute(remove, doc, MAKER)
    with pytest.raises(asyncpg.CheckViolationError, match="document is fixed"):
        await _file(app_conn, "supplier_bill", "supplier_bill", b["id"], case["id"])
    pid = await ops.payout(app_conn, b["id"], "300", by=PAYER)
    slip = await _file(app_conn, "payout_evidence", "supplier_payout", pid, case["id"])
    await app_conn.execute(
        "UPDATE booking.supplier_payouts SET evidence_file_id = $2::uuid WHERE id = $1::uuid", pid, slip
    )
    with pytest.raises(asyncpg.CheckViolationError, match="evidence of a live payout"):
        await app_conn.execute(remove, slip, PAYER)
    with pytest.raises(asyncpg.CheckViolationError, match="replaced, never removed"):
        await app_conn.execute("UPDATE booking.supplier_payouts SET evidence_file_id = NULL WHERE id = $1::uuid", pid)
    better = await _file(app_conn, "payout_evidence", "supplier_payout", pid, case["id"])
    await app_conn.execute(
        "UPDATE booking.supplier_payouts SET evidence_file_id = $2::uuid WHERE id = $1::uuid", pid, better
    )
    await app_conn.execute(remove, slip, PAYER)  # no longer the evidence: it may go
    with pytest.raises(asyncpg.CheckViolationError, match="not an active evidence file"):
        await app_conn.execute(
            "UPDATE booking.supplier_payouts SET evidence_file_id = $2::uuid WHERE id = $1::uuid", pid, slip
        )
    # B2B: no bill document (DM-6.8), but the payout's bank slip is fine.
    b2b = await ops.b2b_case(admin, tag="bk030-b2b")
    agency = await ops.confirmed_booking(admin, b2b, sup)
    statement = await ops.bill(app_conn, sup, by=MAKER, number="STMT-B2B", amount="50", lines=((agency, "50"),))
    with pytest.raises(asyncpg.CheckViolationError, match="B2B"):
        await _file(app_conn, "supplier_bill", "supplier_bill", statement["id"], None)
    plain = await ops.bill(app_conn, sup, by=MAKER, number="STMT-DOC", amount="50")
    await _file(app_conn, "supplier_bill", "supplier_bill", plain["id"], None)
    with pytest.raises(asyncpg.CheckViolationError, match="DM-6.8"):
        await app_conn.execute(
            "INSERT INTO booking.supplier_bill_items (bill_id, supplier_booking_id, amount, created_by) VALUES"
            " ($1::uuid, $2::uuid, 50, $3)",
            plain["id"],
            agency,
            MAKER,
        )
    await ops.approve_bill(app_conn, statement["id"], by=CHECKER)
    b2b_payout = await ops.payout(app_conn, statement["id"], "50", by=PAYER)
    await _file(app_conn, "payout_evidence", "supplier_payout", b2b_payout, None)


async def test_concurrent_payouts_never_exceed_the_bill(
    admin: asyncpg.Connection, migrated_booking_dsn: str, world: dict[str, Any]
) -> None:
    sup = world["sup"]["supplier"]
    b = await ops.bill(admin, sup, by=MAKER, amount="300", lines=((world["room"], "300"),))
    await ops.approve_bill(admin, b["id"], by=CHECKER)
    one, two = await asyncpg.connect(migrated_booking_dsn), await asyncpg.connect(migrated_booking_dsn)
    try:
        tx1, tx2 = one.transaction(), two.transaction()
        await tx1.start()
        await tx2.start()
        await ops.payout(one, b["id"], "300", by=PAYER, tag="c1")
        second = asyncio.ensure_future(ops.payout(two, b["id"], "300", by=PAYER, tag="c2"))
        await asyncio.sleep(0.3)
        assert not second.done()  # waits on the bill row the first payout holds
        await tx1.commit()
        with pytest.raises(asyncpg.CheckViolationError, match="approved bill|exceed"):
            await second
        await tx2.rollback()
    finally:
        await one.close()
        await two.close()
    assert await _status(admin, b["id"]) == ("paid", (await _status(admin, b["id"]))[1])
    assert await admin.fetchval("SELECT count(*) FROM booking.supplier_payouts WHERE bill_id = $1::uuid", b["id"]) == 1


async def test_overdue_in_the_view(app_conn: asyncpg.Connection, world: dict[str, Any]) -> None:
    sup = world["sup"]["supplier"]
    late = await ops.bill(app_conn, sup, by=MAKER, number="LATE-1")
    await app_conn.execute(
        "UPDATE booking.supplier_bills SET issue_date = current_date - 10, due_date = current_date - 1"
        " WHERE id = $1::uuid",
        late["id"],
    )
    row = await app_conn.fetchrow(
        "SELECT overdue, balance FROM booking.v_supplier_bill_balance WHERE bill_id = $1::uuid", late["id"]
    )
    assert row["overdue"] is True and row["balance"] == Decimal("400.00")
