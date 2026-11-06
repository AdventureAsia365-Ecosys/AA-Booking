"""AA-682 S3 (bk-028) pure rules: money amounts in minor units without floats, the default schedule (deposit % with
the remainder on the last row, one payment when the trip is close), staff schedules that must add up, the waterfall,
the canonical request hash of an Idempotency-Key call, the card-number refusal, the case track (supply only since the
customer invoices retired, AA-1046) and the billing permissions (preview never, verify admin only)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.auth.staff import PREVIEW_PERMISSIONS, Staff
from app.errors import Invalid
from app.ops import money, track

ISSUE = date(2026, 11, 2)
POLICY = money.SchedulePolicy(Decimal(30), 3, 30)


def staff(role: str, sub: str = "u-1", *, preview: bool = False) -> Staff:
    return Staff(cis_user_id=sub, email=None, name=sub, role=role, active=True, cis_role=None, preview=preview)


# ── Amounts ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "currency", "expected"),
    [
        ("720", "USD", Decimal("720.00")),
        ("0.10", "USD", Decimal("0.10")),
        (Decimal("1500"), "VND", Decimal("1500.00")),
        (12, "EUR", Decimal("12.00")),
    ],  # fmt: skip
)
def test_parse_amount(value: object, currency: str, expected: Decimal) -> None:
    assert money.parse_amount(value, currency) == expected


@pytest.mark.parametrize("value", [0.1, "1.005", "abc", "NaN", "-1", True, "100000000000000.00"])
def test_parse_amount_refuses(value: object) -> None:
    with pytest.raises(Invalid):
        money.parse_amount(value, "USD")


def test_vnd_has_no_minor_unit_and_negative_adjustments() -> None:
    with pytest.raises(Invalid, match="more decimals than VND"):
        money.parse_amount("10.50", "VND")
    assert money.parse_amount("-100", "USD", allow_negative=True) == Decimal("-100.00")
    assert money.line_amount(Decimal("3"), Decimal("33.33")) == Decimal("99.99")


@pytest.mark.parametrize(
    ("quantity", "unit", "amount"),
    [
        ("0.5", "0.05", "0.03"),  # 0.025: half away from zero (banker's rounding would give 0.02)
        ("0.5", "-0.05", "-0.03"),  # -0.025: away from zero on the negative side too
        ("2.5", "0.01", "0.03"),
        ("2.5", "-0.01", "-0.03"),
        ("1.5", "0.01", "0.02"),
        ("0.25", "-0.07", "-0.02"),  # -0.0175: not a tie
    ],
)
def test_line_amount_rounds_like_postgres(quantity: str, unit: str, amount: str) -> None:
    """Review S253: bk-028 checks amount = round(quantity * unit_price, 2); PostgreSQL rounds numeric half away from
    zero, and so must the API (ROUND_HALF_UP), whatever the thread's decimal context says."""
    assert money.line_amount(Decimal(quantity), Decimal(unit)) == Decimal(amount)
    assert money.round_money(Decimal(quantity) * Decimal(unit), "USD") == Decimal(amount)


# ── Schedule ─────────────────────────────────────────────────────────────────


def test_default_schedule_deposit_and_balance() -> None:
    rows = money.default_schedule(Decimal("2400"), "USD", issue_day=ISSUE, start=ISSUE + timedelta(days=60),
                                  policy=POLICY)  # fmt: skip
    assert [(r["kind"], r["amount"], r["due_date"]) for r in rows] == [
        ("deposit", Decimal("720.00"), ISSUE + timedelta(days=3)),
        ("final", Decimal("1680.00"), ISSUE + timedelta(days=30)),
    ]


def test_default_schedule_rounds_to_minor_units_and_keeps_the_sum() -> None:
    rows = money.default_schedule(Decimal("1000.01"), "USD", issue_day=ISSUE, start=ISSUE + timedelta(days=90),
                                  policy=POLICY)  # fmt: skip
    assert [r["amount"] for r in rows] == [Decimal("300.00"), Decimal("700.01")]
    vnd = money.default_schedule(Decimal("1000001"), "VND", issue_day=ISSUE, start=ISSUE + timedelta(days=90),
                                 policy=POLICY)  # fmt: skip
    assert [r["amount"] for r in vnd] == [Decimal("300000.00"), Decimal("700001.00")]
    assert sum(r["amount"] for r in vnd) == Decimal("1000001")


@pytest.mark.parametrize("start", [None, ISSUE + timedelta(days=20), ISSUE + timedelta(days=33)])
def test_short_lead_time_or_no_start_is_one_payment(start: date | None) -> None:
    rows = money.default_schedule(Decimal("2400"), "USD", issue_day=ISSUE, start=start, policy=POLICY)
    assert [(r["kind"], r["amount"], r["due_date"]) for r in rows] == [
        ("final", Decimal("2400"), ISSUE + timedelta(days=3))
    ]


def test_normalize_schedule() -> None:
    rows = [{"kind": "deposit", "amount": "500", "due_date": ISSUE}, {"kind": "final", "amount": "1900",
                                                                         "due_date": ISSUE}]  # fmt: skip
    out = money.normalize_schedule(rows, Decimal("2400"), "USD")
    assert [(r["seq"], r["amount"]) for r in out] == [(1, Decimal("500.00")), (2, Decimal("1900.00"))]
    with pytest.raises(Invalid) as exc:
        money.normalize_schedule(rows[:1], Decimal("2400"), "USD")
    assert exc.value.code == "schedule_mismatch" and exc.value.extra["schedule_total"] == "500.00"
    for bad in ([], [{"kind": "x", "amount": "1", "due_date": ISSUE}], [{"kind": "final", "amount": "1"}]):
        with pytest.raises(Invalid):
            money.normalize_schedule(bad, Decimal("1"), "USD")


def test_waterfall_oldest_due_first() -> None:
    rows = [
        {"seq": 2, "amount": Decimal("1680"), "due_date": ISSUE + timedelta(days=30)},
        {"seq": 1, "amount": Decimal("720"), "due_date": ISSUE},
    ]
    assert money.waterfall(rows, Decimal("1000")) == [Decimal("280"), Decimal("720")]
    assert money.waterfall(rows, Decimal("-5")) == [Decimal(0), Decimal(0)]
    assert money.waterfall(rows, Decimal("9999")) == [Decimal("1680"), Decimal("720")]


# ── Idempotency and references ───────────────────────────────────────────────


def test_request_hash_is_canonical() -> None:
    a = {"amount": Decimal("720.00"), "method": "cash", "received_on": ISSUE, "note": None}
    b = {"method": "cash", "received_on": ISSUE, "amount": Decimal("720")}
    assert money.request_hash(a) == money.request_hash(b)
    assert money.request_hash(a) != money.request_hash({**b, "amount": Decimal("721")})


@pytest.mark.parametrize(
    ("text", "card"),
    [("4111 1111 1111 1111", True), ("paid by 5555-5555-5555-4444 yesterday", True), ("VCB 2026-10-27 0001", False),
     ("TX 4111111111111112", False), ("FT26300123456789012345", False), (None, False), ("1234567890123", False)],
)  # fmt: skip
def test_card_numbers_are_refused(text: str | None, card: bool) -> None:
    assert money.looks_like_card_number(text) is card


def test_idempotency_key_length() -> None:
    assert money.idempotency_key("  abcdefgh ") == "abcdefgh"
    for bad in (None, "", "short", "x" * 201):
        with pytest.raises(Invalid) as exc:
            money.idempotency_key(bad)
        assert exc.value.code == "idempotency_key_required"


# ── Case track (supply + money) ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "live", "asked", "target"),
    [
        ("confirmed", 1, 1, "supplier_confirming"),
        ("confirmed", 1, 0, None),
        ("supplier_confirming", 1, 1, None),  # keeps its status while a booking is live
        ("supplier_confirming", 0, 1, "confirmed"),
        # AA-1046: the money track is gone; a kept b2c case left in a money status never moves
        ("invoice_sent", 0, 0, None),
        ("payment_pending", 1, 1, None),
        ("paid", 0, 0, None),
        ("pre_departure", 0, 0, None),  # travel statuses never step back
        ("cancelled", 0, 0, None),
    ],
)
def test_next_status(status: str, live: int, asked: int, target: str | None) -> None:
    assert track.next_status(status, live=live, asked=asked) == target


# ── Permissions and actions ──────────────────────────────────────────────────


def test_billing_permissions() -> None:
    assert staff("sales").can("billing.write") and not staff("sales").can("payment.verify")
    assert staff("admin").can("payment.verify")
    assert not staff("contracting").can("billing.read") and not staff("content_creator").can("billing.read")
    assert not {"billing.read", "billing.write", "payment.verify"} & PREVIEW_PERMISSIONS  # critic note 8
    assert not staff("admin", preview=True).can("billing.read")


def test_finance_issues_invoices_and_records_payments_but_does_not_verify() -> None:
    """AA-1035 R2 (root ADR 0019 d9): finance gates the invoice and payment routes; verifying stays admin (D6)."""
    fin = staff("finance")
    assert fin.can("billing.read") and fin.can("billing.write")
    assert not fin.can("payment.verify")
    for other in (
        "trip_case.read",
        "customer.read",
        "pricing.read",
        "supply.read",
        "staff.admin",
        "audit.read",
        "bill.read",
        "payout.write",
    ):
        assert not fin.can(other), other
    assert not staff("finance", preview=True).can("billing.read")
