"""Pure money helpers of the operations billing module (AA-682 S3, bk-028): amounts in the currency's minor units,
the default payment schedule, the waterfall of verified payments over the schedule, the canonical request hash of an
``Idempotency-Key`` call and the card-number refusal.

No float anywhere: amounts are :class:`~decimal.Decimal`, parsed from strings / Decimals / ints and quantized to the
currency's minor units (critic note 3: VND / JPY / IDR / KRW have none). The database stores ``numeric(16,2)``.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from app.errors import Invalid

# Currencies whose minor unit is not 2 (ISO 4217 as used in practice; IDR is quoted without decimals). Booking sells in
# the engine's five (USD, EUR, GBP, AUD, SGD); the B2B quotes add VND.
MINOR_UNITS = {"VND": 0, "JPY": 0, "KRW": 0, "IDR": 0, "CLP": 0, "ISK": 0, "BHD": 3, "JOD": 3, "KWD": 3, "OMR": 3}
MAX_AMOUNT = Decimal("99999999999999.99")  # numeric(16,2)
SCHEDULE_KINDS = ("deposit", "second", "final", "other")
MAX_SCHEDULE_ROWS = 10
# What a sent invoice's status means for its trip case (design §4).
CASE_STATUS_OF_INVOICE = {"sent": "invoice_sent", "partially_paid": "payment_pending", "paid": "paid"}
DIGIT_RUN = re.compile(r"\d(?:[ -]?\d)*")


def minor_units(currency: str) -> int:
    return MINOR_UNITS.get((currency or "").upper(), 2)


def quantum(currency: str) -> Decimal:
    return Decimal(1).scaleb(-minor_units(currency))


def parse_amount(value: Any, currency: str, *, field: str = "amount", allow_negative: bool = False) -> Decimal:
    """A money amount in ``currency``: a finite decimal with at most the currency's minor units, within numeric(16,2).
    Floats are refused (they cannot carry a cent exactly)."""
    if isinstance(value, float) or isinstance(value, bool):
        raise Invalid("invalid_amount", f"{field} must be a decimal string, not a float")
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise Invalid("invalid_amount", f"{field} is not a number") from None
    if not amount.is_finite():
        raise Invalid("invalid_amount", f"{field} is not a number")
    if amount != amount.quantize(quantum(currency), rounding=ROUND_HALF_UP):
        raise Invalid("invalid_amount", f"{field} has more decimals than {currency} allows ({minor_units(currency)})")
    if abs(amount) > MAX_AMOUNT:
        raise Invalid("invalid_amount", f"{field} is too large")
    if amount < 0 and not allow_negative:
        raise Invalid("invalid_amount", f"{field} must not be negative")
    return amount.quantize(Decimal("0.01"))


def round_money(amount: Decimal, currency: str) -> Decimal:
    return amount.quantize(quantum(currency), rounding=ROUND_HALF_UP).quantize(Decimal("0.01"))


def line_amount(quantity: Decimal, unit_price: Decimal) -> Decimal:
    """The amount of an invoice line: round(quantity x unit price, 2) (bk-028 ``invoice_items_amount_chk``)."""
    return (quantity * unit_price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ── Schedule ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SchedulePolicy:
    deposit_pct: Decimal = Decimal(30)
    deposit_due_days: int = 3
    balance_due_days_before_start: int = 30


def default_schedule(
    total: Decimal, currency: str, *, issue_day: date, start: date | None, policy: SchedulePolicy
) -> list[dict[str, Any]]:
    """Deposit (``deposit_pct`` of the total, due ``deposit_due_days`` after the issue day) + final (the rest, due
    ``balance_due_days_before_start`` before the start date). One payment when there is no start date, the balance
    would be due before (or with) the deposit, or the deposit rounds to nothing (design §11 Q4). The deposit is rounded
    to the currency's minor unit and the remainder goes on the last row, so the rows add up to the total exactly."""
    deposit_due = issue_day + timedelta(days=policy.deposit_due_days)
    balance_due = start - timedelta(days=policy.balance_due_days_before_start) if start else None
    single = [{"seq": 1, "kind": "final", "amount": total, "due_date": deposit_due}]
    if balance_due is None or balance_due <= deposit_due or not (0 < policy.deposit_pct < 100):
        return single
    deposit = round_money(total * policy.deposit_pct / Decimal(100), currency)
    if deposit <= 0 or deposit >= total:
        return single
    return [
        {"seq": 1, "kind": "deposit", "amount": deposit, "due_date": deposit_due},
        {"seq": 2, "kind": "final", "amount": total - deposit, "due_date": balance_due},
    ]


def normalize_schedule(rows: list[dict[str, Any]], total: Decimal, currency: str) -> list[dict[str, Any]]:
    """Staff-given rows: 1..10, kinds known, amounts > 0 in minor units, a due date each; numbered in the given order;
    they must add up to the total (422 ``schedule_mismatch``)."""
    if not 1 <= len(rows) <= MAX_SCHEDULE_ROWS:
        raise Invalid("invalid_schedule", f"a payment schedule has 1 to {MAX_SCHEDULE_ROWS} rows")
    out = []
    for seq, row in enumerate(rows, start=1):
        kind = row.get("kind")
        if kind not in SCHEDULE_KINDS:
            raise Invalid("invalid_schedule", f"row {seq}: kind must be one of {', '.join(SCHEDULE_KINDS)}")
        amount = parse_amount(row.get("amount"), currency, field=f"schedule row {seq} amount")
        if amount <= 0:
            raise Invalid("invalid_schedule", f"row {seq}: the amount must be more than 0")
        due = row.get("due_date")
        if not isinstance(due, date):
            raise Invalid("invalid_schedule", f"row {seq}: a due date is required")
        out.append({"seq": seq, "kind": kind, "amount": amount, "due_date": due})
    planned = sum((r["amount"] for r in out), Decimal(0))
    if planned != total:
        raise Invalid(
            "schedule_mismatch",
            f"the schedule adds up to {planned} {currency} but the invoice total is {total} {currency}",
            schedule_total=str(planned),
            total=str(total),
        )
    return out


def waterfall(rows: list[dict[str, Any]], net_paid: Decimal) -> list[Decimal]:
    """The part of each row the net verified payments cover, oldest due first (the bk-028 view's rule); the result is
    in the order of ``rows``."""
    order = sorted(range(len(rows)), key=lambda i: (rows[i]["due_date"], rows[i]["seq"]))
    covered = [Decimal(0)] * len(rows)
    left = max(net_paid, Decimal(0))
    for i in order:
        take = min(rows[i]["amount"], left)
        covered[i] = take
        left -= take
    return covered


# ── Idempotency and references ───────────────────────────────────────────────


def _canonical(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _canonical(v) for k, v in value.items() if v is not None}
    if isinstance(value, list | tuple):
        return [_canonical(v) for v in value]
    return value


def request_hash(body: dict[str, Any]) -> str:
    """SHA-256 of the canonical JSON of a request body (sorted keys, decimals without trailing zeros, ISO dates, None
    fields dropped): the same request under the same Idempotency-Key gives the same hash (critic B6)."""
    canonical = json.dumps(_canonical(body), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def looks_like_card_number(text: str | None) -> bool:
    """True when ``text`` holds a run of 13-19 digits (single spaces / dashes allowed between them) starting with a
    card network digit (3-6) that passes the Luhn check: a payment card number, which never goes into Booking (design
    §3.3, DM-6.1 ``payment`` = references only). Longer runs (bank transaction ids) are not card numbers."""
    if not text:
        return False
    for match in DIGIT_RUN.finditer(text):
        digits = re.sub(r"\D", "", match.group(0))
        if 13 <= len(digits) <= 19 and digits[0] in "3456" and _luhn(digits):
            return True
    return False


def idempotency_key(value: str | None) -> str:
    key = (value or "").strip()
    if not 8 <= len(key) <= 200:
        raise Invalid("idempotency_key_required", "send an Idempotency-Key header of 8 to 200 characters")
    return key
