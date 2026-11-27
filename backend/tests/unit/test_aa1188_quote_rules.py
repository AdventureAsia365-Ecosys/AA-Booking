"""AA-1188 (booking.b2b.v1 1.12): ``GET /terms`` carries ``quote_rules``, the pricing policy values every quote follows
(quote validity, the group size quoted on request, AA's default age bands), or null when one has no approved value."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import pytest

from app.b2b import terms
from app.pricing import policy_values

ON = date(2026, 11, 27)


class Conn:
    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        assert sql == policy_values.IN_FORCE_SQL and args == (ON,)
        return [{"param_key": k, "value": json.dumps(v), "version_no": 1} for k, v in self.values.items()]


FULL = {
    "quote_validity_days": 14,
    "group_on_request_min_pax": 9,
    "b2b_child_age_max": 11,
    "b2b_infant_age_max": 1,
}


@pytest.mark.asyncio
async def test_the_quote_rules_are_the_policy_values_in_force() -> None:
    assert await terms.quote_rules(Conn(FULL), ON) == {
        "quote_validity_days": 14,
        "group_on_request_min_pax": 9,
        "age_bands": {"child_age_max": 11, "infant_age_max": 1},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", sorted(FULL))
async def test_a_value_without_an_approved_version_answers_null(missing: str) -> None:
    values = {k: v for k, v in FULL.items() if k != missing}
    assert await terms.quote_rules(Conn(values), ON) is None
