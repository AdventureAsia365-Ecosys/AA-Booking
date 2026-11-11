"""The agency invoice draft jobs (AA-1037 R4 slice 1, root ADR 0019 d9): ``bk_saas_invoice_draft`` and
``bk_settlement_draft``. Both write **drafts only** (a person issues them, :mod:`app.agency_billing.invoices`) and are
idempotent per period: one live invoice per (agency, kind, period start, currency) (bk-041 partial unique index); a
re-run adds what is missing to a draft and leaves an issued invoice alone.

**Period** = a calendar month (UTC), payload ``period`` ``YYYY-MM``. The schedule (:class:`DraftSchedule`) queues, once
a day, the SaaS draft of the **current** month (start of period, d9) and the settlement draft of the **previous** month
(the closed month); staff can run either for any month (``POST /booking/agency-billing/runs``).

**SaaS** (:func:`run_saas`): the live subscriptions (``active``, ``past_due``) of every agency from the CIS view
``catalog.v_tenant_billing_v1`` (CIS 377; without it the run is ``skipped``). Per subscription one ``subscription``
line: the plan's monthly USD price x seats for a per-seat plan (else x 1); a per-tenant / per-site plan with more seats
than included adds an ``extra_seats`` line. A plan without a price (Enterprise: contract price) gets a zero line and is
counted ``price_missing`` (finance adds the price as an adjustment on the draft). The invoice currency is the agency's
``saas_currency`` (terms); plan prices are USD, so another currency is skipped (``currency_unsupported``). On a draft
the job's own subscription lines are replaced (a plan change shows), adjustments stay.

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

from app.agency_billing import cis, config, invoices
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


def saas_lines(subs: list[dict[str, Any]], period_start: date) -> tuple[list[dict[str, Any]], int]:
    """The subscription / extra-seat lines of one agency's live subscriptions, and how many plans have no price."""
    lines: list[dict[str, Any]] = []
    missing = 0
    label = period_label(period_start)
    for s in subs:
        price = s.get("price_usd_monthly")
        seats = s.get("seats")
        per_seat = s.get("price_unit") == "seat"
        name = f"{s.get('plan_name') or s['plan_key']} ({PRODUCT_LABEL.get(s['product'], s['product'])}), {label}"
        if price is None:
            missing += 1
            lines.append({"line_role": "subscription", "product": s["product"], "plan_key": s["plan_key"],
                          "description": f"{name}: contract price, add it as an adjustment",
                          "quantity": Decimal(1), "unit_price": Decimal(0)})  # fmt: skip
            continue
        quantity = Decimal(max(int(seats or s.get("seats_included") or 1), 1)) if per_seat else Decimal(1)
        lines.append({"line_role": "subscription", "product": s["product"], "plan_key": s["plan_key"],
                      "description": name + (f", {int(quantity)} seats" if per_seat else ""),
                      "quantity": quantity, "unit_price": Decimal(price)})  # fmt: skip
        included, extra = s.get("seats_included"), s.get("price_extra_seat_usd")
        if not per_seat and seats and included is not None and extra is not None and int(seats) > int(included):
            lines.append({"line_role": "extra_seats", "product": s["product"], "plan_key": s["plan_key"],
                          "description": f"Extra seats over {int(included)}, {name}",
                          "quantity": Decimal(int(seats) - int(included)), "unit_price": Decimal(extra)})  # fmt: skip
    return lines, missing


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
            ln["description"],
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


async def run_saas(pool: asyncpg.Pool, *, period: str | None, tenant_id: str | None, actor: str) -> dict[str, Any]:
    today = datetime.now(UTC).date()
    start, end = parse_period(period, default=today)
    async with pool.acquire() as conn:
        if not await cis.ready(conn):
            return {"outcome": "skipped", "reason": f"{cis.VIEW.qualified} not readable (CIS 377)"}
        subs = await cis.subscriptions(conn, [tenant_id] if tenant_id else None)
        partners = {r["tenant_id"]: r["agency_name"] for r in await conn.fetch(
            "SELECT tenant_id, agency_name FROM booking.b2b_partners")}  # fmt: skip
    by_tenant: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for s in subs:
        if s.get("kind") == "internal":
            continue
        by_tenant[s["tenant_id"]].append(s)
    counts = {**_empty_counts(), "price_missing": 0, "no_partner": 0, "currency_unsupported": 0}
    for tid, rows in by_tenant.items():
        if tid not in partners:
            counts["no_partner"] += 1  # bk_tenant_pull has not projected the tenant yet: next run
            continue
        counts["agencies"] += 1
        try:
            async with pool.acquire() as conn, conn.transaction():
                terms = await config.terms_for(conn, tid)
                if terms is None:
                    raise RuntimeError("no billing terms row")
                currency = terms["saas_currency"]
                if currency != "USD":
                    counts["currency_unsupported"] += 1
                    continue
                lines, missing = saas_lines(rows, start)
                counts["price_missing"] += missing
                invoice_id, created = await _draft_for(conn, tid, "saas", (start, end), currency, actor,
                                                       rows[0].get("billing_country"))  # fmt: skip
                if invoice_id is None:
                    counts["period_issued"] += 1
                    continue
                if not created:
                    await conn.execute(
                        "DELETE FROM booking.agency_invoice_line WHERE invoice_id = $1::uuid"
                        " AND line_role = ANY($2::text[])",
                        invoice_id,
                        list(JOB_LINE_ROLES),
                    )
                added = await _insert_lines(conn, invoice_id, "saas", lines, actor)
                await invoices.recompute(conn, invoice_id)
            counts["lines_added"] += added
            counts["invoices_created" if created else "drafts_updated"] += 1
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
