"""The agency invoice draft jobs (AA-1037 R4 slice 1, root ADR 0019 d9): ``bk_saas_invoice_draft`` and
``bk_settlement_draft``. Both write **drafts only** (a person issues them, :mod:`app.agency_billing.invoices`) and are
idempotent per period: one live invoice per (agency, kind, period start, currency) (bk-041 partial unique index); a
re-run adds what is missing to a draft and leaves an issued invoice alone.

**Period** = a calendar month (UTC), payload ``period`` ``YYYY-MM``. The schedule (:class:`DraftSchedule`) queues, once
a day, the SaaS draft of the **current** month (start of period, d9) and the settlement draft of the **previous** month
(the closed month); staff can run either for any month (``POST /booking/agency-billing/runs``).

**SaaS** (:func:`run_saas`): the live subscriptions (``active``, ``past_due``; a ``trialing`` one is not billed during
its trial) of every agency from the CIS view ``catalog.v_tenant_billing_v1`` (CIS 377; without it the run is
``skipped``). The price rules are those of the CIS quote (``services/tenant_account/quote.py``, AA-1116 option B, CIS
438, contract tenant.entitlements.v1 1.9 / 1.10), so the draft and what the agency sees on its Billing tab agree:

- the invoice currency is the agency's CIS ``billing_currency`` (before CIS 438: the Booking ``saas_currency`` terms);
  USD lines read the USD list, VND lines the VND list (``price_vnd_monthly`` / ``price_extra_seat_vnd``); another
  currency is skipped (``currency_unsupported``); AA-1191: a currency that is not enabled in CIS (``catalog.
  v_billing_currencies_v1``, CIS 497; USD only by default) drafts in USD from the USD list, counted
  ``currency_not_enabled`` (an agreed price in it does not apply; the draft of the period in the old currency loses
  its job lines like any currency move);
- per subscription one ``subscription`` line: a per-seat plan bills max(seats, ``seats_min``, 1) seats; any other plan
  bills the plan price and, with more seats than included, an ``extra_seats`` line at the extra-seat list price;
- seat overage on a per-seat plan (AA-1116, S284; the CIS quote's ``seat_split``): the ``subscription`` line bills the
  seats the plan includes (max(``seats_included``, ``seats_min``, 1)) at the seat price and an ``extra_seats`` line the
  seats above them at the plan's extra-seat list price in the invoice currency, else at the seat price (a plan with no
  extra-seat price bills the same total as before); with an agreed price in force the extra seats are at the agreed
  (locked) seat price;
- an agreed price in the billing currency replaces the list UNIT price while ``agreed_until`` is empty or not before
  the period start (per seat on a per-seat plan; the plan fee otherwise, extra seats stay at the list) (``agreed``);
- a plan without a price in the currency (Enterprise: contract price) gets a zero line, counted ``price_missing``
  (finance adds the price as an adjustment on the draft);
- a subscription billed yearly gets a zero line "annual: invoice by hand" (``annual_manual``): no annual drafting;
- a free pilot (``bill_free``: pilot while CIS ``cis.billing.pilot_free`` is on) is drafted at zero, every line kept
  with its list price in the text (``pilot_free``);
- the CIS quote's discount (contract 1.10, CIS 444: ``discount_pct`` / ``discount_kind`` per row, the founding offer
  or the combo, never both) is taken off the amount each subscription bills (its subscription and extra-seat lines):
  one negative ``adjustment`` line per discounted subscription, right after its lines, carrying the product (a job
  discount line is the only adjustment with a product, so a re-run replaces it and leaves finance's adjustments).
  The share is computed on the exact amounts and rounded like the quote (USD to the cent, VND to the dong) on the
  running total, so the lines add up to the quote's rounded discount (``discounted``); a free pilot, an annual line or
  a missing price bills nothing and gets no discount;
- an agency whose CIS currency changed after the month's software invoice was issued in the old currency gets no new
  draft (``currency_moved_issued``, counted ``period_issued``): finance voids and reissues by hand, never two software
  fees for one month.

On a draft the job's own subscription, extra-seat and discount lines are replaced (a plan change shows), finance's
adjustments stay.

**Settlement** (:func:`run_settlement`): every confirmed B2B hold (``b2b_holds.status = confirmed``), its quote total
and currency (``b2b_quotes``), its departure (the trip case ``start_date``, else the first quote line date) and its
cancellation (``b2b_cancellations``) and partial cancellations (``b2b_line_reductions``, AA-1075: their refundable part
leaves the total, their fee stays and joins a later cancellation fee), against the agency's terms (override, else
default; a tour's own deposit % and balance days win, AA-1075 bk-18: agency + tour > tour > agency > default; read when
drafting):

- first billing, when the confirmation is before the period end: ``full`` (the B2B total) when the balance date
  (departure - ``balance_days_before_departure``) is before the period end or the departure is unknown, else
  ``deposit`` = the total x ``deposit_pct`` (none when 0 %);
- ``balance`` = the total less the billed deposit once the balance date is before the period end (a deposit voided
  after its balance was billed is billed again as the total less that balance);
- a cancelled booking (cancelled before the period end): one ``cancellation_fee`` line = the fee less what was billed
  (a credit when negative), and nothing else after it.

- an approved departure fee (AA-1073 rest, bk-048 ``departure_fees``, approved before the period end): one
  ``departure_fee`` line (seats x price per seat, in the fee's currency) naming the fee (``source_fee_id``; one live
  line per fee, bk-048 index), on the agency's draft even when it has no booking that month.

One live line per (hold, role) (bk-041 index), so nothing is billed twice; an event of an earlier period that was never
billed lands on the next run. Lines go on the agency's draft of the period in the quote's currency; when that invoice is
already issued they wait for the next period (``period_issued``).

Each agency is its own transaction; a failing agency is counted and the run raises at the end (retried), the others are
stored. Not deploy-gate exempt (seconds).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import asyncpg

from app.agency_billing import cis, config, currencies, invoices
from app.errors import Invalid
from app.jobs import queue
from app.jobs.queue import Job

log = logging.getLogger(__name__)

SAAS_KIND = "bk_saas_invoice_draft"
SETTLEMENT_KIND = "bk_settlement_draft"
KINDS = (SAAS_KIND, SETTLEMENT_KIND)
SCHEDULER = "system:agency_billing"
MAX_ATTEMPTS = 3
CENT = Decimal("0.01")
JOB_LINE_ROLES = ("subscription", "extra_seats")
ROLE_LABEL = {
    "deposit": "Deposit",
    "balance": "Balance",
    "full": "Full payment",
    "cancellation_fee": "Cancellation fee",
    "departure_fee": "Departure fee",
}
FEE_REASON_LABEL = {
    "min_shortfall": "minimum not reached",
    "allotment_unreleased": "allotted seats not released",
    "other": "other",
}
PRODUCT_LABEL = {"content": "Content", "crm": "CRM", "tripplanner": "Trip planner"}


class DraftFailed(RuntimeError):
    """Some agencies failed: the others are stored, the job is retried."""


# ── Periods ──────────────────────────────────────────────────────────────────


def parse_period(value: str | None, *, default: date) -> tuple[date, date]:
    """``YYYY-MM`` -> (first day, first day of the next month); None -> the month of ``default``."""
    if value is None:
        start = default.replace(day=1)
    else:
        try:
            year, month = (int(p) for p in value.split("-"))
            start = date(year, month, 1)
        except (ValueError, TypeError):
            raise Invalid("invalid_period", "period is YYYY-MM") from None
        if not 2020 <= year <= 2999:
            raise Invalid("invalid_period", "period is YYYY-MM")
    end = (start + timedelta(days=32)).replace(day=1)
    return start, end


def previous_month(day: date) -> date:
    return (day.replace(day=1) - timedelta(days=1)).replace(day=1)


def period_label(start: date) -> str:
    return start.strftime("%B %Y")


# ── Pure line builders ───────────────────────────────────────────────────────


# technical-constant: the currencies CIS keeps a list price in (shared.plan price_usd_* / price_vnd_*), not a policy.
SAAS_CURRENCIES = ("USD", "VND")


def _dec(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _list_price(s: dict[str, Any], currency: str, *, extra_seat: bool = False) -> Decimal | None:
    if currency == "VND":
        return _dec(s.get("price_extra_seat_vnd" if extra_seat else "price_vnd_monthly"))
    return _dec(s.get("price_extra_seat_usd" if extra_seat else "price_usd_monthly"))


def _agreed_price(s: dict[str, Any], currency: str, period_start: date) -> Decimal | None:
    """The agreed unit price in force for the period: in the invoice currency, ``agreed_until`` empty or not before the
    period start (as the CIS quote, which reads it against today: the draft runs on the period's first day)."""
    price, until = _dec(s.get("agreed_price_monthly")), s.get("agreed_until")
    if price is None or s.get("agreed_currency") != currency:
        return None
    if until is not None and until < period_start:
        return None
    return price


# technical-constant: the minor unit of each SaaS currency (the CIS quote's PLACES), not a policy.
PLACES = {"USD": CENT, "VND": Decimal(1)}
DISCOUNT_LABEL = {"founding": "Founding offer discount", "combo": "Combo discount"}


def _money_text(amount: Decimal, currency: str) -> str:
    return f"{amount:,.0f} VND" if currency == "VND" else f"{amount:,.2f} {currency}"


def seat_split(seats: Any, seats_included: Any, seats_min: Any) -> tuple[int, int]:
    """(seats billed at the seat price, extra seats) of a per-seat plan, as the CIS quote's ``seat_split``: the billed
    total is max(seats, seats_min, 1) (no seats = the included ones); the plan includes max(seats_included, seats_min,
    1) of them; the rest are extra. No ``seats_included`` = no extra seats. Pure."""
    total = max(int(seats or seats_included or 1), int(seats_min or 1), 1)
    if seats_included is None:
        return total, 0
    base = min(total, max(int(seats_included), int(seats_min or 1), 1))
    return base, total - base


def _priced_lines(s: dict[str, Any], name: str, unit: Decimal, currency: str, *, agreed: bool = False
                  ) -> list[dict[str, Any]]:  # fmt: skip
    """The subscription line at ``unit`` and, with seats over the included ones, the extra-seat line: on a per-seat plan
    at the extra-seat list price (else ``unit``; an agreed price covers them), on a plan-fee plan at the extra-seat
    list price."""
    per_seat = s.get("price_unit") == "seat"
    seats, included = s.get("seats"), s.get("seats_included")
    extra = _list_price(s, currency, extra_seat=True)
    if per_seat:
        quantity, over = seat_split(seats, included, s.get("seats_min"))
        lines = [{"line_role": "subscription", "product": s["product"], "plan_key": s["plan_key"],
                  "description": f"{name}, {quantity} seats", "quantity": Decimal(quantity),
                  "unit_price": unit}]  # fmt: skip
        if over:
            price = unit if agreed or extra is None else extra
            lines.append({"line_role": "extra_seats", "product": s["product"], "plan_key": s["plan_key"],
                          "description": f"Extra seats over {quantity}, {name}",
                          "quantity": Decimal(over), "unit_price": price})  # fmt: skip
        return lines
    lines = [{"line_role": "subscription", "product": s["product"], "plan_key": s["plan_key"], "description": name,
              "quantity": Decimal(1), "unit_price": unit}]  # fmt: skip
    if seats and included is not None and extra is not None and int(seats) > int(included):
        lines.append({"line_role": "extra_seats", "product": s["product"], "plan_key": s["plan_key"],
                      "description": f"Extra seats over {int(included)}, {name}",
                      "quantity": Decimal(int(seats) - int(included)), "unit_price": extra})  # fmt: skip
    return lines


def _subscription_lines(s: dict[str, Any], period_start: date, currency: str, flags: dict[str, int]
                        ) -> list[dict[str, Any]]:  # fmt: skip
    name = f"{s.get('plan_name') or s['plan_key']} ({PRODUCT_LABEL.get(s['product'], s['product'])}), "
    name += period_label(period_start)
    zero = {"line_role": "subscription", "product": s["product"], "plan_key": s["plan_key"], "quantity": Decimal(1),
            "unit_price": Decimal(0)}  # fmt: skip
    if s.get("billing_interval") == "year":
        flags["annual_manual"] += 1
        return [{**zero, "description": f"{name}: annual: invoice by hand"}]
    agreed = _agreed_price(s, currency, period_start)
    unit = agreed if agreed is not None else _list_price(s, currency)
    if unit is None:
        flags["price_missing"] += 1
        what = "contract price" if currency == "USD" else f"no {currency} price"
        return [{**zero, "description": f"{name}: {what}, add it as an adjustment"}]
    if agreed is not None:
        flags["agreed"] += 1
        until = s.get("agreed_until")
        name += ", agreed price" + (f" until {until.isoformat()}" if until is not None else "")
    lines = _priced_lines(s, name, unit, currency, agreed=agreed is not None)
    if s.get("bill_free"):
        flags["pilot_free"] += 1
        for ln in lines:
            listed = _money_text(ln["quantity"] * ln["unit_price"], currency)
            ln["description"] = f"{ln['description']}: pilot, free (list {listed})"
            ln["unit_price"] = Decimal(0)
    return lines


def _pct_text(pct: Decimal) -> str:
    return f"{pct.normalize():f}%"


def _discount_line(s: dict[str, Any], amount: Decimal, period_start: date) -> dict[str, Any]:
    """The negative ``adjustment`` line of one subscription's discount. It carries the product (never the plan key: the
    bk-041 source check keeps plan keys for subscription / extra-seat lines), which marks it as the job's own."""
    pct = _dec(s["discount_pct"]) or Decimal(0)
    label = DISCOUNT_LABEL.get(s.get("discount_kind") or "", "Discount")
    name = f"{s.get('plan_name') or s['plan_key']} ({PRODUCT_LABEL.get(s['product'], s['product'])})"
    text = f"{label} {_pct_text(pct)}: {name}, {period_label(period_start)}"
    return {"line_role": "adjustment", "product": s["product"], "plan_key": None, "quantity": Decimal(1),
            "unit_price": -amount, "description": text}  # fmt: skip


def saas_lines(subs: list[dict[str, Any]], period_start: date, currency: str = "USD"
               ) -> tuple[list[dict[str, Any]], dict[str, int]]:  # fmt: skip
    """The subscription / extra-seat lines of one agency's live subscriptions in ``currency`` (pure), each followed by
    its discount line when CIS gives the row a ``discount_pct`` (contract 1.10), and the flags counted:
    ``price_missing``, ``agreed``, ``annual_manual``, ``pilot_free`` and ``discounted`` (subscriptions).

    The discount of a subscription is ``discount_pct`` of what its lines bill, exact; each line takes the rounded
    running total less what the lines before it took (USD cents, VND dong), so the lines add up to the CIS quote's
    rounded discount (the quote sums the exact shares and rounds once)."""
    flags = {"price_missing": 0, "agreed": 0, "annual_manual": 0, "pilot_free": 0, "discounted": 0}
    place = PLACES.get(currency, CENT)
    lines: list[dict[str, Any]] = []
    exact_total, taken = Decimal(0), Decimal(0)
    for s in subs:
        own = _subscription_lines(s, period_start, currency, flags)
        lines += own
        pct = _dec(s.get("discount_pct"))
        billed = sum((ln["quantity"] * ln["unit_price"] for ln in own), Decimal(0))
        if pct is None or pct <= 0 or billed <= 0:
            continue
        exact_total += billed * pct / 100
        amount = exact_total.quantize(place, rounding=ROUND_HALF_UP) - taken
        if amount <= 0:
            continue
        taken += amount
        flags["discounted"] += 1
        lines.append(_discount_line(s, amount, period_start))
    return lines, flags


@dataclass(frozen=True)
class Hold:
    hold_id: str
    tenant_id: Any
    trip_case_code: str | None
    proposal_ref: str
    currency: str
    total: Decimal
    confirmed_on: date
    departure: date | None
    cancelled_on: date | None
    fee: Decimal | None
    plan_id: str | None = None  # AA-1075 (bk-18): the tour of the first trip plan line (per-tour terms)


def settlement_lines(hold: Hold, billed: dict[str, Decimal], terms: dict[str, Any], period_end: date
                     ) -> list[dict[str, Any]]:  # fmt: skip
    """New lines for one hold (pure). ``billed`` = role -> amount already on live (not voided) lines."""
    billed_total = sum(billed.values(), Decimal(0))
    ref = hold.trip_case_code or hold.proposal_ref
    dep = f", departs {hold.departure.isoformat()}" if hold.departure else ""

    def line(role: str, amount: Decimal) -> dict[str, Any]:
        return {"line_role": role, "source_hold_id": hold.hold_id, "quantity": Decimal(1), "unit_price": amount,
                "description": f"{ROLE_LABEL[role]}: {ref} ({hold.proposal_ref}){dep}"[:300]}  # fmt: skip

    if hold.cancelled_on is not None and hold.cancelled_on < period_end:
        if "cancellation_fee" in billed:
            return []
        amount = (hold.fee or Decimal(0)) - billed_total
        return [line("cancellation_fee", amount)] if amount != 0 else []
    if hold.confirmed_on >= period_end or "full" in billed:
        return []
    balance_on = (
        hold.departure - timedelta(days=int(terms["balance_days_before_departure"])) if hold.departure else None
    )
    balance_due = balance_on is None or balance_on < period_end
    if "deposit" not in billed and "balance" not in billed:
        if balance_due:
            return [line("full", hold.total)]
        deposit = (hold.total * Decimal(terms["deposit_pct"])).quantize(CENT, rounding=ROUND_HALF_UP)
        return [line("deposit", deposit)] if deposit > 0 else []
    if "deposit" not in billed:  # the deposit invoice was voided after the balance was billed: bill the rest again
        rest = hold.total - billed["balance"]
        return [line("deposit", rest)] if rest > 0 else []
    if "balance" not in billed and balance_due:
        rest = hold.total - billed["deposit"]
        return [line("balance", rest)] if rest > 0 else []
    return []


# ── Writing ──────────────────────────────────────────────────────────────────


def _key(kind: str, tenant_id: Any, period_start: date, currency: str) -> str:
    return f"{kind}:{tenant_id}:{period_start.isoformat()}:{currency}"


async def _live_invoice(conn: asyncpg.Connection, tenant_id: Any, kind: str, period_start: date, currency: str
                        ) -> asyncpg.Record | None:  # fmt: skip
    return await conn.fetchrow(
        "SELECT id::text AS id, status FROM booking.agency_invoice"
        " WHERE party_tenant_id = $1::uuid AND invoice_kind = $2"
        " AND period_start = $3 AND currency = $4 AND status <> 'void' FOR UPDATE",
        str(tenant_id),
        kind,
        period_start,
        currency,
    )


async def _draft_for(conn: asyncpg.Connection, tenant_id: Any, kind: str, period: tuple[date, date], currency: str,
                     actor: str, country: str | None) -> tuple[str | None, bool]:  # fmt: skip
    """(invoice id, created) of the agency's draft for the period, or (None, False) when it is already issued."""
    row = await _live_invoice(conn, tenant_id, kind, period[0], currency)
    if row is not None:
        return (row["id"], False) if row["status"] == "draft" else (None, False)
    today = await conn.fetchval("SELECT current_date")
    rule = await config.pick_tax_rule(conn, kind, country, today)
    invoice_id = await conn.fetchval(
        "INSERT INTO booking.agency_invoice (party_tenant_id, invoice_kind, period_start, period_end, currency,"
        " tax_rule_id, tax_rate, source, created_by, idempotency_key)"
        " VALUES ($1::uuid, $2, $3, $4, $5, $6::uuid, $7, 'job', $8, $9) RETURNING id::text",
        str(tenant_id),
        kind,
        period[0],
        period[1],
        currency,
        rule["id"] if rule else None,
        Decimal(rule["rate"]) if rule else Decimal(0),
        actor,
        _key(kind, tenant_id, period[0], currency),
    )
    return invoice_id, True


async def _insert_lines(conn: asyncpg.Connection, invoice_id: str, kind: str, lines: list[dict[str, Any]], actor: str
                        ) -> int:  # fmt: skip
    n = await conn.fetchval(
        "SELECT coalesce(max(line_no), 0) FROM booking.agency_invoice_line WHERE invoice_id = $1::uuid", invoice_id
    )
    for ln in lines:
        n += 1
        amount = (ln["quantity"] * ln["unit_price"]).quantize(CENT, rounding=ROUND_HALF_UP)
        await conn.execute(
            "INSERT INTO booking.agency_invoice_line (invoice_id, invoice_kind, line_no, line_role, product, plan_key,"
            " source_hold_id, description, quantity, unit_price, amount, created_by, source_fee_id)"
            " VALUES ($1::uuid, $2, $3, $4, $5, $6, $7::uuid, $8, $9, $10, $11, $12, $13::uuid)",
            invoice_id,
            kind,
            n,
            ln["line_role"],
            ln.get("product"),
            ln.get("plan_key"),
            ln.get("source_hold_id"),
            ln["description"][:300],
            ln["quantity"],
            ln["unit_price"],
            amount,
            actor,
            ln.get("source_fee_id"),
        )
    return len(lines)


def _empty_counts() -> dict[str, Any]:
    return {"agencies": 0, "invoices_created": 0, "drafts_updated": 0, "lines_added": 0, "period_issued": 0,
            "failed": 0}  # fmt: skip


# The job's own lines: subscription / extra-seat lines and its discount lines (the only adjustments with a product;
# finance's adjustments, invoices.add_line, never have one).
DELETE_JOB_LINES_SQL = (
    "DELETE FROM booking.agency_invoice_line WHERE invoice_id = $1::uuid"
    " AND (line_role = ANY($2::text[]) OR (line_role = 'adjustment' AND product IS NOT NULL))"
)
ISSUED_OTHER_CURRENCY_SQL = (
    "SELECT currency FROM booking.agency_invoice WHERE party_tenant_id = $1::uuid AND invoice_kind = 'saas'"
    " AND period_start = $2 AND currency <> $3 AND status NOT IN ('draft', 'void') LIMIT 1"
)
OTHER_CURRENCY_DRAFTS_SQL = (
    "SELECT id::text AS id FROM booking.agency_invoice WHERE party_tenant_id = $1::uuid AND invoice_kind = 'saas'"
    " AND period_start = $2 AND currency <> $3 AND status = 'draft'"
    " AND EXISTS (SELECT 1 FROM booking.agency_invoice_line l WHERE l.invoice_id = agency_invoice.id"
    " AND l.line_role IN ('subscription', 'extra_seats') AND NOT l.voided) FOR UPDATE"
)


async def _draft_agency_saas(conn: asyncpg.Connection, tid: Any, rows: list[dict[str, Any]],
                            period: tuple[date, date], actor: str,
                            allowed: tuple[str, ...] = (currencies.BASE,)) -> dict[str, int]:  # fmt: skip
    """One agency's SaaS draft for the period (inside its transaction); the counts it adds to the run. ``allowed`` =
    the enabled currencies (AA-1191, CIS 497): a billing currency that is not enabled drafts in USD."""
    terms = await config.terms_for(conn, tid)
    if terms is None:
        raise RuntimeError("no billing terms row")
    # AA-1116: the agency's CIS billing currency (CIS 438), else the Booking terms (before 438).
    currency = (rows[0].get("billing_currency") or terms["saas_currency"]).strip().upper()
    if currency not in SAAS_CURRENCIES:
        return {"currency_unsupported": 1}
    disabled: dict[str, int] = {}
    if currency not in allowed:
        currency = currencies.effective(currency, allowed)
        disabled = {"currency_not_enabled": 1}
    # A currency changed in CIS after the month was issued in the old one: no draft in the new currency (never two
    # software fees for one month); finance voids the issued invoice and the next run drafts the new currency.
    if await conn.fetchval(ISSUED_OTHER_CURRENCY_SQL, str(tid), period[0], currency) is not None:
        return {"period_issued": 1, "currency_moved_issued": 1}
    lines, done = saas_lines(rows, period[0], currency)
    done.update(disabled)
    # A currency changed in CIS before the month was issued: the job lines of the period's draft in the old currency
    # go (never two live software fees for one month).
    for other in await conn.fetch(OTHER_CURRENCY_DRAFTS_SQL, str(tid), period[0], currency):
        await conn.execute(DELETE_JOB_LINES_SQL, other["id"], list(JOB_LINE_ROLES))
        await invoices.recompute(conn, other["id"])
        done["currency_moved"] = done.get("currency_moved", 0) + 1
    invoice_id, created = await _draft_for(conn, tid, "saas", period, currency, actor, rows[0].get("billing_country"))
    if invoice_id is None:
        return {"period_issued": 1, "currency_moved": done.get("currency_moved", 0)}
    if not created:
        await conn.execute(DELETE_JOB_LINES_SQL, invoice_id, list(JOB_LINE_ROLES))
    done["lines_added"] = await _insert_lines(conn, invoice_id, "saas", lines, actor)
    await invoices.recompute(conn, invoice_id)
    done["invoices_created" if created else "drafts_updated"] = 1
    return done


async def run_saas(pool: asyncpg.Pool, *, period: str | None, tenant_id: str | None, actor: str) -> dict[str, Any]:
    today = datetime.now(UTC).date()
    start, end = parse_period(period, default=today)
    async with pool.acquire() as conn:
        if not await cis.ready(conn):
            return {"outcome": "skipped", "reason": f"{cis.VIEW.qualified} not readable (CIS 377)"}
        subs = await cis.subscriptions(conn, [tenant_id] if tenant_id else None)
        allowed = await currencies.enabled(conn)
        partners = {r["tenant_id"]: r["agency_name"] for r in await conn.fetch(
            "SELECT tenant_id, agency_name FROM booking.b2b_partners")}  # fmt: skip
    by_tenant: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for s in subs:
        if s.get("kind") == "internal":
            continue
        by_tenant[s["tenant_id"]].append(s)
    counts = {**_empty_counts(), "price_missing": 0, "no_partner": 0, "currency_unsupported": 0, "agreed": 0,
              "annual_manual": 0, "pilot_free": 0, "currency_moved": 0, "currency_moved_issued": 0,
              "discounted": 0, "currency_not_enabled": 0}  # fmt: skip
    for tid, rows in by_tenant.items():
        if tid not in partners:
            counts["no_partner"] += 1  # bk_tenant_pull has not projected the tenant yet: next run
            continue
        counts["agencies"] += 1
        try:
            async with pool.acquire() as conn, conn.transaction():
                done = await _draft_agency_saas(conn, tid, rows, (start, end), actor, allowed)
            for k, v in done.items():
                counts[k] += v
        except Exception:  # noqa: BLE001 — one agency never holds the others; the run raises at the end
            counts["failed"] += 1
            log.exception("bk_saas_invoice_draft_failed tenant=%s", tid)
    return _finish("saas", start, counts)


HOLDS_SQL = """
SELECT h.id::text AS hold_id, p.tenant_id, tc.code AS trip_case_code, h.proposal_ref, q.currency, q.total_b2b_price,
       (h.confirmed_at AT TIME ZONE 'UTC')::date AS confirmed_on,
       coalesce(tc.start_date, (SELECT min(ql.service_date) FROM booking.b2b_quote_lines ql WHERE ql.quote_id = q.id))
           AS departure,
       (c.cancelled_at AT TIME ZONE 'UTC')::date AS cancelled_on, c.fee_amount,
       (SELECT substr(ql.component_id, 4) FROM booking.b2b_quote_lines ql
         WHERE ql.quote_id = q.id AND ql.component_id LIKE 'tp:%' ORDER BY ql.line_no LIMIT 1) AS plan_id,
       r.refunded, r.fees
  FROM booking.b2b_holds h
  JOIN booking.b2b_partners p ON p.id = h.partner_id
  JOIN booking.b2b_quotes q ON q.id = h.quote_id
  LEFT JOIN booking.trip_cases tc ON tc.id = h.trip_case_id
  LEFT JOIN booking.b2b_cancellations c ON c.hold_id = h.id
  LEFT JOIN LATERAL (
      SELECT sum(x.refundable_amount) AS refunded, sum(x.fee_amount) AS fees
        FROM booking.b2b_line_reductions x WHERE x.hold_id = h.id AND x.created_at < $1
  ) r ON true
 WHERE h.status = 'confirmed' AND h.confirmed_at < $1
   AND ($2::uuid IS NULL OR p.tenant_id = $2::uuid)
 ORDER BY p.tenant_id, h.confirmed_at, h.id
"""
BILLED_SQL = """
SELECT source_hold_id::text AS hold_id, line_role, sum(amount) AS amount
  FROM booking.agency_invoice_line
 WHERE source_hold_id = ANY($1::uuid[]) AND NOT voided
 GROUP BY source_hold_id, line_role
"""


# AA-1073 rest (bk-048): approved departure fees not on a live line yet, by agency.
FEES_SQL = """
SELECT f.id::text AS fee_id, p.tenant_id, f.currency, f.seats, f.price_per_seat, f.reason, d.start_date
  FROM booking.departure_fees f
  JOIN booking.b2b_partners p ON p.id = f.partner_id
  JOIN booking.departures d ON d.id = f.departure_id
 WHERE f.status = 'approved' AND f.decided_at < $1
   AND ($2::uuid IS NULL OR p.tenant_id = $2::uuid)
   AND NOT EXISTS (SELECT 1 FROM booking.agency_invoice_line il WHERE il.source_fee_id = f.id AND NOT il.voided)
 ORDER BY p.tenant_id, f.decided_at, f.id
"""


def fee_line(fee: Any) -> dict[str, Any]:
    """The settlement line of one approved departure fee (pure): seats x price per seat, naming the fee."""
    reason = FEE_REASON_LABEL.get(fee["reason"], fee["reason"])
    return {
        "line_role": "departure_fee",
        "source_fee_id": fee["fee_id"],
        "quantity": Decimal(int(fee["seats"])),
        "unit_price": Decimal(fee["price_per_seat"]),
        "description": f"{ROLE_LABEL['departure_fee']} ({reason}): departure {fee['start_date'].isoformat()},"
        f" {int(fee['seats'])} seats"[:300],
    }


async def run_settlement(pool: asyncpg.Pool, *, period: str | None, tenant_id: str | None, actor: str
                         ) -> dict[str, Any]:  # fmt: skip
    today = datetime.now(UTC).date()
    start, end = parse_period(period, default=previous_month(today))
    end_ts = datetime(end.year, end.month, end.day, tzinfo=UTC)
    async with pool.acquire() as conn:
        rows = await conn.fetch(HOLDS_SQL, end_ts, tenant_id)
        fee_rows = await conn.fetch(FEES_SQL, end_ts, tenant_id)
    fees_by_tenant: dict[Any, list[Any]] = defaultdict(list)
    for f in fee_rows:
        fees_by_tenant[f["tenant_id"]].append(f)
    holds: dict[Any, list[Hold]] = defaultdict(list)
    for r in rows:
        # AA-1075 (bk-14): partial cancellations take their refundable part off the total (their fee stays billed);
        # a cancel after them charges its fee on the rest, plus those fees.
        refunded = Decimal(r.get("refunded") or 0)
        fees = Decimal(r.get("fees") or 0)
        holds[r["tenant_id"]].append(
            Hold(
                r["hold_id"],
                r["tenant_id"],
                r["trip_case_code"],
                r["proposal_ref"],
                r["currency"],
                Decimal(r["total_b2b_price"]) - refunded,
                r["confirmed_on"],
                r["departure"],
                r["cancelled_on"],
                Decimal(r["fee_amount"]) + fees if r["fee_amount"] is not None else None,
                r.get("plan_id"),
            )  # fmt: skip
        )
    counts = _empty_counts()
    for tid in list(holds) + [t for t in fees_by_tenant if t not in holds]:
        agency_holds = holds.get(tid, [])
        counts["agencies"] += 1
        try:
            async with pool.acquire() as conn, conn.transaction():
                terms = await config.terms_for(conn, tid)
                if terms is None:
                    raise RuntimeError("no billing terms row")
                # AA-1075 (bk-18): the deposit % and balance days of the hold's tour when it has its own terms.
                by_plan: dict[str | None, dict[str, Any]] = {None: terms}
                for plan in {h.plan_id for h in agency_holds if h.plan_id}:
                    by_plan[plan] = await config.plan_terms(conn, tid, plan) or terms
                billed: dict[str, dict[str, Decimal]] = defaultdict(dict)
                for b in await conn.fetch(BILLED_SQL, [h.hold_id for h in agency_holds]):
                    billed[b["hold_id"]][b["line_role"]] = Decimal(b["amount"])
                per_currency: dict[str, list[dict[str, Any]]] = defaultdict(list)
                for h in agency_holds:
                    per_currency[h.currency] += settlement_lines(
                        h, billed.get(h.hold_id, {}), by_plan.get(h.plan_id, terms), end
                    )
                for f in fees_by_tenant.get(tid, []):
                    per_currency[f["currency"].strip()].append(fee_line(f))
                done: dict[str, int] = defaultdict(int)
                for currency, lines in per_currency.items():
                    if not lines:
                        continue
                    invoice_id, created = await _draft_for(conn, tid, "settlement", (start, end), currency, actor,
                                                           None)  # fmt: skip
                    if invoice_id is None:
                        done["period_issued"] += 1
                        continue
                    done["lines_added"] += await _insert_lines(conn, invoice_id, "settlement", lines, actor)
                    await invoices.recompute(conn, invoice_id)
                    done["invoices_created" if created else "drafts_updated"] += 1
            for k, v in done.items():
                counts[k] += v
        except Exception:  # noqa: BLE001 — one agency never holds the others; the run raises at the end
            counts["failed"] += 1
            log.exception("bk_settlement_draft_failed tenant=%s", tid)
    return _finish("settlement", start, counts)


def _finish(kind: str, start: date, counts: dict[str, Any]) -> dict[str, Any]:
    out = {"outcome": "ok", "invoice_kind": kind, "period": start.strftime("%Y-%m"), **counts}
    log.info("agency_billing_draft %s", out)
    if counts["failed"]:
        raise DraftFailed(f"{counts['failed']} agencies failed: {out}")
    return out


# ── Jobs ─────────────────────────────────────────────────────────────────────


def _actor(job: Job) -> str:
    return f"job:{job.id}"


async def handle_saas(pool: asyncpg.Pool, job: Job) -> dict[str, Any]:
    p = job.payload or {}
    return await run_saas(pool, period=p.get("period"), tenant_id=p.get("tenant_id"), actor=_actor(job))


async def handle_settlement(pool: asyncpg.Pool, job: Job) -> dict[str, Any]:
    p = job.payload or {}
    return await run_settlement(pool, period=p.get("period"), tenant_id=p.get("tenant_id"), actor=_actor(job))


HANDLERS = {SAAS_KIND: handle_saas, SETTLEMENT_KIND: handle_settlement}


async def enqueue_run(pool: asyncpg.Pool, kind: str, period: str | None, tenant_id: str | None, actor: str
                      ) -> dict[str, Any]:  # fmt: skip
    """A staff run (``POST /booking/agency-billing/runs``): always a new job (the drafts are idempotent anyway)."""
    job_kind = SAAS_KIND if kind == "saas" else SETTLEMENT_KIND
    today = datetime.now(UTC).date()
    start, _ = parse_period(period, default=today if kind == "saas" else previous_month(today))
    payload: dict[str, Any] = {"trigger": "manual", "period": start.strftime("%Y-%m")}
    if tenant_id:
        payload["tenant_id"] = tenant_id
    job_id = await queue.enqueue(pool, job_kind, payload, max_attempts=MAX_ATTEMPTS, created_by=actor[:200])
    return {"job_id": job_id, "kind": job_kind, **payload}


RUNS_SQL = """
SELECT id::text AS id, kind, status, payload, result, error, attempt, created_by, created_at, started_at, finished_at
  FROM booking.job WHERE kind = ANY($1::text[]) ORDER BY created_at DESC LIMIT $2
"""


async def recent_runs(conn: asyncpg.Connection, limit: int = 20) -> list[dict[str, Any]]:
    return [dict(r) for r in await conn.fetch(RUNS_SQL, list(KINDS), limit)]


class DraftSchedule:
    """Worker maintenance hook: once per UTC day, the SaaS draft of the current month and the settlement draft of the
    previous month (keys per month, so each month is drafted by the schedule once; staff runs are separate)."""

    def __init__(self, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._now = now
        self._done_day: date | None = None

    async def __call__(self, pool: asyncpg.Pool) -> str | None:
        today = self._now().date()
        if self._done_day == today:
            return None
        month = today.strftime("%Y-%m")
        closed = previous_month(today).strftime("%Y-%m")
        first = await queue.enqueue(pool, SAAS_KIND, {"trigger": "schedule", "period": month},
                                    idempotency_key=f"{SAAS_KIND}:{month}", max_attempts=MAX_ATTEMPTS,
                                    created_by=SCHEDULER)  # fmt: skip
        await queue.enqueue(pool, SETTLEMENT_KIND, {"trigger": "schedule", "period": closed},
                            idempotency_key=f"{SETTLEMENT_KIND}:{closed}", max_attempts=MAX_ATTEMPTS,
                            created_by=SCHEDULER)  # fmt: skip
        self._done_day = today
        return first
