"""Supplier net rates (bk-010, AA-823; ADR 0005 decisions 2 and 8) and the net cost a version item takes from them.

A rate prices one hotel room type (per room night) or one service (per the service unit) under a supplier contract
(bk-008): season label, validity inside the contract's, meal plan (rooms), pax band, net amount in the contract
currency.

- **Written under a draft contract only.** Once a second person publishes the contract its rates are fixed ("never
  overwrite", ADR 0005 d2); new prices = a new contract. The DB trigger ``supplier_rate_guard`` enforces the same.
- **A rate write makes the writer the contract's maker** (``updated_by``), so the maker-checker rule of bk-008 also
  covers the rates: whoever adds or changes a rate cannot publish that contract.
- **In force** on a day = contract published and both validities cover the day. Per supplier, when several rates
  match, the supplier's newest published contract among those with a matching rate wins (``published_at``, then the
  contract id: :mod:`app.supply.newest_contract`, the same rule that picks the allotment terms, so a re-imported
  contract's raised price applies on its dates while the older contract still prices the dates it alone covers); inside
  it (seasons, pax bands) the newest ``valid_from`` wins, then a rate with a pax band, then the lowest net. Hotel child
  prices follow the contract of the room rate chosen.
- **Board basis** (AA-1153 T9, bk-069): a lookup asks a board (``meal_plan``) or none. None = the pricing policy's
  default board (``hotel_default_board``, seeded BB; :func:`default_board`): an exact rate on that board first, then a
  rate with no board, then any other board (old data keeps pricing). An asked board takes that board or a rate with no
  board, never another one (a cheaper BB rate never wins over an asked HB).
- **Several DMCs** (AA-1153 T8, bk-069): a hotel may be sold by several suppliers (``booking.hotel_supplier_links``).
  Each supplier's best rate is chosen as above; across suppliers the asked board first, then the hotel's preferred
  DMC, then the lowest net **in USD** (each net at the approved FX rate in force on the day, ``booking.fx_rates``, the
  rate a quote converts with; a currency without one sorts after those with one), then the lower net in its own
  currency. A room rate counts only while its hotel has an active link to the contract's supplier. A lookup may be
  pinned to one supplier (``RateAsk.supplier_id``: the next nights of a stay, and every unit of a B2B room line, stay
  with the first night's DMC). Every found rate names its ``supplier_id``: the booking goes to the DMC of the price
  used.
- **Pax type and age band** (AA-1145, bk-060, B4): a per-person rate prices an ``adult`` (every rate before bk-060),
  a ``child`` or an ``infant``, a child / infant rate optionally for an age band (``age_min`` / ``age_max``). Every
  lookup here asks for one pax type (:class:`RateAsk`, default adult), so a child rate never becomes an adult price;
  the child line of a B2B quote follows :mod:`app.supply.child_price`.
- **Net only** (B7): ``net_amount`` is the only price any calculation reads. A commissionable supplier price keeps its
  two source numbers (``published_amount``, ``commission_pct``) and the net is derived from them
  (:func:`net_from_published`, half-up to the cent; the bk-060 CHECK holds the same equation).
- **Default net of a version item** (:func:`default_cost`): the rate in force on the item's first day (trip start +
  day_from - 1; today, UTC, when the case has no start date) for the case's pax, times a quantity — pax for a
  per-person service, days for a per-day service, one otherwise. A room is priced **night by night**
  (:func:`default_room_costs`, AA-1153): one cost row per run of nights at one rate (a stay across two seasons is
  45 + 45 + 60, never 3 x 45), the first night choosing the DMC and the next nights staying with it. Staff edit the
  lines afterwards like any other; each keeps ``supplier_rate_id`` as provenance.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

import asyncpg

from app import audit
from app.auth.staff import Staff
from app.errors import Conflict, Invalid, NotFound
from app.pricing import policy_values
from app.pricing.engine import BASE_CURRENCY
from app.pricing.policy_params_hotels import DEFAULT_BOARD
from app.suppliers import service as suppliers
from app.suppliers.service import clean_text
from app.supply.newest_contract import NEWEST_CONTRACT_FIRST
from app.supply.records import RATE_IN_FORCE_ON, RATE_IN_FORCE_TODAY, as_uuid

MEAL_PLANS = ("RO", "BB", "HB", "FB", "AI")
EDITABLE = (
    "season",
    "valid_from",
    "valid_to",
    "meal_plan",
    "pax_min",
    "pax_max",
    "net_amount",
    "notes",
    "pax_type",
    "age_min",
    "age_max",
    "published_amount",
    "commission_pct",
)
PAX_TYPES = ("adult", "child", "infant")
ADULT = "adult"
AGE_MAX = 17  # technical-constant: the bk-060 CHECK bound (a child or infant is under 18)
CENT = Decimal("0.01")
HUNDRED = Decimal("100")
SOURCE_NUMBERS = ("published_amount", "commission_pct")
DECIMAL_FIELDS = ("net_amount", *SOURCE_NUMBERS)
UNIT_LABELS = {"room_night": "night", "person": "pax", "group": "group", "vehicle": "vehicle", "day": "day"}
MAX_NOTE = 200

RATE_COLUMNS = f"""
    x.id::text AS id, x.supplier_contract_id::text AS contract_id, k.title AS contract_title,
    k.status AS contract_status, k.supplier_id::text AS supplier_id,
    x.hotel_room_type_id::text AS hotel_room_type_id, t.name AS room_type_name,
    h.id::text AS hotel_id, h.code AS hotel_code, h.name AS hotel_name,
    x.service_id::text AS service_id, s.code AS service_code, s.title AS service_title,
    x.season, x.valid_from, x.valid_to, x.unit, x.meal_plan, x.pax_min, x.pax_max,
    x.net_amount::text AS net_amount, x.currency::text AS currency, x.notes,
    x.pax_type, x.age_min, x.age_max, x.published_amount::text AS published_amount,
    x.commission_pct::text AS commission_pct,
    ({RATE_IN_FORCE_TODAY}) AS in_force,
    x.created_by, x.updated_by, x.created_at, x.updated_at
"""
RATE_FROM = """
    booking.supplier_rates x
    JOIN booking.supplier_contracts k ON k.id = x.supplier_contract_id
    LEFT JOIN booking.hotel_room_types t ON t.id = x.hotel_room_type_id
    LEFT JOIN booking.hotels h ON h.id = t.hotel_id
    LEFT JOIN booking.services s ON s.id = x.service_id
"""
# Inside one contract: the newest rate validity, then a rate with a pax band, then the lowest net.
RATE_ORDER = "x.valid_from DESC, (x.pax_min IS NOT NULL OR x.pax_max IS NOT NULL) DESC, x.net_amount ASC, x.id"
# Per supplier (after the board rank): the supplier's newest published contract with a matching rate first, the same
# order the availability side keeps terms by (app.supply.newest_contract, AA-1153: a re-imported contract's new price
# replaces the older contract's on the dates it covers, never "the lower net of the two"); then (bk-060) among child /
# infant rates of several age bands, the band reaching the oldest age first (a quote asks without ages, so the oldest
# child pays its band's price, never a younger band's; adult rates have no band); then RATE_ORDER.
RATE_ORDER_MANY = f"{NEWEST_CONTRACT_FIRST}, x.age_max DESC NULLS FIRST, {RATE_ORDER}"


def target_label(rate: dict[str, Any]) -> str:
    if rate.get("hotel_room_type_id"):
        return f"{rate['hotel_code']} {rate['hotel_name']} · {rate['room_type_name']}"
    return f"{rate['service_code']} {rate['service_title']}"


# ── Reads ────────────────────────────────────────────────────────────────────


async def list_rates(
    conn: asyncpg.Connection,
    *,
    contract_id: str | None = None,
    hotel_id: str | None = None,
    service_id: str | None = None,
) -> dict[str, Any]:
    """Rates of one contract, of every room type of one hotel, or of one service (newest validity first)."""
    if contract_id:
        where, arg = "x.supplier_contract_id = $1::uuid", contract_id
    elif hotel_id:
        where, arg = "t.hotel_id = $1::uuid", hotel_id
    else:
        where, arg = "x.service_id = $1::uuid", service_id
    rows = await conn.fetch(
        f"SELECT {RATE_COLUMNS} FROM {RATE_FROM} WHERE {where}"  # noqa: S608 — constants
        " ORDER BY k.valid_from DESC, t.name NULLS LAST, x.valid_from, x.meal_plan NULLS FIRST, x.pax_min NULLS FIRST",
        arg,
    )
    return {"items": [dict(r) for r in rows]}


async def get_rate(conn: asyncpg.Connection, rate_id: str) -> dict[str, Any]:
    rid = as_uuid(rate_id, "rate_not_found", "rate")
    row = await conn.fetchrow(f"SELECT {RATE_COLUMNS} FROM {RATE_FROM} WHERE x.id = $1::uuid", rid)  # noqa: S608
    if row is None:
        raise NotFound("rate_not_found", f"rate {rate_id} not found")
    return dict(row)


async def rate_in_force(
    conn: asyncpg.Connection,
    *,
    room_type_id: str | None = None,
    service_id: str | None = None,
    on: date,
    pax: int | None = None,
    pax_type: str = ADULT,
    meal_plan: str | None = None,
    supplier_id: str | None = None,
    board: str | None = None,
) -> dict[str, Any] | None:
    """The rate in force for a room type or service on ``on`` for ``pax`` travellers (``pax_type`` rates only), or
    None: the batched choice (:data:`RATES_MANY_SQL`), then the full row. ``board`` = the default board
    (:func:`rates_in_force_many`)."""
    ask = RateAsk(room_type_id, service_id, on, pax, pax_type, meal_plan, supplier_id)
    found = (await rates_in_force_many(conn, [ask], board=board))[0]
    if found is None:
        return None
    return {**(await get_rate(conn, found["id"])), "supplier_id": found["supplier_id"]}


@dataclass(frozen=True)
class RateAsk:
    """One rate lookup: a room type or a service, a day, a pax count (None = any band), a pax type (bk-060)."""

    room_type_id: str | None
    service_id: str | None
    on: date
    pax: int | None
    pax_type: str = ADULT
    meal_plan: str | None = None  # bk-069 (T9): the board asked; None = the policy's default board first
    supplier_id: str | None = None  # bk-069 (T8): only this supplier's rates (a stay keeps its DMC)


async def default_board(conn: asyncpg.Connection, *, on: date | None = None) -> str:
    """bk-069 (T9): the pricing policy's default board in force on ``on`` (today, UTC, by default):
    ``hotel_default_board``. Raises :class:`app.pricing.policy_values.PolicyUnavailable` when it has no approved
    value (a staff caller answers 503 through :func:`staff_default_board`, a B2B caller 503 ``unavailable``)."""
    day = on or await policy_values.today_utc(conn)
    return str((await policy_values.require(conn, day, DEFAULT_BOARD)).get(DEFAULT_BOARD))


async def staff_default_board(conn: asyncpg.Connection) -> str:
    """:func:`default_board` for a staff path: a missing value is 503 ``policy_unavailable``."""
    policy = await policy_values.require_staff(conn, await policy_values.today_utc(conn), DEFAULT_BOARD)
    return str(policy.get(DEFAULT_BOARD))


# The same choice for every caller (one ask or many): in force on the day, pax band, pax type; per supplier the asked
# board ($8 = the policy's default board when none is asked), then RATE_ORDER_MANY (newest published contract first);
# across suppliers the asked board, the hotel's preferred DMC, the lowest net in USD at the approved FX rate in force on
# the day (FX_SQL of app.pricing.repository, per night), then the lowest net (bk-069). A room rate counts only while
# its hotel has an active link to the contract's supplier. One round trip for many asks (AA-972 review: a B2B quote
# must not run one query per night or component).
RATES_MANY_SQL = f"""
SELECT a.n, r.id, r.unit, r.net_amount, r.currency, r.season, r.contract_id, r.age_max, r.supplier_id, r.meal_plan
FROM unnest($1::uuid[], $2::uuid[], $3::date[], $4::int[], $5::text[], $6::text[], $7::uuid[]) WITH ORDINALITY
     AS a(room_type_id, service_id, on_day, pax, pax_type, meal_plan, supplier_id, n)
LEFT JOIN LATERAL (
    SELECT s.* FROM (
        SELECT DISTINCT ON (k.supplier_id)
               x.id::text AS id, x.unit, x.net_amount::text AS net_amount, x.currency::text AS currency, x.season,
               x.supplier_contract_id::text AS contract_id, x.age_max, k.supplier_id::text AS supplier_id,
               x.meal_plan, x.net_amount AS net_sort, coalesce(l.is_preferred, false) AS preferred,
               CASE WHEN x.meal_plan = coalesce(a.meal_plan, $8::text) THEN 0
                    WHEN x.meal_plan IS NULL THEN 1 ELSE 2 END AS board_rank
        FROM booking.supplier_rates x
        JOIN booking.supplier_contracts k ON k.id = x.supplier_contract_id
        LEFT JOIN booking.hotel_room_types t ON t.id = x.hotel_room_type_id
        LEFT JOIN booking.hotel_supplier_links l
               ON l.hotel_id = t.hotel_id AND l.supplier_id = k.supplier_id AND l.status = 'active'
        WHERE (x.hotel_room_type_id = a.room_type_id OR x.service_id = a.service_id)
          AND {RATE_IN_FORCE_ON.format(day="a.on_day")}
          AND (a.pax IS NULL OR ((x.pax_min IS NULL OR x.pax_min <= a.pax)
                                 AND (x.pax_max IS NULL OR x.pax_max >= a.pax)))
          AND x.pax_type = a.pax_type
          AND (x.hotel_room_type_id IS NULL OR l.hotel_id IS NOT NULL)
          AND (a.supplier_id IS NULL OR k.supplier_id = a.supplier_id)
          AND (a.meal_plan IS NULL OR x.meal_plan IS NULL OR x.meal_plan = a.meal_plan)
        ORDER BY k.supplier_id, board_rank, {RATE_ORDER_MANY}
    ) s
    LEFT JOIN LATERAL (
        SELECT f.rate_per_usd FROM booking.fx_rates f
        JOIN booking.policy_approvals p ON p.fx_rate_id = f.id
        WHERE f.currency = s.currency AND f.as_of <= a.on_day
        ORDER BY f.as_of DESC, p.approved_at DESC, f.id DESC
        LIMIT 1
    ) fx ON s.currency <> '{BASE_CURRENCY}'
    ORDER BY s.board_rank, s.preferred DESC,
             CASE WHEN s.currency = '{BASE_CURRENCY}' THEN s.net_sort ELSE s.net_sort / fx.rate_per_usd END NULLS LAST,
             s.net_sort, s.id
    LIMIT 1
) r ON true
ORDER BY a.n
"""  # noqa: S608 — constants only


async def rates_in_force_many(
    conn: asyncpg.Connection, asks: list[RateAsk], *, board: str | None = None
) -> list[dict[str, Any] | None]:
    """The rate in force for every ask (same order; None where none), in one query. Duplicate asks are looked up once.

    ``board``: the default board of a room ask that names none (bk-069 T9); when it is None and such an ask exists it
    is read from the pricing policy (:func:`staff_default_board`, one more query; a B2B caller passes its own).

    Each found rate has ``id``, ``unit``, ``net_amount`` (text), ``currency`` (stripped), ``season`` (its label),
    ``contract_id``, ``age_max`` (bk-060, a child or infant band), ``supplier_id`` (the contract's) and ``meal_plan``
    (bk-069).
    """
    unique = list(dict.fromkeys(asks))
    if not unique:
        return []
    if board is None and any(a.room_type_id and a.meal_plan is None for a in unique):
        board = await staff_default_board(conn)
    rows = await conn.fetch(
        RATES_MANY_SQL,
        [a.room_type_id for a in unique],
        [a.service_id for a in unique],
        [a.on for a in unique],
        [a.pax for a in unique],
        [a.pax_type for a in unique],
        [a.meal_plan for a in unique],
        [a.supplier_id for a in unique],
        board,
    )
    found: dict[int, dict[str, Any]] = {}
    for row in rows:
        if row["id"] is not None:
            found[int(row["n"])] = {
                "id": row["id"],
                "unit": row["unit"],
                "net_amount": row["net_amount"],
                "currency": str(row["currency"]).strip(),
                "season": row.get("season"),
                "contract_id": row.get("contract_id"),
                "age_max": row.get("age_max"),
                "supplier_id": row.get("supplier_id"),
                "meal_plan": row.get("meal_plan"),
            }
    by_ask = {ask: found.get(i) for i, ask in enumerate(unique, start=1)}
    return [by_ask[a] for a in asks]


# ── Default net of a version item ────────────────────────────────────────────


@dataclass(frozen=True)
class DefaultCost:
    net_amount: Decimal
    net_currency: str
    supplier_note: str
    supplier_rate_id: str


def quantity(unit: str, *, day_from: int, day_to: int, pax: int | None) -> int:
    if unit == "room_night":
        return max(day_to - day_from, 1)
    if unit == "person":
        return max(pax or 1, 1)
    if unit == "day":
        return day_to - day_from + 1
    return 1


def default_cost(rate: dict[str, Any], *, day_from: int, day_to: int, pax: int | None) -> DefaultCost:
    qty = quantity(rate["unit"], day_from=day_from, day_to=day_to, pax=pax)
    unit_net = Decimal(str(rate["net_amount"]))
    amount = (unit_net * qty).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    extras = " ".join(p for p in (rate.get("meal_plan"), rate.get("season")) if p)
    unit = UNIT_LABELS.get(rate["unit"], rate["unit"])
    note = f"{target_label(rate)}{f' {extras}' if extras else ''}: {unit_net} {rate['currency']} x {qty} {unit}"
    return DefaultCost(amount, rate["currency"], note[:MAX_NOTE], rate["id"])


async def default_room_costs(
    conn: asyncpg.Connection,
    room_type_id: str,
    *,
    first_day: date,
    nights: int,
    pax: int | None,
    meal_plan: str | None = None,
    board: str | None = None,
) -> list[DefaultCost] | None:
    """AA-1153: the default net of a room item night by night: the first night's rate picks the DMC, every next night
    asks that DMC's rate in force on its own day; one :class:`DefaultCost` per run of nights at one rate. None when a
    night has no rate (nothing is priced at a guessed net). ``board`` = the default board (read once here if None)."""
    if board is None and meal_plan is None:
        board = await staff_default_board(conn)
    first = await rate_in_force(
        conn, room_type_id=room_type_id, on=first_day, pax=pax, meal_plan=meal_plan, board=board
    )
    if first is None:
        return None
    asks = [
        RateAsk(room_type_id, None, first_day + timedelta(days=i), pax, ADULT, meal_plan, first["supplier_id"])
        for i in range(1, max(nights, 1))
    ]
    ids = [first["id"]]
    for found in await rates_in_force_many(conn, asks, board=board) if asks else []:
        if found is None:
            return None
        ids.append(found["id"])
    runs: list[tuple[str, int]] = []
    for rid in ids:
        if runs and runs[-1][0] == rid:
            runs[-1] = (rid, runs[-1][1] + 1)
        else:
            runs.append((rid, 1))
    rows = {first["id"]: first}
    for rid, _ in runs:
        if rid not in rows:
            rows[rid] = await get_rate(conn, rid)
    out: list[DefaultCost] = []
    for rid, count in runs:
        rate = rows[rid]
        unit_net = Decimal(str(rate["net_amount"]))
        extras = " ".join(p for p in (rate.get("meal_plan"), rate.get("season")) if p)
        note = f"{target_label(rate)}{f' {extras}' if extras else ''}: {unit_net} {rate['currency']} x {count} night"
        amount = (unit_net * count).quantize(CENT, rounding=ROUND_HALF_UP)
        out.append(DefaultCost(amount, rate["currency"], note[:MAX_NOTE], rid))
    return out


async def item_defaults(
    conn: asyncpg.Connection,
    room_type_id: str | None,
    service_id: str | None,
    *,
    on: date,
    day_from: int,
    day_to: int,
    pax: int | None,
    board: str | None = None,
) -> list[DefaultCost] | None:
    """The default cost rows of a version item (None when no rate is in force): a room night by night
    (:func:`default_room_costs`, ``board`` = the policy's default board, read there when None), a service from its
    rate in force on the first day (:func:`default_cost`)."""
    if room_type_id:
        nights = quantity("room_night", day_from=day_from, day_to=day_to, pax=pax)
        return await default_room_costs(conn, room_type_id, first_day=on, nights=nights, pax=pax, board=board)
    rate = await rate_in_force(conn, service_id=service_id, on=on, pax=pax)
    return [default_cost(rate, day_from=day_from, day_to=day_to, pax=pax)] if rate else None


def default_report(position: int, on: date, runs: list[DefaultCost] | None) -> dict[str, Any]:
    """The ``rate_defaults`` line of one item: the first rate and the total net, or ``rate_id`` None."""
    if not runs:
        return {"position": position, "on": on, "rate_id": None}
    total = sum((d.net_amount for d in runs), start=Decimal("0"))
    return {"position": position, "on": on, "rate_id": runs[0].supplier_rate_id, "net_amount": str(total),
            "net_currency": runs[0].net_currency}  # fmt: skip


# ── Writes (draft contracts only) ────────────────────────────────────────────


async def _draft_contract(conn: asyncpg.Connection, contract_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    contract = await suppliers.get_contract(conn, contract_id, lock=True)
    if contract["status"] != "draft":
        raise Conflict(
            "contract_locked", f"rates of a {contract['status']} contract are fixed; create a new contract instead"
        )
    supplier = await suppliers.resolve_supplier(conn, contract["supplier_id"])
    return contract, supplier


def net_from_published(published: Decimal, commission_pct: Decimal) -> Decimal:
    """B7: the net AA pays on a commissionable price: published x (100 - commission %) / 100, half-up to the cent
    (70 at 20 % -> 56.00)."""
    return (published * (HUNDRED - commission_pct) / HUNDRED).quantize(CENT, rounding=ROUND_HALF_UP)


def _decimal(value: Any, code: str, what: str) -> Decimal:
    try:
        number = Decimal(str(value).strip()) if not isinstance(value, bool) else None
    except InvalidOperation:
        number = None
    if number is None or not number.is_finite() or number < 0 or number != number.quantize(CENT):
        raise Invalid(code, f"{what} is a zero or positive number with at most 2 decimals")
    return number


def commissionable(values: dict[str, Any]) -> dict[str, Any]:
    """B7: with ``published_amount`` and ``commission_pct`` set, the net is derived from them (a net sent as well
    must equal it); one without the other is 422. Returns ``values`` with ``net_amount`` filled in."""
    out = dict(values)
    published, pct = out.get("published_amount"), out.get("commission_pct")
    if published is None and pct is None:
        return out
    if published is None or pct is None:
        raise Invalid("commission_pair", "a commissionable price needs both the published price and the commission %")
    published = _decimal(published, "invalid_published_amount", "the published price")
    pct = _decimal(pct, "invalid_commission_pct", "the commission %")
    if pct >= HUNDRED:
        raise Invalid("invalid_commission_pct", "the commission % is below 100")
    net = net_from_published(published, pct)
    sent = values.get("net_amount")
    if sent is not None and Decimal(str(sent)) != net:
        raise Invalid(
            "net_mismatch",
            f"the net is {net} (published {published} less {pct} % commission); leave Net empty or match it",
        )
    out.update(published_amount=published, commission_pct=pct, net_amount=net)
    return out


def _normalize(data: dict[str, Any]) -> dict[str, Any]:
    out = dict(data)
    if "pax_type" in out:
        out["pax_type"] = str(out["pax_type"] or ADULT).strip().lower()
    for key in ("season", "notes"):
        if key in out:
            out[key] = clean_text(out[key])
    if out.get("meal_plan") is not None:
        out["meal_plan"] = str(out["meal_plan"]).strip().upper() or None
    if "net_amount" in out and out["net_amount"] is None:
        raise Invalid("net_amount_required", "a rate needs a net amount")
    if "valid_from" in out and out["valid_from"] is None:
        raise Invalid("valid_from_required", "a rate needs a start date")
    return out


def _check(merged: dict[str, Any], contract: dict[str, Any]) -> None:
    if merged.get("meal_plan") is not None:
        if not merged.get("hotel_room_type_id"):
            raise Invalid("meal_plan_rooms_only", "a meal plan applies to room rates only")
        if merged["meal_plan"] not in MEAL_PLANS:
            raise Invalid("unknown_meal_plan", f"meal plan must be one of {', '.join(MEAL_PLANS)}")
    if merged.get("valid_to") is not None and merged["valid_to"] < merged["valid_from"]:
        raise Invalid("invalid_validity", "valid_to must be on or after valid_from")
    k_from, k_to = contract["valid_from"], contract["valid_to"]
    if merged["valid_from"] < k_from or (
        k_to is not None and (merged["valid_to"] is None or merged["valid_to"] > k_to)
    ):
        raise Invalid(
            "rate_outside_contract",
            f"a rate stays inside its contract validity ({k_from} to {k_to or 'open-ended'})",
        )
    lo, hi = merged.get("pax_min"), merged.get("pax_max")
    if lo is not None and hi is not None and hi < lo:
        raise Invalid("invalid_pax_band", "pax_max must be at least pax_min")
    _check_pax_type(merged)


def _check_pax_type(merged: dict[str, Any]) -> None:
    """bk-060: child / infant rates are per person; an age band only on them, inside 0..17, max >= min."""
    pax_type = merged.get("pax_type") or ADULT
    if pax_type not in PAX_TYPES:
        raise Invalid("unknown_pax_type", f"pax type must be one of {', '.join(PAX_TYPES)}")
    if pax_type != ADULT and merged.get("unit") != "person":
        raise Invalid("pax_type_per_person", "a child or infant rate prices a per-person service")
    age_min, age_max = merged.get("age_min"), merged.get("age_max")
    if pax_type == ADULT and (age_min is not None or age_max is not None):
        raise Invalid("age_band_adult", "an age band belongs to a child or infant rate")
    for age in (age_min, age_max):
        if age is not None and (isinstance(age, bool) or not isinstance(age, int) or not 0 <= age <= AGE_MAX):
            raise Invalid("invalid_age_band", f"ages are whole numbers from 0 to {AGE_MAX}")
    if age_min is not None and age_max is not None and age_max < age_min:
        raise Invalid("invalid_age_band", "age_max must be at least age_min")


async def _touch_contract(conn: asyncpg.Connection, staff: Staff, contract_id: str) -> None:
    """The rate writer becomes the draft's maker (bk-008 maker-checker covers the rates too)."""
    await conn.execute(
        "UPDATE booking.supplier_contracts SET updated_by = $2 WHERE id = $1::uuid", contract_id, staff.cis_user_id
    )


async def _audit(
    conn: asyncpg.Connection,
    staff: Staff,
    action: str,
    rate: dict[str, Any],
    supplier: dict[str, Any],
    contract: dict[str, Any],
    *,
    before: Any = None,
    after: Any = None,
    summary: str,
) -> None:
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action=f"supplier_rate.{action}",
        entity_type="supplier_rate",
        entity_id=rate["id"],
        before=before,
        after=after,
        entity_label=f"{supplier['code']} · {contract['title']} · {target_label(rate)}",
        summary=summary,
    )


async def create_rate(conn: asyncpg.Connection, staff: Staff, contract_id: str, data: dict[str, Any]) -> dict[str, Any]:
    contract, supplier = await _draft_contract(conn, contract_id)
    room_type_id, service_id = data.get("hotel_room_type_id"), data.get("service_id")
    if (room_type_id is None) == (service_id is None):
        raise Invalid("rate_target", "a rate prices exactly one room type or one service")
    if room_type_id:
        # bk-069 (T8): a room type of any hotel linked (active) to the contract's supplier.
        target = await conn.fetchrow(
            "SELECT coalesce(l.supplier_id, h.supplier_id)::text AS supplier_id, 'room_night' AS unit"
            " FROM booking.hotel_room_types t JOIN booking.hotels h ON h.id = t.hotel_id"
            " LEFT JOIN booking.hotel_supplier_links l ON l.hotel_id = h.id AND l.supplier_id = $2::uuid"
            " AND l.status = 'active' WHERE t.id = $1::uuid",
            str(room_type_id),
            contract["supplier_id"],
        )
    else:
        target = await conn.fetchrow(
            "SELECT s.supplier_id::text AS supplier_id, s.unit FROM booking.services s WHERE s.id = $1::uuid",
            str(service_id),
        )
    if target is None:
        raise Invalid("unknown_rate_target", "the room type or service does not exist")
    if target["supplier_id"] != contract["supplier_id"]:
        raise Invalid(
            "rate_supplier_mismatch",
            "a rate prices a service of the contract's supplier or a room type of a hotel linked to it",
        )
    # An omitted (or null) start takes the contract's; an omitted end too (null = open-ended, if the contract is).
    values = _normalize({k: v for k, v in data.items() if k in EDITABLE and not (k == "valid_from" and v is None)})
    values = commissionable({k: v for k, v in values.items() if v is not None or k not in SOURCE_NUMBERS})
    if values.get("net_amount") is None:
        raise Invalid("net_amount_required", "a rate needs a net amount")
    values.setdefault("valid_from", contract["valid_from"])
    if "valid_to" not in values:
        values["valid_to"] = contract["valid_to"]
    _check({**values, "hotel_room_type_id": room_type_id, "unit": target["unit"]}, contract)
    columns = list(values)
    marks = ", ".join(f"${i + 7}" for i in range(len(columns)))
    try:
        rid = await conn.fetchval(
            "INSERT INTO booking.supplier_rates (supplier_contract_id, hotel_room_type_id, service_id, unit, currency,"  # noqa: S608
            f" created_by, updated_by, {', '.join(columns)})"
            f" VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5, $6, $6, {marks}) RETURNING id::text",
            contract["id"],
            str(room_type_id) if room_type_id else None,
            str(service_id) if service_id else None,
            target["unit"],
            contract["currency"],
            staff.cis_user_id,
            *values.values(),
        )
    except asyncpg.CheckViolationError as exc:  # the DB backstop (guard trigger and CHECKs)
        raise Invalid("invalid_rate", str(exc)) from exc
    await _touch_contract(conn, staff, contract["id"])
    rate = await get_rate(conn, rid)
    await _audit(
        conn,
        staff,
        "created",
        rate,
        supplier,
        contract,
        after={k: rate.get(k) for k in ("hotel_room_type_id", "service_id", "unit", "currency", *EDITABLE)},
        summary=f"Added rate {rate['net_amount']} {rate['currency']} per {UNIT_LABELS[rate['unit']]}",
    )
    return rate


async def update_rate(conn: asyncpg.Connection, staff: Staff, rate_id: str, changes: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(changes) - set(EDITABLE))
    if unknown:
        raise Invalid("not_editable", f"not editable: {', '.join(unknown)}")
    current = await get_rate(conn, rate_id)
    contract, supplier = await _draft_contract(conn, current["contract_id"])
    values = _normalize(changes)
    # B7: while the rate keeps its source numbers, the net follows them (a net change alone must still match).
    source = {k: values.get(k, current.get(k)) for k in SOURCE_NUMBERS}
    if any(v is not None for v in source.values()):
        net = {"net_amount": values["net_amount"]} if "net_amount" in values else {}
        values.update(commissionable({**source, **net}))
    for key in DECIMAL_FIELDS:
        if values.get(key) is not None:
            values[key] = Decimal(str(values[key]))
            if current.get(key) is not None and values[key] == Decimal(str(current[key])):
                values.pop(key)
        elif key in values and current.get(key) is None:
            values.pop(key)
    changed = {k: v for k, v in values.items() if k in DECIMAL_FIELDS or current.get(k) != v}
    if not changed:
        return {**current, "changed": []}
    _check({**current, **changed}, contract)
    sets = ", ".join(f"{column} = ${i + 2}" for i, column in enumerate(changed))
    try:
        await conn.execute(
            f"UPDATE booking.supplier_rates SET {sets}, updated_by = ${len(changed) + 2} WHERE id = $1::uuid",  # noqa: S608
            current["id"],
            *changed.values(),
            staff.cis_user_id,
        )
    except asyncpg.CheckViolationError as exc:
        raise Invalid("invalid_rate", str(exc)) from exc
    await _touch_contract(conn, staff, contract["id"])
    await _audit(
        conn,
        staff,
        "updated",
        current,
        supplier,
        contract,
        before={k: current[k] for k in changed},
        after=changed,
        summary=f"Updated rate {', '.join(sorted(changed))}",
    )
    return {**(await get_rate(conn, current["id"])), "changed": sorted(changed)}


async def delete_rate(conn: asyncpg.Connection, staff: Staff, rate_id: str) -> None:
    current = await get_rate(conn, rate_id)
    contract, supplier = await _draft_contract(conn, current["contract_id"])
    try:
        await conn.execute("DELETE FROM booking.supplier_rates WHERE id = $1::uuid", current["id"])
    except asyncpg.ForeignKeyViolationError as exc:  # a cost line names it (only possible for a published rate)
        raise Conflict("rate_in_use", "a proposal cost line uses this rate") from exc
    await _touch_contract(conn, staff, contract["id"])
    await _audit(
        conn,
        staff,
        "deleted",
        current,
        supplier,
        contract,
        before={k: current.get(k) for k in ("hotel_room_type_id", "service_id", "unit", "currency", *EDITABLE)},
        summary=f"Removed rate {current['net_amount']} {current['currency']}",
    )
