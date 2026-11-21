"""The job check parameters of the pricing policy registry (AA-1171, S287 alerts catalogue P0, tech lead decision D3;
bk-063).

Booking keeps every admin-editable policy value in ``booking.pricing_policy`` (Pricing > Policy, S279), so the two
thresholds of the in-app job checks (S263 alert diet) are parameters too, in their own page section "ops" ("Job
checks"), approved by the administrator group. :mod:`app.pricing.policy_params` builds a
:class:`~app.pricing.policy_params.Param` from each spec here (plain data, so this module imports nothing from it).
Read by :mod:`app.jobs.metrics` (the sampler, every 5 minutes) and listed with their values in force by the check
registry the CIS alerts catalogue reads (``GET /booking/ops/checks``, :mod:`app.routers.ops_checks`).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

STUCK_AFTER_S = "ops_jobs_stuck_after_s"
OVERDUE_FACTOR = "ops_jobs_overdue_factor"

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
)
