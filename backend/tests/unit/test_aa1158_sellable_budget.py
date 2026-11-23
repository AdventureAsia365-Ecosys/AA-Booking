"""AA-1158 Q9: the workbook's sellable gate and its recompute budget agree between the dry run and the apply.

The apply recomputes at most ``price_import.MAX_RECOMPUTE`` plans; a tour going sellable counts on that recompute
for its price. So the recompute takes the plans going sellable first, and the dry run accepts the ``priced``
prediction only for a plan within the budget (the others get ``no_approved_price`` before anything is written)."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from app.catalog import price_import as pi
from app.catalog import price_readiness, sellable_gate, trip_plan_prices
from tests.unit.fakes import ScriptedConn

TODAY = date(2027, 3, 1)
PART = {"day_no": 1, "hotel_room_type_id": "rt-1", "service_id": None}


def _plans(n_going: int, n_other: int) -> dict[str, pi.Plan]:
    """``n_other`` plans with components first (workbook order), then ``n_going`` plans going sellable."""
    plans = [pi.Plan(f"o-{i}", f"other-{i}", False, components_now=[PART]) for i in range(n_other)]
    plans += [
        pi.Plan(f"g-{i}", f"going-{i}", False, components_now=[PART], sellable_after=True) for i in range(n_going)
    ]
    return {p.key: p for p in plans}


def test_the_plans_going_sellable_are_recomputed_first() -> None:
    plans = _plans(3, pi.MAX_RECOMPUTE + 5)
    queue = sellable_gate.recompute_queue(plans, pi.MAX_RECOMPUTE)
    assert len(queue) == pi.MAX_RECOMPUTE
    assert [p.id for p in queue[:3]] == ["g-0", "g-1", "g-2"]
    assert [p.id for p in queue[3:]] == [f"o-{i}" for i in range(pi.MAX_RECOMPUTE - 3)]
    # A plan with no components (now or in the file) is never recomputed.
    bare = {"bare": pi.Plan("b-1", "bare", False, sellable_after=True)}
    assert sellable_gate.recompute_queue(bare, pi.MAX_RECOMPUTE) == []


@pytest.mark.asyncio
async def test_the_dry_run_refuses_what_the_apply_would_not_price(monkeypatch: pytest.MonkeyPatch) -> None:
    async def price_ready(conn: Any, ids: Any, today: date) -> set[str]:
        return set()  # no matrix row yet: every tour going sellable counts on the import's recompute

    monkeypatch.setattr(trip_plan_prices, "price_ready", price_ready)
    plans = _plans(pi.MAX_RECOMPUTE + 2, 10)
    sheets = pi.Sheets(tours=[{"tour_key": k, "_row": i + 2} for i, k in enumerate(plans)])
    after = {k: price_readiness.PRICED for k in plans}
    refused: list[Any] = []
    queue = sellable_gate.recompute_queue(plans, pi.MAX_RECOMPUTE)
    await sellable_gate.check_import(
        ScriptedConn(),  # type: ignore[arg-type]
        sheets,
        plans,
        after,
        TODAY,
        recomputed={p.id for p in queue},
        sheet=pi.TOURS,
        refuse=refused.append,
    )
    assert refused == [sheets]
    over = list(sheets.problems)
    assert [p.row for p in over] == [12 + pi.MAX_RECOMPUTE, 13 + pi.MAX_RECOMPUTE]  # the last two going sellable
    assert {p.code for p in over} == {trip_plan_prices.NO_APPROVED_PRICE}
    assert all(p.message == sellable_gate.OVER_BUDGET for p in over)

    # Within the budget the dry run passes: the apply recomputes every one of them before the switch.
    small = _plans(3, pi.MAX_RECOMPUTE + 5)
    ok = pi.Sheets(tours=[{"tour_key": k, "_row": i + 2} for i, k in enumerate(small)])
    await sellable_gate.check_import(
        ScriptedConn(),  # type: ignore[arg-type]
        ok,
        small,
        {k: price_readiness.PRICED for k in small},
        TODAY,
        recomputed={p.id for p in sellable_gate.recompute_queue(small, pi.MAX_RECOMPUTE)},
        sheet=pi.TOURS,
        refuse=refused.append,
    )
    assert ok.problems == [] and refused == [sheets]


@pytest.mark.asyncio
async def test_a_tour_without_a_priced_prediction_keeps_the_plain_message(monkeypatch: pytest.MonkeyPatch) -> None:
    async def price_ready(conn: Any, ids: Any, today: date) -> set[str]:
        return set()

    monkeypatch.setattr(trip_plan_prices, "price_ready", price_ready)
    plans = _plans(1, 0)
    sheets = pi.Sheets(tours=[{"tour_key": "going-0", "_row": 2}])
    await sellable_gate.check_import(
        ScriptedConn(),  # type: ignore[arg-type]
        sheets,
        plans,
        {"going-0": price_readiness.MISSING_RATES},
        TODAY,
        recomputed={"g-0"},
        sheet=pi.TOURS,
        refuse=lambda s: None,
    )
    assert [p.message for p in sheets.problems] == [sellable_gate.MESSAGE]
