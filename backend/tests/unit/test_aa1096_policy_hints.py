"""AA-1096 (S280 settings UI audit): the page hints of the pricing policy parameters stay complete and true.

Every parameter with a cross-parameter bound has it described for the page (and the description matches what
``normalise`` refuses), and every "not enforced yet" reason has its Vietnamese text."""

from __future__ import annotations

from datetime import date

import pytest

from app.errors import Invalid
from app.pricing import policy, policy_hints, policy_params

INT_PAIRS = {k: v for k, v in policy_hints.CROSS_BOUNDS.items() if policy_params.PARAMS[k].type == "integer"}


def test_every_cross_check_has_its_bound_and_back() -> None:
    with_check = {k for k, p in policy_params.PARAMS.items() if p.cross_check is not None}
    assert set(policy_hints.CROSS_BOUNDS) == with_check
    for key, (relation, other) in policy_hints.CROSS_BOUNDS.items():
        assert relation in policy_hints.RELATIONS and other in policy_params.PARAMS and other != key


@pytest.mark.parametrize("key", sorted(INT_PAIRS))
def test_integer_bound_matches_normalise(key: str) -> None:
    relation, other = INT_PAIRS[key]
    ok = {"lt": lambda a, b: a < b, "lte": lambda a, b: a <= b, "gt": lambda a, b: a > b, "gte": lambda a, b: a >= b}
    param = policy_params.PARAMS[key]
    low, high = int(param.minimum or 0), int(param.maximum or 50)
    for other_value in (low + 1, (low + high) // 2):
        for value in range(low, min(high, low + 40) + 1):
            try:
                policy_params.normalise(key, value, {other: other_value})
                accepted = True
            except policy_params.PolicyValueError:
                accepted = False
            assert accepted == ok[relation](value, other_value), (key, value, other_value)


def test_ne_bound_matches_normalise() -> None:
    with pytest.raises(policy_params.PolicyValueError):
        policy_params.normalise("tariff_next_publish_by", "11-01", {"tariff_year_start": "11-01"})
    assert policy_params.normalise("tariff_next_publish_by", "10-01", {"tariff_year_start": "11-01"}) == "10-01"


def test_every_pending_reason_has_vietnamese() -> None:
    for param in policy_params.PARAMS.values():
        hints = policy_hints.hints(param)
        if param.pending:
            assert hints["not_enforced_reason_vi"], param.key
        else:
            assert hints["not_enforced_reason_vi"] is None
    assert set(policy_hints.PENDING_VI) == {p.pending for p in policy_params.PARAMS.values() if p.pending}


def test_new_policy_row_never_takes_effect_backwards() -> None:
    policy.check_effective_day(date(2026, 11, 14), date(2026, 11, 14))
    with pytest.raises(Invalid) as exc:
        policy.check_effective_day(date(2026, 11, 13), date(2026, 11, 14))
    assert exc.value.code == "effective_in_past"
