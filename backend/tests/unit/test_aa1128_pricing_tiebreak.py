"""AA-1128 (B0 review): every "row in force" query of the maker-checker policy tables breaks a tie on the day by the
approval time (``approved_at DESC``), then the row id, so two approved rows of one channel / currency dated on the
same day always resolve to the one approved last, the same row for the pricing engine, the staff lists, the B2B FX
read and the cancellation policies. Real PostgreSQL: tests/integration/test_bk004_pricing.py
(test_same_day_rows_take_the_last_approved)."""

from __future__ import annotations

import re

import pytest

from app.b2b import cancellation, reads
from app.pricing import policy, repository


def _order_by(sql: str, *, cte: bool) -> str:
    """The ORDER BY clause of the query (``cte``: of its ``in_force`` CTE), one line, spaces collapsed."""
    if cte:
        sql = sql.split("WITH in_force AS (", 1)[1].split(")\nSELECT", 1)[0]
    match = re.search(r"ORDER BY (.+?)(?:\n|$)", sql)
    assert match is not None, sql
    return " ".join(match.group(1).split())


@pytest.mark.parametrize(
    ("name", "sql", "cte", "expected"),
    [
        ("repository.RULE_SQL", repository.RULE_SQL, False, "r.valid_from DESC, a.approved_at DESC, r.id DESC"),
        ("repository.FX_SQL", repository.FX_SQL, False, "f.currency, f.as_of DESC, a.approved_at DESC, f.id DESC"),
        (
            "policy.RULES_SQL in_force",
            policy.RULES_SQL,
            True,
            "r.channel, r.valid_from DESC, ap.approved_at DESC, r.id DESC",
        ),
        ("policy.FX_SQL in_force", policy.FX_SQL, True, "f.currency, f.as_of DESC, ap.approved_at DESC, f.id DESC"),
        ("reads.FX_ALL_SQL", reads.FX_ALL_SQL, False, "f.currency, f.as_of DESC, a.approved_at DESC, f.id DESC"),
        (
            "cancellation.LIST_SQL in_force",
            cancellation.LIST_SQL,
            True,
            "p.channel, p.trip_plan_id, p.valid_from DESC, ap.approved_at DESC, p.id DESC",
        ),
        (
            "cancellation.IN_FORCE_SQL",
            cancellation.IN_FORCE_SQL,
            False,
            "(p.trip_plan_id IS NULL), p.valid_from DESC, a.approved_at DESC, p.id DESC",
        ),
    ],
)
def test_in_force_order_breaks_ties_by_approval(name: str, sql: str, cte: bool, expected: str) -> None:
    assert _order_by(sql, cte=cte) == expected, name


def test_engine_and_list_agree_on_the_rule_order() -> None:
    """The staff list marks as ``in_force`` the row the pricing engine prices with: same keys after the channel."""
    engine = _order_by(repository.RULE_SQL, cte=False)
    listed = _order_by(policy.RULES_SQL, cte=True).removeprefix("r.channel, ")
    assert engine.replace("a.approved_at", "ap.approved_at") == listed
    fx_engine = _order_by(repository.FX_SQL, cte=False)
    fx_listed = _order_by(policy.FX_SQL, cte=True)
    assert fx_engine.replace("a.approved_at", "ap.approved_at") == fx_listed
