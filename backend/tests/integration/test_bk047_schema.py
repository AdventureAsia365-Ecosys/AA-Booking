"""bk-047 (AA-1075) on a real PostgreSQL: staff-entered bookings (entered_via, b2b_desk_bookings maker-checker and the
booking.created_by_aa event), voucher and supplier cancel fee columns, the cancellation fee override (guard, approval,
booking.fee_changed), partial cancellations (append-only, booking.line_reduced), child / infant / single prices, per-tour
terms and the two replaced views. A contract file. Every test runs in one transaction logged in as role booking and is
rolled back. Names are invented. No import of app code (the migration PR stands alone)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from tests.integration.conftest import apply_migrations

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

BK047 = Path(__file__).resolve().parents[3] / "migrations" / "047_agency_desk_vouchers_cancel_child_terms.sql"
START = date.today() + timedelta(days=40)
MAKER, CHECKER = "it-1075-maker", "it-1075-checker"
CLIENT = "it1075client"


@pytest_asyncio.fixture
async def conn(migrated_admin_dsn: str, migrated_booking_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    # Tests of bk-022 / bk-045 / bk-046 re-run those files, which put back the older outbox trigger, CHECKs and views.
    admin = await asyncpg.connect(migrated_admin_dsn)
    try:
        await apply_migrations(admin, only=[47])
    finally:
        await admin.close()
    c = await asyncpg.connect(migrated_booking_dsn)
    tx = c.transaction()
    await tx.start()
    for staff_id, role in ((MAKER, "sales"), (CHECKER, "admin")):
        await c.execute(
            "INSERT INTO booking.staff_users (cis_user_id, role, active, name) VALUES ($1, $2, true, $1)"
            " ON CONFLICT (cis_user_id) DO NOTHING",
            staff_id,
            role,
        )
    try:
        yield c
    finally:
        await tx.rollback()
        await c.close()


async def _partner(c: asyncpg.Connection) -> tuple[str, str]:
    tid = uuid.uuid4()
    pid = await c.fetchval(
        "INSERT INTO booking.b2b_partners (tenant_id, agency_name, status, billing_currency, m2m_client_id)"
        " VALUES ($1, $2, 'active', 'USD', $3) RETURNING id::text",
        tid,
        f"Harbourlight Tours {tid.hex[:4]}",
        CLIENT,
    )
    return pid, str(tid)


async def _quote(c: asyncpg.Connection, partner: str, total: str = "1000") -> str:
    return await c.fetchval(
        "INSERT INTO booking.b2b_quotes (partner_id, m2m_client_id, proposal_ref, proposal_version, currency,"
        " fx_rate_per_usd, fx_buffer_pct, fx_as_of, fx_source, pricing_rule_valid_from, margin_applied, card_fee_pct,"
        " rounding, priced_on, total_b2b_price, price_hash, valid_until)"
        " VALUES ($1::uuid, $2, $3, 1, 'USD', 1, 0, current_date, 'test', current_date, 0.15, 0,"
        " 'line_ceil_0.01', current_date, $4::numeric, $5, now() + interval '2 days') RETURNING id::text",
        partner,
        CLIENT,
        f"PR-{uuid.uuid4().hex[:8]}",
        total,
        "d" * 64,
    )


async def _case(c: asyncpg.Connection, tenant: str, **cols: object) -> str:
    names = ["channel", "party_tenant_id", "status", "start_date", "end_date", "pax", "currency", "source_system"]
    values: list[object] = ["b2b", tenant, "confirmed", START, START + timedelta(days=3), 4, "USD", "crm"]
    for k, v in cols.items():
        names.append(k)
        values.append(v)
    marks = ", ".join(f"${i}" for i in range(1, len(values) + 1))
    return await c.fetchval(
        f"INSERT INTO booking.trip_cases ({', '.join(names)}) VALUES ({marks}) RETURNING id::text",  # noqa: S608
        *values,
    )


async def _hold(c: asyncpg.Connection, partner: str, quote: str, *, status: str = "active", case: str | None = None):
    return await c.fetchval(
        "INSERT INTO booking.b2b_holds (partner_id, m2m_client_id, quote_id, proposal_ref, proposal_version,"
        " idempotency_key, status, expires_at, trip_case_id, confirmed_at)"
        " VALUES ($1::uuid, $2, $3::uuid, $4, 1, $5, $6, now() + interval '1 day', $7::uuid,"
        " CASE WHEN $6 = 'confirmed' THEN now() END) RETURNING id::text",
        partner,
        CLIENT,
        quote,
        f"PR-{uuid.uuid4().hex[:8]}",
        f"hold-{uuid.uuid4().hex[:12]}",
        status,
        case,
    )


async def _events(c: asyncpg.Connection, hold: str) -> list[str]:
    rows = await c.fetch("SELECT event_type FROM booking.outbox WHERE hold_id = $1::uuid ORDER BY id", hold)
    return [r["event_type"] for r in rows]


async def _policy(c: asyncpg.Connection) -> str:
    return await c.fetchval(
        "INSERT INTO booking.cancellation_policies (channel, valid_from, title, tiers)"
        " VALUES ('b2b', current_date - 1000, $1, '[{\"min_days\": 0, \"fee_pct\": 50}]') RETURNING id::text",
        f"Tidewater terms {uuid.uuid4().hex[:6]}",
    )


async def test_registered_and_rerun_is_a_no_op(migrated_admin_dsn: str) -> None:
    admin = await asyncpg.connect(migrated_admin_dsn)
    try:
        await apply_migrations(admin, only=[47])
        assert await admin.fetchval("SELECT 1 FROM booking.v_schema_versions WHERE version = 'bk-047'") == 1
        before = await admin.fetchval("SELECT count(*) FROM pg_catalog.pg_constraint")
        triggers = await admin.fetchval("SELECT count(*) FROM pg_catalog.pg_trigger WHERE NOT tgisinternal")
        await apply_migrations(admin, only=[47])
        assert await admin.fetchval("SELECT count(*) FROM pg_catalog.pg_constraint") == before
        assert await admin.fetchval("SELECT count(*) FROM pg_catalog.pg_trigger WHERE NOT tgisinternal") == triggers
        for table in ("b2b_desk_bookings", "b2b_line_reductions", "trip_plan_billing_terms"):
            owner = await admin.fetchval(
                "SELECT relowner::regrole::text FROM pg_class WHERE oid = $1::regclass", f"booking.{table}"
            )
            assert owner == "bk_owner"
        assert BK047.read_text(encoding="utf-8").count("-- migrate:") == 0  # contract: no directive line
    finally:
        await admin.close()


async def test_entered_via_defaults_and_checks(conn: asyncpg.Connection) -> None:
    _, tenant = await _partner(conn)
    case = await _case(conn, tenant)
    row = await conn.fetchrow("SELECT entered_via, entered_by FROM booking.trip_cases WHERE id = $1::uuid", case)
    assert dict(row) == {"entered_via": "api", "entered_by": None}
    staff_case = await _case(conn, tenant, entered_via="staff", entered_by=MAKER)
    assert staff_case
    for cols in ({"entered_via": "staff"}, {"entered_via": "api", "entered_by": MAKER}, {"entered_via": "email"}):
        with pytest.raises(asyncpg.CheckViolationError):
            async with conn.transaction():
                await _case(conn, tenant, **cols)


async def test_desk_booking_maker_checker_and_created_by_aa_event(conn: asyncpg.Connection) -> None:
    partner, tenant = await _partner(conn)
    quote = await _quote(conn, partner)
    hold = await _hold(conn, partner, quote)
    desk = await conn.fetchval(
        "INSERT INTO booking.b2b_desk_bookings (partner_id, agency_ref, quote_id, hold_id, status, currency,"
        " quoted_total, floor_total, override_total, override_reason, idempotency_key, entered_by)"
        " VALUES ($1::uuid, 'AG-2041', $2::uuid, $3::uuid, 'awaiting_approval', 'USD', 1000, 900, 850,"
        " 'Repeat group, matched a rival offer', $4, $5) RETURNING id::text",
        partner,
        quote,
        hold,
        f"desk-{uuid.uuid4().hex[:10]}",
        MAKER,
    )
    # The maker never approves its own override; booked needs the trip case.
    case = await _case(conn, tenant, entered_via="staff", entered_by=MAKER)
    with pytest.raises(asyncpg.CheckViolationError):
        async with conn.transaction():
            await conn.execute(
                "UPDATE booking.b2b_desk_bookings SET status = 'booked', trip_case_id = $2::uuid, decided_by = $3,"
                " decided_at = now() WHERE id = $1::uuid",
                desk,
                case,
                MAKER,
            )
    with pytest.raises(asyncpg.CheckViolationError):
        async with conn.transaction():
            await conn.execute(
                "UPDATE booking.b2b_desk_bookings SET status = 'booked' WHERE id = $1::uuid", desk
            )
    # Facts never change.
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        async with conn.transaction():
            await conn.execute("UPDATE booking.b2b_desk_bookings SET override_total = 1 WHERE id = $1::uuid", desk)
    await conn.execute(
        "UPDATE booking.b2b_holds SET status = 'confirmed', confirmed_at = now(), trip_case_id = $2::uuid"
        " WHERE id = $1::uuid",
        hold,
        case,
    )
    await conn.execute(
        "UPDATE booking.b2b_desk_bookings SET status = 'booked', trip_case_id = $2::uuid, decided_by = $3,"
        " decided_at = now() WHERE id = $1::uuid",
        desk,
        case,
        CHECKER,
    )
    assert await _events(conn, hold) == ["booking.created_by_aa"]
    booking_id = await conn.fetchval(
        "SELECT booking_id::text FROM booking.outbox WHERE hold_id = $1::uuid AND event_type = 'booking.created_by_aa'",
        hold,
    )
    assert booking_id == case
    # Final once booked.
    with pytest.raises(asyncpg.CheckViolationError):
        async with conn.transaction():
            await conn.execute("UPDATE booking.b2b_desk_bookings SET status = 'lapsed' WHERE id = $1::uuid", desk)


async def test_api_confirm_emits_no_created_by_aa(conn: asyncpg.Connection) -> None:
    partner, tenant = await _partner(conn)
    quote = await _quote(conn, partner)
    hold = await _hold(conn, partner, quote)
    case = await _case(conn, tenant)
    await conn.execute(
        "UPDATE booking.b2b_holds SET status = 'confirmed', confirmed_at = now(), trip_case_id = $2::uuid"
        " WHERE id = $1::uuid",
        hold,
        case,
    )
    assert await _events(conn, hold) == []


async def test_waiting_needs_an_override_below_the_floor(conn: asyncpg.Connection) -> None:
    partner, _ = await _partner(conn)
    quote = await _quote(conn, partner)
    hold = await _hold(conn, partner, quote)
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(
            "INSERT INTO booking.b2b_desk_bookings (partner_id, agency_ref, quote_id, hold_id, status, currency,"
            " quoted_total, floor_total, idempotency_key, entered_by)"
            " VALUES ($1::uuid, 'AG-1', $2::uuid, $3::uuid, 'awaiting_approval', 'USD', 1000, 900, $4, $5)",
            partner,
            quote,
            hold,
            f"desk-{uuid.uuid4().hex[:10]}",
            MAKER,
        )


async def test_supplier_booking_voucher_fields_and_cancel_fee(conn: asyncpg.Connection) -> None:
    _, tenant = await _partner(conn)
    case = await _case(conn, tenant)
    supplier = await conn.fetchval(
        "INSERT INTO booking.suppliers (company_name, categories, country_code, contact_email)"
        " VALUES ($1, ARRAY['transport'], 'VN', 'desk@example.test') RETURNING id",
        f"Ferncliff Coaches {uuid.uuid4().hex[:5]}",
    )
    sb = await conn.fetchval(
        "INSERT INTO booking.supplier_bookings (trip_case_id, supplier_id, item_description)"
        " VALUES ($1::uuid, $2, 'Airport transfer') RETURNING id::text",
        case,
        supplier,
    )
    await conn.execute(
        "UPDATE booking.supplier_bookings SET service_time = '07:30', pickup_point = 'Lobby, Willowmere Hotel',"
        " dropoff_point = 'Domestic terminal', voucher_note = 'Driver waits 15 minutes' WHERE id = $1::uuid",
        sb,
    )
    with pytest.raises(asyncpg.CheckViolationError):  # a fee only on a cancelled booking
        async with conn.transaction():
            await conn.execute(
                "UPDATE booking.supplier_bookings SET supplier_cancel_fee = 20, supplier_cancel_fee_currency = 'USD'"
                " WHERE id = $1::uuid",
                sb,
            )
    await conn.execute(
        "UPDATE booking.supplier_bookings SET status = 'cancelled', cancel_reason = 'Group cancelled',"
        " cancelled_at = now() WHERE id = $1::uuid",
        sb,
    )
    await conn.execute(
        "UPDATE booking.supplier_bookings SET supplier_cancel_fee = 20, supplier_cancel_fee_currency = 'USD'"
        " WHERE id = $1::uuid",
        sb,
    )
    cost = await conn.fetchval(
        "SELECT cost_usd FROM booking.v_cost_variance WHERE supplier_booking_id = $1::uuid", sb
    )
    assert cost == Decimal("20.00")


async def _cancelled_booking(c: asyncpg.Connection) -> tuple[str, str, str]:
    partner, tenant = await _partner(c)
    quote = await _quote(c, partner)
    case = await _case(c, tenant)
    hold = await _hold(c, partner, quote, status="confirmed", case=case)
    policy = await _policy(c)
    cancel = await c.fetchval(
        "INSERT INTO booking.b2b_cancellations (hold_id, trip_case_id, partner_id, m2m_client_id, policy_id,"
        " reason_code, idempotency_key, departure_date, days_before_departure, fee_pct, currency, total_b2b_price,"
        " fee_amount, refundable_amount, cancelled_at, entered_via, entered_by)"
        " VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5::uuid, 'client_cancelled', $6, $7, 40, 50, 'USD', 1000, 500,"
        " 500, now(), 'staff', $8) RETURNING id::text",
        hold,
        case,
        partner,
        CLIENT,
        policy,
        f"cancel-{uuid.uuid4().hex[:10]}",
        START,
        MAKER,
    )
    return cancel, hold, case


async def test_fee_override_needs_a_second_person_and_emits_fee_changed(conn: asyncpg.Connection) -> None:
    cancel, hold, case = await _cancelled_booking(conn)
    assert await _events(conn, hold) == ["booking.cancelled"]
    # The amounts never change without an approval.
    with pytest.raises(asyncpg.CheckViolationError):
        async with conn.transaction():
            await conn.execute(
                "UPDATE booking.b2b_cancellations SET fee_amount = 0, refundable_amount = 1000 WHERE id = $1::uuid",
                cancel,
            )
    await conn.execute(
        "UPDATE booking.b2b_cancellations SET fee_override_pct = 10, waive_reason = 'Supplier rebooked the seats',"
        " override_by = $2, override_at = now() WHERE id = $1::uuid",
        cancel,
        MAKER,
    )
    with pytest.raises(asyncpg.CheckViolationError):  # the requester never approves
        async with conn.transaction():
            await conn.execute(
                "UPDATE booking.b2b_cancellations SET override_approved_by = $2, override_approved_at = now(),"
                " fee_amount = 100, refundable_amount = 900 WHERE id = $1::uuid",
                cancel,
                MAKER,
            )
    with pytest.raises(asyncpg.CheckViolationError):  # an override is lower than the policy fee
        async with conn.transaction():
            await conn.execute("UPDATE booking.b2b_cancellations SET fee_override_pct = 60 WHERE id = $1::uuid", cancel)
    await conn.execute(
        "UPDATE booking.b2b_cancellations SET override_approved_by = $2, override_approved_at = now(),"
        " fee_amount = 100, refundable_amount = 900 WHERE id = $1::uuid",
        cancel,
        CHECKER,
    )
    assert await _events(conn, hold) == ["booking.cancelled", "booking.fee_changed"]
    with pytest.raises(asyncpg.CheckViolationError):  # final once approved
        async with conn.transaction():
            await conn.execute("UPDATE booking.b2b_cancellations SET waive_reason = 'x' WHERE id = $1::uuid", cancel)
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        async with conn.transaction():
            await conn.execute("UPDATE booking.b2b_cancellations SET fee_pct = 0 WHERE id = $1::uuid", cancel)
    revenue = await conn.fetchval("SELECT revenue FROM booking.v_case_profit WHERE trip_case_id = $1::uuid", case)
    assert revenue == Decimal("100.00")


async def test_line_reduction_is_append_only_and_emits_line_reduced(conn: asyncpg.Connection) -> None:
    partner, tenant = await _partner(conn)
    quote = await _quote(conn, partner)
    case = await _case(conn, tenant)
    hold = await _hold(conn, partner, quote, status="confirmed", case=case)
    policy = await _policy(conn)
    args = [hold, case, partner, policy, f"reduce-{uuid.uuid4().hex[:10]}"]
    insert = (
        "INSERT INTO booking.b2b_line_reductions (hold_id, trip_case_id, partner_id, line_ref, removed_adults,"
        " removed_children, removed_infants, seats_before, seats_after, policy_id, days_before_departure, fee_pct,"
        " currency, removed_amount, fee_amount, refundable_amount, reason_code, idempotency_key, entered_via)"
        " VALUES ($1::uuid, $2::uuid, $3::uuid, 'L1', 1, 0, 0, 4, $6, $4::uuid, 40, 50, 'USD', 250, 125, 125,"
        " 'client_cancelled', $5, 'api') RETURNING id::text"
    )
    with pytest.raises(asyncpg.CheckViolationError):  # seats_after must match
        async with conn.transaction():
            await conn.fetchval(insert, *args, 2)
    red = await conn.fetchval(insert, *args, 3)
    assert await _events(conn, hold) == ["booking.line_reduced"]
    line_ref = await conn.fetchval(
        "SELECT line_ref FROM booking.outbox WHERE hold_id = $1::uuid AND event_type = 'booking.line_reduced'", hold
    )
    assert line_ref == "L1"
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        async with conn.transaction():
            await conn.execute("UPDATE booking.b2b_line_reductions SET fee_amount = 0 WHERE id = $1::uuid", red)
    revenue = await conn.fetchval("SELECT revenue FROM booking.v_case_profit WHERE trip_case_id = $1::uuid", case)
    assert revenue == Decimal("875.00")  # 1000 - 250 removed + 125 fee kept


async def test_child_infant_single_columns_and_age_bands(conn: asyncpg.Connection) -> None:
    plan = await conn.fetchval(
        "INSERT INTO booking.trip_plans (source_system, source_id, sellable, child_age_max, infant_age_max)"
        " VALUES ('cis', $1, true, 11, 1) RETURNING id",
        f"it-1075-{uuid.uuid4().hex[:8]}",
    )
    await conn.execute(
        "INSERT INTO booking.trip_plan_prices (trip_plan_id, position, channel, valid_from, valid_to, pax_min, currency,"
        " amount, net_amount, net_currency, priced_on, child_amount, infant_amount, single_supplement_amount,"
        " child_net_amount, infant_net_amount, single_supplement_net_amount)"
        " VALUES ($1, 1, 'b2b', current_date, current_date + 30, 2, 'USD', 500, 400, 'USD', current_date, 375, 20, 90,"
        " 300, 15, 70)",
        plan,
    )
    with pytest.raises(asyncpg.CheckViolationError):  # a net without the adult net
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO booking.trip_plan_prices (trip_plan_id, position, channel, valid_from, valid_to, pax_min,"
                " currency, amount, child_net_amount) VALUES ($1, 2, 'b2b', current_date, current_date, 1, 'USD', 500,"
                " 300)",
                plan,
            )
    with pytest.raises(asyncpg.CheckViolationError):  # infant band below the child band
        async with conn.transaction():
            await conn.execute("UPDATE booking.trip_plans SET infant_age_max = 3, child_age_max = 3 WHERE id = $1", plan)
    with pytest.raises(asyncpg.CheckViolationError):
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO booking.pricing_rules (channel, target_margin, floor_margin, peak_uplift_pts,"
                " card_fee_pct, valid_from, child_pct) VALUES ('b2b', 0.2, 0.1, 0, 0, current_date + 900, 1.5)"
            )


async def test_trip_plan_terms_one_row_per_scope(conn: asyncpg.Connection) -> None:
    plan = await conn.fetchval(
        "INSERT INTO booking.trip_plans (source_system, source_id, sellable) VALUES ('cis', $1, true) RETURNING id",
        f"it-1075-{uuid.uuid4().hex[:8]}",
    )
    tenant = uuid.uuid4()
    for party in (None, tenant):
        await conn.execute(
            "INSERT INTO booking.trip_plan_billing_terms (trip_plan_id, party_tenant_id, deposit_pct,"
            " balance_days_before_departure, updated_by) VALUES ($1, $2, 0.5, 45, $3)",
            plan,
            party,
            CHECKER,
        )
    with pytest.raises(asyncpg.UniqueViolationError):
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO booking.trip_plan_billing_terms (trip_plan_id, deposit_pct, balance_days_before_departure)"
                " VALUES ($1, 0.2, 30)",
                plan,
            )
    with pytest.raises(asyncpg.CheckViolationError):
        async with conn.transaction():
            await conn.execute(
                "UPDATE booking.trip_plan_billing_terms SET deposit_pct = 1.5 WHERE trip_plan_id = $1", plan
            )


async def test_cis_app_reads_none_of_the_new_tables(migrated_admin_dsn: str) -> None:
    admin = await asyncpg.connect(migrated_admin_dsn)
    try:
        for table in ("b2b_desk_bookings", "b2b_line_reductions", "trip_plan_billing_terms"):
            assert not await admin.fetchval(
                "SELECT has_table_privilege('cis_app', $1, 'SELECT') AS ok", f"booking.{table}"
            )
    finally:
        await admin.close()
