"""AA-1149 part 1 (bk-065) on a real PostgreSQL, as role ``booking`` where the app writes: registration and re-run
no-op, the policy seed, grants, and the database rules of the tour split and the sale exception:

- quote parts: CHECKs (one target, the room mix only on a room part, the net pair), append-only for role booking;
- supplier bookings of a part: only a part of the case's own confirmed hold quote (no departure line, no hold line
  link), one live booking per part, never with a version item, the part never changes, the quantity is a fact;
- the supply view counts the parts of confirmed hold lines without a departure, an on-request line only once the desk
  accepted it, a waived part not at all, and a part is booked when a confirmed booking names it;
- waivers: never while a live booking names the part, 20..500 characters, final (except redaction);
- sale exceptions: N1 / N3 findings only, one unrevoked per tour, at most 90 days, revoked once and final, redactable;
  a hold confirmed with a line covered by an exception adds one custom trip check to the new case;
- trip_plans.no_hotel_nights range.

The supply graph is written in SQL the way the services write it (a quote with a tp: line and its parts, a hold
confirmed into a B2B case). All names are invented."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from tests.integration import ops

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[3] / "migrations" / "065_tour_split_operable.sql"
STAFF, OTHER = "it-1149-ops", "it-1149-admin"
SOURCE = "e1000000-0000-4000-8000-000000001149"
TENANT = "e2000000-0000-4000-8000-000000001149"
CLIENT = "it1149client"
REDACT_WAIVER = "UPDATE booking.trip_case_part_waivers SET reason = '[redacted]' WHERE id = $1::uuid"
GUARD_ERRORS = (asyncpg.CheckViolationError, asyncpg.UniqueViolationError, asyncpg.InsufficientPrivilegeError)


async def _purge(admin: asyncpg.Connection) -> None:
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE bk_owner")
        await admin.execute("DELETE FROM booking.trip_case_part_waivers")
        await admin.execute("DELETE FROM booking.supplier_bookings WHERE quote_part_id IS NOT NULL")
        await admin.execute("DELETE FROM booking.b2b_hold_lines WHERE hold_id IN"
                            " (SELECT h.id FROM booking.b2b_holds h WHERE h.m2m_client_id = $1)", CLIENT)  # fmt: skip
        await admin.execute("DELETE FROM booking.b2b_holds WHERE m2m_client_id = $1", CLIENT)
        await admin.execute("DELETE FROM booking.b2b_quote_lines WHERE quote_id IN"
                            " (SELECT q.id FROM booking.b2b_quotes q WHERE q.m2m_client_id = $1)", CLIENT)  # fmt: skip
        await admin.execute("DELETE FROM booking.b2b_quotes WHERE m2m_client_id = $1", CLIENT)
        await admin.execute("DELETE FROM booking.departures WHERE trip_plan_id IN"
                            " (SELECT id FROM booking.trip_plans WHERE source_id = $1)", SOURCE)  # fmt: skip
        await admin.execute("DELETE FROM booking.trip_plan_operable_overrides WHERE trip_plan_id IN"
                            " (SELECT id FROM booking.trip_plans WHERE source_id = $1)", SOURCE)  # fmt: skip
        await admin.execute("DELETE FROM booking.trip_plans WHERE source_id = $1", SOURCE)
        await admin.execute("DELETE FROM booking.b2b_partners WHERE m2m_client_id = $1", CLIENT)
    await ops.purge_ops(admin)
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE bk_owner")
        await admin.execute("DELETE FROM booking.trip_cases WHERE notes LIKE 'it-682-1149%'")


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    await _purge(conn)
    await ops.staff(conn, (STAFF, "contracting"), (OTHER, "admin"))
    try:
        yield conn
    finally:
        await _purge(conn)
        await conn.close()


@pytest_asyncio.fixture
async def app_conn(admin: asyncpg.Connection, migrated_booking_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_booking_dsn)
    try:
        yield conn
    finally:
        await conn.close()


async def _plan(admin: asyncpg.Connection) -> str:
    return await admin.fetchval(
        "INSERT INTO booking.trip_plans (source_system, source_id, sellable) VALUES ('cis', $1, true)"
        " ON CONFLICT (source_system, source_id) DO UPDATE SET sellable = true RETURNING id::text",
        SOURCE,
    )


async def _partner(admin: asyncpg.Connection) -> str:
    found = await admin.fetchval("SELECT id::text FROM booking.b2b_partners WHERE m2m_client_id = $1", CLIENT)
    return found or await admin.fetchval(
        "INSERT INTO booking.b2b_partners (tenant_id, agency_name, m2m_client_id) VALUES ($1::uuid, 'Agency 1149', $2)"
        " RETURNING id::text",
        TENANT,
        CLIENT,
    )


async def _sold(
    admin: asyncpg.Connection,
    sup: dict[str, str],
    *,
    tag: str,
    held_as: str = "available",
    departure: bool = False,
    override: str | None = None,
    confirm: bool = True,
) -> dict[str, Any]:
    """A quote with one tp: line of two parts (the room on day 1, the service on day 2) and, with ``departure``, a
    second tp: line on a departure with one part; a hold of it, confirmed into a new B2B case."""
    plan, partner = await _plan(admin), await _partner(admin)
    quote = await admin.fetchval(
        "INSERT INTO booking.b2b_quotes (partner_id, m2m_client_id, proposal_ref, proposal_version, currency,"
        " fx_rate_per_usd, fx_buffer_pct, fx_as_of, fx_source, pricing_rule_valid_from, margin_applied, card_fee_pct,"
        " rounding, priced_on, total_b2b_price, price_hash, valid_until)"
        " VALUES ($1::uuid, $2, $3, 1, 'USD', 1, 0, current_date, 'base', current_date, 0.2, 0, 'cent', current_date,"
        " 900, repeat('a', 64), now() + interval '7 days') RETURNING id::text",
        partner,
        CLIENT,
        f"P-1149-{tag}",
    )
    lines = [(1, "tour", None)]
    if departure:
        dep = await admin.fetchval(
            "INSERT INTO booking.departures (trip_plan_id, start_date, capacity) VALUES ($1::uuid, $2, 10)"
            " RETURNING id::text",
            plan,
            ops.START + timedelta(days=3 + len(tag)),
        )
        lines.append((2, "fixed", dep))
    for no, ref, dep in lines:
        await admin.execute(
            "INSERT INTO booking.b2b_quote_lines (quote_id, line_no, line_ref, component_id, component_kind,"
            " service_date, pax_adult, pax_child, pax_infant, rooms, unit, availability, b2b_price, net_cost_usd,"
            " supplier_rate_ids, departure_id, agency_discount_pct, operable_override_id)"
            " VALUES ($1::uuid, $2, $3, $4, 'trip_plan', $5, 2, 0, 0, '[]', 'per_line', 'available', 450, 300,"
            " ARRAY[gen_random_uuid()], $6::uuid, 0, $7::uuid)",
            quote,
            no,
            ref,
            f"tp:{plan}",
            ops.START,
            dep,
            override if no == 1 else None,
        )
    parts = {
        "room": await _part(admin, quote, 1, 1, sup, room=True),
        "service": await _part(admin, quote, 1, 2, sup, room=False),
    }
    if departure:
        parts["fixed"] = await _part(admin, quote, 2, 1, sup, room=False)
    case = await ops.b2b_case(admin, tag=f"1149{tag}", start=ops.START)
    hold = await admin.fetchval(
        "INSERT INTO booking.b2b_holds (partner_id, m2m_client_id, quote_id, proposal_ref, proposal_version,"
        " idempotency_key, status, expires_at) VALUES ($1::uuid, $2, $3::uuid, $4, 1, $5, 'active',"
        " now() + interval '1 day') RETURNING id::text",
        partner,
        CLIENT,
        quote,
        f"P-1149-{tag}",
        f"it-1149-{tag}-{uuid.uuid4().hex[:8]}",
    )
    for no, ref, dep in lines:
        line_held = held_as if no == 1 else "available"
        await admin.execute(
            "INSERT INTO booking.b2b_hold_lines (hold_id, line_no, line_ref, departure_id, seats, held_as, status)"
            " VALUES ($1::uuid, $2, $3, $4::uuid, 2, $5, $5)",
            hold,
            no,
            ref,
            dep,
            line_held,
        )
    if confirm:
        await _confirm(admin, hold, case["id"])
    return {"case": case["id"], "quote": quote, "hold": hold, "parts": parts, "plan": plan}


async def _confirm(admin: asyncpg.Connection, hold: str, case_id: str) -> None:
    await admin.execute(
        "UPDATE booking.b2b_holds SET status = 'confirmed', confirmed_at = now(), trip_case_id = $2::uuid"
        " WHERE id = $1::uuid",
        hold,
        case_id,
    )
    await admin.execute(
        "UPDATE booking.b2b_hold_lines SET status = 'confirmed' WHERE hold_id = $1::uuid AND held_as = 'available'",
        hold,
    )


async def _part(
    admin: asyncpg.Connection, quote: str, line_no: int, part_no: int, sup: dict[str, str], *, room: bool
) -> str:
    return await admin.fetchval(
        "INSERT INTO booking.b2b_quote_line_parts (quote_id, line_no, part_no, day_no, service_date,"
        " hotel_room_type_id, service_id, supplier_id, quantity, pax, rooms, net_amount, net_currency,"
        " supplier_rate_ids) VALUES ($1::uuid, $2, $3, $3, $4, $5::uuid, $6::uuid, $7::uuid, $8, 2, $9::jsonb, 100,"
        " 'USD', ARRAY[gen_random_uuid()]) RETURNING id::text",
        quote,
        line_no,
        part_no,
        ops.START + timedelta(days=part_no - 1),
        sup["room"] if room else None,
        None if room else sup["service"],
        sup["supplier"],
        1 if room else 2,
        json.dumps([{"occupancy": "double", "count": 1}]) if room else None,
    )


async def _book(conn: asyncpg.Connection, case_id: str, part: str, sup: dict[str, str], **extra: Any) -> str:
    cols = {"trip_case_id": case_id, "quote_part_id": part, "supplier_id": sup["supplier"], **extra}
    names = ", ".join(cols)
    marks = ", ".join(f"${i + 1}" for i in range(len(cols)))
    return await conn.fetchval(
        f"INSERT INTO booking.supplier_bookings ({names}, item_description, created_by)"  # noqa: S608 — test columns
        f" VALUES ({marks}, 'Component', '{STAFF}') RETURNING id::text",
        *cols.values(),
    )


async def _state(conn: asyncpg.Connection, case_id: str) -> tuple[int, int, str]:
    row = await conn.fetchrow(
        "SELECT items_to_book, items_booked, state FROM booking.v_trip_case_supply_state WHERE trip_case_id = $1::uuid",
        case_id,
    )
    return row["items_to_book"], row["items_booked"], row["state"]


async def _confirm_booking(conn: asyncpg.Connection, booking: str) -> None:
    await conn.execute(
        "UPDATE booking.supplier_bookings SET status = 'requested', requested_at = now() WHERE id = $1::uuid", booking
    )
    await conn.execute(
        "UPDATE booking.supplier_bookings SET status = 'confirmed', confirmation_ref = 'R1', confirmed_at = now()"
        " WHERE id = $1::uuid",
        booking,
    )


# ── Registration, seed, grants ──────────────────────────────────────────────────────────────────────────────────


async def test_registered_once_and_rerun_is_a_no_op(admin: asyncpg.Connection) -> None:
    policy = "SELECT id, param_key, value FROM booking.pricing_policy WHERE created_by = 'seed:bk-065'"
    before = sorted((str(r["id"]), r["param_key"], r["value"]) for r in await admin.fetch(policy))
    await admin.execute("SET ROLE bk_owner")
    try:
        await admin.execute(MIGRATION.read_text(encoding="utf-8"))
    finally:
        await admin.execute("RESET ROLE")
    assert sorted((str(r["id"]), r["param_key"], r["value"]) for r in await admin.fetch(policy)) == before
    assert await admin.fetchval("SELECT count(*) FROM shared.schema_versions WHERE version = 'bk-065'") == 1


async def test_contract_file_without_a_directive() -> None:
    text = MIGRATION.read_text(encoding="utf-8")
    assert text.startswith("-- Migration bk-065")
    assert "-- migrate:" not in text


async def test_policy_seeded_and_approved(admin: asyncpg.Connection) -> None:
    rows = await admin.fetch(
        "SELECT p.param_key, p.value::text AS value, bool_or(e.event = 'approved') AS approved"
        " FROM booking.pricing_policy p JOIN booking.pricing_policy_events e ON e.policy_id = p.id"
        " WHERE p.created_by = 'seed:bk-065' GROUP BY p.param_key, p.value"
    )
    assert {r["param_key"]: (r["value"], r["approved"]) for r in rows} == {
        "operable_exception_default_days": ("14", True),
        "operable_exception_max_days": ("30", True),
    }


async def test_grants_are_narrow(admin: asyncpg.Connection) -> None:
    table = "SELECT has_table_privilege('booking', $1, $2)"
    for name, priv, allowed in (
        ("booking.b2b_quote_line_parts", "INSERT", True),
        ("booking.b2b_quote_line_parts", "UPDATE", False),
        ("booking.b2b_quote_line_parts", "DELETE", False),
        ("booking.trip_case_part_waivers", "INSERT", True),
        ("booking.trip_case_part_waivers", "UPDATE", False),
        ("booking.trip_case_part_waivers", "DELETE", False),
        ("booking.trip_plan_operable_overrides", "INSERT", True),
        ("booking.trip_plan_operable_overrides", "DELETE", False),
        ("booking.v_trip_case_supply_state", "SELECT", True),
        ("booking.v_trip_case_supply_state", "UPDATE", False),
    ):
        assert await admin.fetchval(table, name, priv) is allowed, (name, priv)
    column = "SELECT has_column_privilege('booking', 'booking.trip_plan_operable_overrides', $1, 'UPDATE')"
    assert await admin.fetchval(column, "revoked_at") is True
    assert await admin.fetchval(column, "findings") is False
    assert await admin.fetchval(column, "expires_at") is False


# ── Parts and their bookings ────────────────────────────────────────────────────────────────────────────────────


async def test_part_checks(admin: asyncpg.Connection) -> None:
    sup = await ops.supply(admin, "Parts")
    sold = await _sold(admin, sup, tag="chk")
    insert = (
        "INSERT INTO booking.b2b_quote_line_parts (quote_id, line_no, part_no, day_no, service_date,"
        " hotel_room_type_id, service_id, supplier_id, quantity, pax, rooms, net_amount, net_currency,"
        " supplier_rate_ids) VALUES ($1::uuid, 1, $2, 1, current_date, $3::uuid, $4::uuid, $5::uuid, 1, 2, $6::jsonb,"
        " $7, $8, ARRAY[gen_random_uuid()])"
    )
    mix = json.dumps([{"occupancy": "single", "count": 1}])
    for part_no, room, service, rooms, net, cur in (
        (10, sup["room"], sup["service"], mix, 1, "USD"),  # two targets
        (11, sup["room"], None, None, 1, "USD"),  # a room part needs its mix
        (12, None, sup["service"], mix, 1, "USD"),  # a service part has none
        (13, None, sup["service"], None, 1, None),  # net pair
    ):
        with pytest.raises(asyncpg.CheckViolationError):
            await admin.execute(insert, sold["quote"], part_no, room, service, sup["supplier"], rooms, net, cur)
    await admin.execute(insert, sold["quote"], 14, None, sup["service"], sup["supplier"], None, None, None)


async def test_a_booking_books_a_part_of_its_own_case(admin: asyncpg.Connection, app_conn: asyncpg.Connection) -> None:
    sup = await ops.supply(admin, "Own")
    mine = await _sold(admin, sup, tag="own", departure=True)
    other = await _sold(admin, sup, tag="oth")
    booking = await _book(app_conn, mine["case"], mine["parts"]["room"], sup, quantity=1)
    with pytest.raises(asyncpg.UniqueViolationError):  # one live booking per part
        await _book(app_conn, mine["case"], mine["parts"]["room"], sup)
    with pytest.raises(asyncpg.CheckViolationError, match="not a part of the confirmed booking"):
        await _book(app_conn, mine["case"], other["parts"]["room"], sup)
    with pytest.raises(asyncpg.CheckViolationError, match="not a part of the confirmed booking"):
        await _book(app_conn, mine["case"], mine["parts"]["fixed"], sup)  # a departure line's cost: per departure
    with pytest.raises(asyncpg.CheckViolationError):
        await _book(app_conn, mine["case"], mine["parts"]["service"], sup, quantity=1000)
    item = await admin.fetchval("SELECT gen_random_uuid()")
    with pytest.raises(asyncpg.CheckViolationError):
        await _book(app_conn, mine["case"], mine["parts"]["service"], sup, version_item_id=item)
    with pytest.raises(asyncpg.CheckViolationError, match="never change"):
        await app_conn.execute(
            "UPDATE booking.supplier_bookings SET quote_part_id = $2::uuid WHERE id = $1::uuid",
            booking,
            mine["parts"]["service"],
        )
    await app_conn.execute("UPDATE booking.supplier_bookings SET quantity = 2 WHERE id = $1::uuid", booking)
    await _confirm_booking(app_conn, booking)
    with pytest.raises(asyncpg.CheckViolationError, match="facts are fixed"):
        await app_conn.execute("UPDATE booking.supplier_bookings SET quantity = 3 WHERE id = $1::uuid", booking)
    # Cancelled, the part takes a replacement.
    await app_conn.execute(
        "UPDATE booking.supplier_bookings SET status = 'cancelled', cancel_reason = 'x', cancelled_at = now()"
        " WHERE id = $1::uuid",
        booking,
    )
    assert await _book(app_conn, mine["case"], mine["parts"]["room"], sup)


async def test_a_part_of_an_unconfirmed_hold_is_not_bookable(
    admin: asyncpg.Connection, app_conn: asyncpg.Connection
) -> None:
    sup = await ops.supply(admin, "Unconf")
    sold = await _sold(admin, sup, tag="unc", confirm=False)
    with pytest.raises(asyncpg.CheckViolationError, match="not a part of the confirmed booking"):
        await _book(app_conn, sold["case"], sold["parts"]["room"], sup)


# ── Supply state ────────────────────────────────────────────────────────────────────────────────────────────────


async def test_supply_state_counts_the_parts(admin: asyncpg.Connection, app_conn: asyncpg.Connection) -> None:
    sup = await ops.supply(admin, "State")
    sold = await _sold(admin, sup, tag="st", departure=True)
    # Two parts of the tour line; the departure line's part is booked per departure, not here.
    assert await _state(app_conn, sold["case"]) == (2, 0, "confirming")
    room = await _book(app_conn, sold["case"], sold["parts"]["room"], sup)
    await _confirm_booking(app_conn, room)
    assert await _state(app_conn, sold["case"]) == (2, 1, "confirming")
    # A waiver closes the other part: nothing left to book.
    with pytest.raises(asyncpg.CheckViolationError):
        await app_conn.execute(
            "INSERT INTO booking.trip_case_part_waivers (trip_case_id, quote_part_id, reason, created_by)"
            " VALUES ($1::uuid, $2::uuid, 'too short', $3)",
            sold["case"],
            sold["parts"]["service"],
            STAFF,
        )
    await app_conn.execute(
        "INSERT INTO booking.trip_case_part_waivers (trip_case_id, quote_part_id, reason, created_by)"
        " VALUES ($1::uuid, $2::uuid, 'The agency dropped the transfer after the sale.', $3)",
        sold["case"],
        sold["parts"]["service"],
        STAFF,
    )
    assert await _state(app_conn, sold["case"]) == (1, 1, "booked")


async def test_an_on_request_line_counts_once_accepted(admin: asyncpg.Connection, app_conn: asyncpg.Connection) -> None:
    sup = await ops.supply(admin, "OnReq")
    sold = await _sold(admin, sup, tag="orq", held_as="on_request")
    assert await _state(app_conn, sold["case"]) == (0, 0, "none")
    with pytest.raises(asyncpg.CheckViolationError):  # nothing to book until the desk accepts the line
        await app_conn.execute(
            "INSERT INTO booking.trip_case_part_waivers (trip_case_id, quote_part_id, reason, created_by)"
            " VALUES ($1::uuid, gen_random_uuid(), 'A part of another booking entirely.', $2)",
            sold["case"],
            STAFF,
        )
    await admin.execute(
        "UPDATE booking.b2b_hold_lines SET status = 'confirmed', answered_at = now() WHERE hold_id = $1::uuid",
        sold["hold"],
    )
    assert await _state(app_conn, sold["case"]) == (2, 0, "confirming")


async def test_waiver_rules(admin: asyncpg.Connection, app_conn: asyncpg.Connection) -> None:
    sup = await ops.supply(admin, "Waive")
    sold = await _sold(admin, sup, tag="wv")
    await _book(app_conn, sold["case"], sold["parts"]["room"], sup)
    waive = (
        "INSERT INTO booking.trip_case_part_waivers (trip_case_id, quote_part_id, reason, created_by)"
        " VALUES ($1::uuid, $2::uuid, 'No hotel is needed: the guests stay with family.', $3) RETURNING id::text"
    )
    with pytest.raises(asyncpg.CheckViolationError, match="live supplier booking"):
        await app_conn.fetchval(waive, sold["case"], sold["parts"]["room"], STAFF)
    waiver = await app_conn.fetchval(waive, sold["case"], sold["parts"]["service"], STAFF)
    with pytest.raises(GUARD_ERRORS):
        await app_conn.execute(REDACT_WAIVER, waiver)
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE bk_owner")
        with pytest.raises(asyncpg.CheckViolationError, match="final"):
            async with admin.transaction():
                await admin.execute(
                    "UPDATE booking.trip_case_part_waivers SET reason = 'Another long enough reason here.'"
                    " WHERE id = $1::uuid",
                    waiver,
                )
        await admin.execute(REDACT_WAIVER, waiver)


# ── Sale exceptions ─────────────────────────────────────────────────────────────────────────────────────────────

OVERRIDE = (
    "INSERT INTO booking.trip_plan_operable_overrides (trip_plan_id, rules, findings, reason, expires_at, created_by)"
    " VALUES ($1::uuid, $2::text[], $3::text[], $4, now() + make_interval(days => $5), $6) RETURNING id::text"
)
REASON = "The hotel for night 2 is booked by hand with the family guesthouse."


async def test_sale_exception_rules(admin: asyncpg.Connection, app_conn: asyncpg.Connection) -> None:
    plan = await _plan(admin)
    for rules, findings, reason, days in (
        (["N2"], ["N2:day:1:HTL-000001"], REASON, 14),  # a missing rate is never covered
        (["N1"], ["N3:supplier:SUP-000001"], REASON, 14),  # a finding outside its rules
        (["N1"], ["N1:night:2"], "too short", 14),
        (["N1"], ["N1:night:2"], REASON, 91),  # the 90-day backstop
        (["N1"], [], REASON, 14),
    ):
        with pytest.raises(asyncpg.CheckViolationError):
            await app_conn.fetchval(OVERRIDE, plan, rules, findings, reason, days, STAFF)
    first = await app_conn.fetchval(
        OVERRIDE, plan, ["N1", "N3"], ["N1:night:2", "N3:supplier:SUP-000045"], REASON, 14, STAFF
    )
    with pytest.raises(asyncpg.UniqueViolationError):  # one unrevoked per tour
        await app_conn.fetchval(OVERRIDE, plan, ["N1"], ["N1:night:3"], REASON, 14, STAFF)
    with pytest.raises(GUARD_ERRORS):
        await app_conn.execute(
            "UPDATE booking.trip_plan_operable_overrides SET expires_at = now() + interval '20 days'"
            " WHERE id = $1::uuid",
            first,
        )
    revoke = (
        "UPDATE booking.trip_plan_operable_overrides SET revoked_by = $2, revoked_at = now(), revoke_reason = $3"
        " WHERE id = $1::uuid"
    )
    await app_conn.execute(revoke, first, OTHER, "Hotel found.")
    with pytest.raises(asyncpg.CheckViolationError, match="final"):
        await app_conn.execute(revoke, first, STAFF, "Again.")
    assert await app_conn.fetchval(OVERRIDE, plan, ["N1"], ["N1:night:3"], REASON, 14, STAFF)
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE bk_owner")
        await admin.execute(
            "UPDATE booking.trip_plan_operable_overrides SET reason = '[redacted]', revoke_reason = '[redacted]'"
            " WHERE id = $1::uuid",
            first,
        )


async def test_a_sale_under_an_exception_gets_one_trip_check(
    admin: asyncpg.Connection, app_conn: asyncpg.Connection
) -> None:
    sup = await ops.supply(admin, "Except")
    plan = await _plan(admin)
    override = await app_conn.fetchval(OVERRIDE, plan, ["N1"], ["N1:night:2"], REASON, 14, STAFF)
    sold = await _sold(admin, sup, tag="ex", override=override, confirm=False)
    custom = "SELECT title, note FROM booking.trip_case_checks WHERE trip_case_id = $1::uuid AND code = 'custom'"
    assert await admin.fetch(custom, sold["case"]) == []
    await _confirm(admin, sold["hold"], sold["case"])
    rows = await admin.fetch(custom, sold["case"])
    assert len(rows) == 1
    assert rows[0]["title"] == "Sold under a sale exception: book by hand what is missing (night 2 has no hotel)"
    assert "Agency line tour" in rows[0]["note"]
    # Fires once: a later status write of the confirmed hold adds nothing.
    await admin.execute("UPDATE booking.b2b_holds SET status = 'confirmed' WHERE id = $1::uuid", sold["hold"])
    assert len(await admin.fetch(custom, sold["case"])) == 1


async def test_no_hotel_nights_range(admin: asyncpg.Connection) -> None:
    plan = await _plan(admin)
    await admin.execute("UPDATE booking.trip_plans SET no_hotel_nights = '{2,3}' WHERE id = $1::uuid", plan)
    for bad in ([0], [366]):
        with pytest.raises(asyncpg.CheckViolationError):
            await admin.execute("UPDATE booking.trip_plans SET no_hotel_nights = $2 WHERE id = $1::uuid", plan, bad)
