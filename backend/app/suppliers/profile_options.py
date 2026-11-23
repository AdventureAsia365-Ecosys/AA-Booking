"""The choices of the supplier profile (AA-1158 part 2, bk-067 ``booking.supplier_profile_option``; review S289).

Contact roles (``suppliers.role_contacts[].role``), payment terms bases and methods (``suppliers.payment_terms.basis`` /
``method``) are business data an administrator edits, not code lists: one table, three kinds, seeded with the values
of the first release. The supplier form loads them from ``GET /booking/supplier-profile-options``.

- **Read** (``supply.read``): every kind, active and retired, in page order (a retired code still has a label for the
  suppliers that hold it).
- **Add** / **edit** (``staff.admin``, like the role editor): a new code (lower case, ``a-z0-9_``, never changed once
  created), its English and Vietnamese labels, its order; an edit changes the labels, the order or ``active``. A row is
  never deleted (role ``booking`` has no DELETE grant): retiring a choice sets ``active = false``. Every write is one
  audit row (``supplier_profile_option.created`` / ``.updated``).
- **Check** (:func:`check_profile`): a create / update of a supplier names only active codes, or the code that supplier
  already holds (retiring a choice never blocks an unrelated edit). :mod:`app.suppliers.profile` keeps the shapes and
  the one rule tied to a code: a deposit percentage goes with ``deposit_balance`` only.
"""

from __future__ import annotations

import re
from typing import Any

import asyncpg

from app import audit
from app.auth.staff import Staff
from app.errors import Conflict, Forbidden, Invalid, NotFound

KINDS = ("contact_role", "terms_basis", "terms_method")
CODE_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
LABEL_MAX = 80  # technical-constant: the bk-067 CHECK bound (supplier_profile_option_text_chk)
EDITABLE = ("label_en", "label_vi", "position", "active")
COLUMNS = "id::text AS id, kind, code, label_en, label_vi, position, active, updated_at, updated_by"
OPTION_TABLE = "booking.supplier_profile_option (kind, code, label_en, label_vi, position, created_by, updated_by)"
INSERT_SQL = f"INSERT INTO {OPTION_TABLE} VALUES ($1, $2, $3, $4, $5, $6, $6) RETURNING {COLUMNS}"  # noqa: S608
ROW_SQL = f"SELECT {COLUMNS} FROM booking.supplier_profile_option WHERE kind = $1 AND code = $2"  # noqa: S608


async def list_all(conn: asyncpg.Connection) -> dict[str, list[dict[str, Any]]]:
    """Every choice by kind, in page order (``position``, then code), retired ones included (``active`` false)."""
    rows = await conn.fetch(
        f"SELECT {COLUMNS} FROM booking.supplier_profile_option ORDER BY kind, position, code"  # noqa: S608
    )
    out: dict[str, list[dict[str, Any]]] = {kind: [] for kind in KINDS}
    for row in rows:
        out.setdefault(row["kind"], []).append(dict(row))
    return out


async def active_codes(conn: asyncpg.Connection) -> dict[str, set[str]]:
    rows = await conn.fetch("SELECT kind, code FROM booking.supplier_profile_option WHERE active")
    out: dict[str, set[str]] = {kind: set() for kind in KINDS}
    for row in rows:
        out.setdefault(row["kind"], set()).add(row["code"])
    return out


def _held(current: dict[str, Any] | None) -> dict[str, set[str]]:
    """The codes the supplier holds today (valid on an edit even when retired since)."""
    held: dict[str, set[str]] = {kind: set() for kind in KINDS}
    if not current:
        return held
    held["contact_role"] = {c.get("role") for c in current.get("role_contacts") or [] if isinstance(c, dict)}
    terms = current.get("payment_terms") or {}
    if isinstance(terms, dict):
        held["terms_basis"] = {terms.get("basis")} - {None}
        held["terms_method"] = {terms.get("method")} - {None}
    return held


async def check_profile(
    conn: asyncpg.Connection, values: dict[str, Any], current: dict[str, Any] | None = None
) -> None:
    """422 when ``values`` (normalized by :func:`app.suppliers.profile.normalize`) names a code that is neither active
    nor already held by the supplier (``current``, None on a create)."""
    wanted: dict[str, set[str]] = {kind: set() for kind in KINDS}
    if values.get("role_contacts"):
        wanted["contact_role"] = {c["role"] for c in values["role_contacts"]}
    if values.get("payment_terms"):
        wanted["terms_basis"] = {values["payment_terms"]["basis"]}
        wanted["terms_method"] = {values["payment_terms"]["method"]} - {None}
    if not any(wanted.values()):
        return
    allowed, held = await active_codes(conn), _held(current)
    errors = {"contact_role": "invalid_role_contact", "terms_basis": "invalid_payment_terms"}
    for kind in KINDS:
        unknown = sorted(wanted[kind] - allowed.get(kind, set()) - held[kind])
        if unknown:
            raise Invalid(
                errors.get(kind, "invalid_payment_terms"),
                f"not an active {kind.replace('_', ' ')}: {', '.join(unknown)} (Suppliers > Profile choices)",
            )


def _label(body: dict[str, Any], key: str, *, required: bool) -> str | None:
    value = body.get(key)
    text = " ".join(str(value).split()) if value is not None else ""
    if not text:
        if required:
            raise Invalid(f"{key}_required", f"{key.replace('_', ' ')} is required")
        return None
    if len(text) > LABEL_MAX:
        raise Invalid(f"{key}_too_long", f"{key.replace('_', ' ')} is at most {LABEL_MAX} characters")
    return text


def _position(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 999:  # noqa: PLR2004 — DB CHECK
        raise Invalid("invalid_position", "the order is a whole number from 1 to 999")
    return value


def _require_admin(staff: Staff) -> None:
    if not staff.can("staff.admin"):
        raise Forbidden("forbidden", "editing the supplier profile choices needs staff.admin")


async def _audit(conn: asyncpg.Connection, staff: Staff, row: Any, action: str, before: Any, after: Any) -> None:
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action=f"supplier_profile_option.{action}",
        entity_type="supplier_profile_option",
        entity_id=row["id"],
        before=before,
        after=after,
        entity_label=f"{row['kind']} {row['code']}"[:80],
        summary=f"{action.capitalize()} supplier profile choice {row['kind']} {row['code']}",
    )


async def create(conn: asyncpg.Connection, staff: Staff, body: dict[str, Any]) -> dict[str, Any]:
    """A new choice, active, at the end of its kind unless ``position`` is given."""
    _require_admin(staff)
    kind, code = body.get("kind"), str(body.get("code") or "").strip()
    if kind not in KINDS:
        raise Invalid("unknown_kind", f"kind must be one of {', '.join(KINDS)}")
    if not CODE_RE.fullmatch(code):
        raise Invalid("invalid_code", "a code is 2-40 characters: a lower-case letter, then a-z, 0-9 or _")
    label_en, label_vi = _label(body, "label_en", required=True), _label(body, "label_vi", required=True)
    position = _position(body.get("position"))
    if position is None:
        position = min(999, await conn.fetchval(
            "SELECT coalesce(max(position), 0) + 10 AS next FROM booking.supplier_profile_option WHERE kind = $1",
            kind,
        ))  # fmt: skip
    try:
        row = await conn.fetchrow(
            INSERT_SQL,
            kind,
            code,
            label_en,
            label_vi,
            position,
            staff.cis_user_id,
        )
    except asyncpg.UniqueViolationError as exc:
        raise Conflict("option_exists", f"{kind} {code} exists already (retired ones are turned on again)") from exc
    out = dict(row)
    await _audit(conn, staff, out, "created", None, {k: out[k] for k in ("kind", "code", *EDITABLE)})
    return out


async def update(conn: asyncpg.Connection, staff: Staff, kind: str, code: str, body: dict[str, Any]) -> dict[str, Any]:
    """Change the labels, the order or ``active`` of one choice (the code never changes)."""
    _require_admin(staff)
    current = await conn.fetchrow(ROW_SQL + " FOR UPDATE", kind, code)
    if current is None:
        raise NotFound("option_not_found", f"no supplier profile choice {kind} {code}")
    changes: dict[str, Any] = {}
    for key in ("label_en", "label_vi"):
        if key in body:
            changes[key] = _label(body, key, required=True)
    if "position" in body:
        changes["position"] = _position(body["position"])
    if "active" in body:
        if not isinstance(body["active"], bool):
            raise Invalid("invalid_active", "active is true or false")
        changes["active"] = body["active"]
    changed = {k: v for k, v in changes.items() if v is not None and current[k] != v}
    if not changed:
        return dict(current)
    sets = ", ".join(f"{column} = ${i + 3}" for i, column in enumerate(changed))
    row = await conn.fetchrow(
        f"UPDATE booking.supplier_profile_option SET {sets}, updated_at = now(), updated_by = ${len(changed) + 3}"  # noqa: S608
        f" WHERE kind = $1 AND code = $2 RETURNING {COLUMNS}",
        kind,
        code,
        *changed.values(),
        staff.cis_user_id,
    )
    out = dict(row)
    await _audit(conn, staff, out, "updated", {k: current[k] for k in changed}, changed)
    return out
