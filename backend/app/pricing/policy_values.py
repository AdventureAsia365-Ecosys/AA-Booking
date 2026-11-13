"""The single reader of the pricing policy parameters (root ADR 0005b decision 15; AA-1091, bk-050).

Every pricing, quote, deposit, cancellation and validity code path reads a policy value through :func:`load` and the
typed accessors of :class:`Policy`; none keeps its own copy of a value (the CI guard
``tests/unit/test_aa1091_policy_guard.py`` fails on a literal policy number in those modules).

The value in force on a day ``on`` is, per key, the newest **approved** version (``booking.pricing_policy_events``
step ``approved``) whose ``effective_from`` is on or before ``on``; a tie on the day goes to the later approval, then
the higher version number. A pending, rejected or withdrawn version never counts. There is no fallback value in code:
a key without an approved version in force raises :class:`PolicyUnavailable`, which the B2B API answers as 503
``unavailable`` (AA-side data missing, like a missing pricing rule).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import asyncpg

from app.pricing import policy_params

IN_FORCE_SQL = """
SELECT DISTINCT ON (p.param_key) p.param_key, p.value::text AS value, p.version_no, p.id::text AS id,
       p.effective_from
FROM booking.pricing_policy p
JOIN booking.pricing_policy_events a ON a.policy_id = p.id AND a.event = 'approved'
WHERE p.effective_from <= $1
ORDER BY p.param_key, p.effective_from DESC, a.created_at DESC, p.version_no DESC
"""


class PolicyUnavailable(LookupError):
    """A policy parameter has no approved version in force on the day asked."""


def decode(raw: Any) -> Any:
    return json.loads(raw) if isinstance(raw, str) else raw


@dataclass(frozen=True)
class Policy:
    """The parameter values in force on :attr:`on`, by key (stored shapes, see :mod:`app.pricing.policy_params`)."""

    on: date
    values: dict[str, Any]
    versions: dict[str, int]

    def get(self, key: str) -> Any:
        if key not in policy_params.PARAMS:
            raise KeyError(f"unknown policy parameter {key}")
        if key not in self.values:
            raise PolicyUnavailable(f"no approved value of {key} is in force on {self.on}")
        return self.values[key]

    def integer(self, key: str) -> int:
        return int(self.get(key))

    def decimal(self, key: str) -> Decimal:
        return Decimal(str(self.get(key)))

    def fraction(self, key: str) -> Decimal:
        """A percentage parameter as a fraction ("5" -> 0.05)."""
        return self.decimal(key) / 100

    def flag(self, key: str) -> bool:
        return bool(self.get(key))

    # The accessors the code paths use (one name per parameter they read).

    @property
    def quote_validity_days(self) -> int:
        return self.integer("quote_validity_days")

    @property
    def quote_validity_festive_days(self) -> int:
        return self.integer("quote_validity_festive_days")

    @property
    def group_on_request_min_pax(self) -> int:
        return self.integer("group_on_request_min_pax")

    @property
    def diff_highlight_pct(self) -> Decimal:
        return self.decimal("diff_highlight_pct")

    @property
    def tariff_year_start(self) -> str:
        return str(self.get("tariff_year_start"))


def from_rows(on: date, rows: list[Any]) -> Policy:
    values: dict[str, Any] = {}
    versions: dict[str, int] = {}
    for row in rows:
        key = row["param_key"]
        if key not in policy_params.PARAMS:
            continue  # a key the code no longer knows (a newer row written by a later release) is ignored
        values[key] = decode(row["value"])
        versions[key] = int(row["version_no"])
    return Policy(on=on, values=values, versions=versions)


async def load(conn: asyncpg.Connection, on: date) -> Policy:
    """The values in force on ``on`` (one query)."""
    return from_rows(on, list(await conn.fetch(IN_FORCE_SQL, on)))
