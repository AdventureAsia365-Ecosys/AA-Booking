"""Supplier profile fields (AA-1158 part 2, bk-067): legal entity, tax code, contacts by role, structured payment terms.

Kept next to :mod:`app.suppliers.service` (file size limit), which calls :func:`normalize` on every create / update and
:func:`decode` on every row it reads. The database keeps the shapes too (``suppliers_profile_chk``): ``role_contacts``
is a JSON array, ``payment_terms`` a JSON object or NULL. They are supplier data: an edit while the supplier is not
live makes the editor one of its makers (bk-064 ``suppliers_writers``), so the activator never decided them.

- ``legal_entity``: the registered company name AA contracts with and pays (1-300 characters).
- ``tax_code``: the supplier's tax identification number (1-64).
- ``role_contacts``: at most :data:`MAX_CONTACTS` of ``{role, name, email, phone}``, ``role`` one of
  :data:`CONTACT_ROLES`, a name or an e-mail or a phone required. ``pii:contact``.
- ``payment_terms``: ``{basis, deposit_percent, due_days, method, note}``; ``basis`` one of :data:`TERMS_BASES`;
  ``deposit_percent`` 1-100 only with ``deposit_balance``; ``due_days`` 0-365 (days before the service for
  ``prepayment`` / ``deposit_balance``, after it for ``after_service``, after the statement for
  ``monthly_statement``); ``method`` one of :data:`TERMS_METHODS`; ``note`` up to 300 characters. These describe one
  supplier's agreement (data), not a rule of the system.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from app.errors import Invalid

FIELDS = ("legal_entity", "tax_code", "role_contacts", "payment_terms")
JSON_FIELDS = ("role_contacts", "payment_terms")
CONTACT_ROLES = ("reservations", "accounts", "sales", "operations", "emergency", "management")
TERMS_BASES = ("prepayment", "deposit_balance", "on_arrival", "after_service", "monthly_statement")
TERMS_METHODS = ("bank_transfer", "card", "cash", "other")
MAX_CONTACTS = 12  # technical-constant: the bk-067 CHECK bound (suppliers_profile_chk)
MAX_DUE_DAYS = 365  # technical-constant: a shape bound of the stored terms, not a policy
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _text(value: Any, limit: int, code: str) -> str | None:
    text = str(value).strip() if value is not None else ""
    if not text:
        return None
    if len(text) > limit:
        raise Invalid(code, f"at most {limit} characters")
    return text


def _contact(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("role") not in CONTACT_ROLES:
        raise Invalid("invalid_role_contact", f"each contact has a role among {', '.join(CONTACT_ROLES)}")
    out = {
        "role": raw["role"],
        "name": _text(raw.get("name"), 200, "invalid_role_contact"),
        "email": (_text(raw.get("email"), 254, "invalid_role_contact") or "").lower() or None,
        "phone": _text(raw.get("phone"), 40, "invalid_role_contact"),
    }
    if out["email"] and not EMAIL_RE.fullmatch(out["email"]):
        raise Invalid("invalid_role_contact", "a contact e-mail is not valid")
    if not (out["name"] or out["email"] or out["phone"]):
        raise Invalid("invalid_role_contact", "a contact needs a name, an e-mail or a phone")
    return out


def contacts(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_CONTACTS:
        raise Invalid("invalid_role_contacts", f"contacts are a list of at most {MAX_CONTACTS}")
    return [_contact(c) for c in value]


def _percent(value: Any) -> str | None:
    if value is None or value == "":
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise Invalid("invalid_payment_terms", "the deposit is a percentage") from None
    if not Decimal("1") <= number <= Decimal("100"):
        raise Invalid("invalid_payment_terms", "the deposit is 1 to 100 percent")
    return format(number.normalize(), "f")


def terms(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or value.get("basis") not in TERMS_BASES:
        raise Invalid("invalid_payment_terms", f"payment terms have a basis among {', '.join(TERMS_BASES)}")
    deposit = _percent(value.get("deposit_percent"))
    if (deposit is not None) != (value["basis"] == "deposit_balance"):
        raise Invalid("invalid_payment_terms", "a deposit percentage goes with the deposit + balance basis only")
    due = value.get("due_days")
    if due is not None and (not isinstance(due, int) or isinstance(due, bool) or not 0 <= due <= MAX_DUE_DAYS):
        raise Invalid("invalid_payment_terms", f"due days are a whole number from 0 to {MAX_DUE_DAYS}")
    method = value.get("method")
    if method is not None and method not in TERMS_METHODS:
        raise Invalid("invalid_payment_terms", f"the method is one of {', '.join(TERMS_METHODS)}")
    return {
        "basis": value["basis"],
        "deposit_percent": deposit,
        "due_days": due,
        "method": method,
        "note": _text(value.get("note"), 300, "invalid_payment_terms"),
    }


def normalize(out: dict[str, Any]) -> dict[str, Any]:
    """The profile fields of a create / update body, checked and trimmed (empty text -> NULL)."""
    if "legal_entity" in out:
        out["legal_entity"] = _text(out["legal_entity"], 300, "legal_entity_too_long")
    if "tax_code" in out:
        out["tax_code"] = _text(out["tax_code"], 64, "tax_code_too_long")
    if "role_contacts" in out:
        out["role_contacts"] = contacts(out["role_contacts"])
    if "payment_terms" in out:
        out["payment_terms"] = terms(out["payment_terms"])
    return out


def decode(row: dict[str, Any]) -> dict[str, Any]:
    """A row read from the database: the jsonb columns as Python values (asyncpg returns text)."""
    for key in JSON_FIELDS:
        if isinstance(row.get(key), str):
            row[key] = json.loads(row[key])
    return row


def cast(column: str) -> str:
    """The SQL cast of a written column's placeholder."""
    if column == "destination_id":
        return "::uuid"
    return "::jsonb" if column in JSON_FIELDS else ""


def db_value(column: str, value: Any) -> Any:
    """A written value as asyncpg takes it (jsonb as JSON text; NULL payment terms stay NULL)."""
    if column in JSON_FIELDS and value is not None:
        return json.dumps(value)
    return value
