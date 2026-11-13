"""CI guard (AA-1091, tech lead rule S278): no pricing policy value is a literal in the pricing code paths.

Every business policy value (quote validity, group size, surcharge rules, thresholds, deposits, ...) is a parameter of
``booking.pricing_policy`` read through :mod:`app.pricing.policy_values` and changed on the staff page Pricing >
Policy. This test reads the source of every module that prices, quotes, holds, cancels or bills a deposit
(:data:`CONSUMERS`) and fails on:

1. a module-level UPPER_CASE constant holding a number that is not in :data:`KNOWN` (each known one says why it is a
   technical bound, or names the follow-up that turns it into a policy parameter). A new constant fails until a person
   decides which it is;
2. a number other than 0, 1, 2, -1 or 100 inside a comparison or an arithmetic expression (``if pax >= 9``,
   ``days * 14``), except the lines in :data:`KNOWN_INLINE`;
3. a ``timedelta`` with a literal other than ``days=1`` (``timedelta(hours=48)``);
4. a read of the retired setting ``b2b_quote_valid_hours`` anywhere in ``app/``.

It also checks that each parameter the registry says is in use (``Param.used_by``) is read by that module, and that
the scanner itself catches a planted value (self-test).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.pricing import policy_params

APP = Path(__file__).resolve().parents[2] / "app"
CONSUMERS = (
    "b2b/quotes.py",
    "b2b/tariff.py",
    "b2b/cancellation.py",
    "b2b/holds.py",
    "b2b/price_adjustments.py",
    "b2b/agency_pricing.py",
    "b2b/terms.py",
    "b2b/desk_bookings.py",
    "pricing/service.py",
    "pricing/engine.py",
    "pricing/policy_values.py",
    "catalog/trip_plan_prices.py",
    "catalog/cost_sheet.py",
    "catalog/price_workbench.py",
    "agency_billing/config.py",
    # AA-1096 (S279 L3, bk-056): the holds, desk, departures, ages, ops lifecycle and reminder readers. The wider
    # name-based scan of every governed package is test_aa1096_settings_lint.
    "agency_billing/reminders.py",
)
TECH = "technical bound (size, precision or arithmetic constant), not a business policy"
# Business values that predate the S278 rule; each is a follow-up of AA-1091 (implementation notes, "Should know").
GAP = "known gap: a business value still in code; follow-up makes it a policy parameter"
KNOWN: dict[tuple[str, str], str] = {
    ("b2b/quotes.py", "LINE_INCREMENTS"): GAP + " (VND line rounding)",
    ("b2b/quotes.py", "FX_PLACES"): TECH,
    ("b2b/tariff.py", "MAX_ROWS"): TECH,
    ("b2b/tariff.py", "MAX_CELLS"): TECH,
    ("b2b/tariff.py", "LOOKUPS_PER_COMPONENT"): TECH,
    ("b2b/cancellation.py", "MAX_TIERS"): TECH,
    ("b2b/cancellation.py", "MAX_DAYS"): TECH,
    ("b2b/cancellation.py", "HUNDRED"): TECH,
    ("b2b/cancellation.py", "PCT_PLACES"): TECH,
    ("b2b/price_adjustments.py", "MAX_AMOUNT"): TECH,
    ("b2b/price_adjustments.py", "MAX_REASON"): TECH,
    ("b2b/agency_pricing.py", "MAX_ROWS"): TECH,
    ("b2b/agency_pricing.py", "MAX_DISCOUNT"): TECH,
    ("b2b/agency_pricing.py", "ZERO"): TECH,
    ("b2b/terms.py", "HUNDRED"): TECH,
    ("b2b/terms.py", "PCT"): TECH,
    ("b2b/desk_bookings.py", "MAX_PAGE"): TECH,
    ("b2b/desk_bookings.py", "_STAFF_ERRORS"): TECH + " (HTTP status codes)",
    ("pricing/engine.py", "CENT"): TECH,
    ("pricing/engine.py", "RATIO"): TECH,
    ("pricing/engine.py", "ZERO"): TECH,
    ("pricing/engine.py", "ONE"): TECH,
    ("catalog/trip_plan_prices.py", "MAX_ROWS"): TECH,
    ("catalog/trip_plan_prices.py", "FROM_PAX"): GAP + " (the public from price is per person for 2 travellers)",
    ("catalog/trip_plan_prices.py", "MAX_AMOUNT"): TECH,
    ("catalog/trip_plan_prices.py", "MIN_MARGIN"): TECH,
    ("catalog/trip_plan_prices.py", "CHANGES_SHOWN"): TECH,
    ("catalog/cost_sheet.py", "MAX_ATTEMPTS"): TECH,
    ("catalog/cost_sheet.py", "PAX_BANDS"): GAP + " (the pax bands of a computed price matrix)",
    ("catalog/cost_sheet.py", "MAX_SEGMENTS"): TECH,
    ("catalog/cost_sheet.py", "FOREVER"): TECH,
    ("catalog/cost_sheet.py", "SEASON_MAX"): TECH,
    ("catalog/cost_sheet.py", "CENT"): TECH,
    ("catalog/price_workbench.py", "MAX_ACTION_PLANS"): TECH,
    ("catalog/price_workbench.py", "MAX_RECOMPUTE"): TECH,
    ("agency_billing/reminders.py", "MAX_ATTEMPTS"): TECH + " (job runner attempts)",
    ("agency_billing/reminders.py", "MAX_STEP_DAYS"): TECH + " (input bound of a step, billing_terms CHECK)",
    ("agency_billing/reminders.py", "MAX_INVOICES"): TECH + " (batch size of one run)",
}
KNOWN_INLINE: frozenset[tuple[str, str]] = frozenset(
    {
        ("b2b/price_adjustments.py", "len(currency) != 3"),  # an ISO currency code
        ("b2b/desk_bookings.py", "1 <= len(agency_ref) <= 64"),  # a reference's length
        ("agency_billing/config.py", "0 <= days <= 365"),  # the input range of balance days (the value is staff data)
    }
)
SMALL = {0, 1, 2, -1, 100}


def _numbers(node: ast.AST) -> list[ast.AST]:
    out: list[ast.AST] = []
    for n in ast.walk(node):
        if isinstance(n, ast.Constant) and type(n.value) in (int, float):
            out.append(n)
        elif isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", "")) in ("Decimal", "date"):
            out.append(n)
    return out


def findings(rel: str, source: str) -> list[str]:
    """What the guard refuses in one module's source (``rel`` = its path under app/)."""
    tree = ast.parse(source)
    found: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        else:
            targets = [node.target] if isinstance(node, ast.AnnAssign) else []
        value = getattr(node, "value", None)
        for target in targets:
            if isinstance(target, ast.Name) and target.id.isupper() and value is not None and _numbers(value):
                if (rel, target.id) not in KNOWN:
                    found.append(f"{rel}: constant {target.id} = {ast.unparse(value)[:60]}")
    for node in ast.walk(tree):
        if isinstance(node, (ast.Compare, ast.BinOp, ast.AugAssign)):
            text = ast.unparse(node)
            if (rel, text) in KNOWN_INLINE:
                continue
            for n in ast.walk(node):
                if isinstance(n, ast.Constant) and type(n.value) in (int, float) and n.value not in SMALL:
                    found.append(f"{rel}:{node.lineno}: literal {n.value!r} in `{text[:60]}`")
                    break
        if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) == "timedelta":
            for kw in node.keywords:
                if isinstance(kw.value, ast.Constant) and not (kw.arg == "days" and kw.value.value == 1):
                    found.append(f"{rel}:{node.lineno}: literal `{ast.unparse(node)}`")
            if any(isinstance(a, ast.Constant) for a in node.args):
                found.append(f"{rel}:{node.lineno}: literal `{ast.unparse(node)}`")
    return found


@pytest.mark.parametrize("rel", CONSUMERS)
def test_no_policy_literal_in_pricing_code(rel: str) -> None:
    assert findings(rel, (APP / rel).read_text(encoding="utf-8")) == []


def test_every_known_constant_still_exists_and_says_why() -> None:
    for (rel, name), why in KNOWN.items():
        assert why, (rel, name)
        assert f"\n{name} = " in (APP / rel).read_text(encoding="utf-8") or f"\n{name}: " in (APP / rel).read_text(
            encoding="utf-8"
        ), f"{rel}: {name} is gone; drop it from KNOWN"


def test_the_retired_quote_validity_setting_is_never_read() -> None:
    hits = [str(p.relative_to(APP)) for p in APP.rglob("*.py") if "b2b_quote_valid_hours" in p.read_text("utf-8")]
    assert hits == []


def test_parameters_in_use_are_read_by_their_module() -> None:
    for key, param in policy_params.PARAMS.items():
        if param.used_by == "stored":
            continue
        rel = param.used_by.removeprefix("app.").replace(".", "/") + ".py"
        assert key in (APP / rel).read_text(encoding="utf-8"), f"{key}: {rel} does not read it"


@pytest.mark.parametrize(
    "planted",
    [
        "QUOTE_VALID_DAYS = 14\n",
        "def f(pax):\n    return pax >= 9\n",
        "from datetime import timedelta\nx = timedelta(hours=48)\n",
        "from decimal import Decimal\nUPLIFT = Decimal('0.05')\n",
        "def f(price):\n    return price * 1.05\n",
    ],
)
def test_the_guard_catches_a_planted_policy_value(planted: str) -> None:
    assert findings("b2b/quotes.py", planted)
