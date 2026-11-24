"""AA-1153 part 2: the price side (SQL rate choice) and the availability side (allotment terms) rank one DMC's
published contracts the same way, newest ``published_at`` first, then the contract id (app.supply.newest_contract)."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import Any

import pytest

from app.supply import newest_contract, rate_review, rates, restrictions

DAY = date(2027, 3, 10)
EARLY = datetime(2027, 1, 5, 9, 0, tzinfo=UTC)
LATE = datetime(2027, 2, 1, 9, 0, tzinfo=UTC)
# Contract ids in lower-case text form (PostgreSQL orders uuid by bytes, the same order as this text).
LOW, HIGH = "1b0e4f6a-0000-4000-8000-000000000001", "e3c1a2b4-0000-4000-8000-000000000002"


def _sql_first(contracts: list[dict[str, Any]]) -> str:
    """The contract the SQL fragment NEWEST_CONTRACT_FIRST puts first, evaluated term by term in Python
    (``k.<column> DESC|ASC [NULLS FIRST|LAST]``; PostgreSQL's default is NULLS FIRST for DESC, LAST for ASC)."""
    rows = list(contracts)
    terms = [t.strip() for t in newest_contract.NEWEST_CONTRACT_FIRST.split(",")]
    for term in reversed(terms):  # stable sorts, last key first
        m = re.fullmatch(r"k\.(\w+)(?: (ASC|DESC))?(?: NULLS (FIRST|LAST))?", term)
        assert m, f"unexpected ORDER BY term {term!r}"
        column, direction, nulls = m.group(1), m.group(2) or "ASC", m.group(3)
        desc = direction == "DESC"
        nulls_first = (nulls or ("FIRST" if desc else "LAST")) == "FIRST"
        present = sorted((r for r in rows if r[column] is not None), key=lambda r: r[column], reverse=desc)
        missing = [r for r in rows if r[column] is None]
        rows = missing + present if nulls_first else present + missing
    return str(rows[0]["id"])


def _terms_first(contracts: list[dict[str, Any]]) -> str:
    """The contract whose allotment terms ``current_terms`` keeps for one supplier on DAY."""
    rows = [
        restrictions.Restriction(
            f"r{i}",
            restrictions.ALLOTMENT,
            DAY,
            DAY,
            restrictions.CONTRACT_STATUS,
            units=2,
            supplier_id="dmc",
            contract_id=c["id"],
            published_at=c["published_at"],
        )
        for i, c in enumerate(contracts)
    ]
    [kept] = restrictions.current_terms(rows)
    return str(kept.contract_id)


@pytest.mark.parametrize(
    "contracts",
    [
        [{"id": LOW, "published_at": EARLY}, {"id": HIGH, "published_at": LATE}],
        [{"id": HIGH, "published_at": EARLY}, {"id": LOW, "published_at": LATE}],
        [{"id": LOW, "published_at": LATE}, {"id": HIGH, "published_at": LATE}],  # same time: the id breaks the tie
        [{"id": HIGH, "published_at": None}, {"id": LOW, "published_at": EARLY}],  # a time before none
    ],
)
def test_rates_and_terms_pick_the_same_contract_of_a_dmc(contracts: list[dict[str, Any]]) -> None:
    for order in (contracts, list(reversed(contracts))):
        assert _sql_first(order) == _terms_first(order)


def test_the_rate_choice_ranks_the_newest_contract_before_validity_and_net() -> None:
    sql = " ".join(rates.RATES_MANY_SQL.split())
    newest = newest_contract.NEWEST_CONTRACT_FIRST
    assert rates.RATE_ORDER_MANY.startswith(newest)
    per_supplier = sql.index(f"ORDER BY k.supplier_id, board_rank, {newest}")
    assert per_supplier < sql.index("x.valid_from DESC", per_supplier) < sql.index("x.net_amount ASC", per_supplier)
    # Across suppliers the order is unchanged: board, preferred DMC, net in USD.
    assert "ORDER BY s.board_rank, s.preferred DESC," in sql
    assert newest in rate_review.PREVIOUS_SQL  # the diff base of a re-import breaks ties the same way


def test_restrictions_rank_contracts_with_the_shared_key() -> None:
    row = restrictions.Restriction("r", restrictions.ALLOTMENT, DAY, DAY, restrictions.CONTRACT_STATUS,
                                   contract_id=HIGH, published_at=LATE)  # fmt: skip
    assert restrictions._contract_rank(row) == newest_contract.contract_rank(LATE, HIGH)
