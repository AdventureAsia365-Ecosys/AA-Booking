"""S291 follow-ups of AA-1153 without a database: the Inventory part of a rate review's diff (an allotment narrowed is
reported) and the deterministic choice of the contract a draft replaces when one DMC holds two contracts starting the
same day. Invented ids and names only; the HTTP flow runs in tests/integration/test_aa1153_review_followups.py."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from app.supply import inventory_diff, rate_review
from tests.unit.fakes import ScriptedConn

ROOM = "00000000-0000-4000-8000-00000000a291"
CAR = "00000000-0000-4000-8000-00000000b291"
MAR1, MAR31, APR30 = date(2028, 3, 1), date(2028, 3, 31), date(2028, 4, 30)


def _term(kind: str = "allotment", component: str = f"rt:{ROOM}", **kw: Any) -> dict[str, Any]:
    return {
        "component_id": component,
        "kind": kind,
        "date_from": kw.get("date_from", MAR1),
        "date_to": kw.get("date_to", MAR31),
        "units": kw.get("units"),
        "release_days": kw.get("release_days"),
        "max_pax": kw.get("max_pax"),
    }


# ── Inventory diff ───────────────────────────────────────────────────────────


def test_an_allotment_cut_from_five_rooms_to_two_is_a_narrowed_change() -> None:
    out = inventory_diff.diff(
        [_term(units=2, release_days=7)], [_term(units=5, release_days=7)], {ROOM: "HTL-1 Lotus Cove · Deluxe"}
    )
    assert out["added"] == [] and out["removed"] == [] and out["narrowed"] == 1
    (change,) = out["changed"]
    assert change["label"] == "HTL-1 Lotus Cove · Deluxe" and change["narrowed"] is True
    assert change["old"] == {"kind": "allotment", "units": 5, "release_days": 7, "max_pax": None}
    assert change["new"]["units"] == 2


@pytest.mark.parametrize(
    ("old", "new", "narrowed"),
    [
        (_term(units=5, release_days=7), _term(units=5, release_days=14), True),  # rooms go back sooner
        (_term(units=5, release_days=14), _term(units=6, release_days=14), False),
        (_term(units=5, release_days=7), _term("on_request"), True),
        (_term("free_sale"), _term(units=3, release_days=7), True),
        (_term("on_request"), _term("free_sale"), False),
        (_term("free_sale"), _term("free_sale", max_pax=6), True),
        (_term("free_sale", max_pax=6), _term("free_sale", max_pax=8), False),
    ],
)
def test_narrowed_means_the_new_term_sells_less(old: dict[str, Any], new: dict[str, Any], narrowed: bool) -> None:
    assert inventory_diff.narrowed(old, new) is narrowed


def test_moved_dates_are_one_removed_and_one_added_and_a_removed_term_counts_as_narrowed() -> None:
    sheet = [
        {**_term(units=2, release_days=7, date_to=date(2028, 3, 15)), "inventory_kind": "allotment", "row": 4},
        _term("free_sale", component=f"sv:{CAR}", date_to=APR30),
    ]
    sheet[0].pop("kind")  # a sheet preview row names the kind inventory_kind
    out = inventory_diff.diff(sheet, [_term(units=2, release_days=7)])
    assert [(a["component_id"], a["kind"], a["date_to"]) for a in out["added"]] == [
        (f"rt:{ROOM}", "allotment", date(2028, 3, 15)),
        (f"sv:{CAR}", "free_sale", APR30),
    ]
    assert [r["date_to"] for r in out["removed"]] == [MAR31] and out["narrowed"] == 1
    assert out["changed"] == [] and "row" not in out["added"][0]
    assert inventory_diff.diff([], []) == {"added": [], "changed": [], "removed": [], "narrowed": 0}


# ── Which contract a draft replaces ──────────────────────────────────────────


def _contract(cid: str, title: str, reference: str | None = None) -> dict[str, Any]:
    return {"id": cid, "title": title, "reference": reference, "valid_from": MAR1, "valid_to": date(2028, 12, 31)}


HOTELS = _contract("k-hotels", "Lotus rooms 2028", "LC-ROOMS")
CARS = _contract("k-cars", "Lotus transfers 2028", "LC-CARS")  # published later: first in the SQL order


def _conn(candidates: list[dict[str, Any]]) -> ScriptedConn:
    rates = {"k-hotels": [{"hotel_room_type_id": ROOM}], "k-cars": [{"service_id": CAR}]}
    conn = ScriptedConn()
    conn.on("ORDER BY k.published_at DESC NULLS LAST, k.id DESC", candidates)
    conn.on("FROM booking.supplier_rates x WHERE x.supplier_contract_id", lambda cid: rates.get(cid, []))
    conn.on("WHERE k.id = $1::uuid", lambda cid: next((c for c in (HOTELS, CARS) if c["id"] == cid), None))
    return conn


@pytest.mark.asyncio
async def test_two_contracts_starting_the_same_day_the_one_the_draft_reprices_is_the_base() -> None:
    draft_rates = [{"hotel_room_type_id": ROOM}]
    found, before = await rate_review.previous_of(_conn([CARS, HOTELS]), "s-1", {"valid_from": MAR1}, draft_rates)
    assert found["id"] == "k-hotels" and before == [{"hotel_room_type_id": ROOM}]
    # Same answer whatever the order the rows come in (deterministic).
    found, _ = await rate_review.previous_of(_conn([HOTELS, CARS]), "s-1", {"valid_from": MAR1}, draft_rates)
    assert found["id"] == "k-hotels"


@pytest.mark.asyncio
async def test_the_reference_wins_then_the_newest_breaks_a_tie() -> None:
    sheet = {"valid_from": MAR1, "reference": " lc-cars "}
    found, _ = await rate_review.previous_of(_conn([HOTELS, CARS]), "s-1", sheet, [{"hotel_room_type_id": ROOM}])
    assert found["id"] == "k-cars"  # the supplier's own contract number names it, whatever the items
    found, _ = await rate_review.previous_of(_conn([CARS, HOTELS]), "s-1", {"valid_from": MAR1}, [])
    assert found["id"] == "k-cars"  # nothing in common with either: the newest published (SQL order)


@pytest.mark.asyncio
async def test_the_contract_an_import_stored_wins_and_one_candidate_is_read_once() -> None:
    conn = _conn([CARS, HOTELS])
    found, _ = await rate_review.previous_of(conn, "s-1", {"valid_from": MAR1}, [], previous_id="k-hotels")
    assert found["id"] == "k-hotels" and conn.sql("ORDER BY k.published_at DESC") == []
    single = _conn([HOTELS])
    found, _ = await rate_review.previous_of(single, "s-1", {"valid_from": MAR1}, [])
    assert found["id"] == "k-hotels" and len(single.sql("FROM booking.supplier_rates x")) == 1
    found, before = await rate_review.previous_of(_conn([]), "s-1", {"valid_from": MAR1}, [])
    assert found is None and before == []
