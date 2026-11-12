"""AA-1091 (bk-054, root ADR 0005b d1, d2, d9, d10) price list versions without a database: reads and their
permission, drafts (default window from the policy's tariff year, copies from the prices in force or another version,
the calendar's peak on each row with the legacy flag until the calendar is configured), the frozen diff and impact on
submit (SHA-256, floors, pending clash), the maker-checker refusals of approve / reject / withdraw, the derived
superseded states and the withdrawal that lets an older version price its dates again, and the workbook import draft.
A scripted connection answers by SQL fragment (tests/unit/fakes.py); invented plans and people only."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal as D
from typing import Any

import asyncpg
import pytest

from app.auth.staff import Staff
from app.catalog import cost_sheet, version_matrix
from app.catalog import price_versions as pv
from app.errors import Conflict, Forbidden, Invalid, NotFound, Unavailable
from app.pricing import engine, seasons
from tests.unit.fakes import POLICY_FRAGMENT, ScriptedConn, policy_rows

DAY = date(2031, 3, 1)  # quote day: tariff year 2031 (policy start 11-01) = 2030-11-01 .. 2031-10-31
TY = (date(2030, 11, 1), date(2031, 11, 1))
PLAN = "aa109100-0000-4000-8000-0000000000a1"
OTHER_PLAN = "aa109100-0000-4000-8000-0000000000a2"
SOURCE = "aa109100-0000-4000-8000-0000000000b1"
VID = "aa109100-0000-4000-8000-0000000000c3"
OLDER = "aa109100-0000-4000-8000-0000000000c2"
CAL = "aa109100-0000-4000-8000-0000000000d1"
STAMP = datetime(2031, 2, 1, 9, 0, tzinfo=UTC)
MAKER = Staff("maker-1091", None, "Invented Maker", "contracting", True, None)
CHECKER = Staff("checker-1091", None, "Invented Checker", "admin", True, None)
SALES = Staff("sales-1091", None, "Invented Seller", "sales", True, None)
CONTENT = Staff("content-1091", None, "Invented Writer", "content_creator", True, None)

AUDIT = "INSERT INTO booking.audit_logs"
ROW_INSERT = "INSERT INTO booking.price_list_version_rows"
PAX_NONE = {k: None for k in pv.PAX_FIELDS}


@pytest.fixture(autouse=True)
def _fixed_day(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pv, "today", lambda: DAY)


def version(**over: Any) -> dict[str, Any]:
    row = {
        "id": VID,
        "trip_plan_id": PLAN,
        "source_id": SOURCE,
        "version_no": 3,
        "label": "2031 prices",
        "tariff_year": 2031,
        "travel_from": TY[0],
        "travel_to": TY[1],
        "sale_from": None,
        "sale_to": None,
        "status": "draft",
        "source": "staff",
        "reason": None,
        "replaces_id": None,
        "created_by": MAKER.cis_user_id,
        "created_at": STAMP,
        "updated_at": STAMP,
        "submitted_at": None,
        "approved_by": None,
        "approved_at": None,
        "rejected_by": None,
        "rejected_at": None,
        "reject_reason": None,
        "withdrawn_by": None,
        "withdrawn_at": None,
        "withdraw_reason": None,
        "superseded_at": None,
        "diff_sha256": None,
        "rows": 1,
    }
    return {**row, **over}


def stored(position: int = 1, **over: Any) -> dict[str, Any]:
    """A version row as ROWS_SQL reads it (half-open travel dates, text amounts)."""
    row = {
        "position": position,
        "channel": "b2b",
        "season": None,
        "peak": False,
        "travel_from": date(2030, 11, 1),
        "travel_to": date(2031, 5, 1),
        "pax_min": 2,
        "pax_max": 2,
        "currency": "USD ",
        "amount": "560.00",
        "rrp_amount": None,
        "net_amount": None,
        "net_currency": None,
        "effective_margin": None,
        "priced_on": None,
        "overridden": False,
        "computed": False,
        **PAX_NONE,
    }
    return {**row, **over}


def matrix(**over: Any) -> dict[str, Any]:
    """A row of the matrix in force as trip_plan_prices.ROWS_SQL reads it (inclusive valid_to)."""
    row = {
        "position": 1,
        "channel": "b2b",
        "season": None,
        "peak": False,
        "valid_from": date(2030, 11, 1),
        "valid_to": date(2031, 10, 31),
        "pax_min": 2,
        "pax_max": 2,
        "currency": "USD",
        "amount": "500.00",
        "net_amount": None,
        "net_currency": None,
        "effective_margin": None,
        "priced_on": None,
        "overridden": False,
        "computed": False,
        "rrp_amount": None,
        "created_by": None,
        "version_id": OLDER,
        "season_code": "low",
        **PAX_NONE,
    }
    return {**row, **over}


def published(vid: str, no: int, start: date, end: date, *, at: int, status: str = "published") -> dict[str, Any]:
    return {
        "id": vid,
        "version_no": no,
        "travel_from": start,
        "travel_to": end,
        "sale_from": None,
        "sale_to": None,
        "approved_at": datetime(2031, 1, at, tzinfo=UTC),
        "provisional": False,
        "status": status,
    }


def rule_row(channel: Any, _on: Any) -> Any:
    if channel == engine.Channel.FIXED_DEPARTURE.value:
        return engine.PricingError("a stored rule the engine refuses")
    if channel != engine.Channel.B2B.value:
        return None
    return {
        "channel": "b2b",
        "target_margin": D("0.20"),
        "floor_margin": D("0.12"),
        "peak_uplift_pts": D("0.05"),
        "card_fee_pct": D("0"),
        "valid_from": date(2030, 1, 1),
        "rrp_markup_pct": None,
    }


def db(
    ver: dict[str, Any] | None = None,
    *,
    plan: dict[str, Any] | None | bool = True,
    rows: list[dict[str, Any]] | None = None,
    in_force: list[dict[str, Any]] | None = None,
    pub: list[dict[str, Any]] | None = None,
    calendar: str | None = None,
    ranges: list[dict[str, Any]] | None = None,
    extra: dict[str, Any] | None = None,
    clash: int | None = None,
    latest: str | None = None,
    quotes: int = 0,
    departures: list[date] | None = None,
    imports: list[dict[str, Any]] | None = None,
) -> ScriptedConn:
    """The scripted database of one plan. ``pub`` is live: a withdrawal removes the version from it, so the overlap
    rule that runs after the write sees the versions left."""
    plan_row = {"id": PLAN, "source_id": SOURCE, "calendar_id": calendar} if plan is True else plan
    live = pub if pub is not None else []

    def withdrawn(vid: str, *_: Any) -> None:
        live[:] = [p for p in live if p["id"] != vid]

    return ScriptedConn(
        [
            ("SET status = 'withdrawn'", withdrawn),
            ("WHERE v.id = $1::uuid", lambda *_: None if ver is None else dict(ver)),
            ("SELECT diff, impact FROM booking.price_list_versions", extra or {"diff": None, "impact": None}),
            ("FROM booking.price_list_version_rows r WHERE r.version_id = $1::uuid", lambda *_: list(rows or [])),
            (
                "FROM booking.staff_users WHERE cis_user_id = ANY",
                lambda refs: [{"cis_user_id": r, "name": f"Name {r}"} for r in refs if not r.startswith("system:")],
            ),
            ("AS calendar_id FROM booking.trip_plans WHERE source_system = 'cis'", plan_row),
            (
                "FROM booking.trip_plans p WHERE p.id = ANY",
                lambda ids: [{"plan_id": p, "calendar_id": calendar} for p in ids],
            ),
            (
                "FROM booking.season_calendars c WHERE c.id = ANY",
                lambda ids: [{"id": i, "default_season": "low", "name": "Invented Coast"} for i in ids],
            ),
            ("WHERE r.calendar_id = ANY($1::uuid[]) AND r.status = 'approved'", lambda *_: list(ranges or [])),
            ("v.status IN ('published', 'superseded')", lambda *_: [dict(p) for p in live]),
            ("FROM booking.trip_plan_prices pr WHERE pr.trip_plan_id = $1::uuid", lambda *_: list(in_force or [])),
            ("status = 'published' ORDER BY approved_at DESC", latest),
            ("INSERT INTO booking.price_list_versions", VID),
            ("AND status = 'pending' AND travel_from < $3", clash),
            ("FROM booking.b2b_quotes q JOIN", [{"id": f"q-{n}"} for n in range(quotes)]),
            ("FROM booking.departures d WHERE d.trip_plan_id", [{"start_date": d} for d in departures or []]),
            ("FROM booking.pricing_rules r JOIN booking.policy_approvals", rule_row),
            ("AND source = 'import' AND created_by = $2", lambda *_: list(imports or [])),
        ]
    )


def audited(conn: ScriptedConn) -> list[str]:
    return [args[3] for args in conn.sql(AUDIT)]


def audit_after(conn: ScriptedConn, action: str) -> dict[str, Any]:
    return next(json.loads(args[7]) for args in conn.sql(AUDIT) if args[3] == action)


def peak_range(start: date, end: date, code: str = "peak") -> dict[str, Any]:
    """An approved fixed-date season range of the plan's calendar (seasons.RANGES_SQL shape)."""
    return {
        "id": f"r-{code}",
        "calendar_id": CAL,
        "kind": "season",
        "season_code": code,
        "date_from": start,
        "date_to": end,
        "holiday_country": None,
        "holiday_code": None,
        "days_before": 0,
        "days_after": 0,
    }


# ── Pure helpers ─────────────────────────────────────────────────────────────


def test_feb_29_start_falls_back_in_a_common_year() -> None:
    assert pv.tariff_year_window(2031, "02-29") == (date(2030, 2, 28), date(2031, 2, 28))
    assert pv.tariff_year_of(date(2031, 2, 27), "02-29") == 2031


def test_merge_pieces_joins_a_row_cut_by_season_back_into_one() -> None:
    a = {**matrix(valid_to=date(2031, 1, 31), season_code="low"), "amount": "500.00"}
    b = {**matrix(valid_from=date(2031, 2, 1), season_code="peak"), "amount": "500.00"}
    other_price = matrix(pax_min=3, pax_max=3, amount="450.00")
    gap = {**matrix(valid_from=date(2031, 2, 5)), "amount": "500.00", "pax_min": 4, "pax_max": 4}
    merged = pv._merge_pieces([b, other_price, a, gap])
    assert [(r["pax_min"], r["valid_from"], r["valid_to"]) for r in merged] == [
        (2, date(2030, 11, 1), date(2031, 10, 31)),
        (3, date(2030, 11, 1), date(2031, 10, 31)),
        (4, date(2031, 2, 5), date(2031, 10, 31)),
    ]


def test_clip_and_row_shapes() -> None:
    assert pv._clip(matrix(valid_to=date(2030, 10, 1), valid_from=date(2030, 1, 1)), TY) is None
    cut = pv._clip(matrix(valid_from=date(2030, 6, 1), valid_to=date(2032, 1, 1)), TY)
    assert cut is not None and (cut["valid_from"], cut["valid_to"]) == (TY[0], date(2031, 10, 31))
    out = pv.row_out(stored(net_amount="400.00", net_currency="USD ", peak=None))
    assert (out["valid_from"], out["valid_to"], out["currency"]) == (date(2030, 11, 1), date(2031, 4, 30), "USD")
    assert (out["priced"], out["peak"], out["net_currency"]) == (True, False, "USD")
    plain = pv.row_out(stored())
    assert (plain["priced"], plain["net_currency"]) == (False, None)
    vo = pv.version_out(version(sale_to=date(2031, 4, 1), rows=4))
    assert (vo["travel_to"], vo["sale_to"], vo["row_count"]) == (date(2031, 10, 31), date(2031, 3, 31), 4)
    assert "rows" not in vo
    assert pv.version_out(version())["sale_to"] is None


def test_text_rules() -> None:
    assert pv._text("  ", "reason", 300) is None
    assert pv._text(" ok ", "reason", 300) == "ok"
    with pytest.raises(Invalid) as err:
        pv._text(None, "reason", 300, required=True)
    assert err.value.code == "reason_required"
    with pytest.raises(Invalid) as err:
        pv._text("x" * 301, "reason", 300)
    assert err.value.code == "reason_too_long"


# ── Reads ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_every_read_needs_pricing_read() -> None:
    conn = db(version())
    for call in (
        pv.list_versions(conn, CONTENT, SOURCE),
        pv.get_version(conn, CONTENT, VID),
        pv.pending(conn, CONTENT),
        pv.withdraw_preview(conn, CONTENT, VID),
    ):
        with pytest.raises(Forbidden):
            await call
    assert conn.calls == []


@pytest.mark.asyncio
async def test_list_versions_with_what_is_in_force_and_the_calendar() -> None:
    pub = [published(OLDER, 2, *TY, at=1, status="superseded"), published(VID, 3, date(2031, 4, 1), TY[1], at=2)]
    conn = db(version(), pub=pub, calendar=CAL)
    conn.on("ORDER BY v.version_no DESC", [version(status="published", approved_by=CHECKER.cis_user_id)])
    out = await pv.list_versions(conn, SALES, SOURCE)
    assert out["source_id"] == SOURCE and out["calendar"] == {
        "id": CAL,
        "name": "Invented Coast",
        "default_season": "low",
    }
    item = out["items"][0]
    assert item["created_by"] == {"id": MAKER.cis_user_id, "name": f"Name {MAKER.cis_user_id}"}
    assert item["approved_by"]["id"] == CHECKER.cis_user_id and item["travel_to"] == date(2031, 10, 31)
    # The newer version prices April on; the older one keeps the dates before (inclusive ends).
    assert out["in_force"] == [
        {"version_id": OLDER, "version_no": 2, "from": TY[0], "to": date(2031, 3, 31)},
        {"version_id": VID, "version_no": 3, "from": date(2031, 4, 1), "to": date(2031, 10, 31)},
    ]
    assert (await pv.list_versions(db(version()), SALES, SOURCE))["calendar"] is None


@pytest.mark.asyncio
async def test_unknown_plan_or_version() -> None:
    with pytest.raises(NotFound) as err:
        await pv.list_versions(db(plan=None), SALES, SOURCE)
    assert err.value.code == "trip_plan_not_found"
    with pytest.raises(NotFound) as err:
        await pv.get_version(db(None), SALES, VID)
    assert err.value.code == "price_version_not_found"
    bad = db(version()).on("WHERE v.id = $1::uuid", asyncpg.DataError("invalid input syntax for type uuid"))
    with pytest.raises(NotFound):
        await pv.get_version(bad, SALES, "not-a-uuid")


@pytest.mark.asyncio
async def test_get_version_decodes_the_frozen_diff_and_impact() -> None:
    diff = {"rows": [], "removed": [], "summary": {"added": 0}}
    conn = db(version(status="pending"), rows=[stored()], extra={"diff": json.dumps(diff), "impact": {"departures": 2}})
    out = await pv.get_version(conn, SALES, VID)
    assert out["diff"] == diff and out["impact"] == {"departures": 2}
    assert [(i["valid_to"], i["amount"]) for i in out["items"]] == [(date(2031, 4, 30), "560.00")]
    conn.on("WHERE v.status = 'pending' ORDER BY v.submitted_at", [version(status="pending")])
    assert [i["id"] for i in (await pv.pending(conn, SALES))["items"]] == [VID]


# ── Drafts ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_draft_needs_price_version_write() -> None:
    for call in (
        pv.create_draft(db(), SALES, SOURCE, {}),
        pv.save_draft(db(version()), SALES, VID, {}),
        pv.submit(db(version()), SALES, VID, "why"),
    ):
        with pytest.raises(Forbidden):
            await call


@pytest.mark.asyncio
async def test_new_draft_copies_the_hand_prices_in_force_for_the_tariff_year_of_today() -> None:
    in_force = [
        # One version row the matrix cut where the season changes: copied back as one row.
        matrix(valid_to=date(2031, 1, 31), season_code="low"),
        matrix(position=2, valid_from=date(2031, 2, 1), season_code="peak", peak=True),
        # A row the cost sheet computed (no version): never copied into a hand price version.
        matrix(position=3, pax_min=3, pax_max=3, version_id=None, computed=True, net_amount="300", net_currency="USD"),
    ]
    conn = db(in_force=in_force, latest=OLDER, rows=[stored()])
    conn.on("WHERE v.id = $1::uuid", version())
    out = await pv.create_draft(conn, MAKER, SOURCE, {})
    insert = conn.sql("INSERT INTO booking.price_list_versions")[0]
    assert insert[1:] == (PLAN, "2031 prices", 2031, TY[0], TY[1], None, None, None, OLDER, MAKER.cis_user_id)[1:]
    rows = conn.sql(ROW_INSERT)
    assert len(rows) == 1
    # (version, position, channel, season, travel_from, travel_to (half-open), pax_min, pax_max, currency, amount)
    assert rows[0][:10] == (VID, 1, "b2b", None, TY[0], TY[1], 2, 2, "USD", D("500.00"))
    assert audited(conn) == ["price_version.created"]
    assert audit_after(conn, "price_version.created")["copy_from"] == "in_force"
    assert out["id"] == VID and out["items"][0]["amount"] == "560.00"


@pytest.mark.asyncio
async def test_new_empty_draft_for_a_given_window_and_label() -> None:
    conn = db(version())
    body = {
        "copy_from": "none",
        "travel_from": date(2031, 4, 1),
        "travel_to": date(2031, 6, 30),
        "sale_from": date(2031, 3, 1),
        "sale_to": date(2031, 3, 31),
        "label": " Spring offer ",
        "reason": " sale ",
    }
    await pv.create_draft(conn, MAKER, SOURCE, body)
    insert = conn.sql("INSERT INTO booking.price_list_versions")[0]
    assert insert == (
        PLAN,
        "Spring offer",
        2031,
        date(2031, 4, 1),
        date(2031, 7, 1),
        date(2031, 3, 1),
        date(2031, 4, 1),
        "sale",
        None,
        MAKER.cis_user_id,
    )
    assert conn.sql(ROW_INSERT) == []


@pytest.mark.asyncio
async def test_new_draft_copied_from_another_version_takes_its_hand_rows() -> None:
    other = version(id=OLDER, version_no=2, status="published")
    rows = [
        stored(),
        stored(2, pax_min=3, pax_max=3, computed=True, net_amount="300", net_currency="USD"),  # cost sheet's: left
        stored(
            3,
            pax_min=4,
            pax_max=4,
            computed=True,
            overridden=True,
            net_amount="300",
            net_currency="USD",
            amount="420.00",
        ),  # overridden by staff: copied (as typed)
    ]
    conn = db(other, rows=rows)
    await pv.create_draft(conn, MAKER, SOURCE, {"copy_from": OLDER})
    assert conn.sql("INSERT INTO booking.price_list_versions")[0][8] == OLDER  # replaces the copied version
    assert sorted((r[6], r[9]) for r in conn.sql(ROW_INSERT)) == [(2, D("560.00")), (4, D("420.00"))]
    with pytest.raises(Invalid) as err:
        await pv.create_draft(db(version(trip_plan_id=OTHER_PLAN)), MAKER, SOURCE, {"copy_from": OLDER})
    assert err.value.code == "invalid_copy_from"


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"travel_from": date(2031, 5, 1), "travel_to": date(2031, 4, 1)}, "invalid_window"),
        ({"sale_from": date(2031, 3, 5), "sale_to": date(2031, 3, 1)}, "invalid_sale_window"),
        ({"label": "x" * 81}, "label_too_long"),
        ({"reason": "x" * 301}, "reason_too_long"),
    ],
)
@pytest.mark.asyncio
async def test_draft_facts_are_checked(body: dict[str, Any], code: str) -> None:
    conn = db()
    with pytest.raises(Invalid) as err:
        await pv.create_draft(conn, MAKER, SOURCE, {"copy_from": "none", **body})
    assert err.value.code == code
    assert conn.sql("INSERT INTO booking.price_list_versions") == []


@pytest.mark.asyncio
async def test_a_missing_policy_value_is_a_clear_503() -> None:
    conn = db().on(POLICY_FRAGMENT, lambda *_: [r for r in policy_rows() if r["param_key"] != "diff_highlight_pct"])
    with pytest.raises(Unavailable) as err:
        await pv.create_draft(conn, MAKER, SOURCE, {"copy_from": "none"})
    assert err.value.code == "policy_unavailable"


def _item(**over: Any) -> dict[str, Any]:
    item = {
        "channel": "b2b",
        "season": None,
        "valid_from": date(2031, 1, 1),
        "valid_to": date(2031, 3, 31),
        "pax_min": 2,
        "pax_max": 2,
        "currency": "USD",
        "amount": D("600"),
        "peak": False,
    }
    return {**item, **over}


def _body(*items: dict[str, Any], **over: Any) -> dict[str, Any]:
    return {"travel_from": TY[0], "travel_to": date(2031, 10, 31), "items": list(items), **over}


@pytest.mark.asyncio
async def test_save_draft_takes_the_peak_from_a_configured_calendar() -> None:
    conn = db(version(), calendar=CAL, ranges=[peak_range(date(2031, 1, 1), date(2031, 2, 1))])
    body = _body(
        _item(),
        _item(valid_from=date(2031, 4, 1), valid_to=date(2031, 4, 30), peak=True),
        below_floor_reason=" agreed ",
        label="Edited",
    )
    await pv.save_draft(conn, MAKER, VID, body)
    # Row 1 starts on a peak day of the calendar; row 2 is flagged peak by hand, but the calendar says low: the
    # calendar wins once it is configured.
    assert [r[-1] for r in conn.sql(ROW_INSERT)] == [True, False]
    assert conn.sql("DELETE FROM booking.price_list_version_rows") == [(VID,)]
    update = conn.sql("UPDATE booking.price_list_versions SET label = $2")[0]
    assert update[:5] == (VID, "Edited", 2031, TY[0], TY[1])
    assert audit_after(conn, "price_version.saved")["below_floor_reason"] == "agreed"


@pytest.mark.asyncio
async def test_save_draft_keeps_the_rows_own_peak_flag_until_the_calendar_is_configured() -> None:
    # The calendar exists but has only a stop-sale: not configured, so the legacy flags stay (no price moves).
    stop = {**peak_range(date(2031, 1, 1), date(2031, 2, 1)), "kind": "stop_sale", "season_code": None}
    conn = db(version(), calendar=CAL, ranges=[stop])
    await pv.save_draft(
        conn, MAKER, VID, _body(_item(peak=True), _item(valid_from=date(2031, 4, 1), valid_to=date(2031, 4, 30)))
    )
    assert [r[-1] for r in conn.sql(ROW_INSERT)] == [True, False]


@pytest.mark.asyncio
async def test_only_the_maker_edits_their_draft_and_rows_stay_in_the_window() -> None:
    with pytest.raises(Conflict) as err:
        await pv.save_draft(db(version(status="pending")), MAKER, VID, _body())
    assert err.value.code == "not_a_draft"
    other = Staff("maker-2", None, "Invented Other", "contracting", True, None)
    with pytest.raises(Forbidden) as err:
        await pv.save_draft(db(version()), other, VID, _body())
    assert err.value.code == "not_the_maker"
    for item in (_item(valid_from=date(2030, 10, 31)), _item(valid_to=date(2031, 11, 1))):
        with pytest.raises(Invalid) as err:
            await pv.save_draft(db(version()), MAKER, VID, _body(_item(), item))
        assert err.value.code == "row_outside_window" and err.value.extra == {"row": 2}


# ── Submit: the frozen diff and impact ───────────────────────────────────────


@pytest.mark.asyncio
async def test_submit_freezes_the_diff_and_impact_with_their_sha256() -> None:
    rows = [
        stored(amount="560.00"),  # +12 % on the 500 in force: highlighted (policy diff_highlight_pct = 10)
        stored(
            2, pax_min=1, pax_max=1, amount="300.00", net_amount="285", net_currency="USD", effective_margin="0.05"
        ),  # new band, priced under the b2b floor (0.12)
    ]
    in_force = [matrix(), matrix(position=2, pax_min=3, pax_max=3, amount="450.00")]
    conn = db(
        version(rows=2, reason="Supplier increase"),
        rows=rows,
        in_force=in_force,
        quotes=25,
        departures=[date(2031, 4, 2), date(2031, 5, 7)],
    )
    await pv.submit(conn, MAKER, VID, None)
    update = conn.sql("SET status = 'pending'")[0]
    diff, effect = json.loads(update[2]), json.loads(update[3])
    assert update[1] == "Supplier increase"  # the draft's own reason when none is sent
    assert update[4] == pv.sha256(diff)
    first, second = diff["rows"]
    assert (first["old_amount"], first["change_pct"], first["highlight"]) == ("500.00", "12.0", True)
    assert (second["kind"], second["below_floor"]) == ("added", True)
    assert [r["pax_min"] for r in diff["removed"]] == [3]  # the 3-traveller price leaves the window
    assert effect == {
        "open_quotes": 25,
        "open_quote_ids": [f"q-{n}" for n in range(20)],
        "departures": 2,
        "departure_dates": ["2031-04-02", "2031-05-07"],
        "b2b_rows_changed": 2,
        "rows_removed": 1,
    }
    assert conn.sql("FROM booking.b2b_quotes q")[0] == (f"tp:{PLAN}", *TY)
    assert audit_after(conn, "price_version.submitted")["diff_sha256"] == update[4]


@pytest.mark.asyncio
async def test_submit_refusals() -> None:
    with pytest.raises(Invalid) as err:
        await pv.submit(db(version()), MAKER, VID, "  ")
    assert err.value.code == "reason_required"
    with pytest.raises(Invalid) as err:
        await pv.submit(db(version(rows=0)), MAKER, VID, "New prices")
    assert err.value.code == "no_rows"
    conn = db(version(), clash=2)
    with pytest.raises(Conflict) as err:
        await pv.submit(conn, MAKER, VID, "New prices")
    assert err.value.code == "pending_exists" and err.value.extra == {"version_no": 2}
    assert conn.sql("SET status = 'pending'") == []
    with pytest.raises(Conflict):
        await pv.submit(db(version(status="published")), MAKER, VID, "New prices")


# ── Approve, reject, withdraw ────────────────────────────────────────────────


@pytest.fixture
def rebuilt(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    async def load_pricing(conn: Any, on: date) -> str:
        return f"pricing@{on}"

    async def recompute_plan(conn: Any, plan_id: str, pricing: Any, **kw: Any) -> dict[str, Any]:
        seen.append({"plan_id": plan_id, "pricing": pricing, **kw})
        return {"outcome": "rebuilt"}

    monkeypatch.setattr(cost_sheet, "load_pricing", load_pricing)
    monkeypatch.setattr(cost_sheet, "recompute_plan", recompute_plan)
    return seen


SHA = "ab" * 32


@pytest.mark.asyncio
async def test_approve_publishes_marks_superseded_and_rebuilds_the_matrix(rebuilt: list[dict[str, Any]]) -> None:
    pub = [published(OLDER, 2, *TY, at=1), published(VID, 3, *TY, at=2)]
    conn = db(version(status="pending", diff_sha256=SHA), pub=pub)
    out = await pv.approve(conn, CHECKER, VID, f" {SHA.upper()} ")
    assert conn.sql("SET status = 'published', approved_by = $2")[0] == (VID, CHECKER.cis_user_id)
    # The newer version covers all of the older one: it is superseded (derived, d2).
    assert conn.sql("SET status = $2,") == [(OLDER, "superseded")]
    assert out["effect"] == {"states_moved": {"superseded": [OLDER], "published": []}, "matrix": "rebuilt"}
    assert rebuilt == [
        {
            "plan_id": PLAN,
            "pricing": f"pricing@{DAY}",
            "actor_type": "staff",
            "actor_ref": CHECKER.cis_user_id,
            "action": "price_version.published",
            "extra": {"price_version_id": VID, "states_moved": out["effect"]["states_moved"]},
        }
    ]
    assert audited(conn) == ["price_version.approved"]


@pytest.mark.parametrize(
    ("who", "row", "sha", "error", "code"),
    [
        (MAKER, version(status="pending", diff_sha256=SHA), SHA, Forbidden, "maker_checker"),
        (SALES, version(status="pending", diff_sha256=SHA), SHA, Forbidden, "forbidden"),
        (CHECKER, version(status="draft"), SHA, Conflict, "not_pending"),
        (CHECKER, version(status="pending", diff_sha256=SHA), "cd" * 32, Conflict, "diff_changed"),
    ],
)
@pytest.mark.asyncio
async def test_approve_refusals(
    who: Staff, row: dict[str, Any], sha: str, error: type[Exception], code: str, rebuilt: list[Any]
) -> None:
    conn = db(row)
    with pytest.raises(error) as err:
        await pv.approve(conn, who, VID, sha)
    assert err.value.code == code  # type: ignore[attr-defined]
    assert conn.sql("SET status = 'published'") == [] and rebuilt == []


@pytest.mark.asyncio
async def test_reject_needs_a_second_person_and_a_reason() -> None:
    pending = version(status="pending")
    with pytest.raises(Invalid) as err:
        await pv.reject(db(pending), CHECKER, VID, "")
    assert err.value.code == "reason_required"
    with pytest.raises(Forbidden) as err:
        await pv.reject(db(pending), MAKER, VID, "Too high")
    assert err.value.code == "maker_checker"
    with pytest.raises(Conflict):
        await pv.reject(db(version(status="rejected")), CHECKER, VID, "Too high")
    conn = db(pending)
    await pv.reject(conn, CHECKER, VID, " Too high ")
    assert conn.sql("SET status = 'rejected'") == [(VID, CHECKER.cis_user_id, "Too high")]
    assert audit_after(conn, "price_version.rejected")["reason"] == "Too high"


@pytest.mark.asyncio
async def test_maker_withdraws_their_draft_without_touching_the_matrix(rebuilt: list[Any]) -> None:
    conn = db(version(status="pending"))
    out = await pv.withdraw(conn, MAKER, VID, "Wrong tour")
    assert conn.sql("SET status = 'withdrawn'") == [(VID, MAKER.cis_user_id, "Wrong tour")]
    assert out["effect"] == {"reason": "Wrong tour"} and rebuilt == []
    with pytest.raises(Forbidden) as err:
        await pv.withdraw(db(version()), CHECKER, VID, "Not mine")
    assert err.value.code == "not_the_maker"
    with pytest.raises(Invalid):
        await pv.withdraw(db(version()), MAKER, VID, None)
    with pytest.raises(Conflict) as err:
        await pv.withdraw(db(version(status="superseded")), CHECKER, VID, "Old")
    assert err.value.code == "not_withdrawable"


@pytest.mark.asyncio
async def test_withdrawing_a_published_version_lets_the_older_one_price_again(rebuilt: list[Any]) -> None:
    window = (date(2031, 4, 1), TY[1])
    pub = [published(OLDER, 2, *TY, at=1, status="superseded"), published(VID, 3, *window, at=2)]
    row = version(status="published", travel_from=window[0], approved_by=CHECKER.cis_user_id)
    with pytest.raises(Forbidden) as err:
        await pv.withdraw(db(row, pub=list(pub)), MAKER, VID, "Mistake")
    assert err.value.code == "maker_checker"

    preview = await pv.withdraw_preview(db(row, pub=list(pub)), SALES, VID)
    restored = [{"version_id": OLDER, "version_no": 2, "from": window[0], "to": date(2031, 10, 31)}]
    assert preview == {"priced_by": restored, "unquotable": []}

    conn = db(row, pub=list(pub))
    out = await pv.withdraw(conn, CHECKER, VID, "Mistake")
    # The overlap rule runs on the versions left: the superseded older version is published again.
    assert conn.sql("SET status = $2,") == [(OLDER, "published")]
    assert out["effect"]["states_moved"] == {"superseded": [], "published": [OLDER]}
    assert out["effect"]["outcome"]["priced_by"][0]["version_id"] == OLDER
    assert rebuilt[0]["action"] == "price_version.withdrawn"


@pytest.mark.asyncio
async def test_withdraw_preview_lists_the_dates_no_version_prices_any_more() -> None:
    row = version(status="published")
    out = await pv.withdraw_preview(db(row, pub=[published(VID, 3, *TY, at=2)]), SALES, VID)
    assert out == {"priced_by": [], "unquotable": [{"from": TY[0], "to": date(2031, 10, 31)}]}


# ── Workbook import drafts ───────────────────────────────────────────────────


def _file_row(**over: Any) -> dict[str, Any]:
    return {**_item(valid_from=TY[0], valid_to=date(2031, 10, 31), amount=D("500")), **over}


@pytest.mark.asyncio
async def test_import_equal_to_the_hand_prices_in_force_writes_nothing() -> None:
    conn = db(in_force=[matrix(), matrix(position=2, version_id=None, pax_min=3, pax_max=3)])
    out = await pv.import_draft(conn, MAKER, PLAN, SOURCE, [_file_row()], "f" * 64)
    assert out == {"version_id": None, "rows": 1, "unchanged": True}
    assert conn.sql("INSERT INTO booking.price_list_versions") == []


@pytest.mark.asyncio
async def test_import_of_the_same_file_again_returns_the_open_draft() -> None:
    conn = db(imports=[{"id": OLDER, "source_sha256": "f" * 64}])
    out = await pv.import_draft(conn, MAKER, PLAN, SOURCE, [_file_row(amount=D("520"))], "f" * 64)
    assert out == {"version_id": OLDER, "rows": 1, "unchanged": True}


@pytest.mark.asyncio
async def test_a_new_file_replaces_the_older_import_draft_and_keeps_net_priced_rows() -> None:
    priced = matrix(position=2, pax_min=3, pax_max=3, net_amount="380.00", net_currency="USD", amount="450.00")
    covered = matrix(position=3, pax_min=2, pax_max=2, net_amount="400.00", net_currency="USD")  # a file row covers it
    conn = db(in_force=[priced, covered], imports=[{"id": OLDER, "source_sha256": "e" * 64}], latest=OLDER)
    out = await pv.import_draft(conn, MAKER, PLAN, SOURCE, [_file_row(amount=D("520"))], "f" * 64)
    assert out == {"version_id": VID, "rows": 2, "unchanged": False}
    assert conn.sql("SET status = 'withdrawn'") == [(OLDER, MAKER.cis_user_id)]
    insert = conn.sql("INSERT INTO booking.price_list_versions")[0]
    assert insert == (PLAN, "2031 workbook import", 2031, TY[0], TY[1], OLDER, "f" * 64, MAKER.cis_user_id)
    assert sorted((r[6], r[11]) for r in conn.sql(ROW_INSERT)) == [(2, None), (3, D("380.00"))]
    assert audited(conn) == ["price_version.imported"]


# ── Floors ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_floors_skip_a_channel_whose_rule_the_engine_refuses() -> None:
    assert await pv._floors(db(), DAY) == {"b2b": D("0.12")}


# ── The hand rows of the effective matrix ────────────────────────────────────


def _version_row(**over: Any) -> dict[str, Any]:
    """A version row as version_matrix.VERSION_ROWS_SQL reads it."""
    row = {
        **stored(travel_from=date(2031, 1, 1), travel_to=date(2031, 3, 1), currency="USD"),
        "version_id": VID,
        "created_by": MAKER.cis_user_id,
    }
    return {**row, **over}


@pytest.mark.asyncio
async def test_hand_rows_cut_by_the_calendar_or_keep_the_legacy_peak_flag() -> None:
    # No published version yet: the unversioned hand rows written before versions stay; computed rows do not.
    existing = [matrix(version_id=None), matrix(version_id=None, computed=True), matrix(version_id=OLDER)]
    assert await version_matrix.hand_rows(db(), PLAN, DAY, existing) == [matrix(version_id=None)]

    pub = [published(VID, 3, *TY, at=2)]
    conn = db(pub=pub).on("FROM booking.price_list_version_rows r JOIN", [_version_row(peak=True)])
    cal = seasons.Calendar(id=CAL, fixed=(seasons.Span(date(2031, 2, 1), date(2031, 2, 15), "festive"),))
    rows = await version_matrix.hand_rows(conn, PLAN, DAY, calendar=cal)
    assert [(r["valid_from"], r["valid_to"], r["season_code"], r["peak"]) for r in rows] == [
        (date(2031, 1, 1), date(2031, 1, 31), "low", False),
        (date(2031, 2, 1), date(2031, 2, 14), "festive", True),
        (date(2031, 2, 15), date(2031, 2, 28), "low", False),
    ]
    assert conn.sql("FROM booking.price_list_version_rows r JOIN")[0] == ([VID],)
    # The plan's calendar is not configured (read from the database): the row's own flag, one piece.
    legacy = await version_matrix.hand_rows(conn, PLAN, DAY)
    assert [(r["valid_to"], r["season_code"], r["peak"], r["created_by"]) for r in legacy] == [
        (date(2031, 2, 28), "peak", True, MAKER.cis_user_id)
    ]
