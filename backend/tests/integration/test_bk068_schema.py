"""AA-1151 (bk-068) on a real PostgreSQL: registration and re-run no-op, the two policy seeds, grants, the database
rules of add-on offers and the read model ``booking.v_place_addons``.

- offers: start as a draft; the trigger keeps the writer list (the client value is ignored; adding a place makes its
  creator a writer); no writer publishes (CHECK), unless the business owner, flagged (a forged flag is refused); a
  published offer keeps its rules; reopening resets the writers to the person who reopened; never deleted;
- places: written while the offer is a draft only;
- the view: only published offers of active services and suppliers, only on the days the offer runs (weekday), with an
  adult rate in force under a published contract and no stop-sale in effect; inventory free_sale / allotment from the
  shared restriction rows, else on_request; no net or supplier column; role tripplanner may read it.

Dates are relative to the database's UTC day (the view starts today). All names are invented."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date, time, timedelta
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from tests.integration import ops
from tests.integration.supply import purge_supply

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[3] / "migrations" / "068_addon_offers.sql"
ISS = "https://cognito-idp.us-west-1.amazonaws.com/us-west-1_it1151"
OWNER_SUB = "b1151000-0000-4000-8000-00000000000a"
OWNER = f"cog:{OWNER_SUB}"
MAKER, CHECKER, EDITOR = "it-1151-maker", "it-1151-checker", "it-1151-editor"
HANOI = "d0000000-0000-4000-8000-000000000001"
HOI_AN = "d0000000-0000-4000-8000-000000000003"
PUBLISH = (
    "UPDATE booking.addon_offers SET status = 'published', published_by = $2, published_at = now(),"
    " owner_self_approved = $3 WHERE id = $1::uuid"
)
REOPEN = (
    "UPDATE booking.addon_offers SET status = 'draft', published_by = NULL, published_at = NULL,"
    " owner_self_approved = false, retired_by = NULL, retired_at = NULL, updated_by = $2 WHERE id = $1::uuid"
)


async def _purge(admin: asyncpg.Connection) -> None:
    await purge_supply(admin)
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE bk_owner")
        await admin.execute("DELETE FROM booking.supplier_contracts WHERE title LIKE 'it-1151%'")
        await admin.execute("DELETE FROM booking.suppliers WHERE company_name LIKE 'it-1151%'")
    await admin.execute("DELETE FROM shared.staff_booking_grant_stub WHERE cognito_sub LIKE 'b1151000-%'")


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    await _purge(conn)
    await ops.staff(conn, (MAKER, "contracting"), (CHECKER, "admin"), (EDITOR, "content_creator"))
    await conn.execute(
        "INSERT INTO booking.staff_users (cis_user_id, role, active, name, cognito_iss, cognito_sub)"
        " VALUES ($1, 'admin', true, $1, $2, $3) ON CONFLICT (cis_user_id) DO UPDATE SET active = true",
        OWNER,
        ISS,
        OWNER_SUB,
    )
    await conn.execute(
        "INSERT INTO shared.staff_booking_grant_stub (cognito_sub, booking_role_code, is_business_owner)"
        " VALUES ($1, NULL, true) ON CONFLICT (cognito_sub) DO UPDATE SET is_business_owner = true",
        OWNER_SUB,
    )
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


async def _today(conn: asyncpg.Connection) -> date:
    return await conn.fetchval("SELECT (now() AT TIME ZONE 'UTC')::date")


async def _supply(admin: asyncpg.Connection, title: str = "Lotus pond kayak") -> dict[str, str]:
    """An active supplier with one per-person activity and a draft contract from today for 30 days."""
    today = await _today(admin)
    sid = await admin.fetchval(
        "INSERT INTO booking.suppliers (company_name, categories, country_code, status, created_by)"
        " VALUES ($1, ARRAY['activity'], 'VN', 'active', $2) RETURNING id::text",
        f"it-1151 {title} Co",
        MAKER,
    )
    service = await admin.fetchval(
        "INSERT INTO booking.services (supplier_id, service_type, unit, title, destination_id, created_by)"
        " VALUES ($1::uuid, 'activity', 'person', $2, $3::uuid, $4) RETURNING id::text",
        sid,
        title,
        HANOI,
        MAKER,
    )
    contract = await admin.fetchval(
        "INSERT INTO booking.supplier_contracts (supplier_id, title, valid_from, valid_to, currency, created_by)"
        " VALUES ($1::uuid, 'it-1151 rates', $2, $3, 'USD', $4) RETURNING id::text",
        sid,
        today,
        today + timedelta(days=30),
        MAKER,
    )
    return {"supplier": sid, "service": service, "contract": contract}


async def _rate(admin: asyncpg.Connection, contract: str, service: str, start: date, end: date) -> None:
    await admin.execute(
        "INSERT INTO booking.supplier_rates (supplier_contract_id, service_id, valid_from, valid_to, unit, net_amount,"
        " currency, created_by) VALUES ($1::uuid, $2::uuid, $3, $4, 'person', 25, 'USD', $5)",
        contract,
        service,
        start,
        end,
        MAKER,
    )


async def _publish_contract(admin: asyncpg.Connection, contract: str) -> None:
    await admin.execute(
        "UPDATE booking.supplier_contracts SET status = 'published', published_by = $2, published_at = now()"
        " WHERE id = $1::uuid",
        contract,
        CHECKER,
    )


async def _offer(conn: asyncpg.Connection, service: str, by: str = MAKER, **rules: object) -> str:
    cols = {"cutoff_hours": 24, **rules}
    names = ", ".join(cols)
    marks = ", ".join(f"${i + 3}" for i in range(len(cols)))
    return await conn.fetchval(
        f"INSERT INTO booking.addon_offers (service_id, created_by, {names}) VALUES ($1::uuid, $2, {marks})"  # noqa: S608
        " RETURNING id::text",
        service,
        by,
        *cols.values(),
    )


async def _place(conn: asyncpg.Connection, offer: str, destination: str, by: str = MAKER) -> None:
    await conn.execute(
        "INSERT INTO booking.addon_offer_places (offer_id, destination_id, created_by) VALUES ($1::uuid, $2::uuid, $3)",
        offer,
        destination,
        by,
    )


# ── Registration, seed, grants ──────────────────────────────────────────────────────────────────────────────────


async def test_registered_once_and_rerun_is_a_no_op(admin: asyncpg.Connection) -> None:
    policy = "SELECT id, param_key, value FROM booking.pricing_policy WHERE created_by = 'seed:bk-068'"
    before = sorted((str(r["id"]), r["param_key"], r["value"]) for r in await admin.fetch(policy))
    await admin.execute("SET ROLE bk_owner")
    try:
        await admin.execute(MIGRATION.read_text(encoding="utf-8"))
    finally:
        await admin.execute("RESET ROLE")
    assert sorted((str(r["id"]), r["param_key"], r["value"]) for r in await admin.fetch(policy)) == before
    assert await admin.fetchval("SELECT count(*) FROM shared.schema_versions WHERE version = 'bk-068'") == 1


async def test_contract_file_without_a_directive() -> None:
    text = MIGRATION.read_text(encoding="utf-8")
    assert text.startswith("-- Migration bk-068")
    assert "-- migrate:" not in text


async def test_policy_values_seeded_and_approved(admin: asyncpg.Connection) -> None:
    rows = await admin.fetch(
        "SELECT p.param_key, p.value::text AS value, bool_or(e.event = 'approved') AS approved"
        " FROM booking.pricing_policy p JOIN booking.pricing_policy_events e ON e.policy_id = p.id"
        " WHERE p.created_by = 'seed:bk-068' GROUP BY p.param_key, p.value"
    )
    assert {r["param_key"]: (r["value"], r["approved"]) for r in rows} == {
        "addon_days_ahead": ("365", True),
        "contract_expiry_alert_days": ("60", True),
    }


async def test_grants_are_narrow(admin: asyncpg.Connection) -> None:
    table = "SELECT has_table_privilege($1, $2, $3)"
    assert not await admin.fetchval(table, "booking", "booking.addon_offers", "DELETE")
    assert await admin.fetchval(table, "booking", "booking.addon_offers", "UPDATE")
    assert await admin.fetchval(table, "booking", "booking.addon_offer_places", "DELETE")
    assert await admin.fetchval(table, "tripplanner", "booking.v_place_addons", "SELECT")
    for relation in ("booking.addon_offers", "booking.addon_offer_places", "booking.services"):
        assert not await admin.fetchval(table, "tripplanner", relation, "SELECT"), relation
    assert not await admin.fetchval(table, "cis_app", "booking.v_place_addons", "SELECT")
    columns = {
        r["column_name"]
        for r in await admin.fetch(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = 'booking' AND table_name = 'v_place_addons'"
        )
    }
    assert not {c for c in columns if "net" in c or "supplier" in c or "price" in c or "amount" in c}
    assert {"component_id", "destination_id", "service_date", "inventory_kind"} <= columns


# ── Offers: writers, maker-checker, fixed once published ─────────────────────────────────────────────────────────


async def test_no_writer_publishes_and_a_forged_flag_is_refused(
    admin: asyncpg.Connection, app_conn: asyncpg.Connection
) -> None:
    s = await _supply(admin)
    offer = await app_conn.fetchval(
        "INSERT INTO booking.addon_offers (service_id, created_by, cutoff_hours, writers)"
        " VALUES ($1::uuid, $2, 24, ARRAY['forged']) RETURNING id::text",
        s["service"],
        MAKER,
    )
    writers = "SELECT writers FROM booking.addon_offers WHERE id = $1::uuid"
    assert await app_conn.fetchval(writers, offer) == [MAKER]
    await _place(app_conn, offer, HANOI, by=EDITOR)  # adding a place makes its creator a maker
    assert await app_conn.fetchval(writers, offer) == [MAKER, EDITOR]
    for who in (MAKER, EDITOR):
        with pytest.raises(asyncpg.CheckViolationError) as exc:
            await app_conn.execute(PUBLISH, offer, who, False)
        assert exc.value.constraint_name in ("addon_offers_maker_checker_chk", "addon_offers_owner_self_chk")
    with pytest.raises(asyncpg.CheckViolationError, match="not the business owner"):
        await app_conn.execute(PUBLISH, offer, CHECKER, True)
    await app_conn.execute(PUBLISH, offer, CHECKER, False)
    row = await app_conn.fetchrow(
        "SELECT status, published_by, published_at IS NOT NULL AS dated FROM booking.addon_offers WHERE id = $1::uuid",
        offer,
    )
    assert dict(row) == {"status": "published", "published_by": CHECKER, "dated": True}


async def test_the_business_owner_publishes_her_own_offer_flagged(
    admin: asyncpg.Connection, app_conn: asyncpg.Connection
) -> None:
    s = await _supply(admin, "Owner lantern walk")
    offer = await _offer(app_conn, s["service"], by=OWNER)
    await app_conn.execute(PUBLISH, offer, OWNER, True)
    assert await app_conn.fetchval("SELECT owner_self_approved FROM booking.addon_offers WHERE id = $1::uuid", offer)


async def test_a_published_offer_is_fixed_and_reopening_resets_the_makers(
    admin: asyncpg.Connection, app_conn: asyncpg.Connection
) -> None:
    s = await _supply(admin, "Rice terrace cycle")
    offer = await _offer(app_conn, s["service"])
    await _place(app_conn, offer, HANOI)
    await app_conn.execute(PUBLISH, offer, CHECKER, False)
    with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError, match="is fixed"):
        await app_conn.execute("UPDATE booking.addon_offers SET min_pax = 4 WHERE id = $1::uuid", offer)
    with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError, match="places of a published"):
        await _place(app_conn, offer, HOI_AN)
    with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError, match="places of a published"):
        await app_conn.execute("DELETE FROM booking.addon_offer_places WHERE offer_id = $1::uuid", offer)
    # Display order may move on a published offer.
    await app_conn.execute("UPDATE booking.addon_offers SET sort_order = 3, featured = true WHERE id = $1::uuid", offer)
    # Reopened by the checker: the checker is now the only maker and cannot publish; the first maker may.
    await app_conn.execute(REOPEN, offer, CHECKER)
    assert await app_conn.fetchval("SELECT writers FROM booking.addon_offers WHERE id = $1::uuid", offer) == [CHECKER]
    await app_conn.execute(
        "UPDATE booking.addon_offers SET min_pax = 2, updated_by = $2 WHERE id = $1::uuid", offer, CHECKER
    )
    with pytest.raises(asyncpg.CheckViolationError):
        await app_conn.execute(PUBLISH, offer, CHECKER, False)
    await app_conn.execute(PUBLISH, offer, MAKER, False)
    with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError, match="never deleted"):
        await admin.execute("DELETE FROM booking.addon_offers WHERE id = $1::uuid", offer)
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app_conn.execute("DELETE FROM booking.addon_offers WHERE id = $1::uuid", offer)


async def test_new_offers_start_as_drafts_and_keep_their_service(
    admin: asyncpg.Connection, app_conn: asyncpg.Connection
) -> None:
    s = await _supply(admin, "Floating market row")
    other = await _supply(admin, "Cave lantern swim")
    with pytest.raises(asyncpg.CheckViolationError, match="starts as a draft"):
        await _offer(app_conn, s["service"], status="published")
    offer = await _offer(app_conn, s["service"], operating_weekdays=[1, 3, 5])
    with pytest.raises(asyncpg.UniqueViolationError):
        await _offer(app_conn, s["service"])
    with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError, match="keeps its service"):
        await app_conn.execute(
            "UPDATE booking.addon_offers SET service_id = $2::uuid WHERE id = $1::uuid", offer, other["service"]
        )
    for bad in ({"operating_weekdays": [0]}, {"operating_weekdays": []}, {"min_pax": 0}, {"cutoff_hours": 721}):
        with pytest.raises(asyncpg.CheckViolationError):
            await _offer(app_conn, other["service"], **bad)
    # A draft that never sold may be retired; a retired offer goes back through draft before it is published again.
    await app_conn.execute(
        "UPDATE booking.addon_offers SET status = 'retired', retired_by = $2, retired_at = now() WHERE id = $1::uuid",
        offer,
        CHECKER,
    )
    with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError, match="reopened as a draft"):
        await app_conn.execute(
            "UPDATE booking.addon_offers SET status = 'published', published_by = $2, published_at = now(),"
            " retired_by = NULL, retired_at = NULL WHERE id = $1::uuid",
            offer,
            CHECKER,
        )


# ── The read model ──────────────────────────────────────────────────────────────────────────────────────────────


async def test_the_view_lists_only_days_with_an_in_force_price(
    admin: asyncpg.Connection, app_conn: asyncpg.Connection
) -> None:
    today = await _today(admin)
    s = await _supply(admin, "Old quarter food walk")
    # Rates: today .. today+9, then a gap, then today+20 .. today+25 (B3: the gap hides the activity those days).
    await _rate(admin, s["contract"], s["service"], today, today + timedelta(days=9))
    await _rate(admin, s["contract"], s["service"], today + timedelta(days=20), today + timedelta(days=25))
    # Free sale up to 8 travellers on days 2..3 (T7), a contract term of the draft.
    await admin.execute(
        "INSERT INTO booking.supply_restrictions (supplier_contract_id, service_id, inventory_kind, date_from, date_to,"
        " max_pax, status, created_by) VALUES ($1::uuid, $2::uuid, 'free_sale', $3, $4, 8, 'contract', $5)",
        s["contract"],
        s["service"],
        today + timedelta(days=2),
        today + timedelta(days=3),
        MAKER,
    )
    offer = await _offer(app_conn, s["service"], min_pax=2)
    await _place(app_conn, offer, HANOI)
    await _place(app_conn, offer, HOI_AN)
    view = (
        "SELECT service_date, inventory_kind, free_sale_max_pax FROM booking.v_place_addons"
        " WHERE offer_id = $1::uuid AND destination_id = $2::uuid ORDER BY service_date"
    )
    assert await admin.fetch(view, offer, HANOI) == []  # a draft offer is not sold
    await app_conn.execute(PUBLISH, offer, CHECKER, False)
    assert await admin.fetch(view, offer, HANOI) == []  # the contract is still a draft: no rate in force
    await _publish_contract(admin, s["contract"])
    rows = await admin.fetch(view, offer, HANOI)
    days = [r["service_date"] for r in rows]
    expected = [today + timedelta(days=n) for n in (*range(10), *range(20, 26))]
    assert days == expected
    kinds = {r["service_date"]: (r["inventory_kind"], r["free_sale_max_pax"]) for r in rows}
    assert kinds[today + timedelta(days=2)] == ("free_sale", 8)
    assert kinds[today + timedelta(days=4)] == ("on_request", None)
    assert len(await admin.fetch(view, offer, HOI_AN)) == len(expected)

    # A stop-sale in effect (pending, T13) hides its days; tripplanner reads the same rows.
    await admin.execute(
        "INSERT INTO booking.supply_restrictions (supplier_contract_id, service_id, inventory_kind, date_from, date_to,"
        " note, status, created_by) VALUES ($1::uuid, $2::uuid, 'stop_sale', $3, $3, 'it-1151 typhoon', 'pending', $4)",
        s["contract"],
        s["service"],
        today + timedelta(days=5),
        MAKER,
    )
    days = [r["service_date"] for r in await admin.fetch(view, offer, HANOI)]
    assert today + timedelta(days=5) not in days and len(days) == len(expected) - 1
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE tripplanner")
        seen = await admin.fetchval(
            "SELECT count(*) FROM booking.v_place_addons WHERE destination_id = $1::uuid AND component_id = $2",
            HANOI,
            f"sv:{s['service']}",
        )
    assert seen == len(expected) - 1


async def test_the_view_follows_weekdays_and_the_service_and_supplier_state(
    admin: asyncpg.Connection, app_conn: asyncpg.Connection
) -> None:
    today = await _today(admin)
    s = await _supply(admin, "Sunset sampan")
    await _rate(admin, s["contract"], s["service"], today, today + timedelta(days=13))
    await _publish_contract(admin, s["contract"])
    weekdays = [1, 4]  # Monday and Thursday
    offer = await _offer(
        app_conn, s["service"], operating_weekdays=weekdays, start_times=[time(16, 30), time(9, 0)], cutoff_hours=12
    )
    await _place(app_conn, offer, HANOI)
    await app_conn.execute(PUBLISH, offer, CHECKER, False)
    rows = await admin.fetch(
        "SELECT service_date, start_times, cutoff_hours FROM booking.v_place_addons WHERE offer_id = $1::uuid", offer
    )
    expected = [today + timedelta(days=n) for n in range(14) if (today + timedelta(days=n)).isoweekday() in weekdays]
    assert sorted(r["service_date"] for r in rows) == expected
    assert rows[0]["start_times"] == ["09:00", "16:30"] and rows[0]["cutoff_hours"] == 12
    count = "SELECT count(*) FROM booking.v_place_addons WHERE offer_id = $1::uuid"
    await admin.execute("UPDATE booking.services SET status = 'inactive' WHERE id = $1::uuid", s["service"])
    assert await admin.fetchval(count, offer) == 0
    await admin.execute("UPDATE booking.services SET status = 'active' WHERE id = $1::uuid", s["service"])
    assert await admin.fetchval(count, offer) == len(expected)
    # The horizon is the policy value addon_days_ahead: without an approved value in force there is no row (fail
    # closed). Tried inside a transaction that is rolled back (the policy rows stay as seeded).
    with pytest.raises(_Rollback):
        async with admin.transaction():
            await admin.execute("SET LOCAL ROLE bk_owner")
            await admin.execute("ALTER TABLE booking.pricing_policy_events DISABLE TRIGGER USER")
            await admin.execute(
                "DELETE FROM booking.pricing_policy_events e USING booking.pricing_policy p"
                " WHERE e.policy_id = p.id AND p.param_key = 'addon_days_ahead' AND e.event = 'approved'"
            )
            assert await admin.fetchval(count, offer) == 0
            raise _Rollback
    assert await admin.fetchval(count, offer) == len(expected)


class _Rollback(Exception):
    """Raised to roll a test transaction back."""
