"""The price matrix of a trip plan (``booking.trip_plan_prices``, bk-032, AA-996; root ADR 0005 decisions 3 and 6).

One row = the selling price **per person** of the plan for one channel, one season / validity window, one pax band
and one selling currency. Two ways to fill a row, both on the existing pricing model (no second engine):

- **From a net cost**: the row carries the net cost per person (``net_amount`` in ``net_currency``). On save it is
  priced with ``app.pricing.service.price`` (the channel's pricing rule, the approved FX rates and the rounding of
  ``POST /booking/pricing/quote-preview``, peak uplift when ``peak``), as a one-line quote, on the day of the save.
  An ``amount`` sent with it is a sales override of that price (ADR 0005 decision 4, ``overridden``): below the
  channel floor it needs a ``below_floor_reason``, which the audit row keeps. The row stores the effective margin
  and the pricing day.
- **By hand**: no net cost, ``amount`` required. Nothing is computed (like the bk-009 price hint, but per band).

``PUT`` replaces the whole set in one transaction (delete + insert, role ``booking`` has SELECT, INSERT, DELETE, as
``trip_plan_components``) and writes one audit row (``trip_plan.prices_replaced``) with the rows before and after,
only when something changed. Rows of one channel and currency may not overlap (dates and pax bands), so a day and a
party size have one price per channel and currency. The plan must have its commercial row (``PUT …/commercial``).

The **from price** (ADR 0005 decision 6: lowest per-person B2C price for 2 pax) is derived on read: the cheapest row
of a B2C channel whose pax band covers 2 and that is still valid today, USD first. ``booking.v_from_prices`` (contract
price.from.v1) is a later key.

Prices and net costs are commercial data: only staff with ``pricing.read`` read them (admin, sales, contracting);
``trip_plan.write`` (admin, contracting) replaces them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import asyncpg

from app import audit
from app.auth.staff import Staff
from app.errors import Forbidden, Invalid, NotFound, Unavailable
from app.pricing import engine
from app.pricing import service as pricing

MAX_ROWS = 200
B2C_CHANNELS = ("b2c_tailor_made", "fixed_departure")
FROM_PAX = 2
# The fields that make a row (what the audit compares); the derived ones follow from them.
KEY_FIELDS = (
    "channel",
    "season",
    "peak",
    "valid_from",
    "valid_to",
    "pax_min",
    "pax_max",
    "currency",
    "amount",
    "net_amount",
    "net_currency",
    "overridden",
)

ROWS_SQL = """
SELECT pr.position, pr.channel, pr.season, pr.peak, pr.valid_from, pr.valid_to, pr.pax_min, pr.pax_max,
       pr.currency::text AS currency, pr.amount::text AS amount, pr.net_amount::text AS net_amount,
       pr.net_currency::text AS net_currency, pr.effective_margin::text AS effective_margin, pr.priced_on,
       pr.overridden
FROM booking.trip_plan_prices pr
WHERE pr.trip_plan_id = $1::uuid
ORDER BY pr.position
"""

# The from price of one plan; ``{plan}`` is the uuid expression of the plan (a bind or an outer column).
FROM_PRICE_SQL = f"""
SELECT fp.amount::text AS amount, fp.currency::text AS currency, fp.season
FROM booking.trip_plan_prices fp
WHERE fp.trip_plan_id = {{plan}} AND fp.channel IN ({", ".join(f"'{c}'" for c in B2C_CHANNELS)})
  AND fp.pax_min <= {FROM_PAX} AND (fp.pax_max IS NULL OR fp.pax_max >= {FROM_PAX})
  AND fp.valid_to >= (now() AT TIME ZONE 'UTC')::date
ORDER BY (fp.currency = 'USD') DESC, fp.amount, fp.valid_from
LIMIT 1
"""  # noqa: S608 — constants only, no input


def today() -> date:
    return datetime.now(UTC).date()


def from_price(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """The API shape of a from price (``basis`` is always per person: the matrix holds per-person prices)."""
    if not row or row.get("amount") is None:
        return None
    return {"amount": row["amount"], "currency": row["currency"], "basis": "per_person", "season": row["season"]}


@dataclass(frozen=True)
class Row:
    """One validated input row (``position`` = its 1-based place in the request)."""

    position: int
    channel: str
    season: str | None
    peak: bool
    valid_from: date
    valid_to: date
    pax_min: int
    pax_max: int | None
    currency: str
    amount: Decimal | None
    net_amount: Decimal | None
    net_currency: str | None

    def overlaps(self, other: Row) -> bool:
        if (self.channel, self.currency) != (other.channel, other.currency):
            return False
        dates = self.valid_from <= other.valid_to and other.valid_from <= self.valid_to
        top, other_top = self.pax_max or 999, other.pax_max or 999
        pax = self.pax_min <= other_top and other.pax_min <= top
        return dates and pax


def _clean(text: str | None) -> str | None:
    return (text or "").strip() or None


def normalise(rows: list[dict[str, Any]]) -> list[Row]:
    """Validate the request rows (shape is checked by the router; this checks the rules across fields and rows)."""
    if len(rows) > MAX_ROWS:
        raise Invalid("too_many_prices", f"at most {MAX_ROWS} price rows per plan")
    out: list[Row] = []
    for n, raw in enumerate(rows, start=1):
        row = Row(
            position=n,
            channel=str(raw["channel"]),
            season=_clean(raw.get("season")),
            peak=bool(raw.get("peak")),
            valid_from=raw["valid_from"],
            valid_to=raw["valid_to"],
            pax_min=int(raw["pax_min"]),
            pax_max=None if raw.get("pax_max") is None else int(raw["pax_max"]),
            currency=str(raw["currency"]),
            amount=raw.get("amount"),
            net_amount=raw.get("net_amount"),
            net_currency=raw.get("net_currency"),
        )
        if row.valid_to < row.valid_from:
            raise Invalid("invalid_validity", f"row {n}: valid_to is before valid_from", row=n)
        if row.pax_max is not None and row.pax_max < row.pax_min:
            raise Invalid("invalid_pax_band", f"row {n}: pax_max must be at least pax_min", row=n)
        if row.currency not in engine.SELLING_CURRENCIES:
            raise Invalid("invalid_currency", f"row {n}: {row.currency} is not a selling currency", row=n)
        if (row.net_amount is None) != (row.net_currency is None):
            raise Invalid("invalid_net_cost", f"row {n}: give the net cost with its currency, or neither", row=n)
        if row.net_amount is None and row.amount is None:
            raise Invalid("price_missing", f"row {n}: enter a price, or a net cost to price it from", row=n)
        for earlier in out:
            if row.overlaps(earlier):
                raise Invalid(
                    "overlapping_prices",
                    f"rows {earlier.position} and {n} overlap: same channel and currency, overlapping dates and pax"
                    " bands",
                    rows=[earlier.position, n],
                )
        out.append(row)
    return out


async def _price(conn: asyncpg.Connection, row: Row, on: date) -> pricing.PricedQuote:
    assert row.net_amount is not None and row.net_currency is not None  # noqa: S101 — callers check
    try:
        return await pricing.price(
            conn,
            channel=engine.Channel(row.channel),
            currency=row.currency,
            peak=row.peak,
            lines=[engine.CostLine(row.net_amount, row.net_currency)],
            override_total=row.amount,
            on=on,
        )
    except pricing.NoRuleInForce as exc:
        raise Invalid("pricing_rule_missing", f"row {row.position}: {exc}", row=row.position) from None
    except pricing.MissingFxRates as exc:
        raise Invalid(
            "fx_rate_missing", f"row {row.position}: {exc}", row=row.position, currencies=exc.currencies
        ) from None
    except engine.PricingError as exc:
        raise Invalid("pricing_error", f"row {row.position}: {exc}", row=row.position) from None
    except pricing.PricingDataUnavailable:
        raise Unavailable("pricing_data_unavailable", "the pricing rules or FX rates could not be read") from None


async def _plan(conn: asyncpg.Connection, source_id: str, *, lock: bool = False) -> asyncpg.Record | None:
    return await conn.fetchrow(
        "SELECT id::text AS id FROM booking.trip_plans WHERE source_system = 'cis' AND source_id = $1"  # noqa: S608
        + (" FOR UPDATE" if lock else ""),
        source_id,
    )


def _row_out(record: Any) -> dict[str, Any]:
    out = dict(record)
    out["currency"] = out["currency"].strip()
    if out["net_currency"]:
        out["net_currency"] = out["net_currency"].strip()
    out["priced"] = out["net_amount"] is not None
    return out


async def list_prices(conn: asyncpg.Connection, staff: Staff, source_id: str) -> dict[str, Any]:
    """The plan's price matrix and its from price. A tour without a commercial row has an empty matrix."""
    if not staff.can("pricing.read"):
        raise Forbidden("forbidden", "prices are commercial data (pricing.read)")
    plan = await _plan(conn, source_id)
    if plan is None:
        return {"source_id": source_id, "items": [], "from_price": None}
    rows = [_row_out(r) for r in await conn.fetch(ROWS_SQL, plan["id"])]
    best = await conn.fetchrow(FROM_PRICE_SQL.format(plan="$1::uuid"), plan["id"])
    return {"source_id": source_id, "items": rows, "from_price": from_price(dict(best) if best else None)}


def _comparable(row: dict[str, Any]) -> tuple[Any, ...]:
    def dec(value: Any) -> Decimal | None:
        return None if value is None else Decimal(str(value))

    return (
        row["channel"],
        row["season"],
        bool(row["peak"]),
        row["valid_from"],
        row["valid_to"],
        row["pax_min"],
        row["pax_max"],
        str(row["currency"]).strip(),
        dec(row["amount"]),
        dec(row["net_amount"]),
        (row["net_currency"] or "").strip() or None,
        bool(row["overridden"]),
    )


async def replace_prices(
    conn: asyncpg.Connection,
    staff: Staff,
    source_id: str,
    rows: list[dict[str, Any]],
    *,
    below_floor_reason: str | None = None,
    on: date | None = None,
) -> dict[str, Any]:
    """Replace the plan's matrix (PUT semantics). Rows with a net cost are priced on ``on`` (default today, UTC)."""
    norm = normalise(rows)
    plan = await _plan(conn, source_id, lock=True)
    if plan is None:
        raise NotFound("trip_plan_not_found", "save the commercial details of this tour first")
    plan_id = plan["id"]
    reason = _clean(below_floor_reason)
    day = on or today()

    final: list[dict[str, Any]] = []
    below: list[int] = []
    for row in norm:
        amount, margin, priced_on = row.amount, None, None
        if row.net_amount is not None:
            quote = (await _price(conn, row, day)).quote
            amount, margin, priced_on = quote.final_total, quote.effective_margin, day
            if quote.below_floor:
                below.append(row.position)
        assert amount is not None  # noqa: S101 — normalise() requires a price or a net cost
        final.append(
            {
                "position": row.position,
                "channel": row.channel,
                "season": row.season,
                "peak": row.peak,
                "valid_from": row.valid_from,
                "valid_to": row.valid_to,
                "pax_min": row.pax_min,
                "pax_max": row.pax_max,
                "currency": row.currency,
                "amount": amount,
                "net_amount": row.net_amount,
                "net_currency": row.net_currency,
                "effective_margin": margin,
                "priced_on": priced_on,
                "overridden": row.net_amount is not None and row.amount is not None,
            }
        )
    if below and not reason:
        raise Invalid(
            "override_reason_required",
            f"row(s) {', '.join(map(str, below))} are below the channel floor; give a reason",
            rows=below,
        )

    before = [dict(r) for r in await conn.fetch(ROWS_SQL, plan_id)]
    if [_comparable(r) for r in before] != [_comparable(r) for r in final]:
        await conn.execute("DELETE FROM booking.trip_plan_prices WHERE trip_plan_id = $1::uuid", plan_id)
        await conn.executemany(
            "INSERT INTO booking.trip_plan_prices (trip_plan_id, position, channel, season, peak, valid_from, valid_to,"
            " pax_min, pax_max, currency, amount, net_amount, net_currency, effective_margin, priced_on, overridden,"
            " created_by)"
            " VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17)",
            [(plan_id, *r.values(), staff.cis_user_id) for r in final],
        )
        # The plan's updated_at moves with any commercial change (the products feed reads it, AA-972).
        await conn.execute("UPDATE booking.trip_plans SET updated_at = now() WHERE id = $1::uuid", plan_id)
        await audit.record(
            conn,
            actor_type="staff",
            actor_ref=staff.cis_user_id,
            action="trip_plan.prices_replaced",
            entity_type="trip_plan",
            entity_id=plan_id,
            before={"rows": [_audit_row(r) for r in before]},
            after={
                "source_id": source_id,
                "rows": [_audit_row(r) for r in final],
                "below_floor_rows": below,
                "below_floor_reason": reason if below else None,
            },
            entity_label=source_id[:80],
            summary=f"Price matrix: {len(before)} -> {len(final)} rows"
            + (f" (below floor: {reason})" if below else ""),
        )
    return await list_prices(conn, staff, source_id)


def _audit_row(row: dict[str, Any]) -> dict[str, Any]:
    return {k: row[k] for k in (*KEY_FIELDS, "effective_margin")}
