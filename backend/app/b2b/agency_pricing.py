"""Agency price tiers and contract rate cards (AA-1030, bk-045; root ADR 0019 b15, ADR 0005a decision 1).

ADR 0019 b15: one net price per tour per season, three agency tiers with a percentage discount set per contract
(0 / 3 / 6 % to start, policy data editable later) and a suggested retail price (RRP) = net + 25 % (AA-1029). Here
"net" is the agency (b2b) price Booking quotes, so a tier discount is taken off the **b2b price** of every quote line;
the RRP (``price.from.v1``, the public "from" price) stays the same for every agency.

- **Tier** (``booking.partner_tiers``, ``b2b_partners.tier_code``): every partner has one (default ``standard``).
- **Tier discount** (``booking.partner_tier_discounts``): dated, append-only rows, in force only once a second person
  approved them (``booking.partner_price_approvals``, the maker-checker of ``booking.pricing_rules``): in force on a
  day = the newest approved row of the tier dated on or before it. No approved row = 0 %.
- **Contract rate card** (``booking.partner_rate_cards``): an optional discount of one partner, for the whole
  catalogue or one trip plan, ``valid_from`` .. ``valid_to``, approved the same way. A card in force overrides the
  tier; a plan card beats a catalogue card; among cards of one scope the newest ``valid_from`` wins (on one day an end
  card first).
- **End card** (``discount_pct`` NULL): a card that ends the contract rate of its scope from its ``valid_from`` (the
  plan line falls back to the catalogue card, then to the tier). Cards are append-only, so this is how an open-ended
  card is ended (contract over, card approved by mistake); it goes through the same maker-checker. A 0 % card is not
  an end: it still overrides the tier.
- **Applied** (:func:`terms_for` → :meth:`AgencyTerms.discount_for`): ``b2b_price = round_up(sell x (1 - discount))``
  per quote line (``app.b2b.quotes``); the line stores the discount and the card behind it.
- **Floor** (warn, never block): a discount can take AA's margin below the ``b2b`` rule's floor. Nothing is capped
  (the discount is the contract's); the staff page shows the margin left after each tier's discount under the rule
  in force and flags it below the floor (:func:`margin_after_discount`).

Staff admin (``/booking/pricing/partner-*``, ``app.routers.agency_pricing``): reads ``pricing.read``, writes and
approvals ``pricing.write``; the creator of a discount or card row cannot approve it (403 ``own_row``, and the bk-045
trigger). Assigning a partner's tier is audited (``b2b_partner.tier_changed``); it changes no rate, so it needs no
second person.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import asyncpg

from app import audit
from app.auth.staff import Staff
from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.pricing import engine, repository
from app.pricing import policy as pricing_policy

MAX_ROWS = 500
MAX_DISCOUNT = Decimal("0.5")  # the bk-045 CHECK: below half the price
ZERO = Decimal("0")
DEFAULT_TIER = "standard"


# ── Pricing reads (quotes) ───────────────────────────────────────────────────

TIER_IN_FORCE_SQL = """
SELECT p.tier_code,
       (SELECT d.discount_pct
          FROM booking.partner_tier_discounts d
          JOIN booking.partner_price_approvals a ON a.partner_tier_discount_id = d.id
         WHERE d.tier_code = p.tier_code AND d.valid_from <= $2
         ORDER BY d.valid_from DESC LIMIT 1) AS tier_discount
FROM booking.b2b_partners p
WHERE p.id = $1::uuid
"""
# Among the approved cards of one scope in force on the day: the newest valid_from, an end card first on one day.
CARD_ORDER = "c.valid_from DESC, (c.discount_pct IS NULL) DESC, c.created_at DESC"
CARDS_IN_FORCE_SQL = f"""
SELECT DISTINCT ON (c.trip_plan_id) c.id::text AS id, c.trip_plan_id::text AS trip_plan_id, c.discount_pct
FROM booking.partner_rate_cards c
JOIN booking.partner_price_approvals a ON a.partner_rate_card_id = c.id
WHERE c.partner_id = $1::uuid AND c.valid_from <= $2 AND (c.valid_to IS NULL OR c.valid_to >= $2)
ORDER BY c.trip_plan_id, {CARD_ORDER}
"""  # noqa: S608 — constant fragment


@dataclass(frozen=True)
class Card:
    id: str
    trip_plan_id: str | None
    discount_pct: Decimal | None  # None = an end card: no contract rate for this scope

    @property
    def ends(self) -> bool:
        return self.discount_pct is None


@dataclass(frozen=True)
class AgencyTerms:
    """What a partner's quote is priced with on one day."""

    tier_code: str
    tier_discount: Decimal
    cards: tuple[Card, ...] = ()

    def discount_for(self, plan_id: str | None) -> tuple[Decimal, str | None]:
        """``(discount, rate_card_id)`` of a line: the plan's card, else the catalogue card, else the tier. An end card
        in force for a scope means no card there (the next one down applies)."""
        by_plan = {c.trip_plan_id: c for c in self.cards}
        for card in (by_plan.get(plan_id) if plan_id else None, by_plan.get(None)):
            if card is not None and card.discount_pct is not None:
                return card.discount_pct, card.id
        return self.tier_discount, None


NO_TERMS = AgencyTerms(tier_code=DEFAULT_TIER, tier_discount=ZERO)


async def terms_for(conn: asyncpg.Connection, partner_id: str, on: date) -> AgencyTerms:
    row = await conn.fetchrow(TIER_IN_FORCE_SQL, partner_id, on)
    if row is None:
        return NO_TERMS
    cards = await conn.fetch(CARDS_IN_FORCE_SQL, partner_id, on)
    return AgencyTerms(
        tier_code=row["tier_code"],
        tier_discount=row["tier_discount"] if row["tier_discount"] is not None else ZERO,
        cards=tuple(Card(r["id"], r["trip_plan_id"], r["discount_pct"]) for r in cards),
    )


def apply_discount(sell: Decimal, discount: Decimal) -> Decimal:
    """The b2b price after the agency discount (unrounded; the quote rounds the line up)."""
    if discount <= ZERO:
        return sell
    return sell * (engine.ONE - discount)


def margin_after_discount(rule: engine.PricingRule, discount: Decimal) -> Decimal:
    """AA's gross margin on a b2b price sold at the rule's target margin less ``discount`` (card fee kept, as the
    engine's effective margin): ``1 - fee - (1 - margin - fee) / (1 - discount)``, to 4 places."""
    fee = rule.card_fee_pct
    value = engine.ONE - fee - (engine.ONE - rule.target_margin - fee) / (engine.ONE - discount)
    return value.quantize(engine.RATIO)


# ── Staff admin: tiers ───────────────────────────────────────────────────────

TIERS_SQL = """
SELECT t.code, t.label, t.sort_order, t.created_at,
       (SELECT count(*) FROM booking.b2b_partners p WHERE p.tier_code = t.code) AS partners,
       (SELECT d.discount_pct
          FROM booking.partner_tier_discounts d
          JOIN booking.partner_price_approvals a ON a.partner_tier_discount_id = d.id
         WHERE d.tier_code = t.code AND d.valid_from <= $1
         ORDER BY d.valid_from DESC LIMIT 1) AS discount_in_force,
       (SELECT count(*) FROM booking.partner_tier_discounts d
         WHERE d.tier_code = t.code
           AND NOT EXISTS (SELECT 1 FROM booking.partner_price_approvals a
                            WHERE a.partner_tier_discount_id = d.id)) AS pending
FROM booking.partner_tiers t
ORDER BY t.sort_order, t.code
"""


def _floor_info(rule: repository.DatedRule | None, discount: Decimal) -> dict[str, Any]:
    if rule is None:
        return {"margin_after_discount": None, "below_floor": None}
    left = margin_after_discount(rule.rule, discount)
    return {"margin_after_discount": left, "below_floor": left < rule.rule.floor_margin}


async def list_tiers(conn: asyncpg.Connection, *, today: date) -> dict[str, Any]:
    rule = await repository.rule_in_force(conn, engine.Channel.B2B, today)
    items = []
    for r in await conn.fetch(TIERS_SQL, today):
        discount = r["discount_in_force"] if r["discount_in_force"] is not None else ZERO
        items.append(
            {
                "code": r["code"],
                "label": r["label"],
                "sort_order": r["sort_order"],
                "partners": r["partners"],
                "discount_in_force": discount,
                "pending": r["pending"],
                **_floor_info(rule, discount),
            }
        )
    b2b_rule = None
    if rule is not None:
        b2b_rule = {
            "target_margin": rule.rule.target_margin,
            "floor_margin": rule.rule.floor_margin,
            "card_fee_pct": rule.rule.card_fee_pct,
            "valid_from": rule.valid_from,
            "rrp_markup_pct": rule.rrp_markup_pct,
        }
    return {"items": items, "b2b_rule": b2b_rule, "today": today}


async def _tier(conn: asyncpg.Connection, code: str) -> Any:
    row = await conn.fetchrow("SELECT code, label, sort_order FROM booking.partner_tiers WHERE code = $1", code)
    if row is None:
        raise NotFound("tier_not_found", f"no partner tier {code}")
    return row


async def create_tier(conn: asyncpg.Connection, staff: Staff, data: dict[str, Any], *, today: date) -> dict[str, Any]:
    label = data["label"].strip()
    try:
        await conn.execute(
            "INSERT INTO booking.partner_tiers (code, label, sort_order, created_by) VALUES ($1, $2, $3, $4)",
            data["code"],
            label,
            data.get("sort_order") or 0,
            staff.cis_user_id,
        )
    except asyncpg.UniqueViolationError:
        raise Conflict("tier_exists", f"a tier with code {data['code']} already exists") from None
    except asyncpg.CheckViolationError:
        raise Invalid("invalid_tier", "the tier breaks a partner_tiers check") from None
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="partner_tier.created",
        entity_type="partner_tier",
        entity_id=None,
        after={"code": data["code"], "label": label, "sort_order": data.get("sort_order") or 0},
        entity_label=data["code"],
        summary=f"New partner tier {label} ({data['code']}), no discount until one is approved",
    )
    return next(t for t in (await list_tiers(conn, today=today))["items"] if t["code"] == data["code"])


async def update_tier(
    conn: asyncpg.Connection, staff: Staff, code: str, changes: dict[str, Any], *, today: date
) -> dict[str, Any]:
    before = await _tier(conn, code)
    changed = {k: v for k, v in changes.items() if k in ("label", "sort_order") and v is not None and before[k] != v}
    if changed:
        if "label" in changed:
            changed["label"] = changed["label"].strip()
        sets = ", ".join(f"{k} = ${i}" for i, k in enumerate(changed, start=2))
        try:
            await conn.execute(
                f"UPDATE booking.partner_tiers SET {sets} WHERE code = $1",  # noqa: S608 — keys from a fixed pair
                code,
                *changed.values(),
            )
        except asyncpg.CheckViolationError:
            raise Invalid("invalid_tier", "the tier breaks a partner_tiers check") from None
        await audit.record(
            conn,
            actor_type="staff",
            actor_ref=staff.cis_user_id,
            action="partner_tier.updated",
            entity_type="partner_tier",
            entity_id=None,
            before={k: before[k] for k in changed},
            after=changed,
            entity_label=code,
            summary=f"Partner tier {code} edited ({', '.join(sorted(changed))})",
        )
    return next(t for t in (await list_tiers(conn, today=today))["items"] if t["code"] == code)


# ── Staff admin: dated rows with maker-checker ───────────────────────────────

_STATUS = """
CASE
    WHEN a.id IS NULL THEN 'pending'
    WHEN p.id IN (SELECT id FROM in_force) THEN 'in_force'
    WHEN p.valid_from > $1 THEN 'scheduled'
    {expired}
    ELSE 'superseded'
END
"""
DISCOUNTS_SQL = f"""
WITH in_force AS (
    SELECT DISTINCT ON (d.tier_code) d.id
    FROM booking.partner_tier_discounts d
    JOIN booking.partner_price_approvals ap ON ap.partner_tier_discount_id = d.id
    WHERE d.valid_from <= $1
    ORDER BY d.tier_code, d.valid_from DESC
)
SELECT p.id::text AS id, p.tier_code, t.label AS tier_label, p.discount_pct, p.valid_from, p.note, p.created_at,
       p.created_by, cs.name AS created_by_name, a.approved_by, aps.name AS approved_by_name, a.approved_at,
       {_STATUS.format(expired="")} AS status
FROM booking.partner_tier_discounts p
JOIN booking.partner_tiers t ON t.code = p.tier_code
LEFT JOIN booking.partner_price_approvals a ON a.partner_tier_discount_id = p.id
LEFT JOIN booking.staff_users cs ON cs.cis_user_id = p.created_by
LEFT JOIN booking.staff_users aps ON aps.cis_user_id = a.approved_by
WHERE ($2::text IS NULL OR p.tier_code = $2) AND ($3::uuid IS NULL OR p.id = $3)
ORDER BY p.valid_from DESC, t.sort_order, p.created_at DESC
LIMIT $4
"""  # noqa: S608 — constant fragments only
CARDS_SQL = f"""
WITH in_force AS (
    SELECT DISTINCT ON (c.partner_id, c.trip_plan_id) c.id
    FROM booking.partner_rate_cards c
    JOIN booking.partner_price_approvals ap ON ap.partner_rate_card_id = c.id
    WHERE c.valid_from <= $1 AND (c.valid_to IS NULL OR c.valid_to >= $1)
    ORDER BY c.partner_id, c.trip_plan_id, {CARD_ORDER}
)
SELECT p.id::text AS id, p.partner_id::text AS partner_id, bp.agency_name, p.trip_plan_id::text AS trip_plan_id,
       tp.source_id, p.discount_pct, p.valid_from, p.valid_to, p.contract_ref, p.note, p.created_at, p.created_by,
       cs.name AS created_by_name, a.approved_by, aps.name AS approved_by_name, a.approved_at,
       {_STATUS.format(expired="WHEN p.valid_to < $1 THEN 'expired'")} AS status
FROM booking.partner_rate_cards p
JOIN booking.b2b_partners bp ON bp.id = p.partner_id
LEFT JOIN booking.trip_plans tp ON tp.id = p.trip_plan_id
LEFT JOIN booking.partner_price_approvals a ON a.partner_rate_card_id = p.id
LEFT JOIN booking.staff_users cs ON cs.cis_user_id = p.created_by
LEFT JOIN booking.staff_users aps ON aps.cis_user_id = a.approved_by
WHERE ($2::uuid IS NULL OR p.partner_id = $2) AND ($3::uuid IS NULL OR p.id = $3)
ORDER BY p.valid_from DESC, lower(bp.agency_name), p.created_at DESC
LIMIT $4
"""  # noqa: S608 — constant fragments only
APPROVAL_SQL = {
    "partner_tier_discount": "SELECT p.created_by, a.id AS approval_id FROM booking.partner_tier_discounts p"
    " LEFT JOIN booking.partner_price_approvals a ON a.partner_tier_discount_id = p.id WHERE p.id = $1::uuid",
    "partner_rate_card": "SELECT p.created_by, a.id AS approval_id FROM booking.partner_rate_cards p"
    " LEFT JOIN booking.partner_price_approvals a ON a.partner_rate_card_id = p.id WHERE p.id = $1::uuid",
}
APPROVE_SQL = {
    "partner_tier_discount": "INSERT INTO booking.partner_price_approvals"
    " (entity_type, partner_tier_discount_id, approved_by) VALUES ('partner_tier_discount', $1::uuid, $2)",
    "partner_rate_card": "INSERT INTO booking.partner_price_approvals"
    " (entity_type, partner_rate_card_id, approved_by) VALUES ('partner_rate_card', $1::uuid, $2)",
}
NOT_FOUND = {"partner_tier_discount": "tier_discount_not_found", "partner_rate_card": "rate_card_not_found"}


def _discount_row(row: Any, staff: Staff) -> dict[str, Any]:
    return {
        **pricing_policy._common(row, staff),
        "tier_code": row["tier_code"],
        "tier_label": row["tier_label"],
        "discount_pct": row["discount_pct"],
        "valid_from": row["valid_from"],
        "note": row["note"],
    }


def _card_row(row: Any, staff: Staff) -> dict[str, Any]:
    return {
        **pricing_policy._common(row, staff),
        "partner_id": row["partner_id"],
        "agency_name": row["agency_name"],
        "trip_plan_id": row["trip_plan_id"],
        "source_id": row["source_id"],
        "discount_pct": row["discount_pct"],
        "ends_contract_rate": row["discount_pct"] is None,
        "valid_from": row["valid_from"],
        "valid_to": row["valid_to"],
        "contract_ref": row["contract_ref"],
        "note": row["note"],
    }


async def list_tier_discounts(
    conn: asyncpg.Connection, staff: Staff, *, today: date, tier_code: str | None = None, limit: int = MAX_ROWS
) -> list[dict[str, Any]]:
    rows = await conn.fetch(DISCOUNTS_SQL, today, tier_code, None, min(limit, MAX_ROWS))
    return [_discount_row(r, staff) for r in rows]


async def list_rate_cards(
    conn: asyncpg.Connection, staff: Staff, *, today: date, partner_id: str | None = None, limit: int = MAX_ROWS
) -> list[dict[str, Any]]:
    rows = await conn.fetch(CARDS_SQL, today, partner_id, None, min(limit, MAX_ROWS))
    return [_card_row(r, staff) for r in rows]


async def _discount_by_id(conn: asyncpg.Connection, staff: Staff, row_id: str, today: date) -> dict[str, Any]:
    row = await conn.fetchrow(DISCOUNTS_SQL, today, None, row_id, 1)
    if row is None:
        raise NotFound("tier_discount_not_found", "no tier discount with this id")
    return _discount_row(row, staff)


async def _card_by_id(conn: asyncpg.Connection, staff: Staff, row_id: str, today: date) -> dict[str, Any]:
    row = await conn.fetchrow(CARDS_SQL, today, None, row_id, 1)
    if row is None:
        raise NotFound("rate_card_not_found", "no rate card with this id")
    return _card_row(row, staff)


def _pct(value: Decimal) -> str:
    return f"{(value * 100).normalize():f}%"


def _card_terms(value: Decimal | None) -> str:
    return "ends the contract rate (back to the tier)" if value is None else _pct(value)


def _check_discount(value: Decimal) -> None:
    if value < ZERO or value >= MAX_DISCOUNT:
        raise Invalid("invalid_discount", "a discount is from 0 to under 50 %")


async def create_tier_discount(
    conn: asyncpg.Connection, staff: Staff, data: dict[str, Any], *, today: date
) -> dict[str, Any]:
    """A new pending discount row of a tier. ``data``: tier_code, discount_pct, valid_from, note."""
    await _tier(conn, data["tier_code"])
    _check_discount(data["discount_pct"])
    note = (data.get("note") or "").strip() or None
    try:
        row_id = await conn.fetchval(
            "INSERT INTO booking.partner_tier_discounts (tier_code, discount_pct, valid_from, note, created_by)"
            " VALUES ($1, $2, $3, $4, $5) RETURNING id::text",
            data["tier_code"],
            data["discount_pct"],
            data["valid_from"],
            note,
            staff.cis_user_id,
        )
    except asyncpg.UniqueViolationError:
        raise Conflict(
            "tier_discount_exists",
            f"a {data['tier_code']} discount dated {data['valid_from']} already exists; use another date",
        ) from None
    except asyncpg.CheckViolationError:
        raise Invalid("invalid_discount", "the row breaks a partner_tier_discounts check") from None
    row = await _discount_by_id(conn, staff, row_id, today)
    label = f"{data['tier_code']} from {data['valid_from']}"
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="partner_tier_discount.created",
        entity_type="partner_tier_discount",
        entity_id=row_id,
        after={k: row[k] for k in ("tier_code", "discount_pct", "valid_from", "note")},
        entity_label=label,
        summary=f"New tier discount {label}: {_pct(data['discount_pct'])} (pending approval)",
    )
    return row


async def _plan_id(conn: asyncpg.Connection, source_id: str | None) -> str | None:
    if not source_id:
        return None
    plan_id = await conn.fetchval(
        "SELECT id::text FROM booking.trip_plans WHERE source_system = 'cis' AND source_id = $1", source_id
    )
    if plan_id is None:
        raise NotFound("trip_plan_not_found", "no trip plan for this tour key")
    return plan_id


async def create_rate_card(
    conn: asyncpg.Connection, staff: Staff, data: dict[str, Any], *, today: date
) -> dict[str, Any]:
    """A new pending contract rate card. ``data``: partner_id, source_id (None = the whole catalogue), discount_pct
    (None = an end card: ends the contract rate of that scope from valid_from), valid_from, valid_to (None =
    open-ended), contract_ref, note."""
    partner = await conn.fetchrow(
        "SELECT agency_name FROM booking.b2b_partners WHERE id = $1::uuid", data["partner_id"]
    )
    if partner is None:
        raise NotFound("partner_not_found", "no B2B partner with this id")
    if data["discount_pct"] is not None:
        _check_discount(data["discount_pct"])
    if data.get("valid_to") is not None and data["valid_to"] < data["valid_from"]:
        raise Invalid("invalid_validity", "valid_to is on or after valid_from")
    plan_id = await _plan_id(conn, data.get("source_id"))
    contract_ref = (data.get("contract_ref") or "").strip() or None
    note = (data.get("note") or "").strip() or None
    try:
        row_id = await conn.fetchval(
            "INSERT INTO booking.partner_rate_cards (partner_id, trip_plan_id, discount_pct, valid_from, valid_to,"
            " contract_ref, note, created_by) VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8) RETURNING id::text",
            data["partner_id"],
            plan_id,
            data["discount_pct"],
            data["valid_from"],
            data.get("valid_to"),
            contract_ref,
            note,
            staff.cis_user_id,
        )
    except asyncpg.UniqueViolationError:
        raise Conflict(
            "rate_card_exists",
            f"a {'end' if data['discount_pct'] is None else 'discount'} card for this partner and scope dated"
            f" {data['valid_from']} exists; use another date",
        ) from None
    except asyncpg.CheckViolationError:
        raise Invalid("invalid_rate_card", "the card breaks a partner_rate_cards check") from None
    row = await _card_by_id(conn, staff, row_id, today)
    label = _card_label(row)
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="partner_rate_card.created",
        entity_type="partner_rate_card",
        entity_id=row_id,
        after={
            k: row[k]
            for k in ("partner_id", "source_id", "discount_pct", "valid_from", "valid_to", "contract_ref", "note")
        },
        entity_label=label,
        summary=f"New rate card {label}: {_card_terms(data['discount_pct'])} (pending approval)",
    )
    return row


def _card_label(row: dict[str, Any]) -> str:
    scope = row["source_id"] or "all tours"
    return f"{str(row['agency_name'])[:40]} · {scope[:40]} from {row['valid_from']}"


async def _approve(conn: asyncpg.Connection, staff: Staff, entity_type: str, row_id: str) -> None:
    row = await conn.fetchrow(APPROVAL_SQL[entity_type], row_id)
    if row is None:
        raise NotFound(NOT_FOUND[entity_type], f"no {entity_type.replace('_', ' ')} with this id")
    if row["approval_id"] is not None:
        raise Conflict("already_approved", "this row is already approved")
    if row["created_by"] == staff.cis_user_id:
        raise Forbidden("own_row", "maker-checker: a second person must approve a row you created")
    try:
        await conn.execute(APPROVE_SQL[entity_type], row_id, staff.cis_user_id)
    except asyncpg.UniqueViolationError:  # approved concurrently by someone else
        raise Conflict("already_approved", "this row is already approved") from None
    except asyncpg.CheckViolationError:  # the bk-045 trigger: same rule as above, enforced by the DB
        raise Forbidden("own_row", "maker-checker: a second person must approve a row you created") from None


async def approve_tier_discount(conn: asyncpg.Connection, staff: Staff, row_id: str, *, today: date) -> dict[str, Any]:
    await _approve(conn, staff, "partner_tier_discount", row_id)
    row = await _discount_by_id(conn, staff, row_id, today)
    label = f"{row['tier_code']} from {row['valid_from']}"
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="partner_tier_discount.approved",
        entity_type="partner_tier_discount",
        entity_id=row_id,
        before={"status": "pending"},
        after={"status": row["status"], "created_by": (row["created_by"] or {}).get("id")},
        entity_label=label,
        summary=f"Approved tier discount {label} ({row['status'].replace('_', ' ')})",
    )
    return row


async def approve_rate_card(conn: asyncpg.Connection, staff: Staff, row_id: str, *, today: date) -> dict[str, Any]:
    await _approve(conn, staff, "partner_rate_card", row_id)
    row = await _card_by_id(conn, staff, row_id, today)
    label = _card_label(row)
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="partner_rate_card.approved",
        entity_type="partner_rate_card",
        entity_id=row_id,
        before={"status": "pending"},
        after={"status": row["status"], "created_by": (row["created_by"] or {}).get("id")},
        entity_label=label,
        summary=f"Approved rate card {label} ({row['status'].replace('_', ' ')})",
    )
    return row


# ── Staff admin: partners and their tier ────────────────────────────────────

PARTNER_TERMS_SQL = f"""
SELECT p.id::text AS id, p.agency_name, p.status, p.billing_currency::text AS billing_currency, p.tier_code,
       t.label AS tier_label, p.projected_at IS NOT NULL AS projected,
       (SELECT d.discount_pct
          FROM booking.partner_tier_discounts d
          JOIN booking.partner_price_approvals a ON a.partner_tier_discount_id = d.id
         WHERE d.tier_code = p.tier_code AND d.valid_from <= $1
         ORDER BY d.valid_from DESC LIMIT 1) AS tier_discount,
       (SELECT c.discount_pct
          FROM booking.partner_rate_cards c
          JOIN booking.partner_price_approvals a ON a.partner_rate_card_id = c.id
         WHERE c.partner_id = p.id AND c.trip_plan_id IS NULL AND c.valid_from <= $1
           AND (c.valid_to IS NULL OR c.valid_to >= $1)
         ORDER BY {CARD_ORDER} LIMIT 1) AS card_discount,
       (SELECT count(*) FROM (
            SELECT DISTINCT ON (c.trip_plan_id) c.discount_pct
              FROM booking.partner_rate_cards c
              JOIN booking.partner_price_approvals a ON a.partner_rate_card_id = c.id
             WHERE c.partner_id = p.id AND c.trip_plan_id IS NOT NULL AND c.valid_from <= $1
               AND (c.valid_to IS NULL OR c.valid_to >= $1)
             ORDER BY c.trip_plan_id, {CARD_ORDER}) plan_card
         WHERE plan_card.discount_pct IS NOT NULL) AS plan_cards
FROM booking.b2b_partners p
JOIN booking.partner_tiers t ON t.code = p.tier_code
WHERE ($2::uuid IS NULL OR p.id = $2)
ORDER BY lower(p.agency_name), p.id
"""  # noqa: S608 — constant fragment


def _terms_row(r: Any) -> dict[str, Any]:
    tier_discount = r["tier_discount"] if r["tier_discount"] is not None else ZERO
    card = r["card_discount"]
    return {
        "id": r["id"],
        "agency_name": r["agency_name"],
        "status": r["status"],
        "billing_currency": r["billing_currency"],
        "projected": r["projected"],
        "tier_code": r["tier_code"],
        "tier_label": r["tier_label"],
        "tier_discount": tier_discount,
        "card_discount": card,
        "plan_cards": r["plan_cards"],
        # What a catalogue line of this partner is priced with today (a plan card can still differ per tour).
        "effective_discount": card if card is not None else tier_discount,
    }


async def list_partner_terms(conn: asyncpg.Connection, *, today: date) -> dict[str, Any]:
    return {"items": [_terms_row(r) for r in await conn.fetch(PARTNER_TERMS_SQL, today, None)], "today": today}


async def set_partner_tier(
    conn: asyncpg.Connection, staff: Staff, partner_id: str, tier_code: str, *, today: date
) -> dict[str, Any]:
    before = await conn.fetchrow(
        "SELECT agency_name, tier_code FROM booking.b2b_partners WHERE id = $1::uuid FOR UPDATE", partner_id
    )
    if before is None:
        raise NotFound("partner_not_found", "no B2B partner with this id")
    tier = await _tier(conn, tier_code)
    if before["tier_code"] != tier_code:
        await conn.execute(
            "UPDATE booking.b2b_partners SET tier_code = $2, updated_by = $3 WHERE id = $1::uuid",
            partner_id,
            tier_code,
            staff.cis_user_id,
        )
        await audit.record(
            conn,
            actor_type="staff",
            actor_ref=staff.cis_user_id,
            action="b2b_partner.tier_changed",
            entity_type="b2b_partner",
            entity_id=partner_id,
            before={"tier_code": before["tier_code"]},
            after={"tier_code": tier_code},
            entity_label=str(before["agency_name"])[:80],
            summary=f"Price tier {before['tier_code']} → {tier['label']}",
        )
    row = await conn.fetchrow(PARTNER_TERMS_SQL, today, partner_id)
    return _terms_row(row)
