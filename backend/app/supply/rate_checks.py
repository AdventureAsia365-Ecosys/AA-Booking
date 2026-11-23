"""Business checks and the diff of a supplier's rates before a second person publishes them (AA-1153 part 1, T10;
S285 advisory r2 hotels, "importer bảng giá DMC có kiểm tra chặt hơn"). Pure: no database, no clock.

A rate here is a dict with the keys a rate row has (``hotel_room_type_id`` or ``service_id``, ``meal_plan``,
``pax_type``, ``pax_min``, ``pax_max``, ``season``, ``valid_from``, ``valid_to``, ``net_amount``) plus, from a sheet,
``sheet`` and ``row``. A finding is ``{code, severity, message, sheet?, row?, ...}``; severity ``error`` blocks the
import, ``warning`` is listed at the dry run, stored with the import and acknowledged by the publisher.

- ``board_order`` (warning): for one room type, pax band and dates, a board that includes more meals is cheaper than
  one with fewer (order RO <= BB <= HB <= FB <= AI): "Deluxe HB 55.00 is below BB 60.00".
- ``season_overlap`` (error): two different seasons of the contract share a day.
- ``season_gap`` (warning): days of the contract validity no season covers (only when the contract has seasons; an
  open-ended contract is checked up to its last season day).
- ``price_jump`` (warning): a rate at least ``jump_pct`` % above the same rate of the previous contract (same target,
  board, pax type, pax band and season, or the same day-and-month range without a season).

:func:`diff` compares the same keys: ``added``, ``changed`` (old and new net, the change in %) and ``removed``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

BOARDS = ("RO", "BB", "HB", "FB", "AI")  # fewest meals first
HUNDRED = Decimal("100")
MAX_FINDINGS = 200  # technical-constant: what one import reports (the import log CHECK keeps 500)


def _net(rate: Mapping[str, Any]) -> Decimal:
    return Decimal(str(rate["net_amount"]))


def _where(rate: Mapping[str, Any]) -> dict[str, Any]:
    return {k: rate[k] for k in ("sheet", "row") if rate.get(k) is not None}


def target(rate: Mapping[str, Any]) -> str:
    return str(rate.get("hotel_room_type_id") or rate.get("service_id") or "")


def rate_key(rate: Mapping[str, Any]) -> tuple[Any, ...]:
    """What makes two rates of two contracts "the same rate": the season name when there is one, else the day and
    month of the validity (a yearly contract repeats its ranges)."""
    season = (rate.get("season") or "").strip().lower()
    when: Any = season or (
        _month_day(rate.get("valid_from")),
        _month_day(rate.get("valid_to")),
    )
    return (
        target(rate),
        rate.get("meal_plan"),
        rate.get("pax_type") or "adult",
        rate.get("pax_min"),
        rate.get("pax_max"),
        when,
    )


def _month_day(day: date | None) -> str | None:
    return day.strftime("%m-%d") if day else None


def _pct(old: Decimal, new: Decimal) -> Decimal | None:
    if old == 0:
        return None
    return ((new - old) / old * HUNDRED).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def board_order(rates: Iterable[Mapping[str, Any]], names: Mapping[str, str]) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], dict[str, Mapping[str, Any]]] = {}
    for rate in rates:
        board = rate.get("meal_plan")
        if not rate.get("hotel_room_type_id") or board not in BOARDS:
            continue
        key = (rate["hotel_room_type_id"], rate.get("pax_min"), rate.get("pax_max"), rate.get("valid_from"),
               rate.get("valid_to"))  # fmt: skip
        groups.setdefault(key, {})[board] = rate
    out = []
    for (room, *_rest), by_board in groups.items():
        present = [b for b in BOARDS if b in by_board]
        for lower, higher in zip(present, present[1:], strict=False):
            low, high = by_board[lower], by_board[higher]
            if _net(high) < _net(low):
                out.append(
                    {
                        "code": "board_order",
                        "severity": "warning",
                        "message": f"{names.get(room, 'room')} {low.get('valid_from') or ''}: {higher} {_net(high)} is"
                        f" below {lower} {_net(low)} (a board with more meals costs less)",
                        **_where(high),
                    }
                )
    return out


def season_findings(
    seasons: Sequence[Mapping[str, Any]], valid_from: date | None, valid_to: date | None
) -> list[dict[str, Any]]:
    """Overlaps between different seasons (errors) and contract days no season covers (warnings).

    One sweep over the ranges by start day (AA-1153 review: never a pairwise comparison of every range): each range is
    checked against the furthest-reaching earlier range of every other season, so it gets at most one overlap error."""
    if not seasons:
        return []
    out: list[dict[str, Any]] = []
    ranges = sorted(seasons, key=lambda s: (s["date_from"], s["date_to"]))
    reach: dict[str, Mapping[str, Any]] = {}  # per season name, its earlier range that ends last
    for b in ranges:
        name = b["name"].strip().lower()
        a = next(
            (r for n, r in reach.items() if n != name and r["date_to"] >= b["date_from"]),
            None,
        )
        if a is not None:
            out.append(
                {
                    "code": "season_overlap",
                    "severity": "error",
                    "message": f"{a['name']} ({a['date_from']} to {a['date_to']}) and {b['name']} ({b['date_from']}"
                    f" to {b['date_to']}) share {max(a['date_from'], b['date_from'])}",
                    **_where(b),
                }
            )
        if name not in reach or b["date_to"] > reach[name]["date_to"]:
            reach[name] = b
    start = valid_from or ranges[0]["date_from"]
    end = valid_to or max(r["date_to"] for r in ranges)
    cursor = start
    for r in ranges:
        if r["date_from"] > cursor and cursor <= end:
            gap_end = min(r["date_from"] - timedelta(days=1), end)
            out.append(_gap(cursor, gap_end))
        cursor = max(cursor, r["date_to"] + timedelta(days=1))
    if cursor <= end:
        out.append(_gap(cursor, end))
    return out


def _gap(start: date, end: date) -> dict[str, Any]:
    return {
        "code": "season_gap",
        "severity": "warning",
        "message": f"no season covers {start} to {end}",
        "date_from": start.isoformat(),
        "date_to": end.isoformat(),
    }


def price_jumps(
    rates: Iterable[Mapping[str, Any]],
    previous: Iterable[Mapping[str, Any]],
    jump_pct: Decimal,
    names: Mapping[str, str],
) -> list[dict[str, Any]]:
    old = {rate_key(r): r for r in previous}
    out = []
    for rate in rates:
        before = old.get(rate_key(rate))
        if before is None:
            continue
        change = _pct(_net(before), _net(rate))
        if change is not None and change >= jump_pct:
            out.append(
                {
                    "code": "price_jump",
                    "severity": "warning",
                    "message": " ".join(
                        p for p in (names.get(target(rate), "rate"), rate.get("meal_plan"), rate.get("season")) if p
                    )
                    + f": {_net(before)} -> {_net(rate)} (+{change} %)",
                    "old_net": str(_net(before)),
                    "new_net": str(_net(rate)),
                    "change_pct": str(change),
                    **_where(rate),
                }
            )
    return out


def run(
    rates: Sequence[Mapping[str, Any]],
    *,
    seasons: Sequence[Mapping[str, Any]] = (),
    valid_from: date | None = None,
    valid_to: date | None = None,
    previous: Sequence[Mapping[str, Any]] = (),
    jump_pct: Decimal,
    names: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Every finding, errors first (at most :data:`MAX_FINDINGS`)."""
    labels = names or {}
    found = [
        *season_findings(seasons, valid_from, valid_to),
        *board_order(rates, labels),
        *price_jumps(rates, previous, jump_pct, labels),
    ]
    found.sort(key=lambda f: f["severity"] != "error")
    return found[:MAX_FINDINGS]


def errors(findings: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [f for f in findings if f["severity"] == "error"]


def warnings(findings: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [f for f in findings if f["severity"] == "warning"]


def _summary(rate: Mapping[str, Any], names: Mapping[str, str]) -> dict[str, Any]:
    return {
        "target": target(rate),
        "label": names.get(target(rate)),
        "meal_plan": rate.get("meal_plan"),
        "pax_type": rate.get("pax_type") or "adult",
        "pax_min": rate.get("pax_min"),
        "pax_max": rate.get("pax_max"),
        "season": rate.get("season"),
        "valid_from": rate["valid_from"].isoformat() if rate.get("valid_from") else None,
        "valid_to": rate["valid_to"].isoformat() if rate.get("valid_to") else None,
    }


def diff(
    rates: Sequence[Mapping[str, Any]], previous: Sequence[Mapping[str, Any]], names: Mapping[str, str] | None = None
) -> dict[str, list[dict[str, Any]]]:
    """``added`` / ``changed`` / ``removed`` rates against the previous contract (same keys as :func:`rate_key`)."""
    labels = names or {}
    old = {rate_key(r): r for r in previous}
    new = {rate_key(r): r for r in rates}
    changed = []
    for key, rate in new.items():
        before = old.get(key)
        if before is not None and _net(before) != _net(rate):
            change = _pct(_net(before), _net(rate))
            changed.append(
                {
                    **_summary(rate, labels),
                    "old_net": str(_net(before)),
                    "new_net": str(_net(rate)),
                    "change_pct": str(change) if change is not None else None,
                }
            )
    return {
        "added": [{**_summary(r, labels), "new_net": str(_net(r))} for k, r in new.items() if k not in old],
        "changed": changed,
        "removed": [{**_summary(r, labels), "old_net": str(_net(r))} for k, r in old.items() if k not in new],
    }
