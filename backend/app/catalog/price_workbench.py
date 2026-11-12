"""Price setup workbench (AA-1091): every trip plan with what it still needs before an agency can buy it, the next step
per plan and the bulk actions that are safe on many plans at once. Built on the price readiness report
(:mod:`app.catalog.price_readiness`, the same statuses and reasons, so the two pages never disagree) plus three facts
that report does not read: the plan's upcoming fixed departures, its b2b price rows still valid and its reference /
provisional price.

**Checks per plan** (``checks``; each ``ok`` true or false):

- ``components``: the plan has bookable components (hotel room types, services);
- ``rates``: every component has a published rate on some day of the next 365 days (``rated`` of ``total``);
- ``fx``: every rate currency of a component has an approved FX rate today (``missing`` lists the others);
- ``rule``: an approved b2b or fixed-departure pricing rule is in force today (one fact for every plan);
- ``price``: the matrix has a price row still valid (``kind`` ``rates`` = computed from supplier rates, ``hand`` = typed
  by staff, ``provisional`` = only the CIS reference copied as the price hint, see below; ``ok`` for the first two);
- ``sellable``: the commercial switch;
- ``departures``: upcoming fixed departures (not cancelled, after today). Optional: a tailor-made tour has none, so it
  never decides the next step, but it can be filtered on.

**Next step** (``next_step``, the first that applies): ``add_components``, ``fix_inactive``, ``load_rates``,
``approve_fx``, ``approve_rule``, ``recompute`` (the cost sheet prices it; the next run stores the rows),
``make_sellable``, then ``done``. Hand prices alone never make a plan ``done``: an agency quote prices the components
(``app.b2b.components``), so a plan without them cannot be quoted whatever its matrix says.

**Provisional price** (Linear scope item 4): a plan with no b2b price row still valid can show the CIS reference price
(``trip_plans.reference_amount`` / ``_currency``, read from ``catalog.v_tour_price_reference_v2`` (else v1) by the sync)
as its **price hint** (``price_hint_*``, basis per person). The hint is only ever a display price: the public price
parity read (``app.b2b.reads.price_parity``) falls back to it as the RRP while no b2b row exists; a quote, a hold, the
agency tariff and ``booking.v_from_prices`` never read it. ``provisional`` in a row = the hint shown while there is
no supplier price (``supplier_price`` false), flagged so in every UI.

**Bulk actions** (:func:`apply_action`, ``trip_plan.write``; at most :data:`MAX_ACTION_PLANS` plans, one transaction;
each changed plan gets its own audit row; a plan that does not qualify is ``skipped`` with a reason, never an error):
``make_sellable`` (only a plan with components whose tour is in the catalog), ``make_not_sellable``,
``apply_provisional`` (the reference becomes the hint when the plan has no b2b price row and no other hint; marks the
reference reviewed), ``clear_provisional`` (only a hint equal to the reference), ``recompute`` (the plan's cost sheet
now; at most :data:`MAX_RECOMPUTE` plans a call).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import asyncpg

from app import audit
from app.auth.staff import Staff
from app.catalog import cost_sheet, price_readiness
from app.catalog.destinations import CATALOG_ERRORS
from app.errors import Invalid

MAX_ACTION_PLANS = 200
MAX_RECOMPUTE = 50

ADD_COMPONENTS = "add_components"
FIX_INACTIVE = "fix_inactive"
LOAD_RATES = "load_rates"
APPROVE_FX = "approve_fx"
APPROVE_RULE = "approve_rule"
RECOMPUTE = "recompute"
MAKE_SELLABLE = "make_sellable"
DONE = "done"
NEXT_STEPS: tuple[str, ...] = (
    ADD_COMPONENTS,
    FIX_INACTIVE,
    LOAD_RATES,
    APPROVE_FX,
    APPROVE_RULE,
    RECOMPUTE,
    MAKE_SELLABLE,
    DONE,
)
# The readiness reason that each step answers, in the cost sheet's order.
STEP_OF_REASON = {
    cost_sheet.NO_COMPONENTS: ADD_COMPONENTS,
    cost_sheet.INACTIVE_COMPONENT: FIX_INACTIVE,
    price_readiness.MISSING_RATES: LOAD_RATES,
    price_readiness.MISSING_FX: APPROVE_FX,
    cost_sheet.NO_RULE: APPROVE_RULE,
}
CHECKS: tuple[str, ...] = ("components", "rates", "fx", "rule", "price", "sellable", "departures")

ACTIONS: tuple[str, ...] = ("make_sellable", "make_not_sellable", "apply_provisional", "clear_provisional", "recompute")

# Plan facts the readiness report does not read: departures, valid b2b rows, reference and hint. One set query.
EXTRA_SQL = """
SELECT p.id::text AS plan_id, p.reference_price_raw, p.reference_amount,
       p.reference_currency::text AS reference_currency,
       (p.reference_reviewed_at IS NOT NULL) AS reference_reviewed,
       p.price_hint_amount, p.price_hint_currency::text AS price_hint_currency, p.price_hint_basis,
       coalesce(d.n, 0) AS departures, d.next_start, coalesce(b.n, 0) AS b2b_rows
FROM booking.trip_plans p
LEFT JOIN (SELECT trip_plan_id, count(*) AS n, min(start_date) AS next_start FROM booking.departures
           WHERE status <> 'cancelled' AND start_date > $1 GROUP BY trip_plan_id) d ON d.trip_plan_id = p.id
LEFT JOIN (SELECT trip_plan_id, count(*) AS n FROM booking.trip_plan_prices
           WHERE channel = 'b2b' AND valid_to >= $1 GROUP BY trip_plan_id) b ON b.trip_plan_id = p.id
WHERE p.source_system = 'cis' AND ($2::uuid[] IS NULL OR p.id = ANY($2::uuid[]))
"""


# ── Pure part ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Extra:
    """What the workbench adds to a readiness row (one per plan)."""

    departures: int = 0
    next_departure: date | None = None
    b2b_rows: int = 0
    reference_raw: str | None = None
    reference_amount: Decimal | None = None
    reference_currency: str | None = None
    reference_reviewed: bool = False
    hint_amount: Decimal | None = None
    hint_currency: str | None = None
    hint_basis: str | None = None


def _money(amount: Decimal | None, currency: str | None) -> dict[str, str] | None:
    if amount is None or not currency:
        return None
    return {"amount": format(Decimal(amount).quantize(Decimal("0.01")), "f"), "currency": currency.strip()}


def is_provisional(extra: Extra) -> bool:
    """The hint is shown as the price while no b2b price row is valid (no supplier price yet)."""
    return extra.hint_amount is not None and extra.hint_basis == "per_person" and extra.b2b_rows == 0


def hint_is_reference(extra: Extra) -> bool:
    """The hint is the reference copied as it is (what ``apply_provisional`` writes)."""
    return (
        extra.hint_amount is not None
        and extra.reference_amount is not None
        and Decimal(extra.hint_amount) == Decimal(extra.reference_amount)
        and (extra.hint_currency or "").strip() == (extra.reference_currency or "").strip()
        and extra.hint_basis == "per_person"
    )


def next_step(row: Mapping[str, Any]) -> str:
    """The first thing the plan still needs (see the module doc)."""
    step = STEP_OF_REASON.get(row["reason"] or "")
    if step is not None:
        return step
    if row["status"] == price_readiness.PRICED and row.get("pending_recompute"):
        return RECOMPUTE
    if row["status"] not in (price_readiness.PRICED, price_readiness.HAND_PRICED):
        # No reason and no price: only the cost sheet's own "no common window" case gets here.
        return LOAD_RATES
    if not row["sellable"]:
        return MAKE_SELLABLE
    return DONE


def checks(row: Mapping[str, Any], extra: Extra, *, has_rule: bool) -> dict[str, dict[str, Any]]:
    """The seven checks of a readiness row (see the module doc)."""
    total = int(row["components"])
    unrated = len(row["components_without_rate"])
    if row["computed_rows"]:
        kind: str | None = "rates"
    elif row["hand_rows"]:
        kind = "hand"
    elif is_provisional(extra):
        kind = "provisional"
    else:
        kind = None
    reason = row["reason"]
    return {
        "components": {"ok": total > 0 and reason != cost_sheet.INACTIVE_COMPONENT, "count": total},
        "rates": {
            "ok": total > 0 and unrated == 0 and reason != price_readiness.MISSING_RATES,
            "rated": max(total - unrated, 0),
            "total": total,
        },
        "fx": {"ok": not row["missing_fx"], "missing": list(row["missing_fx"])},
        "rule": {"ok": has_rule},
        "price": {"ok": kind in ("rates", "hand"), "kind": kind},
        "sellable": {"ok": bool(row["sellable"])},
        "departures": {
            "ok": extra.departures > 0,
            "count": extra.departures,
            "next": extra.next_departure,
        },
    }


def missing(row_checks: Mapping[str, Mapping[str, Any]]) -> list[str]:
    return [name for name in CHECKS if not row_checks[name]["ok"]]


def workbench_row(row: Mapping[str, Any], extra: Extra, *, has_rule: bool) -> dict[str, Any]:
    """One readiness row with the workbench's checks, next step, reference and provisional price."""
    row_checks = checks(row, extra, has_rule=has_rule)
    return {
        **row,
        "checks": row_checks,
        "missing": missing(row_checks),
        "next_step": next_step(row),
        "reference": None
        if not extra.reference_raw and extra.reference_amount is None
        else {
            "raw": extra.reference_raw,
            **(_money(extra.reference_amount, extra.reference_currency) or {"amount": None, "currency": None}),
            "reviewed": extra.reference_reviewed,
        },
        "provisional": (
            {**(_money(extra.hint_amount, extra.hint_currency) or {}), "from_reference": hint_is_reference(extra)}
            if is_provisional(extra)
            else None
        ),
        "supplier_price": extra.b2b_rows > 0,
    }


def matches(
    row: Mapping[str, Any],
    *,
    status: str | None,
    country: str | None,
    q: str | None,
    missing_check: str | None,
    step: str | None,
) -> bool:
    if not price_readiness.matches(row, status=status, country=country, q=q):
        return False
    if missing_check and missing_check not in row["missing"]:
        return False
    return not (step and row["next_step"] != step)


def summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The readiness summary plus counts per next step and per missing check (every plan)."""
    out = price_readiness.summary(rows)
    out["by_next_step"] = {s: 0 for s in NEXT_STEPS}
    out["by_missing"] = {c: 0 for c in CHECKS}
    out["provisional"] = 0
    for row in rows:
        out["by_next_step"][row["next_step"]] += 1
        for name in row["missing"]:
            out["by_missing"][name] += 1
        out["provisional"] += int(row["provisional"] is not None)
    return out


# ── Database part ────────────────────────────────────────────────────────────


def _extra(r: Mapping[str, Any]) -> Extra:
    return Extra(
        departures=int(r["departures"]),
        next_departure=r["next_start"],
        b2b_rows=int(r["b2b_rows"]),
        reference_raw=r["reference_price_raw"],
        reference_amount=r["reference_amount"],
        reference_currency=(r["reference_currency"] or "").strip() or None,
        reference_reviewed=bool(r["reference_reviewed"]),
        hint_amount=r["price_hint_amount"],
        hint_currency=(r["price_hint_currency"] or "").strip() or None,
        hint_basis=r["price_hint_basis"],
    )


async def extras(conn: asyncpg.Connection, on: date, plan_ids: Sequence[str] | None = None) -> dict[str, Extra]:
    ids = list(plan_ids) if plan_ids is not None else None
    return {r["plan_id"]: _extra(r) for r in await conn.fetch(EXTRA_SQL, on, ids)}


async def rows(conn: asyncpg.Connection, on: date) -> tuple[list[dict[str, Any]], price_readiness.Facts]:
    """Every plan as a workbench row (readiness classification + the workbench facts)."""
    facts = await price_readiness.load(conn, on)
    classified = await price_readiness.resolve(conn, facts, facts.rated)
    plan_of = {p.source_id: p.plan_id for p in facts.plans}
    more = await extras(conn, on)
    has_rule = bool(facts.pricing.rules)
    out = [workbench_row(r, more.get(plan_of[r["tour_key"]], Extra()), has_rule=has_rule) for r in classified]
    return out, facts


async def build(
    conn: asyncpg.Connection,
    *,
    on: date,
    status: str | None = None,
    country: str | None = None,
    q: str | None = None,
    missing_check: str | None = None,
    step: str | None = None,
) -> dict[str, Any]:
    """The workbench: the summary of every plan and the plans that match the filters."""
    all_rows, facts = await rows(conn, on)
    return {
        "today": on,
        "horizon_to": price_readiness.horizon(on),
        "rules": sorted(c.value for c in facts.pricing.rules),
        "fx_currencies": sorted(facts.pricing.fx),
        "summary": summary(all_rows),
        "items": [
            r
            for r in all_rows
            if matches(r, status=status, country=country, q=q, missing_check=missing_check, step=step)
        ],
        "degraded": facts.degraded,
    }


# ── Bulk actions ─────────────────────────────────────────────────────────────

PLANS_SQL = """
SELECT p.id::text AS id, p.source_id, p.sellable,
       (SELECT count(*) FROM booking.trip_plan_components c WHERE c.trip_plan_id = p.id) AS components
FROM booking.trip_plans p
WHERE p.source_system = 'cis' AND p.source_id = ANY($1::text[])
ORDER BY p.source_id
FOR UPDATE OF p
"""
IN_CATALOG_SQL = (
    "SELECT DISTINCT source_id FROM catalog.v_tour WHERE source_system = 'cis' AND source_id = ANY($1::text[])"
)


def _result(tour_key: str, outcome: str, reason: str | None = None) -> dict[str, Any]:
    return {"tour_key": tour_key, "outcome": outcome, "reason": reason}


async def _in_catalog(conn: asyncpg.Connection, keys: Sequence[str]) -> set[str] | None:
    """The keys served by ``catalog.v_tour``; None when the view cannot be read (the caller then refuses to sell)."""
    try:
        async with conn.transaction():
            return {r["source_id"] for r in await conn.fetch(IN_CATALOG_SQL, list(keys))}
    except CATALOG_ERRORS:
        return None


async def set_sellable(conn: asyncpg.Connection, staff: Staff, plan: Mapping[str, Any], value: bool) -> None:
    await conn.execute(
        "UPDATE booking.trip_plans SET sellable = $2, updated_by = $3 WHERE id = $1::uuid",
        plan["id"],
        value,
        staff.cis_user_id,
    )
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="trip_plan.updated",
        entity_type="trip_plan",
        entity_id=plan["id"],
        before={"sellable": plan["sellable"]},
        after={"source_id": plan["source_id"], "sellable": value, "via": "price_workbench"},
        entity_label=plan["source_id"][:80],
        summary=f"{'Made sellable' if value else 'Made not sellable'} (price workbench)",
    )


async def _provisional(
    conn: asyncpg.Connection, staff: Staff, plan: Mapping[str, Any], extra: Extra, *, apply: bool, on: date
) -> dict[str, Any]:
    key = plan["source_id"]
    if apply:
        if extra.reference_amount is None or not extra.reference_currency:
            return _result(key, "skipped", "no_reference_amount")
        if extra.b2b_rows:
            return _result(key, "skipped", "has_supplier_price")
        if hint_is_reference(extra):
            return _result(key, "unchanged")
        if extra.hint_amount is not None:
            return _result(key, "skipped", "hint_set_by_staff")
        amount, currency = extra.reference_amount, extra.reference_currency
    else:
        if extra.hint_amount is None:
            return _result(key, "unchanged")
        if not hint_is_reference(extra):
            return _result(key, "skipped", "hint_set_by_staff")
        amount, currency = None, None
    await conn.execute(
        "UPDATE booking.trip_plans SET price_hint_amount = $2, price_hint_currency = $3,"
        " price_hint_basis = CASE WHEN $2::numeric IS NULL THEN NULL ELSE 'per_person' END, updated_by = $4,"
        " reference_reviewed_by = CASE WHEN $2::numeric IS NOT NULL AND reference_reviewed_at IS NULL THEN $4"
        " ELSE reference_reviewed_by END,"
        " reference_reviewed_at = CASE WHEN $2::numeric IS NOT NULL AND reference_reviewed_at IS NULL THEN now()"
        " ELSE reference_reviewed_at END"
        " WHERE id = $1::uuid",
        plan["id"],
        amount,
        currency,
        staff.cis_user_id,
    )
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="trip_plan.provisional_price_set" if apply else "trip_plan.provisional_price_cleared",
        entity_type="trip_plan",
        entity_id=plan["id"],
        before={"price_hint": _money(extra.hint_amount, extra.hint_currency)},
        after={"source_id": key, "price_hint": _money(amount, currency), "from": "cis_reference", "on": on},
        entity_label=key[:80],
        summary=(
            f"Provisional price {amount} {currency} from the CIS reference (no supplier price yet)"
            if apply
            else "Provisional price cleared"
        ),
    )
    return _result(key, "done")


def check_request(action: str, keys: Iterable[str]) -> list[str]:
    if action not in ACTIONS:
        raise Invalid("invalid_action", f"action is one of {', '.join(ACTIONS)}")
    unique = list(dict.fromkeys(k.strip() for k in keys if k and k.strip()))
    if not unique:
        raise Invalid("no_plans", "pick at least one trip plan")
    limit = MAX_RECOMPUTE if action == "recompute" else MAX_ACTION_PLANS
    if len(unique) > limit:
        raise Invalid("too_many_plans", f"at most {limit} trip plans per {action}")
    return unique


async def apply_action(
    conn: asyncpg.Connection, staff: Staff, action: str, tour_keys: Sequence[str], *, on: date
) -> dict[str, Any]:
    """Run one bulk action on the given plans (call inside a transaction). One result per asked key."""
    keys = check_request(action, tour_keys)
    plans = {r["source_id"]: dict(r) for r in await conn.fetch(PLANS_SQL, keys)}
    results: list[dict[str, Any]] = []
    served: set[str] | None = set()
    if action == "make_sellable":
        served = await _in_catalog(conn, list(plans))
    more = await extras(conn, on, [p["id"] for p in plans.values()]) if "provisional" in action else {}
    pricing = await cost_sheet.load_pricing(conn, on) if action == "recompute" else None
    for key in keys:
        plan = plans.get(key)
        if plan is None:
            results.append(_result(key, "skipped", "no_trip_plan"))
            continue
        if action in ("make_sellable", "make_not_sellable"):
            value = action == "make_sellable"
            if plan["sellable"] == value:
                results.append(_result(key, "unchanged"))
            elif value and not plan["components"]:
                results.append(_result(key, "skipped", "no_components"))
            elif value and (served is None or key not in served):
                results.append(
                    _result(key, "skipped", "not_in_catalog" if served is not None else "catalog_unreadable")
                )
            else:
                await set_sellable(conn, staff, plan, value)
                results.append(_result(key, "done"))
        elif action in ("apply_provisional", "clear_provisional"):
            results.append(
                await _provisional(
                    conn, staff, plan, more.get(plan["id"], Extra()), apply=action == "apply_provisional", on=on
                )
            )
        else:
            assert pricing is not None  # noqa: S101 — loaded above for this action
            async with conn.transaction():
                out = await cost_sheet.recompute_plan(conn, plan["id"], pricing)
            results.append(_result(key, "done" if out.get("outcome") == "changed" else "unchanged", out.get("reason")))
    counts = {k: sum(1 for r in results if r["outcome"] == k) for k in ("done", "unchanged", "skipped")}
    return {"action": action, "results": results, **counts}
