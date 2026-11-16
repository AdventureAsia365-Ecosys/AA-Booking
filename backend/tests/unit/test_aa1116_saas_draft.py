"""AA-1116 (option B, CIS 438, contract tenant.entitlements.v1 1.9): the monthly software draft reads the agency's CIS
billing terms. Pure line rules (:func:`app.agency_billing.drafts.saas_lines`) and the draft run on a scripted
connection: the CIS billing currency (VND from the VND list instead of ``currency_unsupported``), the seat minimum, the
agreed price while it is in force, a free pilot drafted at zero, an annual subscription flagged for a manual invoice,
a currency change clearing the old currency's draft (and drafting nothing when the month was already issued in the old
currency), the discount check line, and the 377 fallback (Booking ``saas_currency``). The same on
PostgreSQL: tests/integration/test_aa1116_saas_draft_pg.py. Names are invented."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from app.agency_billing import cis, drafts
from app.catalog import liveness
from tests.unit.fakes import VIEW_READY_FRAGMENT, ScriptedConn

T1 = uuid.UUID("00000000-0000-4000-8000-000000001116")
INV = "10000000-0000-4000-8000-000000001116"
OLD = "10000000-0000-4000-8000-000000001117"
NOV = date(2026, 11, 1)
TERMS = {"id": "t", "party_tenant_id": None, "deposit_pct": Decimal("0.3000"), "balance_days_before_departure": 30,
         "saas_due_days": 14, "settlement_due_days": 14, "warn_after_days": 1, "suspend_after_days": 30,
         "saas_currency": "USD"}  # fmt: skip


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    liveness.reset_cache()


def sub(product: str, plan: str, usd: str | None, vnd: str | None = None, **over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "tenant_id": T1, "product": product, "plan_key": plan, "plan_name": plan.split(".")[1].title(),
        "price_usd_monthly": Decimal(usd) if usd else None, "price_unit": "tenant", "seats": None,
        "seats_included": None, "price_extra_seat_usd": None, "kind": "demo", "billing_country": "VN",
        **dict.fromkeys(cis.TERMS_COLUMNS),
        "price_vnd_monthly": Decimal(vnd) if vnd else None,
    }  # fmt: skip
    row.update(over)
    return row


def _shape(lines: list[dict[str, Any]]) -> list[tuple[str, Decimal, Decimal]]:
    return [(ln["line_role"], ln["quantity"], ln["unit_price"]) for ln in lines]


def test_seat_minimum_raises_the_quantity() -> None:
    lines, flags = drafts.saas_lines([sub("crm", "crm.team", "19", price_unit="seat", seats=1, seats_min=3)], NOV)
    assert _shape(lines) == [("subscription", Decimal(3), Decimal(19))]
    assert lines[0]["description"].endswith(", 3 seats")
    assert flags == {"price_missing": 0, "agreed": 0, "annual_manual": 0, "pilot_free": 0, "discount_check": 0}
    more, _ = drafts.saas_lines([sub("crm", "crm.team", "19", price_unit="seat", seats=5, seats_min=3)], NOV)
    assert _shape(more) == [("subscription", Decimal(5), Decimal(19))]
    none, _ = drafts.saas_lines([sub("crm", "crm.team", "19", price_unit="seat")], NOV)
    assert _shape(none) == [("subscription", Decimal(1), Decimal(19))]


def test_vnd_lines_read_the_vnd_list() -> None:
    lines, flags = drafts.saas_lines(
        [
            sub("content", "content.growth", "149", "3790000"),
            sub("tripplanner", "tripplanner.hosted", "49", "1250000", seats=4, seats_included=2,
                price_extra_seat_usd=Decimal(9), price_extra_seat_vnd=Decimal(230000)),
            sub("crm", "crm.pro", "39", None),
        ],
        NOV,
        "VND",
    )  # fmt: skip
    assert _shape(lines) == [
        ("subscription", Decimal(1), Decimal(3790000)),
        ("subscription", Decimal(1), Decimal(1250000)),
        ("extra_seats", Decimal(2), Decimal(230000)),
        ("subscription", Decimal(1), Decimal(0)),
    ]
    assert flags["price_missing"] == 1 and "no VND price" in lines[-1]["description"]


def test_agreed_price_replaces_the_unit_price_while_in_force() -> None:
    seat = sub("crm", "crm.team", "19", price_unit="seat", seats=4, seats_min=3, agreed_price_monthly=Decimal(15),
               agreed_currency="USD", agreed_until=date(2027, 10, 31))  # fmt: skip
    lines, flags = drafts.saas_lines([seat], NOV)
    assert _shape(lines) == [("subscription", Decimal(4), Decimal(15))] and flags["agreed"] == 1
    assert "agreed price until 2027-10-31" in lines[0]["description"]
    # the lock ended before the period: the list price again
    lines, flags = drafts.saas_lines([{**seat, "agreed_until": date(2026, 10, 31)}], NOV)
    assert _shape(lines) == [("subscription", Decimal(4), Decimal(19))] and flags["agreed"] == 0
    # the last day of the lock is the period start: still in force; no end date: in force
    assert drafts.saas_lines([{**seat, "agreed_until": NOV}], NOV)[0][0]["unit_price"] == Decimal(15)
    assert drafts.saas_lines([{**seat, "agreed_until": None}], NOV)[0][0]["unit_price"] == Decimal(15)
    # agreed in another currency than the invoice: the list
    assert drafts.saas_lines([{**seat, "agreed_currency": "VND"}], NOV)[0][0]["unit_price"] == Decimal(19)
    # a plan fee plan: the agreed price replaces the fee, extra seats stay at the list
    site = sub("tripplanner", "tripplanner.pro", "99", seats=3, seats_included=2, price_extra_seat_usd=Decimal(9),
               agreed_price_monthly=Decimal(80), agreed_currency="USD")  # fmt: skip
    assert _shape(drafts.saas_lines([site], NOV)[0]) == [
        ("subscription", Decimal(1), Decimal(80)),
        ("extra_seats", Decimal(1), Decimal(9)),
    ]


def test_annual_subscription_is_flagged_for_a_manual_invoice() -> None:
    lines, flags = drafts.saas_lines([sub("content", "content.growth", "149", billing_interval="year")], NOV)
    assert _shape(lines) == [("subscription", Decimal(1), Decimal(0))]
    assert lines[0]["description"].endswith("annual: invoice by hand") and flags["annual_manual"] == 1


def test_free_pilot_is_drafted_at_zero_with_the_list_in_the_text() -> None:
    lines, flags = drafts.saas_lines(
        [
            sub("content", "content.growth", "149", "3790000", bill_free=True, is_pilot=True),
            sub("tripplanner", "tripplanner.hosted", "49", "1250000", seats=3, seats_included=2,
                price_extra_seat_vnd=Decimal(230000), bill_free=True, is_pilot=True),
        ],
        NOV,
        "VND",
    )  # fmt: skip
    assert [ln["unit_price"] for ln in lines] == [Decimal(0)] * 3
    assert lines[0]["description"].endswith("pilot, free (list 3,790,000 VND)")
    assert lines[2]["description"].endswith("pilot, free (list 230,000 VND)")
    assert flags["pilot_free"] == 2
    # a pilot that is not free (cis.billing.pilot_free off): billed
    paid, _ = drafts.saas_lines([sub("content", "content.growth", "149", is_pilot=True, bill_free=False)], NOV)
    assert paid[0]["unit_price"] == Decimal(149)


def test_option_b_draft_with_an_amount_ends_with_the_discount_check_line() -> None:
    # CIS 438 terms read (billing_currency set) and an amount to bill: finance must apply the quote's discount
    lines, flags = drafts.saas_lines([sub("content", "content.growth", "149", billing_currency="USD"),
                                      sub("crm", "crm.pro", "39", billing_currency="USD")], NOV)  # fmt: skip
    assert _shape(lines)[-1] == ("subscription", Decimal(1), Decimal(0)) and len(lines) == 3
    assert lines[-1]["description"] == drafts.DISCOUNT_CHECK_TEXT and flags["discount_check"] == 1
    assert (lines[-1]["product"], lines[-1]["plan_key"]) == ("content", "content.growth")
    # nothing to discount: a free pilot, an annual subscription, a plan without a price
    for row in (sub("content", "content.growth", "149", billing_currency="USD", bill_free=True),
                sub("content", "content.growth", "149", billing_currency="USD", billing_interval="year"),
                sub("content", "content.enterprise", None, billing_currency="USD")):  # fmt: skip
        lines, flags = drafts.saas_lines([row], NOV)
        assert flags["discount_check"] == 0 and all(ln["description"] != drafts.DISCOUNT_CHECK_TEXT for ln in lines)
    # before CIS 438 (no billing_currency): no option B discount, no line
    lines, flags = drafts.saas_lines([sub("content", "content.growth", "149")], NOV)
    assert len(lines) == 1 and flags["discount_check"] == 0


# ── The run on a scripted connection ─────────────────────────────────────────


def saas_conn(rows: list[dict[str, Any]], *, terms_ready: bool = True, others: Any = None,
              issued_other: str | None = None) -> ScriptedConn:  # fmt: skip
    c = ScriptedConn()
    # the 377 view is always readable; the 438 columns only when ``terms_ready``
    c.on(VIEW_READY_FRAGMENT, lambda *a: terms_ready or "billing_currency" not in a[-1])
    c.on("FROM catalog.v_tenant_billing_v1", rows)
    c.on("SELECT tenant_id, agency_name FROM booking.b2b_partners", [{"tenant_id": T1, "agency_name": "Juniper"}])
    c.on("FROM booking.billing_terms", TERMS)
    c.on("currency <> $3 AND status = 'draft'", others or [])
    c.on("currency <> $3 AND status NOT IN ('draft', 'void')", issued_other)
    c.on("status FROM booking.agency_invoice WHERE party_tenant_id", None)
    c.on("FROM booking.tax_rule", None)
    c.on("SELECT current_date", date(2026, 11, 1))
    c.on("INSERT INTO booking.agency_invoice (", INV)
    c.on("coalesce(max(line_no), 0) FROM", 0)
    c.on("SELECT tax_rate FROM", Decimal(0))
    c.on("coalesce(sum(amount), 0) FROM booking.agency_invoice_line", Decimal(0))
    return c


def _view_sql(c: ScriptedConn) -> str:
    return next(flat for flat, _ in c.calls if "FROM catalog.v_tenant_billing_v1" in flat)


def _inserted_invoice_currency(c: ScriptedConn) -> str:
    call = c.sql("INSERT INTO booking.agency_invoice (")
    return call[0][4]


@pytest.mark.asyncio
async def test_run_drafts_a_vnd_agency_in_vnd() -> None:
    c = saas_conn([sub("content", "content.growth", "149", "3790000", billing_currency="VND")])
    out = await drafts.run_saas(c, period="2026-11", tenant_id=None, actor="job:x")
    assert out["currency_unsupported"] == 0 and (out["invoices_created"], out["lines_added"]) == (1, 2)
    assert _inserted_invoice_currency(c) == "VND"
    line = c.sql("INSERT INTO booking.agency_invoice_line")[0]
    assert line[9] == Decimal(3790000) and line[10] == Decimal("3790000.00")
    assert "billing_currency" in _view_sql(c)


@pytest.mark.asyncio
async def test_run_counts_the_flags_and_refuses_another_currency() -> None:
    c = saas_conn([sub("content", "content.growth", "149", billing_currency="USD", billing_interval="year"),
                   sub("crm", "crm.team", "19", billing_currency="USD", price_unit="seat", seats=1, seats_min=3,
                       bill_free=True)])  # fmt: skip
    out = await drafts.run_saas(c, period="2026-11", tenant_id=None, actor="job:x")
    assert (out["annual_manual"], out["pilot_free"], out["lines_added"], out["discount_check"]) == (1, 1, 2, 0)
    liveness.reset_cache()
    out = await drafts.run_saas(saas_conn([sub("content", "content.growth", "149", billing_currency="EUR")]),
                                period="2026-11", tenant_id=None, actor="job:x")  # fmt: skip
    assert out["currency_unsupported"] == 1 and out["invoices_created"] == 0


@pytest.mark.asyncio
async def test_run_clears_the_old_currency_draft() -> None:
    c = saas_conn([sub("content", "content.growth", "149", "3790000", billing_currency="VND")],
                  others=[{"id": OLD}])  # fmt: skip
    out = await drafts.run_saas(c, period="2026-11", tenant_id=None, actor="job:x")
    assert out["currency_moved"] == 1
    deletes = c.sql("DELETE FROM booking.agency_invoice_line")
    assert deletes and deletes[0][0] == OLD


@pytest.mark.asyncio
async def test_run_drafts_nothing_when_the_month_was_issued_in_the_old_currency() -> None:
    # November issued in USD, then CIS moved the agency to VND: no VND draft next to the issued USD invoice
    c = saas_conn([sub("content", "content.growth", "149", "3790000", billing_currency="VND")], issued_other="USD")
    out = await drafts.run_saas(c, period="2026-11", tenant_id=None, actor="job:x")
    assert (out["period_issued"], out["currency_moved_issued"], out["invoices_created"]) == (1, 1, 0)
    assert not c.sql("INSERT INTO booking.agency_invoice") and not c.sql("DELETE FROM booking.agency_invoice_line")
    assert c.sql("status NOT IN ('draft', 'void')")[0][1:] == (NOV, "VND")


@pytest.mark.asyncio
async def test_run_counts_the_discount_check() -> None:
    c = saas_conn([sub("content", "content.growth", "149", billing_currency="USD")])
    out = await drafts.run_saas(c, period="2026-11", tenant_id=None, actor="job:x")
    assert (out["discount_check"], out["lines_added"]) == (1, 2)
    assert c.sql("INSERT INTO booking.agency_invoice_line")[-1][7] == drafts.DISCOUNT_CHECK_TEXT


@pytest.mark.asyncio
async def test_run_before_cis_438_keeps_the_booking_currency() -> None:
    c = saas_conn([sub("content", "content.growth", "149")], terms_ready=False)
    out = await drafts.run_saas(c, period="2026-11", tenant_id=None, actor="job:x")
    assert out["invoices_created"] == 1 and _inserted_invoice_currency(c) == "USD"
    assert "billing_currency" not in _view_sql(c)
