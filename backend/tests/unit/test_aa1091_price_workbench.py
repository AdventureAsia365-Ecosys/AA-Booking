"""AA-1091 price setup workbench without a database: the next step and the checks of a plan, the provisional price
rules, the filters and summary, the bulk actions with a scripted connection, the component suggestions (text
normalising, keywords, the explained scores, covered and unmatched needs) and the structured CIS reference.
Invented tours, places, hotels and codes only."""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D
from typing import Any

import asyncpg
import pytest

from app.auth.staff import Staff
from app.catalog import component_suggest as cs
from app.catalog import cost_sheet, liveness, price_readiness
from app.catalog import price_workbench as pw
from app.catalog import trip_plan_sync as sync
from app.errors import Invalid, NotFound
from tests.unit.fakes import VIEW_READY_FRAGMENT, ScriptedConn

ON = date(2027, 3, 1)
KEY = "aa109100-0000-4000-8000-000000000001"
OTHER = "aa109100-0000-4000-8000-000000000002"
STAFF = Staff("it-1091", None, "it-1091", "contracting", True, "reviewer")


def _row(**over: Any) -> dict[str, Any]:
    row = {
        "tour_key": KEY,
        "title": "Invented Ridge Trek",
        "country": "Vietnam",
        "duration_days": 3,
        "in_catalog": True,
        "sellable": False,
        "status": "no_components",
        "reason": "no_components",
        "components": 0,
        "components_without_rate": [],
        "missing_fx": [],
        "computed_rows": 0,
        "hand_rows": 0,
        "hand_rows_expired": 0,
        "from_price": None,
        "pending_recompute": False,
        "note": None,
    }
    return {**row, **over}


# ── Next step and checks ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("over", "step"),
    [
        ({}, "add_components"),
        ({"status": "hand_priced", "hand_rows": 2}, "add_components"),  # hand prices alone are never quotable
        ({"status": "inactive_component", "reason": "inactive_component", "components": 2}, "fix_inactive"),
        ({"status": "missing_rates", "reason": "missing_rates", "components": 2}, "load_rates"),
        ({"status": "missing_fx", "reason": "missing_fx", "components": 2, "missing_fx": ["THB"]}, "approve_fx"),
        ({"status": "no_rule", "reason": "no_rule", "components": 2}, "approve_rule"),
        ({"status": "priced", "reason": None, "components": 2, "pending_recompute": True}, "recompute"),
        ({"status": "missing_rates", "reason": None, "components": 2}, "load_rates"),  # no common window
        ({"status": "priced", "reason": None, "components": 2, "computed_rows": 6}, "make_sellable"),
        ({"status": "priced", "reason": None, "components": 2, "computed_rows": 6, "sellable": True}, "done"),
    ],
)
def test_next_step(over: dict[str, Any], step: str) -> None:
    assert pw.next_step(_row(**over)) == step
    assert step in pw.NEXT_STEPS


def test_checks_and_the_price_kinds() -> None:
    extra = pw.Extra(departures=2, next_departure=date(2027, 4, 2))
    priced = pw.checks(_row(status="priced", reason=None, components=3, computed_rows=4), extra, has_rule=True)
    assert priced["price"] == {"ok": True, "kind": "rates"}
    assert priced["rates"] == {"ok": True, "rated": 3, "total": 3}
    assert priced["departures"] == {"ok": True, "count": 2, "next": date(2027, 4, 2)}
    unrated = [{"day_no": 1, "kind": "hotel", "code": "HTL-1", "room_type": "Twin", "supplier_code": "SUP-1"}]
    row = _row(status="missing_rates", reason="missing_rates", components=3, components_without_rate=unrated)
    missing = pw.checks(row, pw.Extra(), has_rule=False)
    assert missing["rates"] == {"ok": False, "rated": 2, "total": 3}
    assert pw.missing(missing) == ["rates", "rule", "price", "sellable", "departures"]
    hand = pw.checks(_row(hand_rows=1), pw.Extra(), has_rule=True)
    assert hand["price"] == {"ok": True, "kind": "hand"} and hand["components"]["ok"] is False
    provisional = pw.Extra(hint_amount=D("610"), hint_currency="USD", hint_basis="per_person")
    assert pw.checks(_row(), provisional, has_rule=True)["price"] == {"ok": False, "kind": "provisional"}
    inactive = pw.checks(_row(reason="inactive_component", components=1), pw.Extra(), has_rule=True)
    assert inactive["components"]["ok"] is False


def test_provisional_rules() -> None:
    ref = {"reference_raw": "from USD 610", "reference_amount": D("610"), "reference_currency": "USD"}
    shown = pw.Extra(**ref, hint_amount=D("610.00"), hint_currency="USD", hint_basis="per_person")
    assert pw.is_provisional(shown) and pw.hint_is_reference(shown)
    assert not pw.is_provisional(pw.Extra(**ref, hint_amount=D("610"), hint_basis="per_person", b2b_rows=1))
    assert not pw.is_provisional(pw.Extra(hint_amount=D("610"), hint_basis="per_group"))
    assert not pw.hint_is_reference(pw.Extra(**ref, hint_amount=D("600"), hint_currency="USD", hint_basis="per_person"))
    row = pw.workbench_row(_row(), shown, has_rule=True)
    assert row["provisional"] == {"amount": "610.00", "currency": "USD", "from_reference": True}
    assert row["reference"] == {"raw": "from USD 610", "amount": "610.00", "currency": "USD", "reviewed": False}
    assert row["supplier_price"] is False and row["next_step"] == "add_components"
    unparsed = pw.workbench_row(_row(), pw.Extra(reference_raw="on request"), has_rule=True)
    assert unparsed["reference"] == {"raw": "on request", "amount": None, "currency": None, "reviewed": False}
    assert pw.workbench_row(_row(), pw.Extra(), has_rule=True)["reference"] is None


def test_filters_and_summary() -> None:
    a = pw.workbench_row(_row(), pw.Extra(), has_rule=True)
    b = pw.workbench_row(
        _row(
            tour_key=OTHER,
            title="Invented Lake Loop",
            country="Laos",
            status="priced",
            reason=None,
            components=2,
            computed_rows=3,
            sellable=True,
        ),
        pw.Extra(hint_amount=D("1"), hint_currency="USD", hint_basis="per_person"),
        has_rule=True,
    )
    assert pw.matches(a, status=None, country=None, q=None, missing_check="components", step=None)
    assert not pw.matches(b, status=None, country=None, q=None, missing_check="components", step=None)
    assert pw.matches(b, status="priced", country="laos", q="lake", missing_check=None, step="done")
    assert not pw.matches(b, status=None, country=None, q=None, missing_check=None, step="add_components")
    assert not pw.matches(a, status="priced", country=None, q=None, missing_check=None, step=None)
    out = pw.summary([a, b])
    assert out["total"] == 2 and out["by_next_step"]["done"] == 1 and out["by_next_step"]["add_components"] == 1
    assert out["by_missing"]["components"] == 1 and out["by_missing"]["departures"] == 2
    assert out["provisional"] == 1  # b's hint with no b2b price row
    assert (
        pw.summary(
            [
                pw.workbench_row(
                    _row(), pw.Extra(hint_amount=D("5"), hint_basis="per_person", hint_currency="USD"), has_rule=True
                )
            ]
        )["provisional"]
        == 1
    )


def test_check_request() -> None:
    assert pw.check_request("make_sellable", [" a ", "a", "b", ""]) == ["a", "b"]
    with pytest.raises(Invalid, match="action"):
        pw.check_request("delete_everything", ["a"])
    with pytest.raises(Invalid) as exc:
        pw.check_request("make_sellable", [" "])
    assert exc.value.code == "no_plans"
    with pytest.raises(Invalid) as exc:
        pw.check_request("recompute", [str(i) for i in range(pw.MAX_RECOMPUTE + 1)])
    assert exc.value.code == "too_many_plans"


@pytest.mark.asyncio
async def test_build_joins_the_readiness_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = price_readiness.Plan("p-1", KEY, False)
    facts = price_readiness.Facts(ON, cost_sheet.Pricing(ON, {}, {}), [plan], {}, [])

    async def load(conn: Any, on: date, plan_ids: Any = None) -> Any:
        return facts

    async def resolve(conn: Any, f: Any, rated: Any) -> list[dict[str, Any]]:
        return [_row()]

    monkeypatch.setattr(price_readiness, "load", load)
    monkeypatch.setattr(price_readiness, "resolve", resolve)
    conn = ScriptedConn().on(
        "FROM booking.trip_plans p LEFT JOIN (SELECT trip_plan_id, count(*) AS n, min(start_date)",
        [
            {
                "plan_id": "p-1",
                "reference_price_raw": None,
                "reference_amount": None,
                "reference_currency": None,
                "reference_reviewed": False,
                "price_hint_amount": None,
                "price_hint_currency": None,
                "price_hint_basis": None,
                "departures": 1,
                "next_start": date(2027, 5, 1),
                "b2b_rows": 0,
            }
        ],
    )
    out = await pw.build(conn, on=ON, missing_check="rule")  # type: ignore[arg-type]
    assert out["rules"] == [] and [r["tour_key"] for r in out["items"]] == [KEY]
    assert out["items"][0]["checks"]["departures"]["count"] == 1
    assert out["summary"]["by_missing"]["rule"] == 1


# ── Bulk actions ─────────────────────────────────────────────────────────────


def _plans(*rows: dict[str, Any]) -> ScriptedConn:
    return ScriptedConn().on("FOR UPDATE OF p", list(rows))


def _plan(key: str = KEY, *, sellable: bool = False, components: int = 2) -> dict[str, Any]:
    return {"id": f"id-{key[-1]}", "source_id": key, "sellable": sellable, "components": components}


@pytest.mark.asyncio
async def test_make_sellable_and_not() -> None:
    conn = _plans(_plan(), _plan(OTHER, components=0)).on(
        "SELECT DISTINCT source_id FROM catalog.v_tour", [{"source_id": KEY}]
    )
    out = await pw.apply_action(conn, STAFF, "make_sellable", [KEY, OTHER, "gone"], on=ON)  # type: ignore[arg-type]
    assert [(r["outcome"], r["reason"]) for r in out["results"]] == [
        ("done", None),
        ("skipped", "no_components"),
        ("skipped", "no_trip_plan"),
    ]
    assert (out["done"], out["skipped"]) == (1, 2)
    assert conn.sql("UPDATE booking.trip_plans SET sellable") == [("id-1", True, "it-1091")]
    assert any("trip_plan.updated" in a for a in map(str, conn.sql("INSERT INTO booking.audit_logs")))

    out_of = _plans(_plan()).on("SELECT DISTINCT source_id FROM catalog.v_tour", [])
    out = await pw.apply_action(out_of, STAFF, "make_sellable", [KEY], on=ON)  # type: ignore[arg-type]
    assert out["results"][0]["reason"] == "not_in_catalog"
    broken = _plans(_plan()).on("SELECT DISTINCT source_id FROM catalog.v_tour", asyncpg.UndefinedTableError("x"))
    out = await pw.apply_action(broken, STAFF, "make_sellable", [KEY], on=ON)  # type: ignore[arg-type]
    assert out["results"][0]["reason"] == "catalog_unreadable"

    off = _plans(_plan(sellable=True), _plan(OTHER))
    out = await pw.apply_action(off, STAFF, "make_not_sellable", [KEY, OTHER], on=ON)  # type: ignore[arg-type]
    assert [r["outcome"] for r in out["results"]] == ["done", "unchanged"]


def _extra_row(plan_id: str, **over: Any) -> dict[str, Any]:
    row = {
        "plan_id": plan_id,
        "reference_price_raw": "from USD 610",
        "reference_amount": D("610"),
        "reference_currency": "USD",
        "reference_reviewed": False,
        "price_hint_amount": None,
        "price_hint_currency": None,
        "price_hint_basis": None,
        "departures": 0,
        "next_start": None,
        "b2b_rows": 0,
    }
    return {**row, **over}


@pytest.mark.asyncio
async def test_provisional_actions() -> None:
    keys = [f"aa109100-0000-4000-8000-00000000000{i}" for i in range(1, 6)]
    conn = _plans(*(_plan(k) for k in keys)).on(
        "min(start_date) AS next_start",
        [
            _extra_row("id-1"),
            _extra_row("id-2", reference_amount=None, reference_currency=None),
            _extra_row("id-3", b2b_rows=2),
            _extra_row("id-4", price_hint_amount=D("610.00"), price_hint_currency="USD", price_hint_basis="per_person"),
            _extra_row("id-5", price_hint_amount=D("999"), price_hint_currency="USD", price_hint_basis="per_person"),
        ],
    )
    out = await pw.apply_action(conn, STAFF, "apply_provisional", keys, on=ON)  # type: ignore[arg-type]
    assert [(r["outcome"], r["reason"]) for r in out["results"]] == [
        ("done", None),
        ("skipped", "no_reference_amount"),
        ("skipped", "has_supplier_price"),
        ("unchanged", None),
        ("skipped", "hint_set_by_staff"),
    ]
    (args,) = conn.sql("UPDATE booking.trip_plans SET price_hint_amount")
    assert args == ("id-1", D("610"), "USD", "it-1091")

    clear = _plans(*(_plan(k) for k in keys[:3])).on(
        "min(start_date) AS next_start",
        [
            _extra_row("id-1", price_hint_amount=D("610"), price_hint_currency="USD", price_hint_basis="per_person"),
            _extra_row("id-2"),
            _extra_row("id-3", price_hint_amount=D("5"), price_hint_currency="USD", price_hint_basis="per_person"),
        ],
    )
    out = await pw.apply_action(clear, STAFF, "clear_provisional", keys[:3], on=ON)  # type: ignore[arg-type]
    assert [(r["outcome"], r["reason"]) for r in out["results"]] == [
        ("done", None),
        ("unchanged", None),
        ("skipped", "hint_set_by_staff"),
    ]
    assert clear.sql("UPDATE booking.trip_plans SET price_hint_amount")[0][1:3] == (None, None)


@pytest.mark.asyncio
async def test_recompute_action(monkeypatch: pytest.MonkeyPatch) -> None:
    async def load_pricing(conn: Any, on: date) -> Any:
        return cost_sheet.Pricing(on, {}, {})

    outcomes = iter([{"outcome": "changed", "reason": None}, {"outcome": "unchanged", "reason": "no_rates"}])

    async def recompute_plan(conn: Any, plan_id: str, pricing: Any) -> dict[str, Any]:
        return next(outcomes)

    monkeypatch.setattr(cost_sheet, "load_pricing", load_pricing)
    monkeypatch.setattr(cost_sheet, "recompute_plan", recompute_plan)
    out = await pw.apply_action(_plans(_plan(), _plan(OTHER)), STAFF, "recompute", [KEY, OTHER], on=ON)  # type: ignore[arg-type]
    assert [(r["outcome"], r["reason"]) for r in out["results"]] == [("done", None), ("unchanged", "no_rates")]


# ── Component suggestions ────────────────────────────────────────────────────


def test_text_helpers() -> None:
    assert cs.norm("Hạ Long Bay, Việt-Nam!") == " ha long bay viet nam "
    assert cs.words("The Invented Ridge Lodge & Spa") == ["invented", "ridge"]
    text = cs.norm("Pick-up at the airport, then lunch by the river. No dinner.")
    assert cs.keywords_in(text, "transfer") == ["pick up", "pick-up", "airport"]
    assert cs.keywords_in(text, "meal") == ["lunch", "dinner"]
    assert cs.keywords_in(cs.norm("Breakfast at the lodge"), "meal") == []  # breakfast is in the room rate
    assert not cs.has_phrase(text, "  ")


def _hotel(code: str = "HTL-000101", name: str = "Invented Ridge Lodge", **over: Any) -> cs.Hotel:
    hotel = cs.Hotel(code, name, over.pop("address", None), over.pop("destination_id", None), "VN", "SUP-000101")
    hotel.room_types = over.pop("rooms", [("rt-single", "Single"), ("rt-twin", "Garden Twin")])
    return hotel


def test_hotel_scores_and_room_choice() -> None:
    day = cs.Day(1, "Ridge day", "Night at Invented Ridge Lodge.", ["Ridge Town"], "Ridge Town", "dest-1")
    score, reasons = cs.score_hotel(_hotel(destination_id="dest-1"), day, iso="VN", country="Vietnam")
    assert score == pytest.approx(0.95)
    assert reasons[0] == "named in the day's text" and "in Vietnam" in reasons
    words_only = cs.Day(1, None, "Stay at the ridge lodge run by an invented family.")
    assert cs.score_hotel(_hotel(), words_only, iso=None, country=None) == (
        0.5,
        ["its name words are in the day's text (invented, ridge)"],
    )
    some = cs.Day(1, None, "Invented trails near the Ridge.")
    assert cs.score_hotel(_hotel(name="Invented Ridge Bamboo Hill"), some, iso=None, country=None)[0] == 0.3
    place = cs.Day(1, None, "Free evening.", ["Ridge Town"])
    s, r = cs.score_hotel(_hotel(address="12 Lake Road, Ridge Town"), place, iso=None, country=None)
    assert (s, r) == (0.2, ["'Ridge Town' is in its name or address"])
    assert _hotel().default_room() == ("rt-twin", "Garden Twin")
    assert _hotel(rooms=[("rt-a", "Family"), ("rt-b", "Suite")]).default_room() == ("rt-a", "Family")
    cand = cs.hotel_candidate(_hotel(), 0.95, ["named in the day's text"])
    assert cand["confidence"] == "high" and cand["label"] == "HTL-000101 Invented Ridge Lodge · Garden Twin"
    assert cand["reasons"][-1] == "room type Garden Twin: the default double or twin"
    assert cs.hotel_candidate(_hotel(), 0.3, [])["confidence"] == "medium"
    assert cs.hotel_candidate(_hotel(), 0.2, [])["confidence"] == "low"


def _service(stype: str, title: str, **over: Any) -> dict[str, Any]:
    return {
        "service_id": over.pop("id", f"sv-{stype}"),
        "code": over.pop("code", f"SVC-{stype[:3].upper()}"),
        "title": title,
        "service_type": stype,
        "unit": "person",
        "destination_id": over.pop("destination_id", None),
        "country_code": over.pop("country_code", "VN"),
        "source_id": over.pop("source_id", None),
        "supplier_code": "SUP-000101",
    }


def test_service_scores() -> None:
    day = cs.Day(2, "Valley", "Jeep ride through the valley with lunch at the farm.")
    s, r = cs.score_service(
        _service("transfer", "Invented valley jeep"), day, set(), tour_key=KEY, iso="VN", country="Vietnam"
    )
    assert s == pytest.approx(0.4 * 2 / 3 + 0.05)
    assert r == ["title words in the day's text (valley, jeep)", "in Vietnam"]
    own = _service("activity", "Invented boat", source_id=KEY, destination_id="d-1", country_code=None)
    s, r = cs.score_service(own, day, {"d-1"}, tour_key=KEY, iso="VN", country=None)
    assert s == pytest.approx(0.8) and r == ["made from this tour in CIS", "at one of the day's places in CIS"]


def test_suggest_days_needs_covered_and_unmatched() -> None:
    days = [
        cs.Day(1, "Arrive", "Transfer from the airport to Invented Ridge Lodge. Walk with your guide.", ["Ridge Town"]),
        cs.Day(2, "Valley", "Jeep ride and lunch at the farm.", ["Ridge Town"], destination_id="dest-1"),
    ]
    services = [
        _service("guide", "English speaking guide"),  # only the country: enough for a guide
        _service("transfer", "Invented valley jeep"),
        _service("meal", "Farm lunch"),
    ]
    current = [{"day_no": 2, "kind": "meal", "label": "SVC-MEA Farm lunch"}]
    out = cs.suggest(
        tour_key=KEY,
        duration_days=3,
        days=days,
        hotels=[_hotel(destination_id="dest-1"), _hotel("HTL-000102", "Unrelated Hill", rooms=[])],
        services=services,
        current=current,
        iso="VN",
        country="Vietnam",
    )
    assert [d["day_no"] for d in out["days"]] == [1, 2, 3]
    day1 = {n["kind"]: n for n in out["days"][0]["needs"]}
    assert day1["hotel"]["suggestion"]["code"] == "HTL-000101"
    assert (
        day1["guide"]["suggestion"]["service_id"] == "sv-guide" and day1["guide"]["suggestion"]["confidence"] == "low"
    )
    assert day1["transfer"]["status"] == "unmatched"
    day2 = {n["kind"]: n for n in out["days"][1]["needs"]}
    assert day2["meal"]["status"] == "covered" and day2["meal"]["current"][0]["label"] == "SVC-MEA Farm lunch"
    assert day2["transfer"]["suggestion"]["service_id"] == "sv-transfer"
    assert any("night's place" in r for r in day2["hotel"]["suggestion"]["reasons"])
    assert out["days"][2]["needs"] == [] and out["days"][2]["overnight_place"] is None
    assert out["counts"] == {"suggested": 4, "covered": 1, "unmatched": 1}
    assert out["unmatched"] == [
        {
            "kind": "transfer",
            "keywords": ["transfer", "airport"],
            "place": "Ridge Town",
            "day_no": 1,
            "why": "no active transfer service matches (transfer, airport)",
        }
    ]
    lonely = cs.suggest(
        tour_key=KEY,
        duration_days=2,
        days=[cs.Day(1, None, None, ["Nowhere"])],
        hotels=[],
        services=[],
        current=[],
        iso=None,
        country=None,
    )
    assert lonely["unmatched"][0]["why"] == "no active hotel matches the night at Nowhere"
    nameless = cs.suggest(
        tour_key=KEY, duration_days=2, days=[], hotels=[], services=[], current=[], iso=None, country=None
    )
    assert nameless["unmatched"][0]["why"] == "no active hotel matches this night"
    assert (
        cs.suggest(
            tour_key=KEY, duration_days=None, days=[], hotels=[], services=[], current=[], iso=None, country=None
        )["days"]
        == []
    )


def _suggest_conn(*, tour: Any, route_ready: bool = True) -> ScriptedConn:
    conn = ScriptedConn()
    conn.on("SELECT id::text FROM booking.trip_plans WHERE source_system = 'cis' AND source_id = $1", "plan-1")
    conn.on("FROM booking.trip_plan_components c", [])
    conn.on("FROM catalog.v_tour WHERE source_system = 'cis' AND source_id = $1 LIMIT 1", tour)
    conn.on(
        "FROM catalog.v_tour_day d",
        [{"day_number": 1, "title": "Arrive", "body": "Stay at Invented Ridge Lodge.", "places": ["Ridge Town"]}],
    )
    conn.on(VIEW_READY_FRAGMENT, route_ready)
    conn.on(
        "FROM catalog.v_tour_day_route r",
        [{"day_index": 1, "overnight_place": "Ridge Town", "destination_id": "dest-1"}],
    )
    conn.on("FROM booking.countries", "VN")
    conn.on(
        "FROM booking.hotels h",
        [
            {
                "code": "HTL-000101",
                "name": "Invented Ridge Lodge",
                "address": None,
                "destination_id": "dest-1",
                "country_code": "VN",
                "supplier_code": "SUP-000101",
                "room_type_id": "rt-1",
                "room_type": "Garden Twin",
            }
        ],
    )
    conn.on("FROM booking.services s", [])
    return conn


@pytest.mark.asyncio
async def test_build_suggestions_reads_by_tour_id() -> None:
    tour = [{"tour_id": "t-1", "title": "Invented Ridge Trek", "country": "Vietnam", "duration_days": 2}]
    conn = _suggest_conn(tour=tour)
    out = await cs.build(conn, KEY)  # type: ignore[arg-type]
    assert out["degraded"] == [] and out["candidates"] == {"hotels": 1, "services": 0}
    assert out["days"][0]["needs"][0]["suggestion"]["hotel_room_type_id"] == "rt-1"
    assert conn.sql("FROM catalog.v_tour_day d") == [("t-1",)]
    liveness.reset_cache()  # the route view was ready a moment ago (cached a minute)
    no_route = await cs.build(_suggest_conn(tour=tour, route_ready=False), KEY)  # type: ignore[arg-type]
    assert no_route["degraded"] == ["route"]
    with pytest.raises(NotFound):
        await cs.build(_suggest_conn(tour=[]), KEY)  # type: ignore[arg-type]
    broken = await cs.build(_suggest_conn(tour=asyncpg.UndefinedTableError("x")), KEY)  # type: ignore[arg-type]
    assert broken["degraded"] == ["catalog"] and broken["counts"] == {"suggested": 0, "covered": 0, "unmatched": 0}
    liveness.reset_cache()
    days_gone = _suggest_conn(tour=tour).on("FROM catalog.v_tour_day d", asyncpg.InsufficientPrivilegeError("x"))
    assert (await cs.build(days_gone, KEY))["degraded"] == ["days"]  # type: ignore[arg-type]


# ── Structured CIS reference ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("args", "out"),
    [
        ((D("1390"), "usd", "per_person"), (D("1390.00"), "USD")),
        ((D("1390"), "USD", None), (D("1390.00"), "USD")),
        ((D("3000"), "EUR", "per_group"), None),
        ((D("1200"), "EUR", "from"), (D("1200.00"), "EUR")),
        ((None, "USD", "per_person"), None),
        ((D("10"), "US", "per_person"), None),
        ((D("0"), "USD", "per_person"), None),
        (("abc", "USD", "per_person"), None),
        ((D("NaN"), "USD", "per_person"), None),
    ],
)
def test_structured_reference(args: tuple[Any, ...], out: Any) -> None:
    assert sync.structured_reference(*args) == out


def test_reference_of_prefers_a_trusted_structured_amount() -> None:
    text = "from USD 1,450"
    assert sync.reference_of(text, D("1390"), "USD", "per_person", "confirmed") == (D("1390.00"), "USD")
    assert sync.reference_of(text, D("1390"), "USD", "from", "parsed") == (D("1390.00"), "USD")
    assert sync.reference_of(text, D("3000"), "USD", "per_group", "confirmed") is None  # no per-person amount
    assert sync.reference_of(text, D("1390"), "USD", "per_person", "needs_review") == (D("1450.00"), "USD")
    assert sync.reference_of(text, D("1390"), "USD", "per_person", None) == (D("1450.00"), "USD")  # v1: no status
    assert sync.reference_of(text, None, None, None, "confirmed") == (D("1450.00"), "USD")
    assert sync.reference_of(text, D("1390"), "US", "per_person", "confirmed") == (D("1450.00"), "USD")  # unusable
    assert sync.reference_of(text) == (D("1450.00"), "USD")


@pytest.mark.asyncio
async def test_sync_reads_the_structured_reference_when_cis_has_it() -> None:
    liveness.reset_cache()
    conn = ScriptedConn()
    conn.on(VIEW_READY_FRAGMENT, True)
    conn.on(
        "SELECT source_id, reference_price_raw, reference_amount",
        [
            {
                "source_id": KEY,
                "reference_price_raw": "from USD 1,450",
                "reference_amount": D("1450.00"),
                "reference_currency": "USD",
            },
            {
                "source_id": OTHER,
                "reference_price_raw": "EUR 900",
                "reference_amount": D("900.00"),
                "reference_currency": "EUR",
            },
        ],
    )
    none = {"amount": None, "currency": None, "basis": None, "status": None}
    conn.on(
        "reference_amount::numeric AS amount",
        [
            {
                "source_id": KEY,
                "price_raw": "from USD 1,450",
                "amount": D("1390"),
                "currency": "USD",
                "basis": "per_person",
                "status": "confirmed",
            },
            {"source_id": OTHER, "price_raw": "EUR 900", **none},
            {"source_id": "not-a-plan", "price_raw": "USD 1", **none},
        ],
    )
    out = await sync._references(conn)  # type: ignore[arg-type]
    assert out == {  # only the amount of KEY changed
        "references": "ok",
        "references_source": "structured",
        "references_changed": 1,
        "references_structured": 1,
    }
    assert any("catalog.v_tour_price_reference_v2" in q for q, _ in conn.calls)
    (args,) = conn.sql("UPDATE booking.trip_plans p SET reference_price_raw")
    assert args == ([KEY], ["from USD 1,450"], [D("1390.00")], ["USD"])
    liveness.reset_cache()


@pytest.mark.asyncio
async def test_sync_parses_the_v1_text_without_the_v2_view() -> None:
    liveness.reset_cache()
    conn = ScriptedConn()
    conn.on(VIEW_READY_FRAGMENT, lambda schema, name, cols: name == "v_tour_price_reference_v1")
    conn.on(
        "SELECT source_id, reference_price_raw, reference_amount",
        [{"source_id": KEY, "reference_price_raw": None, "reference_amount": None, "reference_currency": None}],
    )
    conn.on(
        "NULL::text AS status",
        [
            {
                "source_id": KEY,
                "price_raw": "from USD 1,450",
                "amount": None,
                "currency": None,
                "basis": None,
                "status": None,
            }
        ],
    )
    out = await sync._references(conn)  # type: ignore[arg-type]
    assert out == {
        "references": "ok",
        "references_source": "text",
        "references_changed": 1,
        "references_structured": 0,
    }
    (args,) = conn.sql("UPDATE booking.trip_plans p SET reference_price_raw")
    assert args == ([KEY], ["from USD 1,450"], [D("1450.00")], ["USD"])
    liveness.reset_cache()
    conn = ScriptedConn().on(VIEW_READY_FRAGMENT, False)
    assert await sync._references(conn) == {"references": "skipped"}  # type: ignore[arg-type]
    liveness.reset_cache()
