"""AA-1091 (bk-055, root ADR 0005b d1, d4, d10) Pricing > Seasons and Holiday events without a database: the reads and
their permission, calendars (owner rules, the resolved season segments of a year with a festive range taken from a
holiday event and the stop-sale overlay), the default season change, season ranges (fixed dates that never overlap,
festive ranges naming an event, stop-sales) and holiday events with maker-checker and a reason on every proposal,
withdrawal, removal and rejection, the calendar of a trip plan (request and decision) and the default calendars the
catalog sync assigns. A scripted connection answers by SQL fragment; invented places, events and people only."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any

import asyncpg
import pytest

from app.auth.staff import Staff
from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.pricing import seasons_admin as sa
from tests.unit.fakes import ScriptedConn

CAL = "aa109155-0000-4000-8000-0000000000c1"
OTHER_CAL = "aa109155-0000-4000-8000-0000000000c2"
RANGE = "aa109155-0000-4000-8000-0000000000r1"
EVENT = "aa109155-0000-4000-8000-0000000000e1"
PLAN = "aa109155-0000-4000-8000-0000000000p1"
SOURCE = "aa109155-0000-4000-8000-0000000000s1"
DEST = "aa109155-0000-4000-8000-0000000000d1"
CONTRACT = "aa109155-0000-4000-8000-0000000000k1"
STAMP = datetime(2031, 2, 1, 9, 0, tzinfo=UTC)
MAKER = Staff("maker-1091", None, "Invented Maker", "contracting", True, None)
CHECKER = Staff("checker-1091", None, "Invented Checker", "admin", True, None)
SALES = Staff("sales-1091", None, "Invented Seller", "sales", True, None)
CONTENT = Staff("content-1091", None, "Invented Writer", "content_creator", True, None)
AUDIT = "INSERT INTO booking.audit_logs"


def calendar(**over: Any) -> dict[str, Any]:
    row = {
        "id": CAL,
        "owner_kind": "destination",
        "destination_id": DEST,
        "supplier_contract_id": None,
        "name": "Invented Coast",
        "default_season": "low",
        "pending_default_season": None,
        "default_requested_by": None,
        "default_requested_at": None,
        "default_request_reason": None,
        "created_by": MAKER.cis_user_id,
        "created_at": STAMP,
        "updated_at": STAMP,
        "ranges": 2,
        "waiting": 1,
        "plans": 4,
    }
    return {**row, **over}


def range_row(**over: Any) -> dict[str, Any]:
    row = {
        "id": RANGE,
        "calendar_id": CAL,
        "kind": "season",
        "season_code": "high",
        "date_from": date(2031, 1, 1),
        "date_to": date(2031, 4, 1),
        "holiday_country": None,
        "holiday_code": None,
        "days_before": 0,
        "days_after": 0,
        "note": "Dry season",
        "status": "pending",
        "created_by": MAKER.cis_user_id,
        "created_at": STAMP,
        "decided_by": None,
        "decided_at": None,
        "reject_reason": None,
        "withdraw_reason": None,
        "end_requested_by": None,
        "end_requested_at": None,
        "end_reason": None,
        "ended_by": None,
        "ended_at": None,
    }
    return {**row, **over}


def event_row(**over: Any) -> dict[str, Any]:
    row = {
        "id": EVENT,
        "country_iso2": "VN",
        "code": "invented_fest",
        "name": "Invented Festival",
        "year": 2031,
        "date_from": date(2031, 2, 10),
        "date_to": date(2031, 2, 14),
        "reason": "Set by the board",
        "status": "pending",
        "created_by": MAKER.cis_user_id,
        "created_at": STAMP,
        "decided_by": None,
        "decided_at": None,
        "reject_reason": None,
        "withdraw_reason": None,
        "end_requested_by": None,
        "end_requested_at": None,
        "end_reason": None,
        "ended_by": None,
        "ended_at": None,
        "calendars": 3,
    }
    return {**row, **over}


def plan_row(**over: Any) -> dict[str, Any]:
    row = {
        "id": PLAN,
        "calendar_id": CAL,
        "calendar_name": "Invented Coast",
        "pending_calendar_id": None,
        "pending_calendar_name": None,
        "requested_by": None,
        "requested_at": None,
        "request_reason": None,
    }
    return {**row, **over}


def db(
    *,
    cal: dict[str, Any] | None = None,
    rng: dict[str, Any] | None = None,
    ranges: list[dict[str, Any]] | None = None,
    live: list[dict[str, Any]] | None = None,
    approved: list[dict[str, Any]] | None = None,
    events: list[dict[str, Any]] | None = None,
    event: dict[str, Any] | None = None,
    plan: dict[str, Any] | None = None,
) -> ScriptedConn:
    return ScriptedConn(
        [
            (
                "FROM booking.staff_users WHERE cis_user_id = ANY",
                lambda refs: [{"cis_user_id": r, "name": f"Name {r}"} for r in refs if not r.startswith("system:")],
            ),
            ("FROM booking.season_calendars c WHERE c.id = $1::uuid", cal),
            ("WHERE ($1::text IS NULL OR c.owner_kind = $1)", [cal] if cal else []),
            (
                "FROM booking.season_calendars c WHERE c.id = ANY",
                lambda ids: [
                    {"id": i, "default_season": (cal or {}).get("default_season", "low"), "name": "Invented Coast"}
                    for i in ids
                ],
            ),
            ("WHERE r.calendar_id = ANY($1::uuid[]) AND r.status = 'approved'", lambda *_: list(approved or [])),
            ("FROM booking.holiday_events e WHERE e.status = 'approved'", lambda *_: list(events or [])),
            ("WHERE r.calendar_id = $1::uuid ORDER BY r.created_at DESC", lambda *_: list(ranges or [])),
            ("AND r.status IN ('pending', 'approved') AND r.id <> coalesce", lambda *_: list(live or [])),
            ("FROM booking.season_ranges r WHERE r.id = $1::uuid", rng),
            ("FROM booking.holiday_events e WHERE e.id = $1::uuid", event),
            ("FROM booking.trip_plans p LEFT JOIN booking.season_calendars c", plan),
            ("INSERT INTO booking.season_ranges", RANGE),
            ("INSERT INTO booking.holiday_events", EVENT),
        ]
    )


def audited(conn: ScriptedConn) -> list[str]:
    return [args[3] for args in conn.sql(AUDIT)]


def audit_after(conn: ScriptedConn, action: str) -> dict[str, Any]:
    return next(json.loads(args[7]) for args in conn.sql(AUDIT) if args[3] == action)


# ── Shapes and reads ─────────────────────────────────────────────────────────


def test_the_state_of_a_change() -> None:
    assert sa.state(range_row()) == "pending"
    assert sa.state(range_row(status="approved", end_requested_at=STAMP)) == "ending"
    assert sa.state(range_row(status="approved", end_requested_at=STAMP, ended_at=STAMP)) == "ended"
    out = sa.range_out(range_row(holiday_country="VN ", date_from=None, date_to=None))
    assert (out["holiday_country"], out["date_to"], out["state"]) == ("VN", None, "pending")
    assert sa.range_out(range_row())["date_to"] == date(2031, 3, 31)
    ev = sa.event_out(event_row(country_iso2="VN "))
    assert (ev["country_iso2"], ev["date_to"], ev["calendars"]) == ("VN", date(2031, 2, 13), 3)


@pytest.mark.asyncio
async def test_every_read_needs_pricing_read() -> None:
    conn = db(cal=calendar())
    for call in (
        sa.list_calendars(conn, CONTENT),
        sa.get_calendar(conn, CONTENT, CAL, None),
        sa.list_events(conn, CONTENT, None, None),
    ):
        with pytest.raises(Forbidden):
            await call
    assert conn.calls == []


@pytest.mark.asyncio
async def test_list_calendars_counts_and_people() -> None:
    out = await sa.list_calendars(db(cal=calendar(default_requested_by=CHECKER.cis_user_id)), SALES, "destination")
    item = out["items"][0]
    assert (item["ranges"], item["waiting"], item["plans"]) == (2, 1, 4)
    assert item["created_by"] == {"id": MAKER.cis_user_id, "name": f"Name {MAKER.cis_user_id}"}
    assert item["default_requested_by"]["id"] == CHECKER.cis_user_id
    assert out["season_codes"] == ["low", "high", "peak", "festive"]


@pytest.mark.asyncio
async def test_a_calendar_of_a_year_resolves_its_segments_festive_and_stop_sales() -> None:
    approved = [
        {**range_row(status="approved"), "id": "r-high"},
        {
            **range_row(
                status="approved",
                season_code="festive",
                date_from=None,
                date_to=None,
                holiday_country="VN ",
                holiday_code="invented_fest",
                days_before=1,
                days_after=0,
            ),
            "id": "r-fest",
        },
        {
            **range_row(
                status="approved",
                kind="stop_sale",
                season_code=None,
                date_from=date(2031, 12, 20),
                date_to=date(2032, 1, 5),
            ),
            "id": "r-stop",
        },
    ]
    events = [{"country": "VN", "code": "invented_fest", "date_from": date(2031, 2, 10), "date_to": date(2031, 2, 14)}]
    conn = db(cal=calendar(), ranges=[range_row()], approved=approved, events=events)
    out = await sa.get_calendar(conn, SALES, CAL, 2031)
    assert out["year"] == 2031 and out["ranges"][0]["state"] == "pending"
    assert out["segments"] == [
        {"from": date(2031, 1, 1), "to": date(2031, 2, 8), "season": "high"},
        {"from": date(2031, 2, 9), "to": date(2031, 2, 13), "season": "festive"},  # the event, 1 day before
        {"from": date(2031, 2, 14), "to": date(2031, 3, 31), "season": "high"},
        {"from": date(2031, 4, 1), "to": date(2031, 12, 31), "season": "low"},
    ]
    assert out["stop_sales"] == [{"from": date(2031, 12, 20), "to": date(2031, 12, 31)}]  # cut to the year
    assert conn.sql("FROM booking.holiday_events e WHERE e.status = 'approved'")[0] == (["VN:invented_fest"],)
    assert "segments" not in await sa.get_calendar(db(cal=calendar()), SALES, CAL, None)


@pytest.mark.asyncio
async def test_unknown_calendar() -> None:
    with pytest.raises(NotFound) as err:
        await sa.get_calendar(db(), SALES, CAL, None)
    assert err.value.code == "calendar_not_found"
    bad = db().on("FROM booking.season_calendars c WHERE c.id = $1::uuid", asyncpg.DataError("bad uuid"))
    with pytest.raises(NotFound):
        await sa.get_calendar(bad, SALES, "x", None)


# ── Calendars and the default season ────────────────────────────────────────


@pytest.mark.parametrize(
    "body",
    [
        {"owner_kind": "region", "destination_id": DEST, "name": "X"},
        {"owner_kind": "destination", "name": "X"},
        {"owner_kind": "destination", "destination_id": DEST, "supplier_contract_id": CONTRACT, "name": "X"},
        {"owner_kind": "supplier_contract", "destination_id": DEST, "name": "X"},
    ],
)
@pytest.mark.asyncio
async def test_a_calendar_belongs_to_one_destination_or_one_contract(body: dict[str, Any]) -> None:
    with pytest.raises(Invalid) as err:
        await sa.create_calendar(db(), MAKER, body)
    assert err.value.code == "invalid_owner"


@pytest.mark.asyncio
async def test_create_calendar() -> None:
    with pytest.raises(Forbidden):
        await sa.create_calendar(db(), SALES, {"owner_kind": "destination", "destination_id": DEST, "name": "X"})
    with pytest.raises(Invalid) as err:
        await sa.create_calendar(db(), MAKER, {"owner_kind": "destination", "destination_id": DEST, "name": " "})
    assert err.value.code == "name_required"
    contract = {"owner_kind": "supplier_contract", "supplier_contract_id": CONTRACT, "name": "Invented Lodge 2031"}
    with pytest.raises(Invalid) as err:
        await sa.create_calendar(db(), MAKER, contract)
    assert err.value.code == "contract_not_found"
    taken = db().on("SELECT 1 FROM booking.supplier_contracts", 1)
    with pytest.raises(Conflict) as err:
        await sa.create_calendar(taken, MAKER, contract)
    assert err.value.code == "calendar_exists"
    conn = db(cal=calendar(owner_kind="supplier_contract", supplier_contract_id=CONTRACT, destination_id=None))
    conn.on("SELECT 1 FROM booking.supplier_contracts", 1).on("INSERT INTO booking.season_calendars", CAL)
    out = await sa.create_calendar(conn, MAKER, contract)
    assert conn.sql("INSERT INTO booking.season_calendars")[0] == (
        "supplier_contract",
        None,
        CONTRACT,
        "Invented Lodge 2031",
        MAKER.cis_user_id,
    )
    assert out["id"] == CAL and audited(conn) == ["season_calendar.created"]


@pytest.mark.asyncio
async def test_propose_a_default_season() -> None:
    with pytest.raises(Invalid) as err:
        await sa.propose_default(db(cal=calendar()), MAKER, CAL, "monsoon", "Rains")
    assert err.value.code == "invalid_season"
    with pytest.raises(Invalid) as err:
        await sa.propose_default(db(cal=calendar()), MAKER, CAL, "high", None)
    assert err.value.code == "reason_required"
    with pytest.raises(Conflict) as err:
        await sa.propose_default(db(cal=calendar(pending_default_season="peak")), MAKER, CAL, "high", "Busy")
    assert err.value.code == "default_pending"
    conn = db(cal=calendar())
    await sa.propose_default(conn, MAKER, CAL, "high", " Busy year ")
    assert conn.sql("SET pending_default_season = $2")[0] == (CAL, "high", MAKER.cis_user_id, "Busy year")
    assert conn.sql("FOR UPDATE") == [(CAL,)]  # under the calendar's lock
    assert audit_after(conn, "season_calendar.default_proposed") == {"to": "high", "reason": "Busy year"}


@pytest.mark.asyncio
async def test_decide_the_default_season_by_a_second_person() -> None:
    waiting = calendar(
        pending_default_season="high", default_requested_by=MAKER.cis_user_id, default_request_reason="Busy year"
    )
    with pytest.raises(Conflict) as err:
        await sa.decide_default(db(cal=calendar()), CHECKER, CAL, approve=True)
    assert err.value.code == "nothing_pending"
    with pytest.raises(Forbidden) as err:
        await sa.decide_default(db(cal=waiting), MAKER, CAL, approve=True)
    assert err.value.code == "maker_checker"
    with pytest.raises(Forbidden) as err:
        await sa.decide_default(db(cal=waiting), SALES, CAL, approve=True)
    assert err.value.code == "forbidden"
    with pytest.raises(Invalid):
        await sa.decide_default(db(cal=waiting), CHECKER, CAL, approve=False)
    approve = db(cal=waiting)
    await sa.decide_default(approve, CHECKER, CAL, approve=True)
    assert approve.sql("SET default_season = CASE WHEN $2")[0] == (CAL, True)
    reject = db(cal=waiting)
    await sa.decide_default(reject, CHECKER, CAL, approve=False, reason="Not this year")
    after = audit_after(reject, "season_calendar.default_rejected")
    assert after == {"to": "high", "request_reason": "Busy year", "reason": "Not this year"}


# ── Ranges ───────────────────────────────────────────────────────────────────


def _range(**over: Any) -> dict[str, Any]:
    body = {
        "kind": "season",
        "season_code": "peak",
        "date_from": date(2031, 7, 1),
        "date_to": date(2031, 8, 31),
        "note": "School holidays",
    }
    return {**body, **over}


@pytest.mark.parametrize(
    ("over", "code"),
    [
        ({"kind": "closure"}, "invalid_range"),
        ({"season_code": "monsoon"}, "invalid_range"),
        (
            {
                "season_code": "high",
                "holiday_country": "VN",
                "holiday_code": "invented_fest",
                "date_from": None,
                "date_to": None,
            },
            "invalid_range",
        ),  # only a festive range names an event
        ({"season_code": "festive", "holiday_country": "VN", "date_from": None, "date_to": None}, "invalid_range"),
        ({"season_code": "festive", "holiday_country": "VN", "holiday_code": "invented_fest"}, "invalid_range"),
        (
            {
                "season_code": "festive",
                "holiday_country": "VN",
                "holiday_code": "invented_fest",
                "date_from": None,
                "date_to": None,
                "days_before": 61,
            },
            "invalid_days",
        ),
        ({"date_to": None}, "invalid_dates"),
        ({"date_to": date(2031, 6, 30)}, "invalid_dates"),
        ({"date_from": date(2031, 1, 1), "date_to": date(2032, 1, 2)}, "invalid_dates"),  # more than a year
        ({"note": ""}, "note_required"),
        ({"note": "x" * 301}, "note_too_long"),
    ],
)
@pytest.mark.asyncio
async def test_a_range_proposal_is_checked(over: dict[str, Any], code: str) -> None:
    conn = db(cal=calendar())
    with pytest.raises(Invalid) as err:
        await sa.propose_range(conn, MAKER, CAL, _range(**over))
    assert err.value.code == code
    assert conn.sql("INSERT INTO booking.season_ranges") == []


@pytest.mark.asyncio
async def test_fixed_season_ranges_never_overlap() -> None:
    live = [
        {
            "id": "r-high",
            "date_from": date(2031, 8, 1),
            "date_to": date(2031, 10, 1),
            "season_code": "high",
            "status": "approved",
        }
    ]
    conn = db(cal=calendar(), live=live)
    with pytest.raises(Conflict) as err:
        await sa.propose_range(conn, MAKER, CAL, _range())
    assert err.value.code == "range_overlaps" and err.value.extra == {"range_id": "r-high"}
    assert "2031-08-01 to 2031-09-30" in err.value.message
    # Touching (half-open) is fine, and a stop-sale is an overlay: never checked against seasons.
    ok = db(cal=calendar(), rng=range_row(), live=live)
    await sa.propose_range(ok, MAKER, CAL, _range(date_to=date(2031, 7, 31)))
    stop = db(cal=calendar(), rng=range_row(kind="stop_sale"), live=live)
    await sa.propose_range(stop, MAKER, CAL, _range(kind="stop_sale", season_code="peak"))
    assert stop.sql("AND r.status IN ('pending', 'approved')") == []
    assert stop.sql("INSERT INTO booking.season_ranges")[0][1:3] == ("stop_sale", None)


@pytest.mark.asyncio
async def test_a_festive_range_names_a_holiday_event_and_no_dates() -> None:
    conn = db(cal=calendar(), rng=range_row(season_code="festive", date_from=None, date_to=None))
    body = _range(
        season_code="festive",
        date_from=None,
        date_to=None,
        holiday_country=" vn ",
        holiday_code="invented_fest",
        days_before=2,
        days_after=1,
    )
    out = await sa.propose_range(conn, MAKER, CAL, body)
    assert conn.sql("INSERT INTO booking.season_ranges")[0] == (
        CAL,
        "season",
        "festive",
        None,
        None,
        "VN",
        "invented_fest",
        2,
        1,
        "School holidays",
        MAKER.cis_user_id,
    )
    assert out["created_by"]["id"] == MAKER.cis_user_id and audited(conn) == ["season_range.proposed"]
    with pytest.raises(Forbidden):
        await sa.propose_range(db(cal=calendar()), SALES, CAL, body)


@pytest.mark.asyncio
async def test_approving_a_range_is_a_second_persons_and_rechecks_the_overlap() -> None:
    with pytest.raises(Forbidden) as err:
        await sa.decide_range(db(cal=calendar(), rng=range_row()), MAKER, RANGE, "approve")
    assert err.value.code == "maker_checker"
    live = [
        {
            "id": "r-x",
            "date_from": date(2031, 3, 1),
            "date_to": date(2031, 5, 1),
            "season_code": "peak",
            "status": "approved",
        }
    ]
    clash = db(cal=calendar(), rng=range_row(), live=live)
    with pytest.raises(Conflict) as err:
        await sa.decide_range(clash, CHECKER, RANGE, "approve")
    assert err.value.code == "range_overlaps"
    assert clash.sql("AND r.status IN ('pending', 'approved')")[0] == (CAL, RANGE)  # its own row is skipped
    conn = db(cal=calendar(), rng=range_row())
    out = await sa.decide_range(conn, CHECKER, RANGE, "approve")
    assert conn.sql("UPDATE booking.season_ranges SET status = $2")[0] == (
        RANGE,
        "approved",
        CHECKER.cis_user_id,
        None,
        None,
    )
    assert out["id"] == RANGE and audited(conn) == ["season_range.approve"]
    with pytest.raises(Conflict) as err:
        await sa.decide_range(db(cal=calendar(), rng=range_row(status="approved")), CHECKER, RANGE, "reject", "No")
    assert err.value.code == "not_pending"


@pytest.mark.asyncio
async def test_reject_and_withdraw_need_a_reason() -> None:
    with pytest.raises(Invalid) as err:
        await sa.decide_range(db(cal=calendar(), rng=range_row()), CHECKER, RANGE, "reject")
    assert err.value.code == "reason_required"
    reject = db(cal=calendar(), rng=range_row())
    await sa.decide_range(reject, CHECKER, RANGE, "reject", "Wrong dates")
    assert reject.sql("UPDATE booking.season_ranges SET status = $2")[0][1:] == (
        "rejected",
        CHECKER.cis_user_id,
        "Wrong dates",
        None,
    )
    with pytest.raises(Conflict) as err:
        await sa.decide_range(db(cal=calendar(), rng=range_row()), CHECKER, RANGE, "withdraw", "Mine?")
    assert err.value.code == "not_withdrawable"
    withdraw = db(cal=calendar(), rng=range_row())
    await sa.decide_range(withdraw, MAKER, RANGE, "withdraw", "Typo")
    assert withdraw.sql("UPDATE booking.season_ranges SET status = $2")[0][1:] == (
        "withdrawn",
        MAKER.cis_user_id,
        None,
        "Typo",
    )
    with pytest.raises(NotFound):
        await sa.decide_range(db(cal=calendar()), CHECKER, RANGE, "approve")
    bad = db(cal=calendar()).on("FROM booking.season_ranges r WHERE r.id = $1::uuid", asyncpg.DataError("bad uuid"))
    with pytest.raises(NotFound) as err:
        await sa.decide_range(bad, CHECKER, "x", "approve")
    assert err.value.code == "range_not_found"


@pytest.mark.asyncio
async def test_removing_an_approved_range_is_asked_then_decided() -> None:
    approved = range_row(status="approved")
    ending = range_row(
        status="approved", end_requested_by=MAKER.cis_user_id, end_requested_at=STAMP, end_reason="Season moved"
    )
    with pytest.raises(Conflict) as err:
        await sa.decide_range(db(cal=calendar(), rng=range_row()), MAKER, RANGE, "end", "Gone")
    assert err.value.code == "not_in_force"
    ask = db(cal=calendar(), rng=approved)
    await sa.decide_range(ask, MAKER, RANGE, "end", "Season moved")
    assert ask.sql("SET end_requested_by = $2")[0] == (RANGE, MAKER.cis_user_id, "Season moved")
    with pytest.raises(Conflict) as err:
        await sa.decide_range(db(cal=calendar(), rng=ending), MAKER, RANGE, "end", "Again")
    assert err.value.code == "end_pending"
    with pytest.raises(Conflict) as err:
        await sa.decide_range(db(cal=calendar(), rng=approved), CHECKER, RANGE, "end_approve")
    assert err.value.code == "nothing_pending"
    with pytest.raises(Forbidden) as err:
        await sa.decide_range(db(cal=calendar(), rng=ending), MAKER, RANGE, "end_approve")
    assert err.value.code == "maker_checker"
    with pytest.raises(Invalid) as err:
        await sa.decide_range(db(cal=calendar(), rng=ending), CHECKER, RANGE, "end_reject")
    assert err.value.code == "reason_required"
    keep = db(cal=calendar(), rng=ending)
    await sa.decide_range(keep, CHECKER, RANGE, "end_reject", "Still needed")
    assert keep.sql("SET end_requested_by = NULL") == [(RANGE,)]
    assert audit_after(keep, "season_range.end_reject") == {"reason": "Still needed", "end_reason": "Season moved"}
    end = db(cal=calendar(), rng=ending)
    await sa.decide_range(end, CHECKER, RANGE, "end_approve")
    assert end.sql("SET ended_by = $2") == [(RANGE, CHECKER.cis_user_id)]
    with pytest.raises(Invalid) as err:
        await sa.decide_range(db(cal=calendar(), rng=approved), CHECKER, RANGE, "erase", "Why")
    assert err.value.code == "invalid_action"


# ── Holiday events ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_events_with_the_countries() -> None:
    conn = db().on("WHERE ($1::text IS NULL OR e.country_iso2 = $1)", [event_row()])
    conn.on("SELECT iso2::text AS iso2, name FROM booking.countries", [{"iso2": "VN ", "name": "Vietnam"}])
    out = await sa.list_events(conn, SALES, "vn", 2031)
    assert conn.sql("WHERE ($1::text IS NULL OR e.country_iso2 = $1)")[0] == ("VN", 2031)
    assert out["countries"] == [{"iso2": "VN", "name": "Vietnam"}]
    assert out["items"][0]["created_by"]["id"] == MAKER.cis_user_id
    await sa.list_events(conn, SALES, None, None)
    assert conn.sql("WHERE ($1::text IS NULL OR e.country_iso2 = $1)")[1] == (None, None)


def _event(**over: Any) -> dict[str, Any]:
    body = {
        "country_iso2": "vn",
        "code": " Invented_Fest ",
        "name": "Invented Festival",
        "date_from": date(2031, 2, 10),
        "date_to": date(2031, 2, 13),
        "reason": "Set by the board",
    }
    return {**body, **over}


@pytest.mark.parametrize(
    ("over", "code"),
    [
        ({"name": ""}, "name_required"),
        ({"reason": None}, "reason_required"),
        ({"date_to": None}, "invalid_dates"),
        ({"date_to": date(2031, 2, 9)}, "invalid_dates"),
        ({"date_to": date(2031, 4, 11)}, "invalid_dates"),  # 60 days or more
        ({"country_iso2": "zz"}, "invalid_country"),
    ],
)
@pytest.mark.asyncio
async def test_a_holiday_event_proposal_is_checked(over: dict[str, Any], code: str) -> None:
    conn = db().on("SELECT 1 FROM booking.countries WHERE iso2 = $1", lambda c: 1 if c == "VN" else None)
    with pytest.raises(Invalid) as err:
        await sa.propose_event(conn, MAKER, _event(**over))
    assert err.value.code == code


@pytest.mark.asyncio
async def test_propose_a_holiday_event() -> None:
    def conn_with(insert: Any) -> ScriptedConn:
        conn = db(event=event_row()).on("SELECT 1 FROM booking.countries WHERE iso2 = $1", 1)
        return conn.on("INSERT INTO booking.holiday_events", insert)

    conn = conn_with(EVENT)
    out = await sa.propose_event(conn, MAKER, _event())
    assert conn.sql("INSERT INTO booking.holiday_events")[0] == (
        "VN",
        "invented_fest",
        "Invented Festival",
        2031,
        date(2031, 2, 10),
        date(2031, 2, 14),
        "Set by the board",
        MAKER.cis_user_id,
    )
    assert out["id"] == EVENT and audited(conn) == ["holiday_event.proposed"]
    with pytest.raises(Conflict) as err:
        await sa.propose_event(conn_with(asyncpg.UniqueViolationError("duplicate")), MAKER, _event())
    assert err.value.code == "event_exists"
    with pytest.raises(Invalid) as err:
        await sa.propose_event(conn_with(asyncpg.CheckViolationError("check")), MAKER, _event(code="x"))
    assert err.value.code == "invalid_event"


@pytest.mark.asyncio
async def test_decide_a_holiday_event() -> None:
    with pytest.raises(NotFound):
        await sa.decide_event(db(), CHECKER, EVENT, "approve")
    bad = db().on("FROM booking.holiday_events e WHERE e.id = $1::uuid", asyncpg.DataError("bad uuid"))
    with pytest.raises(NotFound):
        await sa.decide_event(bad, CHECKER, "x", "approve")
    with pytest.raises(Forbidden) as err:
        await sa.decide_event(db(event=event_row()), MAKER, EVENT, "approve")
    assert err.value.code == "maker_checker"
    with pytest.raises(Conflict) as err:
        await sa.decide_event(db(event=event_row(status="approved")), CHECKER, EVENT, "approve")
    assert err.value.code == "not_pending"
    conn = db(event=event_row())
    await sa.decide_event(conn, CHECKER, EVENT, "approve")
    assert conn.sql("UPDATE booking.holiday_events SET status = $2")[0][1] == "approved"
    with pytest.raises(Conflict) as err:
        await sa.decide_event(db(event=event_row()), CHECKER, EVENT, "withdraw", "Not mine")
    assert err.value.code == "not_withdrawable"
    withdraw = db(event=event_row())
    await sa.decide_event(withdraw, MAKER, EVENT, "withdraw", "Duplicate")
    assert audited(withdraw) == ["holiday_event.withdraw"]
    end = db(event=event_row(status="approved"))
    await sa.decide_event(end, MAKER, EVENT, "end", "Cancelled this year")
    assert end.sql("UPDATE booking.holiday_events SET end_requested_by = $2")[0][2] == "Cancelled this year"


# ── The calendar of a trip plan ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_plan_calendar_and_its_waiting_change() -> None:
    assert await sa.plan_calendar(db(plan=plan_row()), SOURCE) == {
        "source_id": SOURCE,
        "season_calendar_id": CAL,
        "season_calendar_name": "Invented Coast",
        "request": None,
    }
    waiting = plan_row(
        pending_calendar_id=OTHER_CAL,
        pending_calendar_name="Invented Hills",
        requested_by=MAKER.cis_user_id,
        requested_at=STAMP,
        request_reason="Tour starts inland",
    )
    request = (await sa.plan_calendar(db(plan=waiting), SOURCE))["request"]
    assert request == {
        "season_calendar_id": OTHER_CAL,
        "season_calendar_name": "Invented Hills",
        "requested_by": {"id": MAKER.cis_user_id, "name": f"Name {MAKER.cis_user_id}"},
        "requested_at": STAMP,
        "reason": "Tour starts inland",
    }
    with pytest.raises(NotFound):
        await sa.plan_calendar(db(), SOURCE)


@pytest.mark.asyncio
async def test_request_another_calendar_for_a_plan() -> None:
    other = calendar(id=OTHER_CAL)
    with pytest.raises(Invalid) as err:
        await sa.request_plan_calendar(db(plan=plan_row(), cal=other), MAKER, SOURCE, OTHER_CAL, None)
    assert err.value.code == "reason_required"
    waiting = plan_row(requested_by=CHECKER.cis_user_id)
    with pytest.raises(Conflict) as err:
        await sa.request_plan_calendar(db(plan=waiting, cal=other), MAKER, SOURCE, OTHER_CAL, "Inland")
    assert err.value.code == "calendar_change_pending"
    supplier = calendar(id=OTHER_CAL, owner_kind="supplier_contract")
    with pytest.raises(Invalid) as err:
        await sa.request_plan_calendar(db(plan=plan_row(), cal=supplier), MAKER, SOURCE, OTHER_CAL, "Inland")
    assert err.value.code == "invalid_calendar"
    with pytest.raises(Conflict) as err:
        await sa.request_plan_calendar(db(plan=plan_row(), cal=calendar()), MAKER, SOURCE, CAL, "Same")
    assert err.value.code == "no_change"
    conn = db(plan=plan_row(), cal=other)
    await sa.request_plan_calendar(conn, MAKER, SOURCE, OTHER_CAL, " Tour starts inland ")
    assert conn.sql("SET pending_season_calendar_id = $2::uuid")[0] == (
        PLAN,
        OTHER_CAL,
        MAKER.cis_user_id,
        "Tour starts inland",
    )
    assert audited(conn) == ["trip_plan.season_calendar_requested"]
    none = db(plan=plan_row())
    await sa.request_plan_calendar(none, MAKER, SOURCE, None, "No seasons for this tour")
    assert none.sql("SET pending_season_calendar_id = $2::uuid")[0][1] is None


@pytest.mark.asyncio
async def test_decide_the_calendar_change_of_a_plan() -> None:
    waiting = plan_row(pending_calendar_id=OTHER_CAL, requested_by=MAKER.cis_user_id, request_reason="Inland")
    with pytest.raises(Invalid) as err:
        await sa.decide_plan_calendar(db(plan=waiting), CHECKER, SOURCE, "apply", None)
    assert err.value.code == "invalid_action"
    with pytest.raises(Conflict) as err:
        await sa.decide_plan_calendar(db(plan=plan_row()), CHECKER, SOURCE, "approve", None)
    assert err.value.code == "nothing_pending"
    with pytest.raises(Forbidden) as err:
        await sa.decide_plan_calendar(db(plan=waiting), MAKER, SOURCE, "approve", None)
    assert err.value.code == "maker_checker"
    with pytest.raises(Invalid):
        await sa.decide_plan_calendar(db(plan=waiting), CHECKER, SOURCE, "reject", " ")
    with pytest.raises(Conflict) as err:
        await sa.decide_plan_calendar(db(plan=waiting), CHECKER, SOURCE, "withdraw", "Not mine")
    assert err.value.code == "not_withdrawable"
    approve = db(plan=waiting)
    await sa.decide_plan_calendar(approve, CHECKER, SOURCE, "approve", None)
    assert approve.sql("SET season_calendar_id = pending_season_calendar_id") == [(PLAN,)]
    after = audit_after(approve, "trip_plan.season_calendar_approve")
    assert after == {
        "season_calendar_id": OTHER_CAL,
        "reason": None,
        "requested_by": MAKER.cis_user_id,
        "request_reason": "Inland",
    }
    withdraw = db(plan=waiting)
    await sa.decide_plan_calendar(withdraw, MAKER, SOURCE, "withdraw", "Changed my mind")
    assert withdraw.sql("SET season_calendar_id = pending") == []
    assert withdraw.sql("SET pending_season_calendar_id = NULL") == [(PLAN,)]


@pytest.mark.asyncio
async def test_default_calendars_follow_the_destination_of_day_one() -> None:
    plans = [{"id": "p-1", "tour_id": "t-1"}, {"id": "p-2", "tour_id": "t-2"}, {"id": "p-3", "tour_id": "t-3"}]
    places = {
        "t-1": {"destination_id": DEST, "destination_name": "Invented Bay"},
        "t-3": {"destination_id": DEST, "destination_name": None},
    }
    made: list[str] = []

    def count(dest: str) -> int:
        return int(dest in made)

    def calendar_for(dest: str, name: str, who: str) -> str:
        made.append(dest)
        return CAL

    conn = ScriptedConn(
        [
            ("WHERE p.season_calendar_id IS NULL ORDER BY md5", plans),
            ("FROM catalog.v_tour_place pl", lambda tour: places.get(tour)),
            ("SELECT count(*) FROM booking.season_calendars WHERE destination_id", count),
            ("WITH made AS", calendar_for),
        ]
    )
    out = await sa.assign_default_calendars(conn, 10, "salt-1")
    assert out == {"assigned": 2, "calendars_created": 1, "no_destination": 1}
    assert conn.sql("WITH made AS") == [(DEST, "Invented Bay", sa.SYSTEM), (DEST, "Destination", sa.SYSTEM)]
    assert conn.sql("SET season_calendar_id = $2::uuid") == [("p-1", CAL), ("p-3", CAL)]
    assert conn.sql("ORDER BY md5")[0] == (10, "salt-1")
