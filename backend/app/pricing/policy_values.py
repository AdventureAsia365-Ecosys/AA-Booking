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

from app.errors import Unavailable
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


async def today_utc(conn: asyncpg.Connection) -> date:
    """The database's UTC date (the pricing day of a value that is not tied to a quote day)."""
    return await conn.fetchval("SELECT (now() AT TIME ZONE 'UTC')::date")


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

    # S279 L3 (bk-056): the values that were booking.settings rows or code constants.

    def integers(self, key: str) -> tuple[int, ...]:
        return tuple(int(v) for v in self.get(key))

    def amounts(self, key: str) -> dict[str, Decimal]:
        return {code: Decimal(str(v)) for code, v in self.get(key).items()}

    @property
    def b2b_hold_hours(self) -> int:
        return self.integer("b2b_hold_hours")

    @property
    def b2b_hold_extend_hours(self) -> int:
        return self.integer("b2b_hold_extend_hours")

    @property
    def b2b_hold_max_extensions(self) -> int:
        return self.integer("b2b_hold_max_extensions")

    @property
    def b2b_rehold_limit_per_day(self) -> int:
        return self.integer("b2b_rehold_limit_per_day")

    @property
    def b2b_on_request_answer_business_days(self) -> int:
        return self.integer("b2b_on_request_answer_business_days")

    @property
    def b2b_on_request_due_soon_hours(self) -> int:
        return self.integer("b2b_on_request_due_soon_hours")

    @property
    def age_bands(self) -> tuple[int, int]:
        """``(b2b_child_age_max, b2b_infant_age_max)``."""
        return self.integer("b2b_child_age_max"), self.integer("b2b_infant_age_max")

    @property
    def departure_decision_days_default(self) -> int:
        return self.integer("departure_decision_days_default")

    @property
    def agency_reminder_steps_days(self) -> tuple[int, ...]:
        return self.integers("agency_reminder_steps_days")

    @property
    def rounding_increments(self) -> dict[str, Decimal]:
        """``rounding_increment_by_currency``: the rounding step of each selling currency."""
        return self.amounts("rounding_increment_by_currency")


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


async def load(conn: asyncpg.Connection | asyncpg.Pool, on: date) -> Policy:
    """The values in force on ``on`` (one query)."""
    return from_rows(on, list(await conn.fetch(IN_FORCE_SQL, on)))


async def load_today(conn: asyncpg.Connection) -> Policy:
    """The values in force on the database's UTC date."""
    return await load(conn, await today_utc(conn))


async def require(conn: asyncpg.Connection | asyncpg.Pool, on: date, *keys: str) -> Policy:
    """The values in force on ``on``, or :class:`PolicyUnavailable` when one of ``keys`` has none (no fallback value
    in code: a caller answers it as 503 or fails its job)."""
    policy = await load(conn, on)
    for key in keys:
        policy.get(key)
    return policy


async def require_staff(conn: asyncpg.Connection | asyncpg.Pool, on: date, *keys: str) -> Policy:
    """:func:`require` for a staff API path: a missing value is 503 ``policy_unavailable``."""
    try:
        return await require(conn, on, *keys)
    except PolicyUnavailable as exc:
        raise Unavailable("policy_unavailable", str(exc)) from None
