"""AA-1116 (option B, CIS 438 / 444, contract tenant.entitlements.v1 1.9 / 1.10): the monthly software draft reads the
agency's CIS billing terms and the CIS quote's discount. Pure line rules (:func:`app.agency_billing.drafts.saas_lines`)
and the draft run on a scripted connection: the CIS billing currency (VND from the VND list instead of
``currency_unsupported``), the seat minimum, the agreed price while it is in force, a free pilot drafted at zero, an
annual subscription flagged for a manual invoice, a currency change clearing the old currency's draft (and drafting
nothing when the month was already issued in the old currency, then the next month in the new one), the discount lines
(the 409.50 USD combo case of the CIS quote, the rounding of the running total, VND, nothing before CIS 444, a re-run
replacing them), and the 377 fallback (Booking ``saas_currency``). The same on PostgreSQL:
tests/integration/test_aa1116_saas_draft_pg.py. Names are invented."""

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
    assert flags == {"price_missing": 0, "agreed": 0, "annual_manual": 0, "pilot_free": 0, "discounted": 0}
    more, _ = drafts.saas_lines([sub("crm", "crm.team", "19", price_unit="seat", seats=5, seats_min=3)], NOV)
    assert _shape(more) == [("subscription", Decimal(5), Decimal(19))]
    none, _ = drafts.saas_lines([sub("crm", "crm.team", "19", price_unit="seat")], NOV)
    assert _shape(none) == [("subscription", Decimal(1), Decimal(19))]


def test_seat_overage_on_a_per_seat_plan() -> None:
    """AA-1116 S284: the seats a per-seat plan includes on the subscription line, the seats above them on an
    extra-seat line, at the plan's extra-seat price in the invoice currency, else at the seat price (same total)."""
    crm = sub("crm", "crm.quill", "19", "490000", price_unit="seat", seats=5, seats_included=3, seats_min=3)
    lines, _ = drafts.saas_lines([crm], NOV)
    assert _shape(lines) == [("subscription", Decimal(3), Decimal(19)), ("extra_seats", Decimal(2), Decimal(19))]
    assert lines[0]["description"].endswith(", 3 seats") and lines[1]["description"].startswith("Extra seats over 3, ")
    priced = {**crm, "price_extra_seat_usd": Decimal(25), "price_extra_seat_vnd": Decimal(650000)}
    assert _shape(drafts.saas_lines([priced], NOV)[0])[1] == ("extra_seats", Decimal(2), Decimal(25))
    assert _shape(drafts.saas_lines([priced], NOV, "VND")[0]) == [
        ("subscription", Decimal(3), Decimal(490000)),
        ("extra_seats", Decimal(2), Decimal(650000)),
    ]
    # at or under the included seats, or no included seats on the plan: one line, as before
    assert _shape(drafts.saas_lines([{**priced, "seats": 2}], NOV)[0]) == [("subscription", Decimal(3), Decimal(19))]
    assert _shape(drafts.saas_lines([{**priced, "seats_included": None}], NOV)[0]) == [
        ("subscription", Decimal(5), Decimal(19))
    ]
    # an agreed (locked) seat price covers the extra seats
    locked = {**priced, "agreed_price_monthly": Decimal(17), "agreed_currency": "USD", "agreed_until": None}
    assert _shape(drafts.saas_lines([locked], NOV)[0]) == [
        ("subscription", Decimal(3), Decimal(17)),
        ("extra_seats", Decimal(2), Decimal(17)),
    ]
    # the discount takes its share of both lines: (3 x 19 + 2 x 25) x 10 % = 10.70
    lines, flags = drafts.saas_lines([{**priced, "discount_pct": Decimal(10), "discount_kind": "combo"}], NOV)
    assert _shape(lines)[-1] == ("adjustment", Decimal(1), Decimal("-10.70")) and flags["discounted"] == 1
    # a free pilot keeps both lines at zero with their list amounts in the text
    lines, flags = drafts.saas_lines([{**priced, "bill_free": True}], NOV)
    assert [ln["unit_price"] for ln in lines] == [Decimal(0), Decimal(0)]
    assert "(list 50.00 USD)" in lines[1]["description"]


@pytest.mark.parametrize(
    ("seats", "included", "seats_min", "want"),
    [(5, 3, 3, (3, 2)), (1, 3, 3, (3, 0)), (None, 3, 3, (3, 0)), (5, None, 3, (5, 0)), (8, 3, 5, (5, 3)),
     (2, 3, None, (2, 0)), (None, None, None, (1, 0))],
)  # fmt: skip
def test_seat_split_matches_the_cis_quote(seats: Any, included: Any, seats_min: Any, want: tuple[int, int]) -> None:
    assert drafts.seat_split(seats, included, seats_min) == want


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


def _combo_agency(pct: str | None = "10.00", kind: str | None = "combo") -> list[dict[str, Any]]:
    """The CIS quote's audit case (S282): a plan fee 349, three seats at 19 and a hosted site 49 = 455 USD; the combo
    takes 10 % off every line = 409.50."""
    terms = {"billing_currency": "USD", "discount_pct": Decimal(pct) if pct else None, "discount_kind": kind}
    return [
        sub("content", "content.lumen", "349", **terms),
        sub("crm", "crm.quill", "19", price_unit="seat", seats=3, seats_min=1, **terms),
        sub("tripplanner", "tripplanner.harbor", "49", **terms),
    ]


def _total(lines: list[dict[str, Any]]) -> Decimal:
    return sum((ln["quantity"] * ln["unit_price"] for ln in lines), Decimal(0))


def test_combo_discount_matches_the_cis_quote() -> None:
    lines, flags = drafts.saas_lines(_combo_agency(), NOV)
    assert _shape(lines) == [
        ("subscription", Decimal(1), Decimal(349)),
        ("adjustment", Decimal(1), Decimal("-34.90")),
        ("subscription", Decimal(3), Decimal(19)),
        ("adjustment", Decimal(1), Decimal("-5.70")),
        ("subscription", Decimal(1), Decimal(49)),
        ("adjustment", Decimal(1), Decimal("-4.90")),
    ]
    assert _total(lines) == Decimal("409.50") and flags["discounted"] == 3
    assert lines[1]["description"] == "Combo discount 10%: Lumen (Content), November 2026"
    # the job's own adjustment: a product, no plan key (bk-041 source check; DELETE_JOB_LINES_SQL finds it by that)
    assert (lines[1]["product"], lines[1]["plan_key"]) == ("content", None)


def test_no_discount_without_a_pct() -> None:
    for pct, kind in ((None, None), ("0.00", None)):
        lines, flags = drafts.saas_lines(_combo_agency(pct, kind), NOV)
        assert [ln["line_role"] for ln in lines] == ["subscription"] * 3 and flags["discounted"] == 0
        assert _total(lines) == Decimal(455)
    # before CIS 444 the rows carry no discount columns at all (cis.subscriptions fills them with None)
    lines, flags = drafts.saas_lines([sub("content", "content.lumen", "349", billing_currency="USD")], NOV)
    assert len(lines) == 1 and flags["discounted"] == 0


def test_founding_discount_on_one_line_with_extra_seats_and_an_agreed_price() -> None:
    lines, _ = drafts.saas_lines(
        [
            # a plan fee at an agreed 80 plus 2 extra seats at 9 = 98; founding 30 % of it
            sub("tripplanner", "tripplanner.harbor", "99", seats=4, seats_included=2, price_extra_seat_usd=Decimal(9),
                agreed_price_monthly=Decimal(80), agreed_currency="USD", discount_pct=Decimal("30.00"),
                discount_kind="founding"),
            sub("crm", "crm.quill", "19", price_unit="seat", seats=2),  # not eligible: no share
        ],
        NOV,
    )  # fmt: skip
    assert _shape(lines) == [
        ("subscription", Decimal(1), Decimal(80)),
        ("extra_seats", Decimal(2), Decimal(9)),
        ("adjustment", Decimal(1), Decimal("-29.40")),
        ("subscription", Decimal(2), Decimal(19)),
    ]
    assert lines[2]["description"].startswith("Founding offer discount 30%: Harbor (Trip planner)")


def test_discount_rounds_the_running_total_like_the_quote() -> None:
    # three lines of 33.33 at 12.5 % = 4.16625 each: rounded one by one 3 x 4.17 = 12.51, the quote rounds the sum once
    rows = [sub(p, f"{p}.kite", "33.33", billing_currency="USD", discount_pct=Decimal("12.50"), discount_kind="combo")
            for p in ("content", "crm", "tripplanner")]  # fmt: skip
    lines, _ = drafts.saas_lines(rows, NOV)
    cuts = [-ln["unit_price"] for ln in lines if ln["line_role"] == "adjustment"]
    assert cuts == [Decimal("4.17"), Decimal("4.16"), Decimal("4.17")]
    exact = sum(Decimal("33.33") * Decimal("12.50") / 100 for _ in rows)
    assert sum(cuts) == exact.quantize(Decimal("0.01")) == Decimal("12.50")
    assert lines[1]["description"].startswith("Combo discount 12.5%:")


def test_vnd_discount_rounds_to_the_dong() -> None:
    lines, _ = drafts.saas_lines(
        [sub("content", "content.lumen", "349", "3790005", billing_currency="VND", discount_pct=Decimal("10.00"),
             discount_kind="combo")],
        NOV,
        "VND",
    )  # fmt: skip
    assert _shape(lines) == [
        ("subscription", Decimal(1), Decimal(3790005)),
        ("adjustment", Decimal(1), Decimal(-379001)),
    ]


def test_nothing_billed_gets_no_discount() -> None:
    disc = {"billing_currency": "USD", "discount_pct": Decimal("10.00"), "discount_kind": "combo"}
    for row in (sub("content", "content.lumen", "349", bill_free=True, is_pilot=True, **disc),
                sub("content", "content.lumen", "349", billing_interval="year", **disc),
                sub("content", "content.vault", None, **disc)):  # fmt: skip
        lines, flags = drafts.saas_lines([row], NOV)
        assert flags["discounted"] == 0 and all(ln["line_role"] != "adjustment" for ln in lines)
        assert _total(lines) == 0


# ── The run on a scripted connection ─────────────────────────────────────────


def saas_conn(rows: list[dict[str, Any]], *, terms_ready: bool = True, discount_ready: bool = True, others: Any = None,
              issued_other: str | None = None) -> ScriptedConn:  # fmt: skip
    c = ScriptedConn()
    # the 377 view is always readable; the 438 columns only when ``terms_ready``, the 444 ones when ``discount_ready``
    c.on(VIEW_READY_FRAGMENT, lambda *a: ("discount_pct" not in a[-1] or discount_ready)
         and (terms_ready or "billing_currency" not in a[-1]))  # fmt: skip
    c.on("FROM catalog.v_tenant_billing_v1", rows)
    # AA-1191: VND is enabled here (these tests are about the VND list); USD only: test_aa1191_enabled_currencies.py
    c.on("FROM catalog.v_billing_currencies_v1", {"enabled_currencies": ["USD", "VND"]})
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
    assert out["currency_unsupported"] == 0 and (out["invoices_created"], out["lines_added"]) == (1, 1)
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
    assert (out["annual_manual"], out["pilot_free"], out["lines_added"], out["discounted"]) == (1, 1, 2, 0)
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
async def test_run_writes_the_discount_lines() -> None:
    c = saas_conn(_combo_agency())
    out = await drafts.run_saas(c, period="2026-11", tenant_id=None, actor="job:x")
    assert (out["discounted"], out["lines_added"]) == (3, 6)
    inserted = c.sql("INSERT INTO booking.agency_invoice_line")
    # (role, product, plan key, unit price, amount) of each discount line
    assert [(a[3], a[4], a[5], a[9], a[10]) for a in inserted if a[3] == "adjustment"] == [
        ("adjustment", "content", None, Decimal("-34.90"), Decimal("-34.90")),
        ("adjustment", "crm", None, Decimal("-5.70"), Decimal("-5.70")),
        ("adjustment", "tripplanner", None, Decimal("-4.90"), Decimal("-4.90")),
    ]
    assert "discount_pct" in _view_sql(c)


@pytest.mark.asyncio
async def test_run_before_cis_444_reads_no_discount() -> None:
    rows = [{k: v for k, v in r.items() if k not in cis.DISCOUNT_COLUMNS} for r in _combo_agency()]
    c = saas_conn(rows, discount_ready=False)
    out = await drafts.run_saas(c, period="2026-11", tenant_id=None, actor="job:x")
    assert (out["discounted"], out["lines_added"]) == (0, 3)
    assert "billing_currency" in _view_sql(c) and "discount_pct" not in _view_sql(c)


@pytest.mark.asyncio
async def test_rerun_replaces_the_job_discount_lines_only() -> None:
    c = saas_conn(_combo_agency())
    c.on("status FROM booking.agency_invoice WHERE party_tenant_id", {"id": INV, "status": "draft"})
    out = await drafts.run_saas(c, period="2026-11", tenant_id=None, actor="job:x")
    assert out["drafts_updated"] == 1
    # the job's subscription / extra-seat lines and its discount adjustments (a product); finance's (no product) stay
    assert "(line_role = 'adjustment' AND product IS NOT NULL)" in drafts.DELETE_JOB_LINES_SQL
    assert c.sql("DELETE FROM booking.agency_invoice_line")[0] == (INV, ["subscription", "extra_seats"])


@pytest.mark.asyncio
async def test_currency_move_after_an_issued_month_then_the_next_month_in_vnd() -> None:
    # S282: November issued in USD, CIS moved the agency to VND: nothing in VND for November, December in VND with the
    # VND list and its discount
    vnd = [sub("content", "content.lumen", "349", "8800000", billing_currency="VND", discount_pct=Decimal("10.00"),
               discount_kind="combo")]  # fmt: skip
    nov = await drafts.run_saas(saas_conn(vnd, issued_other="USD"), period="2026-11", tenant_id=None, actor="job:x")
    assert (nov["period_issued"], nov["currency_moved_issued"], nov["invoices_created"], nov["discounted"]) == (
        1, 1, 0, 0)  # fmt: skip
    liveness.reset_cache()
    c = saas_conn(vnd)
    dec = await drafts.run_saas(c, period="2026-12", tenant_id=None, actor="job:x")
    assert (dec["invoices_created"], dec["currency_moved_issued"], dec["discounted"]) == (1, 0, 1)
    assert _inserted_invoice_currency(c) == "VND"
    assert [a[9] for a in c.sql("INSERT INTO booking.agency_invoice_line")] == [Decimal(8800000), Decimal(-880000)]


@pytest.mark.asyncio
async def test_run_before_cis_438_keeps_the_booking_currency() -> None:
    c = saas_conn([sub("content", "content.growth", "149")], terms_ready=False)
    out = await drafts.run_saas(c, period="2026-11", tenant_id=None, actor="job:x")
    assert out["invoices_created"] == 1 and _inserted_invoice_currency(c) == "USD"
    assert "billing_currency" not in _view_sql(c)
