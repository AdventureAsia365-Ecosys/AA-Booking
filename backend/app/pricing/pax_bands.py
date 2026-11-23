"""The group-size bands of a computed price matrix as a pricing policy parameter (AA-1175, S288, bk-066).

The cost sheet (:mod:`app.catalog.cost_sheet`) prices every start-date window once per **band** of travellers, at the
band's smallest party (fixed costs are shared by the fewest people there, so the price covers the whole band). The
bands were a code constant (root ADR 0005 decision 3); the tech lead's rule (S278/S279) makes them the parameter
``price_pax_bands`` of Pricing > Policy (maker-checker, versioned, audited), read for the recompute day.

Stored shape (``pricing_policy.value``): a JSON list of ``[smallest, largest]`` pairs, ``largest`` null = open (only the
last band), e.g. ``[[1, 1], [2, 2], [3, 3], [4, 4], [5, 7], [8, 10], [11, null]]``. Valid bands start at 1, follow each
other with no gap or overlap, stay inside the parameter's range and number at most :data:`MAX_BANDS`.

This module is plain data and pure functions (no import of :mod:`app.pricing.policy_params`, which builds a ``Param``
from :data:`SPECS` and calls :func:`normalise`; both raise :class:`~app.pricing.policy_shapes.PolicyValueError`).
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from app.pricing.policy_shapes import PolicyValueError

KEY = "price_pax_bands"
TYPE = "pax_bands"
# Bounded by the matrix size: 200 rows (app.catalog.trip_plan_prices.MAX_ROWS) / 2 channels / 8 bands = 12 windows.
MAX_BANDS = 8  # technical-constant: matrix size bound (rows per start-date window), not a business value

Band = tuple[int, int | None]

SPECS: tuple[dict[str, Any], ...] = (
    {
        "key": KEY,
        "type": TYPE,
        "unit": "travellers",
        "question": "S288",
        "group": "pricing",
        "label": "Group size bands",
        "help": "The group sizes of the computed price matrix; each band is priced at its smallest group.",
        "used_by": "app.catalog.cost_sheet",
        "section": "prices",
        "minimum": Decimal("1"),
        "maximum": Decimal("60"),
    },
)


def _whole(item: Any, key: str, n: int) -> int:
    if isinstance(item, bool) or not isinstance(item, int):
        raise PolicyValueError(f"{key}: band {n}: whole numbers are required")
    return item


def _band(item: Any, key: str, n: int, last: bool) -> Band:
    if not isinstance(item, (list, tuple)) or len(item) != 2:
        raise PolicyValueError(f"{key}: band {n}: a pair [smallest, largest] is required")
    low = _whole(item[0], key, n)
    if item[1] is None:
        if not last:
            raise PolicyValueError(f"{key}: band {n}: only the last band may be open (no largest group)")
        return low, None
    high = _whole(item[1], key, n)
    if high < low:
        raise PolicyValueError(f"{key}: band {n}: the largest group {high} is smaller than the smallest {low}")
    return low, high


def _in_range(key: str, n: int, top: int, minimum: Decimal | None, maximum: Decimal | None) -> None:
    """The smallest group of every band is already >= 1 (bands start at 1 and rise), so the top of each is checked."""
    if minimum is not None and top < minimum:
        raise PolicyValueError(f"{key}: band {n}: at least {minimum}")
    if maximum is not None and top > maximum:
        raise PolicyValueError(f"{key}: band {n}: at most {maximum}")


def normalise(value: Any, key: str, minimum: Decimal | None, maximum: Decimal | None) -> list[list[int | None]]:
    """The stored shape of ``value`` (see the module docstring), or :class:`PolicyValueError` naming ``key``."""
    if not isinstance(value, list) or not value or len(value) > MAX_BANDS:
        raise PolicyValueError(f"{key}: a list of 1 to {MAX_BANDS} bands is required")
    out: list[list[int | None]] = []
    expected = 1
    for n, item in enumerate(value, start=1):
        low, high = _band(item, key, n, n == len(value))
        if low != expected:
            raise PolicyValueError(f"{key}: band {n} must start at {expected} (from 1, no gap or overlap)")
        _in_range(key, n, low if high is None else high, minimum, maximum)
        out.append([low, high])
        expected = (high or low) + 1
    return out


def as_bands(stored: Any) -> tuple[Band, ...]:
    """The stored value as typed bands (assumes a value :func:`normalise` accepted)."""
    return tuple((int(low), None if high is None else int(high)) for low, high in stored)


def index_for(bands: Sequence[Band], party: int) -> int | None:
    """The position of the band that holds ``party`` travellers; None when no band does (above a capped top band)."""
    for i, (low, high) in enumerate(bands):
        if low <= party and (high is None or party <= high):
            return i
    return None


def band_for(bands: Sequence[Band], party: int) -> Band | None:
    """The band that holds ``party`` travellers, or None."""
    i = index_for(bands, party)
    return None if i is None else bands[i]


def label(band: Band) -> str:
    """``"4"``, ``"5–7"`` or ``"11+"``."""
    low, high = band
    if high is None:
        return f"{low}+"
    return str(low) if high == low else f"{low}–{high}"


def describe(bands: Sequence[Band]) -> str:
    """``"1, 2, 3, 4, 5–7, 8–10, 11+"``."""
    return ", ".join(label(b) for b in bands)


def workbook_line(stored: Any) -> str:
    """The price setup workbook's README line naming the bands in force (``stored`` = the policy value, None = none)."""
    if not stored:
        return "Prices worked out from a cost: no group size bands are in force (Pricing > Policy), none are computed."
    return (
        f"Prices worked out from a cost use the group sizes {describe(as_bands(stored))} (travellers; Pricing > "
        "Policy, Group size bands). Hand prices may use any Pax min / Pax max."
    )
