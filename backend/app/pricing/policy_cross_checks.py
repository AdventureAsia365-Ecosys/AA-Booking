"""The bounds a pricing policy parameter's value has against another parameter's current value
(``policy_params.Param.cross_check``; AA-1091, AA-1096). Each returns an error text or None. Kept apart from the
parameter registry (:mod:`app.pricing.policy_params`) so the registry stays one table of keys, types and ranges."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def festive_not_longer(value: Any, current: Mapping[str, Any]) -> str | None:
    days = current.get("quote_validity_days")
    if isinstance(days, int) and value > days:
        return f"must not be longer than quote_validity_days ({days})"
    return None


def validity_covers_festive(value: Any, current: Mapping[str, Any]) -> str | None:
    festive = current.get("quote_validity_festive_days")
    if isinstance(festive, int) and value < festive:
        return f"must not be shorter than quote_validity_festive_days ({festive})"
    return None


def publish_before_start(value: Any, current: Mapping[str, Any]) -> str | None:
    start = current.get("tariff_year_start")
    if isinstance(start, str) and value == start:
        return "must be another day than tariff_year_start"
    return None


def infant_below_child(value: Any, current: Mapping[str, Any]) -> str | None:
    child = current.get("b2b_child_age_max")
    if isinstance(child, int) and value >= child:
        return f"must be below b2b_child_age_max ({child})"
    return None


def child_above_infant(value: Any, current: Mapping[str, Any]) -> str | None:
    infant = current.get("b2b_infant_age_max")
    if isinstance(infant, int) and value <= infant:
        return f"must be above b2b_infant_age_max ({infant})"
    return None


def failed_not_shorter(value: Any, current: Mapping[str, Any]) -> str | None:
    succeeded = current.get("retention_job_succeeded_days")
    if isinstance(succeeded, int) and value < succeeded:
        return f"must not be shorter than retention_job_succeeded_days ({succeeded})"
    return None


def succeeded_not_longer(value: Any, current: Mapping[str, Any]) -> str | None:
    failed = current.get("retention_job_failed_days")
    if isinstance(failed, int) and value > failed:
        return f"must not be longer than retention_job_failed_days ({failed})"
    return None


def operable_default_within_max(value: Any, current: Mapping[str, Any]) -> str | None:
    longest = current.get("operable_exception_max_days")
    if isinstance(longest, int) and value > longest:
        return f"must not be longer than operable_exception_max_days ({longest})"
    return None


def operable_max_covers_default(value: Any, current: Mapping[str, Any]) -> str | None:
    default = current.get("operable_exception_default_days")
    if isinstance(default, int) and value < default:
        return f"must not be shorter than operable_exception_default_days ({default})"
    return None
