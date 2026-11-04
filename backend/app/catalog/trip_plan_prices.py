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

AA-1029 (bk-039, root ADR 0019 b15): the cost sheet job (``app.catalog.cost_sheet``) writes **computed** rows from the
plan's components and the supplier rates in force; a staff PUT may send them back (``computed: true``, with their net
cost) and keeps them computed, an amount sent with one makes it overridden (a hand row the next recompute keeps). Every
**b2b** row carries ``rrp_amount``, the suggested retail price = round up(amount x (1 + the b2b rule's
``rrp_markup_pct``)), when the rule in force has a markup. Every change of the set is also written to the dated price
history (``app.catalog.price_history``).

The **from price** (ADR 0005 decision 6 as amended by ADR 0019 b15) = the cheapest RRP per person of a b2b row whose pax
band covers 2 and that is still valid today, USD first: the same choice as the contract view ``booking.v_from_prices``
(price.from.v1, bk-039), which TripPlanner reads.

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
from app.catalog import price_history
from app.errors import Conflict, Forbidden, Invalid, NotFound, Unavailable
from app.pricing import engine, repository
from app.pricing import service as pricing

MAX_ROWS = 200
FROM_CHANNEL = engine.Channel.B2B.value
FROM_PAX = 2
# ``amount`` is NUMERIC(12, 2); a margin at or below -100 % is refused as a typo (see ``_check_priced``).
MAX_AMOUNT = Decimal("10000000000")
MIN_MARGIN = Decimal("-1")
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
    "computed",
    "rrp_amount",
)

ROWS_SQL = """
SELECT pr.position, pr.channel, pr.season, pr.peak, pr.valid_from, pr.valid_to, pr.pax_min, pr.pax_max,
       pr.currency::text AS currency, pr.amount::text AS amount, pr.net_amount::text AS net_amount,
       pr.net_currency::text AS net_currency, pr.effective_margin::text AS effective_margin, pr.priced_on,
       pr.overridden, pr.computed, pr.rrp_amount::text AS rrp_amount, pr.created_by
FROM booking.trip_plan_prices pr
WHERE pr.trip_plan_id = $1::uuid
ORDER BY pr.position
"""

# The from price of one plan (the RRP of booking.v_from_prices, bk-039); ``{plan}`` is the uuid expression of the plan
# (a bind or an outer column).
FROM_PRICE_SQL = f"""
SELECT fp.rrp_amount::text AS amount, fp.currency::text AS currency, fp.season
FROM booking.trip_plan_prices fp
WHERE fp.trip_plan_id = {{plan}} AND fp.channel = '{FROM_CHANNEL}' AND fp.rrp_amount IS NOT NULL
  AND fp.pax_min <= {FROM_PAX} AND (fp.pax_max IS NULL OR fp.pax_max >= {FROM_PAX})
  AND fp.valid_to >= (now() AT TIME ZONE 'UTC')::date
ORDER BY (fp.currency = 'USD') DESC, fp.rrp_amount, fp.valid_from
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
    computed: bool = False

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
            computed=bool(raw.get("computed")),
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
        if row.computed and row.net_amount is None:
            raise Invalid("computed_needs_net", f"row {n}: a cost sheet row keeps its net cost", row=n)
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


def _check_priced(row: Row, amount: Decimal, margin: Decimal) -> None:
    """Refuse a priced row the table cannot hold or that is surely a typo (a clear 422, never a 500)."""
    n = row.position
    if amount <= 0:
        raise Invalid("price_missing", f"row {n}: the net cost prices to 0; enter a price", row=n)
    if amount >= MAX_AMOUNT:
        raise Invalid("price_too_high", f"row {n}: the price must be below {MAX_AMOUNT:,}", row=n)
    if margin <= MIN_MARGIN:
        # Only an override can get here: the rule itself never prices below cost. A margin of -100 % or less
        # means the price is about half the net cost or lower, which is a missing digit, not a sales decision.
        raise Invalid(
            "override_too_low",
            f"row {n}: the price is far below the net cost (margin {margin:.0%}); check the amount",
            row=n,
        )


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
    out["computed"] = bool(out.get("computed"))
    return out


async def list_prices(conn: asyncpg.Connection, staff: Staff, source_id: str) -> dict[str, Any]:
    """The plan's price matrix and its from price. A tour without a commercial row has an empty matrix."""
    if not staff.can("pricing.read"):
        raise Forbidden("forbidden", "prices are commercial data (pricing.read)")
    plan = await _plan(conn, source_id)
    if plan is None:
        return {"source_id": source_id, "items": [], "from_price": None, "reference": None, "changes": []}
    rows = [_row_out(r) for r in await conn.fetch(ROWS_SQL, plan["id"])]
    best = await conn.fetchrow(FROM_PRICE_SQL.format(plan="$1::uuid"), plan["id"])
    ref = await conn.fetchrow(REFERENCE_SQL, plan["id"])
    return {
        "source_id": source_id,
        "items": rows,
        "from_price": from_price(dict(best) if best else None),
        # AA-1029 (ADR 0005 d7): the CIS source price text, reference only (never a selling price).
        "reference": reference_out(dict(ref)) if ref else None,
        "changes": [
            {**c, "rows": int(c["rows"])} for c in await price_history.changes(conn, plan["id"], CHANGES_SHOWN)
        ],
    }


REFERENCE_SQL = """
SELECT reference_price_raw, reference_amount::text AS reference_amount, reference_currency::text AS reference_currency,
       reference_reviewed_by, reference_reviewed_at, synced_at
FROM booking.trip_plans WHERE id = $1::uuid
"""
CHANGES_SHOWN = 20


def reference_out(row: dict[str, Any]) -> dict[str, Any] | None:
    """The reviewed reference price of a plan, or None when CIS gave no source price text."""
    if not row.get("reference_price_raw"):
        return None
    return {
        "raw": row["reference_price_raw"],
        "amount": row["reference_amount"],
        "currency": (row["reference_currency"] or "").strip() or None,
        "reviewed_by": row["reference_reviewed_by"],
        "reviewed_at": row["reference_reviewed_at"],
    }


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
        bool(row.get("computed")),
        dec(row.get("rrp_amount")),
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
    markup = await _b2b_markup(conn, norm, day)
    for row in norm:
        amount, margin, priced_on = row.amount, None, None
        if row.net_amount is not None:
            quote = (await _price(conn, row, day)).quote
            amount, margin, priced_on = quote.final_total, quote.effective_margin, day
            _check_priced(row, amount, margin)
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
                "computed": row.computed,
                "rrp_amount": rrp_for(row.channel, amount, row.currency, markup),
            }
        )
    if below and not reason:
        raise Invalid(
            "override_reason_required",
            f"row(s) {', '.join(map(str, below))} are below the channel floor; give a reason",
            rows=below,
        )
    await store(
        conn,
        plan_id,
        source_id,
        final,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        created_by=staff.cis_user_id,
        action="trip_plan.prices_replaced",
        extra={"below_floor_rows": below, "below_floor_reason": reason if below else None},
        summary_suffix=f" (below floor: {reason})" if below else "",
    )
    return await list_prices(conn, staff, source_id)


async def _b2b_markup(conn: asyncpg.Connection, rows: list[Row], day: date) -> Decimal | None:
    """The RRP markup of the b2b rule in force on ``day`` (None without a b2b row, a rule or a markup)."""
    if not any(r.channel == FROM_CHANNEL for r in rows):
        return None
    try:
        dated = await repository.rule_in_force(conn, engine.Channel.B2B, day)
    except Exception:  # noqa: BLE001 — a stored rule the engine refuses: the rows still save, without an RRP
        return None
    return dated.rrp_markup_pct if dated is not None else None


def rrp_for(channel: str, amount: Decimal, currency: str, markup: Decimal | None) -> Decimal | None:
    """The suggested retail price of a b2b row (root ADR 0019 b15), else None."""
    if channel != FROM_CHANNEL or markup is None:
        return None
    try:
        return engine.rrp_price(amount, markup, currency)
    except engine.PricingError:
        return None


INSERT_SQL = """
INSERT INTO booking.trip_plan_prices (trip_plan_id, position, channel, season, peak, valid_from, valid_to, pax_min,
    pax_max, currency, amount, net_amount, net_currency, effective_margin, priced_on, overridden, computed, rrp_amount,
    created_by)
VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19)
"""
INSERT_FIELDS = (
    "position",
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
    "effective_margin",
    "priced_on",
    "overridden",
    "computed",
    "rrp_amount",
)


async def store(
    conn: asyncpg.Connection,
    plan_id: str,
    source_id: str,
    final: list[dict[str, Any]],
    *,
    actor_type: str,
    actor_ref: str,
    created_by: str | None,
    action: str,
    extra: dict[str, Any] | None = None,
    summary_suffix: str = "",
) -> bool:
    """Replace the plan's matrix with ``final`` when it differs: delete + insert, the plan's ``updated_at``, one audit
    row and the dated history, in the caller's transaction. Returns True when something changed."""
    before = [dict(r) for r in await conn.fetch(ROWS_SQL, plan_id)]
    # The same rows in another order are the same price list (a recompute lists hand rows first): no rewrite.
    if sorted(map(_comparable, before), key=repr) == sorted(map(_comparable, final), key=repr):
        return False
    await conn.execute("DELETE FROM booking.trip_plan_prices WHERE trip_plan_id = $1::uuid", plan_id)
    # A row kept from the set before keeps its author (the cost sheet keeps staff rows); new rows take created_by.
    await conn.executemany(
        INSERT_SQL, [(plan_id, *(r[f] for f in INSERT_FIELDS), r.get("created_by", created_by)) for r in final]
    )
    # The plan's updated_at moves with any commercial change (the products feed reads it, AA-972).
    await conn.execute("UPDATE booking.trip_plans SET updated_at = now() WHERE id = $1::uuid", plan_id)
    await audit.record(
        conn,
        actor_type=actor_type,
        actor_ref=actor_ref,
        action=action,
        entity_type="trip_plan",
        entity_id=plan_id,
        before={"rows": [_audit_row(r) for r in before]},
        after={"source_id": source_id, "rows": [_audit_row(r) for r in final], **(extra or {})},
        entity_label=source_id[:80],
        summary=f"Price matrix: {len(before)} -> {len(final)} rows{summary_suffix}",
    )
    await price_history.record(
        conn,
        plan_id,
        final,
        source="cost_sheet" if actor_type == "system" else "staff",
        by=actor_ref,
    )
    return True


def _audit_row(row: dict[str, Any]) -> dict[str, Any]:
    return {k: row.get(k) for k in (*KEY_FIELDS, "effective_margin")}


async def review_reference(conn: asyncpg.Connection, staff: Staff, source_id: str) -> None:
    """Mark the plan's reference price (the CIS source price text) reviewed; audited. 404 / 409 when there is none."""
    plan = await conn.fetchrow(
        "SELECT id::text AS id, reference_price_raw, reference_reviewed_at FROM booking.trip_plans"
        " WHERE source_system = 'cis' AND source_id = $1 FOR UPDATE",
        source_id,
    )
    if plan is None:
        raise NotFound("trip_plan_not_found", "this tour has no trip plan yet")
    if not plan["reference_price_raw"]:
        raise Conflict("no_reference", "CIS gave no source price for this tour")
    if plan["reference_reviewed_at"] is not None:
        return
    await conn.execute(
        "UPDATE booking.trip_plans SET reference_reviewed_by = $2, reference_reviewed_at = now() WHERE id = $1::uuid",
        plan["id"],
        staff.cis_user_id,
    )
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="trip_plan.reference_reviewed",
        entity_type="trip_plan",
        entity_id=plan["id"],
        after={"reference_price_raw": plan["reference_price_raw"]},
        entity_label=source_id[:80],
        summary="Reviewed the CIS source price (reference only)",
    )
