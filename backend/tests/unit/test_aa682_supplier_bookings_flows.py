"""AA-682 S1 (bk-026) supplier-booking flows without a database: the Python branches of ``app.ops.supplier_bookings``
(not found, conflicts, invalid input, net masking, the case auto-advance, the B2B tie-in, the case cancel cascade)
driven through :class:`ScriptedConn`. The SQL itself (edges, guards, indexes) is covered on a real PostgreSQL in
tests/integration/test_bk026_*.py."""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import asyncpg
import pytest

from app.auth.staff import Staff
from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.ops import supplier_bookings as sb
from tests.unit.fakes import ScriptedConn
from tests.unit.test_aa682_supplier_bookings import _item

SALES = Staff("u-1", None, "Sales One", "sales", True, "reviewer")
CONTENT = Staff("u-9", None, "Writer", "content_creator", True, "content")

CASE_ID = "00000000-0000-4000-8000-0000000c0001"
SB_ID = "00000000-0000-4000-8000-0000000b0001"
SB2_ID = "00000000-0000-4000-8000-0000000b0002"
ITEM_ID = "00000000-0000-4000-8000-0000000a0001"
SUPPLIER = "00000000-0000-4000-8000-000000050001"
SUPPLIER2 = "00000000-0000-4000-8000-000000050002"
ROOM = "00000000-0000-4000-8000-0000000d0001"
SERVICE = "00000000-0000-4000-8000-0000000e0001"
HOLD = "00000000-0000-4000-8000-0000000f0001"
VOUCHER = "00000000-0000-4000-8000-000000010001"

# SQL fragments (each matches one statement of the module).
LOCK_CASE = "tc.start_date, tc.pax FROM booking.trip_cases tc"
READ_CASE = "tc.channel FROM booking.trip_cases tc"
GET = "WHERE sb.id = $1::uuid"
CASE_ROWS = "WHERE sb.trip_case_id = $1::uuid ORDER BY"
FILES_OF = "f.entity_id = ANY($2::uuid[])"
ITEMS = "FROM booking.trip_case_versions v"
APPROVED = "SELECT 1 FROM booking.trip_case_versions WHERE"
SUPPLY = "v_trip_case_supply_state"
COUNTS = "AS asked FROM booking.supplier_bookings"
CASE_STATUS = "SELECT status FROM booking.trip_cases WHERE id"
CASE_UPDATE = "UPDATE booking.trip_cases SET status"
AUDIT = "INSERT INTO booking.audit_logs"
INSERT = "INSERT INTO booking.supplier_bookings"
SET_STATUS = "UPDATE booking.supplier_bookings SET status = $2"
UPDATE_FACTS = "UPDATE booking.supplier_bookings SET"
SUPPLIER_ROW = "SELECT status, code FROM booking.suppliers"
ROOM_OF = "WHERE rt.id = $1::uuid AND h.supplier_id = $2::uuid"
SERVICE_OF = "FROM booking.services WHERE id = $1::uuid"
HOLD_LOCK = "FROM booking.b2b_holds WHERE id = $1::uuid FOR UPDATE"

AUDIT_KEYS = (
    "actor_type",
    "actor_id",
    "actor_ref",
    "action",
    "entity_type",
    "entity_id",
    "before",
    "after",
    "entity_label",
    "summary",
)


def booking_row(**over: Any) -> dict[str, Any]:
    return {
        "id": SB_ID,
        "code": "SB-000001",
        "trip_case_id": CASE_ID,
        "trip_case_code": "TC-000001",
        "channel": "b2c",
        "trip_case_status": "confirmed",
        "version_item_id": ITEM_ID,
        "item_position": 1,
        "supplier_id": SUPPLIER,
        "supplier_code": "SUP-000001",
        "supplier_name": "Delta Hotel",
        "hotel_room_type_id": ROOM,
        "service_id": None,
        "item_description": "Delta Hotel · Deluxe",
        "service_date": date(2027, 3, 2),
        "nights": 2,
        "pax": 2,
        "net_amount": Decimal("400.50"),
        "net_currency": "USD",
        "status": "pending",
        "requested_at": None,
        "requested_by": None,
        "requested_by_name": None,
        "confirmed_at": None,
        "confirmed_by": None,
        "confirmed_by_name": None,
        "confirmation_ref": None,
        "declined_at": None,
        "decline_code": None,
        "cancelled_at": None,
        "cancel_reason": None,
        "voucher_file_id": None,
        "b2b_hold_id": None,
        "b2b_line_ref": None,
        "b2b_line_status": None,
        "notes": None,
        "created_by": "u-1",
        "created_at": None,
        "updated_at": None,
        **over,
    }


def case_row(**over: Any) -> dict[str, Any]:
    return {
        "id": CASE_ID,
        "code": "TC-000001",
        "status": "confirmed",
        "channel": "b2c",
        "start_date": date(2027, 3, 1),
        "pax": 2,
        **over,
    }


def write_conn(
    booking: dict[str, Any] | None = None,
    case: dict[str, Any] | None = None,
    *,
    case_status: str = "confirmed",
    live: int = 1,
    asked: int = 0,
) -> ScriptedConn:
    """A connection for one write on one booking: the booking, its case and the advance_case counts."""
    return ScriptedConn(
        [
            (GET, booking or booking_row()),
            (LOCK_CASE, case or case_row()),
            (COUNTS, {"live": live, "asked": asked}),
            (CASE_STATUS, case_status),
        ]
    )


def audits(conn: ScriptedConn) -> list[dict[str, Any]]:
    out = []
    for args in conn.sql(AUDIT):
        row = dict(zip(AUDIT_KEYS, args, strict=True))
        row["before"] = json.loads(row["before"]) if row["before"] else None
        row["after"] = json.loads(row["after"]) if row["after"] else None
        out.append(row)
    return out


def actions(conn: ScriptedConn) -> list[str]:
    return [a["action"] for a in audits(conn)]


class AnswerLine:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def __call__(self, conn: Any, staff: Staff, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((args, kwargs))
        return {}


@pytest.fixture
def answer_line(monkeypatch: pytest.MonkeyPatch) -> AnswerLine:
    rec = AnswerLine()
    monkeypatch.setattr(sb.desk, "answer_line", rec)
    return rec


# ── Case lookups and the booking shape ───────────────────────────────────────


@pytest.mark.asyncio
async def test_lock_case_by_code_or_id_and_not_found() -> None:
    conn = ScriptedConn([(LOCK_CASE, case_row())])
    assert (await sb.lock_case(conn, " tc-000001 "))["id"] == CASE_ID
    assert await sb.lock_case(conn, CASE_ID.upper())
    (by_code, by_id) = conn.calls
    assert "tc.code = $1" in by_code[0] and by_code[1] == ("TC-000001",)
    assert "tc.id = $1::uuid" in by_id[0] and by_id[1] == (CASE_ID,) and by_id[0].endswith("FOR UPDATE")
    with pytest.raises(NotFound) as exc:
        await sb.lock_case(ScriptedConn(), "TC-404")
    assert exc.value.code == "trip_case_not_found"
    with pytest.raises(NotFound):
        await sb._read_case(ScriptedConn(), "TC-404")


def test_booking_out_masks_net_without_pricing_read_and_nests_people_and_line() -> None:
    row = booking_row(
        status="confirmed",
        requested_by="u-1",
        requested_by_name="Sales One",
        b2b_hold_id=HOLD,
        b2b_line_ref="room",
        b2b_line_status="confirmed",
    )
    seen = sb.booking_out(row, SALES, [{"id": "f-1"}])
    assert (seen["net_amount"], seen["net_currency"]) == (Decimal("400.50"), "USD")
    assert seen["trip_case"] == {"id": CASE_ID, "code": "TC-000001", "status": "confirmed"}
    assert seen["supplier"] == {"id": SUPPLIER, "code": "SUP-000001", "name": "Delta Hotel"}
    assert seen["requested_by"] == {"id": "u-1", "name": "Sales One"} and seen["confirmed_by"] is None
    assert seen["b2b_line"] == {"hold_id": HOLD, "line_ref": "room", "status": "confirmed"}
    assert seen["live"] is True and seen["files"] == [{"id": "f-1"}]
    assert "supplier_name" not in seen and "trip_case_code" not in seen

    masked = sb.booking_out(booking_row(status="declined"), CONTENT)
    assert masked["net_amount"] is None and masked["net_currency"] is None
    assert masked["b2b_line"] is None and masked["live"] is False and masked["files"] == []


@pytest.mark.asyncio
async def test_get_refuses_a_bad_id_without_sql_and_a_missing_row() -> None:
    conn = ScriptedConn()
    with pytest.raises(NotFound) as exc:
        await sb._get(conn, "not-a-uuid")
    assert exc.value.code == "supplier_booking_not_found" and conn.calls == []
    with pytest.raises(NotFound):
        await sb.get_booking(conn, SALES, SB_ID)
    assert len(conn.calls) == 1


@pytest.mark.asyncio
async def test_get_booking_attaches_its_files() -> None:
    file_row = {
        "id": "f-1",
        "entity_id": SB_ID,
        "s3_key": "ops-docs/supplier_voucher/f-1.pdf",
        "uploaded_by": "u-1",
        "uploaded_by_name": "Sales One",
    }
    conn = ScriptedConn([(GET, booking_row()), (FILES_OF, [file_row])])
    out = await sb.get_booking(conn, SALES, SB_ID)
    assert out["files"] == [{"id": "f-1", "entity_id": SB_ID, "uploaded_by": {"id": "u-1", "name": "Sales One"}}]
    assert conn.sql(FILES_OF) == [("supplier_booking", [SB_ID])]


@pytest.mark.asyncio
async def test_supply_state_defaults_to_none_without_a_view_row() -> None:
    assert (await sb.supply_state(ScriptedConn(), CASE_ID))["state"] == "none"
    row = {"items_to_book": 2, "items_booked": 1, "live": 1, "confirmed": 0, "waiting": 1, "declined": 0}
    conn = ScriptedConn([(SUPPLY, {**row, "state": "confirming"})])
    assert await sb.supply_state(conn, CASE_ID) == {**row, "state": "confirming"}


# ── Case auto-advance ────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "live", "asked", "target"),
    [
        ("confirmed", 1, 1, "supplier_confirming"),  # the first booking asked of a supplier
        ("confirmed", 2, 0, None),  # only pending bookings: nothing asked yet
        ("supplier_confirming", 0, 0, "confirmed"),  # the last live booking left
        ("supplier_confirming", 1, 1, None),
        ("pre_departure", 0, 0, None),  # travel statuses never move back
        ("invoice_sent", 0, 0, "confirmed"),  # bk-028: its invoice was cancelled (no sent invoice left)
    ],
)
async def test_advance_case(status: str, live: int, asked: int, target: str | None) -> None:
    conn = ScriptedConn([(COUNTS, {"live": live, "asked": asked}), (CASE_STATUS, status)])
    assert await sb.advance_case(conn, case_row(), cause="supplier booking SB-000001 requested") == target
    if target is None:
        assert conn.sql(CASE_UPDATE) == [] and conn.sql(AUDIT) == []
        return
    assert conn.sql("set_config('booking.actor', ''") == [("Auto: supplier booking SB-000001 requested",)]
    assert conn.sql(CASE_UPDATE) == [(CASE_ID, target)]
    assert conn.sql("set_config('booking.status_note', '', true)") == [()]  # the note does not leak to later writes
    (row,) = audits(conn)
    assert (row["actor_type"], row["actor_ref"], row["action"]) == (
        "system",
        "aa682:advance_case",
        "trip_case.status_changed",
    )
    assert row["before"] == {"status": status}
    assert row["after"] == {"status": target, "cause": "supplier booking SB-000001 requested"}


# ── Reads ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_for_case_lists_bookings_and_the_unbooked_items_with_their_skip_reason() -> None:
    items = [
        _item(id="i-1", position=1, live_booking="SB-000001"),  # has a live booking: not listed again
        _item(id="i-2", position=2, supplier_id=None),
        _item(id="i-3", position=3, b2b_line_status="declined"),
    ]
    conn = ScriptedConn(
        [
            (READ_CASE, case_row(channel="b2b")),
            (CASE_ROWS, [booking_row(), booking_row(id=SB2_ID, code="SB-000002", status="declined")]),
            (ITEMS, items),
        ]
    )
    out = await sb.list_for_case(conn, CONTENT, "TC-000001")
    assert out["trip_case"] == {"id": CASE_ID, "code": "TC-000001", "status": "confirmed", "channel": "b2b"}
    assert out["supply"]["state"] == "none"
    assert [i["code"] for i in out["items"]] == ["SB-000001", "SB-000002"]
    assert all(i["net_amount"] is None for i in out["items"])  # content creators see no net
    assert [(u["version_item_id"], u["bookable"], u["skip_reason"]) for u in out["unbooked_items"]] == [
        ("i-2", False, "no_supplier"),
        ("i-3", False, "b2b_line_closed"),
    ]
    assert out["documents_allowed"] is False
    assert conn.sql(FILES_OF) == [("supplier_booking", [SB_ID, SB2_ID])]


@pytest.mark.asyncio
async def test_list_bookings_validates_status_and_page() -> None:
    conn = ScriptedConn()
    with pytest.raises(Invalid) as exc:
        await sb.list_bookings(conn, SALES, status="lost")
    assert exc.value.code == "unknown_status"
    for limit, offset in ((0, 0), (sb.MAX_PAGE + 1, 0), (10, -1)):
        with pytest.raises(Invalid) as exc:
            await sb.list_bookings(conn, SALES, limit=limit, offset=offset)
        assert exc.value.code == "invalid_page"
    assert conn.calls == []


@pytest.mark.asyncio
async def test_list_bookings_resolves_the_supplier_and_filters() -> None:
    with pytest.raises(NotFound) as exc:
        await sb.list_bookings(ScriptedConn(), SALES, supplier_id="SUP-404")
    assert exc.value.code == "supplier_not_found"

    conn = ScriptedConn(
        [
            ("FROM booking.suppliers WHERE id::text = $1 OR code", SUPPLIER),
            ("SELECT count(*) FROM booking.supplier_bookings sb", 1),
            ("LIMIT $5 OFFSET $6", [booking_row(status="requested")]),
        ]
    )
    start, end = date(2027, 3, 1), date(2027, 3, 31)
    out = await sb.list_bookings(
        conn, CONTENT, supplier_id=" sup-000001 ", status="live", start=start, end=end, limit=10, offset=20
    )
    assert conn.sql("OR code = upper($1)") == [("sup-000001",)]
    assert conn.sql("LIMIT $5 OFFSET $6") == [(SUPPLIER, "live", start, end, 10, 20)]
    assert out["total"] == 1 and (out["limit"], out["offset"]) == (10, 20)
    assert out["items"][0]["net_amount"] is None and out["items"][0]["status"] == "requested"

    empty = await sb.list_bookings(ScriptedConn(), SALES, status="pending")
    assert empty == {"items": [], "total": 0, "limit": 50, "offset": 0}


@pytest.mark.asyncio
@pytest.mark.parametrize(("setting", "days", "arg"), [(None, 2, 2), ("x", 2, 2), ("5", 5, 5), ("90", 90, 60)])
async def test_summary_reads_the_answer_days_setting(setting: Any, days: int, arg: int) -> None:
    counts = {"requested": 3, "overdue": 1, "pending": 2}
    conn = ScriptedConn([("key = 'supplier_answer_days'", setting), ("AS overdue", counts)])
    assert await sb.summary(conn) == {**counts, "answer_days": days}
    assert conn.sql("AS overdue") == [(arg,)]  # the interval is clamped to 1..60 days


# ── Generate and add ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_generate_needs_an_open_case_with_an_approved_version() -> None:
    with pytest.raises(Conflict) as exc:
        await sb.generate(ScriptedConn([(LOCK_CASE, case_row(status="building"))]), SALES, "TC-000001")
    assert exc.value.code == "case_not_bookable"
    conn = ScriptedConn([(LOCK_CASE, case_row())])
    with pytest.raises(Conflict) as exc:
        await sb.generate(conn, SALES, "TC-000001")
    assert exc.value.code == "no_approved_version" and conn.sql(INSERT) == []


@pytest.mark.asyncio
async def test_generate_inserts_bookable_items_and_counts_only_new_rows() -> None:
    new_ids = iter([SB_ID, None])  # the second insert lost to a concurrent live booking (ON CONFLICT DO NOTHING)
    conn = ScriptedConn(
        [
            (LOCK_CASE, case_row()),
            (READ_CASE, case_row()),
            (APPROVED, 1),
            (ITEMS, [_item(id="i-1"), _item(id="i-2", supplier_id=None), _item(id="i-3", position=3)]),
            (INSERT, lambda *_: next(new_ids)),
        ]
    )
    out = await sb.generate(conn, SALES, "TC-000001")
    assert out["created"] == 1
    inserts = conn.sql(INSERT)
    assert [args[1] for args in inserts] == ["i-1", "i-3"]  # i-2 has no supplier
    first = inserts[0]
    assert first[0] == CASE_ID and first[2] == "sup-1"
    assert first[6:11] == (date(2027, 3, 2), 2, 2, Decimal("400.50"), "USD")
    assert first[-1] == "u-1"
    (row,) = audits(conn)
    assert row["action"] == "supplier_booking.generated"
    assert row["after"] == {"created": 1, "supplier_booking_ids": [SB_ID]}


@pytest.mark.asyncio
async def test_generate_twice_creates_nothing_and_writes_no_audit() -> None:
    conn = ScriptedConn(
        [(LOCK_CASE, case_row()), (READ_CASE, case_row()), (APPROVED, 1), (ITEMS, [_item(live_booking="SB-1")])]
    )
    out = await sb.generate(conn, SALES, "TC-000001")
    assert out["created"] == 0 and conn.sql(INSERT) == [] and conn.sql(AUDIT) == []


def create_conn(case: dict[str, Any] | None = None, **rules: Any) -> ScriptedConn:
    base: dict[str, Any] = {
        LOCK_CASE: case or case_row(),
        SUPPLIER_ROW: {"status": "active", "code": "SUP-000002"},
        ROOM_OF: 1,
        SERVICE_OF: 1,
        INSERT: SB_ID,
        GET: booking_row(supplier_name="Mekong Transfers"),
    }
    base.update({k: v for k, v in rules.items()})
    return ScriptedConn(list(base.items()))


@pytest.mark.asyncio
async def test_create_an_extra_item_by_hand() -> None:
    conn = create_conn()
    data = {
        "supplier_id": SUPPLIER2,
        "service_id": SERVICE,
        "item_description": "  Airport transfer  ",
        "net_amount": Decimal("50"),
        "net_currency": "USD",
        "notes": "   ",
    }
    out = await sb.create(conn, SALES, "TC-000001", data)
    assert out["id"] == SB_ID
    assert conn.sql(SERVICE_OF) == [(SERVICE, SUPPLIER2)] and conn.sql(ROOM_OF) == []
    (args,) = conn.sql(INSERT)
    assert args[:6] == (CASE_ID, None, SUPPLIER2, None, SERVICE, "Airport transfer")
    assert args[9:11] == (Decimal("50"), "USD") and args[13] is None  # blank notes are no notes
    (row,) = audits(conn)
    assert row["action"] == "supplier_booking.created" and "Mekong Transfers" in row["summary"]
    assert row["after"] == {"supplier_id": SUPPLIER2, "version_item_id": None, "status": "pending"}


@pytest.mark.asyncio
async def test_create_replacing_a_b2b_item_copies_the_item_and_links_the_on_request_line() -> None:
    item = _item(id=ITEM_ID, b2b_hold_id=HOLD, b2b_line_ref="room", b2b_held_as="on_request")
    conn = create_conn(case_row(channel="b2b"), **{ITEMS: [item]})
    data = {"supplier_id": SUPPLIER2, "version_item_id": ITEM_ID, "hotel_room_type_id": ROOM}
    await sb.create(conn, SALES, "TC-000001", data)
    assert conn.sql(ROOM_OF) == [(ROOM, SUPPLIER2)]
    (args,) = conn.sql(INSERT)
    assert args[1:4] == (ITEM_ID, SUPPLIER2, ROOM)  # the new supplier and room override the item's
    assert args[5] == "Delta Hotel · Deluxe"
    assert args[6:11] == (date(2027, 3, 2), 3, 2, Decimal("400.50"), "USD")  # B2B nights: days 2..4 inclusive
    assert args[11:13] == (HOLD, "room")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "rules", "data", "error", "code"),
    [
        (None, {}, {"supplier_id": "nope", "item_description": "x"}, Invalid, "supplier_required"),
        (None, {}, {"supplier_id": SUPPLIER, "hotel_room_type_id": ROOM, "service_id": SERVICE}, Invalid,
         "one_supply_record"),
        (None, {SUPPLIER_ROW: None}, {"supplier_id": SUPPLIER}, NotFound, "supplier_not_found"),
        (None, {SUPPLIER_ROW: {"status": "inactive", "code": "SUP-1"}}, {"supplier_id": SUPPLIER}, Conflict,
         "supplier_inactive"),
        (None, {ROOM_OF: None}, {"supplier_id": SUPPLIER, "hotel_room_type_id": ROOM}, Invalid,
         "supply_not_of_supplier"),
        (None, {SERVICE_OF: None}, {"supplier_id": SUPPLIER, "service_id": SERVICE}, Invalid,
         "supply_not_of_supplier"),
        (None, {}, {"supplier_id": SUPPLIER, "item_description": "x", "net_amount": Decimal("1")}, Invalid,
         "net_pair"),
        (None, {ITEMS: []}, {"supplier_id": SUPPLIER, "version_item_id": ITEM_ID}, Invalid, "item_not_bookable"),
        (None, {ITEMS: [_item(id=ITEM_ID, live_booking="SB-000009")]}, {"supplier_id": SUPPLIER,
         "version_item_id": ITEM_ID}, Conflict, "booking_exists"),
        (case_row(channel="b2b"), {ITEMS: [_item(id=ITEM_ID, b2b_declined=True)]}, {"supplier_id": SUPPLIER,
         "version_item_id": ITEM_ID}, Conflict, "b2b_line_answered"),
        (None, {}, {"supplier_id": SUPPLIER, "item_description": "   "}, Invalid, "description_required"),
        (None, {INSERT: asyncpg.CheckViolationError("violates supplier_bookings_nights_chk\nDETAIL: x")},
         {"supplier_id": SUPPLIER, "item_description": "x"}, Conflict, "invalid_supplier_booking"),
        (None, {INSERT: None}, {"supplier_id": SUPPLIER, "item_description": "x"}, Conflict, "booking_exists"),
        (case_row(status="cancelled"), {}, {"supplier_id": SUPPLIER}, Conflict, "case_not_bookable"),
    ],
)  # fmt: skip
async def test_create_refusals(case: dict | None, rules: dict, data: dict, error: type[Exception], code: str) -> None:
    conn = create_conn(case, **rules)
    with pytest.raises(error) as exc:
        await sb.create(conn, SALES, "TC-000001", data)
    assert exc.value.code == code
    assert conn.sql(AUDIT) == []
    if code == "invalid_supplier_booking":
        assert exc.value.message == "violates supplier_bookings_nights_chk"  # first line of the database message


# ── Edits ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_locks_facts_once_confirmed_but_not_the_notes() -> None:
    conn = write_conn(booking_row(status="confirmed"))
    with pytest.raises(Conflict) as exc:
        await sb.update(conn, SALES, SB_ID, {"nights": 3})
    assert exc.value.code == "supplier_booking_locked"

    conn = write_conn(booking_row(status="confirmed"))
    out = await sb.update(conn, SALES, SB_ID, {"notes": "  call on arrival ", "nights": 2})  # nights unchanged
    assert out["changed"] == ["notes"]
    (sql, args) = next(c for c in conn.calls if c[0].startswith(UPDATE_FACTS))
    assert "SET notes = $3, updated_by = $2" in sql and args == (SB_ID, "u-1", "call on arrival")
    (row,) = audits(conn)
    assert row["action"] == "supplier_booking.updated"
    assert row["before"] == {"notes": None} and row["after"] == {"notes": "call on arrival"}


@pytest.mark.asyncio
async def test_update_with_nothing_changed_writes_nothing() -> None:
    conn = write_conn()
    out = await sb.update(conn, SALES, SB_ID, {"nights": 2, "net_amount": Decimal("400.50"), "status": "x"})
    assert out["changed"] == [] and out["id"] == SB_ID
    assert not [c for c in conn.calls if c[0].startswith(UPDATE_FACTS)] and conn.sql(AUDIT) == []


@pytest.mark.asyncio
async def test_update_switches_supplier_and_room_after_the_supply_check() -> None:
    conn = write_conn()
    conn.on(SUPPLIER_ROW, {"status": "active", "code": "SUP-000002"}).on(ROOM_OF, 1)
    room2 = "00000000-0000-4000-8000-0000000d0002"
    out = await sb.update(conn, SALES, SB_ID, {"supplier_id": SUPPLIER2, "hotel_room_type_id": room2})
    assert out["changed"] == ["hotel_room_type_id", "supplier_id"]
    assert conn.sql(ROOM_OF) == [(room2, SUPPLIER2)]
    (sql, args) = next(c for c in conn.calls if c[0].startswith(UPDATE_FACTS))
    assert "supplier_id = $3::uuid, hotel_room_type_id = $4::uuid" in sql
    assert args == (SB_ID, "u-1", SUPPLIER2, room2)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changes", "error", "code"),
    [
        ({"item_description": "  "}, Invalid, "description_required"),
        ({"service_id": SERVICE}, Invalid, "one_supply_record"),  # the room stays: not both
        ({"net_currency": None}, Invalid, "net_pair"),
        ({"nights": 99}, Conflict, "invalid_supplier_booking"),
    ],
)
async def test_update_refusals(changes: dict, error: type[Exception], code: str) -> None:
    conn = write_conn()
    conn.on(UPDATE_FACTS, asyncpg.CheckViolationError("violates nights_chk"))
    with pytest.raises(error) as exc:
        await sb.update(conn, SALES, SB_ID, changes)
    assert exc.value.code == code and conn.sql(AUDIT) == []


# ── Transitions ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_request_moves_pending_to_requested_and_advances_the_case() -> None:
    conn = write_conn(asked=1)
    out = await sb.request(conn, SALES, SB_ID)
    assert out["id"] == SB_ID
    (args,) = conn.sql(SET_STATUS)
    assert args[:3] == (SB_ID, "requested", "u-1") and isinstance(args[3], datetime) and args[4] == "u-1"
    assert actions(conn) == ["supplier_booking.requested", "trip_case.status_changed"]
    assert conn.sql(CASE_UPDATE) == [(CASE_ID, "supplier_confirming")]


@pytest.mark.asyncio
async def test_request_again_is_a_no_op_and_a_closed_case_is_refused() -> None:
    conn = write_conn(booking_row(status="requested"))
    await sb.request(conn, SALES, SB_ID)
    assert conn.sql(SET_STATUS) == [] and conn.sql(AUDIT) == []

    conn = write_conn(case=case_row(status="cancelled"))
    with pytest.raises(Conflict) as exc:
        await sb.request(conn, SALES, SB_ID)
    assert exc.value.code == "case_not_bookable" and conn.sql(SET_STATUS) == []


@pytest.mark.asyncio
async def test_a_linked_booking_locks_the_hold_before_the_case_and_the_booking() -> None:
    conn = write_conn(booking_row(b2b_hold_id=HOLD, b2b_line_ref="room"))
    await sb._lock_for_write(conn, SB_ID)
    order = [sql for sql, _ in conn.calls]
    assert HOLD_LOCK in order[1] and LOCK_CASE in order[2]
    assert order[3].endswith("FOR UPDATE OF sb")
    assert conn.sql(HOLD_LOCK) == [(HOLD,)]


@pytest.mark.asyncio
async def test_an_illegal_edge_from_the_database_is_a_conflict() -> None:
    conn = write_conn()
    conn.on(SET_STATUS, asyncpg.CheckViolationError("illegal supplier booking transition\nCONTEXT: x"))
    with pytest.raises(Conflict) as exc:
        await sb.request(conn, SALES, SB_ID)
    assert exc.value.code == "illegal_transition" and exc.value.extra == {"status": "pending"}
    assert conn.sql(AUDIT) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("ref", ["", "   ", "x" * 101])
async def test_confirm_needs_a_ref(ref: str) -> None:
    conn = ScriptedConn()
    with pytest.raises(Invalid) as exc:
        await sb.confirm(conn, SALES, SB_ID, confirmation_ref=ref)
    assert exc.value.code == "confirmation_ref_required" and conn.calls == []


@pytest.mark.asyncio
async def test_confirm_with_a_voucher_on_a_b2c_case() -> None:
    conn = write_conn(booking_row(status="requested"), asked=1, case_status="supplier_confirming")
    await sb.confirm(conn, SALES, SB_ID, confirmation_ref=" R-1 ", voucher_file_id=VOUCHER)
    (sql, args) = next(c for c in conn.calls if c[0].startswith(SET_STATUS))
    assert "confirmation_ref = $4, confirmed_at = $5, confirmed_by = $6, voucher_file_id = $7" in sql
    assert args[:4] == (SB_ID, "confirmed", "u-1", "R-1") and args[5:] == ("u-1", VOUCHER)
    assert actions(conn) == ["supplier_booking.confirmed"]  # already supplier_confirming: no case move
    (row,) = audits(conn)
    assert row["after"] == {"status": "confirmed", "confirmation_ref": "R-1", "voucher_file_id": VOUCHER}
    assert "B2B" not in row["summary"]


@pytest.mark.asyncio
async def test_confirm_a_linked_b2b_booking_accepts_the_line(answer_line: AnswerLine) -> None:
    row = booking_row(status="requested", channel="b2b", b2b_hold_id=HOLD, b2b_line_ref="room")
    conn = write_conn(row, case_row(channel="b2b"), asked=1)
    await sb.confirm(conn, SALES, SB_ID, confirmation_ref="R-1")
    assert answer_line.calls == [((HOLD, "room"), {"accept": True, "via_supplier_booking": True})]
    assert audits(conn)[0]["summary"].endswith("· B2B line accepted")


@pytest.mark.asyncio
async def test_confirm_refusals(answer_line: AnswerLine) -> None:
    # The same answer again changes nothing.
    conn = write_conn(booking_row(status="confirmed", confirmation_ref="R-1"))
    await sb.confirm(conn, SALES, SB_ID, confirmation_ref="R-1")
    assert conn.sql(SET_STATUS) == []

    linked = booking_row(status="requested", b2b_hold_id=HOLD, b2b_line_ref="room")
    with pytest.raises(Forbidden):  # answering the agency's line needs b2b_desk.write too
        await sb.confirm(write_conn(linked), CONTENT, SB_ID, confirmation_ref="R-1")

    b2b = write_conn(booking_row(status="requested"), case_row(channel="b2b"))
    with pytest.raises(Invalid) as exc:
        await sb.confirm(b2b, SALES, SB_ID, confirmation_ref="R-1", voucher_file_id=VOUCHER)
    assert exc.value.code == "document_not_allowed_b2b"

    with pytest.raises(Invalid) as exc:
        await sb.confirm(write_conn(booking_row(status="requested")), SALES, SB_ID, confirmation_ref="R-1",
                         voucher_file_id="voucher.pdf")  # fmt: skip
    assert exc.value.code == "invalid_voucher"

    with pytest.raises(Conflict) as exc:
        await sb.confirm(write_conn(), SALES, SB_ID, confirmation_ref="R-1")
    assert exc.value.code == "illegal_transition" and exc.value.extra == {"status": "pending"}
    assert answer_line.calls == []


@pytest.mark.asyncio
async def test_decline_a_linked_booking_declines_the_line_and_moves_the_case_back(answer_line: AnswerLine) -> None:
    row = booking_row(status="requested", b2b_hold_id=HOLD, b2b_line_ref="room")
    conn = write_conn(row, case_row(channel="b2b"), case_status="supplier_confirming", live=0)
    await sb.decline(conn, SALES, SB_ID, decline_code="sold_out")
    (args,) = conn.sql(SET_STATUS)
    assert args[:4] == (SB_ID, "declined", "u-1", "sold_out")
    assert answer_line.calls == [
        ((HOLD, "room"), {"accept": False, "decline_code": "sold_out", "via_supplier_booking": True})
    ]
    assert actions(conn) == ["supplier_booking.declined", "trip_case.status_changed"]
    assert conn.sql(CASE_UPDATE) == [(CASE_ID, "confirmed")]


@pytest.mark.asyncio
async def test_decline_refusals(answer_line: AnswerLine) -> None:
    with pytest.raises(Invalid) as exc:
        await sb.decline(ScriptedConn(), SALES, SB_ID, decline_code="too_expensive")
    assert exc.value.code == "invalid_decline_code"

    conn = write_conn(booking_row(status="declined", decline_code="sold_out"))
    await sb.decline(conn, SALES, SB_ID, decline_code="sold_out")  # same answer again
    assert conn.sql(SET_STATUS) == []

    with pytest.raises(Forbidden):
        await sb.decline(write_conn(booking_row(status="requested", b2b_hold_id=HOLD)), CONTENT, SB_ID,
                         decline_code="other")  # fmt: skip
    with pytest.raises(Conflict) as exc:
        await sb.decline(write_conn(booking_row(status="confirmed")), SALES, SB_ID, decline_code="other")
    assert exc.value.code == "illegal_transition" and exc.value.extra == {"status": "confirmed"}
    assert answer_line.calls == []


@pytest.mark.asyncio
async def test_cancel_a_requested_booking_with_a_reason() -> None:
    conn = write_conn(booking_row(status="requested"), case_status="supplier_confirming", live=0)
    await sb.cancel(conn, SALES, SB_ID, reason="  traveller changed hotel  ")
    (args,) = conn.sql(SET_STATUS)
    assert args[:4] == (SB_ID, "cancelled", "u-1", "traveller changed hotel")
    assert actions(conn) == ["supplier_booking.cancelled", "trip_case.status_changed"]
    assert audits(conn)[0]["after"] == {"status": "cancelled", "reason": "traveller changed hotel"}


@pytest.mark.asyncio
async def test_cancel_refusals() -> None:
    with pytest.raises(Invalid) as exc:
        await sb.cancel(ScriptedConn(), SALES, SB_ID, reason="  ")
    assert exc.value.code == "reason_required"

    conn = write_conn(booking_row(status="cancelled"))
    await sb.cancel(conn, SALES, SB_ID, reason="again")
    assert conn.sql(SET_STATUS) == []

    told = booking_row(status="confirmed", b2b_hold_id=HOLD, b2b_line_ref="room", b2b_line_status="confirmed")
    with pytest.raises(Conflict) as exc:
        await sb.cancel(write_conn(told), SALES, SB_ID, reason="x")
    assert exc.value.code == "b2b_line_answered"

    with pytest.raises(Conflict) as exc:
        await sb.cancel(write_conn(booking_row(status="declined")), SALES, SB_ID, reason="x")
    assert exc.value.code == "illegal_transition"


@pytest.mark.asyncio
async def test_rerequest_clears_the_confirmation_and_keeps_the_old_ref_in_the_audit() -> None:
    conn = write_conn(booking_row(status="confirmed", confirmation_ref="R-1"))
    await sb.rerequest(conn, SALES, SB_ID, reason=" one more night ")
    (args,) = conn.sql(SET_STATUS)
    assert args[:3] == (SB_ID, "requested", "u-1") and args[4:] == ("u-1", None, None, None)
    (row,) = audits(conn)
    assert row["action"] == "supplier_booking.rerequested"
    assert row["before"] == {"status": "confirmed", "confirmation_ref": "R-1"}
    assert row["after"] == {"status": "requested", "reason": "one more night"}


@pytest.mark.asyncio
async def test_rerequest_refusals() -> None:
    with pytest.raises(Invalid) as exc:
        await sb.rerequest(ScriptedConn(), SALES, SB_ID, reason="")
    assert exc.value.code == "reason_required"
    with pytest.raises(Conflict) as exc:
        await sb.rerequest(write_conn(booking_row(status="confirmed", b2b_hold_id=HOLD)), SALES, SB_ID, reason="x")
    assert exc.value.code == "b2b_line_answered"
    with pytest.raises(Conflict) as exc:
        await sb.rerequest(write_conn(booking_row(status="confirmed"), case_row(status="completed")), SALES, SB_ID,
                           reason="x")  # fmt: skip
    assert exc.value.code == "case_not_bookable"
    with pytest.raises(Conflict) as exc:
        await sb.rerequest(write_conn(booking_row(status="requested")), SALES, SB_ID, reason="x")
    assert exc.value.code == "illegal_transition"


# ── Case cancel cascade ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_case_cancel_cancels_pending_and_flags_the_rest_for_the_supplier() -> None:
    conn = ScriptedConn(
        [
            ("SET status = 'cancelled', cancel_reason = $2", [{"code": "SB-000001"}]),
            ("AND status IN ('requested', 'confirmed') ORDER BY code", [{"code": "SB-000002", "status": "requested"},
                                                                        {"code": "SB-000003", "status": "confirmed"}]),
        ]
    )  # fmt: skip
    out = await sb.on_case_cancelled(conn, case_id=CASE_ID, case_code="TC-000001", cause="agency cancelled")
    assert out == {"cancelled": 1, "to_cancel_with_supplier": 2}
    assert conn.sql("SET status = 'cancelled'") == [(CASE_ID, "Trip case cancelled: agency cancelled")]
    (note,) = conn.sql("INSERT INTO booking.notifications")
    assert note[1] == sb.NOTIFY_CANCEL_TYPE and note[2].startswith("TC-000001 cancelled: cancel 2")
    assert json.loads(note[6]) == {"code": "TC-000001", "supplier_bookings": ["SB-000002", "SB-000003"]}
    (row,) = audits(conn)
    assert row["after"] == {"cancelled": ["SB-000001"], "to_cancel_with_supplier": ["SB-000002", "SB-000003"]}


@pytest.mark.asyncio
async def test_case_cancel_without_bookings_writes_nothing_else() -> None:
    conn = ScriptedConn()
    out = await sb.on_case_cancelled(conn, case_id=CASE_ID, case_code="TC-000001", cause="x")
    assert out == {"cancelled": 0, "to_cancel_with_supplier": 0}
    assert conn.sql("INSERT INTO booking.notifications") == [] and conn.sql(AUDIT) == []


@pytest.mark.asyncio
async def test_live_count() -> None:
    assert await sb.live_count(ScriptedConn(), CASE_ID) == 0
    assert await sb.live_count(ScriptedConn([("AND status IN ('pending', 'requested', 'confirmed')", 3)]), CASE_ID) == 3
