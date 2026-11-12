"""``bk_trip_plan_sync`` (AA-1029 R4, root ADR 0005 d3 / d7): every published CIS master tour gets a Booking trip plan,
so loading supplier rates and components makes its prices flow (the cost sheet, ``app.catalog.cost_sheet``).

Source: the CIS contract view ``catalog.v_tour`` (contract catalog.tour.v1, CIS 225; role ``booking`` already reads it
live for the trip plan pages), keys only. One run (a worker maintenance hook queues one per :data:`SLOT_SECONDS`
slot while the view is readable):

- a tour key without a plan -> a plan row: ``sellable = false`` (selling a tour stays a staff decision, AA-679),
  no channel, no price hint, ``synced_at = now()``, ``created_by`` NULL (no staff user);
- a plan staff created before the bridge (``synced_at`` NULL) whose tour is in the view -> ``synced_at`` set once (the
  row is otherwise left exactly as staff wrote it);
- a plan whose tour left the view is left alone (the "Orphaned plans" filter and the tour lifecycle pull report it;
  role ``booking`` has no DELETE);
- one ``booking.audit_logs`` row per run that changed something (``system:bk_trip_plan_sync``, ``trip_plan.synced``,
  counts and up to 50 keys).

**Reference price** (ADR 0005 d7, "parsed once into a reference price with a review flag"): when the CIS contract view
``catalog.v_tour_price_reference_v1`` (source key + the source price text) is readable, the plan keeps that text as
read (``reference_price_raw``, at most 500 characters) and the amount and currency :func:`parse_reference` finds in it.
A changed text clears the review (``reference_reviewed_by`` / ``_at``), so staff look at it again. The reference is
never a selling price and never reaches ``booking.v_from_prices``.

AA-1091: when the CIS contract view ``catalog.v_tour_price_reference_v2`` (CIS 402, :data:`STRUCTURED_VIEW`,
feature-detected) is readable it is read **instead of** v1: per live tour the same source text plus the structured
reference price the customer-service team reviews in CIS (``reference_amount`` / ``_currency`` / ``_basis`` /
``_status``). A value whose status is ``confirmed`` (a person confirmed it against the current text) or ``parsed`` (the
CIS parser, no doubts) is taken as it is: basis ``per_person`` or ``from`` (the lowest per-person price) is the
per-person reference; ``per_group`` gives no per-person amount (the plan keeps the text only, never a group total
shown as a per-person price). Status ``needs_review`` (the CIS parser had doubts, or the text changed since the value
was set) is not trusted: Booking's own parse of the text decides, as without CIS's value. A change of the amount or
currency alone also counts as a change (the review is cleared). The run reports which view it read
(``references_source`` ``structured`` | ``text``) and how many plans hold a CIS structured value
(``references_structured``). Without either view (CIS not applied, no grant) the reference part is skipped and
reported, the bridge still runs.

Without ``catalog.v_tour`` readable the run answers ``skipped``. Idempotent: a second run with nothing new writes
nothing (``ON CONFLICT DO NOTHING``, updates only where something differs).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import asyncpg

from app import audit
from app.catalog import liveness
from app.jobs import queue
from app.jobs.queue import Job

log = logging.getLogger(__name__)

KIND = "bk_trip_plan_sync"
ACTOR = "system:bk_trip_plan_sync"
SCHEDULER = "system:trip_plan_sync"
MAX_ATTEMPTS = 3
SLOT_SECONDS = 900
RAW_MAX = 500
AUDIT_KEYS = 50

TOUR_VIEW = liveness.View("catalog", "v_tour", ("source_system", "source_id"))
REFERENCE_VIEW = liveness.View("catalog", "v_tour_price_reference_v1", ("source_system", "source_id", "price_raw"))
STRUCTURED_VIEW = liveness.View(
    "catalog",
    "v_tour_price_reference_v2",
    (
        "source_system",
        "source_id",
        "price_raw",
        "reference_amount",
        "reference_currency",
        "reference_basis",
        "reference_status",
    ),
)

KEYS_SQL = "SELECT DISTINCT source_id FROM catalog.v_tour WHERE source_system = 'cis' ORDER BY source_id"
INSERT_SQL = """
INSERT INTO booking.trip_plans (source_system, source_id, sellable, synced_at)
SELECT 'cis', k, false, now() FROM unnest($1::text[]) AS k
ON CONFLICT (source_system, source_id) DO NOTHING
RETURNING source_id
"""
ADOPT_SQL = """
UPDATE booking.trip_plans SET synced_at = now()
WHERE source_system = 'cis' AND synced_at IS NULL AND source_id = ANY($1::text[])
RETURNING source_id
"""
REFERENCES_SQL = f"""
SELECT source_id, price_raw, NULL::numeric AS amount, NULL::text AS currency, NULL::text AS basis,
       NULL::text AS status
FROM {REFERENCE_VIEW.qualified} WHERE source_system = 'cis'
"""  # noqa: S608 — constant
STRUCTURED_REFERENCES_SQL = f"""
SELECT source_id, price_raw, reference_amount::numeric AS amount, reference_currency::text AS currency,
       reference_basis::text AS basis, reference_status::text AS status
FROM {STRUCTURED_VIEW.qualified} WHERE source_system = 'cis'
"""  # noqa: S608 — constant
STORED_REFERENCES_SQL = """
SELECT source_id, reference_price_raw, reference_amount, reference_currency::text AS reference_currency
FROM booking.trip_plans WHERE source_system = 'cis'
"""
REFERENCE_UPDATE_SQL = """
UPDATE booking.trip_plans p
   SET reference_price_raw = u.raw, reference_amount = u.amount, reference_currency = u.currency,
       reference_reviewed_by = NULL, reference_reviewed_at = NULL
  FROM unnest($1::text[], $2::text[], $3::numeric[], $4::text[]) AS u(source_id, raw, amount, currency)
 WHERE p.source_system = 'cis' AND p.source_id = u.source_id
"""

# ── Reference price parsing (pure) ───────────────────────────────────────────

SYMBOLS = {"$": "USD", "US$": "USD", "€": "EUR", "£": "GBP", "A$": "AUD", "S$": "SGD", "฿": "THB", "₫": "VND"}
CODES = frozenset(
    {
        "USD",
        "EUR",
        "GBP",
        "AUD",
        "SGD",
        "VND",
        "THB",
        "JPY",
        "CNY",
        "INR",
        "NPR",
        "BTN",
        "LAK",
        "KHR",
        "MMK",
        "MYR",
        "IDR",
        "PHP",
        "KRW",
        "LKR",
        "MNT",
        "CHF",
        "CAD",
        "NZD",
    }
)
NUMBER_RE = re.compile(r"\d[\d.,]*\d|\d")
CODE_RE = re.compile(r"\b([A-Z]{3})\b")
MAX_REFERENCE = Decimal("10000000000")


def _number(text: str) -> Decimal | None:
    token = text
    if "," in token and "." in token:
        decimal_sep = "," if token.rfind(",") > token.rfind(".") else "."
        thousands = "." if decimal_sep == "," else ","
        token = token.replace(thousands, "").replace(decimal_sep, ".")
    elif "," in token:
        parts = token.split(",")
        token = token.replace(",", "") if all(len(p) == 3 for p in parts[1:]) else token.replace(",", ".")
    elif token.count(".") > 1 or (token.count(".") == 1 and len(token.split(".")[1]) == 3):
        token = token.replace(".", "")
    try:
        value = Decimal(token)
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def parse_reference(raw: str | None) -> tuple[Decimal, str] | None:
    """The first amount and its currency in a free-text source price ("$1,722/person", "from USD 495",
    "1.200 EUR pp"), or None when either is missing. The amount is per person as written; never a selling price."""
    text = " ".join((raw or "").split())
    if not text:
        return None
    match = NUMBER_RE.search(text)
    if match is None:
        return None
    amount = _number(match.group(0))
    if amount is None or amount <= 0 or amount >= MAX_REFERENCE:
        return None
    upper = text.upper()
    currency = next((c for c in CODE_RE.findall(upper) if c in CODES), None)
    if currency is None:
        currency = next((SYMBOLS[s] for s in sorted(SYMBOLS, key=len, reverse=True) if s in upper), None)
    if currency is None:
        return None
    return amount.quantize(Decimal("0.01")), currency


# CIS 402 ``reference_status`` values Booking takes as they are; ``needs_review`` falls back to the text parse.
TRUSTED_STATUSES = frozenset({"confirmed", "parsed"})
# ``from`` = the lowest per-person price of the tour (CIS 401): still a per-person reference.
PER_PERSON_BASES = frozenset({"per_person", "from"})
PER_GROUP = "per_group"


def structured_reference(amount: Any, currency: Any, basis: Any) -> tuple[Decimal, str] | None:
    """CIS's structured reference (AA-1091) when it is a usable per-person amount, else None."""
    code = str(currency or "").strip().upper()
    if amount is None or not re.fullmatch(r"[A-Z]{3}", code) or (basis or "per_person") not in PER_PERSON_BASES:
        return None
    try:
        value = Decimal(str(amount))
    except InvalidOperation:
        return None
    if not value.is_finite() or value <= 0 or value >= MAX_REFERENCE:
        return None
    return value.quantize(Decimal("0.01")), code


def trusted(amount: Any, status: Any) -> bool:
    """A CIS structured value Booking takes as it is (status confirmed / parsed); else the text parse decides."""
    return amount is not None and status in TRUSTED_STATUSES


def reference_of(
    raw: str | None, amount: Any = None, currency: Any = None, basis: Any = None, status: Any = None
) -> tuple[Decimal, str] | None:
    """The per-person reference of a plan: CIS's trusted structured value (a per-group one gives None: no per-person
    amount), else the parse of the text. ``status`` None (v1, no structured value) always parses."""
    if trusted(amount, status):
        if (basis or "per_person") == PER_GROUP:
            return None
        found = structured_reference(amount, currency, basis)
        if found is not None:
            return found
    return parse_reference(raw)


def clean_raw(raw: str | None) -> str | None:
    text = " ".join((raw or "").split())[:RAW_MAX]
    return text or None


# ── The job ──────────────────────────────────────────────────────────────────


async def _references(conn: asyncpg.Connection) -> dict[str, Any]:
    if await liveness.view_ready(conn, STRUCTURED_VIEW):
        structured, sql = True, STRUCTURED_REFERENCES_SQL
    elif await liveness.view_ready(conn, REFERENCE_VIEW):
        structured, sql = False, REFERENCES_SQL
    else:
        return {"references": "skipped"}
    stored = {r["source_id"]: r for r in await conn.fetch(STORED_REFERENCES_SQL)}
    ids: list[str] = []
    raws: list[str | None] = []
    amounts: list[Decimal | None] = []
    currencies: list[str | None] = []
    from_cis = 0
    for row in await conn.fetch(sql):
        old = stored.get(row["source_id"])
        if old is None:
            continue
        raw = clean_raw(row["price_raw"])
        found = reference_of(raw, row["amount"], row["currency"], row["basis"], row["status"])
        from_cis += trusted(row["amount"], row["status"])
        same_text = raw == old["reference_price_raw"]
        same_value = found == (
            (old["reference_amount"], old["reference_currency"].strip())
            if old["reference_amount"] is not None
            else None
        )
        if same_text and (same_value or not structured):
            continue
        ids.append(row["source_id"])
        raws.append(raw)
        amounts.append(found[0] if found else None)
        currencies.append(found[1] if found else None)
    if ids:
        await conn.execute(REFERENCE_UPDATE_SQL, ids, raws, amounts, currencies)
    return {
        "references": "ok",
        "references_source": "structured" if structured else "text",
        "references_changed": len(ids),
        "references_structured": from_cis,
    }


async def run(pool: asyncpg.Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        if not await liveness.view_ready(conn, TOUR_VIEW):
            return {"outcome": "skipped", "reason": f"{TOUR_VIEW.qualified} not readable"}
    async with pool.acquire() as conn, conn.transaction():
        keys = [r["source_id"] for r in await conn.fetch(KEYS_SQL)]
        created = [r["source_id"] for r in await conn.fetch(INSERT_SQL, keys)]
        adopted = [r["source_id"] for r in await conn.fetch(ADOPT_SQL, keys)]
        refs = await _references(conn)
        if created or adopted or refs.get("references_changed"):
            await audit.record(
                conn,
                actor_type="system",
                actor_ref=ACTOR,
                action="trip_plan.synced",
                entity_type="trip_plan",
                entity_id=None,
                after={
                    "created": len(created),
                    "adopted": len(adopted),
                    "references_changed": refs.get("references_changed", 0),
                    "created_keys": created[:AUDIT_KEYS],
                    "adopted_keys": adopted[:AUDIT_KEYS],
                },
                entity_label="catalog sync",
                summary=f"Trip plans from the CIS catalog: {len(created)} created, {len(adopted)} adopted",
            )
    result = {"outcome": "ok", "tours": len(keys), "created": len(created), "adopted": len(adopted), **refs}
    if created or adopted:
        log.info("bk_trip_plan_sync: %s", result)
    return result


async def handle(pool: asyncpg.Pool, job: Job) -> dict[str, Any]:
    return await run(pool)


HANDLERS = {KIND: handle}


def schedule_key(at: datetime) -> str:
    return f"{KIND}:slot:{int(at.timestamp()) // SLOT_SECONDS}"


class SyncSchedule:
    """Worker maintenance hook: one ``bk_trip_plan_sync`` run per :data:`SLOT_SECONDS` slot while ``catalog.v_tour``
    is readable (feature detection cached by :func:`app.catalog.liveness.view_ready`)."""

    def __init__(self, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._now = now
        self._done_slot: str | None = None

    async def __call__(self, pool: asyncpg.Pool) -> str | None:
        key = schedule_key(self._now())
        if self._done_slot == key:
            return None
        async with pool.acquire() as conn:
            if not await liveness.view_ready(conn, TOUR_VIEW):
                return None
        job_id = await queue.enqueue(
            pool, KIND, {"trigger": "schedule"}, idempotency_key=key, max_attempts=MAX_ATTEMPTS, created_by=SCHEDULER
        )
        self._done_slot = key
        return job_id
