"""Which items have a published rate in the next year (AA-1029; split out of :mod:`app.catalog.price_readiness` for
AA-1149 so the can-operate check, :mod:`app.catalog.operability`, reads the same rule without importing the report).

An item (room type or service) counts as rated when one of its published rate spans (``cost_sheet.SPANS_SQL`` rows)
covers a day from today to :data:`HORIZON_DAYS` days ahead (or to a later ``until``: the can-operate gate of a quote
stretches the window over the line's service dates, AA-1149). Pure; no database access."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, timedelta
from typing import Any

HORIZON_DAYS = 365


def horizon(on: date) -> date:
    return on + timedelta(days=HORIZON_DAYS)


def rated_items(spans: Iterable[Mapping[str, Any]], on: date, until: date | None = None) -> dict[str, set[str]]:
    """``{item id: currencies}`` of the published rate spans (``cost_sheet.SPANS_SQL`` rows) that cover a day from
    ``on`` to the horizon, or to ``until`` when it is later."""
    end = max(horizon(on), until) if until is not None else horizon(on)
    out: dict[str, set[str]] = {}
    for span in spans:
        if span["start"] <= end and span["stop"] >= on:
            item = span["room_type_id"] or span["service_id"]
            out.setdefault(item, set()).add(str(span["currency"]).strip())
    return out
