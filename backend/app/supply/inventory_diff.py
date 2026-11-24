"""The Inventory part of a rate review's diff (S291, AA-1153 follow-up): the allotment, free-sale and on-request terms
of a draft supplier contract against the contract it replaces (:func:`app.supply.rate_review.checks`). Pure.

Before, the diff listed rates only, so a re-imported sheet that cut an allotment from 5 rooms to 2, or turned it into
on request, reached the second person without a word. A term is keyed by its component (``rt:`` room type or ``sv:``
service) and its dates; the kind and the numbers (units, release days, max pax) are compared:

- ``added`` / ``removed``: a key on one side only (a term whose dates moved is one removed and one added);
- ``changed``: the same key with another kind or other numbers, ``narrowed`` when the new term sells less: a kind
  that holds less (free sale > allotment > on request), fewer units, more release days (the rooms go back sooner), a
  lower or new max pax on a free sale;
- ``narrowed``: how many changed terms narrowed, plus every removed term.

Stop-sales are not terms of a contract (they have their own approval) and are not compared."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

KIND_RANK = {"on_request": 0, "allotment": 1, "free_sale": 2}
NUMBERS = ("units", "release_days", "max_pax")


def term(row: Mapping[str, Any]) -> dict[str, Any]:
    """One term in the diff's shape, from a database row or an Inventory sheet preview row."""
    return {
        "component_id": row["component_id"],
        "date_from": row["date_from"],
        "date_to": row["date_to"],
        "kind": row.get("kind") or row.get("inventory_kind"),
        **{k: row.get(k) for k in NUMBERS},
    }


def _key(t: Mapping[str, Any]) -> tuple[str, Any, Any]:
    return (t["component_id"], t["date_from"], t["date_to"])


def _values(t: Mapping[str, Any]) -> dict[str, Any]:
    return {"kind": t["kind"], **{k: t.get(k) for k in NUMBERS}}


def narrowed(old: Mapping[str, Any], new: Mapping[str, Any]) -> bool:
    """True when ``new`` sells less than ``old`` on the same dates (see the module docstring)."""
    before, after = KIND_RANK.get(old["kind"], 0), KIND_RANK.get(new["kind"], 0)
    if after != before:
        return after < before
    if new["kind"] == "allotment":
        return (new.get("units") or 0) < (old.get("units") or 0) or (new.get("release_days") or 0) > (
            old.get("release_days") or 0
        )
    if new["kind"] == "free_sale" and new.get("max_pax") is not None:
        return old.get("max_pax") is None or new["max_pax"] < old["max_pax"]
    return False


def diff(
    terms: Sequence[Mapping[str, Any]], previous: Sequence[Mapping[str, Any]], names: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """``added`` / ``changed`` / ``removed`` terms and the ``narrowed`` count (the draft ``terms`` against the
    ``previous`` contract's), each row with the component's ``label`` when ``names`` knows it (keyed by its uuid)."""
    labels = names or {}
    new = {_key(t): term(t) for t in terms}
    old = {_key(t): term(t) for t in previous}

    def row(t: dict[str, Any]) -> dict[str, Any]:
        return {**t, "label": labels.get(t["component_id"][3:])}

    changed = []
    for key, after in new.items():
        before = old.get(key)
        if before is not None and _values(before) != _values(after):
            changed.append(
                {
                    **{k: v for k, v in row(after).items() if k not in ("kind", *NUMBERS)},
                    "old": _values(before),
                    "new": _values(after),
                    "narrowed": narrowed(before, after),
                }
            )
    removed = [row(t) for k, t in old.items() if k not in new]
    return {
        "added": [row(t) for k, t in new.items() if k not in old],
        "changed": changed,
        "removed": removed,
        "narrowed": sum(1 for c in changed if c["narrowed"]) + len(removed),
    }
