"""AA-1091 (bk-052 / bk-053, root ADR 0005b d1, d2, d4, d9) on a real PostgreSQL, as role ``booking``:

1. The life of a price list version: draft (maker only), submit (reason, frozen diff + impact + SHA-256), approve by a
   second person with the diff they saw (403 for the maker, 409 for a stale diff), reject, one pending per plan and
   overlapping window, withdrawal of a draft by its maker.
2. The overlap rule and derived superseded: a partly overlapping newer version prices only its dates; versions that
   cover an older one entirely supersede it; withdrawing the newest restores the older ones for the dates nothing
   else covers (the withdraw preview says so first); the matrix rows carry their version.
3. The migration of today's prices: the matrix (computed rows priced from "Peak"-labelled rates, hand rows with a
   peak flag, per-type prices or a net, a computed row a person overrode) and its history become versions by the
   bk-052 data step (past sets withdrawn, the rows in force published), and every price column, the public from price
   and a B2B quote line answer exactly as before, also after the new code rebuilds the matrix.
4. Seasons: fixed-date ranges of a calendar never overlap (409), a range counts only once a second person approves it,
   a festive range takes its dates from the holiday event row of each year (days before and after), stop-sale is an
   overlay, the matrix rows are cut where the season changes (season_code, peak), every proposal, withdrawal, removal
   request and rejection carries a reason, a plan's calendar changes only once a second person approves the request,
   makers and checkers come back as people ({id, name}), and the catalog sync gives a plan the calendar of its first
   day's destination.

Names, places and amounts are invented."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date, timedelta
from decimal import Decimal

import asyncpg
import pytest
import pytest_asyncio

from app.b2b import plan_components, quotes
from app.b2b.components import LineAsk, price_lines
from app.catalog import cost_sheet, price_versions, trip_plan_prices, trip_plan_sync
from app.errors import Conflict, Forbidden, Invalid
from app.pricing import policy, seasons, seasons_admin
from app.suppliers import service as suppliers
from app.supply import rate_sheet
from tests.integration.conftest import apply_migrations
from tests.integration.test_aa1029_price_chain import (  # noqa: F401 — fixtures used by name
    CHECKER,
    MAKER,
    STAFF_TOUR,
    TODAY,
    TOUR,
    YEAR_END,
    _rule,
    _staff,
    _supply,
    _tours,
    admin,
    pool,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

Y = TODAY.year + 2  # a future year: nothing of today's data prices it
HANOI = "d0000000-0000-4000-8000-000000000001"  # catalog stub destination
NO_PARTNER = "00000000-0000-4000-8000-0000000000ff"  # no agency terms: the quote at the rule


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
        await admin.execute(
            "UPDATE booking.trip_plans SET season_calendar_id = NULL, pending_season_calendar_id = NULL,"
            " season_calendar_requested_by = NULL, season_calendar_requested_at = NULL,"
            " season_calendar_request_reason = NULL"
        )
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


async def _names(admin: asyncpg.Connection) -> dict[str, str | None]:  # noqa: F811
    """The staff directory names of the maker and the checker (other test modules may have named them)."""
    rows = await admin.fetch(
        "SELECT cis_user_id, name FROM booking.staff_users WHERE cis_user_id = ANY($1::text[])", [MAKER, CHECKER]
    )
    return {r["cis_user_id"]: r["name"] for r in rows}


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
    assert done["status"] == "published"
    names = await _names(admin)
    assert done["approved_by"] == {"id": CHECKER, "name": names[CHECKER]}
    assert done["created_by"] == {"id": MAKER, "name": names[MAKER]}  # people from the staff directory
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


PRICE_COLUMNS = (
    "channel, season, peak, valid_from, valid_to, pax_min, pax_max, currency::text AS currency, amount, rrp_amount,"
    " net_amount, net_currency::text AS net_currency, effective_margin, priced_on, overridden, computed, child_amount,"
    " infant_amount, single_supplement_amount, child_net_amount, infant_net_amount, single_supplement_net_amount"
)
MATRIX_READ = f"SELECT {PRICE_COLUMNS} FROM booking.trip_plan_prices WHERE trip_plan_id = $1::uuid"  # noqa: S608


async def _every_price(admin: asyncpg.Connection, plan: str) -> list[tuple]:  # noqa: F811
    """Every price column of the plan's matrix, in a stable order (a rebuild may renumber positions)."""
    return sorted((tuple(r) for r in await admin.fetch(MATRIX_READ, plan)), key=repr)


def _peak_sheet(s: dict[str, str]) -> bytes:
    """An invented rate sheet whose later rates are labelled "Peak" (the label the old rule read)."""
    mid = TODAY + timedelta(days=90)
    return rate_sheet.build_workbook(
        {"Title": "Invented peak rates", "Valid from": TODAY.isoformat(), "Valid to": YEAR_END.isoformat(),
         "Currency": "USD"},
        hotel_rows=[
            (s["hotel_code"], "Garden Twin", "Low", TODAY, mid, "BB", 2, 2, 100, None),
            (s["hotel_code"], "Garden Twin", "Low", TODAY, mid, "BB", 1, 1, 80, None),
            (s["hotel_code"], "Garden Twin", "Peak", mid + timedelta(days=1), YEAR_END, "BB", 2, 2, 140, None),
            (s["hotel_code"], "Garden Twin", "Peak", mid + timedelta(days=1), YEAR_END, "BB", 1, 1, 110, None),
        ],
        service_rows={
            "Guide": [(s["guide_code"], None, None, None, None, None, 60, None)],
            "Transport": [(s["jeep_code"], None, None, None, None, None, 20, None)],
        },
    )  # fmt: skip


async def _costed_plan(admin: asyncpg.Connection, pool: asyncpg.Pool) -> str:  # noqa: F811
    """TOUR with components priced from the invented peak sheet and a b2b rule with a peak uplift of 3 points."""
    await _tours(admin)
    await trip_plan_sync.run(pool)
    s = await _supply(admin)
    async with pool.acquire() as conn, conn.transaction():
        done = await rate_sheet.import_sheet(conn, _staff(MAKER), s["supplier"], _peak_sheet(s), dry_run=False)
        await suppliers.update_contract(conn, _staff(CHECKER, "admin"), done["contract"]["id"], {"status": "published"})
        await plan_components.replace_components(
            conn,
            _staff(MAKER),
            TOUR,
            [
                {"day_no": 1, "service_id": s["guide"]},
                {"day_no": 1, "hotel_room_type_id": s["room"]},
                {"day_no": 2, "service_id": s["jeep"]},
                {"day_no": 2, "hotel_room_type_id": s["room"]},
            ],
        )
        rule = await policy.create_rule(
            conn,
            _staff(MAKER),
            {"channel": "b2b", "target_margin": Decimal("0.15"), "floor_margin": Decimal("0.10"),
             "peak_uplift_pts": Decimal("0.03"), "card_fee_pct": Decimal("0.03"), "valid_from": TODAY,
             "note": "it-1029", "rrp_markup_pct": Decimal("0.25")},
            today=TODAY,
        )  # fmt: skip
        await policy.approve_rule(conn, _staff(CHECKER, "admin"), rule["id"], today=TODAY)
    # Sellable, so a B2B quote prices it.
    return str(
        await admin.fetchval("UPDATE booking.trip_plans SET sellable = true WHERE source_id = $1 RETURNING id", TOUR)
    )


async def _recompute(pool: asyncpg.Pool, plan: str) -> None:  # noqa: F811
    async with pool.acquire() as conn, conn.transaction():
        await cost_sheet.recompute_plan(conn, plan, await cost_sheet.load_pricing(conn, TODAY))


async def _quote_line(pool: asyncpg.Pool, plan: str, day: date) -> Decimal:  # noqa: F811
    """The b2b price of a 2-adult ``tp:`` line on ``day``, priced the way ``POST /quotes`` and the tariff price it
    (``app.b2b.quotes.quote_in``: components x rates, the rule, the plan calendar's season, no agency discount)."""
    async with pool.acquire() as conn:
        ask = LineAsk(
            line_ref="l1", component_id=f"tp:{plan}", service_date=day, nights=None, adult=2, child=0, infant=0
        )
        cost = (await price_lines(conn, [ask]))[0]
        basis = await quotes.price_basis(
            conn, partner_id=NO_PARTNER, currency="USD", cost_currencies={c for _, c in cost.costs}, on=TODAY
        )
        calendar = (await seasons.for_plans(conn, [plan]))[plan]
        return quotes.price_line(basis, cost, plan, pax=(2, 0, 0), season=calendar.season_on(day)).b2b_price


async def test_migration_of_todays_prices_keeps_every_price(
    admin: asyncpg.Connection,  # noqa: F811
    pool: asyncpg.Pool,  # noqa: F811
) -> None:
    """Today's matrix (computed rows, some from "Peak"-labelled rates, a hand row flagged peak with per-type prices, a
    hand row priced from a net, a computed row a person overrode) and its history (a staff set, a cost-sheet set):
    the bk-052 data step versions them, and every price column, the from price and a B2B quote line answer exactly
    as before, also after the new code rebuilds the matrix (the plan has no season range yet, so the old peak flags
    stay, :attr:`app.pricing.seasons.Calendar.configured`)."""
    plan = await _costed_plan(admin, pool)
    await _recompute(pool, plan)
    computed_peak = await admin.fetchval(
        "SELECT count(*) FROM booking.trip_plan_prices WHERE trip_plan_id = $1::uuid AND computed AND peak", plan
    )
    assert computed_peak > 0  # the old label rule: a "Peak" rate makes the computed row peak (uplift applied)
    later = YEAR_END + timedelta(days=10)
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE bk_owner")
        position = await admin.fetchval(
            "SELECT max(position) FROM booking.trip_plan_prices WHERE trip_plan_id = $1::uuid", plan
        )
        await admin.execute(
            "INSERT INTO booking.trip_plan_prices (trip_plan_id, position, channel, season, peak, valid_from, valid_to,"
            " pax_min, pax_max, currency, amount, child_amount, infant_amount, single_supplement_amount, created_by)"
            " VALUES ($1::uuid, $2, 'b2b', 'Festival week', true, $3, $4, 2, 2, 'USD', 420, 300, 40, 90, $5)",
            plan, position + 1, later, later + timedelta(days=30), MAKER,
        )  # fmt: skip
        await admin.execute(
            "INSERT INTO booking.trip_plan_prices (trip_plan_id, position, channel, season, valid_from, valid_to,"
            " pax_min, pax_max, currency, amount, net_amount, net_currency, effective_margin, priced_on, created_by)"
            " VALUES ($1::uuid, $2, 'b2b', 'Dry', $3, $4, 3, 4, 'USD', 255, 200, 'USD', 0.2157, $5, $6)",
            plan, position + 2, later, later + timedelta(days=30), TODAY, MAKER,
        )  # fmt: skip
        await admin.execute(
            "INSERT INTO booking.trip_plan_prices (trip_plan_id, position, channel, season, peak, valid_from, valid_to,"
            " pax_min, pax_max, currency, amount, net_amount, net_currency, effective_margin, priced_on, overridden,"
            " computed, created_by) VALUES ($1::uuid, $2, 'b2b', 'Peak', true, $3, $4, 5, 6, 'USD', 230, 150, 'USD',"
            " 0.3478, $5, true, true, $6)",
            plan, position + 3, later, later + timedelta(days=30), TODAY, MAKER,
        )  # fmt: skip
    # The matrix as the code before versions keeps it: hand rows kept, computed rows around them, RRP refreshed.
    await _recompute(pool, plan)
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE bk_owner")
        first_set = await admin.fetchval(
            "SELECT min(effective_from) FROM booking.trip_plan_price_history WHERE trip_plan_id = $1::uuid", plan
        )
        await admin.execute(
            "INSERT INTO booking.trip_plan_price_history (trip_plan_id, channel, season, peak, valid_from, valid_to,"
            " pax_min, pax_max, currency, amount, computed, overridden, change_source, changed_by, effective_from,"
            " effective_to) VALUES ($1::uuid, 'b2b', 'Old dry', true, $2, $3, 2, 2, 'USD', 380, false, false, 'staff',"
            " $4, $5::timestamptz - interval '2 days', $5::timestamptz)",
            plan, TODAY, TODAY + timedelta(days=60), MAKER, first_set,
        )  # fmt: skip
    sets = await admin.fetch(
        "SELECT effective_from, min(change_source) AS source, count(*) AS rows, bool_or(effective_to IS NULL) AS open"
        " FROM booking.trip_plan_price_history WHERE trip_plan_id = $1::uuid GROUP BY 1 ORDER BY 1",
        plan,
    )
    assert [(s["source"], s["open"]) for s in sets] == [("staff", False), ("cost_sheet", False), ("cost_sheet", True)]
    before = await _every_price(admin, plan)
    assert len(before) == sets[-1]["rows"]
    low_day, peak_day = TODAY + timedelta(days=10), TODAY + timedelta(days=120)
    quote_before = (await _quote_line(pool, plan, low_day), await _quote_line(pool, plan, peak_day))
    async with pool.acquire() as conn:
        from_before = (await trip_plan_prices.list_prices(conn, _staff(MAKER), TOUR))["from_price"]

    await apply_migrations(admin, only=[52])

    assert await _every_price(admin, plan) == before
    versions = await admin.fetch(
        "SELECT v.version_no, v.status, v.source, v.withdrawn_by, v.approved_by,"
        " (SELECT count(*) FROM booking.price_list_version_rows r WHERE r.version_id = v.id) AS rows"
        " FROM booking.price_list_versions v WHERE v.trip_plan_id = $1::uuid ORDER BY v.version_no",
        plan,
    )
    assert [(v["status"], v["source"], v["rows"]) for v in versions] == [
        ("withdrawn", "staff", sets[0]["rows"]),
        ("withdrawn", "cost_sheet", sets[1]["rows"]),
        ("published", "cost_sheet", len(before)),
    ]
    assert [v["withdrawn_by"] for v in versions[:2]] == ["migration:bk-052"] * 2
    assert versions[2]["approved_by"] == "migration:bk-052"
    assert (
        await admin.fetchval(
            "SELECT count(*) FROM booking.trip_plan_prices WHERE trip_plan_id = $1::uuid AND version_id IS NULL"
            " AND (NOT computed OR overridden)",
            plan,
        )
        == 0
    )
    # The new code rebuilds the matrix from the version (and the cost sheet around it): the same prices, the same
    # from price and the same quote line.
    await _recompute(pool, plan)
    assert await _every_price(admin, plan) == before
    assert (await _quote_line(pool, plan, low_day), await _quote_line(pool, plan, peak_day)) == quote_before
    async with pool.acquire() as conn:
        assert (await trip_plan_prices.list_prices(conn, _staff(MAKER), TOUR))["from_price"] == from_before


async def _calendar(conn: asyncpg.Connection) -> str:
    cal = await seasons_admin.create_calendar(
        conn, _staff(MAKER), {"owner_kind": "destination", "destination_id": HANOI, "name": "Invented capital"}
    )
    return cal["id"]


async def _approved_range(conn: asyncpg.Connection, cal: str, body: dict[str, object]) -> dict:
    made = await seasons_admin.propose_range(conn, _staff(MAKER), cal, {"note": "Invented demand", **body})
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
            {"kind": "season", "season_code": "peak", "date_from": _d(6, 1), "date_to": _d(6, 30), "note": "Summer"},
        )
    names = await _names(admin)
    assert made["created_by"] == {"id": MAKER, "name": names[MAKER]} and made["note"] == "Summer"
    # A proposal needs its reason (the note).
    async with pool.acquire() as conn, conn.transaction():
        with pytest.raises(Invalid) as err:
            await seasons_admin.propose_range(
                conn,
                _staff(MAKER),
                cal,
                {"kind": "season", "season_code": "low", "date_from": _d(10, 1), "date_to": _d(10, 31)},
            )
        assert err.value.code == "note_required"
    # A fixed range that overlaps another of the calendar is refused (pending ones count too).
    async with pool.acquire() as conn, conn.transaction():
        with pytest.raises(Conflict) as err:
            await seasons_admin.propose_range(
                conn,
                _staff(MAKER),
                cal,
                {"kind": "season", "season_code": "low", "date_from": _d(3, 15), "date_to": _d(4, 15), "note": "x"},
            )
        assert err.value.code == "range_overlaps"
    # A range counts only once a second person approves it.
    async with pool.acquire() as conn:
        assert (await seasons.load_many(conn, [cal]))[cal].season_on(_d(6, 10)).code == "low"
    async with pool.acquire() as conn, conn.transaction():
        with pytest.raises(Forbidden):
            await seasons_admin.decide_range(conn, _staff(MAKER), made["id"], "approve")
        with pytest.raises(Invalid):  # a reject needs its reason
            await seasons_admin.decide_range(conn, _staff(CHECKER, "admin"), made["id"], "reject")
        approved = await seasons_admin.decide_range(conn, _staff(CHECKER, "admin"), made["id"], "approve")
    assert approved["decided_by"] == {"id": CHECKER, "name": names[CHECKER]}
    async with pool.acquire() as conn, conn.transaction():
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
             "date_to": _d(2, 12), "reason": "Official invented calendar"},
        )  # fmt: skip
        assert event["reason"] == "Official invented calendar"
        await seasons_admin.decide_event(conn, _staff(CHECKER, "admin"), event["id"], "approve")
    async with pool.acquire() as conn:
        cal_now = (await seasons.load_many(conn, [cal]))[cal]
    # 10-12 February, one day before and two after: 9 to 14 February is festive, over the high season.
    assert [cal_now.season_on(_d(2, d)).code for d in (8, 9, 12, 14, 15)] == ["high", "festive", "festive",
                                                                                 "festive", "high"]  # fmt: skip
    assert cal_now.season_on(_d(6, 21)).stop_sale and cal_now.season_on(_d(6, 21)).code == "peak"
    assert not cal_now.season_on(_d(6, 23)).stop_sale

    # The plan's calendar changes only through a request with a reason that a second person approves.
    async with pool.acquire() as conn, conn.transaction():
        with pytest.raises(Invalid):
            await seasons_admin.request_plan_calendar(conn, _staff(MAKER), STAFF_TOUR, cal, None)
        asked = await seasons_admin.request_plan_calendar(conn, _staff(MAKER), STAFF_TOUR, cal, "Starts in the capital")
    assert asked["season_calendar_id"] is None and asked["request"]["season_calendar_id"] == cal
    assert asked["request"]["requested_by"] == {"id": MAKER, "name": names[MAKER]}
    async with pool.acquire() as conn, conn.transaction():
        with pytest.raises(Conflict) as err:
            await seasons_admin.request_plan_calendar(conn, _staff(CHECKER, "admin"), STAFF_TOUR, None, "Other")
        assert err.value.code == "calendar_change_pending"
        with pytest.raises(Forbidden) as err:
            await seasons_admin.decide_plan_calendar(conn, _staff(MAKER), STAFF_TOUR, "approve", None)
        assert err.value.code == "maker_checker"
        with pytest.raises(Invalid):
            await seasons_admin.decide_plan_calendar(conn, _staff(CHECKER, "admin"), STAFF_TOUR, "reject", None)
    assert (
        await admin.fetchval("SELECT season_calendar_id FROM booking.trip_plans WHERE source_id = $1", STAFF_TOUR)
        is None
    )
    async with pool.acquire() as conn, conn.transaction():
        applied = await seasons_admin.decide_plan_calendar(conn, _staff(CHECKER, "admin"), STAFF_TOUR, "approve", None)
    assert applied["season_calendar_id"] == cal and applied["request"] is None
    audit_rows = await admin.fetch(
        "SELECT action, after->>'reason' AS reason FROM booking.audit_logs WHERE entity_type = 'trip_plan'"
        " AND entity_id = $1::uuid AND action LIKE 'trip_plan.season_calendar_%'",
        plan,
    )
    assert sorted((a["action"], a["reason"] or "") for a in audit_rows) == [
        ("trip_plan.season_calendar_approve", ""),
        ("trip_plan.season_calendar_requested", "Starts in the capital"),
    ]

    # The plan uses the calendar: its matrix rows are cut where the season changes, peak / festive flagged.
    async with pool.acquire() as conn, conn.transaction():
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
        with pytest.raises(Invalid):  # a removal request needs its reason
            await seasons_admin.decide_range(conn, _staff(MAKER), rng["id"], "end")
        asked_end = await seasons_admin.decide_range(conn, _staff(MAKER), rng["id"], "end", "High season moved")
        assert asked_end["end_reason"] == "High season moved" and asked_end["state"] == "ending"
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
