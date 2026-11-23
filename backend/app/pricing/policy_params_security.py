"""The staff sign-in parameters of the pricing policy registry (AA-1111 slice S2, bk-061, root ADR 0006c).

Booking keeps every admin-editable policy value in ``booking.pricing_policy`` (Pricing > Policy, S279), so the
administrator MFA switch is a parameter too, in its own page section "security" ("Staff sign-in"), approved by the
administrator group. :mod:`app.pricing.policy_params` builds a :class:`~app.pricing.policy_params.Param` from each
spec here (plain data, so this module imports nothing from it). Read by :mod:`app.auth.admin_mfa`.

AA-1158 (bk-064, tech lead S285 Q4 / Q5): the two business-owner self-approval switches, in the same section and
approved by the administrator group. Pricing > Policy itself stays two-person, so the business owner never turns them
on or off alone. Read by :mod:`app.auth.self_approval`.
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
    {
        "key": "owner_self_approval_allowed",
        "type": "boolean",
        "unit": None,
        "question": "S285 Q5",
        "group": "admin",
        "label": "The business owner may approve her own work",
        "help": "On: the business owner approves a supplier, contract, price, season, bill, agency payment, price "
        "adjustment or billing entity change she made herself; each one is flagged and listed in the audit log.",
        "used_by": "app.auth.self_approval",
        "section": "security",
    },
    {
        "key": "owner_payout_after_approval_allowed",
        "type": "boolean",
        "unit": None,
        "question": "S285 Q4",
        "group": "admin",
        "label": "The business owner may pay a bill she approved",
        "help": "On: the business owner records the payout of a supplier bill she approved; the payout is flagged. "
        "Nobody else ever pays a bill they approved.",
        "used_by": "app.auth.self_approval",
        "section": "security",
    },
)
