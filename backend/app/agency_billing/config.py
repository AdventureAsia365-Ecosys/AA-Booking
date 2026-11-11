"""Agency billing configuration (bk-041): the issuing billing entity, the tax rules and the payment terms. All three are
data rows that finance (or an admin) edits in the staff admin (``billing_config.write``), never constants in code
(root ADR 0019 b16 / b17; BO-15..17 "adjustable later"). Every change writes one ``booking.audit_logs`` row.

- **Billing entity**: AA's legal entity on the invoice (b10). At most one default (a partial unique index); making a
  row the default clears the old one in the same transaction. Rows are deactivated, never deleted.
- **Tax rule**: rate + wording per invoice kind and recipient country with an effective window; the draft jobs pick
  the active rule in force on a day, the most specific first (:func:`pick_tax_rule`).
- **Terms**: one default row (``party_tenant_id`` NULL) and optional per-agency overrides, each a whole set
  (:func:`terms_for` returns the override, else the default).
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import asyncpg

from app import audit
from app.errors import Conflict, Invalid, NotFound

ENTITY_COLUMNS = (
    "id::text AS id, code, legal_entity, tax_code, registered_office, country_iso2, invoice_inbox,"
    " payment_instructions,"
    " is_default, active, created_at, updated_at, updated_by"
)
TAX_COLUMNS = (
    "id::text AS id, code, invoice_kind, recipient_country, rate, line_wording, footer_wording, effective_from,"
    " effective_to, active, created_at, updated_at, updated_by"
)
TERMS_COLUMNS = (
    "id::text AS id, party_tenant_id, deposit_pct, balance_days_before_departure, saas_due_days, settlement_due_days,"
    " warn_after_days, suspend_after_days, saas_currency, updated_at, updated_by"
)
TERMS_FIELDS = (
    "deposit_pct",
    "balance_days_before_departure",
    "saas_due_days",
    "settlement_due_days",
    "warn_after_days",
    "suspend_after_days",
    "saas_currency",
)
ENTITY_FIELDS = (
    "legal_entity",
    "tax_code",
    "registered_office",
    "country_iso2",
    "invoice_inbox",
    "payment_instructions",
    "is_default",
    "active",
)
TAX_FIELDS = (
    "invoice_kind",
    "recipient_country",
    "rate",
    "line_wording",
    "footer_wording",
    "effective_from",
    "effective_to",
    "active",
)


def _plain(row: Any) -> dict[str, Any]:
    out = dict(row)
    for k, v in out.items():
        if isinstance(v, uuid.UUID):
            out[k] = str(v)
    return out


async def _audit(conn: asyncpg.Connection, actor: str, action: str, entity: str, entity_id: str | None, label: str,
                 before: Any, after: Any) -> None:  # fmt: skip
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=actor,
        action=action,
        entity_type=entity,
        entity_id=entity_id,
        before=before,
        after=after,
        entity_label=label[:80],
        summary=action.replace("_", " ").replace(".", " "),
    )


def _jsonable(d: dict[str, Any] | None) -> dict[str, Any] | None:
    if d is None:
        return None
    skip = ("created_at", "updated_at")
    return {k: (str(v) if isinstance(v, (Decimal, date)) else v) for k, v in d.items() if k not in skip}


# ── Billing entities ─────────────────────────────────────────────────────────


async def list_entities(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    rows = await conn.fetch(f"SELECT {ENTITY_COLUMNS} FROM booking.billing_entity ORDER BY is_default DESC, code")  # noqa: S608
    return [_plain(r) for r in rows]


async def default_entity(conn: asyncpg.Connection) -> dict[str, Any] | None:
    row = await conn.fetchrow(
        f"SELECT {ENTITY_COLUMNS} FROM booking.billing_entity WHERE is_default AND active"  # noqa: S608
    )
    return _plain(row) if row else None


async def create_entity(conn: asyncpg.Connection, body: dict[str, Any], actor: str) -> dict[str, Any]:
    async with conn.transaction():
        if await conn.fetchval("SELECT 1 FROM booking.billing_entity WHERE code = $1", body["code"]):
            raise Conflict("code_taken", f"a billing entity with code {body['code']} exists")
        if body.get("is_default"):
            await conn.execute("UPDATE booking.billing_entity SET is_default = false WHERE is_default")
        row = await conn.fetchrow(
            "INSERT INTO booking.billing_entity (code, legal_entity, tax_code, registered_office, country_iso2,"  # noqa: S608
            " invoice_inbox, payment_instructions, is_default, active, updated_by)"
            f" VALUES ($1, $2, $3, $4, $5, $6, $7, $8, true, $9) RETURNING {ENTITY_COLUMNS}",
            body["code"],
            body["legal_entity"],
            body.get("tax_code"),
            body.get("registered_office"),
            body.get("country_iso2"),
            body.get("invoice_inbox"),
            body.get("payment_instructions"),
            bool(body.get("is_default")),
            actor,
        )
        out = _plain(row)
        await _audit(conn, actor, "billing_entity.created", "billing_entity", out["id"], out["code"], None,
                     _jsonable(out))  # fmt: skip
    return out


async def update_entity(
    conn: asyncpg.Connection, entity_id: str, changes: dict[str, Any], actor: str
) -> dict[str, Any]:
    async with conn.transaction():
        before = await conn.fetchrow(
            f"SELECT {ENTITY_COLUMNS} FROM booking.billing_entity WHERE id = $1::uuid FOR UPDATE",  # noqa: S608
            entity_id,
        )
        if before is None:
            raise NotFound("billing_entity_not_found", "no billing entity with this id")
        merged = {**dict(before), **{k: v for k, v in changes.items() if k in ENTITY_FIELDS}}
        if merged["is_default"] and not merged["active"]:
            raise Invalid("default_must_be_active", "the default billing entity must be active")
        if merged["is_default"] and not before["is_default"]:
            await conn.execute("UPDATE booking.billing_entity SET is_default = false WHERE is_default")
        row = await conn.fetchrow(
            "UPDATE booking.billing_entity SET legal_entity = $2, tax_code = $3, registered_office = $4,"  # noqa: S608
            " country_iso2 = $5, invoice_inbox = $6, payment_instructions = $7, is_default = $8, active = $9,"
            f" updated_by = $10 WHERE id = $1::uuid RETURNING {ENTITY_COLUMNS}",
            entity_id,
            *(merged[k] for k in ENTITY_FIELDS),
            actor,
        )
        out = _plain(row)
        await _audit(conn, actor, "billing_entity.updated", "billing_entity", entity_id, out["code"],
                     _jsonable(_plain(before)), _jsonable(out))  # fmt: skip
    return out


# ── Tax rules ────────────────────────────────────────────────────────────────


async def list_tax_rules(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    rows = await conn.fetch(f"SELECT {TAX_COLUMNS} FROM booking.tax_rule ORDER BY active DESC, code")  # noqa: S608
    return [_plain(r) for r in rows]


def _check_window(start: date, end: date | None) -> None:
    if end is not None and end <= start:
        raise Invalid("invalid_window", "effective_to must be after effective_from")


async def create_tax_rule(conn: asyncpg.Connection, body: dict[str, Any], actor: str) -> dict[str, Any]:
    _check_window(body["effective_from"], body.get("effective_to"))
    async with conn.transaction():
        if await conn.fetchval("SELECT 1 FROM booking.tax_rule WHERE code = $1", body["code"]):
            raise Conflict("code_taken", f"a tax rule with code {body['code']} exists")
        row = await conn.fetchrow(
            "INSERT INTO booking.tax_rule (code, invoice_kind, recipient_country, rate, line_wording, footer_wording,"  # noqa: S608
            f" effective_from, effective_to, active, updated_by) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, true, $9)"
            f" RETURNING {TAX_COLUMNS}",
            body["code"],
            body.get("invoice_kind"),
            body.get("recipient_country"),
            body["rate"],
            body["line_wording"],
            body.get("footer_wording"),
            body["effective_from"],
            body.get("effective_to"),
            actor,
        )
        out = _plain(row)
        await _audit(conn, actor, "tax_rule.created", "tax_rule", out["id"], out["code"], None, _jsonable(out))
    return out


async def update_tax_rule(
    conn: asyncpg.Connection, rule_id: str, changes: dict[str, Any], actor: str
) -> dict[str, Any]:
    async with conn.transaction():
        before = await conn.fetchrow(
            f"SELECT {TAX_COLUMNS} FROM booking.tax_rule WHERE id = $1::uuid FOR UPDATE",  # noqa: S608
            rule_id,
        )
        if before is None:
            raise NotFound("tax_rule_not_found", "no tax rule with this id")
        merged = {**dict(before), **{k: v for k, v in changes.items() if k in TAX_FIELDS}}
        _check_window(merged["effective_from"], merged["effective_to"])
        row = await conn.fetchrow(
            "UPDATE booking.tax_rule SET invoice_kind = $2, recipient_country = $3, rate = $4, line_wording = $5,"  # noqa: S608
            " footer_wording = $6, effective_from = $7, effective_to = $8, active = $9, updated_by = $10"
            f" WHERE id = $1::uuid RETURNING {TAX_COLUMNS}",
            rule_id,
            *(merged[k] for k in TAX_FIELDS),
            actor,
        )
        out = _plain(row)
        await _audit(conn, actor, "tax_rule.updated", "tax_rule", rule_id, out["code"], _jsonable(_plain(before)),
                     _jsonable(out))  # fmt: skip
    return out


PICK_TAX_SQL = f"""
SELECT {TAX_COLUMNS} FROM booking.tax_rule
 WHERE active AND effective_from <= $3 AND (effective_to IS NULL OR effective_to > $3)
   AND (invoice_kind IS NULL OR invoice_kind = $1)
   AND (recipient_country IS NULL OR recipient_country = $2)
 ORDER BY (recipient_country IS NOT NULL) DESC, (invoice_kind IS NOT NULL) DESC, effective_from DESC, code
 LIMIT 1
"""  # noqa: S608


async def pick_tax_rule(conn: asyncpg.Connection, kind: str, country: str | None, day: date) -> dict[str, Any] | None:
    """The active rule in force on ``day`` for ``kind`` and the recipient ``country``: kind + country first, then
    country, then kind, then a rule for any; None when no rule applies (the invoice then carries no tax line)."""
    row = await conn.fetchrow(PICK_TAX_SQL, kind, country, day)
    return _plain(row) if row else None


# ── Terms ────────────────────────────────────────────────────────────────────


async def list_terms(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        "SELECT t.id::text AS id, t.party_tenant_id, t.deposit_pct, t.balance_days_before_departure, t.saas_due_days,"
        " t.settlement_due_days, t.warn_after_days, t.suspend_after_days, t.saas_currency, t.updated_at, t.updated_by,"
        " p.agency_name FROM booking.billing_terms t"
        " LEFT JOIN booking.b2b_partners p ON p.tenant_id = t.party_tenant_id"
        " ORDER BY t.party_tenant_id IS NOT NULL, p.agency_name"
    )
    return [_plain(r) for r in rows]


async def terms_for(conn: asyncpg.Connection, tenant_id: Any) -> dict[str, Any] | None:
    """The agency's override, else the default row; None when neither exists (the jobs then refuse to draft)."""
    row = await conn.fetchrow(
        f"SELECT {TERMS_COLUMNS} FROM booking.billing_terms"  # noqa: S608
        " WHERE party_tenant_id = $1::uuid OR party_tenant_id IS NULL"
        " ORDER BY party_tenant_id IS NULL LIMIT 1",
        str(tenant_id) if tenant_id is not None else None,
    )
    return _plain(row) if row else None


def _check_terms(t: dict[str, Any]) -> None:
    if t["suspend_after_days"] <= t["warn_after_days"]:
        raise Invalid("invalid_terms", "suspend_after_days must be more than warn_after_days")


async def put_terms(
    conn: asyncpg.Connection, tenant_id: str | None, body: dict[str, Any], actor: str
) -> dict[str, Any]:
    """Replaces the default terms (``tenant_id`` None) or an agency's override (created when missing)."""
    _check_terms(body)
    values = [body[k] for k in TERMS_FIELDS]
    async with conn.transaction():
        if tenant_id is not None and not await conn.fetchval(
            "SELECT 1 FROM booking.b2b_partners WHERE tenant_id = $1::uuid", tenant_id
        ):
            raise NotFound("partner_not_found", "no B2B partner (agency) with this tenant id")
        before = await conn.fetchrow(
            f"SELECT {TERMS_COLUMNS} FROM booking.billing_terms WHERE party_tenant_id IS NOT DISTINCT FROM $1::uuid"  # noqa: S608
            " FOR UPDATE",
            tenant_id,
        )
        if before is None:
            row = await conn.fetchrow(
                "INSERT INTO booking.billing_terms (party_tenant_id, deposit_pct, balance_days_before_departure,"  # noqa: S608
                " saas_due_days, settlement_due_days, warn_after_days, suspend_after_days, saas_currency, updated_by)"
                f" VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9) RETURNING {TERMS_COLUMNS}",
                tenant_id,
                *values,
                actor,
            )
        else:
            row = await conn.fetchrow(
                "UPDATE booking.billing_terms SET deposit_pct = $2, balance_days_before_departure = $3,"  # noqa: S608
                " saas_due_days = $4, settlement_due_days = $5, warn_after_days = $6, suspend_after_days = $7,"
                f" saas_currency = $8, updated_by = $9 WHERE id = $1::uuid RETURNING {TERMS_COLUMNS}",
                before["id"],
                *values,
                actor,
            )
        out = _plain(row)
        await _audit(conn, actor, "billing_terms.set", "billing_terms", out["id"],
                     "default terms" if tenant_id is None else f"terms {tenant_id}",
                     _jsonable(_plain(before)) if before else None, _jsonable(out))  # fmt: skip
    return out


async def delete_terms_override(conn: asyncpg.Connection, tenant_id: str, actor: str) -> None:
    async with conn.transaction():
        before = await conn.fetchrow(
            f"DELETE FROM booking.billing_terms WHERE party_tenant_id = $1::uuid RETURNING {TERMS_COLUMNS}",  # noqa: S608
            tenant_id,
        )
        if before is None:
            raise NotFound("terms_not_found", "this agency has no terms override")
        out = _plain(before)
        await _audit(conn, actor, "billing_terms.removed", "billing_terms", out["id"], f"terms {tenant_id}",
                     _jsonable(out), None)  # fmt: skip


# ── Per-tour terms (AA-1075 bk-18, bk-047) ───────────────────────────────────
#
# booking.trip_plan_billing_terms: deposit % and balance days for one trip plan, for every agency (party NULL) or one
# agency. The most specific wins: agency + tour > tour > agency (billing_terms) > default (billing_terms); the other
# terms (due days, warn / suspend, SaaS currency) always come from billing_terms.

PLAN_TERMS_COLUMNS = (
    "t.id::text AS id, t.trip_plan_id::text AS trip_plan_id, p.source_id, t.party_tenant_id, t.deposit_pct,"
    " t.balance_days_before_departure, t.updated_at, t.updated_by"
)
PLAN_SQL = "SELECT id::text AS id, source_id FROM booking.trip_plans WHERE source_id = $1"


async def plan_terms(conn: asyncpg.Connection, tenant_id: Any, plan_id: str | None) -> dict[str, Any] | None:
    """:func:`terms_for` with the deposit % and balance days of the most specific tour row laid over it (``scope``
    says which row won: ``agency_tour``, ``tour``, or ``agency`` / ``default`` from billing_terms)."""
    base = await terms_for(conn, tenant_id)
    if base is None:
        return None
    base = {**base, "scope": "agency" if base.get("party_tenant_id") is not None else "default"}
    if not plan_id:
        return base
    row = await conn.fetchrow(
        "SELECT party_tenant_id, deposit_pct, balance_days_before_departure FROM booking.trip_plan_billing_terms"
        " WHERE trip_plan_id = $1::uuid AND (party_tenant_id = $2::uuid OR party_tenant_id IS NULL)"
        " ORDER BY party_tenant_id IS NULL LIMIT 1",
        plan_id,
        str(tenant_id) if tenant_id is not None else None,
    )
    if row is None:
        return base
    return {
        **base,
        "deposit_pct": row["deposit_pct"],
        "balance_days_before_departure": row["balance_days_before_departure"],
        "scope": "agency_tour" if row["party_tenant_id"] is not None else "tour",
    }


async def _plan(conn: asyncpg.Connection, source_id: str) -> Any:
    plan = await conn.fetchrow(PLAN_SQL, source_id)
    if plan is None:
        raise NotFound("trip_plan_not_found", f"no trip plan for {source_id}")
    return plan


async def list_plan_terms(conn: asyncpg.Connection, source_id: str) -> list[dict[str, Any]]:
    plan = await _plan(conn, source_id)
    rows = await conn.fetch(
        f"SELECT {PLAN_TERMS_COLUMNS}, pa.agency_name FROM booking.trip_plan_billing_terms t"  # noqa: S608
        " JOIN booking.trip_plans p ON p.id = t.trip_plan_id"
        " LEFT JOIN booking.b2b_partners pa ON pa.tenant_id = t.party_tenant_id"
        " WHERE t.trip_plan_id = $1::uuid ORDER BY t.party_tenant_id IS NOT NULL, pa.agency_name",
        plan["id"],
    )
    return [_plain(r) for r in rows]


def _check_plan_terms(body: dict[str, Any]) -> tuple[Decimal, int]:
    deposit = Decimal(str(body["deposit_pct"]))
    days = int(body["balance_days_before_departure"])
    if not Decimal(0) <= deposit <= Decimal(1):
        raise Invalid("invalid_terms", "deposit_pct is a fraction from 0 to 1")
    if not 0 <= days <= 365:
        raise Invalid("invalid_terms", "balance_days_before_departure is 0 to 365")
    return deposit, days


async def put_plan_terms(
    conn: asyncpg.Connection, source_id: str, tenant_id: str | None, body: dict[str, Any], actor: str
) -> dict[str, Any]:
    """Set the tour's terms for every agency (``tenant_id`` None) or one agency (created when missing)."""
    deposit, days = _check_plan_terms(body)
    async with conn.transaction():
        plan = await _plan(conn, source_id)
        if tenant_id is not None and not await conn.fetchval(
            "SELECT 1 FROM booking.b2b_partners WHERE tenant_id = $1::uuid", tenant_id
        ):
            raise NotFound("partner_not_found", "no B2B partner (agency) with this tenant id")
        before = await conn.fetchrow(
            "SELECT id::text AS id, deposit_pct, balance_days_before_departure FROM booking.trip_plan_billing_terms"
            " WHERE trip_plan_id = $1::uuid AND party_tenant_id IS NOT DISTINCT FROM $2::uuid FOR UPDATE",
            plan["id"],
            tenant_id,
        )
        if before is None:
            row_id = await conn.fetchval(
                "INSERT INTO booking.trip_plan_billing_terms (trip_plan_id, party_tenant_id, deposit_pct,"
                " balance_days_before_departure, updated_by) VALUES ($1::uuid, $2::uuid, $3, $4, $5)"
                " RETURNING id::text",
                plan["id"],
                tenant_id,
                deposit,
                days,
                actor,
            )
        else:
            row_id = before["id"]
            await conn.execute(
                "UPDATE booking.trip_plan_billing_terms SET deposit_pct = $2, balance_days_before_departure = $3,"
                " updated_by = $4 WHERE id = $1::uuid",
                row_id,
                deposit,
                days,
                actor,
            )
        after = {"deposit_pct": deposit, "balance_days_before_departure": days, "party_tenant_id": tenant_id}
        await _audit(conn, actor, "trip_plan_terms.set", "trip_plan", plan["id"], f"tour terms {source_id}",
                     _jsonable(_plain(before)) if before else None, _jsonable(after))  # fmt: skip
        row = await conn.fetchrow(
            f"SELECT {PLAN_TERMS_COLUMNS} FROM booking.trip_plan_billing_terms t"  # noqa: S608
            " JOIN booking.trip_plans p ON p.id = t.trip_plan_id WHERE t.id = $1::uuid",
            row_id,
        )
    return _plain(row)


async def delete_plan_terms(conn: asyncpg.Connection, source_id: str, tenant_id: str | None, actor: str) -> None:
    async with conn.transaction():
        plan = await _plan(conn, source_id)
        before = await conn.fetchrow(
            "DELETE FROM booking.trip_plan_billing_terms WHERE trip_plan_id = $1::uuid"
            " AND party_tenant_id IS NOT DISTINCT FROM $2::uuid"
            " RETURNING id::text AS id, deposit_pct, balance_days_before_departure, party_tenant_id",
            plan["id"],
            tenant_id,
        )
        if before is None:
            raise NotFound("terms_not_found", "this tour has no such terms row")
        await _audit(conn, actor, "trip_plan_terms.removed", "trip_plan", plan["id"], f"tour terms {source_id}",
                     _jsonable(_plain(before)), None)  # fmt: skip
