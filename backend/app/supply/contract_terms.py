"""Structured supplier contract terms (AA-1145, bk-060; advisories T12, B4): ``booking.supplier_contract_terms``.

The terms the supplier (DMC) gives AA under one supplier contract, one row per contract:

- **cancellation tiers** ``[{min_days, fee_pct}]`` (the bk-021 shape and rules,
  :func:`app.b2b.cancellation.parse_tiers`): cancelling ``d`` days before the service costs the ``fee_pct`` of the
  tier with the largest ``min_days <= d``;
- **payment terms**: a deposit (% of the net, due ``deposit_due_days`` after the supplier confirms), then either the
  balance ``balance_due_days_before`` the service (prepaid supplier) or the whole amount ``credit_days_after`` the
  service (credit terms), never both;
- **child %** (B4): the child net as a % of the adult net for a per-person service that has no child rate in this
  contract.

Written while the contract is a draft (``supply.write``); the writer becomes the draft's maker, so a second person
publishes the terms with the contract (bk-008 maker-checker; the DB guard refuses any write once it is published).
The free-text ``supplier_contracts.payment_terms`` / ``cancellation_policy`` stay as the supplier's wording.

**Offers only override** (:func:`effective`): an offer (the activity slice) may carry its own value for any of these
fields; a field the offer leaves empty takes the contract's. There is one terms shape for contracts and offers.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, fields, replace
from decimal import Decimal, InvalidOperation
from typing import Any

import asyncpg

from app import audit
from app.auth.staff import Staff
from app.b2b import cancellation
from app.errors import Conflict, Invalid
from app.suppliers import service as suppliers

FIELDS = (
    "cancellation_tiers",
    "deposit_pct",
    "deposit_due_days",
    "balance_due_days_before",
    "credit_days_after",
    "child_pct",
)
DAYS_MAX = 365  # technical bound of the bk-060 CHECKs
PCT_PLACES = Decimal("0.01")


@dataclass(frozen=True)
class Terms:
    cancellation_tiers: tuple[cancellation.Tier, ...] | None = None
    deposit_pct: Decimal | None = None
    deposit_due_days: int | None = None
    balance_due_days_before: int | None = None
    credit_days_after: int | None = None
    child_pct: Decimal | None = None

    def out(self) -> dict[str, Any]:
        return {
            "cancellation_tiers": cancellation.tiers_out(self.cancellation_tiers) if self.cancellation_tiers else None,
            "deposit_pct": _pct_text(self.deposit_pct),
            "deposit_due_days": self.deposit_due_days,
            "balance_due_days_before": self.balance_due_days_before,
            "credit_days_after": self.credit_days_after,
            "child_pct": _pct_text(self.child_pct),
        }

    def fee_pct(self, days_before: int) -> Decimal | None:
        """The supplier's cancellation fee % ``days_before`` the service, or None without tiers."""
        return cancellation.fee_pct_for(self.cancellation_tiers, days_before) if self.cancellation_tiers else None


EMPTY = Terms()


def _pct_text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def effective(contract: Terms, offer: Terms | None = None) -> Terms:
    """The terms in force for an offer under a contract: each field the offer sets wins, the rest are the contract's."""
    if offer is None:
        return contract
    return replace(
        contract,
        **{f.name: getattr(offer, f.name) for f in fields(Terms) if getattr(offer, f.name) is not None},
    )


def _pct(value: Any, name: str, *, zero_ok: bool) -> Decimal | None:
    if value is None:
        return None
    try:
        number = Decimal(str(value).strip()) if not isinstance(value, bool) else None
    except InvalidOperation:
        number = None
    low_ok = number is not None and number.is_finite() and (number >= 0 if zero_ok else number > 0)
    if not low_ok or number > 100 or number != number.quantize(PCT_PLACES):  # type: ignore[operator]
        floor = "0" if zero_ok else "more than 0"
        raise Invalid(f"invalid_{name}", f"{name} is a number from {floor} to 100 with at most 2 decimals")
    return number.quantize(PCT_PLACES)  # type: ignore[union-attr]


def _days(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= DAYS_MAX:
        raise Invalid(f"invalid_{name}", f"{name} is a whole number from 0 to {DAYS_MAX}")
    return value


def parse(data: Mapping[str, Any]) -> Terms:
    """Validate a full terms body (every field optional) into :class:`Terms`, or 422 naming the field."""
    unknown = sorted(set(data) - set(FIELDS))
    if unknown:
        raise Invalid("not_editable", f"not editable: {', '.join(unknown)}")
    tiers = None
    if data.get("cancellation_tiers") is not None:
        try:
            tiers = cancellation.parse_tiers(data["cancellation_tiers"])
        except (cancellation.TierError, ValueError) as exc:
            raise Invalid("invalid_cancellation_tiers", str(exc)) from None
    terms = Terms(
        cancellation_tiers=tiers,
        deposit_pct=_pct(data.get("deposit_pct"), "deposit_pct", zero_ok=False),
        deposit_due_days=_days(data.get("deposit_due_days"), "deposit_due_days"),
        balance_due_days_before=_days(data.get("balance_due_days_before"), "balance_due_days_before"),
        credit_days_after=_days(data.get("credit_days_after"), "credit_days_after"),
        child_pct=_pct(data.get("child_pct"), "child_pct", zero_ok=True),
    )
    if (terms.deposit_pct is None) != (terms.deposit_due_days is None):
        raise Invalid("deposit_pair", "a deposit needs both its % and the days after confirmation it is due")
    if terms.balance_due_days_before is not None and terms.credit_days_after is not None:
        raise Invalid("balance_or_credit", "the balance is either due before the service or paid on credit after it")
    return terms


def from_row(row: Mapping[str, Any] | None) -> Terms:
    if row is None:
        return EMPTY
    raw = row["cancellation_tiers"]
    tiers = cancellation.parse_tiers(raw) if raw is not None else None
    return Terms(
        cancellation_tiers=tiers,
        deposit_pct=None if row["deposit_pct"] is None else Decimal(str(row["deposit_pct"])),
        deposit_due_days=row["deposit_due_days"],
        balance_due_days_before=row["balance_due_days_before"],
        credit_days_after=row["credit_days_after"],
        child_pct=None if row["child_pct"] is None else Decimal(str(row["child_pct"])),
    )


TERMS_SQL = """
SELECT supplier_contract_id::text AS contract_id, cancellation_tiers::text AS cancellation_tiers, deposit_pct,
       deposit_due_days, balance_due_days_before, credit_days_after, child_pct, created_by, updated_by, updated_at
FROM booking.supplier_contract_terms WHERE supplier_contract_id = ANY($1::uuid[])
"""


async def terms_many(conn: asyncpg.Connection, contract_ids: list[str]) -> dict[str, Terms]:
    """The terms of every contract asked (a contract without a row has :data:`EMPTY`)."""
    if not contract_ids:
        return {}
    rows = {r["contract_id"]: r for r in await conn.fetch(TERMS_SQL, sorted(set(contract_ids)))}
    return {cid: from_row(rows.get(cid)) for cid in contract_ids}


async def get_terms(conn: asyncpg.Connection, contract_id: str) -> dict[str, Any]:
    contract = await suppliers.get_contract(conn, contract_id)
    row = await conn.fetchrow(TERMS_SQL, [contract["id"]])
    return {
        "contract_id": contract["id"],
        "contract_status": contract["status"],
        "terms": from_row(row).out(),
        "updated_by": row["updated_by"] or row["created_by"] if row else None,
        "updated_at": row["updated_at"] if row else None,
    }


async def put_terms(
    conn: asyncpg.Connection, staff: Staff, contract_id: str, data: Mapping[str, Any]
) -> dict[str, Any]:
    """Replace the terms of a draft contract (``supply.write``); the writer becomes the draft's maker."""
    terms = parse(data)
    contract = await suppliers.get_contract(conn, contract_id, lock=True)
    if contract["status"] != "draft":
        raise Conflict(
            "contract_locked", f"the terms of a {contract['status']} contract are fixed; create a new contract instead"
        )
    before = await get_terms(conn, contract["id"])
    tiers = cancellation.tiers_json(terms.cancellation_tiers) if terms.cancellation_tiers else None
    try:
        await conn.execute(
            """
            INSERT INTO booking.supplier_contract_terms
                (supplier_contract_id, cancellation_tiers, deposit_pct, deposit_due_days, balance_due_days_before,
                 credit_days_after, child_pct, created_by)
            VALUES ($1::uuid, $2::jsonb, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (supplier_contract_id) DO UPDATE SET
                cancellation_tiers = EXCLUDED.cancellation_tiers, deposit_pct = EXCLUDED.deposit_pct,
                deposit_due_days = EXCLUDED.deposit_due_days,
                balance_due_days_before = EXCLUDED.balance_due_days_before,
                credit_days_after = EXCLUDED.credit_days_after, child_pct = EXCLUDED.child_pct,
                updated_by = EXCLUDED.created_by
            """,
            contract["id"],
            tiers,
            terms.deposit_pct,
            terms.deposit_due_days,
            terms.balance_due_days_before,
            terms.credit_days_after,
            terms.child_pct,
            staff.cis_user_id,
        )
    except (asyncpg.CheckViolationError, asyncpg.ObjectNotInPrerequisiteStateError) as exc:  # the DB backstop
        raise Invalid("invalid_terms", str(exc)) from exc
    await conn.execute(
        "UPDATE booking.supplier_contracts SET updated_by = $2 WHERE id = $1::uuid", contract["id"], staff.cis_user_id
    )
    after = terms.out()
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="supplier_contract.terms_set",
        entity_type="supplier_contract",
        entity_id=contract["id"],
        before=json.loads(json.dumps(before["terms"])),
        after=after,
        entity_label=contract["title"],
        summary="Set the structured terms (cancellation tiers, payment, child %)",
    )
    return await get_terms(conn, contract["id"])
