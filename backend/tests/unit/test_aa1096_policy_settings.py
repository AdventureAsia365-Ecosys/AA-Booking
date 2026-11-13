"""AA-1096 (S279 thresholds program, lane L3, bk-056): Booking's threshold and policy values on Pricing > Policy.

The registry types added for them (a list of whole days, an amount per currency), the cross-parameter bounds, the
page sections and badge, the administrator approver group of the retention windows and the rounding reader. The
readers of each feature are tested next to it (holds, desk, ages, departures, ops lifecycle, reminders, retention).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D
from typing import Any

import pytest

from app.auth.staff import PERMISSIONS, Staff
from app.errors import Forbidden
from app.pricing import policy_admin, policy_params
from app.pricing import service as pricing
from tests.unit.fakes import POLICY_FRAGMENT, ScriptedConn, policy_rows

ON = date(2026, 11, 13)


@pytest.mark.parametrize(
    ("key", "value", "stored"),
    [
        ("agency_reminder_steps_days", [14, 1, 7, 7], [1, 7, 14]),
        ("agency_reminder_steps_days", ["3"], [3]),
        (
            "rounding_increment_by_currency",
            {"USD": 5, "EUR": "0.50", "GBP": "1", "AUD": "5", "SGD": "10"},
            {"AUD": "5", "EUR": "0.5", "GBP": "1", "SGD": "10", "USD": "5"},
        ),
        ("b2b_hold_hours", 24, 24),
        ("b2b_hold_max_extensions", 0, 0),
        ("retention_job_failed_days", "120", 120),
    ],
)
def test_new_parameter_types_store_a_canonical_value(key: str, value: Any, stored: Any) -> None:
    assert policy_params.normalise(key, value) == stored


@pytest.mark.parametrize(
    ("key", "value", "says"),
    [
        ("agency_reminder_steps_days", [], "a list of 1 to"),
        ("agency_reminder_steps_days", [0], "at least 1"),
        ("agency_reminder_steps_days", [1.5], "whole numbers"),
        ("agency_reminder_steps_days", "7", "a list of 1 to"),
        ("rounding_increment_by_currency", {"USD": "5"}, "exactly one amount per currency"),
        ("rounding_increment_by_currency", [5], "a map of currency"),
        (
            "rounding_increment_by_currency",
            {"USD": "0", "EUR": "5", "GBP": "5", "AUD": "5", "SGD": "5"},
            "at least 0.01",
        ),
        (
            "rounding_increment_by_currency",
            {"USD": "0.005", "EUR": "5", "GBP": "5", "AUD": "5", "SGD": "5"},
            "decimals",
        ),
        ("b2b_hold_max_extensions", 2, "at most 1"),
        ("retention_job_succeeded_days", 3, "at least 7"),
    ],
)
def test_new_parameters_refuse(key: str, value: Any, says: str) -> None:
    with pytest.raises(policy_params.PolicyValueError, match=says):
        policy_params.normalise(key, value)


def test_cross_bounds_keep_ages_and_retention_windows_in_order() -> None:
    current = {"b2b_child_age_max": 11, "b2b_infant_age_max": 1, "retention_job_succeeded_days": 30,
               "retention_job_failed_days": 90}  # fmt: skip
    with pytest.raises(policy_params.PolicyValueError, match="below b2b_child_age_max"):
        policy_params.normalise("b2b_infant_age_max", 4, {**current, "b2b_child_age_max": 3})
    with pytest.raises(policy_params.PolicyValueError, match="above b2b_infant_age_max"):
        policy_params.normalise("b2b_child_age_max", 2, {**current, "b2b_infant_age_max": 2})
    with pytest.raises(policy_params.PolicyValueError, match="not be shorter"):
        policy_params.normalise("retention_job_failed_days", 30, {**current, "retention_job_succeeded_days": 60})
    with pytest.raises(policy_params.PolicyValueError, match="not be longer"):
        policy_params.normalise("retention_job_succeeded_days", 120, current)
    assert policy_params.normalise("b2b_child_age_max", 12, current) == 12


def test_page_sections_and_the_not_enforced_badge() -> None:
    hold = policy_params.describe(policy_params.PARAMS["b2b_hold_hours"])
    assert hold["section"] == "holds" and hold["in_use"] is True and hold["not_enforced_reason"] is None
    gala = policy_params.describe(policy_params.PARAMS["gala_dinner_days"])
    assert gala["in_use"] is False and gala["not_enforced_reason"]
    sections = {p.section for p in policy_params.PARAMS.values()}
    assert sections == set(policy_params.SECTIONS)  # no empty section on the page


def test_retention_windows_are_approved_by_the_administrator_only() -> None:
    for key in ("retention_job_succeeded_days", "retention_job_failed_days", "retention_inbox_redact_days",
                "emergency_contact_keep_days"):  # fmt: skip
        assert policy_params.PARAMS[key].group == policy_params.ADMIN
    assert PERMISSIONS["pricing_policy.approve_admin"] == frozenset({"admin"})
    finance = Staff("fin-1", None, "Finance One", "finance", True, None)
    version = {"key": "retention_job_failed_days", "created_by": {"id": "maker-1"}, "status": "pending"}
    with pytest.raises(Forbidden) as exc:
        policy_admin._checker(finance, version)
    assert exc.value.code == "approver_group" and "administrator" in str(exc.value)
    admin = Staff("adm-1", None, "Admin One", "admin", True, None)
    assert policy_admin._checker(admin, version).key == "retention_job_failed_days"


@pytest.mark.asyncio
async def test_rounding_increments_are_the_policy_value() -> None:
    conn = ScriptedConn().on(
        POLICY_FRAGMENT,
        policy_rows(rounding_increment_by_currency='{"AUD": "5", "EUR": "1", "GBP": "5", "SGD": "5", "USD": "10"}'),
    )
    increments = await pricing.rounding_increments(conn, ON)
    assert increments["USD"] == D("10") and increments["EUR"] == D("1")
    missing = [r for r in policy_rows() if r["param_key"] != pricing.ROUNDING_KEY]
    with pytest.raises(pricing.PricingDataUnavailable):
        await pricing.rounding_increments(ScriptedConn().on(POLICY_FRAGMENT, missing), ON)
