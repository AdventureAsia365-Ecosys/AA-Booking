"""The job check parameters of the pricing policy registry (AA-1171, S287 alerts catalogue P0, tech lead decision D3;
bk-063).

Booking keeps every admin-editable policy value in ``booking.pricing_policy`` (Pricing > Policy, S279), so the two
thresholds of the in-app job checks (S263 alert diet) are parameters too, in their own page section "ops" ("Job
checks"), approved by the administrator group. :mod:`app.pricing.policy_params` builds a
:class:`~app.pricing.policy_params.Param` from each spec here (plain data, so this module imports nothing from it).
Read by :mod:`app.jobs.metrics` (the sampler, every 5 minutes) and listed with their values in force by the check
registry the CIS alerts catalogue reads (``GET /booking/ops/checks``, :mod:`app.routers.ops_checks`).

AA-1151 (bk-068): how many days ahead the place add-on read model ``booking.v_place_addons`` lists (the view reads
the value in force itself) and how many days before its end a published supplier contract is announced as expiring
(:mod:`app.supply.contract_expiry`); section "operations", approved by the pricing group (product / contracting).

AA-1177 (bk-074): which built-in staff roles the same job e-mails about an expiring contract (Jira PR-31), section
"operations", approved by the pricing group like the window; an empty list switches the e-mail off.

AA-1149 (bk-065): the default and maximum length of a can-operate sale exception, in the existing section
"operations", approved by the administrator group; read by :mod:`app.catalog.sell_exceptions`.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.pricing import policy_cross_checks as cross

STUCK_AFTER_S = "ops_jobs_stuck_after_s"
OVERDUE_FACTOR = "ops_jobs_overdue_factor"
OPERABLE_DEFAULT_DAYS = "operable_exception_default_days"
OPERABLE_MAX_DAYS = "operable_exception_max_days"
ADDON_DAYS_AHEAD = "addon_days_ahead"
CONTRACT_EXPIRY_ALERT_DAYS = "contract_expiry_alert_days"
CONTRACT_EXPIRY_MAIL_ROLES = "contract_expiry_mail_roles"
# The built-in Booking staff roles (= app.auth.staff.STAFF_ROLES, by a test; plain data, so not imported from there).
STAFF_ROLE_CHOICES = ("admin", "contracting", "content_creator", "finance", "sales")

SPECS: tuple[dict[str, Any], ...] = (
    {
        "key": STUCK_AFTER_S,
        "type": "integer",
        "unit": "seconds",
        "question": "S287",
        "group": "admin",
        "label": "Job queue stuck after (seconds)",
        "help": "The job check alerts when a due job has waited this long while no healthy job runs.",
        "used_by": "app.jobs.metrics",
        "section": "ops",
        "minimum": Decimal("120"),
        "maximum": Decimal("7200"),
    },
    {
        "key": OVERDUE_FACTOR,
        "type": "decimal",
        "unit": "x expected",
        "question": "S287",
        "group": "admin",
        "label": "Running job overdue after (x its expected time)",
        "help": "A running job counts as overdue once it has run this many times its kind's expected time.",
        "used_by": "app.jobs.metrics",
        "section": "ops",
        "minimum": Decimal("1.2"),
        "maximum": Decimal("10"),
    },
    {
        "key": OPERABLE_DEFAULT_DAYS,
        "type": "integer",
        "unit": "days",
        "question": "S285",
        "group": "admin",
        "label": "Sale exception: default length (days)",
        "help": "How long a sale exception for a tour that cannot be operated yet lasts unless the administrator "
        "picks another length.",
        "used_by": "app.catalog.sell_exceptions",
        "section": "operations",
        "minimum": Decimal("1"),
        "maximum": Decimal("90"),
        "cross_check": cross.operable_default_within_max,
    },
    {
        "key": OPERABLE_MAX_DAYS,
        "type": "integer",
        "unit": "days",
        "question": "S285",
        "group": "admin",
        "label": "Sale exception: longest length (days)",
        "help": "The longest a sale exception for a tour that cannot be operated yet may last.",
        "used_by": "app.catalog.sell_exceptions",
        "section": "operations",
        "minimum": Decimal("1"),
        "maximum": Decimal("90"),
        "cross_check": cross.operable_max_covers_default,
    },
    {
        "key": ADDON_DAYS_AHEAD,
        "type": "integer",
        "unit": "days",
        "question": "S285",
        "group": "pricing",
        "label": "Add-ons: days listed ahead",
        "help": "How many days ahead the optional activities of a place are listed for travellers and agencies.",
        "used_by": "app.supply.addons",
        "section": "operations",
        "minimum": Decimal("30"),
        "maximum": Decimal("730"),
    },
    {
        "key": CONTRACT_EXPIRY_ALERT_DAYS,
        "type": "integer",
        "unit": "days",
        "question": "S285",
        "group": "pricing",
        "label": "Contract expiring warning (days)",
        "help": "A published supplier contract is announced as expiring this many days before it ends.",
        "used_by": "app.supply.contract_expiry",
        "section": "operations",
        "minimum": Decimal("7"),
        "maximum": Decimal("365"),
    },
    {
        "key": CONTRACT_EXPIRY_MAIL_ROLES,
        "type": "codes",
        "unit": None,
        "question": "PR-31",
        "group": "pricing",
        "label": "Contract expiring e-mail: staff roles",
        "help": "The active Booking staff of these roles get one e-mail per expiring contract; empty = no e-mail.",
        "used_by": "app.supply.contract_expiry_mail",
        "section": "operations",
        "choices": STAFF_ROLE_CHOICES,
    },
)
