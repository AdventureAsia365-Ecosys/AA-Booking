"""AA-1091 (bk-050, root ADR 0005b d15): the pricing policy parameters without a database.

The registry (types, ranges, cross-parameter bounds), the reader (no fallback value), the derived statuses, the group
rule of the quote engine and the hold keeping a line the quote answered on request. The HTTP flow with maker-checker
runs on a real PostgreSQL (tests/integration/test_aa1091_pricing_policy.py)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from app.b2b import departures, quotes
from app.pricing import policy_admin, policy_params, policy_values
from tests.unit.fakes import POLICY_SEED, policy_rows
from tests.unit.test_b2b_hold_flow import place, place_conn, quote_lines

ON = date(2026, 11, 12)


# ── Registry ─────────────────────────────────────────────────────────────────


def test_every_parameter_has_a_known_type_group_and_permission() -> None:
    for key, p in policy_params.PARAMS.items():
        assert p.key == key and p.type in policy_params.TYPES
        assert p.group in policy_params.APPROVE_PERMISSION
        assert p.label and p.help and p.question
        if p.type in (policy_params.INTEGER, policy_params.DECIMAL):
            assert p.minimum is not None and p.maximum is not None, key


def test_the_unit_fakes_seed_every_parameter_with_a_valid_value() -> None:
    assert set(POLICY_SEED) == set(policy_params.PARAMS)
    values = {k: json.loads(v) for k, v in POLICY_SEED.items()}
    for key, value in values.items():
        assert policy_params.normalise(key, value, values) == value, key


@pytest.mark.parametrize(
    ("key", "value", "stored"),
    [
        ("quote_validity_days", 10, 10),
        ("quote_validity_days", "10", 10),
        ("surcharge_fx_threshold_pct", "5.50", "5.5"),
        ("surcharge_fx_threshold_pct", 7, "7"),
        ("provisional_cis_ref_quotable", True, True),
        ("tariff_year_start", "02-29", "02-29"),
        ("gala_dinner_days", ["12-31", "12-24", "12-31"], ["12-24", "12-31"]),
        ("surcharge_causes", ["tax", "fx", "tax"], ["fx", "tax"]),
        ("surcharge_causes", [], []),
        ("agency_currency_by_country", {"VN": "VND", "*": "USD", "TH": "THB"}, {"*": "USD", "TH": "THB", "VN": "VND"}),
    ],
)
def test_normalise_accepts_and_stores(key: str, value: Any, stored: Any) -> None:
    assert policy_params.normalise(key, value) == stored


@pytest.mark.parametrize(
    ("key", "value", "says"),
    [
        ("quote_validity_days", 31, "at most 30"),
        ("quote_validity_days", 0, "at least 1"),
        ("quote_validity_days", 2.5, "whole number"),
        ("quote_validity_days", True, "number"),
        ("surcharge_notice_days", 19, "at least 20"),
        ("surcharge_free_cancel_pct", "8.5", "at most 8"),
        ("surcharge_fx_threshold_pct", "5.123", "2 decimals"),
        ("surcharge_fx_threshold_pct", "NaN", "number"),
        ("provisional_cis_ref_quotable", "yes", "true or false"),
        ("tariff_year_start", "13-01", "MM-DD"),
        ("tariff_year_start", "02-30", "not a day"),
        ("surcharge_causes", ["fx", "weather"], "weather"),
        ("agency_currency_by_country", {"VN": "VND"}, r"\*"),
        ("agency_currency_by_country", {"*": "usd"}, "currency"),
        ("agency_currency_by_country", {"Viet": "VND", "*": "USD"}, "country"),
        ("no_such_key", 1, "unknown"),
    ],
)
def test_normalise_refuses(key: str, value: Any, says: str) -> None:
    with pytest.raises(policy_params.PolicyValueError, match=says):
        policy_params.normalise(key, value)


def test_cross_parameter_bounds_use_the_values_in_force() -> None:
    current = {"quote_validity_days": 14, "quote_validity_festive_days": 3, "tariff_year_start": "11-01"}
    with pytest.raises(policy_params.PolicyValueError, match="quote_validity_days"):
        policy_params.normalise("quote_validity_festive_days", 15, current)
    with pytest.raises(policy_params.PolicyValueError, match="festive"):
        policy_params.normalise("quote_validity_days", 2, current)
    with pytest.raises(policy_params.PolicyValueError, match="another day"):
        policy_params.normalise("tariff_next_publish_by", "11-01", current)
    assert policy_params.normalise("quote_validity_festive_days", 14, current) == 14


def test_describe_shows_no_value() -> None:
    d = policy_params.describe(policy_params.PARAMS["quote_validity_days"])
    assert d["in_use"] is True and d["minimum"] == "1" and d["maximum"] == "30" and "value" not in d
    assert policy_params.describe(policy_params.PARAMS["gala_dinner_days"])["in_use"] is False


# ── Reader ───────────────────────────────────────────────────────────────────


def test_reader_types_and_no_fallback() -> None:
    policy = policy_values.from_rows(ON, policy_rows(quote_validity_days="7"))
    assert policy.quote_validity_days == 7 and policy.group_on_request_min_pax == 9
    assert policy.fraction("provisional_uplift_pct") == policy.decimal("provisional_uplift_pct") / 100
    assert policy.flag("provisional_cis_ref_quotable") is False
    missing = policy_values.from_rows(ON, [r for r in policy_rows() if r["param_key"] != "quote_validity_days"])
    with pytest.raises(policy_values.PolicyUnavailable):
        _ = missing.quote_validity_days
    with pytest.raises(KeyError):
        missing.get("not_a_parameter")
    later = policy_values.from_rows(ON, [*policy_rows(), {"param_key": "added_later", "value": "1", "version_no": 1}])
    assert "added_later" not in later.values


# ── Derived statuses ─────────────────────────────────────────────────────────


def _row(vid: str, version_no: int, effective: date, **steps: Any) -> dict[str, Any]:
    base = {"withdrawn_at": None, "rejected_at": None, "approved_at": None, "submitted_at": None}
    return {"id": vid, "param_key": "k", "version_no": version_no, "effective_from": effective, **base, **steps}


def test_statuses_split_published_by_day_and_approval() -> None:
    t = datetime(2026, 11, 1, tzinfo=UTC)
    rows = [
        _row("v1", 1, date(2026, 1, 1), submitted_at=t, approved_at=t),
        _row("v2", 2, ON, submitted_at=t, approved_at=t + timedelta(hours=1)),
        _row("v3", 3, ON, submitted_at=t, approved_at=t + timedelta(hours=2)),  # same day, approved later: wins
        _row("v4", 4, ON + timedelta(days=5), submitted_at=t, approved_at=t),
        _row("v5", 5, ON, submitted_at=t),
        _row("v6", 6, ON),
        _row("v7", 7, ON, submitted_at=t, rejected_at=t),
        _row("v8", 8, ON, withdrawn_at=t),
    ]
    assert policy_admin._statuses(rows, ON) == {
        "v1": "superseded",
        "v2": "superseded",
        "v3": "in_force",
        "v4": "scheduled",
        "v5": "pending",
        "v6": "draft",
        "v7": "rejected",
        "v8": "withdrawn",
    }


# ── Quote engine: groups on request; holds keep it ───────────────────────────


@pytest.mark.parametrize(
    ("availability", "component", "adult", "child", "expected"),
    [
        (departures.AVAILABLE, "tp:00000000-0000-4000-8000-000000000001", 8, 1, departures.ON_REQUEST),
        (departures.AVAILABLE, "tp:00000000-0000-4000-8000-000000000001", 7, 1, departures.AVAILABLE),
        (departures.SOLD_OUT, "tp:00000000-0000-4000-8000-000000000001", 12, 0, departures.SOLD_OUT),
        (departures.AVAILABLE, "sv:00000000-0000-4000-8000-000000000002", 12, 0, departures.AVAILABLE),
    ],
)
def test_group_availability(availability: str, component: str, adult: int, child: int, expected: str) -> None:
    assert quotes.group_availability(availability, component, adult, child, 9) == expected


@pytest.mark.asyncio
async def test_a_line_quoted_on_request_is_held_on_request() -> None:
    lines = quote_lines()
    lines[0]["availability"] = departures.ON_REQUEST  # e.g. a group of the policy's size on a departure with seats
    conn = place_conn(lines=lines)
    await place(conn)
    inserted = conn.sql("INSERT INTO booking.b2b_hold_lines")
    assert inserted[0][5] == departures.ON_REQUEST
    # Seats are still counted on the departure (the hold keeps them while ops answer).
    assert inserted[0][4] == 3
