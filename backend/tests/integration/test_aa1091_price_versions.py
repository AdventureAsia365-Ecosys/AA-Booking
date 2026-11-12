"""AA-1091 (bk-052 / bk-053, root ADR 0005b d1, d2, d4, d9) on a real PostgreSQL, as role ``booking``:

1. The life of a price list version: draft (maker only), submit (reason, frozen diff + impact + SHA-256), approve by a
   second person with the diff they saw (403 for the maker, 409 for a stale diff), reject, one pending per plan and
   overlapping window, withdrawal of a draft by its maker.
2. The overlap rule and derived superseded: a partly overlapping newer version prices only its dates; versions that
   cover an older one entirely supersede it; withdrawing the newest restores the older ones for the dates nothing
   else covers (the withdraw preview says so first); the matrix rows carry their version.
3. The migration of today's prices: hand rows written before versions (no version) become the plan's published
   version by the bk-052 data step, and the matrix, its RRP and the public from price answer exactly as before, also
   after the new code rebuilds the matrix.
4. Seasons: fixed-date ranges of a calendar never overlap (409), a range counts only once a second person approves it,
   a festive range takes its dates from the holiday event row of each year (days before and after), stop-sale is an
   overlay, the matrix rows are cut where the season changes (season_code, peak), and the catalog sync gives a plan the
   calendar of its first day's destination.

Names, places and amounts are invented."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date, timedelta
from decimal import Decimal

import asyncpg
import pytest
import pytest_asyncio

from app.catalog import cost_sheet, price_versions, trip_plan_prices, trip_plan_sync
from app.errors import Conflict, Forbidden, Invalid
from app.pricing import seasons, seasons_admin
from tests.integration.conftest import apply_migrations
from tests.integration.test_aa1029_price_chain import (  # noqa: F401 — fixtures used by name
    CHECKER,
    MAKER,
    STAFF_TOUR,
    TODAY,
    TOUR,
    _rule,
    _staff,
    _tours,
    admin,
    pool,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

Y = TODAY.year + 2  # a future year: nothing of today's data prices it
HANOI = "d0000000-0000-4000-8000-000000000001"  # catalog stub destination


def _d(month: int, day: int, year: int = Y) -> date:
    return date(year, month, day)


def _row(valid_from: date, valid_to: date, amount: str, **over: object) -> dict[str, object]:
    row: dict[str, object] = {
        "channel": "b2b",
        "season": None,
        "valid_from": valid_from,
        "valid_to": valid_to,
        "pax_min": 2,
        "pax_max": 2,
        "currency": "USD",
        "amount": Decimal(amount),
    }
    row.update(over)
    return row


@pytest_asyncio.fixture
async def seasons_clean(admin: asyncpg.Connection) -> AsyncIterator[None]:  # noqa: F811
    yield
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE bk_owner")
        await admin.execute("UPDATE booking.trip_plans SET season_calendar_id = NULL")
        await admin.execute("DELETE FROM booking.season_ranges")
        await admin.execute("DELETE FROM booking.holiday_events")
        await admin.execute("DELETE FROM booking.season_calendars")


async def _plan(admin: asyncpg.Connection, pool: asyncpg.Pool) -> str:  # noqa: F811
    await _tours(admin)
    await trip_plan_sync.run(pool)
    await _rule(pool)
    return str(await admin.fetchval("SELECT id FROM booking.trip_plans WHERE source_id = $1", STAFF_TOUR))


async def _version(
    conn: asyncpg.Connection, rows: list[dict[str, object]], first: date, last: date, label: str = "Test"
) -> dict:
    draft = await price_versions.create_draft(
        conn, _staff(MAKER), STAFF_TOUR, {"copy_from": "none", "travel_from": first, "travel_to": last}
    )
    return await price_versions.save_draft(
        conn, _staff(MAKER), draft["id"], {"label": label, "travel_from": first, "travel_to": last, "items": rows}
    )


async def _publish(conn: asyncpg.Connection, rows: list[dict[str, object]], first: date, last: date) -> dict:
    draft = await _version(conn, rows, first, last)
    sent = await price_versions.submit(conn, _staff(MAKER), draft["id"], "New season prices")
    return await price_versions.approve(conn, _staff(CHECKER, "admin"), draft["id"], sent["diff_sha256"])


async def _matrix(admin: asyncpg.Connection, plan: str) -> list[tuple]:  # noqa: F811
    return [
        (r["valid_from"], r["valid_to"], r["amount"], r["version_no"], r["season_code"], r["peak"])
        for r in await admin.fetch(
            "SELECT r.valid_from, r.valid_to, r.amount, v.version_no, r.season_code, r.peak"
            " FROM booking.trip_plan_prices r LEFT JOIN booking.price_list_versions v ON v.id = r.version_id"
            " WHERE r.trip_plan_id = $1::uuid AND r.channel = 'b2b' AND r.pax_min = 2 ORDER BY r.valid_from",
            plan,
        )
    ]


async def _states(admin: asyncpg.Connection, plan: str) -> list[tuple[int, str]]:  # noqa: F811
    return [
        (r["version_no"], r["status"])
        for r in await admin.fetch(
            "SELECT version_no, status FROM booking.price_list_versions WHERE trip_plan_id = $1::uuid"
            " ORDER BY version_no",
            plan,
        )
    ]


async def test_state_machine_and_maker_checker(admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:  # noqa: F811
    plan = await _plan(admin, pool)
    async with pool.acquire() as conn, conn.transaction():
        draft = await _version(conn, [_row(_d(1, 1), _d(6, 30), "500")], _d(1, 1), _d(12, 31))
    assert draft["status"] == "draft" and draft["row_count"] == 1 and draft["travel_to"] == _d(12, 31)
    assert draft["tariff_year"] == Y  # 1 January is before the 11-01 tariff year start
    vid = draft["id"]
    async with pool.acquire() as conn, conn.transaction():
        # Only the maker edits or submits; a row outside the travel window is refused.
        with pytest.raises(Forbidden):
            await price_versions.submit(conn, _staff(CHECKER, "admin"), vid, "x")
        with pytest.raises(Invalid) as err:
            await price_versions.save_draft(
                conn,
                _staff(MAKER),
                vid,
                {"travel_from": _d(1, 1), "travel_to": _d(3, 31), "items": [_row(_d(1, 1), _d(6, 30), "500")]},
            )
        assert err.value.code == "row_outside_window"
    async with pool.acquire() as conn, conn.transaction():
        with pytest.raises(Invalid):
            await price_versions.submit(conn, _staff(MAKER), vid, None)  # a reason is required
    async with pool.acquire() as conn, conn.transaction():
        sent = await price_versions.submit(conn, _staff(MAKER), vid, "Supplier rates for next year")
    assert sent["status"] == "pending" and sent["diff"]["summary"]["added"] == 1 and len(sent["diff_sha256"]) == 64
    assert sent["impact"]["open_quotes"] == 0 and sent["impact"]["b2b_rows_changed"] == 1
    async with pool.acquire() as conn, conn.transaction():
        with pytest.raises(Conflict):  # not a draft any more
            await price_versions.save_draft(
                conn, _staff(MAKER), vid, {"travel_from": _d(1, 1), "travel_to": _d(12, 31), "items": []}
            )
    # One pending per plan and overlapping window.
    async with pool.acquire() as conn, conn.transaction():
        other = await _version(conn, [_row(_d(5, 1), _d(5, 31), "520")], _d(5, 1), _d(5, 31))
    async with pool.acquire() as conn, conn.transaction():
        with pytest.raises(Conflict) as err:
            await price_versions.submit(conn, _staff(MAKER), other["id"], "Overlaps")
        assert err.value.code == "pending_exists"
    # The maker never approves; a stale diff is refused; then a second person publishes.
    async with pool.acquire() as conn, conn.transaction():
        with pytest.raises(Forbidden) as err:
            await price_versions.approve(conn, _staff(MAKER), vid, sent["diff_sha256"])
        assert err.value.code == "maker_checker"
        with pytest.raises(Conflict) as err:
            await price_versions.approve(conn, _staff(CHECKER, "admin"), vid, "0" * 64)
        assert err.value.code == "diff_changed"
    async with pool.acquire() as conn, conn.transaction():
        done = await price_versions.approve(conn, _staff(CHECKER, "admin"), vid, sent["diff_sha256"])
    assert done["status"] == "published" and done["approved_by"] == CHECKER
    assert await _matrix(admin, plan) == [(_d(1, 1), _d(6, 30), Decimal("500.00"), 1, "low", False)]
    # The other draft is withdrawn by its maker; a pending one is rejected by a second person with a reason.
    async with pool.acquire() as conn, conn.transaction():
        await price_versions.withdraw(conn, _staff(MAKER), other["id"], "Not needed")
        third = await _version(conn, [_row(_d(7, 1), _d(7, 31), "540")], _d(7, 1), _d(7, 31))
        await price_versions.submit(conn, _staff(MAKER), third["id"], "July")
    async with pool.acquire() as conn, conn.transaction():
        with pytest.raises(Invalid):
            await price_versions.reject(conn, _staff(CHECKER, "admin"), third["id"], None)
    async with pool.acquire() as conn, conn.transaction():
        await price_versions.reject(conn, _staff(CHECKER, "admin"), third["id"], "Too high for July")
    assert await _states(admin, plan) == [(1, "published"), (2, "withdrawn"), (3, "rejected")]
    async with pool.acquire() as conn:
        listed = await price_versions.list_versions(conn, _staff(MAKER), STAFF_TOUR)
        queue = await price_versions.pending(conn, _staff(CHECKER, "admin"))
    assert [v["version_no"] for v in listed["items"]] == [3, 2, 1]
    assert listed["in_force"] == [{"version_id": vid, "version_no": 1, "from": _d(1, 1), "to": _d(12, 31)}]
    assert not [v for v in queue["items"] if v["trip_plan_id"] == plan]
    audit = await admin.fetchval(
        "SELECT count(*) FROM booking.audit_logs WHERE entity_type = 'price_list_version' AND entity_id = $1::uuid",
        vid,
    )
    assert audit == 4  # created, saved, submitted, approved


async def test_overlap_rule_superseded_and_withdrawal_restore(
    admin: asyncpg.Connection,  # noqa: F811
    pool: asyncpg.Pool,  # noqa: F811
) -> None:
    plan = await _plan(admin, pool)
    # v1 prices November to October (the whole window).
    async with pool.acquire() as conn, conn.transaction():
        v1 = await _publish(conn, [_row(_d(11, 1, Y - 1), _d(10, 31), "600")], _d(11, 1, Y - 1), _d(10, 31))
    # v2 changes the prices from 1 April: v1 keeps November to March (partly overlapped, still published).
    async with pool.acquire() as conn, conn.transaction():
        v2 = await _publish(conn, [_row(_d(4, 1), _d(10, 31), "650")], _d(4, 1), _d(10, 31))
    assert await _states(admin, plan) == [(1, "published"), (2, "published")]
    assert await _matrix(admin, plan) == [
        (_d(11, 1, Y - 1), _d(3, 31), Decimal("600.00"), 1, "low", False),
        (_d(4, 1), _d(10, 31), Decimal("650.00"), 2, "low", False),
    ]
    # v3 covers November to March: v1 is now covered entirely, superseded.
    async with pool.acquire() as conn, conn.transaction():
        v3 = await _publish(conn, [_row(_d(11, 1, Y - 1), _d(3, 31), "610")], _d(11, 1, Y - 1), _d(3, 31))
    assert await _states(admin, plan) == [(1, "superseded"), (2, "published"), (3, "published")]
    # The withdraw preview of v3: v1 prices November to March again, nothing becomes unquotable.
    async with pool.acquire() as conn:
        preview = await price_versions.withdraw_preview(conn, _staff(CHECKER, "admin"), v3["id"])
    assert preview == {
        "priced_by": [{"version_id": v1["id"], "version_no": 1, "from": _d(11, 1, Y - 1), "to": _d(3, 31)}],
        "unquotable": [],
    }
    # Only a second person withdraws a published version.
    async with pool.acquire() as conn, conn.transaction():
        with pytest.raises(Forbidden):
            await price_versions.withdraw(conn, _staff(MAKER), v3["id"], "Wrong")
    async with pool.acquire() as conn, conn.transaction():
        out = await price_versions.withdraw(conn, _staff(CHECKER, "admin"), v3["id"], "Wrong currency")
    assert out["effect"]["states_moved"]["published"] == [v1["id"]]
    assert await _states(admin, plan) == [(1, "published"), (2, "published"), (3, "withdrawn")]
    assert await _matrix(admin, plan) == [
        (_d(11, 1, Y - 1), _d(3, 31), Decimal("600.00"), 1, "low", False),
        (_d(4, 1), _d(10, 31), Decimal("650.00"), 2, "low", False),
    ]
    # Withdrawing v2 too: v1 prices its whole window again.
    async with pool.acquire() as conn, conn.transaction():
        await price_versions.withdraw(conn, _staff(CHECKER, "admin"), v2["id"], "Back to the old price")
    assert await _matrix(admin, plan) == [(_d(11, 1, Y - 1), _d(10, 31), Decimal("600.00"), 1, "low", False)]
    # A version is never deleted.
    assert len(await _states(admin, plan)) == 3


async def test_migration_of_todays_prices_keeps_every_price(
    admin: asyncpg.Connection,  # noqa: F811
    pool: asyncpg.Pool,  # noqa: F811
) -> None:
    """Hand rows written before price versions: the bk-052 data step versions them, and the matrix (amounts, RRP,
    from price) answers exactly as before, also after the new code rebuilds it."""
    plan = await _plan(admin, pool)
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE bk_owner")
        await admin.execute(
            "INSERT INTO booking.trip_plan_prices (trip_plan_id, position, channel, season, valid_from, valid_to,"
            " pax_min, pax_max, currency, amount, rrp_amount, created_by) VALUES"
            " ($1::uuid, 1, 'b2b', 'Dry', $2, $3, 1, 2, 'USD', 400, 500, $4),"
            " ($1::uuid, 2, 'b2b', 'Dry', $2, $3, 3, NULL, 'USD', 350, 440, $4)",
            plan,
            TODAY,
            TODAY + timedelta(days=120),
            MAKER,
        )
    read = (
        "SELECT position, channel, season, valid_from, valid_to, pax_min, pax_max, amount, rrp_amount"
        " FROM booking.trip_plan_prices WHERE trip_plan_id = $1::uuid ORDER BY position"
    )
    before = [tuple(r) for r in await admin.fetch(read, plan)]
    async with pool.acquire() as conn:
        from_before = (await trip_plan_prices.list_prices(conn, _staff(MAKER), STAFF_TOUR))["from_price"]
    await apply_migrations(admin, only=[52])
    assert await _states(admin, plan) == [(1, "published")]
    assert [tuple(r) for r in await admin.fetch(read, plan)] == before
    assert (
        await admin.fetchval(
            "SELECT count(*) FROM booking.trip_plan_prices WHERE trip_plan_id = $1::uuid AND version_id IS NULL", plan
        )
        == 0
    )
    # The new code rebuilds the matrix from the version: the same prices and the same from price.
    async with pool.acquire() as conn, conn.transaction():
        await cost_sheet.recompute_plan(conn, plan, await cost_sheet.load_pricing(conn, TODAY))
    assert [tuple(r) for r in await admin.fetch(read, plan)] == before
    async with pool.acquire() as conn:
        assert (await trip_plan_prices.list_prices(conn, _staff(MAKER), STAFF_TOUR))["from_price"] == from_before
    assert from_before == {"amount": "500.00", "currency": "USD", "basis": "per_person", "season": "Dry"}


async def _calendar(conn: asyncpg.Connection) -> str:
    cal = await seasons_admin.create_calendar(
        conn, _staff(MAKER), {"owner_kind": "destination", "destination_id": HANOI, "name": "Invented capital"}
    )
    return cal["id"]


async def _approved_range(conn: asyncpg.Connection, cal: str, body: dict[str, object]) -> dict:
    made = await seasons_admin.propose_range(conn, _staff(MAKER), cal, body)
    return await seasons_admin.decide_range(conn, _staff(CHECKER, "admin"), made["id"], "approve")


async def test_calendar_no_overlap_holiday_event_and_matrix_seasons(
    admin: asyncpg.Connection,  # noqa: F811
    pool: asyncpg.Pool,  # noqa: F811
    seasons_clean: None,
) -> None:
    plan = await _plan(admin, pool)
    async with pool.acquire() as conn, conn.transaction():
        cal = await _calendar(conn)
        high = {"kind": "season", "season_code": "high", "date_from": _d(1, 1), "date_to": _d(3, 31)}
        await _approved_range(conn, cal, high)
        made = await seasons_admin.propose_range(
            conn,
            _staff(MAKER),
            cal,
            {"kind": "season", "season_code": "peak", "date_from": _d(6, 1), "date_to": _d(6, 30)},
        )
    # A fixed range that overlaps another of the calendar is refused (pending ones count too).
    async with pool.acquire() as conn, conn.transaction():
        with pytest.raises(Conflict) as err:
            await seasons_admin.propose_range(
                conn,
                _staff(MAKER),
                cal,
                {"kind": "season", "season_code": "low", "date_from": _d(3, 15), "date_to": _d(4, 15)},
            )
        assert err.value.code == "range_overlaps"
    # A range counts only once a second person approves it.
    async with pool.acquire() as conn:
        assert (await seasons.load_many(conn, [cal]))[cal].season_on(_d(6, 10)).code == "low"
    async with pool.acquire() as conn, conn.transaction():
        with pytest.raises(Forbidden):
            await seasons_admin.decide_range(conn, _staff(MAKER), made["id"], "approve")
        await seasons_admin.decide_range(conn, _staff(CHECKER, "admin"), made["id"], "approve")
        # Festive from a holiday event: the range names the event, the event row of the year gives the dates.
        await _approved_range(
            conn,
            cal,
            {"kind": "season", "season_code": "festive", "holiday_country": "VN", "holiday_code": "invented_fest",
             "days_before": 1, "days_after": 2},
        )  # fmt: skip
        await _approved_range(conn, cal, {"kind": "stop_sale", "date_from": _d(6, 20), "date_to": _d(6, 22)})
    async with pool.acquire() as conn:
        cal_now = (await seasons.load_many(conn, [cal]))[cal]
    assert cal_now.season_on(_d(2, 10)).code == "high"  # no event row yet: no festive dates
    async with pool.acquire() as conn, conn.transaction():
        event = await seasons_admin.propose_event(
            conn,
            _staff(MAKER),
            {"country_iso2": "VN", "code": "invented_fest", "name": "Invented festival", "date_from": _d(2, 10),
             "date_to": _d(2, 12)},
        )  # fmt: skip
        await seasons_admin.decide_event(conn, _staff(CHECKER, "admin"), event["id"], "approve")
    async with pool.acquire() as conn:
        cal_now = (await seasons.load_many(conn, [cal]))[cal]
    # 10-12 February, one day before and two after: 9 to 14 February is festive, over the high season.
    assert [cal_now.season_on(_d(2, d)).code for d in (8, 9, 12, 14, 15)] == ["high", "festive", "festive",
                                                                                 "festive", "high"]  # fmt: skip
    assert cal_now.season_on(_d(6, 21)).stop_sale and cal_now.season_on(_d(6, 21)).code == "peak"
    assert not cal_now.season_on(_d(6, 23)).stop_sale

    # The plan uses the calendar: its matrix rows are cut where the season changes, peak / festive flagged.
    async with pool.acquire() as conn, conn.transaction():
        await seasons_admin.set_plan_calendar(conn, _staff(MAKER), STAFF_TOUR, cal)
        await _publish(conn, [_row(_d(1, 1), _d(2, 28), "700")], _d(1, 1), _d(2, 28))
    assert [(m[0], m[1], m[4], m[5]) for m in await _matrix(admin, plan)] == [
        (_d(1, 1), _d(2, 8), "high", False),
        (_d(2, 9), _d(2, 14), "festive", True),
        (_d(2, 15), _d(2, 28), "high", False),
    ]
    async with pool.acquire() as conn:
        detail = await seasons_admin.get_calendar(conn, _staff(MAKER), cal, Y)
    assert {"from": _d(2, 9), "to": _d(2, 14), "season": "festive"} in detail["segments"]
    assert detail["stop_sales"] == [{"from": _d(6, 20), "to": _d(6, 22)}]

    # Removing a range is a request a second person approves.
    async with pool.acquire() as conn, conn.transaction():
        rng = next(r for r in detail["ranges"] if r["season_code"] == "high")
        await seasons_admin.decide_range(conn, _staff(MAKER), rng["id"], "end")
        with pytest.raises(Forbidden):
            await seasons_admin.decide_range(conn, _staff(MAKER), rng["id"], "end_approve")
        ended = await seasons_admin.decide_range(conn, _staff(CHECKER, "admin"), rng["id"], "end_approve")
    assert ended["state"] == "ended"


async def test_sync_gives_a_plan_its_first_day_destination_calendar(
    admin: asyncpg.Connection,  # noqa: F811
    pool: asyncpg.Pool,  # noqa: F811
    seasons_clean: None,
) -> None:
    await _tours(admin)
    await admin.execute(
        "INSERT INTO catalog.v_tour_day (source_id, day_number, title, body, places)"
        " VALUES ($1, 1, 'Arrival', 'Invented day', ARRAY['Hanoi'])",
        TOUR,
    )
    try:
        out = await trip_plan_sync.run(pool)
        assert out["calendars"]["assigned"] >= 1
        cal = await admin.fetchrow(
            "SELECT c.destination_id::text AS dest, c.name, c.created_by FROM booking.trip_plans p"
            " JOIN booking.season_calendars c ON c.id = p.season_calendar_id WHERE p.source_id = $1",
            TOUR,
        )
        assert dict(cal) == {"dest": HANOI, "name": "Hanoi", "created_by": "system:bk_trip_plan_sync"}
        # A plan whose first day names no known place stays without one (every date low season).
        assert (
            await admin.fetchval("SELECT season_calendar_id FROM booking.trip_plans WHERE source_id = $1", STAFF_TOUR)
            is None
        )
    finally:
        await admin.execute("DELETE FROM catalog.v_tour_day WHERE source_id = $1", TOUR)
