"""B2B holds and their confirmation (AA-973, bk-020; contract booking.b2b.v1 ``/holds``).

- **Place** (``POST /holds``, scope ``hold``): the caller presents ``quote_id`` + ``price_hash`` for one proposal
  version. Idempotent twice over: on the ``Idempotency-Key`` (unique per partner; the same key for another request is
  422 ``invalid``) and on the proposal version (one ``active`` or ``confirmed`` set per ``(partner, proposal_ref,
  proposal_version)``: the same quote again returns that hold, another quote is 422 ``invalid`` until the first is
  released). A hash that is not the quote's, or a quote past ``valid_until``, is 409 ``price_changed`` (re-quote; the
  quote is honoured as quoted within its validity, no re-pricing). The departures of the lines are locked
  (``app.b2b.departures.lock_departures``) and counted; a line that does not fit is 422 ``sold_out`` and nothing is
  held. Length: setting ``b2b_hold_hours`` (48), capped by the earliest departure start.
- **Read / release** (``GET``, ``DELETE /holds/{id}``): another partner's hold is 404. An ``active`` hold past
  ``expires_at`` reads as ``expired`` (the expiry job writes it later, ``app.b2b.expiry``). Release: active →
  ``released``; released / expired → no-op; confirmed → 422 (cancel the booking instead, AA-974).
- **Confirm** (``POST /holds/{id}/confirm``, scope ``book``; memo seam 1): the only place Booking creates a commercial
  record for B2B. Expired / released → 410 ``hold_expired``; wrong hash → 409 ``price_changed``; a cancelled
  departure → 422 ``sold_out``; already confirmed → the same booking. Otherwise, in one transaction: a **B2B trip
  case** (``channel = 'b2b'``, no customer, ``party_tenant_id`` = the agency, ``source_system = 'crm'``, status
  ``confirmed``, ``idempotency_key = b2b_hold:<id>``), **one approved version** (channel ``b2b``, the quote's total,
  currency and FX lock) with one item and one cost row per line (net from the quote, never shown to the agency), the
  hold lines ``available`` → ``confirmed`` (``on_request`` stays for the AA-975 desk), the hold ``confirmed``.

No personal data anywhere: seats and pax are counts, the proposal ref and deposit ref are opaque CRM ids.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import asyncpg

from app import audit
from app.b2b import departures
from app.b2b.components import PREFIX_PLAN, PREFIX_ROOM, PREFIX_SERVICE, parse_component
from app.b2b.errors import B2BError

log = logging.getLogger(__name__)

HOLD_HOURS_KEY = "b2b_hold_hours"
DEFAULT_HOLD_HOURS = 48
MAX_HOLD_HOURS = 720
CASE_KEY_PREFIX = "b2b_hold:"
ACTOR_PREFIX = "m2m:"

EFFECTIVE_STATUS = "CASE WHEN h.status = 'active' AND h.expires_at <= now() THEN 'expired' ELSE h.status END"
HOLD_SQL = f"""
SELECT h.id::text AS id, h.partner_id::text AS partner_id, h.quote_id::text AS quote_id, h.proposal_ref,
       h.proposal_version, h.idempotency_key, h.status AS stored_status, {EFFECTIVE_STATUS} AS status, h.expires_at,
       h.extended_count, h.trip_case_id::text AS trip_case_id, h.deposit_ref
FROM booking.b2b_holds h
"""  # noqa: S608 — constant fragment
LINES_SQL = """
SELECT line_no, line_ref, departure_id::text AS departure_id, seats, held_as, status
FROM booking.b2b_hold_lines WHERE hold_id = $1::uuid ORDER BY line_no
"""
QUOTE_SQL = """
SELECT id::text AS id, proposal_ref, proposal_version, currency::text AS currency, fx_rate_per_usd, fx_as_of,
       total_b2b_price, price_hash::text AS price_hash, valid_until
FROM booking.b2b_quotes WHERE id = $1::uuid AND partner_id = $2::uuid
"""
QUOTE_LINES_SQL = """
SELECT line_no, line_ref, component_id, component_kind, service_date, nights, pax_adult, pax_child,
       departure_id::text AS departure_id, net_cost_usd, supplier_rate_ids::text[] AS supplier_rate_ids
FROM booking.b2b_quote_lines WHERE quote_id = $1::uuid ORDER BY line_no
"""


@dataclass(frozen=True)
class Caller:
    """Who acts: the partner row id, its agency tenant and the M2M client (``app.b2b.access.B2BContext``)."""

    partner_id: str
    tenant_id: str
    client_id: str

    @property
    def actor(self) -> str:
        return f"{ACTOR_PREFIX}{self.client_id}"


def _id(value: str) -> str | None:
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


async def _hold_hours(conn: asyncpg.Connection) -> int:
    raw = await conn.fetchval("SELECT value FROM booking.settings WHERE key = $1", HOLD_HOURS_KEY)
    try:
        hours = int(json.loads(raw)) if isinstance(raw, str) else int(raw)
    except (TypeError, ValueError):
        return DEFAULT_HOLD_HOURS
    return hours if 1 <= hours <= MAX_HOLD_HOURS else DEFAULT_HOLD_HOURS


async def _hold_out(conn: asyncpg.Connection, hold: Any) -> dict[str, Any]:
    lines = await conn.fetch(LINES_SQL, hold["id"])
    return {
        "hold_id": hold["id"],
        "quote_id": hold["quote_id"],
        "status": hold["status"],
        "expires_at": hold["expires_at"].isoformat(),
        "extended_count": int(hold["extended_count"]),
        "lines": [{"line_ref": r["line_ref"], "line_status": r["held_as"]} for r in lines],
    }


async def _find(conn: asyncpg.Connection, caller: Caller, hold_id: str, *, lock: bool = False) -> Any:
    ident = _id(hold_id)
    row = None
    if ident is not None:
        row = await conn.fetchrow(
            HOLD_SQL + " WHERE h.id = $1::uuid AND h.partner_id = $2::uuid" + (" FOR UPDATE" if lock else ""),
            ident,
            caller.partner_id,
        )
    if row is None:
        raise B2BError("not_found", "hold not found")
    return row


async def _quote(conn: asyncpg.Connection, caller: Caller, quote_id: str) -> Any:
    ident = _id(quote_id)
    row = await conn.fetchrow(QUOTE_SQL, ident, caller.partner_id) if ident else None
    if row is None:
        raise B2BError("not_found", "quote not found")
    return row


def _same_hash(quote: Any, price_hash: str) -> None:
    if quote["price_hash"] != price_hash:
        raise B2BError(
            "price_changed",
            "price_hash is not the quote's; re-quote or present the quote's hash",
            price_hash=quote["price_hash"],
        )


# ── Place ────────────────────────────────────────────────────────────────────


async def create_hold(
    pool: asyncpg.Pool,
    caller: Caller,
    *,
    quote_id: str,
    price_hash: str,
    proposal_ref: str,
    proposal_version: int,
    idempotency_key: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Place (or return) the hold set of one proposal version; the contract ``Hold`` body. Raises B2BError."""
    for attempt in (1, 2):
        try:
            async with pool.acquire() as conn, conn.transaction():
                return await _create(
                    conn,
                    caller,
                    quote_id=quote_id,
                    price_hash=price_hash,
                    proposal_ref=proposal_ref,
                    proposal_version=proposal_version,
                    idempotency_key=idempotency_key,
                    now=now or datetime.now(UTC),
                )
        except asyncpg.UniqueViolationError:
            # A concurrent request with the same key or for the same proposal version won the insert: the second pass
            # answers from the winner (same request → its hold; another request → 422).
            if attempt == 2:
                raise
    raise AssertionError("unreachable")  # pragma: no cover


async def _create(
    conn: asyncpg.Connection,
    caller: Caller,
    *,
    quote_id: str,
    price_hash: str,
    proposal_ref: str,
    proposal_version: int,
    idempotency_key: str,
    now: datetime,
) -> dict[str, Any]:
    replay = await conn.fetchrow(
        HOLD_SQL + " WHERE h.partner_id = $1::uuid AND h.idempotency_key = $2", caller.partner_id, idempotency_key
    )
    if replay is not None:
        if (replay["quote_id"], replay["proposal_ref"], replay["proposal_version"]) != (
            _id(quote_id),
            proposal_ref,
            proposal_version,
        ):
            raise B2BError("invalid", "this Idempotency-Key was already used for another request")
        return await _hold_out(conn, replay)

    quote = await _quote(conn, caller, quote_id)
    if (quote["proposal_ref"], quote["proposal_version"]) != (proposal_ref, proposal_version):
        raise B2BError("invalid", "proposal_ref / proposal_version are not the quote's")
    _same_hash(quote, price_hash)
    if quote["valid_until"] <= now:
        raise B2BError("price_changed", "the quote has expired; re-quote", price_hash=quote["price_hash"])

    # Write down holds of this proposal version that are already past their expiry, so they free the version.
    await conn.execute(
        "UPDATE booking.b2b_holds SET status = 'expired', expired_at = now() WHERE partner_id = $1::uuid"
        " AND proposal_ref = $2 AND proposal_version = $3 AND status = 'active' AND expires_at <= now()",
        caller.partner_id,
        proposal_ref,
        proposal_version,
    )
    current = await conn.fetchrow(
        HOLD_SQL + " WHERE h.partner_id = $1::uuid AND h.proposal_ref = $2 AND h.proposal_version = $3"
        " AND h.status IN ('active', 'confirmed') FOR UPDATE",
        caller.partner_id,
        proposal_ref,
        proposal_version,
    )
    if current is not None:
        if current["quote_id"] != quote["id"]:
            raise B2BError(
                "invalid",
                f"hold {current['id']} already holds this proposal version ({current['stored_status']});"
                " release it before holding another quote",
            )
        return await _hold_out(conn, current)

    lines = await conn.fetch(QUOTE_LINES_SQL, quote["id"])
    asks = [
        departures.SeatAsk(
            line_ref=r["line_ref"],
            component_id=r["component_id"],
            service_date=r["service_date"],
            departure_id=r["departure_id"],
            seats=departures.seats(r["pax_adult"], r["pax_child"]),
        )
        for r in lines
    ]
    result, locked = await departures.lock_departures(conn, asks, today=now.date())
    for ask, (availability, _, why) in zip(asks, result, strict=True):
        if availability == departures.SOLD_OUT:
            raise B2BError("sold_out", f"line {ask.line_ref}: {why}")
    expires_at = now + timedelta(hours=await _hold_hours(conn))
    for dep_id in {d for _, d, _ in result if d}:
        expires_at = min(expires_at, departures.start_of(locked[dep_id]["start_date"]))

    hold_id = await conn.fetchval(
        "INSERT INTO booking.b2b_holds (partner_id, m2m_client_id, quote_id, proposal_ref, proposal_version,"
        " idempotency_key, expires_at, created_at) VALUES ($1::uuid, $2, $3::uuid, $4, $5, $6, $7, $8)"
        " RETURNING id::text",
        caller.partner_id,
        caller.client_id,
        quote["id"],
        proposal_ref,
        proposal_version,
        idempotency_key,
        expires_at,
        now,
    )
    await conn.executemany(
        "INSERT INTO booking.b2b_hold_lines (hold_id, line_no, line_ref, departure_id, seats, held_as, status)"
        " VALUES ($1::uuid, $2, $3, $4::uuid, $5, $6, $6)",
        [
            (hold_id, r["line_no"], ask.line_ref, dep_id, ask.seats, availability)
            for r, ask, (availability, dep_id, _) in zip(lines, asks, result, strict=True)
        ],
    )
    await audit.record(
        conn,
        actor_type="system",
        actor_ref=caller.actor,
        action="b2b_hold.placed",
        entity_type="b2b_hold",
        entity_id=hold_id,
        after={
            "quote_id": quote["id"],
            "proposal_version": proposal_version,
            "expires_at": expires_at,
            "lines": [
                {"line_ref": a.line_ref, "held_as": av, "departure_id": d}
                for a, (av, d, _) in zip(asks, result, strict=True)
            ],
        },
        entity_label=f"hold {hold_id[:8]}",
        summary=f"B2B hold of {len(lines)} lines until {expires_at.isoformat(timespec='minutes')}",
    )
    log.info("b2b_hold_placed hold=%s partner=%s lines=%s", hold_id, caller.partner_id, len(lines))
    return await _hold_out(conn, await conn.fetchrow(HOLD_SQL + " WHERE h.id = $1::uuid", hold_id))


# ── Read / release ───────────────────────────────────────────────────────────


async def get_hold(pool: asyncpg.Pool, caller: Caller, hold_id: str) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await _hold_out(conn, await _find(conn, caller, hold_id))


async def release_hold(pool: asyncpg.Pool, caller: Caller, hold_id: str) -> None:
    async with pool.acquire() as conn, conn.transaction():
        hold = await _find(conn, caller, hold_id, lock=True)
        if hold["stored_status"] == "confirmed":
            raise B2BError("invalid", "the hold is confirmed; cancel the booking instead")
        if hold["status"] != "active":
            if hold["stored_status"] == "active":  # past expiry, not yet written by the job
                await conn.execute(
                    "UPDATE booking.b2b_holds SET status = 'expired', expired_at = now() WHERE id = $1::uuid",
                    hold["id"],
                )
            return
        await conn.execute(
            "UPDATE booking.b2b_holds SET status = 'released', released_at = now() WHERE id = $1::uuid", hold["id"]
        )
        await audit.record(
            conn,
            actor_type="system",
            actor_ref=caller.actor,
            action="b2b_hold.released",
            entity_type="b2b_hold",
            entity_id=hold["id"],
            before={"status": "active"},
            after={"status": "released"},
            entity_label=f"hold {hold['id'][:8]}",
            summary="B2B hold released by the agency",
        )
    log.info("b2b_hold_released hold=%s partner=%s", hold["id"], caller.partner_id)


# ── Confirm → B2B trip case + approved version ───────────────────────────────


async def _booking_out(conn: asyncpg.Connection, hold: Any) -> dict[str, Any]:
    code = await conn.fetchval("SELECT code FROM booking.trip_cases WHERE id = $1::uuid", hold["trip_case_id"])
    lines = await conn.fetch(LINES_SQL, hold["id"])
    return {
        "booking_id": hold["trip_case_id"],
        "hold_id": hold["id"],
        "trip_case_code": code,
        "lines": [{"line_ref": r["line_ref"], "status": r["status"]} for r in lines],
    }


def _span(line: Any, plan_days: dict[str, int]) -> int:
    prefix, ident = parse_component(line["component_id"])
    if prefix == PREFIX_PLAN:
        return max(plan_days.get(ident, 1), 1)
    return max(int(line["nights"] or 1), 1)


def _cents(value: Decimal) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


async def confirm_hold(
    pool: asyncpg.Pool,
    caller: Caller,
    hold_id: str,
    *,
    price_hash: str,
    deposit_ref: str | None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Confirm an active hold (contract ``Booking`` body). Idempotent on the hold itself: a confirmed hold returns its
    booking whatever ``Idempotency-Key`` the retry carries (the key is kept in the audit row)."""
    async with pool.acquire() as conn, conn.transaction():
        hold = await _find(conn, caller, hold_id, lock=True)
        quote = await _quote(conn, caller, hold["quote_id"])
        _same_hash(quote, price_hash)
        if hold["stored_status"] == "confirmed":
            return await _booking_out(conn, hold)
        if hold["status"] != "active":
            raise B2BError("hold_expired", f"the hold is {hold['status']}; place a new hold")
        lines = await conn.fetch(QUOTE_LINES_SQL, quote["id"])
        held = {r["line_no"]: r for r in await conn.fetch(LINES_SQL, hold["id"])}
        dep_ids = sorted({r["departure_id"] for r in held.values() if r["departure_id"]})
        if dep_ids:
            rows = await conn.fetch(departures.LOCK_SQL, dep_ids)
            for row in rows:
                if row["status"] == "cancelled":
                    raise B2BError("sold_out", f"departure {row['id']} was cancelled; the hold cannot be confirmed")
        case_id, code = await _open_case(conn, caller, hold, quote, lines)
        await conn.execute(
            "UPDATE booking.b2b_hold_lines SET status = 'confirmed' WHERE hold_id = $1::uuid AND held_as = 'available'",
            hold["id"],
        )
        await conn.execute(
            "UPDATE booking.b2b_holds SET status = 'confirmed', confirmed_at = now(), trip_case_id = $2::uuid,"
            " deposit_ref = $3 WHERE id = $1::uuid",
            hold["id"],
            case_id,
            deposit_ref,
        )
        await audit.record(
            conn,
            actor_type="system",
            actor_ref=caller.actor,
            action="b2b_hold.confirmed",
            entity_type="b2b_hold",
            entity_id=hold["id"],
            before={"status": "active"},
            after={
                "status": "confirmed",
                "trip_case_id": case_id,
                "deposit_ref": deposit_ref,
                "idempotency_key": idempotency_key,
            },
            entity_label=code,
            summary=f"B2B hold confirmed as {code}",
        )
        confirmed = await conn.fetchrow(HOLD_SQL + " WHERE h.id = $1::uuid", hold["id"])
        out = await _booking_out(conn, confirmed)
    log.info("b2b_hold_confirmed hold=%s trip_case=%s partner=%s", hold["id"], code, caller.partner_id)
    return out


async def _open_case(
    conn: asyncpg.Connection, caller: Caller, hold: Any, quote: Any, lines: list[Any]
) -> tuple[str, str]:
    """The B2B trip case + its approved V1 (items, costs). Returns (case id, code)."""
    plan_ids = sorted(
        {ident for prefix, ident in (parse_component(r["component_id"]) for r in lines) if prefix == PREFIX_PLAN}
    )
    plan_days: dict[str, int] = {}
    plan_source: dict[str, str] = {}
    if plan_ids:
        for row in await conn.fetch(
            "SELECT p.id::text AS id, p.source_id, coalesce(max(c.day_no), 1) AS days FROM booking.trip_plans p"
            " LEFT JOIN booking.trip_plan_components c ON c.trip_plan_id = p.id WHERE p.id = ANY($1::uuid[])"
            " GROUP BY p.id, p.source_id",
            plan_ids,
        ):
            plan_days[row["id"]], plan_source[row["id"]] = int(row["days"]), row["source_id"]
    start = min(r["service_date"] for r in lines)
    end = max(r["service_date"] + timedelta(days=_span(r, plan_days) - 1) for r in lines)
    biggest = max(lines, key=lambda r: (r["pax_adult"] + r["pax_child"], r["pax_adult"]))

    # The status history's changed_by is a staff key (FK): a machine change is a system change (empty actor); the
    # note names the M2M client and the hold.
    await conn.execute("SELECT set_config('booking.actor', '', true)")
    await conn.execute(
        "SELECT set_config('booking.status_note', $1, true)", f"B2B confirm by {caller.actor} of hold {hold['id']}"
    )
    case = await conn.fetchrow(
        "INSERT INTO booking.trip_cases (customer_id, status, source_system, source_id, channel, party_tenant_id,"
        " start_date, end_date, pax, pax_adults, pax_children, currency, idempotency_key)"
        " VALUES (NULL, 'confirmed', 'crm', $1, 'b2b', $2::uuid, $3, $4, $5, $6, $7, $8, $9)"
        " RETURNING id::text AS id, code",
        hold["proposal_ref"],
        caller.tenant_id,
        start,
        end,
        biggest["pax_adult"] + biggest["pax_child"],
        biggest["pax_adult"],
        biggest["pax_child"],
        quote["currency"],
        f"{CASE_KEY_PREFIX}{hold['id']}",
    )
    version_id = await conn.fetchval(
        "INSERT INTO booking.trip_case_versions (trip_case_id, version_number, title, total_price, currency, channel,"
        " valid_until, fx_rate_locked, fx_as_of) VALUES ($1::uuid, 1, $2, $3, $4, 'b2b', $5, $6, $7)"
        " RETURNING id::text",
        case["id"],
        f"B2B {hold['proposal_ref']} v{hold['proposal_version']}"[:200],
        quote["total_b2b_price"],
        quote["currency"],
        quote["valid_until"].date(),
        quote["fx_rate_per_usd"],
        quote["fx_as_of"],
    )
    for line in lines:
        prefix, ident = parse_component(line["component_id"])
        day_from = (line["service_date"] - start).days + 1
        item_id = await conn.fetchval(
            "INSERT INTO booking.trip_case_version_itinerary (trip_case_version_id, position, title, day_from, day_to,"
            " source_tour_id, hotel_room_type_id, service_id) VALUES ($1::uuid, $2, $3, $4, $5, $6, $7::uuid, $8::uuid)"
            " RETURNING id::text",
            version_id,
            line["line_no"],
            f"{line['component_kind']} {line['component_id']} · line {line['line_ref']}"[:500],
            day_from,
            day_from + _span(line, plan_days) - 1,
            plan_source.get(ident) if prefix == PREFIX_PLAN else None,
            ident if prefix == PREFIX_ROOM else None,
            ident if prefix == PREFIX_SERVICE else None,
        )
        rate_ids = list(line["supplier_rate_ids"] or [])
        await conn.execute(
            "INSERT INTO booking.version_item_costs (version_item_id, net_amount, net_currency, supplier_note,"
            " supplier_rate_id) VALUES ($1::uuid, $2, 'USD', $3, $4::uuid)",
            item_id,
            _cents(line["net_cost_usd"]),
            f"B2B quote {quote['id']} line {line['line_no']} ({len(rate_ids)} supplier rates, USD at the plain rate)",
            rate_ids[0] if len(set(rate_ids)) == 1 else None,
        )
    # Approved last: the bk-003 / bk-004 locks refuse item and cost writes on an approved version.
    await conn.execute(
        "UPDATE booking.trip_case_versions SET is_approved = true, approved_at = now(), approval_note = $2"
        " WHERE id = $1::uuid",
        version_id,
        f"Approved by the B2B confirm of hold {hold['id']}",
    )
    await audit.record(
        conn,
        actor_type="system",
        actor_ref=caller.actor,
        action="trip_case.created",
        entity_type="trip_case",
        entity_id=case["id"],
        after={
            "channel": "b2b",
            "party_tenant_id": caller.tenant_id,
            "status": "confirmed",
            "source_system": "crm",
            "hold_id": hold["id"],
            "quote_id": quote["id"],
            "lines": len(lines),
        },
        entity_label=case["code"],
        summary=f"Created {case['code']} (B2B, confirmed) from hold {hold['id'][:8]}",
    )
    return case["id"], case["code"]
