"""CI guard ``settings-lint`` (AA-1096, S279 thresholds program, root audit 2026-11-13-S279 section 5.4).

Tech lead rule S279: every threshold, limit, cap, floor, ceiling, quota, budget, retry limit or retention window of
Booking is an admin-editable value (Booking: a parameter of ``booking.pricing_policy``, Pricing > Policy, maker-checker,
versioned, audited), never a code literal, an env var or a config file value alone. This guard covers the governed
packages (:data:`GOVERNED`: B2B, operations, retention, pricing, agency billing) and fails on:

1. a module-level UPPER_CASE constant holding a number whose name reads like policy (``*_MAX``, ``MAX_*``, ``*_DAYS``,
   ``*_HOURS``, ``*_LIMIT``, ``*_PCT``, ``*_ATTEMPTS``, ...), unless its line carries ``# technical-constant: <why>``
   or it is in :data:`BASELINE` (the constants that predate the rule, each classified: a technical bound, or a known
   gap with its follow-up). A new one fails until a person decides: a policy parameter, or a technical constant;
2. a number read from the process environment (``int(os.environ[...])``, ``float(getenv(...))``, ``Decimal(...)``);
3. a :data:`BASELINE` entry whose constant is gone (the baseline only shrinks);
4. a pricing policy parameter that no code reads and that does not say why on the page (``used_by="stored"`` needs
   ``pending``, shown as the "not enforced yet" badge), or a reading module that does not name its key (the other
   direction is ``test_aa1091_policy_guard.test_parameters_in_use_are_read_by_their_module``);
5. a parameter with no seed in the unit-test policy (``tests/unit/fakes.POLICY_SEED``). The migration seed of every
   key is checked on PostgreSQL (``tests/integration/test_aa1091_pricing_policy.py``: every parameter has a value in
   force after the migrations), not here: under MIG-4 the code PR is pushed before its migration is on main.

The scanner is self-tested on planted values.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from app.pricing import policy_params
from tests.unit.fakes import POLICY_SEED

APP = Path(__file__).resolve().parents[2] / "app"
GOVERNED = ("b2b", "ops", "retention", "pricing", "agency_billing")
POLICY_NAME = re.compile(
    r"(^|_)(MAX|MIN|CAP|LIMIT|THRESHOLD|PCT|USD|ATTEMPTS|RETRIES|DAYS|HOURS|MINUTES|SECONDS|TTL|WINDOW|DEFAULT"
    r"|INCREMENTS|BANDS|QUOTA|BUDGET|FLOOR|CEILING|STEPS)(_|$)"
)
ESCAPE = "# technical-constant:"

T = "technical bound (size, page, batch, precision, input range or protocol), not a business policy"
JOB = "job runner mechanics (attempts, batch, pause) like every job kind; follow-up: job-kind settings (audit 3.2)"
GAP = "known gap: a business value still in code; follow-up of AA-1096 (implementation notes, Should know)"
BASELINE: dict[tuple[str, str], str] = {
    ("b2b/agency_pricing.py", "MAX_ROWS"): T,
    ("b2b/agency_pricing.py", "MAX_DISCOUNT"): T + " (the input bound of an agency discount, staff data)",
    ("b2b/allotments.py", "MAX_SEATS"): T,
    ("b2b/allotments.py", "MAX_RELEASE_DAYS"): T,
    ("b2b/cancellation.py", "MAX_TIERS"): T,
    ("b2b/cancellation.py", "MAX_DAYS"): T,
    ("b2b/cancellation.py", "PCT_PLACES"): T,
    ("b2b/components.py", "MAX_RATE_LOOKUPS"): T + " (query budget of one quote)",
    ("b2b/departure_board.py", "MAX_WINDOW"): T,
    ("b2b/departure_board.py", "MAX_PAGE"): T,
    ("b2b/departure_fees.py", "MAX_SEATS"): T,
    ("b2b/departure_fees.py", "MAX_PRICE"): T,
    ("b2b/departure_fees.py", "MAX_NOTE"): T,
    ("b2b/departure_fees.py", "MAX_PAGE"): T,
    ("b2b/departure_ops.py", "MAX_POINT"): T,
    ("b2b/departure_ops.py", "MAX_NOTE"): T,
    ("b2b/departure_watch.py", "MAX_ATTEMPTS"): JOB,
    ("b2b/departure_watch.py", "MAX_LISTED"): T,
    ("b2b/departures.py", "MAX_CAPACITY"): T,
    ("b2b/departures.py", "LIST_DAYS"): T,
    ("b2b/departures.py", "MAX_CUTOFF_DAYS"): T + " (input bound; the value is staff data per departure)",
    ("b2b/departures.py", "MAX_DECISION_DAYS"): T + " (input bound; the default is departure_decision_days_default)",
    ("b2b/departures.py", "MAX_GENERATE_DAYS"): T,
    ("b2b/departures.py", "MAX_GENERATE_DATES"): T,
    ("b2b/desk.py", "MAX_PAGE"): T,
    ("b2b/desk.py", "DEFAULT_WINDOW_BEFORE"): T + " (the desk list's default date filter, a view setting)",
    ("b2b/desk.py", "DEFAULT_WINDOW_AFTER"): T + " (the desk list's default date filter, a view setting)",
    ("b2b/desk.py", "MAX_WINDOW"): T,
    ("b2b/desk_bookings.py", "MAX_PAGE"): T,
    ("b2b/errors.py", "MAX_MESSAGE"): T,
    ("b2b/events.py", "DEFAULT_LIMIT"): T,
    ("b2b/events.py", "MAX_LIMIT"): T,
    ("b2b/expiry.py", "MAX_BATCHES"): JOB,
    ("b2b/expiry.py", "MAX_ATTEMPTS"): JOB,
    ("b2b/invoices.py", "DEFAULT_LIMIT"): T,
    ("b2b/invoices.py", "MAX_LIMIT"): T,
    ("b2b/invoices.py", "MAX_OFFSET"): T,
    ("b2b/plan_components.py", "MAX_COMPONENTS"): T,
    ("b2b/plan_span.py", "MAX_DAYS"): T,
    ("b2b/price_adjustments.py", "MAX_AMOUNT"): T,
    ("b2b/price_adjustments.py", "MAX_REASON"): T,
    ("b2b/quotes.py", "LINE_INCREMENTS"): GAP + " (VND quote line rounding, next to rounding_increment_by_currency)",
    ("b2b/tariff.py", "MAX_ROWS"): T,
    ("b2b/tariff.py", "MAX_CELLS"): T,
    ("b2b/tenant_pull.py", "MAX_ATTEMPTS"): JOB,
    ("b2b/tenant_pull.py", "SLOT_SECONDS"): JOB,
    ("b2b/tenant_pull.py", "NAME_MAX"): T,
    ("b2b/terms.py", "PCT"): T,
    ("ops/files.py", "MAX_BYTES"): T + " (tied to the staff BFF body cap; the retired ops_docs_max_bytes row)",
    ("ops/files.py", "MAX_BASE64_CHARS"): T,
    ("ops/files.py", "PRESIGN_SECONDS"): T,
    ("ops/files.py", "MAX_ACTIVE_PER_ENTITY"): T,
    ("ops/files.py", "MAX_NAME"): T,
    ("ops/lifecycle.py", "MAX_ATTEMPTS"): JOB,
    ("ops/lifecycle.py", "MAX_BATCHES"): JOB,
    ("ops/lifecycle.py", "MAX_CASE_IDS"): T,
    ("ops/lifecycle.py", "MAX_LISTED"): T,
    ("ops/money.py", "MAX_AMOUNT"): T,
    ("ops/money.py", "MAX_SCHEDULE_ROWS"): T,
    ("ops/supplier_bills.py", "MAX_PAGE"): T,
    ("ops/supplier_bills.py", "MAX_LINES"): T,
    ("ops/supplier_bills.py", "MAX_FUTURE_DAYS"): T + " (a bill dated in the future is a typo guard)",
    ("ops/supplier_bookings.py", "MAX_PAGE"): T,
    ("ops/trip_checks.py", "MAX_PAGE"): T,
    ("retention/jobs.py", "RUN_MAX_ROWS"): GAP + " (retention run safety cap; DM-7.2 approval, audit 3.2 low)",
    ("retention/jobs.py", "DEFAULT_PAUSE_MS"): JOB,
    ("retention/jobs.py", "MAX_ATTEMPTS"): JOB,
    ("retention/jobs.py", "OVERRIDE_MIN_AGE_DAYS"): T + " (the floor of a Dev-only test override, env-gated)",
    ("retention/jobs.py", "OVERRIDE_MAX_ROWS"): T + " (the cap of a Dev-only test override, env-gated)",
    ("retention/runner.py", "BATCH_SIZE_MAX"): JOB,
    ("pricing/policy.py", "MAX_ROWS"): T,
    ("pricing/policy_admin.py", "MAX_REASON"): T,
    ("pricing/policy_params.py", "MAX_LIST"): T,
    ("pricing/seasons_admin.py", "REASON_MAX"): T,
    ("pricing/seasons_admin.py", "NAME_MAX"): T,
    ("agency_billing/drafts.py", "MAX_ATTEMPTS"): JOB,
    ("agency_billing/export.py", "MAX_DAYS"): T,
    ("agency_billing/invoices.py", "MAX_LINES"): T,
    ("agency_billing/reminders.py", "MAX_ATTEMPTS"): JOB,
    ("agency_billing/reminders.py", "MAX_INVOICES"): JOB,
}
NUMBER_CALLS = ("int", "float", "Decimal")


def _has_number(node: ast.AST) -> bool:
    for n in ast.walk(node):
        if isinstance(n, ast.Constant) and type(n.value) in (int, float):
            return True
        if isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", "")) == "Decimal":
            return True
    return False


def _reads_env(node: ast.AST) -> bool:
    for n in ast.walk(node):
        if isinstance(n, ast.Attribute) and n.attr in ("environ", "getenv"):
            return True
        if isinstance(n, ast.Name) and n.id in ("environ", "getenv"):
            return True
    return False


def lint(rel: str, source: str) -> list[str]:
    """What settings-lint refuses in one module (``rel`` = its path under app/)."""
    tree = ast.parse(source)
    lines = source.splitlines()
    found: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        else:
            targets = [node.target] if isinstance(node, ast.AnnAssign) else []
        value = getattr(node, "value", None)
        for target in targets:
            if not (isinstance(target, ast.Name) and target.id.isupper() and value is not None):
                continue
            if not POLICY_NAME.search(target.id) or not _has_number(value):
                continue
            if (rel, target.id) in BASELINE or ESCAPE in lines[node.lineno - 1]:
                continue
            found.append(f"{rel}:{node.lineno}: constant {target.id} = {ast.unparse(value)[:60]}")
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) in NUMBER_CALLS:
            if any(_reads_env(a) for a in [*node.args, *(k.value for k in node.keywords)]):
                found.append(f"{rel}:{node.lineno}: a number read from the environment `{ast.unparse(node)[:60]}`")
    return found


def governed_modules() -> list[str]:
    return sorted(str(p.relative_to(APP)) for d in GOVERNED for p in (APP / d).rglob("*.py"))


@pytest.mark.parametrize("rel", governed_modules())
def test_no_new_policy_constant_or_env_number(rel: str) -> None:
    assert lint(rel, (APP / rel).read_text(encoding="utf-8")) == []


def test_every_baseline_entry_still_exists_and_says_why() -> None:
    for (rel, name), why in BASELINE.items():
        assert why, (rel, name)
        text = (APP / rel).read_text(encoding="utf-8")
        assert f"\n{name} = " in text or f"\n{name}: " in text, f"{rel}: {name} is gone; drop it from BASELINE"


def test_a_stored_parameter_says_why_it_is_not_enforced() -> None:
    for key, param in policy_params.PARAMS.items():
        assert param.section in policy_params.SECTIONS, key
        if param.used_by == "stored":
            assert param.pending.strip(), f"{key}: a stored parameter needs pending (the 'not enforced yet' badge)"
            assert policy_params.describe(param)["not_enforced_reason"] == param.pending
        else:
            assert not param.pending, f"{key}: read by {param.used_by}, so it has no pending text"
            assert policy_params.describe(param)["not_enforced_reason"] is None


def test_every_parameter_is_seeded_in_the_unit_policy() -> None:
    assert set(POLICY_SEED) == set(policy_params.PARAMS), "tests/unit/fakes.POLICY_SEED must seed every parameter"


@pytest.mark.parametrize(
    "planted",
    [
        "HOLD_HOURS = 48\n",
        "MAX_REMINDERS = 5\n",
        "from decimal import Decimal\nFLOOR_PCT = Decimal('0.2')\n",
        "import os\nx = int(os.environ.get('HOLD_HOURS', '48'))\n",
        "from os import getenv\nx = float(getenv('FEE'))\n",
    ],
)
def test_settings_lint_catches_a_planted_value(planted: str) -> None:
    assert lint("b2b/quotes.py", planted)


def test_settings_lint_allows_an_explained_technical_constant() -> None:
    assert lint("b2b/quotes.py", "MAX_PAGE = 100  # technical-constant: page size of a list endpoint\n") == []
    assert lint("b2b/quotes.py", "STATUSES = ('a', 'b')\nNAME = 'x'\n") == []
