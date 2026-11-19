"""The staff sign-in parameters of the pricing policy registry (AA-1111 slice S2, bk-061, root ADR 0006c).

Booking keeps every admin-editable policy value in ``booking.pricing_policy`` (Pricing > Policy, S279), so the
administrator MFA switch is a parameter too, in its own page section "security" ("Staff sign-in"), approved by the
administrator group. :mod:`app.pricing.policy_params` builds a :class:`~app.pricing.policy_params.Param` from each
spec here (plain data, so this module imports nothing from it). Read by :mod:`app.auth.admin_mfa`.
"""

from __future__ import annotations

from typing import Any

SPECS: tuple[dict[str, Any], ...] = (
    {
        "key": "staff_admin_mfa_required",
        "type": "boolean",
        "unit": None,
        "question": "S285",
        "group": "admin",
        "label": "Administrators need an authenticator code",
        "help": "On: a Booking administrator session signed in without an authenticator app code is refused.",
        "used_by": "app.auth.admin_mfa",
        "section": "security",
    },
)
