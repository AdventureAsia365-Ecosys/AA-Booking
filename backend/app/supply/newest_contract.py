"""Which of one DMC's published contracts answers a date (AA-1153 part 2): the newest published one.

A contract exported, edited and imported back is a second published contract of the same supplier, usually with the
same validity; publishing it never expires the older one (that may still cover earlier dates, and expiring is its own
approval). Both sides of a quote therefore rank one supplier's contracts the same way, newest ``published_at`` first,
then the contract id (a stable tie-break):

- the price side (:data:`app.supply.rates.RATE_ORDER_MANY`, SQL): per supplier, among the contracts with a rate that
  matches the ask on the day, the newest one prices it (then, inside it, the rate order of before);
- the availability side (:func:`app.supply.restrictions.current_terms`, Python): per supplier, only the terms of the
  newest contract with a term on the day count.

An older contract keeps answering the dates, boards or pax bands the newer one does not cover. Both orders are written
here once; ``tests/unit/test_aa1153_newest_contract.py`` checks they agree."""

from __future__ import annotations

from datetime import datetime

# SQL ORDER BY fragment over ``booking.supplier_contracts k``: the first row is the newest published contract. uuid
# ordering in PostgreSQL is byte order, the same as the order of its lower-case text form (the Python key below).
NEWEST_CONTRACT_FIRST = "k.published_at DESC NULLS LAST, k.id DESC"


def contract_rank(published_at: datetime | None, contract_id: str | None) -> tuple[bool, float, str]:
    """The Python key of :data:`NEWEST_CONTRACT_FIRST`: the greatest key is the contract that SQL order puts first
    (a published time before none, the newest time, then the greatest id)."""
    return (published_at is not None, published_at.timestamp() if published_at else 0.0, contract_id or "")
