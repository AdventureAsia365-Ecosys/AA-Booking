"""AA-1153 part 1 (bk-069) without a database: the duplicate-hotel rules, external codes, the rate-sheet business checks
and diff, the board basis and hotel child prices of a quote line, the double-dinner warning, the quote hash, the
Seasons and Hotel children sheets, and the six hotel policy parameters. The HTTP flow and the issue's Done-when run on
PostgreSQL in ``tests/integration/test_aa1153_hotel_contracts.py``. All names are invented."""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal as D
from typing import Any

import pytest
from pydantic import ValidationError

from app.b2b import components, hotel_children, quotes
from app.b2b.errors import B2BError
from app.errors import Invalid
from app.pricing import policy_guides, policy_params, policy_params_hotels
from app.routers.b2b import QuoteLineRequest
from app.supply import hotel_master, rate_checks, rate_review, rate_sheet, rates
from tests.unit.fakes import POLICY_SEED, ScriptedConn

LIMITS = {
    policy_params_hotels.MATCH_BLOCK_PCT: 85,
    policy_params_hotels.MATCH_BLOCK_M: 300,
    policy_params_hotels.MATCH_WARN_PCT: 60,
    policy_params_hotels.MATCH_WARN_M: 1000,
}
ROOM = "00000000-0000-4000-8000-000000001153"
MEAL = "00000000-0000-4000-8000-000000011530"
DAY = date(2027, 6, 10)


# ── Duplicate hotels ─────────────────────────────────────────────────────────


def test_clean_name_drops_accents_punctuation_and_common_words() -> None:
    assert hotel_master.clean_name("La Siesta Classic Hotel – Ma May") == "la siesta classic ma may"
    assert hotel_master.clean_name("Khách sạn Đông Á & Spa") == "khach san dong a"
    assert hotel_master.clean_name(None) == ""


def test_similarity_and_distance() -> None:
    assert hotel_master.similarity("lotus bay lodge", "lotus bay lodge") == 100
    assert hotel_master.similarity("lotus bay lodge", "") == 0
    assert 60 <= hotel_master.similarity("lotus bay lodge", "lotus bay garden") < 85
    metres = hotel_master.distance_m((D("21.0330"), D("105.8500")), (D("21.0331"), D("105.8501")))
    assert metres is not None and 10 < metres < 20
    assert hotel_master.distance_m((None, None), (1, 2)) is None


@pytest.mark.parametrize(
    ("sim", "metres", "same_place", "expected"),
    [
        (95, 20.0, False, "block"),
        (95, None, True, "block"),  # no position on one side: the same place stands in for the distance
        (95, 2000.0, False, None),  # far apart: a chain's other branch
        (70, 500.0, False, "warn"),
        (70, None, True, "warn"),
        (40, 50.0, True, None),
        (95, 600.0, False, "warn"),
    ],
)
def test_verdict(sim: int, metres: float | None, same_place: bool, expected: str | None) -> None:
    assert hotel_master.verdict(sim, metres, same_place, LIMITS) == expected


def test_external_codes() -> None:
    assert hotel_master.clean_codes({"Lantern": " LB-1 ", "giata": None}) == {"lantern": "LB-1"}
    assert hotel_master.clean_codes(None) == {}
    for bad in ([1], {"x y": "1"}, {"ok": "z" * 65}):
        with pytest.raises(Invalid):
            hotel_master.clean_codes(bad)
    assert hotel_master.reconfirm_due(date(2027, 1, 1), 180) == date(2027, 6, 30)
    assert hotel_master.reconfirm_due(None, 180) is None


# ── Business checks and diff ─────────────────────────────────────────────────


def _rate(board: str | None, net: str, **kw: Any) -> dict[str, Any]:
    return {
        "hotel_room_type_id": ROOM,
        "meal_plan": board,
        "net_amount": D(net),
        "valid_from": date(2027, 1, 1),
        "valid_to": date(2027, 12, 31),
        "season": "All year",
        **kw,
    }


def test_board_order_flags_a_board_with_more_meals_that_costs_less() -> None:
    found = rate_checks.board_order([_rate("BB", "60"), _rate("HB", "55", row=3), _rate("FB", "70")], {ROOM: "Deluxe"})
    assert [(f["code"], f["severity"], f["row"]) for f in found] == [("board_order", "warning", 3)]
    assert "HB 55 is below BB 60" in found[0]["message"]
    assert rate_checks.board_order([_rate("BB", "60"), _rate("HB", "75")], {}) == []


def test_seasons_overlap_is_an_error_and_a_gap_a_warning() -> None:
    seasons = [
        {"name": "Low", "date_from": date(2027, 1, 1), "date_to": date(2027, 4, 30)},
        {"name": "High", "date_from": date(2027, 4, 20), "date_to": date(2027, 8, 31)},
        {"name": "Low", "date_from": date(2027, 10, 1), "date_to": date(2027, 12, 31)},
    ]
    found = rate_checks.season_findings(seasons, date(2027, 1, 1), date(2027, 12, 31))
    assert [(f["code"], f["severity"]) for f in found] == [("season_overlap", "error"), ("season_gap", "warning")]
    assert (found[1]["date_from"], found[1]["date_to"]) == ("2027-09-01", "2027-09-30")
    # One season of two ranges, end to end, covers the year: nothing to say.
    year = [{"name": "All", "date_from": date(2027, 1, 1), "date_to": date(2027, 6, 30)},
            {"name": "All", "date_from": date(2027, 7, 1), "date_to": date(2027, 12, 31)}]  # fmt: skip
    assert rate_checks.season_findings(year, date(2027, 1, 1), date(2027, 12, 31)) == []
    assert rate_checks.season_findings([], date(2027, 1, 1), None) == []


def test_price_jump_and_diff_against_the_previous_contract() -> None:
    before = [_rate("HB", "56"), _rate("BB", "48"), _rate("RO", "40")]
    after = [_rate("HB", "96"), _rate("BB", "50"), _rate("FB", "80")]
    jumps = rate_checks.price_jumps(after, before, D("60"), {ROOM: "Deluxe"})
    assert [(j["old_net"], j["new_net"], j["change_pct"]) for j in jumps] == [("56", "96", "71.4")]
    diff = rate_checks.diff(after, before, {ROOM: "Deluxe"})
    assert [(c["meal_plan"], c["old_net"], c["new_net"]) for c in diff["changed"]] == [
        ("HB", "56", "96"),
        ("BB", "48", "50"),
    ]
    assert [a["meal_plan"] for a in diff["added"]] == ["FB"] and [r["meal_plan"] for r in diff["removed"]] == ["RO"]
    # Without a season, the same day-and-month range of another year is the same rate.
    plain = {**_rate("BB", "48"), "season": None, "valid_from": date(2026, 1, 1), "valid_to": date(2026, 12, 31)}
    assert rate_checks.rate_key(plain) == rate_checks.rate_key({**_rate("BB", "1"), "season": None})
    findings = rate_checks.run(after, previous=before, jump_pct=D("60"))
    assert rate_checks.errors(findings) == []  # FB 80 below HB 96 and the HB jump: two warnings
    assert sorted(f["code"] for f in rate_checks.warnings(findings)) == ["board_order", "price_jump"]


# ── Board basis, hotel child prices, double dinner ───────────────────────────


def _ask(**kw: Any) -> components.LineAsk:
    base: dict[str, Any] = {"line_ref": "L", "component_id": f"rt:{ROOM}", "service_date": DAY, "nights": 2,
                            "adult": 2, "child": 0, "infant": 0}  # fmt: skip
    return components.LineAsk(**{**base, **kw})


def test_extra_children_are_the_ones_the_rooms_do_not_hold() -> None:
    two = [(2, 1)]
    assert components.extra_children(_ask(child=1, child_ages=(7,)), two) == (7,)
    assert components.extra_children(_ask(child=2, child_ages=(4, 9)), two) == (9, 4)
    assert components.extra_children(_ask(child=2, child_ages=(4,)), two) == (None, 4)  # unknown age first
    assert components.extra_children(_ask(child=1), [(2, 1), (1, 1)]) == ()


def test_child_policy_takes_the_board_first_then_any_board() -> None:
    rows = [
        {"meal_plan": None, "age_min": 0, "age_max": 5, "net_amount": "0"},
        {"meal_plan": None, "age_min": 6, "age_max": 11, "net_amount": "10"},
        {"meal_plan": "HB", "age_min": 6, "age_max": 11, "net_amount": "15"},
    ]
    assert hotel_children.child_policy(rows, 7, "HB")["net_amount"] == "15"
    assert hotel_children.child_policy(rows, 7, "BB")["net_amount"] == "10"
    assert hotel_children.child_policy(rows, 3, "HB")["net_amount"] == "0"  # no HB band for 3: any board
    assert hotel_children.child_policy(rows, None, None)["age_max"] == 11  # unknown age: the oldest band
    assert hotel_children.child_policy(rows, 14, "HB") is None


def _room_conn(board: str, *, policy: list[dict[str, Any]], meal_service: bool = False) -> ScriptedConn:
    def rates_answer(*args: Any) -> list[dict[str, Any]]:
        out = []
        for n, room in enumerate(args[0], start=1):
            if room:
                out.append({"n": n, "id": f"r-{board}", "unit": "room_night", "net_amount": "56.00", "currency": "USD",
                            "season": None, "contract_id": "k-b", "age_max": None, "supplier_id": "sup-b",
                            "meal_plan": board})  # fmt: skip
            else:
                out.append({"n": n, "id": "r-meal", "unit": "group", "net_amount": "20.00", "currency": "USD",
                            "season": None, "contract_id": "k-m", "age_max": None, "supplier_id": "sup-m",
                            "meal_plan": None})  # fmt: skip
        return out

    conn = ScriptedConn()
    conn.on(
        "FROM booking.hotel_room_types rt JOIN booking.hotels h",
        [
            {
                "id": ROOM,
                "status": "active",
                "max_occupancy": 2,
                "hotel_status": "active",
                "supplier_id": "sup-a",
                "hotel_id": "h-1",
            }
        ],  # fmt: skip
    )
    if meal_service:
        conn.on("FROM booking.services WHERE id = ANY",
                [{"id": MEAL, "status": "active", "service_type": "meal", "supplier_id": "sup-m"}])  # fmt: skip
    conn.on("WITH ORDINALITY", rates_answer)
    conn.on("FROM booking.hotel_child_policies", policy)
    return conn


@pytest.mark.asyncio
async def test_a_room_line_asks_its_board_and_charges_the_child_the_room_does_not_hold() -> None:
    policy = [{"contract_id": "k-b", "hotel_id": "h-1", "age_min": 6, "age_max": 11, "meal_plan": "HB",
               "net_amount": "15.00"}]  # fmt: skip
    conn = _room_conn("HB", policy=policy)
    ask = _ask(nights=3, child=1, child_ages=(7,), meal_plan="HB", rooms=(components.RoomAsk("double", 1),))
    [cost] = await components.price_lines(conn, [ask])
    assert sum(a for a, _ in cost.costs) == D("213.00")  # 3 x 56 + 3 x 15
    # A double with max_occupancy 2 has no free place: the child is on an extra bed (no extra-bed price here: the
    # sharing price is taken).
    assert cost.child_room == {"children": 1, "priced": 1, "unpriced": 0, "extra_bed": 1}
    assert cost.board == {"meal_plan": "HB"}
    [args] = conn.sql("WITH ORDINALITY")
    assert set(args[5]) == {"HB"}  # every night asks the line's board
    # No child price for the age: no extra, said in the note.
    [cost] = await components.price_lines(_room_conn("HB", policy=[]), [ask])
    assert sum(a for a, _ in cost.costs) == D("168.00") and cost.child_room["unpriced"] == 1


@pytest.mark.asyncio
async def test_half_board_with_a_meal_service_that_night_warns_of_a_double_dinner() -> None:
    conn = _room_conn("HB", policy=[], meal_service=True)
    meal = components.LineAsk("M", f"sv:{MEAL}", DAY, None, 2, 0, 0)
    room, _ = await components.price_lines(conn, [_ask(meal_plan="HB"), meal])
    assert room.board == {
        "meal_plan": "HB",
        "warnings": [{"code": "double_dinner", "date": DAY.isoformat(), "meal_plan": "HB"}],
    }
    plain, _ = await components.price_lines(_room_conn("BB", policy=[], meal_service=True), [_ask(), meal])
    assert plain.board is None  # BB, no board asked: nothing to say


def test_the_board_and_ages_are_hashed_only_when_sent() -> None:
    assert quotes._board_hash({"meal_plan": None, "child_ages": []}) == {}
    assert quotes._board_hash({"meal_plan": "HB", "child_ages": [7]}) == {"meal_plan": "HB", "child_ages": [7]}


def test_request_line_ages_match_the_children() -> None:
    body = {
        "line_ref": "L",
        "component_id": f"rt:{ROOM}",
        "service_date": "2027-06-10",
        "pax": {"adult": 2, "child": 1},
        "meal_plan": "HB",
        "child_ages": [7],
    }
    ask = components.ask_from_request(QuoteLineRequest(**body))
    assert (ask.meal_plan, ask.child_ages) == ("HB", (7,))
    with pytest.raises(ValidationError):
        QuoteLineRequest(**{**body, "child_ages": [7, 9]})
    with pytest.raises(ValidationError):
        QuoteLineRequest(**{**body, "meal_plan": "XB"})


# ── Seasons and Hotel children sheets ────────────────────────────────────────


def test_a_season_row_becomes_one_rate_per_range_and_children_are_read() -> None:
    data = rate_sheet.build_workbook(
        {"Title": "Harbour 2027", "Valid from": "2027-01-01", "Valid to": "2027-12-31", "Currency": "USD"},
        [
            ("HB-LOTUS", "Deluxe", "High", None, None, "HB", None, None, "70", None),
            ("HB-LOTUS", "Deluxe", "Low", "2027-05-01", "2027-05-31", "HB", None, None, "50", None),
            ("HB-LOTUS", "Deluxe", "Peak", None, None, "HB", None, None, "90", None),
        ],
        season_rows=[("High", "2027-01-01", "2027-03-31"), ("High", "2027-10-01", "2027-12-31")],
        child_rows=[("HB-LOTUS", 6, 11, "extra bed", "hb", "20"), ("HB-LOTUS", 12, 11, "sharing", None, "5")],
    )
    sheet = rate_sheet.parse(data)
    high = [r.values for r in sheet.rows if r.values.get("season") == "High"]
    assert [(v["valid_from"], v["valid_to"]) for v in high] == [
        (date(2027, 1, 1), date(2027, 3, 31)),
        (date(2027, 10, 1), date(2027, 12, 31)),
    ]
    assert any(r.values.get("season") == "Low" and r.values["valid_from"] == date(2027, 5, 1) for r in sheet.rows)
    assert {(p.sheet, p.code) for p in sheet.problems} == {("Hotel", "unknown_season"), ("Hotel children",
                                                            "invalid_child_price")}  # fmt: skip
    assert sheet.children == [{"hotel_code": "HB-LOTUS", "age_min": 6, "age_max": 11, "basis": "extra_bed",
                               "meal_plan": "HB", "net_amount": D("20"), "row": 2}]  # fmt: skip


# ── Policy parameters ────────────────────────────────────────────────────────


def test_the_seven_hotel_parameters_are_registered_with_their_guides_and_seed() -> None:
    keys = {s["key"] for s in policy_params_hotels.SPECS}
    assert keys == {*policy_params_hotels.MATCH_KEYS, policy_params_hotels.FACTS_RECONFIRM_DAYS,
                    policy_params_hotels.JUMP_WARN_PCT, policy_params_hotels.DEFAULT_BOARD}  # fmt: skip
    board = policy_params.PARAMS[policy_params_hotels.DEFAULT_BOARD]
    assert (board.section, board.group, board.type, board.used_by) == ("hotels", "pricing", "code", "app.supply.rates")
    assert board.choices == policy_params_hotels.BOARDS == rates.MEAL_PLANS
    assert policy_params.normalise(board.key, "HB") == "HB"
    for bad in ("XX", ["BB"], None, "bb"):
        with pytest.raises(policy_params.PolicyValueError):
            policy_params.normalise(board.key, bad)
    assert policy_guides.guide(board.key) is not None and json.loads(POLICY_SEED[board.key]) == "BB"
    for key in keys - {board.key}:
        param = policy_params.PARAMS[key]
        assert (param.section, param.group, param.type) == ("hotels", "pricing", "integer")
        assert policy_guides.guide(key) is not None and key in POLICY_SEED
        policy_params.normalise(key, int(POLICY_SEED[key]))  # the seeded value is inside the range
    assert "hotels" in policy_params.SECTIONS


# ── Review fixes: child basis, one DMC per room, the policy board, bounded sheets ───────────────────────────────


def test_child_policy_takes_the_basis_first_then_the_other_basis() -> None:
    rows = [
        {"meal_plan": None, "basis": "sharing", "age_min": 6, "age_max": 11, "net_amount": "10"},
        {"meal_plan": None, "basis": "extra_bed", "age_min": 6, "age_max": 11, "net_amount": "25"},
        {"meal_plan": None, "basis": "extra_bed", "age_min": 12, "age_max": 17, "net_amount": "30"},
    ]
    assert hotel_children.child_policy(rows, 7, None, "sharing")["net_amount"] == "10"
    assert hotel_children.child_policy(rows, 7, "HB", "extra_bed")["net_amount"] == "25"
    assert hotel_children.child_policy(rows, 14, None, "sharing")["net_amount"] == "30"  # no sharing band: extra bed
    assert hotel_children.child_policy(rows, None, None, "extra_bed")["age_max"] == 17


def test_children_share_the_free_places_and_the_oldest_take_extra_beds() -> None:
    assert hotel_children.child_bases(0, [(2, 1)], 3) == ()
    assert hotel_children.child_bases(2, [(2, 1)], 3) == ("extra_bed", "sharing")
    assert hotel_children.child_bases(2, [(2, 2)], 3) == ("sharing", "sharing")
    assert hotel_children.child_bases(1, [(2, 1)], 2) == ("extra_bed",)
    assert hotel_children.child_bases(2, [(2, 1)], None) == ("sharing", "sharing")  # no maximum known


def _two_dmc_conn(*, pinned_has_rate: bool = True) -> ScriptedConn:
    """Night 1 is cheapest at DMC A (60), night 2 at DMC B (40); A sells night 2 at 65 when asked pinned."""

    def answer(*args: Any) -> list[dict[str, Any]]:
        out = []
        for n, (on, supplier) in enumerate(zip(args[2], args[6], strict=True), start=1):
            if supplier == "sup-a":
                if pinned_has_rate:
                    out.append({"n": n, "id": "r-a2", "unit": "room_night", "net_amount": "65.00", "currency": "USD",
                                "season": None, "contract_id": "k-a", "age_max": None, "supplier_id": "sup-a",
                                "meal_plan": "BB"})  # fmt: skip
                continue
            first = on == DAY
            out.append({"n": n, "id": "r-a1" if first else "r-b2", "unit": "room_night",
                        "net_amount": "60.00" if first else "40.00", "currency": "USD", "season": None,
                        "contract_id": "k-a" if first else "k-b", "age_max": None,
                        "supplier_id": "sup-a" if first else "sup-b", "meal_plan": "BB"})  # fmt: skip
        return out

    conn = _room_conn("BB", policy=[])
    conn.on("WITH ORDINALITY", answer)
    return conn


@pytest.mark.asyncio
async def test_a_room_line_keeps_the_dmc_of_its_first_night() -> None:
    conn = _two_dmc_conn()
    [cost] = await components.price_lines(conn, [_ask(nights=2)], default_board="BB")
    assert cost.rate_ids == ["r-a1", "r-a2"]  # never DMC B's 40 on night 2
    assert sum(a for a, _ in cost.costs) == D("125.00")
    first, again = conn.sql("WITH ORDINALITY")
    assert list(again[6]) == ["sup-a"] and list(again[2]) == [DAY + timedelta(days=1)]
    with pytest.raises(B2BError, match="from the DMC that prices the first night"):
        await components.price_lines(_two_dmc_conn(pinned_has_rate=False), [_ask(nights=2)], default_board="BB")


@pytest.mark.asyncio
async def test_the_default_board_comes_from_the_policy_unless_the_quote_passes_it() -> None:
    conn = _room_conn("BB", policy=[])
    await components.price_lines(conn, [_ask()])
    [args] = conn.sql("WITH ORDINALITY")
    assert args[7] == "BB"  # the seeded hotel_default_board
    conn = _room_conn("HB", policy=[])
    await components.price_lines(conn, [_ask()], default_board="HB")
    [args] = conn.sql("WITH ORDINALITY")
    assert args[7] == "HB" and set(args[5]) == {None}  # the line asks nothing; the default board ranks first


def test_rate_choice_sorts_across_dmcs_in_usd() -> None:
    sql = " ".join(rates.RATES_MANY_SQL.split())
    assert "FROM booking.fx_rates f JOIN booking.policy_approvals p" in sql
    assert "s.net_sort / fx.rate_per_usd END NULLS LAST" in sql
    assert "'BB'" not in sql and "coalesce(a.meal_plan, $8::text)" in sql


def test_seasons_and_child_rows_count_against_the_row_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rate_sheet, "MAX_ROWS", 4)
    contract = {"Title": "Harbour 2027", "Valid from": "2027-01-01", "Valid to": "2027-12-31", "Currency": "USD"}
    rate = [("HB-LOTUS", "Deluxe", None, "2027-01-01", "2027-12-31", "BB", None, None, "50", None)]
    seasons = [(f"S{i}", f"2027-0{i}-01", f"2027-0{i}-02") for i in range(1, 6)]
    with pytest.raises(Invalid) as err:
        rate_sheet.parse(rate_sheet.build_workbook(contract, rate, season_rows=seasons))
    assert err.value.code == "rate_sheet_too_many_rows"
    children = [("HB-LOTUS", i, i, "sharing", None, "5") for i in range(5)]
    with pytest.raises(Invalid) as err:
        rate_sheet.parse(rate_sheet.build_workbook(contract, rate, child_rows=children))
    assert err.value.code == "rate_sheet_too_many_rows"


def test_a_season_label_expanded_past_the_limit_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rate_sheet, "MAX_ROWS", 6)
    contract = {"Title": "Harbour 2027", "Valid from": "2027-01-01", "Valid to": "2027-12-31", "Currency": "USD"}
    rows = [("HB-LOTUS", "Deluxe", "High", None, None, "BB", None, None, "50", None)] * 2
    seasons = [("High", f"2027-0{i}-01", f"2027-0{i}-02") for i in range(1, 4)]  # 2 rows x 3 ranges = 6: fine
    assert len(rate_sheet.parse(rate_sheet.build_workbook(contract, rows, season_rows=seasons)).rows) == 6
    seasons.append(("High", "2027-05-01", "2027-05-02"))  # 2 x 4 = 8 > 6 (and 2 + 4 rows read is within 6)
    with pytest.raises(Invalid) as err:
        rate_sheet.parse(rate_sheet.build_workbook(contract, rows, season_rows=seasons))
    assert err.value.code == "rate_sheet_too_many_rows"


def test_a_child_price_twice_for_one_basis_is_a_row_problem_both_bases_are_fine() -> None:
    contract = {"Title": "Harbour 2027", "Valid from": "2027-01-01", "Valid to": "2027-12-31", "Currency": "USD"}
    rate = [("HB-LOTUS", "Deluxe", None, "2027-01-01", "2027-12-31", "BB", None, None, "50", None)]
    children = [
        ("HB-LOTUS", 6, 11, "sharing", None, "10"),
        ("HB-LOTUS", 6, 11, "extra bed", None, "25"),
        ("HB-LOTUS", 6, 11, "extra bed", None, "30"),
    ]
    sheet = rate_sheet.parse(rate_sheet.build_workbook(contract, rate, child_rows=children))
    assert [(c["basis"], c["net_amount"]) for c in sheet.children] == [("sharing", D("10")), ("extra_bed", D("25"))]
    assert [(p.sheet, p.row, p.code) for p in sheet.problems] == [("Hotel children", 4, "duplicate_child_price")]


def test_overlapping_seasons_are_found_in_one_sweep() -> None:
    def season(name: str, start: date, days: int, row: int) -> dict[str, Any]:
        return {
            "name": name,
            "date_from": start,
            "date_to": start + timedelta(days=days),
            "sheet": "Seasons",
            "row": row,
        }

    found = rate_checks.season_findings(
        [
            season("Low", date(2027, 1, 1), 30, 2),
            season("High", date(2027, 1, 20), 30, 3),
            season("Low", date(2027, 1, 25), 3, 4),
        ],  # fmt: skip
        date(2027, 1, 1),
        date(2027, 2, 19),
    )
    assert [(f["code"], f.get("row")) for f in found] == [("season_overlap", 3), ("season_overlap", 4)]
    many = [season(f"S{i}", date(2027, 1, 1), 300, i) for i in range(1500)]  # all overlap: one error each, fast
    assert sum(f["code"] == "season_overlap" for f in rate_checks.season_findings(many, None, None)) == 1499


@pytest.mark.asyncio
async def test_a_refused_child_price_names_its_sheet_and_row() -> None:
    import asyncpg

    conn = ScriptedConn().on("INSERT INTO booking.hotel_child_policies", asyncpg.UniqueViolationError("duplicate"))
    staff = type("S", (), {"cis_user_id": "it-1153"})()
    child = {"hotel_id": "h-1", "age_min": 6, "age_max": 11, "basis": "sharing", "meal_plan": None,
             "net_amount": D("10"), "row": 7}  # fmt: skip
    with pytest.raises(Invalid) as err:
        await rate_review.write_parts(conn, staff, "k-1", [], [child], [])  # type: ignore[arg-type]
    assert err.value.extra["errors"][0] | {"message": ""} == {
        "sheet": "Hotel children", "row": 7, "column": None, "code": "invalid_value", "message": ""}  # fmt: skip


def test_a_workbook_too_large_once_unpacked_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    data = rate_sheet.build_workbook()
    monkeypatch.setattr(rate_sheet, "MAX_UNPACKED_BYTES", 1024)
    with pytest.raises(Invalid) as err:
        rate_sheet.parse(data)
    assert err.value.code == "rate_sheet_too_large"
