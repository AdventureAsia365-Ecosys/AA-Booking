"""The enabled currencies (AA-1191): which currencies the platform shows and accepts, as CIS says.

One CIS setting, ``cis.billing.enabled_currencies`` (CIS Platform › Settings › Enabled currencies; tech lead S295: USD
only for now), read here through the contract view ``catalog.v_billing_currencies_v1`` (CIS 498, contract
tenant.entitlements.v1 1.12, SELECT to role ``booking`` only), like the billing terms of ``catalog.v_tenant_billing_v1``
(no HTTP, no secret, no Booking copy of the value). USD is the base currency and always enabled.

Feature-detected (:func:`app.catalog.liveness.view_ready`): before CIS 498 is applied the view is missing and Booking
keeps USD only, the default of the setting. Readers:

- the SaaS invoice drafts (:mod:`app.agency_billing.drafts`): an agency whose CIS ``billing_currency`` is not enabled
  is drafted in USD from the USD list (:func:`effective`); its CIS value stays and comes back when CIS enables it;
- the billing terms write (:func:`app.agency_billing.config.put_terms`): a SaaS currency that is not enabled is refused
  with 422 ``currency_not_enabled`` (:func:`refuse_unless_enabled`);
- ``GET /booking/billing-terms`` serves ``enabled_currencies`` so the staff page hides what is not enabled.

Supplier cost currencies (rate sheets, Booking FX) are out of this rule (AA-1191 scope).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import asyncpg

from app.catalog import liveness
from app.errors import Invalid

# technical-constant: the base currency (every CIS plan has a USD price), never disabled; not a policy value
BASE = "USD"
CODE = "currency_not_enabled"
VIEW = liveness.View("catalog", "v_billing_currencies_v1", ("enabled_currencies", "base_currency"))


def normalise(values: Iterable[Any] | None) -> tuple[str, ...]:
    """USD first, then every other code of ``values`` in its order, upper-cased, without duplicates. Pure."""
    out = [BASE]
    for v in values or ():
        code = v.strip().upper() if isinstance(v, str) else ""
        if code and code not in out:
            out.append(code)
    return tuple(out)


async def enabled(conn: asyncpg.Connection) -> tuple[str, ...]:
    """The enabled currencies in force; USD only while the CIS view cannot be read (CIS 498 not applied)."""
    if not await liveness.view_ready(conn, VIEW):
        return (BASE,)
    row = await conn.fetchrow(f"SELECT enabled_currencies FROM {VIEW.qualified} LIMIT 1")  # noqa: S608
    return normalise(row["enabled_currencies"] if row else None)


def effective(code: str | None, allowed: Iterable[str]) -> str:
    """The currency an agency is billed in: ``code`` while enabled, else USD. Pure."""
    c = (code or "").strip().upper()
    return c if c and c in tuple(allowed) else BASE


def problem(code: str | None, allowed: Iterable[str], field: str = "currency") -> str | None:
    """Why a write in ``code`` is refused (None = accepted). Pure; the message is the API's."""
    allowed = tuple(allowed)
    if not code or code.strip().upper() in allowed:
        return None
    return (
        f"{field}: {code} is not an enabled currency (enabled: {', '.join(allowed)}). "
        "An administrator can enable it in CIS Platform › Settings › Enabled currencies."
    )


def refuse_unless_enabled(code: str | None, allowed: Iterable[str], field: str = "currency") -> None:
    why = problem(code, allowed, field)
    if why:
        raise Invalid(CODE, why)
