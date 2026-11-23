"""Shared pieces of the pricing policy value checks (split out of :mod:`app.pricing.policy_params`, file size limit;
AA-1175): the error every check raises and the map-shaped values. :mod:`app.pricing.policy_params` re-exports
:class:`PolicyValueError`; :mod:`app.pricing.pax_bands` raises it too, so neither imports the other's module."""

from __future__ import annotations

import re
from typing import Any

COUNTRY_RE = re.compile(r"^(\*|[A-Z]{2})$")
CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


class PolicyValueError(ValueError):
    """A value outside its type or allowed range; the message says what is allowed."""


def currency_map(value: Any, key: str, max_items: int) -> dict[str, str]:
    """``{"<ISO country>" | "*": "<ISO currency>"}`` with a ``"*"`` entry, sorted by country."""
    if not isinstance(value, dict) or not value or len(value) > max_items:
        raise PolicyValueError(f"{key}: a map of country to currency is required")
    out = {}
    for country, currency in value.items():
        if not isinstance(country, str) or not COUNTRY_RE.match(country):
            raise PolicyValueError(f"{key}: {country!r} is not an ISO country code or *")
        if not isinstance(currency, str) or not CURRENCY_RE.match(currency):
            raise PolicyValueError(f"{key}: {currency!r} is not an ISO currency code")
        out[country] = currency
    if "*" not in out:
        raise PolicyValueError(f"{key}: an entry for * (every other country) is required")
    return dict(sorted(out.items()))
