"""Read routes of booking.b2b.v1 (AA-972): products, approved FX rates, the public "from" price for parity.

- **Products**: sellable ``booking.trip_plans`` rows whose tour is in ``catalog.v_tour`` now (live, O7: a tour that
  leaves the catalog drops out), with their bookable components (``app.b2b.components``). No prices, no content:
  content comes from ``catalog.http.v1``. Keyset pages ordered by ``source_id``; ``updated_since`` filters on the
  latest of the plan's ``updated_at``, its newest component and the ``updated_at`` of the hotels, room types and
  services behind its components, and also lists plans that stopped being sellable (``sellable = false``, no
  components). Since AA-986 L5 (root ADR 0015 decision 5) a delta (``updated_since``) also lists the plans whose tour
  was **retired** (``catalog.v_tour_retired``, CIS 274) as ``withdrawn: true`` with ``withdrawn_reason`` and
  ``updated_at`` = the retirement time; a paused tour is a temporary absence and is not listed. A tour that comes
  back is listed again (its catalog ``updated_at`` moves). Without CIS 274 the feed degrades to the AA-972 behaviour
  (a removal shows only in a periodic full resync).
- **FX rates**: the approved ``booking.fx_rates`` row in force on the asked date for every currency (plain rate, no
  buffer): the R17 "Booking's approved rates" source.
- **Price parity** (ADR 0005a decision 1, warn never block): the plan's public "from" price per person from the bk-009
  price hint when its basis is ``per_person``, converted to the asked currency (hint currency → USD at the plain rate
  → asked currency x rate x (1 + buffer)), rounded up to the cent. ``null`` when there is no per-person hint or no
  approved rate. Booking never compares or refuses anything: the CRM warns when its sell price is below.
"""

from __future__ import annotations

import base64
import binascii
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import asyncpg

from app.b2b.components import components_by_plan
from app.b2b.errors import B2BError
from app.b2b.quotes import ceil_cents, money
from app.catalog import liveness
from app.catalog.destinations import CATALOG_ERRORS
from app.pricing import engine, repository

SOURCE_SYSTEM = "cis"
PAGE = 100
PARITY_BASIS = "per_person_2pax_low_season"

# has_fixed_departures (AA-973) = a departure that is not cancelled and starts after today (UTC); departure edits touch
# the plan row, so they show in updated_at too.
# updated_at = the latest change that can alter what a product offers: the plan row (sellable, price hint; touched by
# every component replace and departure edit), any component row, the hotel / room type / service behind a component
# (status) and, since AA-986 L5, the catalog row of the tour (catalog.v_tour.updated_at follows the Master row, CIS
# 273), so a tour that comes back into the catalog (restored) is listed again by a delta sync. With updated_since,
# plans that stopped being sellable are listed too (sellable = false, no components), so a delta sync sees them go.
LIVE_PRODUCTS_SQL = """
SELECT p.id::text AS trip_plan_id, p.source_system, p.source_id, p.sellable,
       EXISTS (SELECT 1 FROM booking.departures d WHERE d.trip_plan_id = p.id AND d.status <> 'cancelled'
               AND d.start_date > (now() AT TIME ZONE 'UTC')::date) AS has_fixed_departures,
       greatest(p.updated_at, t.updated_at, (
           SELECT greatest(max(c.created_at), max(rt.updated_at), max(h.updated_at), max(s.updated_at))
           FROM booking.trip_plan_components c
           LEFT JOIN booking.hotel_room_types rt ON rt.id = c.hotel_room_type_id
           LEFT JOIN booking.hotels h ON h.id = rt.hotel_id
           LEFT JOIN booking.services s ON s.id = c.service_id
           WHERE c.trip_plan_id = p.id)) AS updated_at,
       false AS withdrawn, NULL::text AS withdrawn_reason
FROM booking.trip_plans p
JOIN LATERAL (SELECT t.updated_at FROM catalog.v_tour t
              WHERE t.source_system = p.source_system AND t.source_id = p.source_id LIMIT 1) t ON true
WHERE (p.sellable OR $2::timestamptz IS NOT NULL) AND p.source_system = 'cis'
  AND ($1::text IS NULL OR p.source_id > $1)
"""
# AA-986 L5 (root ADR 0015 decision 5): with updated_since and CIS 274, plans whose tour was RETIRED (not paused: a
# paused tour is a temporary absence and is never listed as withdrawn) are listed as withdrawn products, updated_at =
# the retirement time. A key that is back in catalog.v_tour is never listed twice (the live row wins; keyset paging on
# source_id needs one row per plan). Only plans that were offered to agencies are listed: sellable, or with components
# (built for sale); a plan never offered is not told to partners, so its key and existence stay internal.
WITHDRAWN_PRODUCTS_SQL = """
SELECT p.id::text AS trip_plan_id, p.source_system, p.source_id, false AS sellable, false AS has_fixed_departures,
       r.changed_at AS updated_at, true AS withdrawn, r.reason AS withdrawn_reason
FROM booking.trip_plans p
JOIN LATERAL (SELECT r.state, r.reason, r.changed_at FROM catalog.v_tour_retired r
              WHERE r.source_system = p.source_system AND r.source_id = p.source_id
              ORDER BY r.changed_at DESC NULLS LAST LIMIT 1) r ON r.state = 'retired'
WHERE $2::timestamptz IS NOT NULL AND p.source_system = 'cis'
  AND ($1::text IS NULL OR p.source_id > $1)
  AND (p.sellable OR EXISTS (SELECT 1 FROM booking.trip_plan_components c WHERE c.trip_plan_id = p.id))
  AND NOT EXISTS (SELECT 1 FROM catalog.v_tour t WHERE t.source_system = p.source_system AND t.source_id = p.source_id)
"""


def products_sql(with_withdrawn: bool) -> str:
    """One keyset page ordered by ``source_id``: the live products, plus the withdrawn ones when CIS 274 is there."""
    inner = LIVE_PRODUCTS_SQL + ("UNION ALL" + WITHDRAWN_PRODUCTS_SQL if with_withdrawn else "")
    return f"""
SELECT * FROM ({inner}) x
WHERE ($2::timestamptz IS NULL OR x.updated_at >= $2)
ORDER BY x.source_id
LIMIT $3
"""  # noqa: S608 — constant fragments


FX_ALL_SQL = """
SELECT DISTINCT ON (f.currency) f.currency::text AS currency, f.rate_per_usd, f.as_of, f.source
FROM booking.fx_rates f
JOIN booking.policy_approvals a ON a.fx_rate_id = f.id
WHERE f.as_of <= $1
ORDER BY f.currency, f.as_of DESC
"""

PLAN_HINT_SQL = """
SELECT p.price_hint_amount, p.price_hint_currency::text AS price_hint_currency, p.price_hint_basis
FROM booking.trip_plans p
WHERE p.source_system = $1 AND p.source_id = $2 AND p.sellable
  AND EXISTS (SELECT 1 FROM catalog.v_tour t WHERE t.source_system = p.source_system AND t.source_id = p.source_id)
"""


def _encode_cursor(source_id: str) -> str:
    return base64.urlsafe_b64encode(source_id.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str | None) -> str | None:
    if not cursor:
        return None
    try:
        return base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise B2BError("invalid", "invalid cursor") from None


async def list_products(
    pool: asyncpg.Pool, *, cursor: str | None, updated_since: datetime | None, limit: int = PAGE
) -> dict[str, Any]:
    after = _decode_cursor(cursor)
    if updated_since is not None and updated_since.tzinfo is None:
        updated_since = updated_since.replace(tzinfo=UTC)
    async with pool.acquire() as conn:
        try:
            withdrawn = updated_since is not None and await liveness.view_ready(conn, liveness.RETIRED_VIEW)
            rows = await conn.fetch(products_sql(withdrawn), after, updated_since, limit)
        except CATALOG_ERRORS:
            raise B2BError("unavailable", "the catalog is not readable right now", retry_after_s=60) from None
        parts = await components_by_plan(conn, [r["trip_plan_id"] for r in rows if r["sellable"]])
    items = [
        {
            "tour": {"source_system": r["source_system"], "source_id": r["source_id"]},
            "trip_plan_id": r["trip_plan_id"],
            "sellable": bool(r["sellable"]),
            "has_fixed_departures": bool(r["has_fixed_departures"]) and bool(r["sellable"]),
            "components": parts.get(r["trip_plan_id"], []),
            "updated_at": r["updated_at"].isoformat(),
            "withdrawn": bool(r.get("withdrawn")),
            "withdrawn_reason": r.get("withdrawn_reason"),
        }
        for r in rows
    ]
    next_cursor = _encode_cursor(rows[-1]["source_id"]) if len(rows) == limit else None
    return {"items": items, "next_cursor": next_cursor}


async def fx_rates(pool: asyncpg.Pool, on: date) -> dict[str, Any]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(FX_ALL_SQL, on)
    return {
        "date": on.isoformat(),
        "base_currency": engine.BASE_CURRENCY,
        "rates": [
            {
                "currency": r["currency"].strip(),
                "rate_per_usd": format(r["rate_per_usd"].quantize(Decimal("0.00000001")), "f"),
                "as_of": r["as_of"].isoformat(),
                "source": (r["source"] or "")[:64],
            }
            for r in rows
        ],
    }


async def price_parity(pool: asyncpg.Pool, *, source_id: str, currency: str, on: date) -> dict[str, Any]:
    async with pool.acquire() as conn:
        try:
            plan = await conn.fetchrow(PLAN_HINT_SQL, SOURCE_SYSTEM, source_id)
        except CATALOG_ERRORS:
            raise B2BError("unavailable", "the catalog is not readable right now", retry_after_s=60) from None
        if plan is None:
            raise B2BError("not_found", "no sellable trip plan with this key")
        from_price = None
        if plan["price_hint_basis"] == "per_person" and plan["price_hint_amount"] is not None:
            hint_currency = plan["price_hint_currency"].strip()
            amount = plan["price_hint_amount"]
            if hint_currency != currency:
                dated = await repository.fx_rates_in_force(conn, {hint_currency, currency}, on)
                rates = {code: d.rate for code, d in dated.items()}
                try:
                    usd = engine.to_usd(amount, hint_currency, rates)
                    sell_rate = None if currency == engine.BASE_CURRENCY else rates.get(currency)
                    if currency != engine.BASE_CURRENCY and sell_rate is None:
                        raise engine.MissingFxRate(currency)
                    amount = ceil_cents(engine.from_usd(usd, sell_rate))
                except engine.PricingError:
                    amount = None
            if amount is not None:
                from_price = money(amount, currency)
    return {
        "tour": {"source_system": SOURCE_SYSTEM, "source_id": source_id},
        "currency": currency,
        "from_price": from_price,
        "basis": PARITY_BASIS,
    }
