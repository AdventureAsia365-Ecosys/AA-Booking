"""The Prices sheet of the price setup workbook as rows (AA-1091; moved out of :mod:`app.catalog.price_import` in
AA-1187 to keep that module under its size ratchet): the hand row a price writes, the cost-priced rows an import
keeps, the merge of both, and the draft price version row."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.catalog import cost_sheet, trip_plan_prices
from app.pricing import engine


def hand_row(p: Mapping[str, Any], markup: Decimal | None, increments: engine.Increments) -> dict[str, Any]:
    """One imported hand price as ``trip_plan_prices.store`` writes it (no net cost: nothing is computed)."""
    amount = p["amount"]
    return {
        "channel": p["channel"],
        "season": str(p["season"])[: cost_sheet.SEASON_MAX] if p.get("season") else None,
        "peak": bool(p.get("peak")),
        "valid_from": p["valid_from"],
        "valid_to": p["valid_to"],
        "pax_min": int(p["pax_min"]),
        "pax_max": p.get("pax_max"),
        "currency": p["currency"],
        "amount": amount,
        "net_amount": None,
        "net_currency": None,
        "effective_margin": None,
        "priced_on": None,
        "overridden": False,
        "computed": False,
        "rrp_amount": trip_plan_prices.rrp_for(p["channel"], amount, p["currency"], markup, increments),
        "child_amount": p.get("child_amount"),
        "infant_amount": p.get("infant_amount"),
        "single_supplement_amount": p.get("single_supplement_amount"),
        "child_net_amount": None,
        "infant_net_amount": None,
        "single_supplement_net_amount": None,
    }


def cost_priced(row: Mapping[str, Any]) -> bool:
    """A row priced from a cost: computed from supplier rates, or typed with a net cost on the single price form (its
    net cost, margin and override live only in the matrix). Neither is in the workbook, so an import keeps them."""
    return bool(row["computed"]) or row["net_amount"] is not None


@dataclass(frozen=True)
class Merged:
    rows: list[dict[str, Any]]
    computed_replaced: int
    net_replaced: int


def merge_prices(
    existing: Sequence[dict[str, Any]], hand: Sequence[dict[str, Any]], pricing: cost_sheet.Pricing
) -> Merged:
    """The new matrix: the imported hand rows, then the cost-priced rows kept (as stored, their RRP under today's
    markup) that no hand row overlaps; positions 1..n. Plain hand rows (no net cost) are the file's and are replaced.
    Also counts the cost-priced rows a hand row replaces (computed, and typed with a net cost)."""
    priced = [cost_sheet.kept_row(r, pricing) for r in existing if cost_priced(r)]
    covered = [any(cost_sheet.overlaps(r, h) for h in hand) for r in priced]
    kept = [r for r, c in zip(priced, covered, strict=True) if not c]
    gone = [r for r, c in zip(priced, covered, strict=True) if c]
    final = [*(dict(h) for h in hand), *kept]
    for i, row in enumerate(final, start=1):
        row["position"] = i
    computed = sum(1 for r in gone if r["computed"])
    return Merged(final, computed, len(gone) - computed)


def draft_row(p: Mapping[str, Any]) -> dict[str, Any]:
    """One imported hand price as a draft version row (the peak comes from the season calendar)."""
    return {
        "channel": p["channel"],
        "season": str(p["season"])[: cost_sheet.SEASON_MAX] if p.get("season") else None,
        "valid_from": p["valid_from"],
        "valid_to": p["valid_to"],
        "pax_min": int(p["pax_min"]),
        "pax_max": p.get("pax_max"),
        "currency": p["currency"],
        "amount": p["amount"],
        "child_amount": p.get("child_amount"),
        "infant_amount": p.get("infant_amount"),
        "single_supplement_amount": p.get("single_supplement_amount"),
    }
