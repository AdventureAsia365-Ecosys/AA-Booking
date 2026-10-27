"""AA-682 S1 (bk-026) on a real PostgreSQL, as role ``booking`` where the app writes: registration and re-run no-op,
grants (no DELETE anywhere, edges read-only, files insert + removal only), the supplier-booking state machine (edges
from ``ops_transitions``, facts by CHECK and guard), one LIVE booking per version item (critic B3: a declined one is
replaced; two concurrent inserts → exactly one), the post-sale fact guard on trip cases (critic B1, B9), the supply
view (none / confirming / booked), ``ops_files`` (derived key, bucket, no file on a B2B case, removed is final), and the
DM-6.1 tags in the same file. The HTTP flows are in ``test_bk026_supplier_bookings_api.py``."""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# import AsyncIterator
import asyncpg
import pytest
import pytest_asyncio

from tests.integration import ops
from tests.integration.conftest import apply_migrations

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[3] / "migrations" / "026_ops_supplier_bookings.sql"
BUCKET = "aa-booking-dev-ops-docs-000000000000"
STAFF = "it-682-schema"


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    await ops.purge_ops(conn)
    await ops.staff(conn, (STAFF, "sales"))
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
    return {**await ops.sold_case(admin, sup), "sup": sup}


async def _book(conn: asyncpg.Connection, sold: dict[str, Any], item: str | None = "room_item", **cols: Any) -> str:
    values = {
        "trip_case_id": sold["id"],
        "version_item_id": sold[item] if item else None,
        "supplier_id": sold["sup"]["supplier"],
        "item_description": "Delta Hotel · Deluxe",
        **cols,
    }
    names = ", ".join(values)
    marks = ", ".join(f"${i + 1}" for i in range(len(values)))
    return await conn.fetchval(
        f"INSERT INTO booking.supplier_bookings ({names}) VALUES ({marks}) RETURNING id::text",  # noqa: S608
        *values.values(),
    )


async def _move(conn: asyncpg.Connection, booking: str, status: str, **extra: Any) -> None:
    sets = ", ".join(["status = $2"] + [f"{k} = ${i + 3}" for i, k in enumerate(extra)])
    await conn.execute(
        f"UPDATE booking.supplier_bookings SET {sets} WHERE id = $1::uuid",  # noqa: S608
        booking,
        status,
        *extra.values(),
    )


async def _request(conn: asyncpg.Connection, booking: str) -> None:
    await _move(conn, booking, "requested", requested_at=asyncpg_now(), requested_by=STAFF)


def asyncpg_now() -> datetime:
    return datetime.now(UTC)


async def _case_status(conn: asyncpg.Connection, case_id: str, status: str) -> None:
    await conn.execute("UPDATE booking.trip_cases SET status = $2 WHERE id = $1::uuid", case_id, status)


async def test_registered_rerunnable_and_grants(admin: asyncpg.Connection) -> None:
    await admin.execute("SET ROLE bk_owner")
    try:
        await admin.execute(MIGRATION.read_text(encoding="utf-8"))  # a second run is a no-op
    finally:
        await admin.execute("RESET ROLE")
    # The re-run put back the bk-026 bodies of functions later files replace (bk-028: trip_cases_post_sale_guard,
    # ops_files_guard): replay those files too, as DB Migrate's order would leave them.
    await apply_migrations(admin, only=range(27, 1000))
    assert await admin.fetchval("SELECT count(*) FROM shared.schema_versions WHERE version = 'bk-026'") == 1
    assert await admin.fetchval("SELECT count(*) FROM booking.ops_transitions WHERE entity = 'supplier_booking'") == 7
    for rel in ("ops_transitions", "ops_files", "supplier_bookings", "v_trip_case_supply_state"):
        owner = await admin.fetchval(
            f"SELECT relowner::regrole::text FROM pg_class WHERE oid = 'booking.{rel}'::regclass"
        )
        assert owner == "bk_owner", rel
        for privilege in ("DELETE", "TRUNCATE"):
            assert not await admin.fetchval(
                "SELECT has_table_privilege('booking', $1, $2)", f"booking.{rel}", privilege
            )
        assert not await admin.fetchval("SELECT has_table_privilege('cis_app', $1, 'SELECT')", f"booking.{rel}")
    assert not await admin.fetchval("SELECT has_table_privilege('booking', 'booking.ops_transitions', 'INSERT')")
    assert not await admin.fetchval("SELECT has_table_privilege('booking', 'booking.ops_files', 'UPDATE')")
    assert await admin.fetchval("SELECT has_column_privilege('booking', 'booking.ops_files', 'status', 'UPDATE')")
    assert not await admin.fetchval("SELECT has_column_privilege('booking', 'booking.ops_files', 's3_key', 'UPDATE')")
    edges = {
        (r["from_status"], r["to_status"])
        for r in await admin.fetch("SELECT from_status, to_status FROM booking.trip_case_status_transitions")
    }
    assert {
        ("confirmed", "supplier_confirming"),
        ("supplier_confirming", "confirmed"),
        ("on_trip", "completed"),
    } <= edges
    # Critic B1: bk-026 seeds no money edge (they ship with bk-028).
    seeded = MIGRATION.read_text(encoding="utf-8").split("INSERT INTO booking.trip_case_status_transitions", 1)[1]
    seeded = seeded.split(";", 1)[0]
    assert not re.search(r"invoice_sent|payment_pending|'paid'", seeded)
    settings = {r["key"] for r in await admin.fetch("SELECT key FROM booking.settings")}
    assert {"supplier_answer_days", "pre_departure_days", "ops_docs_max_bytes"} <= settings


async def test_tags_in_the_same_file(admin: asyncpg.Connection) -> None:
    comments = {
        r["col"]: r["c"]
        for r in await admin.fetch(
            "SELECT c.relname || '.' || a.attname AS col, col_description(c.oid, a.attnum) AS c FROM pg_attribute a"
            " JOIN pg_class c ON c.oid = a.attrelid WHERE c.relnamespace = 'booking'::regnamespace"
            " AND c.relname IN ('supplier_bookings', 'ops_files') AND a.attnum > 0"
        )
    }
    assert comments["supplier_bookings.net_amount"].startswith("conf:commercial")
    assert comments["supplier_bookings.notes"].startswith("pii:free_text; also conf:commercial")
    assert comments["supplier_bookings.cancel_reason"].startswith("pii:free_text")
    assert comments["ops_files.s3_key"].startswith("pii:payment; also pii:name")
    assert comments["ops_files.file_name"].startswith("pii:free_text")


async def test_supplier_booking_machine(app_conn: asyncpg.Connection, sold: dict[str, Any]) -> None:
    booking = await _book(app_conn, sold)
    code = await app_conn.fetchval("SELECT code FROM booking.supplier_bookings WHERE id = $1::uuid", booking)
    assert code.startswith("SB-")
    # An edge that is not listed: pending -> confirmed.
    with pytest.raises(asyncpg.CheckViolationError, match="illegal supplier_booking status transition"):
        await _move(app_conn, booking, "confirmed", confirmation_ref="X1", confirmed_at=asyncpg_now())
    await _request(app_conn, booking)
    # confirmed needs the supplier's ref.
    with pytest.raises(asyncpg.CheckViolationError, match="supplier_bookings_confirmed_chk"):
        await _move(app_conn, booking, "confirmed", confirmed_at=asyncpg_now())
    # declined needs a desk code.
    with pytest.raises(asyncpg.CheckViolationError, match="supplier_bookings_decline_chk"):
        await _move(app_conn, booking, "declined", decline_code="no_such_code", declined_at=asyncpg_now())
    await _move(app_conn, booking, "declined", decline_code="sold_out", declined_at=asyncpg_now())
    # declined is final.
    with pytest.raises(asyncpg.CheckViolationError, match="illegal supplier_booking status transition"):
        await _request(app_conn, booking)
    # Facts of a final booking are fixed; case / item / code never change.
    with pytest.raises(asyncpg.CheckViolationError, match="facts are fixed"):
        await app_conn.execute("UPDATE booking.supplier_bookings SET pax = 3 WHERE id = $1::uuid", booking)
    with pytest.raises(asyncpg.CheckViolationError, match="never change"):
        await app_conn.execute("UPDATE booking.supplier_bookings SET code = 'SB-999999' WHERE id = $1::uuid", booking)
    # A booking starts pending; never deleted by the app.
    with pytest.raises(asyncpg.CheckViolationError, match="starts pending"):
        await _book(app_conn, sold, item=None, status="requested", requested_at=asyncpg_now())
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app_conn.execute("DELETE FROM booking.supplier_bookings WHERE id = $1::uuid", booking)


async def test_one_live_booking_per_item_and_replacement(app_conn: asyncpg.Connection, sold: dict[str, Any]) -> None:
    first = await _book(app_conn, sold)
    with pytest.raises(asyncpg.UniqueViolationError, match="supplier_bookings_item_live_uq"):
        await _book(app_conn, sold)
    await _request(app_conn, first)
    await _move(app_conn, first, "declined", decline_code="supplier_unavailable", declined_at=asyncpg_now())
    # Critic B3: a declined booking does not block its replacement.
    replacement = await _book(app_conn, sold)
    assert replacement != first
    await _move(app_conn, replacement, "cancelled", cancel_reason="client changed plan", cancelled_at=asyncpg_now())
    third = await _book(app_conn, sold)
    live = await app_conn.fetchval(
        "SELECT count(*) FROM booking.supplier_bookings WHERE version_item_id = $1::uuid"
        " AND status IN ('pending', 'requested', 'confirmed')",
        sold["room_item"],
    )
    assert live == 1 and third
    # An item of another case, or of no approved version, is refused.
    with pytest.raises(asyncpg.CheckViolationError, match="not an item of the approved version"):
        await _book(app_conn, sold, version_item_id=str(uuid.uuid4()), item=None)


async def test_concurrent_inserts_of_one_item_one_wins(
    app_conn: asyncpg.Connection, sold: dict[str, Any], migrated_booking_dsn: str
) -> None:
    conns = [await asyncpg.connect(migrated_booking_dsn) for _ in range(4)]
    try:

        async def attempt(conn: asyncpg.Connection) -> str:
            async with conn.transaction():
                await asyncio.sleep(0)
                try:
                    await _book(conn, sold, item="service_item")
                    return "won"
                except asyncpg.UniqueViolationError:
                    return "lost"

        results = await asyncio.gather(*(attempt(c) for c in conns))
    finally:
        for c in conns:
            await c.close()
    assert sorted(results) == ["lost", "lost", "lost", "won"]


async def test_post_sale_fact_guard(
    app_conn: asyncpg.Connection, admin: asyncpg.Connection, sold: dict[str, Any]
) -> None:
    case = sold["id"]
    # supplier_confirming needs a live booking.
    with pytest.raises(asyncpg.CheckViolationError, match="needs a live supplier booking"):
        await _case_status(app_conn, case, "supplier_confirming")
    booking = await _book(app_conn, sold)
    await _case_status(app_conn, case, "supplier_confirming")
    # Back to confirmed only without a live booking.
    with pytest.raises(asyncpg.CheckViolationError, match="no live supplier booking"):
        await _case_status(app_conn, case, "confirmed")
    # A B2C case reaches pre_departure only with a paid invoice, and a money status only from its invoice (bk-028).
    with pytest.raises(asyncpg.CheckViolationError):
        await _case_status(app_conn, case, "pre_departure")
    with pytest.raises(asyncpg.CheckViolationError, match="must be what its invoice says"):
        await _case_status(app_conn, case, "invoice_sent")
    # Cancel, then reopen refused while a live booking exists (critic B9).
    await app_conn.execute("SELECT set_config('booking.status_note', 'it', false)")
    await _case_status(app_conn, case, "cancelled")
    with pytest.raises(asyncpg.CheckViolationError, match="before reopening"):
        await _case_status(app_conn, case, "new_inquiry")
    await _move(app_conn, booking, "cancelled", cancel_reason="case cancelled", cancelled_at=asyncpg_now())
    await _case_status(app_conn, case, "new_inquiry")


async def test_travel_dates_gate_on_trip(admin: asyncpg.Connection, sold: dict[str, Any]) -> None:
    """on_trip only from the start date; B2B cases with nothing to book pass pre_departure (supply state none)."""
    await admin.execute("SET ROLE bk_owner")
    try:
        await admin.execute(
            "UPDATE booking.trip_cases SET channel = 'b2b', customer_id = NULL, party_tenant_id = gen_random_uuid()"
            " WHERE id = $1::uuid",
            sold["id"],
        )
        # The approved items need supply: not booked yet, so no pre_departure.
        with pytest.raises(asyncpg.CheckViolationError, match="supply is confirming"):
            await _case_status(admin, sold["id"], "pre_departure")
        async with admin.transaction():
            await admin.execute("ALTER TABLE booking.trip_case_version_itinerary DISABLE TRIGGER USER")
            await admin.execute(
                "UPDATE booking.trip_case_version_itinerary SET hotel_room_type_id = NULL, service_id = NULL"
                " WHERE trip_case_version_id = $1::uuid",
                sold["version"],
            )
            await admin.execute("ALTER TABLE booking.trip_case_version_itinerary ENABLE TRIGGER USER")
        state = await admin.fetchval(
            "SELECT state FROM booking.v_trip_case_supply_state WHERE trip_case_id = $1::uuid", sold["id"]
        )
        assert state == "none"
        await _case_status(admin, sold["id"], "pre_departure")
        with pytest.raises(asyncpg.CheckViolationError, match="on_trip only from its start date"):
            await _case_status(admin, sold["id"], "on_trip")
    finally:
        await admin.execute("RESET ROLE")


async def test_supply_view_states(app_conn: asyncpg.Connection, sold: dict[str, Any]) -> None:
    async def state() -> dict[str, Any]:
        row = await app_conn.fetchrow(
            "SELECT * FROM booking.v_trip_case_supply_state WHERE trip_case_id = $1::uuid", sold["id"]
        )
        return dict(row)

    assert (await state())["state"] == "confirming" and (await state())["items_to_book"] == 2
    room = await _book(app_conn, sold)
    service = await _book(app_conn, sold, item="service_item")
    for b in (room, service):
        await _request(app_conn, b)
        await _move(app_conn, b, "confirmed", confirmation_ref=f"REF-{b[:4]}", confirmed_at=asyncpg_now())
    final = await state()
    assert final["state"] == "booked" and final["items_booked"] == 2 and final["confirmed"] == 2 == final["live"]
    # An extra booking still waiting: no longer booked.
    extra = await _book(app_conn, sold, item=None, item_description="Extra night")
    assert (await state())["state"] == "confirming"
    await _move(app_conn, extra, "cancelled", cancel_reason="not needed", cancelled_at=asyncpg_now())
    assert (await state())["state"] == "booked"


async def test_ops_files_rules(app_conn: asyncpg.Connection, admin: asyncpg.Connection, sold: dict[str, Any]) -> None:
    booking = await _book(app_conn, sold)
    fid = str(uuid.uuid4())

    async def insert(**over: Any) -> None:
        values = {
            "id": fid,
            "kind": "supplier_voucher",
            "entity_type": "supplier_booking",
            "entity_id": booking,
            "trip_case_id": sold["id"],
            "s3_bucket": BUCKET,
            "s3_key": f"ops-docs/supplier_voucher/{fid}.pdf",
            "content_type": "application/pdf",
            "bytes": 10,
            "sha256": "a" * 64,
            "file_name": "voucher.pdf",
            "uploaded_by": STAFF,
            **over,
        }
        names = ", ".join(values)
        marks = ", ".join(f"${i + 1}" for i in range(len(values)))
        await app_conn.execute(
            f"INSERT INTO booking.ops_files ({names}) VALUES ({marks})",  # noqa: S608
            *values.values(),
        )

    with pytest.raises(asyncpg.CheckViolationError, match="ops_files_key_chk"):
        await insert(s3_key="ops-docs/elsewhere.pdf")
    with pytest.raises(asyncpg.CheckViolationError, match="ops_files_bucket_chk"):
        await insert(s3_bucket="public-bucket")
    with pytest.raises(asyncpg.CheckViolationError, match="ops_files_kind_entity_chk"):
        await insert(kind="payment_evidence", s3_key=f"ops-docs/payment_evidence/{fid}.pdf")
    with pytest.raises(asyncpg.CheckViolationError, match="trip_case_id must be"):
        await insert(trip_case_id=None)
    await insert()
    # The voucher of the booking: an active supplier_voucher file of this booking only.
    await app_conn.execute(
        "UPDATE booking.supplier_bookings SET voucher_file_id = $2::uuid WHERE id = $1::uuid", booking, fid
    )
    await app_conn.execute(
        "UPDATE booking.ops_files SET status = 'removed', removed_reason = 'wrong file', removed_at = now(),"
        " removed_by = $2 WHERE id = $1::uuid",
        fid,
        STAFF,
    )
    with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
        await app_conn.execute("UPDATE booking.ops_files SET status = 'active' WHERE id = $1::uuid", fid)
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app_conn.execute("UPDATE booking.ops_files SET file_name = 'x' WHERE id = $1::uuid", fid)
    # No document on a B2B case (DM-6.8).
    await admin.execute("SET ROLE bk_owner")
    try:
        await admin.execute(
            "UPDATE booking.trip_cases SET channel = 'b2b', customer_id = NULL, party_tenant_id = gen_random_uuid()"
            " WHERE id = $1::uuid",
            sold["id"],
        )
    finally:
        await admin.execute("RESET ROLE")
    other = str(uuid.uuid4())
    with pytest.raises(asyncpg.CheckViolationError, match="no supplier document on a B2B trip case"):
        await insert(id=other, s3_key=f"ops-docs/supplier_voucher/{other}.pdf", sha256="b" * 64)
